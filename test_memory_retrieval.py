import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import memory_budget as budget
import memory_retrieval as retrieval
from memory_listing import list_records
from test_memory_discovery import use_test_encoding_when_offline


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        use_test_encoding_when_offline(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = sqlite3.connect(self.root / 'memory.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT,title TEXT,source TEXT,created_at TEXT);
            CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title,tokenize='unicode61');''')

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def add(self, id, text, title='Title', source='conversation://example'):
        self.db.execute('INSERT INTO memories VALUES(?,?,?,?,?)', (id,text,title,source,'2026-11-01T12:00:00Z'))
        self.db.execute('INSERT INTO memory_fts VALUES(?,?,?,?)', (id,text,source,title))
        self.db.commit()

    def assert_bounded(self, result):
        self.assertLessEqual(budget.transport_cost(result), result['budget']['charged'])
        self.assertGreaterEqual(result['budget']['remaining'], 0)
        self.assertLessEqual(result['budget']['charged'], 2000)

    def resolve_hit(self, result, index=0):
        return budget.resolve_reference(self.root, result['lookup_id'], result['hits'][index]['ref'])

    def test_original_unicode_crlf_and_cr_slices_and_revision(self):
        text = '# Café\r\n🌊 e\u0301 Straße\r\ndef exact():\r\n    return "漢字"\r\n'
        self.add('unicode', text)
        result = retrieval.get_memory_slice(self.db,self.root,'unicode')
        self.assertEqual(result['text'], text)
        self.assertEqual(result['revision'], hashlib.sha256(text.encode()).hexdigest())
        start, end = text.index('🌊'), text.index('def')
        sliced = retrieval.get_memory_slice(self.db,self.root,'unicode',revision=result['revision'],
                                           start_char=start,end_char=end)
        self.assertEqual(sliced['text'],text[start:end])
        self.assertEqual(sliced['end_char']-sliced['start_char'],len(sliced['text']))
        blocks = retrieval.source_blocks('one\rtwo\rthree\r')
        self.assertEqual(blocks[0]['end_line'],3)
        self.assert_bounded(sliced)

    def test_fence_length_and_partition_never_loses_bytes(self):
        text = '# Notes\r\nIntroduction\r\n````python\r\ndef a():\r\n    x = "```"\r\n```\r\n# Still code\r\n````\r\n## End\r\n' + 'α'*4000
        blocks = retrieval.source_blocks(text)
        self.assertEqual(''.join(text[b['start_char']:b['end_char']] for b in blocks),text)
        fenced = [b for b in blocks if b['kind']=='fenced_code']
        self.assertEqual(len(fenced),1)
        self.assertIn('# Still code', text[fenced[0]['start_char']:fenced[0]['end_char']])
        self.assertEqual(blocks,retrieval.source_blocks(text))
        self.assertTrue(all(b['end_char']-b['start_char']<=retrieval.MAX_BLOCK
                            for b in blocks if b['kind']!='fenced_code'))

    def test_complete_long_fenced_code_by_block_id_and_oversize_atomic_refusal(self):
        function='def set_deadband(value):\n'+''.join(f'    threshold_{i} = value + {i}\n' for i in range(80))+'    return value\n'
        self.assertGreater(len(function),2300)
        fence='```python\n'+function+'```\n'
        text='# Context\nA tested implementation follows.\n'+fence+'# End\n'
        self.add('fenced',text)
        with mock.patch('memory_search.search',return_value=[]):
            found=retrieval.compact_search(self.db,self.root,'set_deadband',limit=1)
        hit=self.resolve_hit(found)
        peek=retrieval.peek_memory(self.db,self.root,ref=found['hits'][0]['ref'],lookup_id=found['lookup_id'],before=0,after=0)
        block=next(b for b in retrieval.source_blocks(text) if b['block_id']==peek['block_id'])
        self.assertEqual(block['kind'],'fenced_code')
        self.assertEqual(text[block['start_char']:block['end_char']],fence)
        result=retrieval.get_memory_slice(self.db,self.root,'fenced',revision=hit['revision'],block_id=peek['block_id'])
        self.assertEqual(result['status'],'ok')
        self.assertTrue(result['complete'])
        self.assertEqual(result['text'],fence)
        self.assert_bounded(result)
        giant='```python\n'+function*25+'```\n'
        self.add('giant',giant)
        giant_block=retrieval.source_blocks(giant)[0]
        refused=retrieval.get_memory_slice(self.db,self.root,'giant',revision=retrieval.source_revision(giant),
                                           block_id=giant_block['block_id'])
        self.assertEqual(refused['status'],'range_too_large')
        self.assertNotIn('text',refused)
        self.assertGreater(refused['suggested_end_char'],0)
        continuation=retrieval.get_memory_slice(self.db,self.root,'giant',revision=refused['revision'],
            start_char=0,end_char=refused['suggested_end_char'])
        self.assertEqual(continuation['text'],giant[:refused['suggested_end_char']])

    def test_literal_spans_original_casefold_accents_and_combining(self):
        text = '🌊 Straße café e\u0301\r\nneedle'
        spans = retrieval.lexical_spans(text,'STRASSE cafe é needle')
        self.assertEqual([text[m['start_char']:m['end_char']] for m in spans], ['Straße','café','e\u0301','needle'])

    def test_function_identifier_underscore_matches_have_exact_offsets(self):
        text='def set_deadband(x): return x'
        self.add('function',text)
        with mock.patch('memory_search.search',return_value=[]):
            result=retrieval.compact_search(self.db,self.root,'set_deadband',limit=1)
        item=self.resolve_hit(result)
        self.assertEqual(text[item['start_char']:item['end_char']],'set')
        self.assertEqual([text[m['start_char']:m['end_char']] for m in retrieval.lexical_spans(text,'set_deadband')],['set','deadband'])

    def test_stale_revision_and_cross_revision_match_are_rejected(self):
        self.add('m','before needle after')
        revision = retrieval.source_revision('before needle after')
        match = retrieval.lexical_spans('before needle after','needle')[0]
        self.db.execute('UPDATE memories SET text=? WHERE id=?',('changed needle','m'))
        self.db.commit()
        result = retrieval.get_memory_slice(self.db,self.root,'m',revision=revision,start_char=0,end_char=6)
        self.assertEqual(result['status'],'stale_revision')
        self.assertNotIn('text',result)
        current = retrieval.source_revision('changed needle')
        with self.assertRaisesRegex(ValueError,'does not belong'):
            retrieval.peek_memory(self.db,self.root,'m',current,match_id=match['match_id'])

    def test_large_full_and_exact_ranges_refuse_without_truncation_then_paginate(self):
        text = ''.join(f'def function_{i}():\r\n    return "🌊漢字{i}"\r\n' for i in range(200))
        self.add('code',text)
        revision = retrieval.source_revision(text)
        result = retrieval.get_memory_slice(self.db,self.root,'code',full=True)
        self.assertEqual(result['status'],'range_too_large')
        self.assertNotIn('text',result)
        self.assertGreater(result['suggested_end_char'],0)
        pieces=[]
        for start in range(0,len(text),300):
            page=retrieval.get_memory_slice(self.db,self.root,'code',revision=revision,
                start_char=start,end_char=min(start+300,len(text)))
            self.assertEqual(page['status'],'ok')
            self.assert_bounded(page)
            pieces.append(page['text'])
        self.assertEqual(''.join(pieces),text)

    def test_map_pages_revision_required_and_index_replaces_old_revision(self):
        text=''.join(f'# Section {i}\n'+'content '*200+'\n' for i in range(20))
        self.add('map',text)
        first=retrieval.get_memory_slice(self.db,self.root,'map')
        self.assertEqual(first['kind'],'map')
        self.assertLessEqual(len(first['blocks']),5)
        cursor=first['next_start_char']
        second=retrieval.get_memory_slice(self.db,self.root,'map',revision=first['revision'],start_char=cursor)
        self.assertEqual(second['blocks'][0]['start_char'],cursor)
        missing=retrieval.get_memory_slice(self.db,self.root,'map',start_char=0,end_char=10)
        self.assertEqual(missing['status'],'revision_required')
        self.db.execute('UPDATE memories SET text=? WHERE id=?',('updated','map'))
        self.db.commit()
        retrieval.get_memory_slice(self.db,self.root,'map')
        cache=budget.cache_connect(self.root)
        try:
            self.assertEqual(cache.execute('SELECT COUNT(DISTINCT revision) FROM blocks WHERE memory_id=?',('map',)).fetchone()[0],1)
            self.assertEqual(cache.execute('SELECT COUNT(*) FROM block_fts WHERE memory_id=?',('map',)).fetchone()[0],1)
        finally:
            cache.close()

    def test_index_version_change_rebuilds_old_window_boundaries(self):
        text='```python\n'+'x = 1\n'*400+'```\n'
        self.add('versioned',text)
        retrieval.get_memory_slice(self.db,self.root,'versioned')
        cache=budget.cache_connect(self.root)
        with cache:
            cache.execute('UPDATE source_maps SET index_version=0')
            cache.execute('UPDATE blocks SET end_char=1200,kind="fenced_code_window"')
        cache.close()
        revision,blocks=retrieval._index(self.root,retrieval._record(self.db,'versioned'))
        self.assertEqual(len(blocks),1)
        self.assertEqual(blocks[0]['end_char'],len(text))
        self.assertEqual(blocks[0]['kind'],'fenced_code')

    def test_hybrid_recall_literal_priority_and_duplicate_hits(self):
        self.add('literal','needle is here')
        self.add('semantic','the circuit protects the load')
        hit=dict(id='semantic',text='the circuit protects the load')
        with mock.patch('memory_search.search',return_value=[hit,hit]):
            result=retrieval.compact_search(self.db,self.root,'needle',limit=3)
        self.assertEqual([self.resolve_hit(result,i)['memory_id'] for i in range(len(result['hits']))],['literal','semantic'])
        self.assertEqual(result['hits'][1]['match_kind'],'semantic')
        self.assert_bounded(result)

    def test_search_title_match_without_fabricated_body_span(self):
        self.add('m','completely different body',title='needle guide')
        with mock.patch('memory_search.search',return_value=[]):
            result=retrieval.compact_search(self.db,self.root,'needle')
        item=result['hits'][0]
        self.assertEqual(self.resolve_hit(result)['start_char'],self.resolve_hit(result)['end_char'])
        self.assertEqual(item['match_kind'],'metadata')

    def test_oversized_uncached_record_searches_without_fts_cache(self):
        self.add('big','prefix needle suffix '*200)
        with mock.patch.object(retrieval,'MAX_INDEX_CHARS',10),mock.patch('memory_search.search',return_value=[]):
            result=retrieval.compact_search(self.db,self.root,'needle',limit=1)
        match=self.resolve_hit(result)
        self.assertEqual(match['memory_id'],'big')
        self.assertEqual(('prefix needle suffix '*200)[match['start_char']:match['end_char']],'needle')

    def test_cumulative_lookup_scope_and_peak_exact_match(self):
        text='prefix '*50+'needle'+' suffix'*50
        self.add('m',text)
        self.add('other','unrelated')
        with mock.patch('memory_search.search',return_value=[]):
            result=retrieval.compact_search(self.db,self.root,'needle',limit=1)
        item=result['hits'][0]
        peek=retrieval.peek_memory(self.db,self.root,ref=item['ref'],lookup_id=result['lookup_id'])
        self.assertEqual(peek['text'],text[peek['start_char']:peek['end_char']])
        self.assertIn('needle',peek['text'])
        self.assertLess(peek['budget']['remaining'],result['budget']['remaining'])
        self.assertLessEqual(result['budget']['charged']+peek['budget']['charged'],2000)
        with self.assertRaisesRegex(ValueError,'outside this lookup'):
            retrieval.get_memory_slice(self.db,self.root,'other',lookup_id=result['lookup_id'])

    def test_catalogue_is_interpretation_with_exact_evidence_pointer(self):
        self.add('m','needle was corrected yesterday')
        entry=dict(suggested_title='Correction',summary='An earlier claim was corrected.',
            summary_source=dict(start_char=0,end_char=30,chunk_id='c'),topics=['needle'],
            evidence=[dict(label='corrected_as_reported',quote='corrected',start_char=11,end_char=20)])
        with mock.patch('memory_catalogue.catalogue_for_record',return_value=entry):
            result=retrieval.get_memory_slice(self.db,self.root,'m')
        catalogue=result['catalogue']
        self.assertEqual(catalogue['trust'],'interpretation')
        self.assertFalse(catalogue['action_authority'])
        self.assertEqual(catalogue['evidence'][0]['label'],'corrected_as_reported')
        self.assertNotIn('truth_score',catalogue)

    def test_listing_budget_cursor_completeness_and_no_raw_dump(self):
        for index in range(12):
            self.add(f'm{index:03d}','long body '*1000)
        found,cursor=[],None
        while True:
            page=list_records(self.db,limit=100,cursor=cursor,root=self.root)
            self.assert_bounded(page)
            self.assertTrue(all(len(row['text'])<=120 for row in page['records']))
            found.extend(row['id'] for row in page['records'])
            cursor=page['next_cursor']
            if cursor is None:
                break
        self.assertEqual(len(found),12)
        self.assertEqual(len(set(found)),12)

    def test_session_hint_is_partial_cited_and_bounded(self):
        self.add('m','needle appears here'+'\nSource content '*2000)
        self.add('cited','A previous design discussion')
        entry=dict(conversation_id='chat-a',suggested_title='Design discussion',
            coverage=dict(partial=False,archive_records=2),
            summaries=[dict(text='An earlier design was discussed.',memory_id='cited',
                revision=retrieval.source_revision('A previous design discussion'),
                source=dict(start_char=0,end_char=27))])
        with mock.patch('memory_catalogue.conversation_for_record',return_value=entry):
            result=retrieval.get_memory_slice(self.db,self.root,'m')
        conversation=result['conversation']
        self.assertTrue(conversation['partial'])
        self.assertFalse(conversation['action_authority'])
        self.assertEqual(conversation['summary']['memory_id'],'cited')
        cited=retrieval.get_memory_slice(self.db,self.root,'cited',lookup_id=result['lookup_id'])
        self.assertEqual(cited['text'],'A previous design discussion')
        self.assert_bounded(result)

    def test_fallback_bounds_unicode_metadata_and_counts_whole_response(self):
        self.add('m','🌊'*10000,title='漢'*5000,source='path'*5000)
        with mock.patch.object(budget,'_encoders',return_value=[]):
            result=retrieval.get_memory_slice(self.db,self.root,'m',full=True)
            self.assertEqual(result['budget']['method'],'json_utf8_byte_upper_bound')
            self.assert_bounded(result)
            self.assertLessEqual(len(json.dumps(budget.transport_template(result),ensure_ascii=True,indent=2).encode())+64,2000)

    def test_quota_cleanup_ttl_and_scope_rows(self):
        cache=budget.cache_connect(self.root)
        with cache:
            cache.execute('INSERT INTO lookups VALUES(?,?,?,?,?)',('old',2000,0,'',0))
            cache.execute('INSERT INTO lookup_sources VALUES(?,?)',('old','m'))
        cache.close()

        cache=budget.cache_connect(self.root)
        self.assertEqual(cache.execute('SELECT COUNT(*) FROM lookups WHERE id="old"').fetchone()[0],0)
        self.assertEqual(cache.execute('SELECT COUNT(*) FROM lookup_sources').fetchone()[0],0)
        cache.close()

    def test_cold_tokenizer_returns_first_usable_ref(self):
        self.add('cold','A sampled amber keyword starts this passage. '*16,title='Large unicode title 🌊'*16)
        with mock.patch.object(budget,'_encoders',return_value=[]):
            found=retrieval.compact_search(self.db,self.root,'amber',limit=1)
            self.assertEqual(len(found['hits']),1)
            self.assertTrue(found['hits'][0]['preview'])
            self.assertEqual(self.resolve_hit(found)['memory_id'],'cold')
            self.assert_bounded(found)


if __name__=='__main__':
    unittest.main()
