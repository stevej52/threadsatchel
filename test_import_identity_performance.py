"""Identity resolution must avoid broad replay comparisons and preserve links."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import import_memory


class ImportIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='import-identity-synthetic-')
        self.root = Path(self.temp.name)
        self.db = self.root / 'memory.sqlite3'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT,source TEXT,title TEXT,created_at TEXT)')
            db.execute('CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title)')

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, packet):
        path = self.root / (name + '.json')
        path.write_text(json.dumps(packet), encoding='utf-8')
        return import_memory.import_file(path, self.db)

    def test_known_replay_avoids_text_pairs_and_keeps_provenance(self):
        packet = {'conversation_id': 'synthetic-conversation', 'messages': [
            {'message_id': f'message-{i}', 'text': f'SYNTHETIC message {i}.'} for i in range(200)]}
        self.assertEqual(self.write('initial', packet)['added_revisions'], 200)
        for i in range(20):
            self.write(f'metadata-{i}', dict(packet, title=f'SYNTHETIC metadata revision {i}'))
        with patch.object(import_memory, 'compatible', wraps=import_memory.compatible) as compare:
            result = self.write('replay', dict(packet, title='SYNTHETIC final metadata revision'))
        self.assertEqual(compare.call_count, 0)
        self.assertEqual(result['added_revisions'], 0)
        self.assertEqual(result['reused_messages'], 200)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM import_packet_sources').fetchone()[0], 22)
            self.assertEqual(db.execute('SELECT count(*) FROM memory_fts').fetchone()[0], 200)

    def test_resolved_ids_still_link_earlier_anonymous_overlap(self):
        first = {'conversation_id': 'synthetic-link', 'messages': [
            {'message_id': 'known-a', 'text': 'SYNTHETIC alpha.'}]}
        self.write('known', first)
        anonymous = {'conversation_id': 'synthetic-link', 'messages': [
            {'text': 'SYNTHETIC alpha.'}, {'text': 'SYNTHETIC beta.'}]}
        self.write('anonymous', anonymous)
        final = {'conversation_id': 'synthetic-link', 'messages': [
            {'message_id': 'known-a', 'text': 'SYNTHETIC alpha.'},
            {'message_id': 'known-b', 'text': 'SYNTHETIC beta.'}]}
        result = self.write('complete', final)
        self.assertEqual(result['linked_entities'], 1)
        self.assertEqual(result['added_revisions'], 0)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM memory_fts').fetchone()[0], 2)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_existing_archive_is_backed_up_before_new_lookup_index(self):
        import_memory.migrate(self.db)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('DROP INDEX import_items_memory')
        backup = import_memory.migrate(self.db)
        self.assertIsNotNone(backup)
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='import_items_memory'").fetchone())
        with closing(sqlite3.connect(self.db)) as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='import_items_memory'").fetchone())
        self.assertIsNone(import_memory.migrate(self.db))


if __name__ == '__main__':
    unittest.main()
