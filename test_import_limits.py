"""Focused synthetic bounds and pre-commit hash checks; no installed archive."""
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from import_memory import import_file, parse


class ImportLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='import-limits-synthetic-')
        self.root = Path(self.temp.name)
        self.db = self.root / 'memory.sqlite3'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT,source TEXT,title TEXT,created_at TEXT)')
            db.execute('CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title)')

    def tearDown(self):
        self.temp.cleanup()

    def test_file_limit_and_hash_reject_before_any_archive_write(self):
        path = self.root / 'packet.json'
        raw = json.dumps({'messages': [{'text': 'SYNTHETIC original packet.'}]}).encode()
        path.write_bytes(raw)
        for kwargs in ({'max_file_bytes': len(raw) - 1}, {'expected_sha256': '0' * 64}):
            with self.assertRaises(ValueError):
                import_file(path, self.db, **kwargs)
        self.assertEqual(path.read_bytes(), raw)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='import_schema'").fetchone())
            self.assertEqual(db.execute('SELECT count(*) FROM memories').fetchone()[0], 0)
        self.assertEqual(import_file(path, self.db, max_file_bytes=len(raw))['added_revisions'], 1)

    def test_zip_expansion_and_member_limits_with_explicit_allowance(self):
        content = json.dumps([{'id': 'synthetic-export', 'current_node': 'one',
            'mapping': {'one': {'parent': None, 'message': {'id': 'one',
                'author': {'role': 'user'}, 'content': {'content_type': 'text',
                'parts': ['SYNTHETIC ' + 'x' * 4096]}}}}}]).encode()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('conversations.json', content)
            archive.writestr('ignored.txt', 'SYNTHETIC unused export attachment')
        raw = stream.getvalue()
        path = self.root / 'export.zip'
        path.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, 'Expanded'):
            import_file(path, self.db, max_expanded_bytes=1024)
        with self.assertRaisesRegex(ValueError, 'member'):
            parse(path, raw, {}, max_zip_members=1)
        result = import_file(path, self.db, max_expanded_bytes=len(content), max_zip_members=2)
        self.assertEqual(result['added_revisions'], 1)
        self.assertEqual(path.read_bytes(), raw)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT raw FROM import_objects').fetchone()[0], raw)


if __name__ == '__main__':
    unittest.main()
