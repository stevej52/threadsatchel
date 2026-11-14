"""One bounded inbox pass; storage and reconciliation belong to import_memory."""
import argparse
from bisect import bisect_right
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from codex_capture import capture_lock, dump_atomic, SECRET
from import_memory import import_file, is_temporary_file, parse, describe_import_error
from inbox_recovery import RecoveryJournal

ROOT = Path(__file__).resolve().parent
FORMATS = {'.json', '.txt', '.md', '.markdown'}
MAX_BYTES = 8 * 1024 * 1024
MAX_IMPORTS = 50
BACKUPS_TO_KEEP = 4
BACKUP_INTERVAL_SECONDS = 60 * 60


def timestamp():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def read_guard(path):
    """Windows refuses concurrent writers/deleters; POSIX locks are advisory."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
            wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
            wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
    else:
        import fcntl
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('not_regular_file')
        yield stream, info


def has_receipt(db_path, sha, path, connection=None):
    if connection is not None:
        return connection.execute('SELECT 1 FROM import_receipts WHERE object_sha=? AND path=?',
                                  (sha, str(path))).fetchone() is not None
    with closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
        db.execute('PRAGMA query_only=ON')
        return has_receipt(db_path, sha, path, db)


def contains_possible_credential(value):
    """Defense in depth only: inspect decoded packet strings, never log matches."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if SECRET.search(item):
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return False


def _owned_backup_files(folder, pattern):
    """Never follow a file link or remove anything outside this backup directory."""
    # The configured backups parent may be the intentional E: -> F: junction.
    # The sweep-owned child directory must itself be a regular directory.
    if folder.is_symlink() or folder.is_junction() or (folder.exists() and
            getattr(folder.lstat(), 'st_file_attributes', 0) & 0x400):
        raise ValueError('inbox_backup_directory_must_not_be_a_link')
    resolved = folder.resolve()
    if resolved.parent != folder.parent.resolve():
        raise ValueError('inbox_backup_directory_escaped_parent')
    return [path for path in folder.glob(pattern)
            if re.fullmatch(r'before-[0-9]{8}T[0-9]{12}Z\.sqlite3(?:\.part)?', path.name)
            and path.is_file() and not path.is_symlink() and not path.is_junction()
            and not getattr(path.lstat(), 'st_file_attributes', 0) & 0x400
            and path.resolve().parent == resolved]


def cleanup_unfinished_backups(root):
    """Caller owns the sweep lock: no legitimate sweep backup can still be open."""
    root = Path(root)
    folder = root / 'backups' / 'inbox-sweep'
    result = {'removed_files': [], 'removed_bytes': 0, 'errors': []}
    if not folder.exists():
        return result
    for path in _owned_backup_files(folder, 'before-*.sqlite3.part'):
        try:
            size = path.stat().st_size
            path.unlink()
            result['removed_files'].append(path.name)
            result['removed_bytes'] += size
        except OSError as error:
            result['errors'].append({'file': path.name, 'error_type': type(error).__name__})
    if result['removed_files'] or result['errors']:
        previous_path = root / 'inbox-sweep' / 'backup-status.json'
        if previous_path.is_file():
            try:
                previous = json.loads(previous_path.read_text(encoding='utf-8'))
                if previous.get('state') == 'running':
                    result['interrupted_backup'] = previous
            except (OSError, ValueError):
                pass
        dump_atomic(root / 'inbox-sweep' / 'backup-cleanup.json', dict(result, at=timestamp()))
    return result


def _prune_backups(folder, protected=None):
    backups = sorted(_owned_backup_files(folder, 'before-*.sqlite3'),
                     key=lambda path: (path.stat().st_mtime_ns, path.name))
    # A future-dated sibling must never displace the snapshot being returned.
    others = [path for path in backups if path != protected]
    keep = BACKUPS_TO_KEEP - int(protected in backups)
    result = {'removed_files': [], 'removed_bytes': 0, 'errors': []}
    for old in others[:-keep] if keep else others:
        try:
            size = old.stat().st_size
            old.unlink()
            result['removed_files'].append(old.name)
            result['removed_bytes'] += size
        except OSError as error:
            result['errors'].append({'file': old.name, 'error_type': type(error).__name__})
    return result


