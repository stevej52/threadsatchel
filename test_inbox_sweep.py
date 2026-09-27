"""Bounded synthetic inbox tests; never access the installed archive."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from import_memory import migrate
from inbox_sweep import sweep, read_guard


class InboxSweepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='inbox-sweep-synthetic-')
        self.root = Path(self.temp.name)
        self.inbox = self.root / 'inbox'
        self.inbox.mkdir()
        with closing(sqlite3.connect(self.root / 'memory.sqlite3')) as db, db:
            db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT,source TEXT,title TEXT,created_at TEXT)')
            db.execute('CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title)')
            db.execute("INSERT INTO memories VALUES('legacy','SYNTHETIC previous memory','test',NULL,'2000-01-01')")
            db.execute("INSERT INTO memory_fts VALUES('legacy','SYNTHETIC previous memory','test',NULL)")
        migrate(self.root / 'memory.sqlite3')
        self.raw = json.dumps({'format': 'steve-memory/1', 'kind': 'note',
            'messages': [{'text': 'SYNTHETIC inboxsweepquartz: the blue robot carries four paper flowers.'}]}).encode()

    def tearDown(self):
        self.temp.cleanup()

    def count(self):
        with closing(sqlite3.connect(self.root / 'memory.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT text FROM memories WHERE id='legacy'").fetchone()[0], 'SYNTHETIC previous memory')
            return db.execute('SELECT count(*) FROM memories').fetchone()[0]

    def test_finalize_exact_bytes_and_retry(self):
        temporary = self.inbox / '.incoming-example.json.part'
        temporary.write_bytes(self.raw)
        self.assertEqual(sweep(self.root, settle_seconds=0)['imported_files'], 0)
        final = self.inbox / 'example.json'
        temporary.rename(final)
        first = sweep(self.root, settle_seconds=0)
        self.assertEqual(first['added_revisions'], 1)
        self.assertTrue(Path(first['backup']).is_file())
        self.assertEqual(self.count(), 2)
        again = sweep(self.root, settle_seconds=0)
        self.assertEqual(again['already_present'], 1)
        self.assertIsNone(again['backup'])
        (self.inbox / 'redelivered.json').write_bytes(self.raw)
        self.assertEqual(sweep(self.root, settle_seconds=0)['added_revisions'], 0)
        self.assertEqual(self.count(), 2)
        with closing(sqlite3.connect(self.root / 'memory.sqlite3')) as db:
            self.assertEqual(bytes(db.execute('SELECT raw FROM import_objects').fetchone()[0]), self.raw)
            self.assertEqual(db.execute("SELECT count(*) FROM memory_fts WHERE memory_fts MATCH 'inboxsweepquartz'").fetchone()[0], 1)

    def test_invalid_recent_and_unsupported_are_retained(self):
        bad = self.inbox / 'bad.json'
        bad.write_bytes(b'{broken')
        (self.inbox / 'program.exe').write_bytes(b'not executed')
        (self.inbox / 'archive.zip').write_bytes(b'not automatically imported')
        result = sweep(self.root, settle_seconds=0)
        self.assertEqual(result['held_files'], 1)
        self.assertEqual(bad.read_bytes(), b'{broken')
        self.assertEqual(self.count(), 1)
        bad.write_bytes(self.raw)
        self.assertEqual(sweep(self.root)['deferred_files'], 1)
        self.assertEqual(sweep(self.root, settle_seconds=0)['added_revisions'], 1)

    def test_interrupted_commit_and_sensitive_data(self):
        good = self.inbox / 'good.json'
        good.write_bytes(self.raw)
        sweep(self.root, settle_seconds=0)
        state_path = self.root / 'inbox-sweep' / 'state.json'
        state = json.loads(state_path.read_text())
        state['files']['good.json']['status'] = 'importing'
        state_path.write_text(json.dumps(state))
        self.assertEqual(sweep(self.root, settle_seconds=0)['already_present'], 1)
        packet = {'format': 'steve-memory/1', 'messages': [{'text': 'SYNTHETIC token example: ' + 'sk-' + 'z' * 32}]}
        secret = self.inbox / 'sensitive.json'
        secret.write_text(json.dumps(packet))
        result = sweep(self.root, settle_seconds=0)
        self.assertEqual(result['held_files'], 1)
        self.assertEqual(self.count(), 2)
        self.assertTrue(secret.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics')
    def test_windows_read_guard_blocks_writes(self):
        path = self.inbox / 'guard.json'
        path.write_bytes(self.raw)
        with read_guard(path):
            with self.assertRaises(OSError):
                path.write_bytes(b'changed')
            with self.assertRaises(OSError):
                path.rename(self.inbox / 'renamed.json')
        self.assertEqual(path.read_bytes(), self.raw)


if __name__ == '__main__':
    unittest.main()
