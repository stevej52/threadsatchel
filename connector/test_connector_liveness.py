"""Connector liveness regressions; no network, live archive, or credentials."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error

from connector import connector


def job(action="search_memory", args=None):
    payload = connector.encode({"action": action, "args": args or {}}).decode()
    return {"id": "a" * 64, "lease": "test-lease", "payload": payload,
            "sha256": connector.sha(payload.encode())}


class LivenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.archive = connector.Archive(self.root, self.state)

    def events(self):
        return [json.loads(line) for line in self.archive.diagnostics.events.read_text().splitlines()]

    def test_slow_job_stays_online_without_claiming_another_job(self):
        # Scale the reported 90s threshold to 90ms; work runs for over twice it.
        calls, health_writers = [], []
        last_checkin = [time.monotonic()]
        heartbeat_seen = threading.Event()
        current_job = job()
        owner = threading.get_ident()

        def transport(config, payload, **kwargs):
            calls.append((payload.copy(), threading.get_ident()))
            if payload["action"] == "poll":
                last_checkin[0] = time.monotonic()
                return {"job": current_job}
            if payload["action"] == "heartbeat":
                self.assertEqual(set(payload), {"action", "id", "lease"})
                self.assertEqual(payload["lease"], "test-lease")
                last_checkin[0] = time.monotonic()
                heartbeat_seen.set()
                return {"acknowledged": True}
            return {"acknowledged": True}

        def execute(action, args):
            self.assertTrue(heartbeat_seen.wait(1))
            time.sleep(.21)
            self.assertLess(time.monotonic() - last_checkin[0], .09)
            return []

        original_atomic = connector.atomic

        def atomic(path, value):
            if path.name == "health.json":
                health_writers.append(threading.get_ident())
            original_atomic(path, value)

        with mock.patch.object(connector, "post", transport), \
                mock.patch.object(connector, "HEARTBEAT_INTERVAL", .005), \
                mock.patch.object(connector, "atomic", atomic), \
                mock.patch.object(self.archive, "execute", execute):
            connector.run_loop({}, self.archive, once=True)
        self.assertEqual([p["action"] for p, _ in calls if p["action"] != "heartbeat"], ["poll", "complete"])
        self.assertGreater(len([p for p, _ in calls if p["action"] == "heartbeat"]), 2)
        self.assertTrue(all(tid == owner for p, tid in calls if p["action"] != "heartbeat"))
        self.assertEqual(set(health_writers), {owner})
        self.assertEqual(json.loads((self.state / "health.json").read_text())["status"], "connected")
        records = self.events()
        job_records = [e for e in records if e["status"] in ("started", "succeeded")]
        self.assertEqual([e["status"] for e in job_records], ["started", "succeeded"])
        self.assertEqual(sum(e["status"] == "heartbeat_started" for e in records), 1)
        self.assertGreaterEqual(job_records[-1]["duration_ms"], 200)

    def test_heartbeat_first_failure_recovers_and_logs_start_only_once(self):
        attempts = []
        repeated_success = threading.Event()

        def transport(config, payload, **kwargs):
            attempts.append(payload)
            if len(attempts) == 1:
                raise urllib.error.HTTPError("https://unused", 503, "private", {}, None)
            if len(attempts) >= 3:
                repeated_success.set()
            return {"acknowledged": True}

        with mock.patch.object(connector, "post", transport), \
                mock.patch.object(connector, "HEARTBEAT_INTERVAL", .005):
            with connector.BusyHeartbeat({}, self.archive, job()):
                self.assertTrue(repeated_success.wait(1))
        statuses = [e["status"] for e in self.events()]
        self.assertEqual(statuses, ["heartbeat_failed", "heartbeat_started", "heartbeat_recovered"])
        self.assertGreaterEqual(len(attempts), 3)

    def test_heartbeat_and_completion_failure_preserve_write_receipts(self):
        imports = []
        heartbeat_seen = threading.Event()

        def importer(packet, delivery):
            imports.append(delivery)
            self.assertTrue(heartbeat_seen.wait(1))
            return {"saved": True, "delivery": delivery}

        self.archive._importer = importer
        current_job = job("save_memory", {"request_key": "same-save", "packet": {"format": "test"}})

        def transport(config, payload, **kwargs):
            if payload["action"] == "poll":
                return {"job": current_job}
            if payload["action"] == "heartbeat":
                heartbeat_seen.set()
            raise urllib.error.HTTPError("https://private.invalid/secret", 503, "sensitive error body", {}, None)

        with mock.patch.object(connector, "post", transport), \
                mock.patch.object(connector, "HEARTBEAT_INTERVAL", .005):
            connector.run_loop({}, self.archive, once=True)
        receipt_path = self.state / (current_job["id"] + ".json")
        original_receipt = receipt_path.read_bytes()
        original_write = list((self.state / "writes").glob("*.json"))[0].read_bytes()
        result, _ = connector.run_job(self.archive, current_job)
        self.assertTrue(result["result"]["saved"])
        self.assertEqual(len(imports), 1)
        self.assertEqual(receipt_path.read_bytes(), original_receipt)
        self.assertEqual(list((self.state / "writes").glob("*.json"))[0].read_bytes(), original_write)
        encoded_events = self.archive.diagnostics.events.read_text()
        self.assertNotIn("sensitive", encoded_events)
        self.assertNotIn("private.invalid", encoded_events)
        self.assertTrue(any(e.get("error_detail", {}).get("http_status") == 503 for e in self.events()))
        self.assertFalse(list(self.archive.diagnostics.failures.glob("*.json")))

    def test_heartbeat_budget_stops_renewal_and_caps_request_timeout(self):
        calls = []

        def transport(config, payload, **kwargs):
            calls.append((time.monotonic(), kwargs["timeout"]))
            return {"acknowledged": True}

        with mock.patch.object(connector, "post", transport), \
                mock.patch.object(connector, "HEARTBEAT_INTERVAL", .005), \
                mock.patch.object(connector, "HEARTBEAT_BUDGET", .04):
            with connector.BusyHeartbeat({}, self.archive, job()) as worker:
                worker.thread.join(timeout=1)
                self.assertFalse(worker.thread.is_alive())
        self.assertTrue(calls)
        self.assertTrue(all(0 < timeout <= .04 for _, timeout in calls))
        self.assertLess(calls[-1][0] - calls[0][0], .1)
        self.assertEqual(self.events()[-1]["status"], "heartbeat_budget_exhausted")

    def test_lost_lease_and_completed_job_stop_heartbeats(self):
        for response in (urllib.error.HTTPError("https://unused", 409, "private", {}, None),
                         {"acknowledged": True, "completed": True}):
            with self.subTest(response=type(response).__name__):
                with mock.patch.object(connector, "post", side_effect=[response]) as post, \
                        mock.patch.object(connector, "HEARTBEAT_INTERVAL", .005):
                    with connector.BusyHeartbeat({}, self.archive, job()) as worker:
                        worker.thread.join(timeout=1)
                        self.assertFalse(worker.thread.is_alive())
                self.assertEqual(post.call_count, 1)

    def test_stuck_transport_does_not_block_main_thread_shutdown(self):
        entered, release = threading.Event(), threading.Event()

        def transport(*args, **kwargs):
            entered.set()
            release.wait(2)
            return {"acknowledged": True}

        with mock.patch.object(connector, "post", transport), \
                mock.patch.object(connector, "HEARTBEAT_INTERVAL", .005), \
                mock.patch.object(connector, "HEARTBEAT_JOIN_TIMEOUT", .01):
            started = time.monotonic()
            with connector.BusyHeartbeat({}, self.archive, job()) as worker:
                self.assertTrue(entered.wait(1))
            self.assertLess(time.monotonic() - started, .5)
            release.set()
            worker.thread.join(timeout=1)
            self.assertFalse(worker.thread.is_alive())

    def test_concurrent_diagnostics_rotate_without_losing_records(self):
        with mock.patch.object(connector, "EVENT_MAX_BYTES", 1500), \
                mock.patch.object(connector, "EVENT_BACKUPS", 20):
            writers = [threading.Thread(target=lambda: [self.archive.diagnostics.record(
                None, "connector", "heartbeat_failed") for _ in range(15)]) for _ in range(4)]
            for writer in writers:
                writer.start()
            for writer in writers:
                writer.join()
        lines = [line for path in self.state.glob("connector-events.jsonl*") for line in path.read_text().splitlines()]
        self.assertEqual(len(lines), 60)
        self.assertTrue(all(json.loads(line)["status"] == "heartbeat_failed" for line in lines))

    def test_main_stops_prewarming_on_failure(self):
        (self.root/'memory.sqlite3').touch()
        config_path = self.root / "config.dpapi"
        config_path.write_text(json.dumps({"archive": str(self.root)}))
        prewarm = mock.Mock()
        with mock.patch.object(connector.sys, "argv", ["connector.py", "--config", str(config_path), "--once"]), \
                mock.patch.object(connector, "load_config", return_value={"archive": str(self.root)}), \
                mock.patch.object(connector, "start_prewarm", return_value=prewarm) as start, \
                mock.patch.object(connector, "run_loop", side_effect=RuntimeError("test")):
            with self.assertRaisesRegex(RuntimeError, "test"):
                connector.main()
        start.assert_called_once_with(self.root)
        prewarm.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
