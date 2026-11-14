"""Deterministic socket races and HTTPS integration; no external network."""
import builtins
from contextlib import ExitStack
import errno
import io
import socket
import ssl
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.request

from connector import connector


def address(family, name):
    target = (name, 443, 0, 0) if family == socket.AF_INET6 else (name, 443)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", target)


class FakeSocket:
    def __init__(self, lab, family, kind, protocol):
        self.lab, self.family = lab, family
        self.closed, self.close_count = False, 0
        self.ready_at, self.error = float("inf"), 0
        self.timeout, self.bound = None, None
        self.sent = []
        lab.sockets.append(self)

    def setblocking(self, value):
        self.blocking = value

    def settimeout(self, value):
        self.timeout = value

    def bind(self, value):
        self.bound = value

    def connect_ex(self, target):
        self.target = target
        self.lab.started.append((self.lab.now, target[0]))
        plan = self.lab.plans[target[0]]
        self.ready_at = self.lab.now + plan.get("delay", float("inf"))
        self.error = plan.get("error", 0)
        return plan.get("immediate", errno.EINPROGRESS)

    def getsockopt(self, *args):
        return self.error

    def setsockopt(self, *args):
        pass

    def sendall(self, data):
        self.sent.append(bytes(data))

    def makefile(self, *args, **kwargs):
        return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")

    def close(self):
        self.closed = True
        self.close_count += 1


class FakeSelector:
    def __init__(self, lab):
        self.lab, self.registered, self.closed = lab, {}, False
        lab.selectors.append(self)

    def register(self, sock, events):
        self.registered[sock] = SimpleNamespace(fileobj=sock)
        self.lab.max_pending = max(self.lab.max_pending, len(self.registered))

    def unregister(self, sock):
        return self.registered.pop(sock)

    def select(self, timeout):
        if self.lab.select_error:
            raise self.lab.select_error
        wake = min([self.lab.now + timeout] + [s.ready_at for s in self.registered])
        self.lab.now = wake
        return [(key, connector.selectors.EVENT_WRITE) for sock, key in self.registered.items()
                if sock.ready_at <= wake]

    def close(self):
        self.closed = True
        self.registered.clear()


class SocketLab:
    def __init__(self, addresses, plans):
        self.addresses, self.plans = addresses, plans
        self.now, self.max_pending = 0.0, 0
        self.sockets, self.selectors, self.started = [], [], []
        self.select_error = None

    def __enter__(self):
        self.stack = ExitStack()
        self.stack.enter_context(mock.patch.object(connector.time, "monotonic", lambda: self.now))
        self.dns = self.stack.enter_context(mock.patch.object(connector.socket, "getaddrinfo", return_value=self.addresses))
        self.stack.enter_context(mock.patch.object(connector.socket, "socket", lambda *args: FakeSocket(self, *args)))
        self.stack.enter_context(mock.patch.object(connector.selectors, "DefaultSelector", lambda: FakeSelector(self)))
        self.stack.enter_context(mock.patch.object(urllib.request, "getproxies", return_value={}))
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)


class RecordingTLS:
    check_hostname = True
    verify_mode = ssl.CERT_REQUIRED

    def __init__(self):
        self.hostnames = []

    def wrap_socket(self, sock, *, server_hostname):
        self.hostnames.append(server_hostname)
        return sock


