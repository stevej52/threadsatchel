"""Synthetic bounded read-only health checks; no live archive or model calls."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import ai_memory as ai
from ai_embeddings import pack_vector
from import_memory import import_file
import memory_health as health


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-memory-health-')
        self.root = Path(self.temp.name)
        self.config = dict(ai.DEFAULTS, enabled=True, rerank=False)
        (self.root/'memory-ai.json').write_text(json.dumps(self.config),encoding='utf-8')
        self.archive = self.root/'memory.sqlite3'
        with closing(sqlite3.connect(self.archive)) as db:
            db.executescript('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT NOT NULL,source TEXT NOT NULL,title TEXT,created_at TEXT NOT NULL); CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title);')
        self.packet = self.root/'synthetic.json'
        self.packet.write_text(json.dumps(dict(format='threadsatchel/1',kind='excerpt',
            title='SYNTHETIC PRIVATE SECRET CANARY',messages=[dict(speaker='user',text='SYNTHETIC secret canary '+str(i)) for i in range(3)])),encoding='utf-8')
        import_file(self.packet,self.archive)
        with closing(ai.archive_connect(self.root)) as source, closing(ai.cache_connect(self.root,True)) as cache:
            records,_ = ai.current_sources(source,self.config)
            ai.sync_sources(cache,records,self.config)
            with cache:
                for row in cache.execute('SELECT id,text FROM chunks').fetchall():
                    analysis=dict(summary='synthetic',keywords=[],questions=[],facts=[dict(subject='synthetic',key='fixture',value='test',kind='observed',quote=row['text'])])
                    cache.execute('UPDATE chunks SET analysis=?,analysis_version=?,vector=?,embedding_version=? WHERE id=?',
                                  (json.dumps(analysis),ai.config_fingerprint(self.config),pack_vector([1.,0.]),'synthetic',row['id']))

    def tearDown(self):
        ai.close_cached_readers(self.root)
        self.temp.cleanup()

    def test_good_samples_preserve_authority_cache_and_privacy(self):
        paths=[self.archive,self.root/'.ai-cache'/'index.sqlite3',self.packet]
        before={path:hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        report=health.run_health(self.root)
        self.assertEqual(report['state'],'ok',report)
        self.assertEqual(report['checks']['source_sample']['sampled'],3)
        self.assertEqual(report['checks']['original_hash_sample']['sampled'],1)
        self.assertEqual(report['checks']['backup_sample']['verified'],1)
        self.assertFalse(report['full_archive_verified'])
        self.assertFalse(report['repairs_attempted'])
        self.assertFalse(report['checks']['backup_sample']['restore_tested'])
        self.assertEqual(before,{path:hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})
        self.assertEqual(report,health.read_health(self.root))
        raw=json.dumps(report)
        for private in ('CANARY','canary',str(self.root),'synthetic.json'):
            self.assertNotIn(private,raw)
        self.assertFalse(health.health_due(self.root))
        self.assertFalse(list((self.root/'.ai-cache').glob('*.tmp')))

    def test_missing_duplicate_and_mismatched_fts_are_reported(self):
        with closing(sqlite3.connect(self.archive)) as db,db:
            rows=db.execute('SELECT * FROM memories ORDER BY id').fetchall()
            db.execute('DELETE FROM memory_fts WHERE id=?',(rows[0][0],))
            db.execute('INSERT INTO memory_fts SELECT id,text,source,title FROM memories WHERE id=?',(rows[1][0],))
            db.execute('UPDATE memory_fts SET text=? WHERE id=?',('SYNTHETIC changed',rows[2][0]))
        report=health.run_health(self.root)['checks']['archive_structure']
        self.assertEqual(report['state'],'failed')
        self.assertEqual(report['missing_fts_rows'],1)
        self.assertEqual(report['duplicate_fts_rows'],1)
        self.assertEqual(report['mismatched_fts_rows'],1)

    def test_reconciled_alias_is_not_a_missing_search_record(self):
        with closing(sqlite3.connect(self.archive)) as db,db:
            ids=[r[0] for r in db.execute('SELECT id FROM memories ORDER BY id')]
            db.execute('UPDATE import_revisions SET canonical_memory_id=? WHERE memory_id=?',(ids[0],ids[1]))
            db.execute('DELETE FROM memory_fts WHERE id=?',(ids[1],))
        check=health.run_health(self.root)['checks']['archive_structure']
        self.assertEqual(check['state'],'ok',check)
        self.assertEqual(check['searchable_records'],2)

    def test_changed_original_bytes_and_revision_hash_are_detected(self):
        with closing(sqlite3.connect(self.archive)) as db,db:
            db.execute('UPDATE import_objects SET raw=?',(b'SYNTHETIC corrupted',))
            db.execute('UPDATE import_revisions SET text_sha=?',('0'*64,))
        report=health.run_health(self.root)
        self.assertEqual(report['checks']['original_hash_sample']['hash_mismatches'],1)
        self.assertEqual(report['checks']['source_sample']['text_hash_mismatches'],3)
        self.assertEqual(report['state'],'failed')

    def test_invalid_quotes_and_vectors_and_stale_coverage_are_reported(self):
        with closing(ai.cache_connect(self.root,True)) as cache,cache:
            rows=cache.execute('SELECT id,analysis FROM chunks ORDER BY id').fetchall()
            analysis=json.loads(rows[0]['analysis']);analysis['facts'][0]['quote']='SYNTHETIC invented PRIVATE'
            cache.execute('UPDATE chunks SET analysis=?,vector=? WHERE id=?',(json.dumps(analysis),b'TSV1invalid',rows[0]['id']))
            cache.execute('UPDATE records SET fingerprint=?',('stale',))
        report=health.run_health(self.root)
        check=report['checks']['derived_sample']
        self.assertEqual(check['invalid_analysis'],1)
        self.assertEqual(check['invalid_vectors'],1)
        self.assertEqual(report['checks']['source_sample']['stale_derived_records'],3)
        self.assertEqual(report['state'],'failed')
        self.assertNotIn('PRIVATE',json.dumps(report))

    def test_rotating_samples_and_size_bound_are_explicit(self):
        a=health.run_health(self.root,sample_records=1,sample_chunks=1,max_object_bytes=1)
        b=health.run_health(self.root,sample_records=1,sample_chunks=1,max_object_bytes=1)
        self.assertNotEqual(a['cursors']['source'],b['cursors']['source'])
        self.assertNotEqual(a['cursors']['chunks'],b['cursors']['chunks'])
        self.assertEqual(a['checks']['original_hash_sample']['skipped_oversize'],1)
        self.assertEqual(a['checks']['original_hash_sample']['hashed_bytes'],0)
        self.assertEqual(a['state'],'partial')

    def test_corrupt_backup_and_missing_backup_are_not_passes(self):
        bad=self.root/'backups'/'000.sqlite3';bad.write_bytes(b'SYNTHETIC not a database')
        report=health.run_health(self.root)
        self.assertEqual(report['checks']['backup_sample']['state'],'failed')
        bad.unlink()
        for path in (self.root/'backups').iterdir():
            path.unlink()
        report=health.run_health(self.root)
        self.assertEqual(report['checks']['backup_sample']['state'],'attention')
        self.assertEqual(report['checks']['backup_sample']['verified'],0)

    def test_backup_window_skips_derived_indexes_and_advances_each_candidate(self):
        for index in range(3):
            with closing(sqlite3.connect(self.root/'backups'/('00%d.sqlite3'%index))) as db:
                db.execute('CREATE TABLE chunks(id TEXT PRIMARY KEY)')
        report=health.run_health(self.root)
        backup=report['checks']['backup_sample']
        self.assertEqual(backup['state'],'ok',backup)
        self.assertEqual(backup['verified'],1)
        self.assertEqual(backup['candidates_examined'],4)
        self.assertEqual(backup['skipped_nonarchive'],3)
        self.assertEqual(report['cursors']['backup'],4)
        with closing(sqlite3.connect(self.root/'backups'/'003.sqlite3')) as db:
            db.execute('CREATE TABLE chunks(id TEXT PRIMARY KEY)')
        report,cursor=health._backup_sample(self.root,time.monotonic()+5,0)
        self.assertEqual(report['state'],'partial')
        self.assertEqual(report['code'],'no_archive_backup_in_sample_window')
        self.assertEqual(report['candidates_examined'],4)
        self.assertEqual(cursor,4)
        report,cursor=health._backup_sample(self.root,time.monotonic()+5,cursor)
        self.assertEqual(report['verified'],1)
        self.assertEqual(cursor,5)

    def test_sqlite_deadline_interrupts_and_partial_does_not_claim_pass(self):
        with closing(health._connect(self.archive,time.monotonic()-1)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x WHERE n<1000000) SELECT sum(n) FROM x').fetchone()
        error=sqlite3.OperationalError('SYNTHETIC PRIVATE timed out')
        error.sqlite_errorcode=sqlite3.SQLITE_INTERRUPT
        with patch.object(health,'_check_structure',side_effect=error):
            report=health.run_health(self.root)
        self.assertEqual(report['state'],'partial')
        self.assertEqual(report['checks']['archive_structure']['code'],'database_busy_or_budget')
        self.assertNotIn('PRIVATE',json.dumps(report))

    def test_foreign_key_violation_and_invalid_chunk_window_are_detected(self):
        with closing(sqlite3.connect(self.archive)) as db,db:
            db.execute('UPDATE import_revisions SET canonical_memory_id=?',('synthetic-missing-id',))
        with closing(ai.cache_connect(self.root,True)) as cache,cache:
            cache.execute('UPDATE chunks SET end=1000000000')
        report=health.run_health(self.root)
        self.assertEqual(report['checks']['archive_structure']['foreign_key_violations'],3)
        self.assertEqual(report['checks']['derived_sample']['source_mismatches'],3)
        self.assertEqual(report['state'],'failed')

    def test_invalid_limits_and_malformed_report_are_bounded(self):
        for kwargs in ({'budget_seconds':float('nan')},{'sample_records':1000000},{'sample_chunks':True},{'max_hash_bytes':0}):
            with self.assertRaises(ValueError):
                health.run_health(self.root,**kwargs)
        report=self.root/'.ai-cache'/health.REPORT_NAME
        report.write_text('x'*(health.MAX_REPORT_BYTES+1),encoding='utf-8')
        self.assertIsNone(health.read_health(self.root))
        self.assertTrue(health.health_due(self.root))


if __name__=='__main__':
    unittest.main()
