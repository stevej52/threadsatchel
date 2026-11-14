"""Persistent HTTP integration with synthetic sockets; no external services."""
from contextlib import ExitStack
import http.client
import io
import socket
import ssl
import threading
import time
import unittest
from unittest import mock
import urllib.error

from connector import connector
from connector.test_connector_transport import RecordingTLS


CONFIG = {"endpoint": "https://gateway.example/api/agent", "key": "test-key", "access_token": "test-token"}


def wire(body=b"{}", *, status=200, headers=b"", length=True):
    framing = b"Content-Length: " + str(len(body)).encode() + b"\r\n" if length else b""
    return b"HTTP/1.1 " + str(status).encode() + b" Test\r\n" + framing + headers + b"\r\n" + body


class MemorySocket:
    def __init__(self, replies=None):
        self.replies = list(replies if replies is not None else [wire(), wire()])
        self.sent, self.timeouts = [], []
        self.closed = False
        self.shutdowns = 0
        self.fail_send = False

    def sendall(self, data):
        if self.fail_send:
            raise BrokenPipeError("synthetic stale socket")
        self.sent.append(bytes(data))

    def makefile(self, *args, **kwargs):
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            class FailedRead(io.BytesIO):
                def readline(self, *args):
                    raise reply
            return FailedRead()
        return io.BytesIO(reply)

    def setsockopt(self, *args):
        pass

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def shutdown(self, how):
        self.shutdowns += 1

    def close(self):
        self.closed = True


