"""Bounded, read-only archive checks; only disposable diagnostic metadata is written.

These checks never repair or delete memory and never call a model or the network.
An 'ok' result describes the completed checks and samples, not a full archive or
backup restore certification. Subsequent runs rotate the rowid-based samples.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

REPORT_NAME = 'health-report.json'
MAX_REPORT_BYTES = 65536
MAX_TEXT_BYTES = 1024 * 1024
MAX_BACKUP_BYTES = 512 * 1024 * 1024


def _cache_dir(root, create=False):
    path = Path(root) / '.ai-cache'
    if _linked(path) or (path.exists() and path.resolve() != Path(root).resolve()/'.ai-cache'):
        raise ValueError('Unsafe diagnostic directory')
    if create:
        path.mkdir(exist_ok=True)
    return path


def _linked(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def read_health(root):
    """Read the bounded metadata report; malformed/missing reports return None."""
    try:
        path = _cache_dir(root) / REPORT_NAME
        if path.is_symlink() or path.stat().st_size > MAX_REPORT_BYTES:
            return None
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('format') != 'threadsatchel-health/1':
            return None
        return data
    except (OSError, ValueError, RecursionError):
        return None


def health_due(root, interval_seconds=21600):
    if type(interval_seconds) not in (int, float) or not math.isfinite(interval_seconds) or not 300 <= interval_seconds <= 604800:
        raise ValueError('Health interval must be between 300 and 604800 seconds')
    report = read_health(root)
    try:
        completed = datetime.fromisoformat(report['completed_at']).timestamp()
        age = time.time() - completed
        return age < 0 or age >= interval_seconds
    except (TypeError, KeyError, ValueError, OverflowError):
        return True


def _connect(path, deadline):
    # URI read-only plus query_only prevent accidental writes or migrations.
    if not path.is_file() or _linked(path):
        raise FileNotFoundError('Missing database')
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('PRAGMA busy_timeout=100')
    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 500)
    return db


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _rows(db, table, cursor, count, columns):
    # Identifiers are fixed internal literals; cursor and limit remain parameters.
    rows = db.execute('SELECT rowid AS sample_rowid,' + columns + ' FROM ' + table +
                      ' WHERE rowid>? ORDER BY rowid LIMIT ?', (cursor, count)).fetchall()
    if not rows and cursor:
        rows = db.execute('SELECT rowid AS sample_rowid,' + columns + ' FROM ' + table +
                          ' ORDER BY rowid LIMIT ?', (count,)).fetchall()
    return rows


def _check_structure(path, deadline, archive=False):
    with closing(_connect(path, deadline)) as db:
        db.execute('BEGIN')
        result = dict(state='ok', quick_check='ok', foreign_key_violations=0)
        # No SQLite error details are copied to reports (they can contain text).
        if [r[0] for r in db.execute('PRAGMA quick_check(5)')] != ['ok']:
            result.update(state='failed', quick_check='failed')
        result['foreign_key_violations'] = len(db.execute('PRAGMA foreign_key_check').fetchmany(5))
        if result['foreign_key_violations']:
            result['state'] = 'failed'
        result['foreign_key_count_is_capped'] = result['foreign_key_violations'] == 5
        if not archive:
            return result
        tables = _tables(db)
        if not {'memories', 'memory_fts'} <= tables:
            return dict(result, state='failed', code='archive_schema_missing')
        expected = ('NOT EXISTS (SELECT 1 FROM import_revisions r WHERE r.memory_id=m.id '
                    'AND r.canonical_memory_id!=m.id)') if 'import_revisions' in tables else '1'
        queries = {
            'records': 'SELECT count(*) FROM memories',
            'searchable_records': 'SELECT count(*) FROM memories m WHERE ' + expected,
            'fts_rows': 'SELECT count(*) FROM memory_fts',
            'duplicate_fts_rows': 'SELECT coalesce(sum(n-1),0) FROM (SELECT count(*) n FROM memory_fts GROUP BY id HAVING count(*)>1)',
            'missing_fts_rows': 'SELECT count(*) FROM memories m WHERE ' + expected + ' AND m.id NOT IN(SELECT id FROM memory_fts WHERE id IS NOT NULL)',
            'unexpected_fts_rows': 'SELECT count(*) FROM memory_fts f WHERE NOT EXISTS(SELECT 1 FROM memories m WHERE m.id=f.id AND ' + expected + ')',
            'mismatched_fts_rows': 'SELECT count(*) FROM memory_fts f JOIN memories m ON m.id=f.id WHERE f.text IS NOT m.text OR f.source IS NOT m.source OR f.title IS NOT m.title',
        }
        for name, query in queries.items():
            result[name] = db.execute(query).fetchone()[0]
        if any(result[k] for k in ('duplicate_fts_rows', 'missing_fts_rows', 'unexpected_fts_rows', 'mismatched_fts_rows')):
            result['state'] = 'failed'
        return result


def _source_samples(root, deadline, cursor, limit, config):
    import ai_memory as ai
    result = dict(state='ok', scope='rotating_sample', sampled=0, skipped_oversize=0,
                  missing_derived_records=0, stale_derived_records=0, text_hash_mismatches=0)
    with closing(_connect(root/'memory.sqlite3', deadline)) as db:
        db.execute('BEGIN')
        tables = _tables(db)
        imports = 'import_revisions' in tables and 'import_entities' in tables
        cache_path = root/'.ai-cache'/'index.sqlite3'
        cache = _connect(cache_path, deadline) if cache_path.is_file() else None
        try:
            rows = _rows(db, 'memories', cursor, limit, 'id,length(text) AS chars,length(CAST(text AS BLOB)) AS bytes,length(source) AS source_size,length(title) AS title_size')
            for row in rows:
                if time.monotonic() >= deadline:
                    result['state'] = 'partial'; break
                cursor = row['sample_rowid']
                if row['bytes'] > MAX_TEXT_BYTES or (row['source_size'] or 0) > 8192 or (row['title_size'] or 0) > 8192:
                    result['skipped_oversize'] += 1; continue
                record = dict(db.execute('SELECT * FROM memories WHERE rowid=?', (cursor,)).fetchone())
                record['source_ids'] = {}
                if imports:
                    extra = db.execute('SELECT r.canonical_memory_id,r.text_sha,e.conversation_id,e.message_id,e.speaker FROM import_revisions r LEFT JOIN import_entities e ON e.id=r.entity_id WHERE r.memory_id=?', (record['id'],)).fetchone()
                    record['source_ids'] = {k: extra[k] if extra else None for k in ('canonical_memory_id','conversation_id','message_id','speaker')}
                    if extra and hashlib.sha256(record['text'].encode('utf-8')).hexdigest() != extra['text_sha']:
                        result['text_hash_mismatches'] += 1
                result['sampled'] += 1
                # Reconciled alias revisions intentionally do not get search aids.
                if not db.execute('SELECT 1 FROM memory_fts WHERE id=?', (record['id'],)).fetchone() or ai.project_for(record, config) is None:
                    continue
                derived = cache.execute('SELECT fingerprint FROM records WHERE id=?', (record['id'],)).fetchone() if cache else None
                if derived is None:
                    result['missing_derived_records'] += 1
                elif derived['fingerprint'] != ai.source_fingerprint(record):
                    result['stale_derived_records'] += 1
        finally:
            if cache:
                cache.close()
    if result['text_hash_mismatches']:
        result['state'] = 'failed'
    elif any(result[k] for k in ('missing_derived_records','stale_derived_records')):
        result['state'] = 'attention'
    elif result['skipped_oversize']:
        result['state'] = 'partial'
    return result, cursor


def _object_samples(root, deadline, cursor, limit, max_object_bytes, max_hash_bytes):
    result = dict(state='ok', scope='rotating_retained_original_bytes', sampled=0,
                  hash_mismatches=0, skipped_oversize=0, hashed_bytes=0)
    with closing(_connect(root/'memory.sqlite3', deadline)) as db:
        db.execute('BEGIN')
        if 'import_objects' not in _tables(db):
            return dict(result, state='skipped', code='no_retained_import_objects'), cursor
        for row in _rows(db, 'import_objects', cursor, limit, 'sha256,length(raw) AS bytes'):
            if time.monotonic() >= deadline:
                result['state'] = 'partial'; break
            if row['bytes'] > min(max_object_bytes, max_hash_bytes):
                result['skipped_oversize'] += 1; cursor = row['sample_rowid']; continue
            if result['hashed_bytes'] + row['bytes'] > max_hash_bytes:
                result['state'] = 'partial'; break
            digest = hashlib.sha256()
            complete = True
            for offset in range(0, row['bytes'], 65536):
                if time.monotonic() >= deadline:
                    complete = False; break
                block = db.execute('SELECT substr(raw,?,65536) FROM import_objects WHERE rowid=?', (offset+1,row['sample_rowid'])).fetchone()[0]
                digest.update(block); result['hashed_bytes'] += len(block)
            if not complete:
                result['state'] = 'partial'; break
            result['sampled'] += 1
            result['hash_mismatches'] += digest.hexdigest() != row['sha256']
            cursor = row['sample_rowid']
    if result['hash_mismatches']:
        result['state'] = 'failed'
    elif result['skipped_oversize']:
        result['state'] = 'partial'
    return result, cursor


def _chunk_samples(root, deadline, cursor, limit, config):
    import ai_memory as ai
    from ai_embeddings import EmbeddingError, unpack_vector
    result = dict(state='ok', scope='rotating_sample', sampled=0, source_mismatches=0,
                  invalid_analysis=0, invalid_vectors=0, stale_analysis=0, skipped_oversize=0)
    cache_path = root/'.ai-cache'/'index.sqlite3'
    if not cache_path.is_file():
        return dict(result, state='skipped', code='derived_cache_missing'), cursor
    with closing(_connect(cache_path, deadline)) as cache, closing(_connect(root/'memory.sqlite3', deadline)) as db:
        cache.execute('BEGIN'); db.execute('BEGIN')
        result['chunks'] = cache.execute('SELECT count(*) FROM chunks').fetchone()[0]
        result['pending_analysis'] = cache.execute("SELECT count(*) FROM chunks WHERE coalesce(analysis_version,'')!=?", (ai.config_fingerprint(config),)).fetchone()[0]
        result['missing_vectors'] = cache.execute('SELECT count(*) FROM chunks WHERE vector IS NULL').fetchone()[0]
        result['embedding_identity_checked'] = False  # No model file hashing or launch.
        rows = _rows(cache, 'chunks', cursor, limit, 'id,length(text) AS text_bytes,length(analysis) AS analysis_bytes,length(vector) AS vector_bytes')
        dimensions = {}
        for row in rows:
            if time.monotonic() >= deadline:
                result['state'] = 'partial'; break
            cursor = row['sample_rowid']
            if (row['text_bytes'] or 0) > 8192 or (row['analysis_bytes'] or 0) > 32768 or (row['vector_bytes'] or 0) > 262144:
                result['skipped_oversize'] += 1; continue
            chunk = dict(cache.execute('SELECT * FROM chunks WHERE rowid=?', (cursor,)).fetchone())
            result['sampled'] += 1
            if (type(chunk['start']) is not int or type(chunk['end']) is not int or
                    not 0 <= chunk['start'] < chunk['end'] or chunk['end']-chunk['start'] > 8192):
                result['source_mismatches'] += 1
                continue
            original = db.execute('SELECT substr(text,?,?) FROM memories WHERE id=?', (chunk['start']+1,chunk['end']-chunk['start'],chunk['memory_id'])).fetchone()
            if not original or original[0] != chunk['text']:
                result['source_mismatches'] += 1
            if chunk['analysis'] is not None:
                try:
                    ai.validate_analysis(json.loads(chunk['analysis']), {'text': original[0] if original else ''})
                except (ValueError, TypeError, KeyError, RecursionError):
                    result['invalid_analysis'] += 1
                if chunk['analysis_version'] != ai.config_fingerprint(config):
                    result['stale_analysis'] += 1
            if chunk['vector'] is not None:
                try:
                    vector = unpack_vector(chunk['vector'])
                    version = chunk['embedding_version']
                    if len(vector) != dimensions.setdefault(version, len(vector)):
                        raise ValueError('Inconsistent vector dimensions')
                except (ValueError, TypeError, KeyError, RecursionError, EmbeddingError):
                    result['invalid_vectors'] += 1
    if result['invalid_analysis'] or result['invalid_vectors']:
        result['state'] = 'failed'
    elif result['source_mismatches'] or result['stale_analysis'] or result['pending_analysis'] or result['missing_vectors']:
        result['state'] = 'attention'
    elif result['skipped_oversize']:
        result['state'] = 'partial'
    return result, cursor


def _backup_sample(root, deadline, cursor):
    result = dict(state='attention', scope='one_bounded_sqlite_backup', candidates=0,
                  verified=0, restore_tested=False, archive_equivalence_checked=False,
                  discovery_complete=True, candidates_examined=0,
                  skipped_nonarchive=0, skipped_oversize=0)
    directory = root/'backups'
    if not directory.is_dir() or _linked(directory) or directory.resolve().parent != root:
        return dict(result, code='backup_directory_missing'), cursor
    candidates, pending, visited = [], [(directory, 0)], 0
    while pending and visited < 256 and time.monotonic() < deadline:
        path, depth = pending.pop(0)
        with os.scandir(path) as entries:
            for entry in entries:
                visited += 1
                if visited > 256 or time.monotonic() >= deadline:
                    result['discovery_complete'] = False; break
                if _linked(Path(entry.path)):
                    continue
                if entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in ('.sqlite3', '.sqlite', '.db'):
                    candidates.append(Path(entry.path))
                elif depth == 0 and entry.is_dir(follow_symlinks=False):
                    pending.append((Path(entry.path), 1))
    if pending:
        result['discovery_complete'] = False
    result['candidates'] = len(candidates)
    if not candidates:
        return dict(result, code='no_sqlite_backup_in_bounded_scan'), cursor
    candidates.sort()
    # Derived index snapshots share the backup tree. Inspect a small consecutive
    # window so those cannot consume an entire scheduled archive check each.
    for _ in range(min(4,len(candidates))):
        if time.monotonic() >= deadline:
            return dict(result,state='partial',code='backup_sample_budget'),cursor
        path = candidates[cursor % len(candidates)]
        cursor += 1
        result['candidates_examined'] += 1
        if path.stat().st_size > MAX_BACKUP_BYTES:
            result['skipped_oversize'] += 1
            continue
        # Avoid reporting arbitrary backup paths, file contents, or exception text.
        try:
            with closing(_connect(path, deadline)) as db:
                if 'memories' not in _tables(db):
                    result['skipped_nonarchive'] += 1
                    continue
                okay = [r[0] for r in db.execute('PRAGMA quick_check(5)')] == ['ok']
        except sqlite3.Error as error:
            code = getattr(error,'sqlite_errorcode',None)
            deferred = time.monotonic() >= deadline or code in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED,sqlite3.SQLITE_INTERRUPT)
            return dict(result,state='partial' if deferred else 'failed',
                        code='database_busy_or_budget' if deferred else 'database_check_failed'),cursor
        return dict(result, state='ok' if okay else 'failed', verified=int(okay),
                    quick_check='ok' if okay else 'failed', sample_age_seconds=max(0,round(time.time()-path.stat().st_mtime))), cursor
    return dict(result,state='partial',code='no_archive_backup_in_sample_window'),cursor


def run_health(root, budget_seconds=8, *, sample_records=24, sample_objects=4,
               sample_chunks=24, max_object_bytes=8*1024*1024, max_hash_bytes=16*1024*1024):
    """Run six individually budgeted checks; save metadata with atomic replacement.

    SQLite opcodes, streamed hashes, enumeration and row samples have deadlines.
    A single OS filesystem call can exceed the soft deadline on stalled storage.
    No authority/cache database is opened writable. No repair is attempted.
    """
    import ai_memory as ai
    if type(budget_seconds) not in (int,float) or not math.isfinite(budget_seconds) or not .05 <= budget_seconds <= 30:
        raise ValueError('Health budget must be between .05 and 30 seconds')
    for value, low, high in ((sample_records,1,100),(sample_objects,1,32),(sample_chunks,1,100),
                            (max_object_bytes,1,32*1024*1024),(max_hash_bytes,1,64*1024*1024)):
        if type(value) is not int or not low <= value <= high:
            raise ValueError('Invalid health sample bound')
    root = Path(root).resolve()
    _cache_dir(root)  # Reject redirected metadata/cache directories before any reads.
    prior = read_health(root) or {}
    old = prior.get('cursors', {})
    if not isinstance(old, dict):
        old = {}
    cursors = {key: old.get(key,0) if type(old.get(key)) is int and 0 <= old[key] <= 2**63-1 else 0
               for key in ('source','objects','chunks','backup')}
    config = ai.load_config(root)
    started = time.monotonic()
    checks = {}
    work = [
        ('archive_structure', lambda end: (_check_structure(root/'memory.sqlite3',end,True),None), None),
        ('derived_structure', lambda end: (_check_structure(root/'.ai-cache'/'index.sqlite3',end),None), None),
        ('source_sample', lambda end: _source_samples(root,end,cursors['source'],sample_records,config), 'source'),
        ('original_hash_sample', lambda end: _object_samples(root,end,cursors['objects'],sample_objects,max_object_bytes,max_hash_bytes), 'objects'),
        ('derived_sample', lambda end: _chunk_samples(root,end,cursors['chunks'],sample_chunks,config), 'chunks'),
        ('backup_sample', lambda end: _backup_sample(root,end,cursors['backup']), 'backup'),
    ]
    for index, (name, operation, cursor_key) in enumerate(work):
        now = time.monotonic()
        deadline = min(started+budget_seconds, now + max(0,started+budget_seconds-now)/(len(work)-index))
        if now >= deadline:
            checks[name] = dict(state='partial',code='budget_exhausted'); continue
        try:
            result, cursor = operation(deadline)
            checks[name] = result
            if cursor_key:
                cursors[cursor_key] = cursor
        except FileNotFoundError:
            checks[name] = dict(state='failed' if name=='archive_structure' else 'skipped',code='database_missing')
        except sqlite3.Error as error:
            code = getattr(error, 'sqlite_errorcode', None)
            deferred = time.monotonic() >= deadline or code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_INTERRUPT)
            checks[name] = dict(state='partial' if deferred else 'failed',code='database_busy_or_budget' if deferred else 'database_check_failed')
        except (OSError, ValueError, TypeError, KeyError):
            checks[name] = dict(state='failed',code='check_failed')
    states = {check['state'] for check in checks.values()}
    if 'skipped' in states:
        states.add('partial')
    state = next((item for item in ('failed','partial','attention') if item in states),'ok')
    report = dict(format='threadsatchel-health/1', state=state,
                  completed_at=datetime.now(timezone.utc).isoformat(),
                  elapsed_seconds=round(time.monotonic()-started,3),
                  full_archive_verified=False, repairs_attempted=False,
                  cursors=cursors, checks=checks)
    directory = _cache_dir(root, create=True)
    target = directory/REPORT_NAME
    if target.is_symlink():
        raise ValueError('Unsafe diagnostic report')
    temp = directory/('.health-'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('x',encoding='utf-8') as stream:
            json.dump(report,stream,sort_keys=True,separators=(',',':'))
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temp,target)
    finally:
        temp.unlink(missing_ok=True)
    return report
