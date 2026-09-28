"""Synthetic bounded-read and fair scheduling regressions for both collectors."""
from contextlib import closing
import io
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import capture_read
import capture_transport
import codex_capture
import codex_capture_export
from test_codex_capture import data, meta, msg


class CaptureLimits(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='threadsatchel-SYNTHETIC-limits-')
        self.root = Path(self.temp.name)
        self.source = self.root / 'sources'
        self.source.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_header_read_is_bounded_before_json_parsing(self):
        class Stream(io.BytesIO):
            def readline(self, size=-1):
                self.asserted_size = size
                return super().readline(size)
        stream = Stream(b'{' + b' ' * (2 * 1024 * 1024) + b'}\n')
        with self.assertRaisesRegex(ValueError, 'header_size_limit'):
            capture_read.read_header(stream, 1024)
        self.assertEqual(stream.asserted_size, 1024)
        self.assertEqual(stream.tell(), 1024)

    def test_large_event_is_held_without_advancing_or_reading_past_budget(self):
        path = self.source / 'held.jsonl'
        header = data([meta()])
        raw = header + data([msg('large', 'SYNTHETIC ' + 'x' * 20000), msg('later', 'SYNTHETIC later')])
        path.write_bytes(raw)
        for module in (codex_capture, codex_capture_export):
            with self.subTest(module=module.__name__):
                _, checkpoint, batches, counts = module.project_file(path, {}, 1024)
                self.assertEqual(checkpoint['offset'], len(header))
                self.assertEqual(batches, [])
                self.assertEqual(counts['oversized_line_blocking'], 1)
                _, retry, batches, counts = module.project_file(path, checkpoint, 1024)
                self.assertEqual(retry['offset'], checkpoint['offset'])
                self.assertEqual(batches, [])
                self.assertEqual(path.read_bytes(), raw)

    def test_budget_boundary_resumes_a_complete_event_next_pass(self):
        path = self.source / 'resume.jsonl'
        header, first, second = data([meta()]), data([msg('one', 'SYNTHETIC first')]), data([msg('two', 'SYNTHETIC other')])
        path.write_bytes(header + first + second)
        budget = len(header) + len(first) + len(second) // 2
        for module in (codex_capture, codex_capture_export):
            with self.subTest(module=module.__name__):
                _, checkpoint, batches, counts = module.project_file(path, {}, budget)
                self.assertEqual([m['message_id'] for batch in batches for m in batch], ['one'])
                self.assertEqual(counts['byte_budget_reached'], 1)
                _, checkpoint, batches, counts = module.project_file(path, checkpoint, budget)
                self.assertEqual([m['message_id'] for batch in batches for m in batch], ['two'])
                self.assertEqual(checkpoint['offset'], path.stat().st_size)

    def test_held_header_does_not_starve_another_session(self):
        import os
        bad = self.source / 'a-held.jsonl'
        good = self.source / 'z-good.jsonl'
        bad.write_bytes(b'{' + b'x' * 20000 + b'}\n')
        good.write_bytes(data([meta(), msg('good', 'SYNTHETIC healthy session')]))
        os.utime(bad, (1000, 1000))
        os.utime(good, (1001, 1001))
        for module in (codex_capture, codex_capture_export):
            with self.subTest(module=module.__name__):
                folder = self.root / module.__name__
                db_path = self.root / (module.__name__ + '.sqlite3')
                with closing(sqlite3.connect(db_path)) as db, db:
                    db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT NOT NULL,source TEXT NOT NULL,title TEXT,created_at TEXT NOT NULL)')
                    db.execute('CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title)')
                config = dict(source_roots=[str(self.source)], capture_dir=str(folder),
                              db_path=str(db_path), max_run_seconds=1, max_bytes_per_session=1024)
                with patch.object(module.time, 'monotonic', side_effect=[0, 0, 2, 2]):
                    first = module.run(config)
                self.assertEqual(first['counts']['files_checked'], 1)
                self.assertIn('header_size_limit', first['errors'][0]['reason'])
                state = json.loads((folder / 'state.json').read_text(encoding='utf-8'))
                self.assertEqual(state['sessions'], {})
                with patch.object(module.time, 'monotonic', side_effect=[0, 0, 2, 2]):
                    second = module.run(config)
                self.assertEqual(second['counts']['added_revisions'], 1)
                self.assertFalse(second['errors'])
                with closing(sqlite3.connect(db_path)) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM memories').fetchone()[0], 1)

    def test_generated_packet_limit_splits_without_changing_messages(self):
        messages = [dict(message_id='synthetic-' + str(i), speaker='user', text='SYNTHETIC ' + 'x' * 700000)
                    for i in range(13)]
        packet = dict(format='steve-memory/1', kind='excerpt', conversation_id='SYNTHETIC boundary', messages=messages)
        encoded = list(capture_read.encode_packets(packet))
        self.assertGreater(len(encoded), 1)
        self.assertTrue(all(len(raw) <= capture_read.MAX_PACKET_BYTES for raw in encoded))
        decoded = [json.loads(raw) for raw in encoded]
        self.assertEqual([m for part in decoded for m in part['messages']], messages)
        self.assertTrue(all(part['format'] == 'steve-memory/1' for part in decoded))
        self.assertEqual(list(capture_read.encode_packets(packet)), encoded)
        small = dict(packet, messages=messages[:1])
        expected = json.dumps(small, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        self.assertEqual(list(capture_read.encode_packets(small)), [expected])
        with self.assertRaisesRegex(ValueError, 'projected_packet_size_limit'):
            list(capture_read.encode_packets(small, max_bytes=256))

    def test_held_queue_entries_do_not_block_healthy_delivery_or_get_acknowledged(self):
        folder = self.root / 'queue'
        packets = folder / 'packets'
        packets.mkdir(parents=True)
        large = packets / ('0' * 64 + '.json')
        large.write_bytes(b'x' * 1025)
        corrupt = packets / ('1' * 64 + '.json')
        corrupt.write_bytes(b'SYNTHETIC corrupt packet')
        raw = b'{"format":"steve-memory/1","kind":"note","messages":[{"text":"SYNTHETIC healthy"}]}'
        while hashlib.sha256(raw).hexdigest()[0] <= '1':
            raw += b' '
        sha = hashlib.sha256(raw).hexdigest()
        healthy = packets / (sha + '.json')
        healthy.write_bytes(raw)
        original_status = dict(counts={'eligible_messages': 1}, errors=[{'reason': 'SYNTHETIC existing warning'}])
        (folder / 'status.json').write_text(json.dumps(original_status), encoding='utf-8')
        with patch.object(capture_transport, 'MAX_PACKET_BYTES', 1024):
            bundle = capture_transport.pending(folder)
            self.assertEqual([p['sha'] for p in bundle['packets']], [sha])
            self.assertEqual(bundle['status']['counts'], original_status['counts'])
            self.assertEqual(bundle['status']['errors'], original_status['errors'])
            self.assertEqual(bundle['status']['transport_held_count'], 2)
            self.assertEqual({entry['reason'] for entry in bundle['status']['transport_held']},
                             {'oversized_queued_packet', 'queue_hash_mismatch'})
            capture_transport.acknowledge(folder, [sha])
            self.assertFalse(healthy.exists())
            for held in (large, corrupt):
                before = held.read_bytes()
                with self.assertRaises(ValueError):
                    capture_transport.acknowledge(folder, [held.stem])
                self.assertEqual(held.read_bytes(), before)

    def test_disappearing_or_nonregular_queue_entry_does_not_block_siblings(self):
        folder = self.root / 'racing-queue'
        packets = folder / 'packets'
        packets.mkdir(parents=True)
        disappearing = packets / ('0' * 64 + '.json')
        disappearing.write_bytes(b'SYNTHETIC concurrently acknowledged')
        nonregular = packets / ('1' * 64 + '.json')
        nonregular.mkdir()
        raw = b'SYNTHETIC healthy queued data'
        sha = hashlib.sha256(raw).hexdigest()
        (packets / (sha + '.json')).write_bytes(raw)
        original = Path.lstat
        def racing_lstat(path, *args, **kwargs):
            if path == disappearing:
                disappearing.unlink(missing_ok=True)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'lstat', racing_lstat):
            bundle = capture_transport.pending(folder)
        self.assertEqual([p['sha'] for p in bundle['packets']], [sha])
        self.assertEqual(bundle['status']['transport_held_count'], 2)
        self.assertEqual({x['reason'] for x in bundle['status']['transport_held']},
                         {'queue_read_failed', 'nonregular_queued_packet'})
        self.assertTrue(nonregular.is_dir())
        with self.assertRaisesRegex(ValueError, 'nonregular_queued_packet'):
            capture_transport.acknowledge(folder, [nonregular.stem])


if __name__ == '__main__':
    unittest.main()
