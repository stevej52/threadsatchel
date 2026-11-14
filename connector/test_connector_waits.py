"""Connector pickup/heartbeat latency regressions; synthetic state, no network."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error

from connector import connector


class StopLoop(Exception):
    pass


def make_job(identifier="a", action="get_memory"):
    payload = connector.encode({"action": action, "args": {}}).decode()
    return {"id": identifier * 64, "lease": "synthetic-lease", "payload": payload,
            "sha256": connector.sha(payload.encode())}


class ConnectorWaitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="connector-waits-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.archive = connector.Archive(self.root, self.state)

    def test_short_job_sends_no_heartbeat_or_sleep(self):
        calls = []

        def transport(config, payload, **kwargs):
            calls.append(payload["action"])
            if payload["action"] == "poll":
                return {"job": make_job()}
            if payload["action"] == "complete":
                return {"acknowledged": True}
            raise AssertionError("Short jobs must not send a heartbeat")

        with mock.patch.object(connector, "post", transport), \
                mock.patch.object(self.archive, "execute", return_value={"found": True}), \
                mock.patch.object(connector.time, "sleep") as sleep:
            connector.run_loop({}, self.archive, once=True)
        self.assertEqual(calls, ["poll", "complete"])
        sleep.assert_not_called()
        events = [json.loads(line) for line in self.archive.diagnostics.events.read_text().splitlines()]
        self.assertEqual([event["status"] for event in events], ["started", "succeeded"])

    def test_long_job_renews_before_online_and_lease_expiry(self):
        clock = [0.0]
        last_checkin = [0.0]
        lease_expiry = [300.0]
        calls = []

        class ClockEvent:
            def wait(self, timeout):
                clock[0] += timeout
                return False

            def is_set(self):
                return False

        def transport(config, payload, **kwargs):
            self.assertEqual(payload["action"], "heartbeat")
            self.assertLess(clock[0] - last_checkin[0], 90)
            self.assertLess(clock[0], lease_expiry[0])
            self.assertGreater(kwargs["timeout"], 0)
            self.assertLessEqual(kwargs["timeout"], connector.HEARTBEAT_TIMEOUT)
            calls.append(clock[0])
            last_checkin[0] = clock[0]
            lease_expiry[0] = clock[0] + 300
            return {"acknowledged": True}

        worker = connector.BusyHeartbeat({}, self.archive, make_job())
        worker.stop_event = ClockEvent()
        with mock.patch.object(connector.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(connector, "post", transport):
            worker._run()
        self.assertEqual(calls, list(range(15, 240, 15)))
        self.assertEqual(clock[0], connector.HEARTBEAT_BUDGET)
        self.assertGreater(lease_expiry[0], clock[0])

    def test_completed_jobs_have_no_pause_and_execute_serially(self):
        jobs = [make_job("a", "save_memory"), make_job("b", "get_memory")]
        calls, actions, sleeps = [], [], []
        owner = threading.get_ident()
        executing = [False]

        def transport(config, payload, **kwargs):
            calls.append(payload["action"])
            self.assertEqual(threading.get_ident(), owner)
            self.assertFalse(executing[0])
            if payload["action"] == "poll":
                return {"job": jobs.pop(0)} if jobs else {}
            self.assertEqual(payload["action"], "complete")
            return {"acknowledged": True}

        def execute(action, args):
            self.assertEqual(threading.get_ident(), owner)
            self.assertFalse(executing[0])
            executing[0] = True
            actions.append(action)
            executing[0] = False
            return {"saved": True} if action == "save_memory" else {"found": True}

        def sleep(seconds):
            sleeps.append(seconds)
            raise StopLoop()

        with mock.patch.object(connector, "post", transport), \
                mock.patch.object(self.archive, "execute", execute), \
                mock.patch.object(connector.time, "sleep", sleep):
            with self.assertRaises(StopLoop):
                connector.run_loop({}, self.archive)
        self.assertEqual(calls, ["poll", "complete", "poll", "complete", "poll"])
        self.assertEqual(actions, ["save_memory", "get_memory"])
        self.assertEqual(sleeps, [connector.IDLE_POLL_INTERVAL])

    def test_idle_polls_are_rate_limited(self):
        calls, sleeps = [], []

        def transport(config, payload, **kwargs):
            calls.append(payload["action"])
            return {}

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 4:
                raise StopLoop()

        with mock.patch.object(connector, "post", transport), \
                mock.patch.object(connector.time, "sleep", sleep):
            with self.assertRaises(StopLoop):
                connector.run_loop({}, self.archive)
        self.assertEqual(calls, ["poll"] * 4)
        self.assertEqual(sleeps, [.75] * 4)
        self.assertGreaterEqual(sum(sleeps), 3)

    def test_failure_backoff_is_capped_and_success_resets_it(self):
        polls, sleeps = [], []

        def transport(config, payload, **kwargs):
            self.assertEqual(payload["action"], "poll")
            polls.append(payload)
            if len(polls) <= 7:
                raise urllib.error.HTTPError("https://unused", 503, "unavailable", {}, None)
            return {}

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 8:
                raise StopLoop()

        with mock.patch.object(connector, "post", transport), \
                mock.patch.object(connector.time, "sleep", sleep):
            with self.assertRaises(StopLoop):
                connector.run_loop({}, self.archive)
        self.assertEqual(sleeps, [2, 4, 8, 16, 32, 60, 60, .75])
        self.assertEqual(json.loads((self.state / "health.json").read_text())["status"], "connected")


if __name__ == "__main__":
    unittest.main(verbosity=2)
