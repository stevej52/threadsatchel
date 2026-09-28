"""Bounded synthetic regressions for the AI audit; no live model or archive writes."""
from contextlib import closing
import json
from pathlib import Path
import socketserver
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import ai_memory as ai
from ai_embeddings import VectorIndex, pack_vector, unpack_vector
from memory_search import search


class Chat:
    def available(self): return True
    def analyze(self, row, project):
        return dict(summary=row['text'][:100],keywords=['syntheticneedle'],questions=[],facts=[])


class RemediationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ai-audit-synthetic-')
        self.root=Path(self.temp.name)
        self.db=sqlite3.connect(self.root/'memory.sqlite3')
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT,source TEXT,title TEXT,created_at TEXT);
            CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title);''')
        self.config=dict(ai.DEFAULTS,enabled=True,embeddings=False,rerank=False)
        self.write_config()

    def tearDown(self):
        ai.close_cached_readers(self.root)
        self.db.close()
        self.temp.cleanup()

    def write_config(self):
        (self.root/'memory-ai.json').write_text(json.dumps(self.config),encoding='utf-8')

    def add(self, mid, text, date='2026-01-01'):
        self.db.execute('INSERT INTO memories VALUES(?,?,?,?,?)',(mid,text,'synthetic',None,date))
        self.db.execute('INSERT INTO memory_fts VALUES(?,?,?,?)',(mid,text,'synthetic',None))
        self.db.commit()

    def process(self, **kw):
        return ai.process(self.root,chat_client=Chat(),max_records=kw.pop('max_records',100),budget_seconds=20,**kw)

    def test_whitespace_skipped_and_failed_embeddings_do_not_starve_following_work(self):
        self.add('bad','SYNTHETIC failing record','2026-02-01')
        self.add('good','SYNTHETIC start.'+' '*4000+'SYNTHETIC finish.')
        self.config['embeddings']=True;self.write_config()
        class Embeddings:
            model_fingerprint='synthetic'
            def embed(inner,texts,query=False):
                if 'failing' in texts[0]:raise ValueError('synthetic')
                self.assertTrue(texts[0].strip())
                return [[1.,0.]]
        reports=[self.process(max_records=1,embedding_client=Embeddings(),embeddings_only=True) for _ in range(4)]
        self.assertEqual(reports[0]['embedded_chunks'],0)
        self.assertGreater(sum(r['embedded_chunks'] for r in reports[1:]),0)
        with closing(ai.cache_connect(self.root)) as cache:
            bad=cache.execute("SELECT embedding_attempts,embedding_error,embedding_retry_at FROM chunks WHERE memory_id='bad'").fetchone()
            self.assertEqual(bad['embedding_attempts'],1)
            self.assertEqual(bad['embedding_error'],'ValueError')
            self.assertGreater(bad['embedding_retry_at'],time.time())
            self.assertTrue(all(r[0].strip() for r in cache.execute('SELECT text FROM chunks')))

    def test_runtime_tuning_and_legacy_migration_preserve_analysis_and_vectors(self):
        self.add('one','SYNTHETIC initial evidence')
        self.assertEqual(self.process()['analyzed_chunks'],1)
        old=ai.legacy_config_fingerprint(self.config)
        with closing(ai.cache_connect(self.root,True)) as cache,cache:
            cache.execute('UPDATE chunks SET analysis_version=?,vector=?,embedding_version=?',(old,'[1.0,0.0]','synthetic'))
            cache.execute("UPDATE state SET value=? WHERE key='config_version'",(old,))
        self.assertEqual(self.process()['analyzed_chunks'],0)
        self.assertNotEqual(ai.config_fingerprint(self.config),
                            ai.config_fingerprint(dict(self.config,chat_model_revision='synthetic-new-weights')))
        self.config.update(max_chunks_per_pass=12,embedding_threads=8,chat_timeout_seconds=50)
        self.write_config()
        self.assertEqual(self.process()['analyzed_chunks'],0)
        with closing(ai.cache_connect(self.root)) as cache:
            row=cache.execute('SELECT vector,analysis_version FROM chunks').fetchone()
            self.assertIsInstance(row['vector'],bytes)
            self.assertEqual(unpack_vector(row['vector']),[1.,0.])
            self.assertEqual(row['analysis_version'],ai.config_fingerprint(self.config))
        # Deployment may safely move identical model files using the recorded old settings.
        old_config=dict(self.config,embedding_model_path='synthetic-old-location')
        new_config=dict(self.config,embedding_model_path='synthetic-new-location')
        with closing(ai.cache_connect(self.root,True)) as cache,cache:
            cache.execute('UPDATE chunks SET analysis_version=?',(ai.legacy_config_fingerprint(old_config),))
            cache.execute('UPDATE responses SET config_version=?',(ai.legacy_config_fingerprint(old_config),))
            self.assertGreater(cache.execute('SELECT count(*) FROM responses').fetchone()[0],0)
            self.assertEqual(ai.migrate_analysis_identity(cache,old_config,new_config),1)
            self.assertEqual(cache.execute('SELECT count(*) FROM responses').fetchone()[0],0)
            self.assertEqual(cache.execute('SELECT analysis_version FROM chunks').fetchone()[0],ai.config_fingerprint(new_config))
            incompatible=dict(new_config,chat_model_revision='different-weights')
            self.assertEqual(ai.migrate_analysis_identity(cache,old_config,incompatible),0)

    def test_busy_chat_does_not_starve_embeddings_in_combined_passes_and_held_retry(self):
        for n in range(3):self.add(str(n),'SYNTHETIC separate passage '+str(n))
        self.config['embeddings']=True;self.write_config()
        class Busy(Chat):
            def available(self):return False
        class Embeddings:
            model_fingerprint='synthetic'
            def embed(self,*args,**kwargs):return [[1.,0.]]
        for _ in range(3):
            report=ai.process(self.root,max_records=1,budget_seconds=20,
                chat_client=Busy(),embedding_client=Embeddings())
            self.assertEqual(report['embedded_chunks'],1)
        with closing(ai.cache_connect(self.root,True)) as cache,cache:
            cache.execute('UPDATE chunks SET embedding_attempts=3,embedding_error=? WHERE memory_id=?',('Synthetic','0'))
        from memory_ai import retry_held
        self.assertEqual(retry_held(self.root)['embedding_chunks'],1)

    def test_source_snapshot_cache_is_wal_safe_and_immutable(self):
        self.db.execute('PRAGMA journal_mode=WAL')
        self.add('one','SYNTHETIC initial evidence')
        with patch.object(ai,'source_records',wraps=ai.source_records) as reads:
            before,version=ai.cached_sources(self.root,self.db,self.config)
            again,other=ai.cached_sources(self.root,self.db,self.config)
            self.assertIs(before,again);self.assertEqual(reads.call_count,1)
            with self.assertRaises(TypeError):before[0]['source_ids']['speaker']='poison'
            self.db.execute("UPDATE memories SET text='SYNTHETIC corrected evidence'");self.db.commit()
            after,new_version=ai.cached_sources(self.root,self.db,self.config)
            self.assertNotEqual(version,new_version);self.assertEqual(reads.call_count,2)
            self.assertEqual(after[0]['text'],'SYNTHETIC corrected evidence')
        self.db.execute('BEGIN')
        self.db.execute('SELECT * FROM memories').fetchall()
        with closing(sqlite3.connect(self.root/'memory.sqlite3')) as writer,writer:
            writer.execute("UPDATE memories SET text='SYNTHETIC newer external commit'")
        old,_=ai.cached_sources(self.root,self.db,self.config)
        self.assertEqual(old[0]['text'],'SYNTHETIC corrected evidence')
        self.assertTrue(self.db.in_transaction);self.db.rollback()

    def test_lexical_snapshot_race_never_caches_old_text_under_new_source_identity(self):
        self.add('one','SYNTHETIC original superseded statement')
        self.process()
        old=dict(self.db.execute('SELECT * FROM memories').fetchone())
        self.db.execute("UPDATE memories SET text='SYNTHETIC corrected statement',source='corrected source' WHERE id='one'")
        self.db.commit()
        for _ in range(2):
            result=ai.enhance_search(self.root,self.db,'Which statement about evidence should be retrieved',[old],20)
            self.assertEqual([r['id'] for r in result],['one'])
            self.assertEqual(result[0]['text'],'SYNTHETIC corrected statement')
            self.assertEqual(result[0]['source'],'corrected source')

    def test_long_record_indexing_is_resumable_and_exact(self):
        self.add('long','SYNTHETIC bounded passage. '*5000)
        records,_=ai.current_sources(self.db,self.config)
        ticks=iter([0,0,1])
        with closing(ai.cache_connect(self.root,True)) as cache:
            with patch.object(ai.time,'monotonic',side_effect=lambda:next(ticks,1)):
                count=ai.sync_sources(cache,records,self.config,deadline=.5)
            self.assertEqual(count,1)
            cursor=cache.execute('SELECT cursor FROM indexing').fetchone()[0]
            self.assertGreater(cursor,0)
            ai.sync_sources(cache,records,self.config)
            self.assertEqual(cache.execute('SELECT count(*) FROM indexing').fetchone()[0],0)
            actual=[tuple(r) for r in cache.execute('SELECT start,end,text FROM chunks ORDER BY part')]
            self.assertEqual(actual,list(ai.split_text(records[0]['text'])))

    def test_rank_and_brief_diversity_and_full_semantic_results(self):
        self.add('long','SYNTHETIC passage '*500,'2026-02-01')
        self.add('short','SYNTHETIC separate evidence')
        self.process()
        hits=[dict(id='short',text='SYNTHETIC separate evidence',source='synthetic',title=None,created_at='2026-01-01'),
              dict(id='long',text='SYNTHETIC passage',source='synthetic',title=None,created_at='2026-02-01')]
        enhanced=ai.enhance_search(self.root,self.db,'syntheticneedle',hits,20)
        self.assertEqual(enhanced[0]['id'],'short')
        brief=ai.project_brief(self.root,self.db,'general')
        self.assertEqual({r['memory_id'] for r in brief['summaries']},{'short','long'})
        self.assertEqual(len(brief['summaries']),2)
        compact=search(self.db,self.root,'syntheticneedle')
        full=search(self.db,self.root,'syntheticneedle',full=True)
        self.assertEqual([r['id'] for r in compact],[r['id'] for r in full])
        self.assertGreater(len(next(r['text'] for r in full if r['id']=='long')),600)

    def test_model_io_holds_no_cache_transaction_and_matrix_reused(self):
        self.add('one','SYNTHETIC literal evidence');self.process()
        self.config['embeddings']=True;self.write_config()
        with closing(ai.cache_connect(self.root,True)) as cache,cache:
            cache.execute('UPDATE chunks SET vector=?,embedding_version=?',(pack_vector([1.,0.]),'synthetic'))
        path=self.root/'.ai-cache'/'index.sqlite3'
        calls=[]
        class Embeddings:
            model_fingerprint='synthetic'
            def __init__(inner,*args):pass
            def __enter__(inner):return inner
            def __exit__(inner,*args):pass
            def ensure_ready(inner,allow_launch=False):
                with closing(sqlite3.connect(path,timeout=.1)) as writer,writer:
                    writer.execute("UPDATE state SET value=value WHERE key='config_version'")
                calls.append('ready')
            def embed(inner,*args,**kwargs):return [[1.,0.]]
        with patch('ai_embeddings.EmbeddingClient',Embeddings):
            self.assertEqual(search(self.db,self.root,'unrelatedquery')[0]['id'],'one')
            generation=ai.cache_generation(self.root)
            ai.close_cached_readers(self.root)
            with closing(ai.cache_connect(self.root,True)) as cache,cache:
                cache.execute('UPDATE chunks SET vector=?',(pack_vector([-1.,0.]),))
            self.assertNotEqual(generation,ai.cache_generation(self.root))
            self.assertEqual(search(self.db,self.root,'differentunrelatedquery'),[])
        self.assertEqual(calls,['ready','ready'])

    def test_binary_vectors_and_distinct_memory_topk(self):
        packed=pack_vector([1.,0.]);self.assertLess(len(pack_vector([.12345]*1024)),len(json.dumps([.12345]*1024)))
        index=VectorIndex((str(i),packed) for i in range(6))
        result=index.top_k([1.,0.],k=2,groups={str(i):'long' if i<5 else 'short' for i in range(6)})
        self.assertEqual([cid for cid,_ in result],['0','5'])
        with self.assertRaises(Exception):VectorIndex([('bad',b'NOT A VECTOR')])

    def test_chat_auth_file_is_bounded_and_not_part_of_analysis_identity(self):
        secret=self.root/'synthetic-key.txt';secret.write_text('x'*48)
        config=dict(self.config,chat_api_key_file=str(secret))
        self.assertEqual(ai.config_fingerprint(config),ai.config_fingerprint(self.config))
        with patch('ai_embeddings.bounded_json_request',return_value=[]) as request:
            ai.LocalChat(config).request('/slots')
            self.assertEqual(request.call_args.kwargs['authorization'],'x'*48)
            self.assertEqual(request.call_args.args[3],'/slots')
        secret.write_text('x'*2048)
        with self.assertRaisesRegex(ValueError,'Invalid local chat credential file'):
            ai.LocalChat(config).request('/slots')


class DeadlineTests(unittest.TestCase):
    def test_slow_headers_and_body_respect_total_deadline(self):
        for headers in (True,False):
            class Handler(socketserver.BaseRequestHandler):
                def handle(inner):
                    inner.request.recv(65536)
                    try:
                        if not headers:inner.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n')
                        for byte in (b'HTTP/1.1 200 OK\r\n' if headers else b'                    '):
                            inner.request.sendall(bytes([byte]));time.sleep(.03)
                    except OSError:pass
            with socketserver.TCPServer(('127.0.0.1',0),Handler) as service:
                worker=threading.Thread(target=service.serve_forever,daemon=True);worker.start()
                chat=ai.LocalChat(dict(ai.DEFAULTS,endpoint='http://127.0.0.1:'+str(service.server_address[1])))
                start=time.monotonic()
                with self.assertRaises(Exception):chat.request('/slots',timeout=.1)
                self.assertLess(time.monotonic()-start,.5)
                service.shutdown();worker.join()


if __name__=='__main__':unittest.main()
