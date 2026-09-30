"""Three bounded synthetic tests; all DB writes stay in temporary directories."""
import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT
TEXT = 'SYNTHETIC directdropquartz: the silver kite has eleven blue ribbons.'
RAW = json.dumps({'format': 'threadsatchel/1', 'kind': 'note',
                  'title': 'SYNTHETIC direct delivery test',
                  'messages': [{'speaker': 'assistant', 'text': TEXT}]},
                 ensure_ascii=False, separators=(',', ':')).encode('utf-8')


class DirectDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='threadsatchel-direct-isolated-')
        self.root = Path(self.temp.name)
        self.inbox = self.root / 'inbox'
        self.inbox.mkdir()
        shutil.copy2(ROOT / 'import_memory.py', self.root / 'import_memory.py')
        for name in ('server_readonly.py', 'import_metadata.py', 'memory_search.py', 'memory_listing.py'):
            shutil.copy2(SOURCE / name, self.root / name)
        with closing(sqlite3.connect(self.root / 'memory.sqlite3')) as db, db:
            db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT NOT NULL,source TEXT NOT NULL,title TEXT,created_at TEXT NOT NULL)')
            db.execute("CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title,tokenize='unicode61')")
            db.execute("INSERT INTO memories VALUES('synthetic-legacy','SYNTHETIC original remains intact','synthetic',NULL,'2000-01-01')")
            db.execute("INSERT INTO memory_fts VALUES('synthetic-legacy','SYNTHETIC original remains intact','synthetic',NULL)")

    def tearDown(self):
        self.temp.cleanup()

    def run_import(self, *args):
        return subprocess.run([sys.executable, str(self.root / 'import_memory.py'), *map(str, args)],
                              cwd=self.root, capture_output=True, text=True, timeout=30)

    def count(self):
        with closing(sqlite3.connect(self.root / 'memory.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT text FROM memories WHERE id='synthetic-legacy'").fetchone()[0],
                             'SYNTHETIC original remains intact')
            return db.execute('SELECT COUNT(*) FROM memories').fetchone()[0]

    def test_legacy_and_public_format_share_identity(self):
        for index, marker in enumerate(('steve-memory/1', 'threadsatchel/1')):
            packet = json.loads(RAW)
            packet['format'] = marker
            path = self.inbox / ('format-' + str(index) + '.json')
            path.write_text(json.dumps(packet), encoding='utf-8')
            result = self.run_import(path)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(self.count(), 2)

    def test_temporary_files_never_ingested(self):
        names = ['.incoming-test.json', '.incoming-test.json.part', 'test.json.tmp']
        for name in names:
            (self.inbox / name).write_bytes(RAW)
        (self.inbox / '.incoming-partial.json').write_bytes(b'{"format":')
        scan = self.run_import()
        self.assertEqual(scan.returncode, 0, scan.stdout)
        self.assertEqual(scan.stdout, '')
        self.assertEqual(self.count(), 1)
        for name in names:
            rejected = self.run_import(self.inbox / name)
            self.assertEqual(rejected.returncode, 1)
            self.assertIn('Temporary input is not finalized', json.loads(rejected.stdout)['error'])
            self.assertEqual((self.inbox / name).read_bytes(), RAW)
        self.assertEqual(self.count(), 1)

    def test_finalize_import_repeat_and_mcp(self):
        temporary = self.inbox / '.incoming-test.json.part'
        final = self.inbox / 'chatgpt-test.json'
        temporary.write_bytes(RAW)
        self.assertEqual(hashlib.sha256(temporary.read_bytes()).hexdigest(), hashlib.sha256(RAW).hexdigest())
        temporary.rename(final)  # same-directory rename on the actual test filesystem
        self.assertFalse(temporary.exists())
        first = self.run_import(final)
        self.assertEqual(first.returncode, 0, first.stdout)
        first_result = json.loads(first.stdout)
        self.assertEqual(first_result['added_revisions'], 1)
        self.assertEqual(first_result['sha256'], hashlib.sha256(RAW).hexdigest())
        self.assertEqual(self.count(), 2)
        repeated = self.run_import(final)
        self.assertEqual(repeated.returncode, 0, repeated.stdout)
        self.assertEqual(json.loads(repeated.stdout)['added_revisions'], 0)
        self.assertEqual(json.loads(repeated.stdout)['repeated_packets'], 1)
        other_temporary = self.inbox / '.incoming-second.json.part'
        other_final = self.inbox / 'chatgpt-second.json'
        other_temporary.write_bytes(RAW)
        other_temporary.rename(other_final)
        redelivered = self.run_import(other_final)
        self.assertEqual(redelivered.returncode, 0)
        self.assertEqual(json.loads(redelivered.stdout)['added_revisions'], 0)
        self.assertEqual(self.count(), 2)
        self.assertEqual(final.read_bytes(), RAW)
        with closing(sqlite3.connect(self.root / 'memory.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT raw FROM import_objects').fetchone()[0], RAW)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        async def check_mcp():
            async with Client(StdioServerParameters(command=sys.executable,
                              args=[str(self.root / 'server_readonly.py')])) as client:
                found = await client.call_tool('search_memory', {'query': 'directdropquartz'})
                self.assertFalse(found.is_error)
                records = [json.loads(c.text) for c in found.content if c.type == 'text']
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]['text'], TEXT)
                exact = await client.call_tool('get_memory', {'id': records[0]['id']})
                self.assertFalse(exact.is_error)
                result = json.loads(next(c.text for c in exact.content if c.type == 'text'))
                result = result.get('result', result)
                self.assertEqual(result['text'], TEXT)
        asyncio.run(check_mcp())

    def test_invalid_packet_retained_and_no_changes(self):
        invalid = self.inbox / 'chatgpt-invalid.json'
        raw = b'{"format":"threadsatchel/1","messages":[]}'
        invalid.write_bytes(raw)
        result = self.run_import(invalid)
        self.assertEqual(result.returncode, 1)
        self.assertIn('error', json.loads(result.stdout))
        self.assertEqual(invalid.read_bytes(), raw)
        self.assertEqual(self.count(), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
