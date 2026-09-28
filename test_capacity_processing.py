"""Synthetic resource deferral and cache-stability checks; no live services."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import ai_memory as ai


class CapacityProcessingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='capacity-synthetic-')
        self.root=Path(self.temp.name)
        self.config=dict(ai.DEFAULTS,enabled=True,embeddings=False,rerank=False)
        self.write_config()
        with closing(sqlite3.connect(self.root/'memory.sqlite3')) as db,db:
            db.executescript('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT,source TEXT,title TEXT,created_at TEXT);'
                'CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title);')
            for n in range(2):
                row=(str(n),'SYNTHETIC capacity source '+str(n),'synthetic',None,'2030-01-01')
                db.execute('INSERT INTO memories VALUES(?,?,?,?,?)',row)
                db.execute('INSERT INTO memory_fts VALUES(?,?,?,?)',row[:4])

    def tearDown(self):
        ai.close_cached_readers(self.root)
        self.temp.cleanup()

    def write_config(self):
        (self.root/'memory-ai.json').write_text(json.dumps(self.config),encoding='utf-8')

    def test_guard_before_work_creates_no_cache(self):
        result=ai.process(self.root,embeddings_only=True,should_continue=lambda:False)
        self.assertEqual(result['state'],'deferred')
        self.assertFalse((self.root/'.ai-cache').exists())

    def test_guard_between_chunks_preserves_commit_without_failure(self):
        class Embeddings:
            model_fingerprint='synthetic'
            calls=0
            def embed(inner,texts):
                inner.calls+=1
                return [[1.,0.]]
        client=Embeddings()
        self.config['embeddings']=True;self.write_config()
        result=ai.process(self.root,embedding_client=client,embeddings_only=True,
            should_continue=lambda:client.calls<1)
        self.assertEqual(result['state'],'deferred')
        self.assertEqual(result['embedded_chunks'],1)
        with closing(ai.cache_connect(self.root)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM chunks WHERE vector IS NOT NULL').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT sum(embedding_attempts) FROM chunks').fetchone()[0],0)

    def test_noop_pass_does_not_invalidate_foreground_cache(self):
        ai.process(self.root,embeddings_only=True)
        generation=ai.cache_generation(self.root)
        ai.process(self.root,embeddings_only=True)
        self.assertEqual(ai.cache_generation(self.root),generation)

    def test_source_budget_defers_without_marking_model_failures(self):
        with patch.object(ai,'current_sources',side_effect=TimeoutError):
            result=ai.process(self.root,embeddings_only=True,should_continue=lambda:True)
        self.assertEqual((result['state'],result['reason']),('deferred','source_budget'))
        with closing(ai.cache_connect(self.root)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM diagnostic_events').fetchone()[0],0)

    def test_resource_tuning_does_not_invalidate_accepted_interpretations(self):
        tuned=dict(self.config,idle_embeddings=True,prewarm_enabled=True,health_checks_enabled=True,
                   idle_max_chunks=90,prewarm_max_projects=2)
        self.assertEqual(ai.config_fingerprint(tuned),ai.config_fingerprint(self.config))

    def test_limits_fail_closed(self):
        for key,value in [('idle_embeddings',1),('prewarm_enabled','true'),('idle_max_chunks',101),
                          ('idle_min_available_mb',0),('health_budget_seconds',999),('prewarm_budget_seconds',float('nan'))]:
            with self.subTest(key=key):
                (self.root/'memory-ai.json').write_text(json.dumps(dict(self.config,**{key:value})),encoding='utf-8')
                with self.assertRaises(ValueError):ai.load_config(self.root)


if __name__=='__main__':unittest.main()
