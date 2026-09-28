"""Writable MCP retry tests against a temporary synthetic archive."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server


class StoreMemory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='threadsatchel-SYNTHETIC-store-')
        self.db_patch = patch.object(server, 'DB', Path(self.temp.name) / 'memory.sqlite3')
        self.db_patch.start()
        server.initialize()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def counts(self):
        with closing(server.connect()) as db:
            return tuple(db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                         for table in ('memories', 'memory_fts', 'memory_write_receipts'))

    def test_lost_response_replay_returns_the_same_original_record(self):
        first = server.store_memory('SYNTHETIC blue robot.', 'SYNTHETIC source', 'SYNTHETIC title', 'client-a:write-1')
        replay = server.store_memory('SYNTHETIC blue robot.', 'SYNTHETIC source', 'SYNTHETIC title', 'client-a:write-1')
        self.assertEqual(first, replay)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_same_key_with_changed_text_source_or_title_is_a_conflict(self):
        server.store_memory('SYNTHETIC original.', 'SYNTHETIC source', 'SYNTHETIC title', 'client-a:write-1')
        for text, source, title in [('SYNTHETIC changed.', 'SYNTHETIC source', 'SYNTHETIC title'),
                                    ('SYNTHETIC original.', 'SYNTHETIC other', 'SYNTHETIC title'),
                                    ('SYNTHETIC original.', 'SYNTHETIC source', None)]:
            with self.assertRaisesRegex(ValueError, 'different content'):
                server.store_memory(text, source, title, 'client-a:write-1')
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_separate_keys_and_legacy_calls_preserve_distinct_writes(self):
        records = [server.store_memory('SYNTHETIC same text.', 'SYNTHETIC source', idempotency_key=key)
                   for key in ('conversation-a:1', 'conversation-b:1', None, None)]
        self.assertEqual(len({r['id'] for r in records}), 4)
        self.assertEqual(self.counts(), (4, 4, 2))

    def test_concurrent_same_key_is_one_transactional_write(self):
        def write(_):
            return server.store_memory('SYNTHETIC concurrent.', 'SYNTHETIC source', idempotency_key='client-a:concurrent')['id']
        with ThreadPoolExecutor(max_workers=4) as executor:
            ids = list(executor.map(write, range(8)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_receipt_failure_rolls_back_memory_and_fts(self):
        with closing(server.connect()) as db, db:
            db.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON memory_write_receipts BEGIN SELECT RAISE(ABORT,'SYNTHETIC failure'); END")
        with self.assertRaisesRegex(Exception, 'SYNTHETIC failure'):
            server.store_memory('SYNTHETIC rollback.', 'SYNTHETIC source', idempotency_key='client-a:rollback')
        self.assertEqual(self.counts(), (0, 0, 0))
        with closing(server.connect()) as db, db:
            db.execute('DROP TRIGGER synthetic_failure')
        server.store_memory('SYNTHETIC rollback.', 'SYNTHETIC source', idempotency_key='client-a:rollback')
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_input_byte_limits_and_invalid_keys_leave_no_partial_writes(self):
        for kwargs in [dict(text='x' * (server.MAX_TEXT_BYTES + 1)),
                       dict(text='\U0001f916' * (server.MAX_TEXT_BYTES // 4 + 1)),
                       dict(source='x' * (server.MAX_SOURCE_BYTES + 1)),
                       dict(optional_title='x' * (server.MAX_TITLE_BYTES + 1)),
                       dict(idempotency_key='invalid key'), dict(idempotency_key=''),
                       dict(idempotency_key='x' * 129)]:
            values = dict(text='SYNTHETIC bounded.', source='SYNTHETIC source')
            values.update(kwargs)
            with self.assertRaises(ValueError):
                server.store_memory(**values)
        self.assertEqual(self.counts(), (0, 0, 0))


if __name__ == '__main__':
    unittest.main()
