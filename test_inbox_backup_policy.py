"""Hourly backup/retention regression tests over synthetic temporary archives."""
from contextlib import closing
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import inbox_sweep as sweep_module
from import_memory import migrate


class InboxBackupPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='inbox-backup-policy-')
        self.root = Path(self.temporary.name)
        self.folder = self.root / 'backups' / 'inbox-sweep'
        self.folder.mkdir(parents=True)
        (self.root / 'inbox').mkdir()
        (self.root / 'inbox-sweep').mkdir()
        self.db_path = self.root / 'memory.sqlite3'
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT,source TEXT,title TEXT,created_at TEXT)')
            db.execute('CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title)')
            db.execute("INSERT INTO memories VALUES('original','SYNTHETIC original','test',NULL,'2000-01-01')")
            db.execute("INSERT INTO memory_fts VALUES('original','SYNTHETIC original','test',NULL)")
        self.now = int(time.time())

    def tearDown(self):
        self.temporary.cleanup()

    def seed(self, name, mtime):
        suffix = int.from_bytes(hashlib.sha256(name.encode()).digest()[:4], 'big') % 1000000
        path = self.folder / f'before-20000101T000000{suffix:06d}Z.sqlite3'
        with closing(sqlite3.connect(self.db_path)) as src, closing(sqlite3.connect(path)) as dest:
            src.backup(dest)
        os.utime(path, (mtime, mtime))
        return path

    def final_paths(self):
        return {path for path in self.folder.glob('before-*.sqlite3') if path.is_file() and not path.is_symlink()}

    def status(self):
        return json.loads((self.root / 'inbox-sweep' / 'backup-status.json').read_text())

    def test_first_backup_publishes_verified_pre_import_snapshot(self):
        result = Path(sweep_module.backup_before_import(self.root))
        self.assertEqual(result.parent, self.folder)
        self.assertTrue(result.is_file())
        self.assertFalse(list(self.folder.glob('*.part')))
        with closing(sqlite3.connect(result)) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            self.assertEqual(db.execute('SELECT text FROM memories').fetchone()[0], 'SYNTHETIC original')
        self.assertEqual(self.status()['state'], 'completed')
        self.assertEqual(self.status()['stage'], 'published')

    def test_reuse_until_hour_boundary_without_opening_archive_or_rescanning_backup(self):
        existing = self.seed('recent', self.now - 3599.5)
        with mock.patch.object(sweep_module.time, 'time', return_value=self.now), \
                mock.patch.object(sweep_module.sqlite3, 'connect', side_effect=AssertionError('reuse must not open SQLite')):
            result = sweep_module.backup_before_import(self.root)
        self.assertEqual(Path(result), existing)
        self.assertEqual(self.final_paths(), {existing})

    def test_exact_hour_boundary_creates_new_snapshot(self):
        old = self.seed('hour-old', self.now - 3600)
        with mock.patch.object(sweep_module.time, 'time', return_value=self.now):
            new = Path(sweep_module.backup_before_import(self.root))
        self.assertNotEqual(new, old)
        self.assertTrue(new.is_file())

    def test_recent_candidate_is_selected_by_mtime_not_filename(self):
        self.seed('zzzz-older', self.now - 300)
        newest = self.seed('aaaa-newer', self.now - 100)
        with mock.patch.object(sweep_module.time, 'time', return_value=self.now):
            self.assertEqual(Path(sweep_module.backup_before_import(self.root)), newest)

    def test_future_timestamps_cannot_force_repeated_copies_or_prune_current_snapshot(self):
        for index in range(4):
            self.seed(f'future-{index}', self.now + 3600 + index)
        with mock.patch.object(sweep_module.time, 'time', return_value=self.now):
            current = Path(sweep_module.backup_before_import(self.root))
        self.assertTrue(current.is_file(), 'Retention removed the newly verified snapshot')
        self.assertEqual(len(self.final_paths()), 4)
        with mock.patch.object(sweep_module.time, 'time', return_value=self.now + 10), \
                mock.patch.object(sweep_module.sqlite3, 'connect', side_effect=AssertionError('fresh current snapshot must be reused')):
            reused = Path(sweep_module.backup_before_import(self.root))
        self.assertEqual(reused, current)

    def test_reuse_prunes_excess_finals_to_four_and_keeps_unrelated_files(self):
        backups = [self.seed(f'{index:02d}', self.now - 600 + index) for index in range(7)]
        unrelated = self.folder / 'manual.sqlite3'
        unrelated.write_bytes(b'SYNTHETIC manual backup marker')
        with mock.patch.object(sweep_module.time, 'time', return_value=self.now):
            reused = Path(sweep_module.backup_before_import(self.root))
        self.assertEqual(reused, backups[-1])
        self.assertEqual(self.final_paths(), set(backups[-4:]))
        self.assertEqual(unrelated.read_bytes(), b'SYNTHETIC manual backup marker')

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing semantics')
    def test_locked_old_final_does_not_block_valid_snapshot_and_retries_retention(self):
        backups = [self.seed(f'locked-policy-{index}', self.now - 600 + index) for index in range(5)]
        oldest, newest = backups[0], backups[-1]
        with sweep_module.read_guard(oldest), mock.patch.object(sweep_module.time, 'time', return_value=self.now):
            reused = Path(sweep_module.backup_before_import(self.root))
        self.assertEqual(reused, newest)
        self.assertTrue(reused.is_file())
        self.assertTrue(oldest.is_file())
        report = json.loads((self.root / 'inbox-sweep' / 'backup-retention.json').read_text())
        self.assertEqual(report['errors'][0]['file'], oldest.name)
        with mock.patch.object(sweep_module.time, 'time', return_value=self.now):
            self.assertEqual(Path(sweep_module.backup_before_import(self.root)), newest)
        self.assertFalse(oldest.exists())
        self.assertEqual(self.final_paths(), set(backups[1:]))

    def test_successful_new_copy_prunes_only_after_publication(self):
        old = [self.seed(f'old-{index}', self.now - 7200 + index) for index in range(6)]
        with mock.patch.object(sweep_module.time, 'time', return_value=self.now):
            current = Path(sweep_module.backup_before_import(self.root))
        self.assertEqual(self.final_paths(), set(old[-3:] + [current]))

    def test_failed_copy_removes_partial_but_preserves_all_existing_finals(self):
        existing = [self.seed(f'old-{index}', self.now - 7200) for index in range(5)]
        before = {path: path.read_bytes() for path in existing}
        real_connect = sqlite3.connect

        class BrokenSource(sqlite3.Connection):
            def backup(self, target, *args, **kwargs):
                raise OSError('SYNTHETIC interrupted copy')

        def connect(path, *args, **kwargs):
            if str(path).endswith('?mode=ro'):
                kwargs['factory'] = BrokenSource
            return real_connect(path, *args, **kwargs)

        with mock.patch.object(sweep_module.sqlite3, 'connect', side_effect=connect), \
                self.assertRaisesRegex(OSError, 'interrupted copy'):
            sweep_module.backup_before_import(self.root)
        self.assertEqual(self.final_paths(), set(existing))
        self.assertTrue(all(path.read_bytes() == raw for path, raw in before.items()))
        self.assertFalse(list(self.folder.glob('*.part')))
        self.assertEqual(self.status()['state'], 'failed')
        self.assertEqual(self.status()['stage'], 'copy')

    def test_failed_integrity_check_does_not_publish_or_prune(self):
        existing = [self.seed(f'old-{index}', self.now - 7200) for index in range(5)]
        real_connect = sqlite3.connect

        class BadIntegrity(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql.strip().lower() == 'pragma integrity_check':
                    return super().execute("SELECT 'ok' UNION ALL SELECT 'SYNTHETIC corrupt page'")
                return super().execute(sql, *args, **kwargs)

        def connect(path, *args, **kwargs):
            if str(path).endswith('.part'):
                kwargs['factory'] = BadIntegrity
            return real_connect(path, *args, **kwargs)

        with mock.patch.object(sweep_module.sqlite3, 'connect', side_effect=connect), \
                self.assertRaisesRegex(RuntimeError, 'backup_integrity_failed'):
            sweep_module.backup_before_import(self.root)
        self.assertEqual(self.final_paths(), set(existing))
        self.assertFalse(list(self.folder.glob('*.part')))
        self.assertEqual(self.status()['state'], 'failed')
        self.assertEqual(self.status()['stage'], 'integrity_check')

    def test_cleanup_only_removes_owned_backup_parts_and_accounts_bytes(self):
        parts = [self.folder / f'before-20000101T000000{n:06d}Z.sqlite3.part' for n in (1,2)]
        parts[0].write_bytes(b'12345')
        parts[1].write_bytes(b'123')
        os.utime(parts[0], (self.now - 86400, self.now - 86400))
        untouched = [self.folder / 'manual.sqlite3.part', self.folder / 'before-note.txt.part',
                     self.folder / 'before-manual.sqlite3.part',
                     self.root / 'inbox' / 'before-upload.sqlite3.part']
        for path in untouched:
            path.write_bytes(b'SYNTHETIC retained')
        directory = self.folder / 'before-directory.sqlite3.part'
        directory.mkdir()
        result = sweep_module.cleanup_unfinished_backups(self.root)
        self.assertEqual(set(result['removed_files']), {path.name for path in parts})
        self.assertEqual(result['removed_bytes'], 8)
        self.assertEqual(result['errors'], [])
        self.assertTrue(all(path.is_file() for path in untouched))
        self.assertTrue(directory.is_dir())
        self.assertFalse(any(path.exists() for path in parts))

    def test_backup_reuse_also_cleans_orphan_parts(self):
        recent = self.seed('recent', self.now - 10)
        orphan = self.folder / 'before-20000101T000000000003Z.sqlite3.part'
        orphan.write_bytes(b'SYNTHETIC orphan')
        with mock.patch.object(sweep_module.time, 'time', return_value=self.now):
            reused = Path(sweep_module.backup_before_import(self.root))
        self.assertEqual(reused, recent)
        self.assertFalse(orphan.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing semantics')
    def test_locked_part_is_reported_and_preserved_for_next_pass(self):
        path = self.folder / 'before-20000101T000000000004Z.sqlite3.part'
        path.write_bytes(b'SYNTHETIC held file')
        with sweep_module.read_guard(path):
            result = sweep_module.cleanup_unfinished_backups(self.root)
        self.assertTrue(path.exists())
        self.assertEqual(result['removed_bytes'], 0)
        self.assertEqual(result['errors'][0]['file'], path.name)
        sweep_module.cleanup_unfinished_backups(self.root)
        self.assertFalse(path.exists())

    def test_empty_sweep_cleans_orphans_without_making_backup(self):
        migrate(self.db_path)
        orphan = self.folder / 'before-20000101T000000000003Z.sqlite3.part'
        orphan.write_bytes(b'SYNTHETIC orphan')
        result = sweep_module.sweep(self.root, settle_seconds=0)
        self.assertEqual(result['imported_files'], 0)
        self.assertIsNone(result['backup'])
        self.assertFalse(orphan.exists())
        self.assertEqual(self.final_paths(), set())

    def test_distinct_imports_in_two_sweeps_share_hourly_backup(self):
        migrate(self.db_path)
        for index in range(2):
            packet = {'format': 'threadsatchel/1', 'kind': 'note',
                      'messages': [{'text': f'SYNTHETIC separate import {index}'}]}
            (self.root / 'inbox' / f'{index}.json').write_text(json.dumps(packet), encoding='utf-8')
            result = sweep_module.sweep(self.root, settle_seconds=0)
            self.assertEqual(result['imported_files'], 1)
            if index == 0:
                first = result['backup']
            else:
                self.assertEqual(result['backup'], first)
        self.assertEqual(len(self.final_paths()), 1)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM memories').fetchone()[0], 3)

    def test_directory_link_cannot_redirect_cleanup_outside_archive(self):
        # Linking needs developer mode or privileges on some Windows systems.
        outside = self.root / 'outside'
        outside.mkdir()
        victim = outside / 'before-20000101T000000000005Z.sqlite3.part'
        victim.write_bytes(b'SYNTHETIC outside bytes')
        self.folder.rmdir()
        try:
            self.folder.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.folder.mkdir()
            self.skipTest('Directory symlinks unavailable: ' + type(error).__name__)
        try:
            try:
                sweep_module.cleanup_unfinished_backups(self.root)
            except (ValueError, OSError):
                pass  # Refusing the linked directory is an acceptable guard.
            self.assertEqual(victim.read_bytes(), b'SYNTHETIC outside bytes')
        finally:
            self.folder.unlink()

    def test_linked_child_directory_is_rejected_before_any_cleanup(self):
        # Exercise the junction guard even on hosts without symlink privileges.
        victim = self.folder / 'before-20000101T000000000006Z.sqlite3.part'
        victim.write_bytes(b'SYNTHETIC protected')
        original = Path.is_junction

        def is_junction(path):
            return path == self.folder or original(path)

        with mock.patch.object(Path, 'is_junction', is_junction):
            with self.assertRaisesRegex(ValueError, 'must_not_be_a_link'):
                sweep_module.cleanup_unfinished_backups(self.root)
        self.assertEqual(victim.read_bytes(), b'SYNTHETIC protected')


if __name__ == '__main__':
    unittest.main()
