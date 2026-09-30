"""Bounded local lexical retrieval with optional, failure-tolerant AI enhancement."""
from pathlib import Path
import re

from import_metadata import compact_source_ids, enrich_many

MAX_RESULTS = 100
MAX_TEXT = 600
MAX_QUERY = 2048
MAX_TERMS = 32
STOP_WORDS = frozenset('''a an and are as at be been but by can could did do does
    for from had has have how i in is it its me my of on or our please show tell
    than that the their them then there these they this to us was we were what
    when where which who why will with would you your find search
    remember about'''.split())


def _bounded(value, maximum):
    if value is None or len(value) <= maximum:
        return value
    return value[:maximum - 1] + '\u2026'


def _excerpt(value, pattern):
    if len(value) <= MAX_TEXT:
        return value
    match = pattern.search(value) if pattern is not None else None
    # Token lengths vary; keep the hit visible even when a 64-token FTS fragment is long.
    start = max(0, match.start() - MAX_TEXT // 3) if match else 0
    body = value[start:start + MAX_TEXT - 2]
    return ('\u2026' if start else '') + body + ('\u2026' if start + len(body) < len(value) else '')


def _query_plan(query):
    if not isinstance(query, str):
        raise ValueError('query must be a string')
    query = query.strip()[:MAX_QUERY]
    words = re.findall(r'[^\W_]+', query, flags=re.UNICODE)
    terms = list(dict.fromkeys(word.casefold() for word in words
                              if word.casefold() not in STOP_WORDS))[:MAX_TERMS]
    # Literal quotes select phrases; FTS operators and punctuation never become syntax.
    phrases = []
    for quoted in re.findall(r'"([^"\n]+)"', query):
        tokens = re.findall(r'[^\W_]+', quoted, flags=re.UNICODE)[:MAX_TERMS]
        if tokens:
            phrases.append('"' + ' '.join(tokens) + '"')
    if len(terms) > 1:
        phrases.append('"' + ' '.join(terms) + '"')
    expressions = list(dict.fromkeys(phrases))[:4]
    if terms:
        literals = ['"' + term + '"' for term in terms]
        expressions.extend([' AND '.join(literals), ' OR '.join(literals)])
    return query, list(dict.fromkeys(expressions))


def validate_limit(limit):
    """Clamp integer limits to the documented 1..100 window; reject implicit coercion."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError('limit must be an integer')
    return max(1, min(MAX_RESULTS, limit))


def lexical_search(db, query, limit=20):
    """Return compact hits: exact ID, phrase, all words, then literal OR fallback."""
    limit = validate_limit(limit)
    query, expressions = _query_plan(query)
    if not query:
        return []
    hits = []
    seen = set()
    words = [word for word in re.findall(r'[^\W_]+', query, flags=re.UNICODE)
             if word.casefold() not in STOP_WORDS][:MAX_TERMS]
    pattern = re.compile(r'\b(?:' + '|'.join(re.escape(word) for word in words) + r')\b',
                         flags=re.IGNORECASE) if words else None

    def append(rows):
        for row in rows:
            if row['id'] in seen or len(hits) >= limit:
                continue
            record = dict(row)
            record['text'] = _excerpt(record['text'], pattern)
            record['title'] = _bounded(record['title'], 240)
            record['source'] = _bounded(record['source'], 240)
            hits.append(record)
            seen.add(record['id'])

    # memory_fts.id is UNINDEXED, so exact IDs need the memories primary-key lookup.
    append(db.execute('''SELECT id,substr(text,1,601) AS text,source,title,created_at
                         FROM memories WHERE id=?''', (query,)))
    for expression in expressions:
        if len(hits) >= limit:
            break
        # Keep full message bodies in SQLite. FTS extracts a matching fragment here.
        append(db.execute('''SELECT m.id,
            snippet(memory_fts,1,'','',' \u2026 ',64) AS text,
            substr(m.source,1,241) AS source,substr(m.title,1,241) AS title,m.created_at
            FROM memory_fts JOIN memories AS m ON m.id=memory_fts.id
            WHERE memory_fts MATCH ?
            ORDER BY CASE WHEN lower(m.title)=lower(?) THEN 0 ELSE 1 END,
                     bm25(memory_fts),m.created_at DESC,m.id LIMIT ?''',
            (expression, query, limit)))
    return hits


def search(db, root, query, limit=20, full=False):
    """Rank once; full=True expands the same IDs to original bodies and provenance."""
    limit = validate_limit(limit)
    if not isinstance(full, bool):
        raise ValueError('full must be a boolean')
    query, expressions = _query_plan(query)
    if not query:
        return []
    owns_snapshot = not db.in_transaction
    if owns_snapshot:
        db.execute('BEGIN')
    try:
        hits = lexical_search(db, query, limit)
        if not expressions and not hits:
            return []
        hits = compact_source_ids(db, hits)
        # Config is read by the optional module on every call, so stdio workers see toggles.
        try:
            from ai_memory import enhance_search
        except ImportError:
            enhance_search = None
        try:
            if enhance_search is None:
                enhanced = hits
            else:
                enhanced = enhance_search(Path(root), db, query, hits, limit,
                                          release_snapshot=owns_snapshot)
            if isinstance(enhanced, list) and all(isinstance(hit, dict) and 'id' in hit for hit in enhanced):
                hits = enhanced[:limit]
        except Exception:
            # A model/service failure must never take the authoritative lexical path down.
            pass
        if full and hits:
            if owns_snapshot and not db.in_transaction:
                db.execute('BEGIN')
            ids = list(dict.fromkeys(hit['id'] for hit in hits))
            records = {row['id']: row for row in db.execute(
                'SELECT * FROM memories WHERE id IN (' + ','.join('?' for _ in ids) + ')', ids)}
            return enrich_many(db, [records[mid] for mid in ids if mid in records])
        return hits
    finally:
        if owns_snapshot and db.in_transaction:
            db.rollback()
