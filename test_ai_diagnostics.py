"""Synthetic selected recovery, strict validation, and metadata-only history checks."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import ai_memory as ai
from ai_embeddings import pack_vector
from memory_ai import retry_held


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='qwen-diagnostics-synthetic-')
        self.root=Path(self.temp.name)
        self.config=dict(ai.DEFAULTS,enabled=True,embeddings=False,rerank=False)
        (self.root/'memory-ai.json').write_text(json.dumps(self.config))
        with closing(sqlite3.connect(self.root/'memory.sqlite3')) as db,db:
            db.executescript('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT,source TEXT,title TEXT,created_at TEXT); CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title);')
            for mid in ('one','two'):
                text='SYNTHETIC evidence for '+mid
                db.execute('INSERT INTO memories VALUES(?,?,?,?,?)',(mid,text,'synthetic',None,'2026-01-01'))
                db.execute('INSERT INTO memory_fts VALUES(?,?,?,?)',(mid,text,'synthetic',None))
        with closing(ai.archive_connect(self.root)) as source,closing(ai.cache_connect(self.root,True)) as cache:
            records,_=ai.current_sources(source,self.config)
            ai.sync_sources(cache,records,self.config)
            with cache:
                cache.execute('INSERT INTO state VALUES(?,?)',('config_version',ai.config_fingerprint(self.config)))
                cache.execute('UPDATE chunks SET attempts=3,error=?,vector=?,embedding_version=?',
                    ('ValueError',pack_vector([1.,0.]),'synthetic-existing-vector'))
            self.ids={r['memory_id']:r['id'] for r in cache.execute('SELECT memory_id,id FROM chunks')}

    def tearDown(self):
        ai.close_cached_readers(self.root);self.temp.cleanup()

    def test_precise_validation_codes_do_not_log_rejected_values(self):
        secret='SYNTHETIC_REJECTED_PRIVATE_VALUE'
        base=dict(summary='synthetic',keywords=[],questions=[],facts=[dict(subject='robot',key='part',value='one',kind='observed',quote='SYNTHETIC evidence')])
        cases=[]
        value=deepcopy(base);value['facts'][0]['kind']=secret;cases.append((value,'fact_kind_invalid',{'field':'kind','index':0}))
        value=deepcopy(base);value['facts'][0]['quote']=secret;cases.append((value,'fact_quote_not_in_source',{'field':'quote','index':0}))
        value=deepcopy(base);value['facts'][0]['value']=None;cases.append((value,'fact_value_type_invalid',{'field':'value','index':0}))
        value=deepcopy(base);value['facts'][0]['value']=' ';cases.append((value,'fact_value_empty',{'field':'value','index':0}))
        value=deepcopy(base);value['keywords']=[secret]*9;cases.append((value,'list_length_invalid',{'field':'keywords','count':9}))
        value=deepcopy(base);value['facts'][0][secret]=secret;cases.append((value,'fact_fields_invalid',{'field':'facts','index':0,'count':6}))
        for value,code,details in cases:
            with self.subTest(code=code),self.assertRaises(ai.AnalysisFailure) as failed:
                ai.validate_analysis(value,{'text':'SYNTHETIC evidence'})
            diagnostic=ai.failure_diagnostic(failed.exception,'analysis')
            self.assertEqual(diagnostic,(code,details))
            self.assertNotIn(secret,json.dumps(diagnostic))
        self.assertEqual(ai.safe_diagnostic_details({'field':secret,'index':0,'count':2,'raw':secret,'exception':secret}),{'index':0,'count':2})
        self.assertEqual(ai.failure_diagnostic(ValueError(secret),'analysis'),('unexpected_error',{}))

    def test_local_chat_token_and_json_errors_are_precise_and_redacted(self):
        chat=ai.LocalChat(self.config)
        for response,code in [
            ({'choices':[{'finish_reason':'length','message':{'content':'SYNTHETIC_PRIVATE'}}]},'model_token_limit'),
            ({'choices':[{'finish_reason':'stop','message':{'content':'SYNTHETIC_PRIVATE invalid json'}}]},'model_json_invalid'),
            ({'choices':[]},'model_response_invalid')]:
            with patch.object(chat,'available',return_value=True),patch.object(chat,'request',return_value=response):
                with self.assertRaises(ai.AnalysisFailure) as failed:chat.analyze({'text':'SYNTHETIC source'},'general')
            self.assertEqual(failed.exception.code,code)
            self.assertNotIn('SYNTHETIC_PRIVATE',str(failed.exception))

    def test_selected_failure_then_success_preserves_other_holds_vectors_and_history(self):
        selected=self.ids['one'];other=self.ids['two']
        before=hashlib.sha256((self.root/'memory.sqlite3').read_bytes()).digest()
        with closing(ai.cache_connect(self.root)) as cache:
            vectors={r['id']:r['vector'] for r in cache.execute('SELECT id,vector FROM chunks')}
        queued=retry_held(self.root,chunk_ids=[selected])
        self.assertEqual(queued['analysis_chunks'],1)
        with closing(ai.cache_connect(self.root)) as cache:
            row=cache.execute('SELECT attempts,error,analysis_retry_requested FROM chunks WHERE id=?',(selected,)).fetchone()
            self.assertEqual(tuple(row),(3,'ValueError',1))
        class Chat:
            def __init__(inner,bad):inner.bad=bad;inner.calls=[]
            def available(inner):return True
            def analyze(inner,row,project):
                inner.calls.append(row['id'])
                facts=[dict(subject='robot',key='part',value='one',kind='observed',quote='SYNTHETIC PRIVATE BAD QUOTE')] if inner.bad else []
                return dict(summary='SYNTHETIC accepted summary',keywords=[],questions=[],facts=facts)
        bad=Chat(True)
        failed=ai.process(self.root,max_records=1,budget_seconds=20,chat_client=bad,chunk_ids=[selected])
        self.assertEqual(bad.calls,[selected])
        self.assertEqual(failed['diagnostics'][0]['code'],'fact_quote_not_in_source')
        self.assertNotIn('PRIVATE BAD QUOTE',json.dumps(failed))
        with closing(ai.cache_connect(self.root)) as cache:
            self.assertEqual(tuple(cache.execute('SELECT attempts,error,analysis_retry_requested FROM chunks WHERE id=?',(selected,)).fetchone()),(4,'ValueError',0))
            self.assertEqual(tuple(cache.execute('SELECT attempts,error,analysis_retry_requested FROM chunks WHERE id=?',(other,)).fetchone()),(3,'ValueError',0))
        retry_held(self.root,chunk_ids=[selected])
        good=Chat(False)
        recovered=ai.process(self.root,max_records=1,budget_seconds=20,chat_client=good,chunk_ids=[selected])
        self.assertEqual(recovered['analyzed_chunks'],1);self.assertEqual(good.calls,[selected])
        with closing(ai.cache_connect(self.root)) as cache:
            self.assertEqual(tuple(cache.execute('SELECT attempts,error,analysis_retry_requested FROM chunks WHERE id=?',(selected,)).fetchone()),(0,None,0))
            self.assertEqual(cache.execute('SELECT attempts FROM chunks WHERE id=?',(other,)).fetchone()[0],3)
            self.assertEqual(vectors,{r['id']:r['vector'] for r in cache.execute('SELECT id,vector FROM chunks')})
        history=ai.diagnostic_history(self.root,chunk_ids=[selected])
        self.assertEqual([e['outcome'] for e in reversed(history)],['retry_requested','failed','retry_requested','succeeded'])
        self.assertEqual(history[0]['attempt'],5)
        self.assertEqual(hashlib.sha256((self.root/'memory.sqlite3').read_bytes()).digest(),before)

    def test_history_is_bounded_and_empty_selection_cannot_retry_everything(self):
        with closing(ai.cache_connect(self.root,True)) as cache,cache,patch.object(ai,'DIAGNOSTIC_LIMIT',5):
            for attempt in range(8):ai.record_diagnostic(cache,self.ids['one'],'analysis','failed',attempt,'unexpected_error',{'raw':'SYNTHETIC PRIVATE'})
            self.assertEqual(cache.execute('SELECT count(*) FROM diagnostic_events').fetchone()[0],5)
        history=ai.diagnostic_history(self.root)
        self.assertEqual([e['attempt'] for e in history],[7,6,5,4,3])
        self.assertNotIn('PRIVATE',json.dumps(history))
        for ids in ([],['not-a-chunk'],[self.ids['one']]*101):
            with self.assertRaises(ValueError):retry_held(self.root,chunk_ids=ids)
            with self.assertRaises(ValueError):ai.process(self.root,chunk_ids=ids)

    def test_selected_retry_does_not_relabel_or_publish_unrelated_stale_evidence(self):
        legacy=ai.legacy_config_fingerprint(self.config)
        with closing(ai.cache_connect(self.root,True)) as cache,cache:
            cache.execute('UPDATE chunks SET analysis=?,analysis_version=?,attempts=0 WHERE memory_id=?',
                (ai.encoded(dict(summary='SYNTHETIC obsolete unrelated conclusion',keywords=[],questions=[],facts=[])),legacy,'two'))
            cache.execute("UPDATE state SET value=? WHERE key='config_version'",(legacy,))
            cache.execute('INSERT INTO state VALUES(?,?)',('archive_version','synthetic-old-generation'))
        with closing(sqlite3.connect(self.root/'memory.sqlite3')) as source,source:
            source.execute("UPDATE memories SET text='SYNTHETIC corrected unrelated conclusion' WHERE id='two'")
        class Chat:
            def available(self):return True
            def analyze(self,*args):return dict(summary='SYNTHETIC selected result',keywords=[],questions=[],facts=[])
        retry_held(self.root,chunk_ids=[self.ids['one']])
        ai.process(self.root,chat_client=Chat(),chunk_ids=[self.ids['one']])
        with closing(ai.cache_connect(self.root)) as cache:
            self.assertEqual(cache.execute("SELECT analysis_version FROM chunks WHERE memory_id='two'").fetchone()[0],legacy)
            self.assertEqual(cache.execute("SELECT value FROM state WHERE key='archive_version'").fetchone()[0],'synthetic-old-generation')
            self.assertEqual(cache.execute('SELECT count(*) FROM responses').fetchone()[0],0)
        with closing(ai.archive_connect(self.root)) as source:
            self.assertFalse(ai.project_brief(self.root,source,'general')['generated'])

    def test_selected_embedding_model_change_does_not_clear_failed_hold(self):
        self.config['embeddings']=True
        (self.root/'memory-ai.json').write_text(json.dumps(self.config))
        selected=self.ids['one']
        with closing(ai.cache_connect(self.root,True)) as cache,cache:
            cache.execute('UPDATE chunks SET embedding_attempts=3,embedding_error=?,embedding_attempt_version=? WHERE id=?',
                ('EmbeddingError','synthetic-old-model',selected))
        class Embeddings:
            model_fingerprint='synthetic-new-model'
            def embed(self,*args,**kwargs):raise ValueError('SYNTHETIC private failure message')
        retry_held(self.root,chunk_ids=[selected])
        ai.process(self.root,embedding_client=Embeddings(),embeddings_only=True,chunk_ids=[selected])
        with closing(ai.cache_connect(self.root)) as cache:
            row=cache.execute('SELECT embedding_attempts,embedding_error,embedding_retry_requested,embedding_attempt_version FROM chunks WHERE id=?',(selected,)).fetchone()
            self.assertEqual(tuple(row),(4,'ValueError',0,'synthetic-new-model'))

    def test_diagnostics_reads_legacy_cache_without_migration(self):
        with closing(ai.cache_connect(self.root,True)) as cache,cache:
            cache.execute('DROP TABLE diagnostic_events')
        before=(self.root/'.ai-cache'/'index.sqlite3').read_bytes()
        self.assertEqual(ai.diagnostic_history(self.root),[])
        self.assertEqual((self.root/'.ai-cache'/'index.sqlite3').read_bytes(),before)

    def test_quote_choice_recovery_is_explicit_bounded_and_still_validated(self):
        text='SYNTHETIC: The proposal was not approved.\r\nUnknown dates stay unknown. '+('café robot evidence '*100)
        choices=ai.recovery_quote_choices(text)
        self.assertGreater(len(choices),0);self.assertLessEqual(len(choices),8)
        self.assertTrue(all(0<len(quote)<=160 and quote in text for quote in choices))
        self.assertEqual([text.find(quote) for quote in choices],sorted(text.find(quote) for quote in choices))
        chat=ai.LocalChat(self.config)
        unchanged=deepcopy(ai.ANALYSIS_SCHEMA)
        with patch.object(chat,'complete',return_value=dict(summary='SYNTHETIC',keywords=[],questions=[],facts=[])) as complete:
            chat.analyze({'text':text,'_quote_recovery':True},'general')
            content=complete.call_args.args[1];schema=complete.call_args.kwargs['response_schema']
            self.assertEqual(content['quote_choices'],choices)
            self.assertEqual(schema['properties']['facts']['maxItems'],1)
            self.assertEqual(schema['properties']['facts']['items']['properties']['quote']['enum'],choices)
            self.assertEqual(complete.call_args.kwargs['max_tokens'],650)
            chat.analyze({'text':text},'general')
            self.assertNotIn('quote_choices',complete.call_args.args[1])
            self.assertEqual(complete.call_args.kwargs['response_schema'],unchanged)
        self.assertEqual(ai.ANALYSIS_SCHEMA,unchanged)
        raw=dict(summary='SYNTHETIC',keywords=[],questions=[],facts=[dict(subject='robot',key='part',value='one',kind='observed',quote='NOT IN SYNTHETIC SOURCE')])
        with self.assertRaises(ai.AnalysisFailure) as error:ai.validate_analysis(raw,{'text':text})
        self.assertEqual(error.exception.code,'fact_quote_not_in_source')
        with self.assertRaises(ValueError):ai.process(self.root,recover_analysis=True)
        selected=self.ids['one'];retry_held(self.root,chunk_ids=[selected])
        seen=[]
        class Chat:
            def available(self):return True
            def analyze(self,row,project):
                seen.append(row.get('_quote_recovery'))
                return dict(summary='SYNTHETIC recovered',keywords=[],questions=[],facts=[])
        report=ai.process(self.root,chat_client=Chat(),chunk_ids=[selected],recover_analysis=True)
        self.assertEqual(seen,[True]);self.assertEqual(report['analyzed_chunks'],1)

    def test_matching_archive_generation_still_rejects_stale_cited_evidence(self):
        with closing(ai.archive_connect(self.root)) as source:
            _,version=ai.current_sources(source,self.config)
        brief=dict(generated=True,state='ready',facts=[],summaries=[dict(memory_id='two',
            summary='SYNTHETIC stale interpretation',source_fingerprint='not-current')])
        with closing(ai.cache_connect(self.root,True)) as cache,cache:
            cache.execute('INSERT INTO responses VALUES(?,?,?,?)',
                ('brief:general',version,ai.config_fingerprint(self.config),ai.encoded(brief)))
        with closing(ai.archive_connect(self.root)) as source:
            result=ai.project_brief(self.root,source,'general')
        self.assertFalse(result['generated']);self.assertEqual(result['state'],'pending')


if __name__=='__main__':unittest.main()
