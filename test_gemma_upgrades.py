"""Isolated unit tests for ThreadSatchel Gemma-4 multi-slot and upload-vision upgrades."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import ai_memory
import import_memory
import upload_vision


# Valid 1x1 transparent PNG
TINY_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01'
    b'\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
)


class MultiSlotAvailabilityTests(unittest.TestCase):
    def _make_chat(self, responses):
        cfg = dict(ai_memory.DEFAULTS, enabled=True, endpoint='http://127.0.0.1:8090')
        chat = ai_memory.LocalChat(cfg)
        calls = list(responses)

        def fake_request(path, body=None, timeout=2):
            self.assertEqual(path, '/slots')
            item = calls.pop(0) if len(calls) > 1 else calls[0]
            if isinstance(item, Exception):
                raise item
            return item

        chat.request = fake_request
        return chat

    def test_rejects_when_first_slot_busy_with_zero_wait(self):
        chat = self._make_chat([[
            {'id': 0, 'is_processing': True},
            {'id': 1, 'is_processing': False},
        ]])
        self.assertFalse(chat.available(wait_seconds=0))

    def test_waits_briefly_for_all_slots_idle(self):
        chat = self._make_chat([
            [{'id': 0, 'is_processing': True}, {'id': 1, 'is_processing': False}],
            [{'id': 0, 'is_processing': False}, {'id': 1, 'is_processing': False}],
        ])
        self.assertTrue(chat.available(wait_seconds=0.8))

    def test_returns_false_immediately_on_connection_error(self):
        chat = self._make_chat([ai_memory.AnalysisFailure('model_transport_error', 'down')])
        self.assertFalse(chat.available(wait_seconds=2.0))

    def test_returns_false_when_all_slots_stay_busy(self):
        chat = self._make_chat([[{'id': 0, 'is_processing': True}, {'id': 1, 'is_processing': True}]])
        self.assertFalse(chat.available(wait_seconds=0.1))


class UploadVisionTests(unittest.TestCase):
    def test_scan_deduplicates_sha_redacts_secrets_and_imports_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / 'memory'
            (root / 'inbox').mkdir(parents=True)
            (root / '.ai-cache').mkdir(parents=True)
            projects_cfg = {
                'sample-images': ['sample-images'],
                'demo': ['demo'],
            }
            (root / 'memory-ai.json').write_text(json.dumps({
                'enabled': True,
                'endpoint': 'http://127.0.0.1:8090',
                'model': 'gemma-4-26B-A4B-it',
                'projects': projects_cfg,
            }), encoding='utf-8')

            canonical = base / 'canonical'
            up_dir = canonical / 'upload1'
            up_dir.mkdir(parents=True)
            (up_dir / 'meta.json').write_text(json.dumps({
                'id': 'upload1',
                'name': 'sample.png',
                'type': 'image/png',
                'size': len(TINY_PNG),
                'at': 1762900000000,
            }), encoding='utf-8')
            (up_dir / 'sample.png').write_bytes(TINY_PNG)

            sample_uploads = base / 'sample' / 'uploads'
            sample_uploads.mkdir(parents=True)
            (sample_uploads / 'upload1-sample.png').write_bytes(TINY_PNG)

            calls = []

            def fake_caption(config, data_url, filename, project, *, timeout=30.0):
                calls.append((filename, project, data_url.startswith('data:image/png;base64,')))
                return {
                    'title': upload_vision._scrub_secrets('Sample view http://192.0.2.10:8123/api/webhook/secret123'),
                    'summary': 'A test chart with three color patches.',
                    'visible_text': upload_vision._scrub_secrets('Example caption Bearer sk-' + 'a' * 24),
                    'tags': ['chart', 'colors'],
                }

            r1 = upload_vision.scan_once(
                root,
                canonical_dir=canonical,
                project_dirs=[('sample-images', sample_uploads)],
                settle_seconds=0.0,
                caption_fn=fake_caption,
            )
            self.assertEqual(r1['discovered'], 1)
            self.assertEqual(r1['captioned'], 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0], ('sample.png', 'sample-images', True))

            # Second pass skips already-captioned SHA-256
            r2 = upload_vision.scan_once(
                root,
                canonical_dir=canonical,
                project_dirs=[('sample-images', sample_uploads)],
                settle_seconds=0.0,
                caption_fn=fake_caption,
            )
            self.assertEqual(r2['captioned'], 0)
            self.assertEqual(r2['skipped_known'], 1)
            self.assertEqual(len(calls), 1)

            packets = list((root / 'inbox').glob('vision-upload-*.json'))
            self.assertEqual(len(packets), 1)
            raw_text = packets[0].read_text(encoding='utf-8')
            self.assertNotIn('secret123', raw_text)
            self.assertNotIn('sk-' + 'a' * 24, raw_text)
            self.assertIn('[REDACTED_WEBHOOK]', raw_text)

            # Verify inbox_sweep + import_memory accepts the packet into an isolated SQLite DB cleanly
            db_path = root / 'memory.sqlite3'
            with closing(sqlite3.connect(db_path)) as db:
                db.executescript('''
                    CREATE TABLE memories(id TEXT PRIMARY KEY, title TEXT, text TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED, title, text, source);
                ''')
            import_memory.migrate(db_path)
            import inbox_sweep
            res = inbox_sweep.sweep(root, settle_seconds=0)
            self.assertEqual(res['imported_files'], 1)
            self.assertEqual(res['added_revisions'], 1)

            with closing(sqlite3.connect(db_path)) as db:
                db.row_factory = sqlite3.Row
                row = dict(db.execute('SELECT * FROM memories').fetchone())
                cfg = ai_memory.load_config(root)
                self.assertEqual(ai_memory.project_for(row, cfg), 'sample-images')


class OvernightBoostTests(unittest.TestCase):
    def test_window_and_pass_limits_preserve_config_fingerprint(self):
        from datetime import datetime
        import memory_ai

        day_cfg = dict(ai_memory.DEFAULTS, enabled=True, max_chunks_per_pass=20, budget_seconds=115)
        night_cfg = dict(
            day_cfg,
            overnight_boost_enabled=True,
            overnight_start_hour=0,
            overnight_end_hour=7,
            overnight_max_chunks_per_pass=40,
            overnight_budget_seconds=255,
        )
        # Runtime boost settings must never invalidate already-analyzed Gemma chunks
        self.assertEqual(ai_memory.config_fingerprint(day_cfg), ai_memory.config_fingerprint(night_cfg))

        at_2am = datetime(2026, 11, 13, 2, 15)
        at_7am = datetime(2026, 11, 13, 7, 0)
        at_7pm = datetime(2026, 11, 12, 19, 30)

        self.assertTrue(memory_ai.is_overnight_window(night_cfg, now_local=at_2am))
        self.assertFalse(memory_ai.is_overnight_window(night_cfg, now_local=at_7am))
        self.assertFalse(memory_ai.is_overnight_window(night_cfg, now_local=at_7pm))
        self.assertFalse(memory_ai.is_overnight_window(day_cfg, now_local=at_2am))

        boost_2am, max_2am, bud_2am, guard_2am = memory_ai.scheduled_pass_limits(night_cfg, now_local=at_2am)
        self.assertEqual((boost_2am, max_2am, bud_2am, guard_2am), (True, 40, 255, 275))

        boost_7pm, max_7pm, bud_7pm, guard_7pm = memory_ai.scheduled_pass_limits(night_cfg, now_local=at_7pm)
        self.assertEqual((boost_7pm, max_7pm, bud_7pm, guard_7pm), (False, 20, 115, 185))


if __name__ == '__main__':
    unittest.main()
