"""Synthetic retry/transport checks; never contact a real SSH host or archive."""
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sync_codex_capture as sync

RESULT = dict(added_revisions=1, reused_messages=0, repeated_packets=0,
              warnings=[], uncertain=[], linked_entities=0, ambiguous_excerpt_packets=0)

class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='threadsatchel-SYNTHETIC-sync-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root/'remote-capture'
        self.machines = [
            dict(name=name, capture_home='SYNTHETIC', ssh_argv=['ssh', name, 'python3 -'])
            for name in ('source-a', 'source-b')]
        (self.root/'remote-capture-config.json').write_text(json.dumps({'machines': self.machines}))
        self.offline = set()
        self.packets = []
        self.calls = []
        self.events = []
        for name, value in [('ROOT', self.root), ('FOLDER', self.folder)]:
            self.enterContext(patch.object(sync, name, value))
        self.process = self.enterContext(patch.object(sync.subprocess, 'run', side_effect=self.transport))
        self.importer = self.enterContext(patch.object(sync, 'import_file', side_effect=self.import_packet))

    def transport(self, args, **kwargs):
        name = args[1]
        self.calls.append(name)
        if name in self.offline:
            return SimpleNamespace(returncode=255, stdout='', stderr='SYNTHETIC offline')
        if 't.acknowledge(' in kwargs['input']:
            self.events.append('acknowledged')
            return SimpleNamespace(returncode=0, stdout='{"acknowledged":1}', stderr='')
        packets = self.packets if name == 'source-a' else []
        return SimpleNamespace(returncode=0, stdout=json.dumps({'status': {}, 'packets': packets}), stderr='')

    def import_packet(self, *args):
        self.events.append('imported')
        return RESULT

    def run_at(self, now):
        with patch.object(sync.time, 'time', return_value=now):
            return sync.run()

    def state(self):
        return json.loads((self.folder/'retry-state.json').read_text())

    def test_offline_delay_survives_runs_without_blocking_other_source(self):
        self.offline.add('source-a')
        first = self.run_at(1000)
        self.assertEqual(first['machines']['source-b']['sync_state'], 'succeeded')
        self.assertEqual(self.state()['source-a']['next_attempt'], 1600)
        deferred = self.run_at(1300)
        self.assertEqual(self.calls.count('source-a'), 1)
        self.assertEqual(self.calls.count('source-b'), 2)
        self.assertTrue(deferred['errors'][0]['deferred'])
        self.assertEqual(deferred['machines']['source-a']['sync_state'], 'waiting_to_retry')
        self.offline.clear()
        self.assertEqual(self.run_at(1600)['errors'], [])
        self.assertEqual(self.state(), {})

    def test_delays_grow_to_one_hour_then_reset_after_success(self):
        self.offline.add('source-a')
        now = 1000
        for delay in (600, 1200, 2400, 3600, 3600):
            self.run_at(now)
            retry = self.state()['source-a']
            self.assertEqual(retry['next_attempt'] - now, delay)
            now = retry['next_attempt']
        self.offline.clear()
        self.run_at(now)
        self.assertEqual(self.state(), {})
        self.offline.add('source-a')
        self.run_at(now + 300)
        self.assertEqual(self.state()['source-a']['next_attempt'], now + 900)

    def test_failed_import_keeps_packet_and_does_not_acknowledge(self):
        raw = b'{"synthetic":true}'
        sha = hashlib.sha256(raw).hexdigest()
        self.packets = [{'sha': sha, 'data': base64.b64encode(raw).decode()}]
        self.importer.side_effect = ValueError('SYNTHETIC import failure')
        self.assertEqual(len(self.run_at(1000)['errors']), 1)
        self.assertEqual(self.calls.count('source-a'), 1)
        self.assertNotIn('acknowledged', self.events)
        self.assertTrue((self.folder/'source-a'/'packets'/(sha + '.json')).exists())
        self.importer.side_effect = self.import_packet
        self.assertEqual(self.run_at(1600)['errors'], [])
        self.assertEqual(self.events, ['imported', 'acknowledged'])
        self.assertEqual(self.state(), {})

    def test_timeout_also_waits_before_reconnecting(self):
        self.process.side_effect = TimeoutError('SYNTHETIC timeout')
        self.run_at(1000)
        self.assertEqual(self.process.call_count, 2)
        self.run_at(1300)
        self.assertEqual(self.process.call_count, 2)
        self.assertEqual(self.state()['source-a']['next_attempt'], 1600)

    def test_hidden_window_flag_and_portable_zero_flag_preserve_arguments(self):
        for flag in (0x08000000, 0):
            with self.subTest(flag=flag), patch.object(sync.subprocess, 'CREATE_NO_WINDOW', flag, create=True):
                self.assertEqual(self.run_at(1000)['errors'], [])
                self.assertEqual(self.process.call_args.kwargs['creationflags'], flag)
                self.assertEqual(self.process.call_args.args[0], ['ssh', 'source-b', 'python3 -'])
                self.assertIn('t.pending(', self.process.call_args.kwargs['input'])
                self.assertNotIn('shell', self.process.call_args.kwargs)

if __name__ == '__main__':
    unittest.main()