def prune_backups(root, protected=None):
    root = Path(root)
    result = _prune_backups(root / 'backups' / 'inbox-sweep', protected)
    if result['removed_files'] or result['errors']:
        dump_atomic(root / 'inbox-sweep' / 'backup-retention.json', dict(result, at=timestamp()))
    return result


def backup_before_import(root):
    """Reuse a verified hourly snapshot. Caller must hold the sweep's capture lock."""
    root = Path(root)
    folder = root / 'backups' / 'inbox-sweep'
    folder.mkdir(parents=True, exist_ok=True)
    cleanup_unfinished_backups(root)
    now = time.time()
    # Ignore future timestamps when choosing reuse, so clock skew cannot force
    # another full copy on every subsequent pass after a fresh valid snapshot.
    completed = [path for path in _owned_backup_files(folder, 'before-*.sqlite3')
                 if path.stat().st_mtime <= now]
    if completed:
        newest = max(completed, key=lambda path: (path.stat().st_mtime_ns, path.name))
        age = now - newest.stat().st_mtime
        if 0 <= age < BACKUP_INTERVAL_SECONDS:
            prune_backups(root, protected=newest)
            return str(newest)
    name = 'before-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    temporary = folder / (name + '.sqlite3.part')
    final = folder / (name + '.sqlite3')
    started = time.monotonic()
    diagnostic = {'state': 'running', 'started_at': timestamp(), 'file': temporary.name,
                  'pid': os.getpid(), 'stage': 'copy'}
    status_path = root / 'inbox-sweep' / 'backup-status.json'
    dump_atomic(status_path, diagnostic)
    try:
        with closing(sqlite3.connect((root / 'memory.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=30)) as src:
            with closing(sqlite3.connect(temporary)) as dest:
                src.backup(dest)
                diagnostic['stage'] = 'integrity_check'
                dump_atomic(status_path, diagnostic)
                if dest.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise RuntimeError('backup_integrity_failed')
        temporary.rename(final)
        diagnostic.update(state='completed', stage='published', file=final.name,
                          bytes=final.stat().st_size)
    except BaseException as error:
        diagnostic.update(state='failed', error_type=type(error).__name__,
                          sqlite_error=getattr(error, 'sqlite_errorname', None))
        raise
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError as error:
                diagnostic['cleanup_error'] = type(error).__name__
        diagnostic.update(finished_at=timestamp(), duration_seconds=round(time.monotonic() - started, 3))
        dump_atomic(status_path, diagnostic)
    # Only published, integrity-checked snapshots qualify for retention/reuse.
    prune_backups(root, protected=final)
    return str(final)


def sweep(root=ROOT, *, settle_seconds=30, retry_failed=False):
    root = Path(root).resolve()
    inbox, db_path, runtime = root / 'inbox', root / 'memory.sqlite3', root / 'inbox-sweep'
    if not db_path.is_file() or not inbox.is_dir() or inbox.is_symlink() or inbox.is_junction():
        raise ValueError('Expected the existing archive and a regular inbox directory')
    runtime.mkdir(exist_ok=True)
    with capture_lock(runtime):
        cleanup_unfinished_backups(root)
        return _sweep(root, inbox, db_path, runtime, settle_seconds, retry_failed)


def _sweep(root, inbox, db_path, runtime, settle_seconds, retry_failed):
    state_path = runtime / 'state.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'files': {}}
    # Recovery state and pass membership live outside memory.sqlite3. Reconcile
    # against actual receipts before even considering the normal scan cursor.
    with RecoveryJournal(root) as journal:
        recovery = journal.reconcile(db_path, state)
        if recovery['active']:
            report = {'started_at': timestamp(), 'finished_at': timestamp(),
                      'state': 'recovery_required', 'recovery': recovery,
                      'imported_files': 0, 'files': [], 'backup': None}
            dump_atomic(state_path, state)
            dump_atomic(runtime / 'last-run.json', report)
            return report
        pass_id = journal.begin_pass()
        try:
            report = _sweep_pass(root, inbox, db_path, runtime, settle_seconds,
                                 retry_failed, state, journal, pass_id)
        except BaseException as error:
            journal.fail_pass(pass_id, type(error).__name__, stage='pass', error=error)
            journal.mirror_holds(state)
            dump_atomic(state_path, state)
            raise
        if report['state'] != 'recovery_required':
            journal.finish_pass(pass_id)
        return report


def _sweep_pass(root, inbox, db_path, runtime, settle_seconds, retry_failed, state, journal, pass_id):
    state_path = runtime / 'state.json'
    entries = state['files']
    report = {'started_at': timestamp(), 'pass_id': pass_id, 'state': 'running', 'files': [], 'backup': None,
              'imported_files': 0, 'already_present': 0, 'added_revisions': 0,
              'held_files': 0, 'deferred_files': 0, 'ignored_files': 0}
    dump_atomic(runtime / 'last-run.json', report)
    handler = RotatingFileHandler(runtime / 'events.log', maxBytes=262144, backupCount=3, encoding='utf-8')
    logger = logging.getLogger('inbox-sweep-' + str(os.getpid()))
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    started = time.monotonic()
    receipt_db = None
    try:
        receipt_db = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=10)
        receipt_db.execute('PRAGMA query_only=ON')
        paths = sorted(inbox.iterdir(), key=lambda path: path.name)
        # Resume strictly after the last examined name, wrapping once. A removed
        # cursor file is harmless; newly inserted earlier names get the next turn.
        offset = bisect_right([path.name for path in paths], state.get('cursor', ''))
        for path in paths[offset:] + paths[:offset]:
            if (runtime / 'disabled.flag').exists():
                report['state'] = 'disabled'
                break
            if time.monotonic() - started > 60 or report['imported_files'] >= MAX_IMPORTS:
                report['state'] = 'more_pending'
                break
            state['cursor'] = path.name
            item = {'file': path.name, 'checked_at': timestamp()}
            phase = 'validate'
            import_intended = False
            try:
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                        or path.is_symlink() or is_temporary_file(path) or path.suffix.lower() not in FORMATS):
                    report['ignored_files'] += 1
                    continue
                with read_guard(path) as (stream, info):
                    if time.time() - info.st_mtime < settle_seconds:
                        report['deferred_files'] += 1
                        continue
                    if info.st_size > MAX_BYTES:
                        item.update(status='held', error='file_exceeds_8_mib')
                    else:
                        raw = stream.read(MAX_BYTES + 1)
                        if len(raw) > MAX_BYTES:
                            raise ValueError('file_exceeds_8_mib')
                        sha = hashlib.sha256(raw).hexdigest()
                        item['sha256'] = sha
                        previous = entries.get(path.name, {})
                        if has_receipt(db_path, sha, path, receipt_db):
                            item.update(status='already_present')
                            report['already_present'] += 1
                        elif (previous.get('sha256') == sha and previous.get('status') in ('held', 'importing')
                              and not previous.get('retryable', False) and not retry_failed):
                            item.update(status='held', error=previous.get('error', 'interrupted_import_check_before_retry'))
                            if previous.get('error_detail'):
                                item['error_detail'] = previous['error_detail']
                        else:
                            packets, _ = parse(path, raw, {})  # validate before any DB writes
                            if SECRET.search(raw.decode('utf-8-sig')) or contains_possible_credential(packets):
                                item.update(status='held', error='possible_credential_review_required')
                            else:
                                if report['backup'] is None:
                                    phase = 'backup'
                                    report['backup'] = backup_before_import(root)
                                phase = 'import'
                                journal.before_import(pass_id, path, sha, mtime_ns=info.st_mtime_ns,
                                                      backup=report['backup'])
                                import_intended = True
                                entries[path.name] = dict(item, status='importing')
                                dump_atomic(state_path, state)
                                result = import_file(path, db_path, max_file_bytes=MAX_BYTES, expected_sha256=sha)
                                if result['sha256'] != sha or not has_receipt(db_path, sha, path, receipt_db):
                                    raise RuntimeError('commit_verification_failed')
                                journal.verified_import(pass_id, path, sha, db_path, receipt_db=receipt_db)
                                item.update(status='imported', **{key: result[key] for key in
                                    ('added_revisions', 'reused_messages', 'linked_entities', 'repeated_packets')})
                                item.update(uncertain_matches=len(result['uncertain']), warnings=len(result['warnings']))
                                item['import_status'] = result.get('import_status', 'imported')
                                report['imported_files'] += 1
                                report['added_revisions'] += result['added_revisions']
            except OSError as error:
                if getattr(error, 'winerror', None) in (32, 33) or isinstance(error, BlockingIOError):
                    if not import_intended and phase != 'backup':
                        report['deferred_files'] += 1
                        continue
                item.update(status='held', error='file_access_error', error_type=type(error).__name__, retryable=True)
            except json.JSONDecodeError as error:
                item.update(status='held', error='invalid_json', line=error.lineno, column=error.colno)
            except UnicodeDecodeError:
                item.update(status='held', error='invalid_utf8')
            except sqlite3.OperationalError as error:
                item.update(status='held', error='database_operation_failed', retryable=
                    getattr(error, 'sqlite_errorcode', None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))
            except ValueError as error:
                item.update(status='held', error='invalid_import_packet', error_detail=describe_import_error(error))
            except Exception as error:
                item.update(status='held', error='import_failed', error_type=type(error).__name__)
            if item['status'] == 'held':
                report['held_files'] += 1
            previous = entries.get(path.name, {})
            if item['status'] == 'imported' or any(previous.get(k) != item.get(k) for k in ('status', 'error', 'sha256')):
                logger.info(json.dumps(item, ensure_ascii=True))
            entries[path.name] = item
            report['files'].append(item)
            if item['status'] == 'held' and import_intended:
                journal.fail_pass(pass_id, item.get('error', 'import_failed'),
                                  stage=phase, path=path, sha=item.get('sha256'))
                journal.mirror_holds(state)
                report['state'] = 'recovery_required'
                report['failed_file'] = path.name
                break
            if item['status'] == 'held' and phase == 'backup':
                report['state'] = 'attention_required'
                report['failed_file'] = path.name
                break
        # Importing checkpoints above remain durable before each DB write. Other
        # receipts/check times and the fair continuation cursor need only one
        # atomic write per completed/budget-limited pass.
        dump_atomic(state_path, state)
        if report['state'] == 'running':
            report['state'] = 'attention_required' if report['held_files'] else 'ok'
        report['finished_at'] = timestamp()
        dump_atomic(runtime / 'last-run.json', report)
        logger.info(json.dumps({k: v for k, v in report.items() if k != 'files'}))
        return report
    finally:
        if receipt_db is not None:
            receipt_db.close()
        logger.removeHandler(handler)
        handler.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--retry-failed', action='store_true', help='Retry unchanged held files after reviewing the error')
    args = parser.parse_args()
    try:
        result = sweep(retry_failed=args.retry_failed)
    except OSError as error:
        if isinstance(error, BlockingIOError) or getattr(error, 'winerror', None) in (32, 33, 36):
            return 0  # another sweep owns the lock
        result = {'state': 'failed', 'at': timestamp(), 'error_type': type(error).__name__}
    except Exception as error:
        result = {'state': 'failed', 'at': timestamp(), 'error_type': type(error).__name__}
    if result['state'] == 'failed':
        dump_atomic(ROOT / 'inbox-sweep' / 'last-run.json', result)
    print(json.dumps(result, ensure_ascii=True))
    return 1 if result['state'] in ('failed', 'attention_required', 'recovery_required') else 0


if __name__ == '__main__':
    raise SystemExit(main())
