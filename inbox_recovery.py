"""Durable sweep recovery journal, outside memory.sqlite3.

Normal sweep integration owns inbox-sweep/capture.lock while using this class.
The CLI and replay_next()/prepare_restore() acquire that same lock themselves.
Restoring the archive never restores this journal. No original text is stored here.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from codex_capture import capture_lock, dump_atomic

ROOT = Path(__file__).resolve().parent
SHA = re.compile(r'[a-f0-9]{64}\Z')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def _connection(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
    db.execute('PRAGMA query_only=ON')
    return db


def quick_check(path):
    """Fail closed; a receipt is never a substitute for the replay integrity check."""
    with closing(_connection(path)) as db:
        rows = db.execute('PRAGMA quick_check').fetchall()
        if rows != [('ok',)]:
            raise RuntimeError('archive_quick_check_failed')
    return True


def _receipt(db, path, sha, options_json):
    if options_json is None:
        return False  # Unknown historical import options must never be guessed.
    return db.execute('SELECT 1 FROM import_receipts WHERE object_sha=? AND path=? AND options_json=?',
                      (sha, str(path), options_json)).fetchone() is not None


def _options(value):
    options = json.loads(value) if isinstance(value, str) else None
    reserved = {'path', 'db_path', 'max_file_bytes', 'max_expanded_bytes', 'max_zip_members', 'expected_sha256'}
    if (not isinstance(options, dict) or any(not isinstance(key, str) for key in options)
            or reserved.intersection(options)):
        raise ValueError('historical_receipt_options_invalid')
    return options


def _state(root):
    path = Path(root) / 'inbox-sweep' / 'state.json'
    return json.loads(path.read_text('utf-8')) if path.exists() else {'files': {}}


class RecoveryJournal:
    """Methods require the caller's existing sweep lock; commits are FULL synchronous."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.inbox = self.root / 'inbox'
        if (not self.inbox.is_dir() or self.inbox.is_symlink()
                or getattr(self.inbox, 'is_junction', lambda: False)()):
            raise ValueError('recovery_inbox_must_be_regular_directory')
        runtime = self.root / 'inbox-sweep'
        if runtime.is_symlink() or getattr(runtime, 'is_junction', lambda: False)():
            raise ValueError('recovery_runtime_must_be_regular_directory')
        runtime.mkdir(exist_ok=True)
        path = runtime / 'recovery.sqlite3'
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError('recovery_journal_must_be_regular_file')
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS passes(
                id TEXT PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT,
                state TEXT NOT NULL,kind TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS files(
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT,path TEXT NOT NULL,sha256 TEXT NOT NULL,
                first_seen TEXT NOT NULL,mtime_ns INTEGER NOT NULL DEFAULT 0,
                receipt_seen INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL,
                reason TEXT,last_pass TEXT,checked_at TEXT,
                missing_latched INTEGER NOT NULL DEFAULT 0,
                receipt_options TEXT,options_ambiguous INTEGER NOT NULL DEFAULT 0,
                UNIQUE(path,sha256));
            CREATE TABLE IF NOT EXISTS pass_items(
                pass_id TEXT NOT NULL REFERENCES passes(id),file_id INTEGER NOT NULL REFERENCES files(ordinal),
                backup TEXT,PRIMARY KEY(pass_id,file_id));
            CREATE INDEX IF NOT EXISTS recovery_queue ON files(state,first_seen,ordinal);
        ''')
        if 'missing_latched' not in {r[1] for r in self.db.execute('PRAGMA table_info(files)')}:
            self.db.execute('ALTER TABLE files ADD COLUMN missing_latched INTEGER NOT NULL DEFAULT 0')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(files)')}
        if 'receipt_options' not in columns:
            self.db.execute('ALTER TABLE files ADD COLUMN receipt_options TEXT')
            # The earlier journal wrote import intents only for default-option
            # imports. Its receipt-only legacy baselines had no known options.
            self.db.execute("UPDATE files SET receipt_options='{}' WHERE EXISTS "
                            '(SELECT 1 FROM pass_items WHERE file_id=files.ordinal)')
        if 'options_ambiguous' not in columns:
            self.db.execute('ALTER TABLE files ADD COLUMN options_ambiguous INTEGER NOT NULL DEFAULT 0')
        self.db.commit()
        if [r[0] for r in self.db.execute('PRAGMA quick_check')] != ['ok']:
            self.db.close()
            raise RuntimeError('recovery_journal_quick_check_failed')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def _set(self, key, value):
        self.db.execute('INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (key, str(value)))

    def _activate(self, reason):
        self._set('active', '1')
        self._set('reason', reason)

    def _path(self, path):
        path = Path(path)
        if not path.is_absolute():
            path = self.inbox / path
        # Do not resolve the file itself: that could follow a malicious link.
        if path.parent.resolve() != self.inbox.resolve() or path.name in ('', '.', '..'):
            raise ValueError('recovery_file_must_be_direct_inbox_child')
        return self.inbox / path.name

    def _register(self, path, sha, when=None, mtime_ns=0, receipt_seen=False, state='known', receipt_options=None):
        path = self._path(path)
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise ValueError('invalid_recovery_sha256')
        self.db.execute('INSERT INTO files(path,sha256,first_seen,mtime_ns,receipt_seen,state) '
                        'VALUES(?,?,?,?,?,?) ON CONFLICT(path,sha256) DO UPDATE SET '
                        'first_seen=min(files.first_seen,excluded.first_seen),'
                        'receipt_seen=max(files.receipt_seen,excluded.receipt_seen) '
                        'WHERE excluded.first_seen<files.first_seen OR excluded.receipt_seen>files.receipt_seen',
                        (str(path), sha, when or stamp(), mtime_ns, int(receipt_seen), state))
        if receipt_options is not None:
            _options(receipt_options)
            # Bind once. A different receipt appearing later cannot replace a
            # missing historical variant and accidentally conceal a restore.
            self.db.execute('UPDATE files SET receipt_options=? WHERE path=? AND sha256=? AND receipt_options IS NULL',
                            (receipt_options, str(path), sha))
        return self.db.execute('SELECT * FROM files WHERE path=? AND sha256=?', (str(path), sha)).fetchone()

    def _hold_ids(self, ids, reason):
        ids = list(dict.fromkeys(ids))
        for item_id in ids:
            self.db.execute("UPDATE files SET state='held',reason=? WHERE ordinal=?", (reason, item_id))
        if ids:
            self._activate(reason)

    def _hold_related(self, ids, reason):
        ids = set(ids)
        # Every attempted file from an affected pass is a suspect, even if its
        # receipt survived the restore or the earlier import returned success.
        for item_id in list(ids):
            for row in self.db.execute('SELECT other.file_id FROM pass_items AS mine '
                    'JOIN pass_items AS other ON other.pass_id=mine.pass_id WHERE mine.file_id=?', (item_id,)):
                ids.add(row[0])
        self._hold_ids(ids, reason)

    def status(self, limit=20):
        active = self.db.execute("SELECT value FROM metadata WHERE key='active'").fetchone()
        reason = self.db.execute("SELECT value FROM metadata WHERE key='reason'").fetchone()
        held = self.db.execute("SELECT count(*) FROM files WHERE state IN ('held','importing')").fetchone()[0]
        items = [dict(file=Path(r['path']).name, sha256=r['sha256'], first_seen=r['first_seen'],
                      state=r['state'], reason=r['reason'], receipt_options_known=r['receipt_options'] is not None,
                      options_ambiguous=bool(r['options_ambiguous'])) for r in self.db.execute(
            "SELECT * FROM files WHERE state IN ('held','importing') ORDER BY first_seen,ordinal LIMIT ?", (limit,))]
        failure = self.db.execute("SELECT value FROM metadata WHERE key='last_failure'").fetchone()
        return dict(active=bool(active and active[0] == '1'), reason=reason[0] if reason else None,
                    held_files=held, files=items, more=held > len(items),
                    last_failure=json.loads(failure[0]) if failure else None)

    def mirror_holds(self, state):
        changed = False
        entries = state.setdefault('files', {})
        seen = set()
        for row in self.db.execute("SELECT * FROM files WHERE state IN ('held','importing') ORDER BY first_seen,ordinal"):
            name = Path(row['path']).name
            if name in seen:
                continue
            seen.add(name)
            prior = entries.get(name, {})
            current = dict(prior, file=name, status='held', error='recovery_hold_requires_explicit_replay',
                           recovery_reason=row['reason'], recovery_expected_sha256=row['sha256'])
            current.setdefault('sha256', row['sha256'])
            current.setdefault('checked_at', row['first_seen'])
            if current != prior:
                entries[name] = current
                changed = True
        return changed

    def reconcile(self, db_path=None, state=None, receipt_db=None):
        """Detect lost receipts and interrupted passes BEFORE receipt skipping/imports."""
        db_path = Path(db_path or self.root / 'memory.sqlite3')
        owns_state = state is None
        state = _state(self.root) if owns_state else state
        db = receipt_db or _connection(db_path)
        try:
            with self.db:
                legacy = sorted(state.get('files', {}).items(), key=lambda kv: (kv[1].get('checked_at', ''), kv[0]))
                for filename, item in legacy:
                    sha = item.get('sha256')
                    if not isinstance(sha, str) or not SHA.fullmatch(sha) or Path(filename).name != filename:
                        continue
                    if item.get('status') not in ('imported', 'already_present', 'importing'):
                        continue
                    self._register(filename, sha, item.get('checked_at'),
                        receipt_seen=item['status'] != 'importing',
                        state='importing' if item['status'] == 'importing' else 'verified')
                # Seed an external receipt baseline, including imports completed
                # before this journal existed and imports from other entry points.
                receipts_available = True
                try:
                    receipts = db.execute('SELECT object_sha,path,options_json,first_seen FROM import_receipts').fetchall()
                except sqlite3.DatabaseError:
                    self._hold_ids([r[0] for r in self.db.execute('SELECT ordinal FROM files')], 'archive_receipts_unavailable')
                    self._activate('archive_receipts_unavailable')
                    receipts = []
                    receipts_available = False
                grouped = {}
                for sha, path, options, when in receipts:
                    grouped.setdefault((sha, path), []).append((options, when))
                for (sha, path), variants in sorted(grouped.items(), key=lambda kv: (min(v[1] for v in kv[1]), kv[0][1])):
                    try:
                        options = {v[0] for v in variants}
                        row = self._register(path, sha, min(v[1] for v in variants), receipt_seen=True, state='verified')
                        if len(options) != 1:
                            self.db.execute('UPDATE files SET options_ambiguous=1 WHERE ordinal=?', (row['ordinal'],))
                            self._hold_ids([row['ordinal']], 'ambiguous_receipt_options_requires_review')
                        else:
                            self._register(path, sha, min(v[1] for v in variants), receipt_seen=True,
                                           state='verified', receipt_options=next(iter(options)))
                    except ValueError:
                        continue  # Only inbox inputs belong to this sweep journal.
                interrupted = [r['id'] for r in self.db.execute("SELECT id FROM passes WHERE state='running'")]
                for pass_id in interrupted:
                    self._hold_ids([r[0] for r in self.db.execute('SELECT file_id FROM pass_items WHERE pass_id=?',
                                                                 (pass_id,))], 'interrupted_pass_requires_review')
                    self.db.execute("UPDATE passes SET state='held',finished_at=? WHERE id=?", (stamp(), pass_id))
                self._hold_ids([r[0] for r in self.db.execute("SELECT ordinal FROM files WHERE state='importing'")],
                               'interrupted_import_requires_review')
                newly_missing = []
                for row in (self.db.execute('SELECT * FROM files WHERE receipt_seen=1').fetchall() if receipts_available else []):
                    missing = not _receipt(db, row['path'], row['sha256'], row['receipt_options'])
                    if missing and not row['missing_latched']:
                        newly_missing.append(row['ordinal'])
                    if int(missing) != row['missing_latched']:
                        self.db.execute('UPDATE files SET missing_latched=? WHERE ordinal=?',
                                        (int(missing), row['ordinal']))
                # Latch each loss once. Otherwise B's still-missing receipt would
                # re-hold already-checked A from their common pass on every replay.
                self._hold_related(newly_missing, 'prior_receipt_missing_after_restore')
            if self.mirror_holds(state) and owns_state:
                dump_atomic(self.root / 'inbox-sweep' / 'state.json', state)
            return dict(self.status(), receipt_inventory_available=receipts_available)
        finally:
            if receipt_db is None:
                db.close()

    def begin_pass(self, kind='normal'):
        if kind not in ('normal', 'replay'):
            raise ValueError('invalid_pass_kind')
        if kind == 'normal' and self.status()['active']:
            raise RuntimeError('recovery_required')
        pass_id = uuid4().hex
        with self.db:
            self.db.execute('INSERT INTO passes VALUES(?,?,NULL,?,?)', (pass_id, stamp(), 'running', kind))
        return pass_id

    def before_import(self, pass_id, path, sha, mtime_ns=0, backup=None):
        with self.db:
            run = self.db.execute('SELECT * FROM passes WHERE id=?', (pass_id,)).fetchone()
            if run is None or run['state'] != 'running':
                raise RuntimeError('recovery_pass_not_running')
            row = self._register(path, sha, mtime_ns=mtime_ns,
                                 receipt_options='{}' if run['kind'] == 'normal' else None)
            if run['kind'] == 'normal' and (self.status()['active'] or row['state'] == 'held'):
                raise RuntimeError('recovery_required')
            if run['kind'] == 'normal' and row['receipt_options'] != '{}':
                raise RuntimeError('historical_receipt_options_conflict')
            self.db.execute('INSERT OR IGNORE INTO pass_items VALUES(?,?,?)',
                            (pass_id, row['ordinal'], str(backup) if backup else None))
            self.db.execute("UPDATE files SET state='importing',reason='import_in_progress',last_pass=?,mtime_ns=? WHERE ordinal=?",
                            (pass_id, mtime_ns, row['ordinal']))

    def verified_import(self, pass_id, path, sha, db_path=None, receipt_db=None):
        path = self._path(path)
        db = receipt_db or _connection(db_path or self.root / 'memory.sqlite3')
        try:
            row = self.db.execute('SELECT receipt_options FROM files WHERE path=? AND sha256=?', (str(path), sha)).fetchone()
            if row is None or not _receipt(db, path, sha, row['receipt_options']):
                raise RuntimeError('commit_verification_failed')
            variants = db.execute('SELECT options_json FROM import_receipts WHERE object_sha=? AND path=?',
                                  (sha, str(path))).fetchall()
            if len(variants) != 1:
                with self.db:
                    self.db.execute('UPDATE files SET options_ambiguous=1 WHERE path=? AND sha256=?', (str(path), sha))
                raise RuntimeError('ambiguous_receipt_options_require_review')
            with self.db:
                changed = self.db.execute("UPDATE files SET receipt_seen=1,state='verified',reason=NULL,checked_at=? "
                    "WHERE path=? AND sha256=? AND last_pass=? AND state='importing'", (stamp(), str(path), sha, pass_id)).rowcount
                if changed != 1:
                    raise RuntimeError('missing_durable_import_intent')
        finally:
            if receipt_db is None:
                db.close()

    def fail_pass(self, pass_id, reason='import_failure_requires_review', *, stage='pass', error=None,
                  path=None, sha=None):
        with self.db:
            ids = [r[0] for r in self.db.execute('SELECT file_id FROM pass_items WHERE pass_id=?', (pass_id,))]
            self._hold_ids(ids, reason)
            self.db.execute("UPDATE passes SET state='held',finished_at=? WHERE id=?", (stamp(), pass_id))
            failure = dict(at=stamp(), pass_id=pass_id, stage=stage, reason=reason,
                           error_type=type(error).__name__ if error is not None else None)
            if path is not None:
                failure['file'] = Path(path).name
            if isinstance(sha, str) and SHA.fullmatch(sha):
                failure['sha256'] = sha
            sqlite_name = getattr(error, 'sqlite_errorname', None)
            if isinstance(sqlite_name, str) and re.fullmatch(r'SQLITE_[A-Z0-9_]{1,80}', sqlite_name):
                failure['sqlite_errorname'] = sqlite_name
            self._set('last_failure', json.dumps(failure, separators=(',', ':')))
        return self.status()

    def finish_pass(self, pass_id):
        unfinished = self.db.execute("SELECT 1 FROM pass_items JOIN files ON ordinal=file_id "
                                    "WHERE pass_id=? AND files.state!='verified' LIMIT 1", (pass_id,)).fetchone()
        if unfinished:
            return self.fail_pass(pass_id, 'incomplete_pass_requires_review')
        with self.db:
            self.db.execute("UPDATE passes SET state='completed',finished_at=? WHERE id=?", (stamp(), pass_id))
        return self.status()


def prepare_restore(root, backup_path):
    """Validate a snapshot and latch recovery. Does NOT replace or open the archive writable.

    Stop all archive writers (sweep, capture/sync, importer/connector) before an
    operator restores a DB; the sweep lock alone cannot quiesce those writers.
    Preserve recovery.sqlite3 and inbox originals. Restore SQLite with all
    connections closed and handle WAL/SHM as part of that controlled procedure.
    Resume only explicit one-file replay until the queue is checked.
    """
    root, supplied = Path(root).resolve(), Path(backup_path).absolute()
    if supplied.is_symlink() or supplied.is_junction() or not supplied.is_file():
        raise ValueError('expected_separate_regular_backup')
    backup = supplied.resolve()
    if backup.samefile(root / 'memory.sqlite3'):
        raise ValueError('expected_separate_regular_backup')
    runtime = root / 'inbox-sweep'
    runtime.mkdir(exist_ok=True)
    with capture_lock(runtime):
        quick_check(backup)
        with RecoveryJournal(root) as journal, closing(_connection(backup)) as db:
            journal.reconcile(root / 'memory.sqlite3')
            with journal.db:
                missing = [r['ordinal'] for r in journal.db.execute('SELECT * FROM files WHERE receipt_seen=1')
                           if not _receipt(db, r['path'], r['sha256'], r['receipt_options'])]
                journal._hold_related(missing, 'prepared_restore_requires_replay')
                journal._activate('restore_prepared_writers_must_remain_stopped')
                journal._set('prepared_backup', str(backup))
            state = _state(root)
            if journal.mirror_holds(state):
                dump_atomic(runtime / 'state.json', state)
            return dict(journal.status(), backup=str(backup), restored=False,
                        instruction='Stop every archive writer before restoring. Preserve the external journal and inbox originals. Replay one file per explicit invocation.')


def replay_next(root=ROOT):
    """Explicitly replay ONE oldest held input, with integrity checks before and after."""
    # Late import avoids the normal sweep integration's import cycle.
    from inbox_sweep import read_guard, contains_possible_credential, MAX_BYTES, FORMATS
    from import_memory import import_file, parse, is_temporary_file
    from codex_capture import SECRET
    root = Path(root).resolve()
    runtime, db_path = root / 'inbox-sweep', root / 'memory.sqlite3'
    runtime.mkdir(exist_ok=True)
    with capture_lock(runtime), RecoveryJournal(root) as journal:
        reconciliation = journal.reconcile(db_path)
        if not reconciliation['receipt_inventory_available']:
            return dict(reconciliation, replayed=0, state='recovery_required',
                        error='Receipt inventory is unavailable; recovery cannot be cleared.')
        item = journal.db.execute("SELECT * FROM files WHERE state='held' ORDER BY first_seen,ordinal LIMIT 1").fetchone()
        if item is None:
            try:
                quick_check(db_path)
            except Exception as exc:
                with journal.db:
                    journal._activate('archive_quick_check_failed')
                return dict(journal.status(), replayed=0, state='recovery_required', error_type=type(exc).__name__)
            with journal.db:
                journal._set('active', '0')
                journal._set('reason', '')
            return dict(journal.status(), replayed=0, state='nothing_to_replay')
        pass_id = journal.begin_pass('replay')
        path, sha = Path(item['path']), item['sha256']
        stage = 'checkpoint'
        try:
            # Checkpoint before the first check, so interruption at any point
            # leaves this attempt suspect even when a receipt already exists.
            journal.before_import(pass_id, path, sha, item['mtime_ns'])
            stage = 'precheck'
            quick_check(db_path)
            stage = 'validate'
            if item['receipt_options'] is None or item['options_ambiguous']:
                raise ValueError('historical_receipt_options_require_review')
            options = _options(item['receipt_options'])
            if path.suffix.lower() not in FORMATS or is_temporary_file(path) or path.is_symlink():
                raise ValueError('unsupported_replay_source')
            with read_guard(path) as (stream, info):
                if info.st_size > MAX_BYTES:
                    raise ValueError('replay_source_too_large')
                raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != sha:
                    raise ValueError('replay_source_changed')
                packets, _ = parse(path, raw, options)
                if (SECRET.search(raw.decode('utf-8-sig')) or contains_possible_credential(packets)
                        or contains_possible_credential(options)):
                    raise ValueError('possible_credential_review_required')
                stage = 'import'
                result = import_file(path, db_path, max_file_bytes=MAX_BYTES, expected_sha256=sha, **options)
                stage = 'receipt'
                if result.get('sha256') != sha:
                    raise RuntimeError('commit_verification_failed')
                stage = 'postcheck'
                quick_check(db_path)
                stage = 'receipt'
                journal.verified_import(pass_id, path, sha, db_path)
            journal.finish_pass(pass_id)
            with journal.db:
                if not journal.db.execute("SELECT 1 FROM files WHERE state IN ('held','importing') LIMIT 1").fetchone():
                    journal._set('active', '0')
                    journal._set('reason', '')
            stage = 'state'
            state = _state(root)
            state.setdefault('files', {})[path.name] = dict(file=path.name, sha256=sha,
                checked_at=stamp(), status='imported', recovery_verified=True)
            dump_atomic(runtime / 'state.json', state)
            return dict(journal.status(), replayed=1, state='replayed', file=path.name,
                        added_revisions=result.get('added_revisions', 0))
        except Exception as exc:
            status = journal.fail_pass(pass_id, 'replay_failed_requires_review', stage=stage, error=exc, path=path, sha=sha)
            return dict(status, replayed=0, state='recovery_required', file=path.name,
                        error_type=type(exc).__name__, error='Replay failed; this input remains held. No later input was attempted.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--status', action='store_true')
    action.add_argument('--prepare-restore', type=Path, metavar='BACKUP')
    action.add_argument('--replay-next', action='store_true')
    args = parser.parse_args(argv)
    if args.replay_next:
        result = replay_next(args.root)
    elif args.prepare_restore:
        result = prepare_restore(args.root, args.prepare_restore)
    else:
        runtime = args.root.resolve() / 'inbox-sweep'
        runtime.mkdir(exist_ok=True)
        with capture_lock(runtime), RecoveryJournal(args.root) as journal:
            result = journal.reconcile(args.root / 'memory.sqlite3')
    print(json.dumps(result, ensure_ascii=True))
    return 1 if result.get('active') else 0


if __name__ == '__main__':
    raise SystemExit(main())