class ReuseTests(unittest.TestCase):
    def setUp(self):
        connector.close_thread_transport()
        self.addCleanup(connector.close_thread_transport)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.context = RecordingTLS()
        self.sockets = []
        self.connections = []
        self.next_replies = []
        connection_class = connector.RacingHTTPSConnection

        def make_connection(*args, **kwargs):
            connection = connection_class(*args, context=self.context, **kwargs)
            self.connections.append((threading.get_ident(), connection))
            return connection

        def dial(*args, **kwargs):
            sock = MemorySocket(self.next_replies.pop(0) if self.next_replies else None)
            self.sockets.append(sock)
            return sock

        self.stack.enter_context(mock.patch.object(connector.urllib.request, "getproxies", return_value={}))
        self.stack.enter_context(mock.patch.object(connector, "RacingHTTPSConnection", side_effect=make_connection))
        self.dial = self.stack.enter_context(mock.patch.object(connector, "racing_connection", side_effect=dial))

    def post(self, **kwargs):
        return connector.post(CONFIG, {"action": "maintenance", "mode": "status"}, **kwargs)

    def test_two_posts_reuse_one_tls_connection_and_refresh_credentials(self):
        self.assertEqual(self.post(), {})
        updated = {**CONFIG, "key": "new-key", "access_token": "new-token"}
        self.assertEqual(connector.post(updated, {"action": "poll"}), {})
        self.assertEqual(self.dial.call_count, 1)
        self.assertEqual(self.context.hostnames, ["gateway.example"])
        sent = b"".join(self.sockets[0].sent)
        self.assertEqual(sent.count(b"POST /api/agent HTTP/1.1"), 2)
        self.assertEqual(sent.count(b"X-ThreadSatchel-Key: test-key"), 1)
        self.assertEqual(sent.count(b"X-ThreadSatchel-Key: new-key"), 1)
        self.assertEqual(sent.count(b"Bearer new-token"), 1)
        self.assertNotIn(b"Connection: close", sent)
        self.assertFalse(self.sockets[0].closed)

    def test_stale_socket_failure_is_not_replayed_and_next_call_can_connect(self):
        self.post()
        self.sockets[0].fail_send = True
        with self.assertRaises(BrokenPipeError):
            self.post()
        self.assertEqual(self.dial.call_count, 1)
        self.assertTrue(self.sockets[0].closed)
        self.assertIsNone(connector._transport.connection)
        self.assertEqual(self.post(), {})
        self.assertEqual(self.dial.call_count, 2)

    def test_ambiguous_read_failure_never_replays_post(self):
        self.next_replies = [[ConnectionResetError("synthetic response reset")]]
        with self.assertRaises(ConnectionResetError):
            self.post()
        self.assertEqual(self.dial.call_count, 1)
        self.assertEqual(b"".join(self.sockets[0].sent).count(b"POST /api/agent"), 1)
        self.assertTrue(self.sockets[0].closed)

    def test_idle_socket_is_discarded_before_next_post(self):
        self.post()
        connector._transport.last_used -= connector.KEEPALIVE_MAX_IDLE
        self.post()
        self.assertEqual(self.dial.call_count, 2)
        self.assertTrue(self.sockets[0].closed)

    def test_endpoint_change_discards_connection(self):
        self.post()
        connector.post({**CONFIG, "endpoint": "https://other.example/api/agent"}, {})
        self.assertEqual(self.dial.call_count, 2)
        self.assertEqual(self.context.hostnames, ["gateway.example", "other.example"])
        self.assertTrue(self.sockets[0].closed)

    def test_server_close_prevents_reuse(self):
        self.next_replies = [[wire(headers=b"Connection: close\r\n")]]
        self.post()
        self.assertTrue(self.sockets[0].closed)
        self.post()
        self.assertEqual(self.dial.call_count, 2)

    def test_error_status_redirect_json_and_framing_failures_discard(self):
        cases = [
            (wire(status=503), urllib.error.HTTPError),
            (wire(status=302, headers=b"Location: https://other.example/\r\n"), ValueError),
            (wire(b"invalid json"), ValueError),
            (b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\n{}", http.client.IncompleteRead),
            (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n", http.client.IncompleteRead),
            (b"not an HTTP response\r\n\r\n", http.client.BadStatusLine),
        ]
        for reply, error in cases:
            with self.subTest(error=error, reply=reply[:40]):
                self.next_replies = [[reply]]
                with self.assertRaises(error) as raised:
                    self.post()
                self.assertTrue(self.sockets[-1].closed)
                self.assertIsNone(connector._transport.connection)
                if error is urllib.error.HTTPError:
                    detail = connector.error_detail(raised.exception)
                    self.assertEqual(detail["http_status"], 503)
                    self.assertNotIn("test-key", str(detail))

    def test_oversized_declared_and_unframed_bodies_are_bounded(self):
        with mock.patch.object(connector, "MAX_RESPONSE", 128):
            for reply in [b"HTTP/1.1 200 OK\r\nContent-Length: 129\r\n\r\n", wire(b"x" * 129, length=False)]:
                with self.subTest(reply=reply[:40]):
                    self.next_replies = [[reply]]
                    with self.assertRaisesRegex(ValueError, "Gateway response too large"):
                        self.post()
                    self.assertTrue(self.sockets[-1].closed)

    def test_main_and_worker_connections_are_independent_and_cleanup(self):
        self.post()
        main_socket = self.sockets[0]
        ready, release = threading.Event(), threading.Event()
        errors = []

        def worker():
            try:
                self.post()
                self.post()
                ready.set()
                release.wait(2)
            except BaseException as exc:
                errors.append(exc)
            finally:
                connector.close_thread_transport()

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(ready.wait(2))
            self.post()
            self.assertEqual(self.dial.call_count, 2)
            self.assertEqual(len(set(owner for owner, _ in self.connections)), 2)
            self.assertFalse(main_socket.closed)
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(self.sockets[1].closed)
        self.assertFalse(main_socket.closed)

    def test_proxy_uses_existing_handler_and_discards_direct_connection(self):
        self.post()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"{}"
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(connector.urllib.request, "getproxies", return_value={"https": "http://proxy.example"}), \
                mock.patch.object(connector.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(self.post(), {})
        self.assertEqual(self.dial.call_count, 1)
        self.assertTrue(self.sockets[0].closed)
        self.assertIsInstance(build.call_args.args[0], connector.NoRedirect)
        self.assertIsInstance(build.call_args.args[1], connector.RacingHTTPSHandler)
        opener.open.assert_called_once()

    def test_heartbeat_finally_closes_its_thread_transport(self):
        heartbeat = connector.BusyHeartbeat(CONFIG, mock.Mock(), {})
        with mock.patch.object(heartbeat, "_run_heartbeat", side_effect=lambda: self.post()):
            heartbeat._run()
        self.assertTrue(self.sockets[0].closed)

    def test_disarmed_timer_cannot_close_a_reused_connection(self):
        self.post()
        connection = connector._transport.connection
        old_deadline = connector.RequestDeadline(connection, 1)
        old_deadline.stop()
        old_deadline._expire()
        self.assertFalse(old_deadline.expired)
        self.assertEqual(self.sockets[0].shutdowns, 0)
        self.post()
        self.assertEqual(self.dial.call_count, 1)

    def test_request_timeout_does_not_reset_after_tcp(self):
        clock = [0.0]
        original_dial = self.dial.side_effect

        def slow_dial(*args, **kwargs):
            clock[0] += .4
            return original_dial(*args, **kwargs)

        self.dial.side_effect = slow_dial
        with mock.patch.object(connector.time, "monotonic", lambda: clock[0]):
            self.post(timeout=1)
        self.assertLessEqual(max(self.sockets[0].timeouts), .600001)

    def test_actual_blocked_header_read_is_interrupted_at_total_deadline(self):
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        self.dial.side_effect = None
        self.dial.return_value = client
        # Send a partial header then keep the local socket open. This reproduces
        # header trickling, which a per-read socket timeout alone cannot bound.
        stopped = threading.Event()

        def responder():
            try:
                server.recv(8192)
                fixture_deadline = time.monotonic() + 1.5
                while time.monotonic() < fixture_deadline and not stopped.wait(.015):
                    server.sendall(b" ")
                server.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        thread = threading.Thread(target=responder, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                self.post(timeout=.12)
        finally:
            stopped.set()
            thread.join(1)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.dial.call_count, 1)
        self.assertIsNone(connector._transport.connection)

    def test_dns_and_tls_failures_remain_network_errors(self):
        for error in [socket.gaierror(-2, "synthetic DNS failure"),
                      ssl.SSLCertVerificationError("synthetic certificate failure")]:
            with self.subTest(error=type(error).__name__):
                self.dial.side_effect = error
                with self.assertRaises(urllib.error.URLError) as raised:
                    self.post()
                self.assertEqual(connector.error_detail(raised.exception)["code"], "connectivity")
                self.assertIsNone(connector._transport.connection)

    def test_close_response_trickling_chunk_line_obeys_total_deadline(self):
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        self.dial.side_effect = None
        self.dial.return_value = client
        stopped = threading.Event()

        def responder():
            try:
                server.recv(8192)
                server.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n"
                               b"Transfer-Encoding: chunked\r\n\r\n")
                # An unfinished chunk-size line keeps read1() blocked. Every
                # byte arrives before the socket timeout, but the full request
                # must still end at its deadline after http.client detaches it.
                fixture_deadline = time.monotonic() + 1.5
                while time.monotonic() < fixture_deadline and not stopped.wait(.015):
                    server.sendall(b"1")
                server.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        thread = threading.Thread(target=responder, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                self.post(timeout=.12)
        finally:
            stopped.set()
            thread.join(1)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.dial.call_count, 1)
        self.assertIsNone(connector._transport.connection)

    def test_tls_exhausting_budget_does_not_begin_http_post(self):
        clock = [0.0]
        wrap = self.context.wrap_socket

        def slow_tls(*args, **kwargs):
            clock[0] = 1.1
            return wrap(*args, **kwargs)

        with mock.patch.object(connector.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(self.context, "wrap_socket", side_effect=slow_tls):
            with self.assertRaises(TimeoutError):
                self.post(timeout=1)
        self.assertEqual(self.sockets[0].sent, [])
        self.assertTrue(self.sockets[0].closed)


if __name__ == "__main__":
    unittest.main()
