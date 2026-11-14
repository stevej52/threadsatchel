"""Synthetic-only regressions for background navigation aids and resource guards."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import ai_memory as memory
import memory_ai as worker
import memory_catalogue as catalogue


class FakeChat:
    def __init__(self, bad=False, legacy=False):
        self.calls=0;self.bad=bad;self.legacy=legacy
    def available(self):return True
    def analyze(self, record, project):
        self.calls+=1
        quote=record['text'][:300]
        result=dict(summary='Joystick calibration with reported test results.',
            keywords=['joystick','deadband'],questions=[],facts=[dict(subject='joystick',
                key='test',value='Reported result',kind='observed',quote='invented missing evidence' if self.bad else quote)])
        if not self.legacy:result['suggested_title']='Joystick deadband calibration'
        return result


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.config=dict(memory.DEFAULTS,enabled=True,embeddings=False)
        (self.root/'memory-ai.json').write_text(json.dumps(self.config),encoding='utf-8')
        self.source=sqlite3.connect(self.root/'memory.sqlite3')
        self.source.executescript('''CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT,source TEXT,title TEXT,created_at TEXT);
            CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title);''')
        self.text='All tests passed for café 🌊.\r\n```python\r\ndef deadband(x):\r\n    return x\r\n```\r\n'
        self.put('a',self.text)
    def tearDown(self):
        self.source.close();memory.close_cached_readers(self.root);self.tmp.cleanup()
    def put(self,ident,text):
        self.source.execute('INSERT OR REPLACE INTO memories VALUES(?,?,?,?,?)',
                            (ident,text,'fixture://local','Original conversation title','2026-11-01'))
        self.source.execute('DELETE FROM memory_fts WHERE id=?',(ident,))
        self.source.execute('INSERT INTO memory_fts VALUES(?,?,?,?)',(ident,text,'fixture://local','Original conversation title'))
        self.source.commit()
    def run_pass(self,chat=None,**kwargs):
        return memory.process(self.root,chat_client=chat or FakeChat(),max_records=100,budget_seconds=10,**kwargs)
    def read(self,ident='a',text=None):
        return catalogue.catalogue_for_record(self.root,ident,catalogue.text_revision(text or self.text))
    def attach_conversation(self,ident,cid):
        self.source.executescript('''CREATE TABLE IF NOT EXISTS import_entities(id TEXT PRIMARY KEY,conversation_id TEXT,message_id TEXT,speaker TEXT);
            CREATE TABLE IF NOT EXISTS import_revisions(memory_id TEXT PRIMARY KEY,entity_id TEXT,canonical_memory_id TEXT);''')
        self.source.execute('INSERT OR REPLACE INTO import_entities VALUES(?,?,?,?)',('entity-'+ident,cid,'message-'+ident,'user'))
        self.source.execute('INSERT OR REPLACE INTO import_revisions VALUES(?,?,?)',(ident,'entity-'+ident,ident))
        self.source.commit()

    def test_exact_source_refs_unicode_titles_and_archive_unchanged(self):
        before=self.source.execute('SELECT * FROM memories').fetchall()
        report=self.run_pass()
        self.assertEqual(report['analyzed_chunks'],1)
        self.assertEqual(report['catalogued_records'],1)
        item=self.read()
        self.assertEqual(item['suggested_title'],'Joystick deadband calibration')
        self.assertTrue(item['interpretation']);self.assertFalse(item['action_authority'])
        self.assertEqual(item['offset_unit'],'unicode_code_points')
        evidence=item['evidence'][0]
        self.assertEqual(evidence['label'],'tested_as_reported')
        self.assertEqual(self.text[evidence['start_char']:evidence['end_char']],evidence['quote'])
        self.assertIn('deadband',item['topics'])
        self.assertEqual(before,self.source.execute('SELECT * FROM memories').fetchall())

    def test_unchanged_pass_does_not_repeat_inference_or_rebuild_catalogue(self):
        chat=FakeChat();self.run_pass(chat)
        first=self.read();calls=chat.calls
        result=self.run_pass(chat)
        self.assertEqual(chat.calls,calls)
        self.assertEqual(result['catalogued_records'],0)
        self.assertEqual(self.read(),first)

    def test_legacy_validated_summaries_get_titles_without_new_model_call(self):
        self.run_pass(FakeChat(legacy=True))
        item=self.read()
        self.assertEqual(item['suggested_title'],'Joystick calibration with reported test results.')
        with closing(memory.cache_connect(self.root,True)) as cache,cache:
            cache.execute('DELETE FROM catalogue')
        chat=FakeChat();self.run_pass(chat)
        self.assertEqual(chat.calls,0)
        self.assertIsNotNone(self.read())

    def test_changed_source_rejects_old_refs_and_reprocesses_only_changed_record(self):
        self.put('b','Other original text.');self.run_pass()
        changed='Tests failed. This version is not verified.\r\n'
        self.put('a',changed)
        self.assertIsNone(self.read(text=changed))
        chat=FakeChat();report=self.run_pass(chat)
        self.assertEqual(chat.calls,1)
        self.assertEqual(report['analyzed_chunks'],1)
        self.assertIsNone(self.read())
        current=self.read(text=changed)
        self.assertEqual(current['evidence'][0]['label'],'unknown')
        self.assertEqual(current['evidence'][0]['quote'],changed)

    def test_hallucinated_quote_cannot_be_persisted_as_evidence(self):
        result=self.run_pass(FakeChat(bad=True))
        self.assertEqual(result['analyzed_chunks'],0)
        self.assertIsNone(self.read())
        with closing(memory.cache_connect(self.root)) as cache:
            self.assertEqual(cache.execute('SELECT count(*) FROM catalogue').fetchone()[0],0)

    def test_ambiguous_or_negated_success_stays_unknown(self):
        for text in ['Tests passed','Not all tests passed','If tests passed, deploy','Tests passed but failure remains',
                     'Tests passed?','The code says "tests passed"']:
            if text=='Tests passed':self.assertEqual(catalogue._evidence_label(text,'observed'),'tested_as_reported')
            else:self.assertEqual(catalogue._evidence_label(text,'observed'),'unknown')
        self.assertEqual(catalogue._evidence_label('Maybe this is superseded','superseded'),'unknown')

    def test_invalid_title_and_old_analysis_contract(self):
        valid=FakeChat().analyze(dict(text=self.text),'general')
        self.assertEqual(memory.validate_analysis(valid,dict(text=self.text))['suggested_title'],'Joystick deadband calibration')
        valid['suggested_title']='x'*121
        with self.assertRaises(ValueError):memory.validate_analysis(valid,dict(text=self.text))
        del valid['suggested_title']
        self.assertNotIn('suggested_title',memory.validate_analysis(valid,dict(text=self.text)))

    def test_background_stream_does_not_use_full_body_snapshot(self):
        with (mock.patch.object(memory,'source_records',side_effect=AssertionError('whole archive materialized')),
              mock.patch.object(memory,'current_sources',side_effect=AssertionError('whole archive cached'))):
            self.run_pass()

    def test_stream_identity_matches_existing_snapshot_and_keeps_no_text(self):
        with closing(memory.archive_connect(self.root)) as source,closing(memory.cache_connect(self.root,True)) as cache:
            original,expected=memory.current_sources(source,self.config)
            compact,actual,indexed=memory.sync_background_sources(source,cache,self.config,deadline=time.monotonic()+5)
            self.assertEqual(expected,actual)
            self.assertEqual(len(original),len(compact))
            self.assertTrue(all('text' not in row for row in compact))
            self.assertEqual(indexed,1)

    def test_removed_source_catalogue_is_pruned(self):
        self.run_pass()
        self.source.execute("DELETE FROM memories WHERE id='a'")
        self.source.execute("DELETE FROM memory_fts WHERE id='a'")
        self.source.commit();self.run_pass()
        self.assertIsNone(self.read())

    def test_read_does_not_create_missing_database(self):
        self.assertIsNone(self.read())
        self.assertFalse((self.root/'.ai-cache').exists())

    def test_expired_budget_or_resource_denial_does_not_run_model(self):
        chat=FakeChat()
        result=self.run_pass(chat,should_continue=lambda:False)
        self.assertEqual(result['state'],'deferred');self.assertEqual(chat.calls,0)
        with closing(memory.archive_connect(self.root)) as source,closing(memory.cache_connect(self.root,True)) as cache:
            count=catalogue.refresh_catalogues(cache,source,memory.config_fingerprint(self.config),deadline=time.monotonic()-1)
            self.assertEqual(count,0)

    def test_scheduled_worker_honors_low_memory_and_cpu_limits(self):
        for sample,reason in [(dict(available=True,cpu_percent=1,available_mb=100),'low_memory'),
                              (dict(available=True,cpu_percent=95,available_mb=99999),'cpu_busy'),
                              (dict(available=False,cpu_percent=None,available_mb=None),'resource_unavailable')]:
            monitor=mock.Mock();monitor.sample.return_value=sample
            with mock.patch.object(worker,'process_with_lock_retry') as run,mock.patch.object(worker,'lower_process_priority'):
                result=worker.run_scheduled_process(self.root,monitor=monitor,warmup=lambda _:None)
            run.assert_not_called();self.assertEqual(result['reason'],reason)
        self.assertTrue((self.root/'.ai-cache'/'last-scheduled-run.json').exists())

    def test_scheduled_worker_uses_one_existing_processor_with_guard(self):
        monitor=mock.Mock();monitor.sample.return_value=dict(available=True,cpu_percent=1,available_mb=99999)
        with (mock.patch.object(worker,'process_with_lock_retry',return_value=dict(state='ok')) as run,
              mock.patch.object(worker,'lower_process_priority')):
            worker.run_scheduled_process(self.root,monitor=monitor,warmup=lambda _:None)
        run.assert_called_once()
        self.assertTrue(run.call_args.kwargs['should_continue']())

    def test_transient_cpu_burst_retries_without_weakening_guard(self):
        low=dict(available=True,cpu_percent=5,available_mb=99999)
        busy=dict(available=True,cpu_percent=80,available_mb=99999)
        monitor=mock.Mock();monitor.sample.side_effect=[low,busy,low]
        pauses=[]
        with (mock.patch.object(worker,'process_with_lock_retry',return_value=dict(state='ok')) as run,
              mock.patch.object(worker,'lower_process_priority')):
            result=worker.run_scheduled_process(self.root,monitor=monitor,warmup=pauses.append,budget_seconds=999)
        run.assert_called_once()
        self.assertEqual(pauses,[1.05,3.0])
        self.assertEqual(result['resource_wait_seconds'],3)
        self.assertEqual(run.call_args.kwargs['budget_seconds'],120)
        self.assertEqual(result['resources']['cpu_percent'],5)
        self.assertIn('at',result)

    def test_persistent_cpu_pressure_has_bounded_wait_and_visible_reason(self):
        monitor=mock.Mock();monitor.sample.return_value=dict(available=True,cpu_percent=95,available_mb=99999)
        pauses=[]
        with mock.patch.object(worker,'process_with_lock_retry') as run:
            result=worker.run_scheduled_process(self.root,monitor=monitor,warmup=pauses.append)
        run.assert_not_called()
        self.assertEqual(sum(pauses[1:]),15)
        self.assertEqual(result['reason'],'cpu_busy')
        self.assertEqual(result['resource_limits']['max_cpu_percent'],25)

    def test_durable_progress_survives_deferral_and_rolling_diagnostics(self):
        self.run_pass()
        with closing(memory.cache_connect(self.root,True)) as cache,cache:
            cache.execute('DELETE FROM diagnostic_events')
        monitor=mock.Mock();monitor.sample.return_value=dict(available=True,cpu_percent=1,available_mb=100)
        result=worker.run_scheduled_process(self.root,monitor=monitor,warmup=lambda _:None)
        self.assertEqual(result['analyzed_chunks'],0)
        self.assertEqual(result['analysis_coverage']['analyzed_chunks'],1)
        self.assertEqual(result['analysis_coverage']['analysis_completed_since_tracking'],1)
        self.assertIn('last_analysis_progress_at',result['analysis_coverage'])

    def test_worker_exception_records_type_and_prior_committed_progress(self):
        self.run_pass()
        monitor=mock.Mock();monitor.sample.return_value=dict(available=True,cpu_percent=1,available_mb=99999)
        with mock.patch.object(worker,'process_with_lock_retry',side_effect=RuntimeError('private source text')):
            result=worker.run_scheduled_process(self.root,monitor=monitor,warmup=lambda _:None)
        self.assertEqual(result['state'],'failed')
        self.assertEqual(result['error_type'],'RuntimeError')
        self.assertNotIn('private source',json.dumps(result))
        self.assertEqual(result['analysis_coverage']['analysis_completed_since_tracking'],1)

    def prepare_priority_records(self):
        self.put('b','Recent source.');self.put('c','Opened older source.');self.put('d','Oldest source.')
        self.source.executemany('UPDATE memories SET created_at=? WHERE id=?',[
            ('2026-11-08','b'),('2026-10-01','c'),('2020-01-01','d')]);self.source.commit()
        self.run_pass()
        with closing(memory.cache_connect(self.root,True)) as cache,cache:
            cache.execute('UPDATE chunks SET analysis_version=NULL')

    def queue(self,cache,limit):
        return memory.queued_chunks(cache,self.root,analysis_ready=True,
            analysis_version=memory.config_fingerprint(self.config),embedding_version='',max_records=limit)

    def test_opened_sources_receive_priority_but_old_backlog_gets_slot(self):
        self.prepare_priority_records()
        self.assertTrue(memory.note_retrieved(self.root,['c']))
        with closing(memory.cache_connect(self.root,True)) as cache:
            rows=self.queue(cache,3)
        self.assertEqual([row['memory_id'] for row in rows],['d','c','b'])

    def test_single_slot_analysis_rotation_independent_of_idle_embedding_passes(self):
        self.prepare_priority_records()
        self.assertTrue(memory.note_retrieved(self.root,['c']))
        with closing(memory.cache_connect(self.root,True)) as cache,cache:
            cache.execute("INSERT OR REPLACE INTO state VALUES('queue_analysis_passes','0')")
            ids=[]
            for _ in range(4):
                ids.append(self.queue(cache,1)[0]['memory_id'])
                memory.queued_chunks(cache,self.root,analysis_ready=False,
                    analysis_version=memory.config_fingerprint(self.config),embedding_version='',max_records=1)
        self.assertEqual(ids,['c','c','c','d'])

    def test_priority_queue_is_capped_expires_and_never_changes_archive(self):
        self.run_pass();before=self.source.execute('SELECT * FROM memories').fetchall()
        for start in range(0,300,30):self.assertTrue(memory.note_retrieved(self.root,[str(n) for n in range(start,start+30)]))
        with closing(sqlite3.connect(self.root/'.ai-cache/background-priority.sqlite3')) as hints,hints:
            self.assertEqual(hints.execute('SELECT count(*) FROM retrieved').fetchone()[0],256)
            hints.execute('UPDATE retrieved SET last_opened=0')
        self.assertEqual(memory._retrieved_priorities(self.root),[])
        self.assertEqual(before,self.source.execute('SELECT * FROM memories').fetchall())

    def test_outcome_card_only_links_existing_source_claims(self):
        self.run_pass()
        card=self.read()['outcome_card']
        self.assertTrue(card['interpretation']);self.assertTrue(card['partial'])
        self.assertEqual(set(card['stages']),{'result'})
        result=card['stages']['result']
        self.assertEqual(self.text[result['start_char']:result['end_char']],result['quote'])
        self.assertEqual(result['label'],'tested_as_reported')
        self.assertIsNone(catalogue._outcome_role('If tests passed, we would deploy.','observed','unknown'))
        self.assertIsNone(catalogue._outcome_role('We never decided to deploy.','decided','unknown'))

    def test_conversation_map_groups_original_ids_with_explicit_coverage(self):
        self.put('b','Tests failed for the earlier attempt.')
        self.attach_conversation('a','chat-1');self.attach_conversation('b','chat-1')
        result=self.run_pass()
        self.assertEqual(result['catalogued_conversations'],1)
        item=catalogue.conversation_for_record(self.root,'a',catalogue.text_revision(self.text))
        self.assertEqual(item['conversation_id'],'chat-1')
        self.assertEqual(item['coverage']['archive_records'],2)
        self.assertEqual(item['coverage']['catalogued_records'],2)
        self.assertTrue(item['coverage']['partial'])  # Duplicate summaries are represented once.
        self.assertEqual(item['members'][0]['memory_id'],'a')
        self.assertEqual(item['freshness'],'cited_sources_checked')
        self.assertTrue(item['interpretation']);self.assertNotIn('_source_fingerprints',item)
        self.assertEqual(self.source.execute("SELECT title FROM memories WHERE id='a'").fetchone()[0],'Original conversation title')

    def test_conversation_rejects_stale_cited_source_or_changed_membership(self):
        btext='A second record.';self.put('b',btext)
        self.attach_conversation('a','chat-1');self.attach_conversation('b','chat-1');self.run_pass()
        self.put('a','The earlier information is obsolete.')
        self.assertIsNone(catalogue.conversation_for_record(self.root,'b',catalogue.text_revision(btext)))
        self.run_pass()
        self.attach_conversation('b','chat-2')
        self.assertIsNone(catalogue.conversation_for_record(self.root,'b',catalogue.text_revision(btext)))
        self.run_pass()
        item=catalogue.conversation_for_record(self.root,'b',catalogue.text_revision(btext))
        self.assertEqual(item['conversation_id'],'chat-2')

    def test_conversation_identifier_is_never_truncated_into_another_chat(self):
        self.attach_conversation('a','a'*513);self.run_pass()
        self.assertIsNone(self.read()['conversation_id'])
        self.assertIsNone(catalogue.conversation_for_record(self.root,'a',catalogue.text_revision(self.text)))

    def test_new_chat_record_marks_cached_conversation_coverage_partial(self):
        self.attach_conversation('a','chat-1');self.run_pass()
        before=catalogue.conversation_for_record(self.root,'a',catalogue.text_revision(self.text))
        self.assertFalse(before['coverage']['partial'])
        self.put('b','A new correction awaiting analysis.');self.attach_conversation('b','chat-1')
        after=catalogue.conversation_for_record(self.root,'a',catalogue.text_revision(self.text))
        self.assertTrue(after['coverage']['partial'])
        self.assertEqual(after['coverage']['archive_records_current'],2)
        self.assertFalse(after['coverage']['background_counts_current'])


if __name__=='__main__':unittest.main()
