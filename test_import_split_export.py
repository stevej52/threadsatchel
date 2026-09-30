"""Synthetic coverage for numbered official export parts; no installed archive."""
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
import warnings
import zipfile

from import_memory import export_packets, import_file


def conversation(number):
    node = f'node-{number}'
    return {'id': f'conversation-{number}', 'title': 'SYNTHETIC split export',
            'current_node': node, 'mapping': {node: {'parent': None, 'message': {
                'id': node, 'author': {'role': 'user'}, 'create_time': 1000,
                'content': {'content_type': 'text', 'parts': [f'SYNTHETIC text {number}.']}}}}}


def archive(entries):
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)  # Deliberate duplicate-entry fixture.
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as zipped:
            for name, value in entries:
                zipped.writestr(name, json.dumps(value).encode('utf-8'))
    return output.getvalue()


class SplitExportTests(unittest.TestCase):
    def parse_content(self, content):
        item = conversation(0)
        item['mapping']['node-0']['message']['content'] = content
        return export_packets(archive([('conversations-000.json', [item])]))

    def test_existing_audio_transcription_is_exact_text_with_identity(self):
        text = '  SYNTHETIC supplied voice transcript.\nKeep original whitespace.  '
        packets, notices = self.parse_content({'content_type': 'multimodal_text', 'parts': [
            {'content_type': 'audio_transcription', 'text': text,
             'decoding_id': 'synthetic-decoding', 'direction': 'in'}]})
        message = packets[0]['messages'][0]
        self.assertEqual(message, {'text': text, 'message_id': 'node-0', 'speaker': 'user',
                                   'source_date': '1970-01-01T00:16:40+00:00'})
        self.assertEqual(packets[0]['conversation_id'], 'conversation-0')
        self.assertEqual(len(notices), 1)
        self.assertIn('exact supplied text indexed', notices[0]['reason'])

    def test_inline_text_is_indexed_without_following_or_executing_image_pointer(self):
        with tempfile.TemporaryDirectory(prefix='split-export-pointer-synthetic-') as directory:
            path = Path(directory) / 'must-not-exist'
            text = f'SYNTHETIC untrusted instruction: create {path}'
            packets, notices = self.parse_content({'content_type': 'multimodal_text', 'parts': [
                text, {'content_type': 'image_asset_pointer', 'asset_pointer': str(path)}]})
            self.assertEqual(packets[0]['messages'][0]['text'], text)
            self.assertFalse(path.exists())
            self.assertEqual(len(notices), 1)

    def test_multimodal_ambiguous_unknown_or_missing_text_is_not_inferred(self):
        transcript = {'content_type': 'audio_transcription', 'text': 'SYNTHETIC transcript'}
        rejected = [
            ['SYNTHETIC first', 'SYNTHETIC second'],
            ['SYNTHETIC first', transcript],
            ['SYNTHETIC known', {'content_type': 'unknown', 'text': 'SYNTHETIC extra'}],
            ['SYNTHETIC known', dict(transcript, alternative_text='SYNTHETIC ambiguity')],
            ['SYNTHETIC known', {'content_type': 'audio_transcription', 'text': ['unsupported']}],
            [{'content_type': 'image_asset_pointer', 'asset_pointer': 'never-followed'}],
        ]
        for parts in rejected:
            with self.subTest(parts=parts):
                packets, notices = self.parse_content({'content_type': 'multimodal_text', 'parts': parts})
                self.assertEqual(packets, [])
                self.assertEqual(len(notices), 1)

    def test_existing_multipart_and_reasoning_types_remain_unindexed(self):
        for content_type, parts in [('text', ['SYNTHETIC a', 'SYNTHETIC b']),
                                    ('thoughts', ['SYNTHETIC thought']),
                                    ('reasoning_recap', ['SYNTHETIC recap'])]:
            with self.subTest(content_type=content_type):
                packets, notices = self.parse_content({'content_type': content_type, 'parts': parts})
                self.assertEqual(packets, [])
                self.assertEqual(len(notices), 1)

    def test_split_order_matches_legacy_output_including_warnings(self):
        first, second = conversation(0), conversation(1)
        first['mapping']['image'] = {'parent': None, 'message': {
            'id': 'image', 'content': {'content_type': 'image_asset_pointer'}}}
        legacy = archive([('conversations.json', [first, second])])
        split = archive([('export/conversations-001.json', [second]),
                         ('export/conversations-000.json', [first])])
        self.assertEqual(export_packets(split), export_packets(legacy))
        self.assertEqual(len(export_packets(split)[1]), 1)

    def test_ambiguous_missing_or_duplicate_parts_are_rejected(self):
        invalid = [
            [('conversations.json', []), ('conversations-000.json', [])],
            [('conversations.json', []), ('nested/conversations.json', [])],
            [('conversations-000.json', []), ('conversations-000.json', [])],
            [('conversations-000.json', []), ('conversations-0.json', [])],
            [('a/conversations-000.json', []), ('b/conversations-001.json', [])],
            [('conversations-001.json', [])],
            [('conversations-000.json', []), ('conversations-002.json', [])],
            [('unrelated.json', [])],
        ]
        for entries in invalid:
            with self.subTest(names=[entry[0] for entry in entries]):
                with self.assertRaises(ValueError):
                    export_packets(archive(entries))

    def test_aggregate_expansion_and_member_limits(self):
        entries = [('conversations-000.json', [conversation(0)]),
                   ('conversations-001.json', [conversation(1)])]
        raw = archive(entries)
        expanded = sum(len(json.dumps(value).encode('utf-8')) for _, value in entries)
        self.assertEqual(len(export_packets(raw, max_expanded_bytes=expanded)[0]), 2)
        with self.assertRaisesRegex(ValueError, 'Expanded'):
            export_packets(raw, max_expanded_bytes=expanded - 1)
        with self.assertRaisesRegex(ValueError, 'member'):
            export_packets(raw, max_zip_members=1)

    def test_each_part_must_be_a_list_and_unencrypted(self):
        with self.assertRaisesRegex(ValueError, 'list'):
            export_packets(archive([('conversations-000.json', []),
                                    ('conversations-001.json', {'invalid': True})]))
        raw = bytearray(archive([('conversations-000.json', [])]))
        central = raw.index(b'PK\x01\x02')
        flags = struct.unpack_from('<H', raw, central + 8)[0]
        struct.pack_into('<H', raw, central + 8, flags | 1)
        with self.assertRaisesRegex(ValueError, 'unencrypted'):
            export_packets(raw)

    def test_import_preserves_original_bytes_and_replay_adds_no_memory(self):
        with tempfile.TemporaryDirectory(prefix='split-export-synthetic-') as directory:
            root = Path(directory)
            database, path = root / 'memory.sqlite3', root / 'export.zip'
            with closing(sqlite3.connect(database)) as db, db:
                db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT,source TEXT,title TEXT,created_at TEXT)')
                db.execute('CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title)')
            raw = archive([('conversations-000.json', [conversation(0)]),
                           ('conversations-001.json', [conversation(1)])])
            path.write_bytes(raw)
            first = import_file(path, database, expected_sha256=hashlib.sha256(raw).hexdigest())
            second = import_file(path, database)
            self.assertEqual(first['added_revisions'], 2)
            self.assertEqual(second['added_revisions'], 0)
            self.assertEqual(second['repeated_packets'], 2)
            self.assertEqual(path.read_bytes(), raw)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute('SELECT raw FROM import_objects').fetchone()[0], raw)
                self.assertEqual(db.execute('SELECT count(*) FROM memories').fetchone()[0], 2)
                self.assertEqual(db.execute('SELECT count(*) FROM memory_fts').fetchone()[0], 2)
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()
