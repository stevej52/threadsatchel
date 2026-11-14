"""Read-only keyset pagination over original records, independent of search/AI.

A cursor fixes membership at the first page's rowid boundary. Membership IDs
are hashed on each page, so removals/replacements require restarting instead
of silently skipping records. New append-only imports join the next scan.
This is a membership snapshot, not a frozen copy of mutable record contents.
"""
import base64
import hashlib
import json
import re
from pathlib import Path

from memory_budget import lookup_budget

MAX_PAGE_SIZE = 100
MAX_PREVIEW = 120
MAX_CURSOR = 4096
_FIELDS = {'v', 'upper', 'fingerprint', 'after', 'total', 'seen'}


def _encode(state):
    return base64.urlsafe_b64encode(json.dumps(state, ensure_ascii=True,
        separators=(',', ':'), sort_keys=True).encode('ascii')).decode('ascii').rstrip('=')


def _decode(cursor):
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= MAX_CURSOR:
            raise ValueError()
        if not re.fullmatch(r'[A-Za-z0-9_-]+', cursor):
            raise ValueError()
        state = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4),
                                          altchars=b'-_', validate=True))
        if not isinstance(state, dict) or set(state) != _FIELDS:
            raise ValueError()
        if any(type(state[k]) is not int for k in ('v', 'upper', 'total', 'seen')):
            raise ValueError()
        if state['v'] != 1 or not 0 < state['upper'] <= 2**63 - 1:
            raise ValueError()
        if not 0 < state['seen'] < state['total'] <= 2**63 - 1:
            raise ValueError()
        if not isinstance(state['after'], str) or not state['after']:
            raise ValueError()
        if not isinstance(state['fingerprint'], str) or not re.fullmatch(r'[0-9a-f]{64}', state['fingerprint']):
            raise ValueError()
        return state
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError('Invalid archive cursor; start again without a cursor') from None


def list_records(db, limit=3, cursor=None, *, root=None, budget_tokens=2000, lookup_id=None):
    """Return bounded previews of every original ID, never ranked or AI chunks.

    Follow next_cursor until null, then compare returned_total with total_count.
    Use revision-linked get_memory slices for original text. Cursors are opaque
    continuation state, not credentials, and never authorize additional data.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError('limit must be an integer from 1 to 100')
    state = _decode(cursor) if cursor is not None else None
    owns_snapshot = not db.in_transaction
    try:
        if owns_snapshot:
            db.execute('BEGIN')
        upper = state['upper'] if state else db.execute(
            'SELECT COALESCE(MAX(rowid),0) FROM memories').fetchone()[0]
        # The ID index covers this scan; original message bodies are not read.
        digest, total, seen, found = hashlib.sha256(), 0, 0, False
        for row in db.execute('SELECT id FROM memories WHERE rowid<=? ORDER BY id', (upper,)):
            memory_id = row[0]
            encoded = memory_id.encode('utf-8')
            digest.update(len(encoded).to_bytes(8, 'big'))
            digest.update(encoded)
            total += 1
            if state and memory_id <= state['after']:
                seen += 1
                found = found or memory_id == state['after']
        fingerprint = digest.hexdigest()
        if state and (total != state['total'] or fingerprint != state['fingerprint']
                      or seen != state['seen'] or not found):
            raise ValueError('Archive membership changed or cursor is invalid; start again without a cursor')
        after = state['after'] if state else None
        rows = db.execute('''SELECT id, substr(title,1,80) AS title,
            substr(source,1,100) AS source, substr(created_at,1,40) AS created_at,
            substr(text,1,?) AS text, length(text)>? AS text_truncated
            FROM memories WHERE rowid<=? AND (? IS NULL OR id>?)
            ORDER BY id LIMIT ?''', (MAX_PREVIEW, MAX_PREVIEW, upper, after, after, limit))
        records = [dict(row) for row in rows]
        for record in records:
            record['text_truncated'] = bool(record['text_truncated'])
        if root is None:
            path = db.execute('PRAGMA database_list').fetchone()[2]
            root = Path(path).parent if path else Path(__file__).resolve().parent
        with lookup_budget(root, budget_tokens, lookup_id) as budget:
            def response(selected):
                returned_total = seen + len(selected)
                has_more = returned_total < total
                next_cursor = _encode(dict(v=1, upper=upper, fingerprint=fingerprint,
                    after=selected[-1]['id'], total=total, seen=returned_total)) if has_more and selected else None
                return dict(status='ok', records=selected, returned_count=len(selected),
                    returned_total=returned_total, total_count=total, has_more=has_more,
                    next_cursor=next_cursor)
            selected = []
            for record in records:
                selected.append(record)
                if not budget.fits(response(selected)):
                    selected.pop()
                    break
                budget.authorize(record['id'])
            if records and not selected:
                return budget.finish(dict(status='budget_exhausted',
                    next_action='Begin a new lookup with the same cursor'))
            return budget.finish(response(selected))
    finally:
        if owns_snapshot and db.in_transaction:
            db.rollback()
