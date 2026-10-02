"""Bounded synthetic tests for preserved extras, safe diagnostics, and retries."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import import_memory
import import_metadata


class ExtraFieldImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='extra-fields-synthetic-')
        self.root = Path(self.temp.name)
        self.db = self.root / 'memory.sqlite3'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT,source TEXT,title TEXT,created_at TEXT)')
            db.execute('CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title)')

    def tearDown(self):
        self.temp.cleanup()

    def write(self, packet, name='packet.json'):
        path = self.root / name
        path.write_bytes(b'\xef\xbb\xbf' + json.dumps(packet, ensure_ascii=False, indent=2).encode('utf-8'))
        return path

    def test_extra_fields_are_exact_inert_metadata_with_safe_persistent_notes(self):
        packet = {'format': 'steve-memory/1', 'kind': 'excerpt',
                  'provenance': {'origin': 'SYNTHETIC private context',
                                 'nested': [None, True, 1, 2.5, {'key': '\u2603'}]},
                  'notes': 'SYNTHETIC supplied note',
                  'import_notes': {'code': 'not-an-importer-notice'},
                  'conversationId': 'do-not-promote-this-id',
                  'timestamp': 'do-not-promote-this-date',
                  'messages': [{'text': '  SYNTHETIC exact text.\n', 'speaker': 'user',
                                'notes': ['message note'], 'source': {'message_id': 'untrusted-id'},
                                'command': {'run': str(self.root / 'must-not-exist')}}]}
        path = self.write(packet)
        raw = path.read_bytes()
        result = import_memory.import_file(path, self.db)
        self.assertEqual(result['import_status'], 'imported_with_warnings')
        self.assertTrue(result['has_warnings'])
        self.assertEqual(result['added_revisions'], 1)
        self.assertEqual([(n['location'], n['field_count']) for n in result['warnings']],
                         [('$', 5), ('$.messages[0]', 3)])
        serialized_notices = json.dumps(result['warnings'])
        self.assertNotIn('SYNTHETIC private', serialized_notices)
        self.assertNotIn('conversationId', serialized_notices)
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse((self.root / 'must-not-exist').exists())
        with closing(sqlite3.connect(self.db)) as db:
            db.row_factory = sqlite3.Row
            self.assertEqual(db.execute('SELECT raw FROM import_objects').fetchone()[0], raw)
            stored = json.loads(db.execute('SELECT metadata_json FROM import_packets').fetchone()[0])
            self.assertEqual(stored, packet)
            entity = db.execute('SELECT conversation_id,message_id,source_order FROM import_entities').fetchone()
            self.assertEqual(tuple(entity), (None, None, None))
            row = db.execute('SELECT * FROM memories').fetchone()
            self.assertEqual(row['text'], packet['messages'][0]['text'])
            full = import_metadata.enrich(db, row)
            provenance = full['import_metadata']['provenance'][0]
            self.assertEqual(provenance['packet']['provenance'], packet['provenance'])
            self.assertEqual(provenance['packet']['notes'], packet['notes'])
            self.assertEqual(provenance['packet']['import_notes'], packet['import_notes'])
            self.assertEqual(provenance['message'], packet['messages'][0])
            self.assertEqual(provenance['import_notes'], result['warnings'])
            self.assertEqual(provenance['import_status'], 'imported_with_warnings')
            persisted = [json.loads(row[0]) for row in db.execute('SELECT warning_json FROM import_warnings')]
            self.assertEqual(persisted, result['warnings'])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_repeat_and_renamed_identical_packet_add_nothing(self):
        packet = {'messages': [{'text': 'SYNTHETIC retry', 'extra': {'value': 3}}],
                  'provenance': {'transport': 'synthetic'}}
        path = self.write(packet)
        first = import_memory.import_file(path, self.db)
        second = import_memory.import_file(path, self.db)
        renamed = self.write(packet, 'renamed.json')
        third = import_memory.import_file(renamed, self.db)
        self.assertEqual(first['added_revisions'], 1)
        for result in (second, third):
            self.assertEqual(result['added_revisions'], 0)
            self.assertEqual(result['repeated_packets'], 1)
            self.assertTrue(result['has_warnings'])
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM memory_fts').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM import_warnings').fetchone()[0], 2)

    def test_standard_canonical_hash_is_unchanged_and_extra_identity_is_inert(self):
        packet = {'format': 'threadsatchel/1', 'messages': [{'text': 'SYNTHETIC stable canonical'}]}
        expected = dict(packet, format='steve-memory/1', kind='excerpt')
        expected_bytes = json.dumps(expected, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')
        result = import_memory.import_file(self.write(packet), self.db)
        self.assertFalse(result['has_warnings'])
        self.assertEqual(result['import_status'], 'imported')
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT packet_key FROM import_packets').fetchone()[0],
                             hashlib.sha256(expected_bytes).hexdigest())
        self.assertEqual(import_memory.import_file(self.root / 'packet.json', self.db)['import_status'],
                         'already_present')

    def test_changed_extras_with_known_identity_preserve_both_provenances(self):
        packet = {'conversation_id': 'synthetic-conversation', 'messages': [
            {'message_id': 'synthetic-message', 'text': 'SYNTHETIC known source'}],
            'provenance': {'revision': 1}}
        self.assertEqual(import_memory.import_file(self.write(packet), self.db)['added_revisions'], 1)
        packet['provenance']['revision'] = 2
        result = import_memory.import_file(self.write(packet, 'more-metadata.json'), self.db)
        self.assertEqual(result['added_revisions'], 0)
        self.assertEqual(result['reused_messages'], 1)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM import_packets').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM memory_fts').fetchone()[0], 1)

    def test_invalid_core_or_nonfinite_values_fail_before_any_archive_write(self):
        valid = {'text': 'SYNTHETIC valid text'}
        packets = [
            {'format': 'unsupported/99', 'messages': [valid]},
            {'kind': [], 'messages': [valid]},
            {'messages': []}, {'messages': ['not-an-object']},
            {'messages': [{'text': ''}]}, {'messages': [{'text': 7}]},
            {'messages': [dict(valid, message_id=[])]},
            {'messages': [dict(valid, source_order=True)]},
            {'messages': [dict(valid, message_id='same'), dict(valid, message_id='same')]},
            {'messages': [valid], 'provenance': {'nested': [float('nan')]}},
            {'messages': [dict(valid, extra=float('inf'))]},
            {'messages': [valid], 'source_date': {'extra': 'invalid known field'}},
        ]
        for packet in packets:
            with self.subTest(packet_index=packets.index(packet)):
                path = self.write(packet)
                raw = path.read_bytes()
                with self.assertRaises(ValueError):
                    import_memory.import_file(path, self.db)
                self.assertEqual(path.read_bytes(), raw)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM memories').fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='import_schema'").fetchone())

    def test_executable_temporary_and_deep_packets_are_still_rejected(self):
        packet = {'messages': [{'text': 'SYNTHETIC text'}], 'provenance': {}}
        for name in ('untrusted.exe', '.incoming-test.json', 'packet.json.part'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                import_memory.import_file(self.write(packet, name), self.db)
        nested = 'leaf'
        for _ in range(101):
            nested = [nested]
        packet['provenance'] = nested
        with self.assertRaises(import_memory.ImportValidationError) as caught:
            import_memory.validate_packet(packet)
        self.assertEqual(import_memory.describe_import_error(caught.exception)['code'], 'invalid_json_value')

    def test_cli_diagnostics_are_specific_without_echoing_private_content(self):
        path = self.write({'messages': [{'text': 'SYNTHETIC private text',
                                       'message_id': {'private_field': 'SYNTHETIC private value'}}]})
        completed = subprocess.run([sys.executable, str(Path(import_memory.__file__)), str(path),
                                    '--db', str(self.db)], capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 1)
        detail = json.loads(completed.stdout)
        self.assertIsInstance(detail['error'], str)
        self.assertEqual(detail['error_detail']['code'], 'invalid_metadata')
        self.assertEqual(detail['error_detail']['location'], '$.messages[0].message_id')
        self.assertNotIn('SYNTHETIC private', completed.stdout + completed.stderr)
        self.assertNotIn('private_field', completed.stdout + completed.stderr)
        self.assertTrue(path.exists())
        for error in (ValueError('SYNTHETIC secret'), RuntimeError('SYNTHETIC secret'),
                      FileNotFoundError('SYNTHETIC secret'), sqlite3.OperationalError('SYNTHETIC secret')):
            self.assertNotIn('SYNTHETIC secret', json.dumps(import_memory.describe_import_error(error)))
        try:
            json.loads('{"private": }')
        except json.JSONDecodeError as error:
            safe = import_memory.describe_import_error(error)
        self.assertEqual(safe['code'], 'invalid_json')
        self.assertEqual(safe['line'], 1)
        self.assertIsInstance(safe['column'], int)


if __name__ == '__main__':
    unittest.main()
