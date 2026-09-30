"""Synthetic, temporary-database checks for bounded retrieval and exact full reads."""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from import_memory import import_file
from import_metadata import enrich, enrich_many
from memory_search import MAX_TEXT, lexical_search, search


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='threadsatchel-search-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'memory.sqlite3'
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT NOT NULL,
            source TEXT NOT NULL,title TEXT,created_at TEXT NOT NULL)''')
        self.db.execute('''CREATE VIRTUAL TABLE memory_fts USING
            fts5(id UNINDEXED,text,source,title,tokenize='unicode61')''')
        self.db.commit()

    def add(self, memory_id, text, title=None, source='synthetic source'):
        record = dict(id=memory_id, text=text, source=source, title=title, created_at='2026-01-01')
        self.db.execute('INSERT INTO memories VALUES (:id,:text,:source,:title,:created_at)', record)
        self.db.execute('INSERT INTO memory_fts VALUES (:id,:text,:source,:title)', record)
        self.db.commit()
        return record

    def packet(self, name, messages):
        path = self.root / name
        path.write_text(json.dumps(dict(format='threadsatchel/1', kind='excerpt',
            title='SYNTHETIC retrieval tests', conversation_id='synthetic-conversation',
            messages=messages)), encoding='utf-8')
        return import_file(path, self.path)

    def test_public_default_off_has_no_network_config_or_side_effect(self):
        self.add('one', 'SYNTHETIC amberquartz searchable sentence.')
        before = {p.name for p in self.root.iterdir()}
        with patch('socket.socket.connect', side_effect=AssertionError('Network attempted')):
            hits = search(self.db, self.root, 'amberquartz')
        self.assertEqual([h['id'] for h in hits], ['one'])
        self.assertEqual(set(hits[0]), {'id', 'text', 'source', 'title', 'created_at'})
        self.assertEqual(before, {p.name for p in self.root.iterdir()})

    def test_compact_excerpt_size_and_matching_fragment(self):
        text = ('SYNTHETIC background words ' * 1000 +
                'amberquartz critical finding ' + 'closing words ' * 1000)
        self.add('long', text, title='t' * 1000, source='s' * 1000)
        hit = lexical_search(self.db, 'amberquartz')[0]
        self.assertLessEqual(len(hit['text']), MAX_TEXT)
        self.assertIn('amberquartz', hit['text'])
        self.assertLessEqual(len(hit['title']), 240)
        self.assertLessEqual(len(hit['source']), 240)
        self.assertLess(len(json.dumps(hit)), 1500)
        self.assertEqual(search(self.db, self.root, 'amberquartz', full=True)[0]['text'], text)
        long_tokens = ('x' * 100 + ' ') * 100 + 'rarecompactneedle ' + ('y' * 100 + ' ') * 100
        self.add('longtokens', long_tokens)
        fragment = lexical_search(self.db, 'rarecompactneedle')[0]['text']
        self.assertLessEqual(len(fragment), MAX_TEXT)
        self.assertIn('rarecompactneedle', fragment)

    def test_exact_id_phrase_and_all_words_precede_or_fallback(self):
        self.add('noise', 'SYNTHETIC amberquartz ' * 12)
        self.add('phrase', 'SYNTHETIC amberquartz cobalt comet', title='reference')
        self.add('separated', 'SYNTHETIC amberquartz and an intervening cobalt comet')
        self.add('opaque-id:abc_123', 'SYNTHETIC entirely different text')
        self.assertEqual(lexical_search(self.db, 'opaque-id:abc_123')[0]['id'], 'opaque-id:abc_123')
        hits = lexical_search(self.db, 'please show me amberquartz cobalt')
        self.assertEqual([h['id'] for h in hits], ['phrase', 'separated', 'noise'])
        self.assertEqual(lexical_search(self.db, '"amberquartz cobalt"')[0]['id'], 'phrase')
        self.assertEqual(lexical_search(self.db, 'amberquartz absenttoken')[0]['id'], 'noise')

    def test_empty_hostile_queries_and_bounds(self):
        for n in range(25):
            self.add(str(n), 'SYNTHETIC amberquartz searchable data')
        for query in ('', '  ', '***', 'NEAR() + - :', 'please show me the memories'):
            self.assertEqual(search(self.db, self.root, query), [])
        for query in ('" OR 1=1; DROP TABLE memories;--', 'amberquartz AND NOT "',
                      'amberquartz:foo* NEAR(bar)', '\x00amberquartz\x00',
                      'amberquartz ' * 10000, 'café 日本語'):
            self.assertLessEqual(len(search(self.db, self.root, query)), 20)
        self.assertEqual(self.db.execute('SELECT count(*) FROM memories').fetchone()[0], 25)
        self.assertEqual(len(search(self.db, self.root, 'amberquartz', limit=1000)), 25)
        self.assertEqual(len(search(self.db, self.root, 'amberquartz', limit=0)), 1)
        self.assertEqual(len(search(self.db, self.root, 'amberquartz', limit=-4)), 1)
        self.assertEqual(len(search(self.db, self.root, 'amberquartz', limit=3)), 3)
        for limit in (True, '2', 1.5, None):
            with self.assertRaises(ValueError):
                search(self.db, self.root, 'amberquartz', limit=limit)
        with self.assertRaises(ValueError):
            search(self.db, self.root, 'amberquartz', full='yes')
        with self.assertRaises(ValueError):
            search(self.db, self.root, None)

    def test_default_twenty_and_maximum_one_hundred(self):
        for n in range(125):
            self.add(str(n), 'SYNTHETIC amberquartz searchable data')
        self.assertEqual(len(search(self.db, self.root, 'amberquartz')), 20)
        self.assertEqual(len(search(self.db, self.root, 'amberquartz', limit=100)), 100)
        self.assertEqual(len(search(self.db, self.root, 'amberquartz', limit=1000)), 100)
        self.assertEqual(len(search(self.db, self.root, 'amberquartz', limit=100, full=True)), 100)

    def test_compact_skips_provenance_and_full_preserves_exact_record(self):
        original = '\ufeffSYNTHETIC amberquartz café\r\n' + 'Long original text. ' * 100
        self.packet('first.json', [dict(text=original, message_id='m1', speaker='user',
                                       source_date='2024-01-02T03:04:05Z')])
        self.packet('revision.json', [dict(text=original + 'edited', message_id='m1', speaker='user')])
        # A repeated source path must still appear in full provenance without duplicates.
        (self.root / 'renamed.json').write_bytes((self.root / 'first.json').read_bytes())
        import_file(self.root / 'renamed.json', self.path)
        statements = []
        self.db.set_trace_callback(statements.append)
        compact = search(self.db, self.root, 'amberquartz')
        self.db.set_trace_callback(None)
        self.assertEqual(len(compact), 2)
        self.assertTrue(all('import_metadata' not in h and 'source_ids' in h for h in compact))
        self.assertTrue(all(len(h['text']) <= MAX_TEXT for h in compact))
        traced = '\n'.join(statements).lower()
        for table in ('import_receipts', 'import_packets', 'import_objects', 'import_items'):
            self.assertNotIn(table, traced)
        full = search(self.db, self.root, 'amberquartz', full=True)
        hit = next(h for h in full if h['text'] == original)
        stored = self.db.execute('SELECT * FROM memories WHERE id=?', (hit['id'],)).fetchone()
        self.assertEqual(hit, enrich(self.db, stored))
        metadata = hit['import_metadata']
        self.assertEqual(metadata['kind'], 'excerpt')
        self.assertEqual(metadata['conversation_id'], 'synthetic-conversation')
        self.assertEqual(metadata['message_id'], 'm1')
        self.assertEqual(len(metadata['revision_ids']), 2)
        self.assertEqual(len(metadata['provenance']), 1)
        provenance = metadata['provenance'][0]
        self.assertEqual(provenance['message']['source_date'], '2024-01-02T03:04:05Z')
        self.assertEqual(len(provenance['paths']), 2)
        self.assertNotIn('messages', provenance['packet'])
        self.assertEqual(set(hit), {'id', 'text', 'source', 'title', 'created_at', 'import_metadata'})

    def test_full_metadata_queries_are_batched(self):
        self.packet('many.json', [dict(text='SYNTHETIC amberquartz item ' + str(n),
                                      message_id='m' + str(n), speaker='user') for n in range(20)])
        rows = list(self.db.execute('SELECT * FROM memories'))
        statements = []
        self.db.set_trace_callback(statements.append)
        records = enrich_many(self.db, rows)
        self.db.set_trace_callback(None)
        self.assertEqual(len(records), 20)
        selects = [s for s in statements if s.lstrip().upper().startswith('SELECT')]
        self.assertEqual(len(selects), 6, selects)
        self.assertEqual(sum('sqlite_master' in s for s in selects), 1)
        self.assertEqual(sum('FROM import_receipts' in s for s in selects), 1)
        self.assertTrue(all(len(h['import_metadata']['provenance']) == 1 for h in records))


    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'Optional MCP test dependency unavailable')
    def test_both_server_tools_preserve_get_and_readonly_connection(self):
        import server
        import server_readonly
        original = self.add('one', 'SYNTHETIC amberquartz\r\nexact café text ' * 100)
        for endpoint in (server, server_readonly):
            with patch.object(endpoint, 'DB', self.path):
                self.assertEqual(endpoint.get_memory('one'), original)
                self.assertLessEqual(len(endpoint.search_memory('amberquartz')[0]['text']), MAX_TEXT)
                self.assertEqual(endpoint.search_memory(query='amberquartz', limit=1, full=True), [original])
                with self.assertRaises(ValueError):
                    endpoint.get_memory('missing')
        with patch.object(server_readonly, 'DB', self.path), closing(server_readonly.connect()) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM memories')







if __name__ == '__main__':
    unittest.main()
