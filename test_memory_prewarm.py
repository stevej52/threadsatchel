"""Synthetic checks for optional RAM preparation; no models or live archive."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import ai_memory as ai
import memory_prewarm as prewarm
from ai_embeddings import VectorIndex, pack_vector
from memory_search import search


class PrewarmTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='memory-prewarm-synthetic-')
        self.root = Path(self.temp.name)
        self.db = sqlite3.connect(self.root / 'memory.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT,source TEXT,title TEXT,created_at TEXT);
            CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title);
            INSERT INTO memories VALUES('one','SYNTHETIC amber robot evidence.','synthetic','Robot','2026-01-01');
            INSERT INTO memory_fts SELECT id,text,source,title FROM memories;''')
        self.db.commit()
        self.config = dict(ai.DEFAULTS, enabled=True, prewarm_enabled=True, embeddings=True,
                           rerank=False, projects={'Robot':['Robot']})
        (self.root/'memory-ai.json').write_text(json.dumps(self.config), encoding='utf-8')
        records, version = ai.current_sources(self.db, self.config)
        self.fingerprint = records[0]['_source_fingerprint']
        with closing(ai.cache_connect(self.root, True)) as cache, cache:
            ai.sync_sources(cache, records, self.config)
            cache.execute('UPDATE chunks SET vector=?,embedding_version=?',
                          (pack_vector([1., 0.]), 'synthetic-model'))
            brief = dict(project='Robot', state='ready', generated=True,
                         facts=[], summaries=[dict(memory_id='one', summary='Synthetic summary',
                            source_fingerprint=self.fingerprint)], coverage={})
            cache.execute('INSERT INTO responses VALUES(?,?,?,?)',
                          ('brief:Robot', version, ai.config_fingerprint(self.config), ai.encoded(brief)))

    def tearDown(self):
        worker = prewarm._workers.pop(str(self.root.resolve()), None)
        if worker is not None:
            worker.stop()
        ai.close_cached_readers(self.root)
        self.db.close()
        self.temp.cleanup()

    def test_prepares_shared_retrieval_and_brief_caches_without_inference_or_writes(self):
        before = (self.root/'memory.sqlite3').read_bytes()
        with patch.object(ai, 'current_sources', wraps=ai.current_sources) as sources, \
             patch('ai_embeddings.VectorIndex', wraps=VectorIndex) as matrices, \
             patch('ai_embeddings.EmbeddingClient', side_effect=AssertionError('No model client')), \
             patch.object(ai, 'LocalChat', side_effect=AssertionError('No generation')):
            first = prewarm.warm_once(self.root)
            second = prewarm.warm_once(self.root)
            self.assertEqual(first['state'], 'ready')
            self.assertEqual((first['source_records'], first['vector_chunks'], first['prepared_projects']), (1, 1, 1))
            self.assertEqual(second['state'], 'ready')
            self.assertEqual(sources.call_count, 1)
            self.assertEqual(matrices.call_count, 1)
            index, _, _ = ai.cached_vector_index(self.root, ai.cache_generation(self.root), 'synthetic-model')
            self.assertEqual(index.top_k([1., 0.])[0][1], 1.)
            brief = ai.project_brief(self.root, self.db, 'Robot')
            brief['summaries'][0]['summary'] = 'caller mutation'
            self.assertEqual(ai.project_brief(self.root, self.db, 'Robot')['summaries'][0]['summary'], 'Synthetic summary')
        self.assertEqual((self.root/'memory.sqlite3').read_bytes(), before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM memories').fetchone()[0], 1)
        self.assertNotIn('Synthetic summary', json.dumps(first))

    def test_wal_source_changes_invalidate_prepared_context(self):
        prewarm.warm_once(self.root)
        self.db.execute("UPDATE memories SET text='SYNTHETIC corrected robot evidence.'")
        self.db.commit()
        result = ai.project_brief(self.root, self.db, 'Robot')
        self.assertFalse(result['generated'])
        self.assertEqual(result['state'], 'pending')

    def test_derived_updates_invalidate_prepared_context_without_archive_changes(self):
        prewarm.warm_once(self.root)
        with closing(ai.cache_connect(self.root, True)) as cache, cache:
            row = cache.execute("SELECT result FROM responses WHERE key='brief:Robot'").fetchone()
            brief = json.loads(row[0]); brief['summaries'][0]['summary'] = 'New validated synthetic summary'
            cache.execute("UPDATE responses SET result=? WHERE key='brief:Robot'", (ai.encoded(brief),))
        self.assertEqual(ai.project_brief(self.root, self.db, 'Robot')['summaries'][0]['summary'],
                         'New validated synthetic summary')

    def test_new_uncited_source_keeps_valid_old_evidence_explicitly_partial(self):
        prewarm.warm_once(self.root)
        self.db.execute("INSERT INTO memories VALUES('two','SYNTHETIC new evidence.','synthetic','Robot','2026-01-02')")
        self.db.commit()
        result = ai.project_brief(self.root, self.db, 'Robot')
        self.assertEqual((result['state'], result['freshness']), ('partial', 'source_updates_pending'))
        self.assertEqual(result['coverage']['current_source_records'], 2)

    def test_background_snapshot_does_not_hold_shared_observer_lock(self):
        entered, release = threading.Event(), threading.Event()
        original = ai.current_sources
        def delayed(db, config):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(db, config)
        result = []
        def run():
            with closing(ai.archive_connect(self.root)) as db:
                result.append(ai.cached_sources(self.root, db, self.config))
        with patch.object(ai, 'current_sources', side_effect=delayed):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                started = time.monotonic()
                ai.cache_generation(self.root)
                self.assertLess(time.monotonic()-started, .5)
            finally:
                release.set(); thread.join(3)
        self.assertEqual(len(result), 1)

    def test_disabled_and_exhausted_budget_are_safe(self):
        self.assertEqual(prewarm.warm_once(self.root, dict(self.config, prewarm_enabled=False))['state'], 'off')
        with patch.object(ai, 'cached_sources', side_effect=TimeoutError):
            result = prewarm.warm_once(self.root)
        self.assertEqual(result['state'], 'budget_exhausted')
        self.assertEqual(self.db.execute('SELECT count(*) FROM memories').fetchone()[0], 1)

    def test_resource_guard_defers_without_calling_warmer(self):
        config = dict(self.config, prewarm_startup_delay_seconds=0)
        worker = prewarm.PrewarmWorker(self.root)
        samples = [dict(available=False, cpu_percent=None, available_mb=64000),
                   dict(available=True, cpu_percent=90., available_mb=64000)]
        def warm(*args, **kwargs):
            self.fail('Busy system must not prewarm')
        waits = iter([False, True])
        def wait(_):
            value = next(waits)
            if value: worker.stop_event.set()
            return value
        with patch.object(ai, 'load_config', return_value=config), \
             patch('memory_resources.ResourceMonitor.sample', side_effect=samples), \
             patch.object(prewarm, 'warm_once', side_effect=warm), \
             patch.object(worker.stop_event, 'wait', side_effect=wait):
            worker._run()
        self.assertEqual(worker.status()['state'], 'deferred_busy')

    def embedding_client(self, calls):
        class Embeddings:
            model_fingerprint = 'synthetic-model'
            def __init__(inner, *args): pass
            def __enter__(inner): return inner
            def __exit__(inner, *args): pass
            def ensure_ready(inner, **kwargs): pass
            def embed(inner, *args, **kwargs):
                calls.append('query')
                return [[1., 0.]]
        return Embeddings

    def test_derived_writes_do_not_thrash_queries_and_total_refresh_delay_is_bounded(self):
        calls = []
        clock = [time.monotonic()]
        with patch('ai_embeddings.EmbeddingClient', self.embedding_client(calls)), \
             patch.object(ai.time, 'monotonic', side_effect=lambda: clock[0]):
            first = search(self.db, self.root, 'unrelatedquery')
            self.assertEqual(first[0]['id'], 'one')
            with closing(ai.cache_connect(self.root, True)) as cache, cache:
                cache.execute('UPDATE chunks SET vector=?', (pack_vector([-1., 0.]),))
            self.assertEqual(search(self.db, self.root, 'unrelatedquery'), first)
            self.assertEqual(len(calls), 1)  # Derived writes preserve the response TTL.
            clock[0] += 29
            self.assertEqual(search(self.db, self.root, 'anotherquery')[0]['id'], 'one')
            self.assertEqual(len(calls), 2)  # Different query can use the bounded old matrix.
            clock[0] += 2
            self.assertEqual(search(self.db, self.root, 'anotherquery'), [])
            self.assertEqual(len(calls), 3)  # No extra 30-second TTL stacked on the matrix.

    def test_original_change_bypasses_both_derived_ttls_immediately(self):
        calls = []
        with patch('ai_embeddings.EmbeddingClient', self.embedding_client(calls)):
            self.assertEqual(search(self.db, self.root, 'unrelatedquery')[0]['id'], 'one')
            self.db.execute("UPDATE memories SET text='SYNTHETIC wholly corrected original.'")
            self.db.commit()
            self.assertEqual(search(self.db, self.root, 'unrelatedquery'), [])
            self.assertEqual(len(calls), 2)

    def test_model_file_and_identity_change_bypasses_both_derived_ttls(self):
        model = self.root/'synthetic.gguf'
        model.write_bytes(b'first-model')
        self.config['embedding_model_path'] = str(model)
        (self.root/'memory-ai.json').write_text(json.dumps(self.config), encoding='utf-8')
        calls = []
        client = self.embedding_client(calls)
        with patch('ai_embeddings.EmbeddingClient', client):
            self.assertEqual(search(self.db, self.root, 'unrelatedquery')[0]['id'], 'one')
            model.write_bytes(b'second-model-content')
            client.model_fingerprint = 'synthetic-second'
            with closing(ai.cache_connect(self.root, True)) as cache, cache:
                cache.execute('UPDATE chunks SET vector=?,embedding_version=?',
                              (pack_vector([-1., 0.]), 'synthetic-second'))
            self.assertEqual(search(self.db, self.root, 'unrelatedquery'), [])
            self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
