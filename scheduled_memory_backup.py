"""Verified daily ThreadSatchel backups to an operator-selected directory."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import time
import uuid
import zipfile

from codex_capture import capture_lock, dump_atomic

ROOT = Path(__file__).resolve().parent
KEEP = 14
FORMAT = 'threadsatchel-scheduled-backup/1'
NAME = re.compile(r'^memory-\d{8}T\d{12}Z-[a-f0-9]{8}\.zip$')
DATA_DIRS = ('handoffs', 'inbox', 'docs')
ROOT_CONFIGS = {'capture-local.json', 'capture-export.json', 'remote-capture-config.json',
                'memory-ai.json', 'requirements.txt', 'requirements-ai.txt'}


def linked(path):
    return path.is_symlink() or path.is_junction()


def validate_roots(source, destination):
    for root in (source, destination):
        if not root.is_dir() or any(linked(p) for p in (root, *root.parents)):
            raise ValueError('An existing directory without linked components is required')
    if source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ValueError('Choose a backup destination outside the installation, in a separate directory')
    if linked(source/'memory.sqlite3') or not (source/'memory.sqlite3').is_file():
        raise ValueError('Expected an initialized archive with a regular database file')


def checked_child(directory, name):
    path = directory / name
    if Path(name).name != name or linked(path) or path.resolve().parent != directory:
        raise ValueError('Unsafe backup file path')
    return path


def sha256_file(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def input_files(source):
    for path in sorted(source.iterdir()):
        if path.is_file() and (path.suffix in {'.py', '.ps1', '.md'} or path.name in ROOT_CONFIGS):
            yield path
    for name in DATA_DIRS:
        folder = source / name
        if linked(folder):
            raise ValueError('Linked input directory')
        if not folder.exists():
            continue
        for base, dirs, names in os.walk(folder, followlinks=False):
            if any(linked(Path(base)/n) for n in dirs):
                raise ValueError('Linked input directory')
            for name in sorted(names):
                path = Path(base)/name
                if path.suffix not in {'.tmp', '.part'}:
                    yield path


def add_file(archive, path, name):
    digest = hashlib.sha256()
    total = 0
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or linked(path):
        raise RuntimeError('Backup input is not a regular file')
    with path.open('rb') as src, archive.open(name, 'w', force_zip64=True) as out:
        if not os.path.samestat(before, os.fstat(src.fileno())):
            raise RuntimeError('Backup input changed before opening')
        while chunk := src.read(1024 * 1024):
            out.write(chunk)
            digest.update(chunk)
            total += len(chunk)
    after = path.stat()
    if total != before.st_size or after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
        raise RuntimeError('A backup input changed while being copied; retry on the next run')
    return {'path': name, 'bytes': total, 'sha256': digest.hexdigest()}


def verified_receipts(destination, source_id):
    found = []
    for receipt in destination.glob('memory-*.zip.json'):
        if linked(receipt) or not receipt.is_file() or receipt.stat().st_size > 65536:
            continue
        name = receipt.name[:-5]
        if not NAME.fullmatch(name):
            continue
        try:
            info = json.loads(receipt.read_text(encoding='utf-8'))
            if not isinstance(info, dict) or info.get('format') != FORMAT or info.get('source_id') != source_id:
                continue
            target = checked_child(destination, name)
            if (info.get('archive') == name and info.get('verified') is True and target.is_file()
                    and target.stat().st_size == info.get('archive_bytes')
                    and re.fullmatch('[0-9a-f]{64}', str(info.get('archive_sha256','')))):
                found.append((name, info, target, receipt))
        except (OSError, ValueError):
            continue
    return sorted(found)


def snapshot(source_db, target, deadline, *, count_records=False):
    def progress(_status, _remaining, _total):
        if time.monotonic() >= deadline:
            raise TimeoutError('SQLite snapshot exceeded the time limit')

    if linked(source_db) or not source_db.is_file():
        raise ValueError('Expected a regular SQLite input')
    with closing(sqlite3.connect(source_db.as_uri()+'?mode=ro', uri=True, timeout=30)) as source:
        with closing(sqlite3.connect(target)) as dest:
            source.backup(dest, pages=4096, progress=progress, sleep=.1)
            if dest.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise RuntimeError('Backup database integrity check failed')
            if count_records:
                return dest.execute('SELECT count(*) FROM memories').fetchone()[0]


def prune(destination, source_id, keep):
    removed = []
    for name, receipt, archive, receipt_path in verified_receipts(destination, source_id)[:-keep]:
        if sha256_file(archive) != receipt['archive_sha256']:
            raise RuntimeError('An old backup changed; retention stopped')
        checked_child(destination, name).unlink()
        checked_child(destination, receipt_path.name).unlink()
        removed.append(name)
    return removed


def cleanup_interrupted(destination):
    # A killed process cannot run its finally block. All jobs targeting this
    # folder share this lock, including jobs for a different installation.
    for suffix in ('.partial', '.sqlite3.tmp', '.journal.tmp'):
        for path in destination.glob('memory-*.zip'+suffix):
            if NAME.fullmatch(path.name[:-len(suffix)]) and path.is_file() and not linked(path):
                checked_child(destination, path.name).unlink()


def run(source, destination, keep=KEEP):
    if type(keep) is not int or keep < 1:
        raise ValueError('Retention must be at least one verified backup')
    source = Path(source).expanduser().absolute()
    destination = Path(destination).expanduser().absolute()
    validate_roots(source, destination)
    source, destination = source.resolve(), destination.resolve()
    source_id = hashlib.sha256(os.fsencode(os.path.normcase(str(source)))).hexdigest()
    checked_child(destination, 'capture.lock')
    with capture_lock(destination):
        cleanup_interrupted(destination)
        today = datetime.now().date().isoformat()
        for _, receipt, archive, _ in reversed(verified_receipts(destination, source_id)):
            if receipt['local_date'] == today:
                if sha256_file(archive) != receipt['archive_sha256']:
                    raise RuntimeError('Existing daily backup checksum failed')
                # Retry an interrupted retention pass even if the ZIP was published.
                return dict(receipt, status='already_backed_up_today', pruned=prune(destination, source_id, keep))
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        name = f'memory-{stamp}-{uuid.uuid4().hex[:8]}.zip'
        final = checked_child(destination, name)
        partial = checked_child(destination, name+'.partial')
        database = checked_child(destination, name+'.sqlite3.tmp')
        journal_tmp = checked_child(destination, name+'.journal.tmp')
        journal = source/'inbox-sweep'/'recovery.sqlite3'
        if linked(journal.parent) or linked(journal):
            raise ValueError('Linked recovery directory or database')
        files = list(input_files(source))
        estimate = (source/'memory.sqlite3').stat().st_size*3 + sum(p.stat().st_size for p in files)
        if shutil.disk_usage(destination).free < estimate:
            raise RuntimeError('Insufficient disk space for a new verified backup')
        started = time.monotonic()
        try:
            records = snapshot(source/'memory.sqlite3', database, started+1800, count_records=True)
            inputs = [(database, 'memory.sqlite3')]
            if journal.exists():
                snapshot(journal, journal_tmp, started+1800)
                inputs.append((journal_tmp, 'inbox-sweep/recovery.sqlite3'))
            inputs.extend((p,p.relative_to(source).as_posix()) for p in files)
            manifest = dict(format=FORMAT, created_at_utc=datetime.now(timezone.utc).isoformat(),
                            record_count=records, files=[])
            with zipfile.ZipFile(partial, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                for path, member in inputs:
                    manifest['files'].append(add_file(archive, path, member))
                archive.writestr('BACKUP_MANIFEST.json', json.dumps(manifest, indent=2))
            with zipfile.ZipFile(partial) as archive:
                if json.loads(archive.read('BACKUP_MANIFEST.json')) != manifest:
                    raise RuntimeError('Archive manifest mismatch')
                if set(archive.namelist()) != {i['path'] for i in manifest['files']} | {'BACKUP_MANIFEST.json'}:
                    raise RuntimeError('Archive inventory mismatch')
                for item in manifest['files']:
                    with archive.open(item['path']) as stream:
                        if hashlib.file_digest(stream,'sha256').hexdigest() != item['sha256']:
                            raise RuntimeError('Archived file checksum failed')
            receipt = dict(format=FORMAT, source_id=source_id, archive=name, archive_bytes=partial.stat().st_size,
                           archive_sha256=sha256_file(partial), verified=True, database_quick_check='ok',
                           record_count=records, files=len(inputs), local_date=today,
                           completed_at_utc=datetime.now(timezone.utc).isoformat())
            os.replace(partial, final)
            dump_atomic(checked_child(destination, name+'.json'), receipt)
            return dict(receipt, status='completed', pruned=prune(destination, source_id, keep))
        finally:
            for path in (partial, database, journal_tmp):
                checked_child(destination, path.name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT)
    parser.add_argument('--destination', required=True, type=Path, help='Existing external backup directory')
    parser.add_argument('--keep', type=int, default=KEEP)
    args = parser.parse_args()
    print(json.dumps(run(args.source,args.destination,args.keep), indent=2))


if __name__ == '__main__':
    main()
