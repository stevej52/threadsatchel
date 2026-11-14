"""Public connector delivery and reads against a separate synthetic archive."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from connector.connector import Archive, OperationError


class ArchiveIntegration(unittest.TestCase):
    def test_busy_archive_never_allocates_or_overwrites_a_handoff(self):
        from codex_capture import capture_lock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = Archive(root, root / 'connector-state', importer=lambda *_: {'saved':True})
            args = {'name':'Example', 'instructions':'Example instructions.', 'request_key':'example'}
            with capture_lock(root / 'delivery-lock'), self.assertRaises(OperationError) as raised:
                archive.execute('save_handoff', args)
            self.assertEqual(raised.exception.code, 'write_busy')
            self.assertEqual(list((root / 'handoffs').iterdir()), [])
            self.assertEqual(archive.execute('save_handoff', args)['version'], 'v1')

    def test_read_and_write_use_archive_data_and_installed_runtime_code(self):
        import server
        from import_memory import migrate

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db_path = root / 'memory.sqlite3'
            with patch.object(server, 'DB', db_path):
                server.initialize()
            migrate(db_path)
            # Data downloaded into an archive must never become a Python import path.
            for file in ('inbox_sweep.py', 'server_readonly.py', 'import_memory.py'):
                (root / file).write_text('raise RuntimeError("unsafe import from archive data")')
            archive = Archive(root, root / 'connector-state')
            args = {'packet': {'format': 'threadsatchel/1', 'kind': 'note',
                'messages': [{'text': 'Temporary synthetic connector record.'}]},
                'request_key': 'public-connector-test'}
            saved = archive.execute('save_memory', args)
            self.assertTrue(saved['saved'])
            self.assertEqual(archive.execute('save_memory', args), saved)
            with closing(sqlite3.connect(db_path)) as db:
                [(memory_id,)] = list(db.execute('select id from memories'))
            self.assertEqual(archive.execute('get_memory', {'id':memory_id})['text'],
                             args['packet']['messages'][0]['text'])
