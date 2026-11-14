"""Verify local daily backup, retained receipts and recoverable SQLite contents."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

import scheduled_memory_backup as backup


class DailyBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'fixture'
        self.dest = Path(self.temp.name)/'snapshots'
        self.root.mkdir(); self.dest.mkdir()
        with closing(sqlite3.connect(self.root/'memory.sqlite3')) as db, db:
            db.execute('CREATE TABLE memories(text TEXT)')
            db.execute("INSERT INTO memories VALUES('synthetic')")
        (self.root/'inbox').mkdir(); (self.root/'inbox'/'note.txt').write_text('fixture')
        (self.root/'models').mkdir(); (self.root/'models'/'local.bin').write_bytes(b'fixture')

    def test_verified_zip_reuse_and_restore(self):
        journal = self.root/'inbox-sweep'/'recovery.sqlite3'
        journal.parent.mkdir()
        with closing(sqlite3.connect(journal)) as db, db:
            db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
        receipt = backup.run(self.root, self.dest)
        archive = self.dest/receipt['archive']
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(),receipt['archive_sha256'])
        with zipfile.ZipFile(archive) as z:
            self.assertEqual(set(z.namelist()), {'BACKUP_MANIFEST.json','memory.sqlite3','inbox/note.txt','inbox-sweep/recovery.sqlite3'})
            restore = z.extract('memory.sqlite3', Path(self.temp.name)/'restore')
        with closing(sqlite3.connect(restore)) as db:
            self.assertEqual(db.execute('SELECT text FROM memories').fetchone()[0], 'synthetic')
        self.assertEqual(backup.run(self.root,self.dest)['status'],'already_backed_up_today')
        with archive.open('r+b') as file:
            file.seek(1); file.write(b'?')
        with self.assertRaisesRegex(RuntimeError,'checksum'):
            backup.run(self.root,self.dest)

    def test_prune_after_publish_only_this_archives_own_verified_receipts(self):
        first = backup.run(self.root,self.dest)
        receipt = self.dest/(first['archive']+'.json')
        data = json.loads(receipt.read_text()); data['local_date'] = '2000-01-01'
        receipt.write_text(json.dumps(data))
        unrelated = self.dest/'manual.zip'; unrelated.write_bytes(b'fixture')
        second = backup.run(self.root,self.dest,keep=1)
        self.assertEqual(second['pruned'],[first['archive']]); self.assertTrue(unrelated.is_file())
        self.assertFalse(receipt.exists()); self.assertTrue((self.dest/second['archive']).is_file())

    def test_reject_destination_overlap_and_zero_retention(self):
        inside = self.root/'backups'; inside.mkdir()
        for destination in (inside,self.root, self.root.parent):
            with self.assertRaises(ValueError):
                backup.run(self.root,destination)
        with self.assertRaises(ValueError):
            backup.run(self.root,self.dest,keep=0)

    def test_cleanup_abandoned_owned_temps_under_lock(self):
        orphan = self.dest/'memory-20000101T120000123456Z-00000000.zip.partial'
        other = self.dest/'memory-manual.zip.partial'
        orphan.write_bytes(b'fixture'); other.write_bytes(b'fixture')
        backup.run(self.root,self.dest)
        self.assertFalse(orphan.exists()); self.assertTrue(other.exists())