class TransportTests(unittest.TestCase):
    def test_blackholed_ipv6_falls_back_to_ipv4_after_one_stagger(self):
        addresses = [address(socket.AF_INET6, "v6a"), address(socket.AF_INET6, "v6b"),
                     address(socket.AF_INET, "v4a"), address(socket.AF_INET, "v4b")]
        with SocketLab(addresses, {"v6a": {}, "v6b": {}, "v4a": {"delay": .03}, "v4b": {}}) as lab:
            winner = connector.racing_connection(("gateway.example", 443), timeout=10)
            self.assertEqual(lab.started, [(0, "v6a"), (.25, "v4a")])
            self.assertAlmostEqual(lab.now, .28)
            self.assertEqual(winner.target[0], "v4a")
            self.assertEqual(winner.timeout, 10)
            self.assertFalse(winner.closed)
            self.assertEqual(lab.sockets[0].close_count, 1)
            self.assertTrue(lab.selectors[0].closed)

    def test_healthy_preferred_ipv6_needs_no_ipv4_attempt(self):
        with SocketLab([address(socket.AF_INET6, "v6"), address(socket.AF_INET, "v4")],
                       {"v6": {"delay": .02}, "v4": {}}) as lab:
            winner = connector.racing_connection(("gateway.example", 443), timeout=10)
            self.assertEqual(winner.family, socket.AF_INET6)
            self.assertEqual(lab.started, [(0, "v6")])
            self.assertAlmostEqual(lab.now, .02)

    def test_all_blackholed_addresses_share_one_deadline_and_close(self):
        with SocketLab([address(socket.AF_INET6, "v6"), address(socket.AF_INET, "v4")],
                       {"v6": {}, "v4": {}}) as lab:
            with self.assertRaises(TimeoutError):
                connector.racing_connection(("gateway.example", 443), timeout=.6)
            self.assertAlmostEqual(lab.now, .6)
            self.assertTrue(all(s.closed and s.close_count == 1 for s in lab.sockets))

    def test_simultaneous_success_keeps_only_one_socket(self):
        with SocketLab([address(socket.AF_INET6, "v6"), address(socket.AF_INET, "v4")],
                       {"v6": {"delay": .5}, "v4": {"delay": .25}}) as lab:
            winner = connector.racing_connection(("gateway.example", 443), timeout=10)
            self.assertEqual(len(lab.sockets), 2)
            self.assertEqual(sum(not sock.closed for sock in lab.sockets), 1)
            self.assertFalse(winner.closed)
            self.assertTrue(all(sock.close_count == 1 for sock in lab.sockets if sock is not winner))

    def test_dns_over_budget_starts_no_tcp_attempt(self):
        with SocketLab([address(socket.AF_INET6, "v6")], {"v6": {}}) as lab:
            def slow_dns(*args):
                lab.now = 2
                return lab.addresses
            lab.dns.side_effect = slow_dns
            with self.assertRaises(TimeoutError):
                connector.racing_connection(("gateway.example", 443), timeout=1)
            self.assertEqual(lab.sockets, [])

    def test_immediate_and_async_failures_advance_without_stagger(self):
        for plan, expected in [({"immediate": errno.ECONNREFUSED}, 0),
                               ({"delay": .1, "error": errno.ECONNREFUSED}, .1)]:
            with self.subTest(plan=plan):
                with SocketLab([address(socket.AF_INET6, "v6"), address(socket.AF_INET, "v4")],
                               {"v6": plan, "v4": {"immediate": 0}}) as lab:
                    winner = connector.racing_connection(("gateway.example", 443), timeout=10)
                    self.assertEqual(lab.started[-1], (expected, "v4"))
                    self.assertEqual(winner.target[0], "v4")
                    self.assertEqual(lab.sockets[0].close_count, 1)

    def test_concurrency_cap_and_source_address(self):
        addresses = [address(socket.AF_INET6 if i % 2 == 0 else socket.AF_INET, str(i)) for i in range(8)]
        with SocketLab(addresses, {str(i): {} for i in range(8)}) as lab:
            with self.assertRaises(TimeoutError):
                connector.racing_connection(("gateway.example", 443), timeout=2, source_address=("", 0))
            self.assertEqual(lab.max_pending, 4)
            self.assertTrue(all(s.bound == ("", 0) and s.closed for s in lab.sockets))

    def test_selector_exception_closes_every_owned_socket(self):
        with SocketLab([address(socket.AF_INET6, "v6")], {"v6": {}}) as lab:
            lab.select_error = RuntimeError("synthetic selector failure")
            with self.assertRaises(RuntimeError):
                connector.racing_connection(("gateway.example", 443), timeout=2)
            self.assertTrue(all(s.closed for s in lab.sockets))
            self.assertTrue(lab.selectors[0].closed)

    @unittest.skipUnless(hasattr(builtins, "ExceptionGroup"), "Python 3.11+ all_errors contract")
    def test_all_errors_preserves_both_failures(self):
        with SocketLab([address(socket.AF_INET6, "v6"), address(socket.AF_INET, "v4")],
                       {"v6": {"immediate": errno.ECONNREFUSED}, "v4": {"immediate": errno.ENETUNREACH}}):
            with self.assertRaises(ExceptionGroup) as raised:
                connector.racing_connection(("gateway.example", 443), timeout=2, all_errors=True)
            self.assertEqual([exc.errno for exc in raised.exception.exceptions], [errno.ECONNREFUSED, errno.ENETUNREACH])

    def test_default_tls_requires_trusted_certificate_and_hostname(self):
        connection = connector.RacingHTTPSConnection("gateway.example", timeout=10)
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIs(connection._create_connection, connector.racing_connection)
        self.assertIsNot(socket.create_connection, connector.racing_connection)

    def test_post_uses_only_winning_socket_once_and_keeps_hostname(self):
        context = RecordingTLS()
        connection_class = connector.RacingHTTPSConnection
        with SocketLab([address(socket.AF_INET6, "v6"), address(socket.AF_INET, "v4")],
                       {"v6": {}, "v4": {"delay": .02}}) as lab, \
                mock.patch.object(connector, "RacingHTTPSConnection",
                                  side_effect=lambda *args, **kwargs: connection_class(*args, context=context, **kwargs)):
            result = connector.post({"endpoint": "https://gateway.example/api/agent", "key": "test", "access_token": "test"},
                                    {"action": "complete", "id": "test"}, timeout=10)
            self.assertEqual(result, {})
            self.assertEqual(context.hostnames, ["gateway.example"])
            self.assertEqual(lab.sockets[0].sent, [])
            sent = b"".join(data for sock in lab.sockets for data in sock.sent)
            self.assertEqual(sent.count(b"POST /api/agent HTTP/1.1"), 1)
            self.assertEqual(sent.count(b'"action":"complete"'), 1)
            self.assertTrue(all(s.closed for s in lab.sockets))

    def test_https_proxy_connect_and_target_sni_are_preserved(self):
        context = RecordingTLS()
        handler = connector.RacingHTTPSHandler(context=context)
        request = urllib.request.Request("https://gateway.example/api/agent", data=b"{}")
        request.set_proxy("proxy.example:8080", "http")
        request.timeout = 10
        with SocketLab([address(socket.AF_INET, "proxy-ip")], {"proxy-ip": {"immediate": 0}}) as lab:
            with handler.https_open(request) as response:
                self.assertEqual(response.read(), b"{}")
            self.assertEqual(lab.dns.call_args.args[:2], ("proxy.example", 8080))
            self.assertEqual(context.hostnames, ["gateway.example"])
            sent = b"".join(lab.sockets[0].sent)
            self.assertEqual(sent.count(b"CONNECT gateway.example:443 HTTP/1."), 1)
            self.assertEqual(sent.count(b"POST /api/agent HTTP/1.1"), 1)


if __name__ == "__main__":
    unittest.main()
