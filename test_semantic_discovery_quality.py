"""Synthetic semantic discovery, exact-source validation and bounded preparation."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import ai_embeddings as vectors
import ai_memory as memory
import memory_search


class FakeEmbedding:
    calls=0
    model_fingerprint='synthetic-vector-v1'
    def __init__(self,*args,**kwargs):pass
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def ensure_ready(self,allow_launch=True):
        assert allow_launch is False
    def embed(self,texts,query=False):
        FakeEmbedding.calls+=1
        return [[1.,0.,0.] for _ in texts]


class SemanticDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.config=dict(memory.DEFAULTS,enabled=True,embeddings=True,rerank=False)
        (self.root/'memory-ai.json').write_text(json.dumps(self.config),encoding='utf-8')
        self.source=sqlite3.connect(self.root/'memory.sqlite3');self.source.row_factory=sqlite3.Row
        self.source.executescript('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT,source TEXT,title TEXT,created_at TEXT);'
            "CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title);")
        self.put('literal','Joystick code and settings.')
        self.put('meaning','Releasing the control stick must stop wheel motion.')
        self.put('other','Apples and oranges in a basket.')
        self.index();FakeEmbedding.calls=0
    def tearDown(self):
        memory.close_cached_readers(self.root);self.source.close();self.tmp.cleanup()
    def put(self,mid,text):
        self.source.execute('INSERT OR REPLACE INTO memories VALUES(?,?,?,?,?)',(mid,text,'fixture','Original title','2026-11-08'))
        self.source.execute('DELETE FROM memory_fts WHERE id=?',(mid,))
        self.source.execute('INSERT INTO memory_fts VALUES(?,?,?,?)',(mid,text,'fixture','Original title'));self.source.commit()
    def index(self):
        with closing(memory.cache_connect(self.root,True)) as cache:
            memory.sync_sources(cache,memory.source_records(self.source),self.config)
            with cache:
                for mid,vector in [('literal',[.7,.7,0.]),('meaning',[1.,0.,0.]),('other',[0.,1.,0.])]:
                    cache.execute('UPDATE chunks SET vector=?,embedding_version=? WHERE memory_id=?',
                                  (vectors.pack_vector(vector),FakeEmbedding.model_fingerprint,mid))
    def search(self,query='joystick',**patches):
        hits=memory_search.lexical_search(self.source,query,10);diagnostics={}
        with mock.patch.object(vectors,'EmbeddingClient',FakeEmbedding):
            result=memory.enhance_search(self.root,self.source,query,hits,10,diagnostics=diagnostics)
        return result,diagnostics

    def test_semantic_only_match_and_hybrid_provenance(self):
        result,diagnostics=self.search();by_id={row['id']:row for row in result}
        self.assertIn('meaning',by_id);self.assertNotIn('other',by_id)
        self.assertEqual(by_id['meaning']['retrieval_channels'],['embedding'])
        self.assertEqual(by_id['literal']['retrieval_channels'],['embedding','lexical'])
        self.assertEqual(diagnostics['semantic_state'],'used')
        self.assertEqual(diagnostics['semantic_added'],1)

    def test_short_keyword_with_many_literal_hits_still_runs_semantics(self):
        self.put('literal2','Joystick option two.');self.put('literal3','Joystick option three.')
        result,diagnostics=self.search()
        self.assertGreater(FakeEmbedding.calls,0)
        self.assertTrue(any(row['id']=='meaning' for row in result))
        self.assertEqual(diagnostics['semantic_state'],'used')

    def test_no_whole_archive_body_snapshot(self):
        with (mock.patch.object(memory,'cached_sources',side_effect=AssertionError('all-source scan')),
              mock.patch.object(memory,'source_maps',side_effect=AssertionError('all-source map'))):
            result,diagnostics=self.search()
        self.assertEqual(diagnostics['semantic_state'],'used')
        self.assertTrue(any(row['id']=='meaning' for row in result))

    def test_changed_original_excludes_stale_vector_and_invalidates_memo(self):
        result,_=self.search();self.assertTrue(any(row['id']=='meaning' for row in result))
        self.put('meaning','This revision discusses weather, not robot controls.')
        result,diagnostics=self.search()
        self.assertFalse(any(row['id']=='meaning' for row in result))
        self.assertFalse(diagnostics.get('cache_hit',False))

    def test_derived_passage_corruption_cannot_be_returned_as_original(self):
        with closing(memory.cache_connect(self.root,True)) as cache,cache:
            cache.execute("UPDATE chunks SET text='Forged quotation.' WHERE memory_id='meaning'")
        result,_=self.search()
        self.assertFalse(any(row['id']=='meaning' for row in result))

    def test_exact_id_fastpath_and_endpoint_failure_have_honest_status(self):
        result,diagnostics=self.search('literal')
        self.assertEqual(diagnostics['semantic_state'],'skipped_exact');self.assertEqual(FakeEmbedding.calls,0)
        with mock.patch.object(FakeEmbedding,'embed',side_effect=ConnectionError()):
            result,diagnostics=self.search()
        self.assertEqual(diagnostics['semantic_state'],'unavailable')
        self.assertEqual(diagnostics['semantic_returned'],0)
        self.assertEqual([row['id'] for row in result],['literal'])
        result,diagnostics=self.search()
        self.assertEqual(diagnostics['semantic_state'],'used')
        self.assertTrue(any(row['id']=='meaning' for row in result))

    def test_expired_budget_reports_fallback_without_claiming_semantic_use(self):
        with mock.patch.object(memory,'SEARCH_ENHANCEMENT_SECONDS',0):result,diagnostics=self.search()
        self.assertEqual(diagnostics['semantic_state'],'time_budget')
        self.assertEqual(diagnostics['semantic_returned'],0)
        self.assertEqual(result[0]['retrieval_channels'],['lexical'])

    def test_successful_result_memo_keeps_provenance(self):
        first,_=self.search();calls=FakeEmbedding.calls
        second,diagnostics=self.search()
        self.assertEqual(FakeEmbedding.calls,calls)
        self.assertEqual(first,second);self.assertTrue(diagnostics['cache_hit'])

    def test_original_archive_is_never_written(self):
        before=self.source.execute('SELECT * FROM memories').fetchall()
        self.search();self.assertEqual(before,self.source.execute('SELECT * FROM memories').fetchall())

    def test_verified_embedding_location_survives_lexical_first_merge_and_reranking(self):
        text = 'Joystick mention. ' + 'Résumé 🛞. ' * 350 + 'Release the stick to stop the wheels.'
        self.put('meaning', text)
        self.index()
        with closing(memory.cache_connect(self.root, True)) as cache, cache:
            last = cache.execute("SELECT id,start,end FROM chunks WHERE memory_id='meaning' ORDER BY start DESC LIMIT 1").fetchone()
            cache.execute("UPDATE chunks SET vector=? WHERE memory_id='meaning'", (vectors.pack_vector([0.,1.,0.]),))
            cache.execute('UPDATE chunks SET vector=? WHERE id=?', (vectors.pack_vector([1.,0.,0.]), last['id']))
            cache.execute("UPDATE chunks SET vector=? WHERE memory_id='other'", (vectors.pack_vector([.6,.8,0.]),))
        self.assertGreater(last['start'], 600)
        expected = dict(start_char=last['start'], end_char=last['end'], revision=hashlib.sha256(text.encode()).hexdigest())
        query = 'joystick wheel control meaning of stick release'
        (self.root/'memory-ai.json').write_text(json.dumps(dict(self.config, rerank=True)), encoding='utf-8')
        with (mock.patch.object(vectors, 'EmbeddingClient', FakeEmbedding),
              mock.patch.object(memory.LocalChat, 'complete', return_value={'ids':['other','meaning','literal']}) as rerank):
            public = memory_search.search(self.source, self.root, query)
            private = memory_search.search(self.source, self.root, query, include_passages=True)
            self.assertEqual(private[0]['id'], 'other')
            self.assertGreaterEqual(rerank.call_count, 2)
            public_again = memory_search.search(self.source, self.root, query)
        self.assertTrue(all('_semantic_passage' not in hit for hit in public + public_again))
        lexical = memory_search.lexical_search(self.source, query)
        combined = memory_search.rank_discovery_candidates(self.source, query, lexical + private, root=self.root)
        chosen = next(hit for hit in combined if hit['id'] == 'meaning')
        self.assertEqual(chosen['_semantic_passage'], expected)
        self.assertEqual(set(chosen['retrieval_channels']), {'lexical','embedding'})

    def test_stale_embedding_location_never_attaches_to_current_lexical_hit(self):
        with mock.patch.object(vectors, 'EmbeddingClient', FakeEmbedding):
            cached = memory_search.search(self.source, self.root, 'joystick', include_passages=True)
            self.assertTrue(any(hit.get('_semantic_passage') for hit in cached))
            self.put('meaning','Joystick calibration in this new revision.')
            result = memory_search.search(self.source, self.root, 'joystick', include_passages=True)
        revised = next(hit for hit in result if hit['id'] == 'meaning')
        self.assertEqual(revised['retrieval_channels'], ['lexical'])
        self.assertNotIn('_semantic_passage', revised)


class NumericPreparationTests(unittest.TestCase):
    def test_repeated_queries_start_at_most_one_import_worker(self):
        done=mock.Mock()
        with (mock.patch.object(vectors,'_loaded_numpy',return_value=None),
              mock.patch.object(vectors,'_numpy_prepare_started',False),
              mock.patch.object(vectors,'_numpy_prepare_done',done),
              mock.patch.object(vectors.threading,'Thread') as thread):
            vectors.prepare_numpy(deadline=time.monotonic()+1)
            vectors.prepare_numpy(deadline=time.monotonic()+1)
        thread.assert_called_once();thread.return_value.start.assert_called_once()
        self.assertTrue(all(call.args[0]<=.5 for call in done.wait.call_args_list))

    def test_expired_preparation_does_not_launch_thread(self):
        with mock.patch.object(vectors,'_loaded_numpy',return_value=None),mock.patch.object(vectors.threading,'Thread') as thread:
            with self.assertRaises(TimeoutError):vectors.prepare_numpy(deadline=time.monotonic()-1)
        thread.assert_not_called()


if __name__=='__main__':unittest.main()
