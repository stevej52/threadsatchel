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
import sqlite3
import stat
import time

from codex_capture import capture_lock, dump_atomic, SECRET
from import_memory import import_file, is_temporary_file, parse

ROOT = Path(__file__).resolve().parent
FORMATS = {'.json', '.txt', '.md', '.markdown'}
MAX_BYTES = 8 * 1024 * 1024
MAX_IMPORTS = 50
BACKUPS_TO_KEEP = 8


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


def backup_before_import(root):
    folder = root / 'backups' / 'inbox-sweep'
    folder.mkdir(parents=True, exist_ok=True)
    name = 'before-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    temporary = folder / (name + '.sqlite3.part')
    final = folder / (name + '.sqlite3')
    with closing(sqlite3.connect((root / 'memory.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=30)) as src:
        with closing(sqlite3.connect(temporary)) as dest:
            src.backup(dest)
            if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('backup_integrity_failed')
    temporary.rename(final)
    # Retention applies only to this sweep's verified backups, never originals.
    for old in sorted(folder.glob('before-*.sqlite3'))[:-BACKUPS_TO_KEEP]:
        if old.is_file() and not old.is_symlink() and old.resolve().parent == folder.resolve():
            old.unlink()
    return str(final)


def sweep(root=ROOT, *, settle_seconds=30, retry_failed=False):
    root = Path(root).resolve()
    inbox, db_path, runtime = root / 'inbox', root / 'memory.sqlite3', root / 'inbox-sweep'
    if not db_path.is_file() or not inbox.is_dir() or inbox.is_symlink() or inbox.is_junction():
        raise ValueError('Expected the existing archive and a regular inbox directory')
    runtime.mkdir(exist_ok=True)
    with capture_lock(runtime):
        return _sweep(root, inbox, db_path, runtime, settle_seconds, retry_failed)


def _sweep(root, inbox, db_path, runtime, settle_seconds, retry_failed):
    state_path = runtime / 'state.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'files': {}}
    entries = state['files']
    report = {'started_at': timestamp(), 'state': 'running', 'files': [], 'backup': None,
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
                        else:
                            packets, _ = parse(path, raw, {})  # validate before any DB writes
                            if SECRET.search(raw.decode('utf-8-sig')) or contains_possible_credential(packets):
                                item.update(status='held', error='possible_credential_review_required')
                            else:
                                if report['backup'] is None:
                                    report['backup'] = backup_before_import(root)
                                entries[path.name] = dict(item, status='importing')
                                dump_atomic(state_path, state)
                                result = import_file(path, db_path, max_file_bytes=MAX_BYTES, expected_sha256=sha)
                                if result['sha256'] != sha or not has_receipt(db_path, sha, path, receipt_db):
                                    raise RuntimeError('commit_verification_failed')
                                item.update(status='imported', **{key: result[key] for key in
                                    ('added_revisions', 'reused_messages', 'linked_entities', 'repeated_packets')})
                                item.update(uncertain_matches=len(result['uncertain']), warnings=len(result['warnings']))
                                report['imported_files'] += 1
                                report['added_revisions'] += result['added_revisions']
            except OSError as error:
                if getattr(error, 'winerror', None) in (32, 33) or isinstance(error, BlockingIOError):
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
            except ValueError:
                item.update(status='held', error='invalid_import_packet')
            except Exception as error:
                item.update(status='held', error='import_failed', error_type=type(error).__name__)
            if item['status'] == 'held':
                report['held_files'] += 1
            previous = entries.get(path.name, {})
            if item['status'] == 'imported' or any(previous.get(k) != item.get(k) for k in ('status', 'error', 'sha256')):
                logger.info(json.dumps(item, ensure_ascii=True))
            entries[path.name] = item
            report['files'].append(item)
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
    return 1 if result['state'] in ('failed', 'attention_required') else 0


if __name__ == '__main__':
    raise SystemExit(main())
