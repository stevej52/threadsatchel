"""Restore/replay tests use only temporary synthetic archives."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import inbox_recovery as recovery
from import_memory import import_file, migrate
from inbox_sweep import sweep


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-inbox-recovery-')
        self.root = Path(self.temp.name)
        self.inbox = self.root / 'inbox'
        self.inbox.mkdir()
        self.db = self.root / 'memory.sqlite3'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT,source TEXT,title TEXT,created_at TEXT)')
            db.execute('CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title)')
        migrate(self.db)
        self.base = self.snapshot('base.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self, name):
        path = self.root / name
        with closing(sqlite3.connect(self.db)) as src, closing(sqlite3.connect(path)) as dest:
            src.backup(dest)
        return path

    def file(self, name, text=None):
        path = self.inbox / name
        path.write_bytes(json.dumps({'format': 'threadsatchel/1', 'kind': 'note',
            'messages': [{'text': text or ('SYNTHETIC separate recovery message ' + name)}]}).encode())
        return path

    def count(self):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute('SELECT count(*) FROM memories').fetchone()[0]

    def tracked_import(self, journal, run, path):
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        journal.before_import(run, path, sha, path.stat().st_mtime_ns, self.base)
        result = import_file(path, self.db, expected_sha256=sha)
        journal.verified_import(run, path, sha, self.db)
        return result

    def import_pair(self):
        first, second = self.file('z-oldest.json'), self.file('a-newer.json')
        with recovery.RecoveryJournal(self.root) as journal:
            journal.reconcile(self.db)
            run = journal.begin_pass()
            self.tracked_import(journal, run, first)
            middle = self.snapshot('middle.sqlite3')
            self.tracked_import(journal, run, second)
            journal.finish_pass(run)
        return first, second, middle

    def test_restore_blocks_retry_failed_even_when_state_deleted(self):
        self.file('a.json'); self.file('b.json')
        self.assertEqual(sweep(self.root, settle_seconds=0)['imported_files'], 2)
        (self.root / 'inbox-sweep' / 'state.json').unlink()
        shutil.copyfile(self.base, self.db)
        result = sweep(self.root, settle_seconds=0, retry_failed=True)
        self.assertEqual(result['state'], 'recovery_required')
        self.assertEqual(result['recovery']['held_files'], 2)
        self.assertEqual(self.count(), 0)
        state = json.loads((self.root / 'inbox-sweep' / 'state.json').read_text())
        self.assertTrue(all(item['status'] == 'held' for item in state['files'].values()))

    def test_same_pass_survivor_is_held_then_each_replay_advances_once(self):
        first, second, middle = self.import_pair()
        shutil.copyfile(middle, self.db)  # first receipt survives, second is lost
        with recovery.RecoveryJournal(self.root) as journal:
            status = journal.reconcile(self.db)
        self.assertEqual(status['held_files'], 2)
        with patch.object(recovery, 'quick_check', wraps=recovery.quick_check) as checks:
            result = recovery.replay_next(self.root)
        self.assertEqual(checks.call_count, 2)
        self.assertEqual(result['file'], first.name)
        self.assertEqual(result['replayed'], 1)
        self.assertEqual(result['held_files'], 1)
        self.assertEqual(result['added_revisions'], 0)
        self.assertEqual(self.count(), 1)
        result = recovery.replay_next(self.root)
        self.assertEqual(result['file'], second.name)
        self.assertFalse(result['active'])
        self.assertEqual(self.count(), 2)

    def test_postcheck_failure_stays_held_despite_committed_receipt(self):
        first, second, _ = self.import_pair()
        shutil.copyfile(self.base, self.db)
        with patch.object(recovery, 'quick_check', side_effect=[True, RuntimeError('synthetic check failure')]):
            failed = recovery.replay_next(self.root)
        self.assertEqual(failed['state'], 'recovery_required')
        self.assertEqual(failed['file'], first.name)
        self.assertEqual(failed['last_failure']['stage'], 'postcheck')
        self.assertEqual(failed['last_failure']['error_type'], 'RuntimeError')
        self.assertEqual(self.count(), 1)  # commit happened before the failed check
        self.assertEqual(sweep(self.root, settle_seconds=0, retry_failed=True)['state'], 'recovery_required')
        retried = recovery.replay_next(self.root)
        self.assertEqual(retried['file'], first.name)
        self.assertEqual(retried['added_revisions'], 0)
        self.assertTrue(retried['active'])
        self.assertEqual(recovery.replay_next(self.root)['file'], second.name)

    def test_interrupted_pass_holds_all_intents_even_with_receipts(self):
        first, second = self.file('first.json'), self.file('second.json')
        with recovery.RecoveryJournal(self.root) as journal:
            journal.reconcile(self.db)
            run = journal.begin_pass()
            self.tracked_import(journal, run, first)
            sha = hashlib.sha256(second.read_bytes()).hexdigest()
            journal.before_import(run, second, sha)
            import_file(second, self.db, expected_sha256=sha)
            # Crash: no verified_import for second and no finish_pass.
        self.assertEqual(self.count(), 2)
        result = sweep(self.root, settle_seconds=0, retry_failed=True)
        self.assertEqual(result['state'], 'recovery_required')
        self.assertEqual(result['recovery']['held_files'], 2)

    def test_bootstrap_order_uses_receipt_time_not_latest_state_check(self):
        first, second = self.file('z-oldest.json'), self.file('a-newer.json')
        results = [import_file(path, self.db) for path in (first, second)]
        with closing(sqlite3.connect(self.db)) as db, db:
            for path, when in [(first, '2000-01-01T00:00:00+00:00'), (second, '2001-01-01T00:00:00+00:00')]:
                db.execute('UPDATE import_receipts SET first_seen=? WHERE path=?', (when, str(path)))
        state = {'files': {path.name: {'status': 'imported', 'sha256': result['sha256'], 'checked_at': when}
            for path, result, when in [(first, results[0], '2040-01-01'), (second, results[1], '2030-01-01')]}}
        with recovery.RecoveryJournal(self.root) as journal:
            journal.reconcile(self.db, state)
        shutil.copyfile(self.base, self.db)
        result = recovery.replay_next(self.root)
        self.assertEqual(result['file'], first.name)

    def test_prepare_restore_validates_but_never_restores_database(self):
        self.import_pair()
        before = self.db.read_bytes()
        result = recovery.prepare_restore(self.root, self.base)
        self.assertFalse(result['restored'])
        self.assertTrue(result['active'])
        self.assertEqual(result['held_files'], 2)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.count(), 2)

    def test_changed_oldest_input_blocks_later_inputs(self):
        first, second, _ = self.import_pair()
        shutil.copyfile(self.base, self.db)
        first.write_bytes(first.read_bytes() + b' ')
        result = recovery.replay_next(self.root)
        self.assertEqual(result['file'], first.name)
        self.assertEqual(result['replayed'], 0)
        self.assertEqual(result['last_failure']['stage'], 'validate')
        self.assertTrue(result['active'])
        self.assertEqual(self.count(), 0)

    def test_new_restore_after_success_reholds_lost_receipts(self):
        self.import_pair()
        shutil.copyfile(self.base, self.db)
        recovery.replay_next(self.root)
        recovery.replay_next(self.root)
        with recovery.RecoveryJournal(self.root) as journal:
            self.assertFalse(journal.reconcile(self.db)['active'])
        shutil.copyfile(self.base, self.db)
        with recovery.RecoveryJournal(self.root) as journal:
            status = journal.reconcile(self.db)
        self.assertTrue(status['active'])
        self.assertEqual(status['held_files'], 2)

    def test_missing_receipt_inventory_cannot_be_cleared_by_quick_check(self):
        self.db.unlink()
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('CREATE TABLE unrelated(x)')
        result = recovery.replay_next(self.root)
        self.assertTrue(result['active'])
        self.assertEqual(result['reason'], 'archive_receipts_unavailable')
        self.assertEqual(result['state'], 'recovery_required')

    def test_different_current_options_never_replace_known_receipt_identity(self):
        path = self.file('options.json')
        result = import_file(path, self.db)
        with recovery.RecoveryJournal(self.root) as journal:
            journal.reconcile(self.db)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE import_receipts SET options_json='{" + '"project":"other"' + "}'")
        with recovery.RecoveryJournal(self.root) as journal:
            status = journal.reconcile(self.db)
            self.assertEqual(journal.db.execute('SELECT receipt_options FROM files').fetchone()[0], '{}')
        self.assertEqual(status['held_files'], 1)
        self.assertTrue(status['active'])

    def test_legacy_custom_receipt_bootstrap_does_not_hold_or_reimport(self):
        path = self.inbox / 'legacy.md'
        path.write_text('SYNTHETIC legacy markdown with retained custom title.', encoding='utf-8')
        options = {'kind': 'excerpt', 'title': 'SYNTHETIC retained title'}
        result = import_file(path, self.db, **options)
        state = {'files': {path.name: {'status': 'already_present', 'sha256': result['sha256'], 'checked_at': '2040-01-01'}}}
        with recovery.RecoveryJournal(self.root) as journal:
            status = journal.reconcile(self.db, state)
            saved = journal.db.execute('SELECT receipt_options FROM files').fetchone()[0]
        self.assertFalse(status['active'])
        self.assertEqual(json.loads(saved), options)
        self.assertEqual(sweep(self.root, settle_seconds=0)['already_present'], 1)
        self.assertEqual(self.count(), 1)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM import_receipts').fetchone()[0], 1)

    def test_restored_custom_receipt_replays_with_original_metadata(self):
        path = self.inbox / 'legacy.md'
        path.write_text('SYNTHETIC legacy markdown to replay exactly.', encoding='utf-8')
        options = {'kind': 'excerpt', 'title': 'SYNTHETIC original title', 'speaker': 'user'}
        import_file(path, self.db, **options)
        with recovery.RecoveryJournal(self.root) as journal:
            self.assertFalse(journal.reconcile(self.db)['active'])
            expected_options = journal.db.execute('SELECT receipt_options FROM files').fetchone()[0]
        shutil.copyfile(self.base, self.db)
        replayed = recovery.replay_next(self.root)
        self.assertEqual(replayed['replayed'], 1)
        self.assertFalse(replayed['active'])
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT options_json FROM import_receipts').fetchone()[0], expected_options)
            self.assertEqual(db.execute('SELECT title FROM memories').fetchone()[0], options['title'])

    def test_ambiguous_receipt_options_remain_held(self):
        path = self.file('ambiguous.json')
        import_file(path, self.db)
        import_file(path, self.db, title='SYNTHETIC other options')
        with recovery.RecoveryJournal(self.root) as journal:
            status = journal.reconcile(self.db)
        self.assertTrue(status['active'])
        self.assertTrue(status['files'][0]['options_ambiguous'])
        with patch('import_memory.import_file') as importer:
            replayed = recovery.replay_next(self.root)
        importer.assert_not_called()
        self.assertTrue(replayed['active'])
        self.assertEqual(replayed['replayed'], 0)

    def test_options_seed_does_not_automatically_clear_old_false_hold(self):
        path = self.inbox / 'legacy.md'
        path.write_text('SYNTHETIC old baseline false hold.', encoding='utf-8')
        imported = import_file(path, self.db, kind='excerpt', title='SYNTHETIC legacy')
        with recovery.RecoveryJournal(self.root) as journal:
            with journal.db:
                row = journal._register(path, imported['sha256'], receipt_seen=True, state='verified')
                journal._hold_ids([row['ordinal']], 'prior_receipt_missing_after_restore')
                journal.db.execute('UPDATE files SET missing_latched=1')
            status = journal.reconcile(self.db)
            self.assertIsNotNone(journal.db.execute('SELECT receipt_options FROM files').fetchone()[0])
        self.assertTrue(status['active'])
        self.assertEqual(status['held_files'], 1)

    def test_no_intent_failure_does_not_hold_an_empty_queue(self):
        with recovery.RecoveryJournal(self.root) as journal:
            journal.reconcile(self.db)
            run = journal.begin_pass()
            status = journal.fail_pass(run, 'synthetic_pre_import_failure')
        self.assertFalse(status['active'])
        self.assertEqual(status['held_files'], 0)


if __name__ == '__main__':
    unittest.main()
