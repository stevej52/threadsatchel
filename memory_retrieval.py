"""Revision-linked source maps and exact, budgeted slices of original memories.

All coordinates are Python/Unicode code points, zero based, half-open. A block
is a navigation window, not a claim about syntax or meaning. The derived SQLite
index is disposable; original rows and their newlines are never rewritten.
"""
import hashlib
from contextlib import closing
from datetime import datetime, timezone
from itertools import chain
import re
import sqlite3
import time
import unicodedata

from memory_budget import (cache_connect, count_method, lookup_budget, transport_cost, token_count,
                           resolve_reference, REFERENCE_PATTERN)

OFFSET_UNIT = 'unicode_codepoint_half_open'
MAX_HITS = 20
MAX_BLOCK = 1200
MAP_PAGE = 5
MAX_INDEX_RECORDS = 256
MAX_INDEX_CHARS = 8 * 1024 * 1024
INDEX_VERSION = 2
_NEWLINE = re.compile(r'\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]')


def source_revision(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _key(revision, start, end, prefix='b'):
    return prefix + hashlib.sha256(f'{revision}:{start}:{end}'.encode()).hexdigest()[:20]


def _int(value, name, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(name + ' is outside the allowed integer range')
    return value


def _record(db, memory_id):
    if not isinstance(memory_id, str) or not memory_id or len(memory_id) > 4096:
        raise ValueError('id must be nonempty text of at most 4096 characters')
    row = db.execute('SELECT id,text,title,source,created_at FROM memories WHERE id=?',
                     (memory_id,)).fetchone()
    return dict(row) if row is not None else None


def source_blocks(text, revision=None):
    """Partition exact text into section/fence-aware line windows, without gaps.

    Closing fences must match the opener character and be at least its length.
    Fenced code remains an atomic structural block of any length. Long plain
    sections become explicit line windows; response budgets apply at retrieval.
    """
    revision = revision or source_revision(text)
    units, offset, line_no = [], 0, 1
    section_start, section_line, label, kind = 0, 1, 'Text', 'section'
    fence = None

    def flush(end, end_line):
        nonlocal section_start, section_line
        if end > section_start:
            units.append((section_start, end, section_line, end_line, label, kind))
        section_start, section_line = end, end_line + 1

    for line in text.splitlines(keepends=True):
        opener = re.match(r'^ {0,3}(`{3,}|~{3,})([^\r\n]*)', line)
        heading = re.match(r'^ {0,3}#{1,6}\s+(.+?)(?:\r?\n)?$', line)
        if fence is None and opener:
            flush(offset, line_no - 1)
            fence = (opener[1][0], len(opener[1]))
            label, kind = ('Code ' + opener[2].strip())[:80], 'fenced_code'
            section_start, section_line = offset, line_no
        elif fence is not None:
            if re.fullmatch(r' {0,3}' + re.escape(fence[0]) + '{' + str(fence[1]) + r',}[ \t]*(?:\r\n|\r|\n)?', line):
                offset += len(line)
                flush(offset, line_no)
                fence, label, kind = None, 'Text', 'section'
                line_no += 1
                continue
        elif heading:
            flush(offset, line_no - 1)
            label, kind = heading[1].strip()[:80], 'section'
            section_start, section_line = offset, line_no
        offset += len(line)
        line_no += 1
    flush(len(text), max(1, line_no - 1))
    result = []
    for start, end, first_line, last_line, title, unit_kind in units:
        position, current_line = start, first_line
        while position < end:
            stop = end if unit_kind == 'fenced_code' else min(position + MAX_BLOCK, end)
            if stop < end:
                newlines = list(_NEWLINE.finditer(text, position, stop))
                if newlines:
                    stop = newlines[-1].end()
                    if text[stop-1:stop+1] == '\r\n':
                        stop -= 1
                        if stop == position:
                            stop += 2
            body = text[position:stop]
            breaks = list(_NEWLINE.finditer(body))
            final_line = current_line + len(breaks) - int(bool(breaks) and breaks[-1].end() == len(body))
            result.append(dict(block_id=_key(revision, position, stop),
                start_char=position, end_char=stop, start_line=current_line,
                end_line=max(current_line, final_line),
                kind=unit_kind if position == start and stop == end else unit_kind + '_window',
                label=title))
            current_line += len(breaks)
            position = stop
    return result


def _index(root, record):
    revision = source_revision(record['text'])
    if len(record['text']) > MAX_INDEX_CHARS:
        return revision, source_blocks(record['text'], revision)
    cache = cache_connect(root)
    try:
        cache.executescript('''
            CREATE TABLE IF NOT EXISTS source_maps(memory_id TEXT PRIMARY KEY,revision TEXT NOT NULL,
                touched REAL NOT NULL DEFAULT 0,index_version INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS blocks(memory_id TEXT NOT NULL,revision TEXT NOT NULL,
                block_id TEXT NOT NULL,start_char INTEGER NOT NULL,end_char INTEGER NOT NULL,
                start_line INTEGER NOT NULL,end_line INTEGER NOT NULL,kind TEXT NOT NULL,label TEXT NOT NULL,
                PRIMARY KEY(memory_id,block_id));
            CREATE VIRTUAL TABLE IF NOT EXISTS block_fts USING fts5(
                memory_id UNINDEXED,block_id UNINDEXED,text,tokenize='unicode61');
        ''')
        if 'touched' not in {row[1] for row in cache.execute('PRAGMA table_info(source_maps)')}:
            cache.execute('ALTER TABLE source_maps ADD COLUMN touched REAL NOT NULL DEFAULT 0')
        if 'index_version' not in {row[1] for row in cache.execute('PRAGMA table_info(source_maps)')}:
            cache.execute('ALTER TABLE source_maps ADD COLUMN index_version INTEGER NOT NULL DEFAULT 0')
        cache.execute('BEGIN IMMEDIATE')
        old = cache.execute('SELECT revision,index_version FROM source_maps WHERE memory_id=?', (record['id'],)).fetchone()
        if old is None or old[0] != revision or old[1] != INDEX_VERSION:
            blocks = source_blocks(record['text'], revision)
            cache.execute('DELETE FROM blocks WHERE memory_id=?', (record['id'],))
            cache.execute('DELETE FROM block_fts WHERE memory_id=?', (record['id'],))
            for block in blocks:
                cache.execute('INSERT INTO blocks VALUES(?,?,?,?,?,?,?,?,?)',
                    (record['id'], revision, block['block_id'], block['start_char'],
                     block['end_char'], block['start_line'], block['end_line'], block['kind'], block['label']))
                cache.execute('INSERT INTO block_fts VALUES(?,?,?)', (record['id'], block['block_id'],
                    record['text'][block['start_char']:block['end_char']]))
            cache.execute('INSERT OR REPLACE INTO source_maps(memory_id,revision,touched,index_version) VALUES(?,?,?,?)',
                          (record['id'], revision, time.time(), INDEX_VERSION))
        else:
            cache.execute('UPDATE source_maps SET touched=? WHERE memory_id=?', (time.time(), record['id']))
        # Eviction affects derived navigation only; originals remain authoritative.
        kept, chars = 0, 0
        for entry in cache.execute('''SELECT s.memory_id,COALESCE(SUM(b.end_char-b.start_char),0) AS chars
                FROM source_maps s LEFT JOIN blocks b ON b.memory_id=s.memory_id
                GROUP BY s.memory_id ORDER BY s.touched DESC,s.memory_id''').fetchall():
            kept += 1
            chars += entry['chars']
            if kept > MAX_INDEX_RECORDS or chars > MAX_INDEX_CHARS:
                cache.execute('DELETE FROM blocks WHERE memory_id=?', (entry['memory_id'],))
                cache.execute('DELETE FROM block_fts WHERE memory_id=?', (entry['memory_id'],))
                cache.execute('DELETE FROM source_maps WHERE memory_id=?', (entry['memory_id'],))
        blocks = [dict(row) for row in cache.execute(
            'SELECT block_id,start_char,end_char,start_line,end_line,kind,label FROM blocks '
            'WHERE memory_id=? AND revision=? ORDER BY start_char', (record['id'], revision))]
        cache.commit()
        return revision, blocks
    finally:
        cache.close()


def _normalize(text):
    chars, offsets = [], []
    for index, char in enumerate(text):
        normalized = ''.join(c for c in unicodedata.normalize('NFD', char.casefold())
                             if unicodedata.category(c) != 'Mn')
        for normalized_char in normalized:
            chars.append(normalized_char)
            offsets.append(index)
    return ''.join(chars), offsets


def lexical_spans(text, query, revision=None, maximum=5):
    """Literal lexical matches with original offsets, never generated locations."""
    revision = revision or source_revision(text)
    normalized, offsets = _normalize(text)
    from memory_search import STOP_WORDS
    terms = list(dict.fromkeys(term for term in re.findall(r'[^\W_]+', _normalize(query)[0])
                               if term not in STOP_WORDS))[:32]
    if not terms:
        return []
    # unicode61 and our query planner split underscores; Python \b does not.
    pattern = re.compile(r'(?<![^\W_])(?:' + '|'.join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) + r')(?![^\W_])')
    matches = []
    for match in pattern.finditer(normalized):
        start, end = offsets[match.start()], offsets[match.end()-1] + 1
        while end < len(text) and unicodedata.category(text[end]) == 'Mn':
            end += 1
        matches.append(dict(match_id=f'm{start}:{end}:' + _key(revision, start, end, '')[:12],
                            start_char=start, end_char=end))
        if len(matches) >= maximum:
            break
    return matches


def _matched_spans(root, record, revision, query):
    """Rank indexed line windows, then derive exact lexical locations locally."""
    if len(record['text']) > MAX_INDEX_CHARS:
        return lexical_spans(record['text'], query, revision, maximum=2)
    from memory_search import _query_plan
    _, expressions = _query_plan(query)
    cache = cache_connect(root)
    try:
        for expression in expressions:
            rows = cache.execute('''SELECT b.start_char,b.end_char FROM block_fts
                JOIN blocks b ON b.memory_id=block_fts.memory_id AND b.block_id=block_fts.block_id
                WHERE block_fts MATCH ? AND b.memory_id=? AND b.revision=?
                ORDER BY bm25(block_fts),b.start_char LIMIT 2''',
                (expression, record['id'], revision)).fetchall()
            found = []
            for row in rows:
                for match in lexical_spans(record['text'][row['start_char']:row['end_char']], query,
                                           revision, maximum=2):
                    start, end = match['start_char']+row['start_char'], match['end_char']+row['start_char']
                    found.append(dict(match_id=f'm{start}:{end}:' + _key(revision,start,end,'')[:12],
                                      start_char=start,end_char=end))
            if found:
                return found[:2]
        return lexical_spans(record['text'], query, revision, maximum=2)
    finally:
        cache.close()


def _base(record, revision):
    result = dict(id=record['id'], revision=revision, offset_unit=OFFSET_UNIT,
                  total_chars=len(record['text']))
    if record.get('title'):
        result['title'] = record['title'][:120]
    if record.get('created_at'):
        result['created_at'] = record['created_at'][:40]
    if record.get('source'):
        result['source'] = record['source'][:120]
    return result


def _catalogue(root, record, revision):
    try:
        from memory_catalogue import catalogue_for_record
        entry = catalogue_for_record(root, record['id'], revision)
        if entry and entry.get('revision', revision) == revision:
            result = dict(trust='interpretation', action_authority=False)
            coverage = entry.get('coverage', {})
            result['partial'] = bool(coverage.get('partial', True))
            counts = {key: value for key, value in coverage.items()
                      if type(value) is int and value >= 0 and key in
                      ('indexed_chunks','analyzed_chunks','sampled_chunks','source_chars','indexed_chars')}
            if counts:
                result['coverage'] = counts
            if entry.get('suggested_title'):
                result['suggested_title'] = str(entry['suggested_title'])[:120]
            if entry.get('summary'):
                result['summary'] = str(entry['summary'])[:240]
            if entry.get('summary_source'):
                result['summary_source'] = entry['summary_source']
            if entry.get('topics'):
                result['topics'] = [str(topic)[:60] for topic in entry['topics'][:3]]
            evidence = []
            for source in entry.get('evidence', [])[:2]:
                start, end = source.get('start_char'), source.get('end_char')
                if type(start) is int and type(end) is int and 0 <= start < end <= len(record['text']):
                    # Quotes are original evidence, never model paraphrases.
                    if record['text'][start:end] == source.get('quote'):
                        evidence.append(dict(label=str(source.get('label', 'unknown'))[:40],
                                             start_char=start, end_char=end))
            if evidence:
                result['evidence'] = evidence
            stages = {}
            for name, claim in entry.get('outcome_card', {}).get('stages', {}).items():
                start, end = claim.get('start_char'), claim.get('end_char')
                if (name in ('problem','attempt','result','decision') and type(start) is int and
                    type(end) is int and 0 <= start < end <= len(record['text']) and
                    record['text'][start:end] == claim.get('quote')):
                    stages[name] = {key: claim[key] for key in
                        ('label','quote','start_char','end_char','chunk_id') if key in claim}
            if stages:
                result['outcome_card'] = dict(stages=stages, interpretation=True, partial=True)
            return result
    except (ImportError, OSError, ValueError):
        pass
    return None


def _conversation(root, record, revision):
    """A small session hint with one cited source; never an exhaustive verdict."""
    try:
        from memory_catalogue import conversation_for_record
        entry = conversation_for_record(root, record['id'], revision)
        if not entry:
            return None
        result = dict(conversation_id=entry['conversation_id'], trust='interpretation',
            action_authority=False, partial=entry.get('coverage', {}).get('partial', True))
        if entry.get('suggested_title'):
            result['suggested_title'] = str(entry['suggested_title'])[:80]
        summaries = entry.get('summaries') or []
        result['partial'] = bool(result['partial'] or len(summaries) > 1 or
                                 entry.get('coverage', {}).get('archive_records', 1) > 1)
        if summaries:
            cited = summaries[0]
            result['summary'] = dict(text=str(cited.get('text', ''))[:160],
                memory_id=cited['memory_id'], revision=cited['revision'], source=cited.get('source', {}))
        return result
    except (ImportError, OSError, ValueError, KeyError, TypeError):
        return None


def _authorize_hint(budget, item):
    summary = item.get('conversation', {}).get('summary')
    if summary:
        budget.authorize(summary['memory_id'])


def _target(db, root, id, revision, ref, lookup_id):
    """Resolve compact refs without weakening legacy explicit revision checks."""
    selected = None
    if ref is not None:
        if id is not None:
            raise ValueError('Choose id or ref, not both')
        selected = resolve_reference(root, lookup_id, ref)
    elif (lookup_id and isinstance(id, str) and REFERENCE_PATTERN.fullmatch(id) and
          db.execute('SELECT 1 FROM memories WHERE id=?', (id,)).fetchone() is None):
        # Older tool schemas can carry a returned short ref in their id field.
        selected = resolve_reference(root, lookup_id, id, optional=True)
    if selected:
        if revision is not None and revision != selected['revision']:
            raise ValueError('revision conflicts with the lookup-bound ref')
        return selected['memory_id'], selected['revision'], selected
    return id, revision, None


def _note_selected(root, memory_id):
    try:
        from ai_memory import note_retrieved
        note_retrieved(root, [memory_id])
    except Exception:
        # Background prioritization must not affect authoritative source access.
        pass


def _range_payload(record, revision, start, end):
    return dict(status='ok', kind='slice', id=record['id'], revision=revision,
        offset_unit=OFFSET_UNIT, start_char=start, end_char=end,
        total_chars=len(record['text']), text=record['text'][start:end], complete=True)


def _finish_evidence(budget, payload, root, record):
    if payload.get('status') == 'ok':
        metadata = _catalogue(root, record, payload['revision'])
        if metadata:
            enriched = dict(payload, catalogue=metadata)
            if budget.fits(enriched):
                payload = enriched
    return budget.finish(payload)


def _range(budget, record, revision, start, end):
    result = _range_payload(record, revision, start, end)
    if budget.fits(result):
        return result
    # An explicit range is atomic: never silently return only part of a function.
    lo, hi = start, end
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = _range_payload(record, revision, start, mid)
        # The suggested next request starts a fresh random lookup ID, whose
        # tokenizer length can differ even though its character length is fixed.
        if budget.fits(candidate) and transport_cost(budget.envelope(candidate)) + 64 <= budget.capacity-budget.used:
            lo = mid
        else:
            hi = mid - 1
    return dict(status='range_too_large', id=record['id'], revision=revision,
        offset_unit=OFFSET_UNIT, requested_start=start, requested_end=end,
        suggested_end_char=lo if lo > start else None,
        next_lookup_required=True,
        next_action='The suggested range needs a separately authorized lookup; do not reset this budget automatically')


def _map(budget, root, record, revision, blocks, start=0):
    result = dict(_base(record, revision), status='ok', kind='map', blocks=[],
                  next_start_char=None, total_blocks=len(blocks))
    # After discovery, a map is another navigation step, not the final evidence.
    # Source IDs/revision and exact block coordinates stay; repeated labels and
    # optional generated metadata cannot consume the remaining reading budget.
    stage_max = min(450, budget.capacity-budget.used) if budget.used else None
    if stage_max is not None:
        for field in ('title','created_at','source'):
            result.pop(field, None)
    fits = (lambda value: _navigation_fits(budget,value,stage_max)) if stage_max is not None else budget.fits
    eligible = [b for b in blocks if b['end_char'] > start]
    for block in eligible[:MAP_PAGE]:
        result['blocks'].append(block)
        result['next_start_char'] = block['end_char'] if block != eligible[-1] else None
        if not fits(result):
            result['blocks'].pop()
            result['next_start_char'] = block['start_char']
            break
    if eligible and not result['blocks']:
        return dict(status='budget_exhausted', next_action='Additional source retrieval requires a separately authorized lookup')
    catalogue = _catalogue(root, record, revision)
    if catalogue:
        result['catalogue'] = catalogue
        if not fits(result):
            result.pop('catalogue')
    conversation = _conversation(root, record, revision)
    if conversation:
        candidate = dict(result, conversation=conversation)
        if not fits(candidate):
            candidate.pop('catalogue', None)
        if fits(candidate):
            result = candidate
        _authorize_hint(budget, result)
    return result


def get_memory_slice(db, root, id=None, revision=None, block_id=None, start_char=None,
                     end_char=None, full=False, budget_tokens=2000, lookup_id=None, ref=None):
    if type(full) is not bool:
        raise ValueError('full must be a boolean')
    id, revision, selected = _target(db, root, id, revision, ref, lookup_id)
    record = _record(db, id)
    if record is None:
        with lookup_budget(root, budget_tokens, lookup_id, id) as budget:
            return budget.finish(dict(status='not_found'))
    current, blocks = _index(root, record)
    with lookup_budget(root, budget_tokens, lookup_id, id) as budget:
        if revision is not None and revision != current:
            return budget.finish(dict(status='stale_revision', id=id, revision=current))
        _note_selected(root, id)
        if (block_id is not None or start_char is not None or end_char is not None) and revision is None:
            return budget.finish(dict(status='revision_required', id=id, revision=current))
        if block_id is not None:
            if start_char is not None or end_char is not None:
                raise ValueError('Choose a block_id or an explicit range')
            block = next((b for b in blocks if b['block_id'] == block_id), None)
            if block is None:
                return budget.finish(dict(status='unknown_block', id=id, revision=current))
            return _finish_evidence(budget, _range(budget, record, current, block['start_char'], block['end_char']), root, record)
        if end_char is not None:
            start = _int(start_char, 'start_char', maximum=len(record['text']))
            end = _int(end_char, 'end_char', minimum=start, maximum=len(record['text']))
            return _finish_evidence(budget, _range(budget, record, current, start, end), root, record)
        if start_char is not None:
            start = _int(start_char, 'start_char', maximum=len(record['text']))
            return budget.finish(_map(budget, root, record, current, blocks, start))
        whole = _range_payload(record, current, 0, len(record['text']))
        if budget.fits(whole):
            return _finish_evidence(budget, whole, root, record)
        if full:
            return budget.finish(_range(budget, record, current, 0, len(record['text'])))
        return budget.finish(_map(budget, root, record, current, blocks))


def peek_memory(db, root, id=None, revision=None, match_id=None, start_char=None, before=500,
                after=500, budget_tokens=2000, lookup_id=None, ref=None):
    _int(before, 'before', maximum=2000)
    _int(after, 'after', maximum=2000)
    id, revision, selected = _target(db, root, id, revision, ref, lookup_id)
    record = _record(db, id)
    blocks = _index(root, record)[1] if record is not None else []
    with lookup_budget(root, budget_tokens, lookup_id, id) as budget:
        if record is None:
            return budget.finish(dict(status='not_found'))
        current = source_revision(record['text'])
        if revision != current:
            return budget.finish(dict(status='stale_revision', id=id, revision=current))
        _note_selected(root, id)
        if match_id is not None:
            if start_char is not None:
                raise ValueError('Choose match_id or start_char')
            parsed = re.fullmatch(r'm(\d+):(\d+):([0-9a-f]{12})', match_id) if isinstance(match_id, str) else None
            if not parsed:
                raise ValueError('Invalid match_id')
            position, match_end = int(parsed[1]), int(parsed[2])
            if not 0 <= position < match_end <= len(record['text']) or parsed[3] != _key(current, position, match_end, '')[:12]:
                raise ValueError('match_id does not belong to this source revision')
        elif selected is not None and start_char is None:
            position, match_end = selected['start_char'], selected['end_char']
        else:
            position = _int(start_char, 'start_char', maximum=len(record['text']))
            match_end = position
        start, end = max(0, position-before), min(len(record['text']), match_end+after)
        # Peeks may shrink context, but boundaries and the shrink are explicit.
        result = _range_payload(record, current, start, end)
        result.update(kind='peek', requested_start=start, requested_end=end, context_reduced=False)
        block = next((b for b in blocks if b['start_char'] <= position < b['end_char']), None)
        if block:
            result.update(block_id=block['block_id'], block_start_char=block['start_char'],
                          block_end_char=block['end_char'], block_kind=block['kind'])
        while not budget.fits(result) and (start < position or end > match_end):
            left, right = position-start, end-match_end
            if left >= right and left:
                start += max(1, left // 4)
            elif right:
                end -= max(1, right // 4)
            result.update(start_char=start, end_char=end, text=record['text'][start:end], context_reduced=True)
        if not budget.fits(result):
            result = dict(status='budget_exhausted', next_action='Additional source retrieval requires a separately authorized lookup')
        return budget.finish(result)


_CORRECTION = re.compile(r'\b(?:correction|corrected|retracted|retraction)\b|\bI\s+was\s+wrong\b', re.I)
_SUPERSESSION = re.compile(r'\bsupersed(?:ed|es|ing)\b', re.I)


def _reported_marker(text):
    """A lexical navigation hint is not a determination that a claim is true."""
    for pattern, label in ((_SUPERSESSION, 'supersession_mention'), (_CORRECTION, 'correction_mention')):
        match = pattern.search(text)
        if match:
            return label, match.start(), match.end()
    return None, 0, 0


def timeline_memory(db, root, id=None, revision=None, ref=None, before=3, after=3,
                    budget_tokens=2000, lookup_id=None):
    """Adjacent imported messages in the same account and conversation only.

    Explicit source order takes precedence. Without it, record dates provide a
    navigation order and may be import dates. Later messages are not inherently
    more correct; correction/supersession flags only identify source mentions.
    """
    from import_metadata import _context
    from memory_budget import validate_budget
    _int(before, 'before', maximum=5)
    _int(after, 'after', maximum=5)
    validate_budget(budget_tokens)
    id, revision, selected = _target(db, root, id, revision, ref, lookup_id)
    anchor = _record(db, id)
    current = source_revision(anchor['text']) if anchor is not None else None
    rows, order_basis, unavailable = [], None, None
    if anchor is not None and (revision is None or revision == current):
        revisions, entities = _context(db, [anchor])
        entity = entities.get(id)
        if not entity or not entity.get('conversation_id'):
            unavailable = 'no_imported_conversation'
        else:
            known_order = entity.get('source_order') is not None
            order_basis = 'source_order' if known_order else 'record_date'
            # The canonical revision collapses repeated exports, but differing
            # message revisions remain separate evidence in chronological order.
            canonical_anchor = revisions[id]['canonical_memory_id']
            order = ('source_order,' if known_order else '') + \
                    "CASE WHEN julianday(created_at) IS NULL THEN 1 ELSE 0 END,julianday(created_at),created_at,id"
            query = '''WITH candidates AS (
                SELECT DISTINCT m.id,m.created_at,e.source_order,e.speaker
                FROM import_entities e JOIN import_revisions r ON r.entity_id=e.id
                JOIN memories m ON m.id=r.canonical_memory_id
                WHERE e.conversation_id=? AND e.account_id IS ? AND e.canonical_id IS NULL'''
            if known_order:
                query += ' AND e.source_order IS NOT NULL'
            query += '), ordered AS (SELECT *,row_number() OVER (ORDER BY '+order+') AS position FROM candidates), '
            query += 'anchor AS (SELECT position FROM ordered WHERE id=? LIMIT 1) '
            query += '''SELECT ordered.*,anchor.position AS anchor_position FROM ordered,anchor
                WHERE ordered.position BETWEEN anchor.position-? AND anchor.position+? ORDER BY ordered.position'''
            located = db.execute(query, (entity['conversation_id'], entity.get('account_id'),
                                         canonical_anchor, before, after)).fetchall()
            for item in located:
                record = anchor if item['id'] == canonical_anchor else _record(db, item['id'])
                if record is not None:
                    label, start, end = _reported_marker(record['text'])
                    if item['id'] == canonical_anchor and selected is not None:
                        start, end = selected['start_char'], selected['end_char']
                    rows.append(dict(record=record, revision=source_revision(record['text']),
                        position=item['position'], anchor_position=item['anchor_position'],
                        speaker=item['speaker'], source_order=item['source_order'],
                        label=label, start=start, end=end))
            if not rows:
                unavailable = 'conversation_anchor_unavailable'
    with lookup_budget(root, budget_tokens, lookup_id, id) as budget:
        if anchor is None:
            return budget.finish(dict(status='not_found'))
        if revision is not None and revision != current:
            return budget.finish(dict(status='stale_revision', id=id, revision=current))
        if unavailable:
            return budget.finish(dict(status='timeline_unavailable', reason=unavailable))
        _note_selected(root, id)
        anchor_row = next(row for row in rows if row['position'] == row['anchor_position'])
        anchor_ref = budget.reference(id, current, anchor_row['start'], anchor_row['end'])
        result = dict(status='ok', kind='timeline', anchor=anchor_ref, order_basis=order_basis,
                      items=[], omitted_items=0, labels='source_mentions_not_verified_truth')
        if order_basis == 'record_date':
            result['ordering_note'] = 'Record dates may be import dates'
        else:
            result['ordering_note'] = 'Neighbors with known source order; other messages may be unplaced'
        # Navigation must leave room for the evidence it points to. Measure the
        # actual transport charge, not just the inner JSON, for stage caps.
        stage_max = min(budget.capacity-budget.used, max(450, min(550,
            budget.capacity-budget.used-int(budget.capacity*.40))))
        dates = _navigation_dates(db, [row['record'] for row in rows])
        chosen = []
        # Reserve the anchor, then reported corrections and nearby messages.
        priority = sorted(rows, key=lambda row: (
            row['position'] != row['anchor_position'], row['label'] is None,
            abs(row['position']-row['anchor_position']), row['position'] < row['anchor_position']))
        for row in priority:
            item = _navigation_row(budget, row['record'], row['revision'], row['start'], row['end'],
                                   include_title=False, preview_characters=160, dates=dates)
            if row['position'] == row['anchor_position'] and selected is not None:
                # The search already displayed this anchor; do not buy it twice.
                item.pop('preview', None)
                item['anchor'] = True
            if row['label']:
                item['notice'] = row['label']
            if row['speaker']:
                item['speaker'] = str(row['speaker'])
            if row['source_order'] is not None:
                item['order'] = row['source_order']
            result['items'].append(item)
            if not _navigation_fits(budget, result, stage_max):
                result['items'].pop()
                result['omitted_items'] += 1
            else:
                chosen.append((row['position'], item))
        result['items'] = [item for _, item in sorted(chosen, key=lambda pair: pair[0])]
        if not result['items']:
            return budget.finish(dict(status='budget_exhausted',
                next_action='Use already returned evidence; a new lookup requires an explicit broader request'))
        budget.publish_reference(anchor_ref)
        _publish_rows(budget, result['items'])
        return budget.finish(result)


def _short_text(value, maximum=12, characters=64):
    """Short exact prefix; the enclosing response is always metered again."""
    value = str(value or '')[:characters]
    lo, hi = 0, len(value)
    while lo < hi:
        mid = (lo+hi+1)//2
        if token_count(value[:mid]) <= maximum:
            lo = mid
        else:
            hi = mid-1
    return value[:max(1, lo)] if value else ''


def _sentence_preview(text, position=0, maximum=240):
    """An exact source sentence/line around the match, never a generated summary.

    Very long sentences use explicitly marked, whole-word excerpts. The ref
    continues to anchor the actual match, independently of the preview start.
    """
    if not text:
        return ''
    position = min(max(0, position), len(text)-1)
    # Bound scanning even for huge logs; line boundaries suit code/config text.
    low, high = max(0, position-maximum), min(len(text), position+maximum)
    boundaries = list(re.finditer(r'(?<=[.!?])(?:["\u201d\u2019\')\]]*)\s+|\r?\n', text[low:high]))
    left = low
    right = high
    for boundary in boundaries:
        if low+boundary.end() <= position:
            left = low+boundary.end()
        elif low+boundary.start() >= position:
            right = low+boundary.start()+len(boundary.group().rstrip())
            break
    fragment = text[left:right].strip()
    if len(fragment) <= maximum and (left > low or low == 0) and (right < high or high == len(text)):
        return fragment
    start = max(left, position-maximum//3)
    if start and not text[start-1].isspace():
        boundary = re.search(r'\s+', text[start:min(right,start+maximum//3)])
        if boundary and start+boundary.end() <= position:
            start += boundary.end()
    end = min(right, start+maximum)
    if end < right and end > position:
        spaces = list(re.finditer(r'\s+',text[max(position+1,start):end]))
        if spaces:
            end = max(position+1,start)+spaces[-1].start()
    fragment = text[start:end].strip()
    return ('\u2026' if start > left or left == low > 0 else '') + fragment + ('\u2026' if end < right or right == high < len(text) else '')


def _content_anchor(text, start, end):
    """Move a heading-only hit to exact nearby source context, without rewriting.

    Explicit status/summary/problem lines are more useful than repeating the
    heading. Otherwise use the first nearby sentence, skipping title metadata.
    The returned ref anchors this excerpt; it never invents a keyword offset.
    """
    line_start = text.rfind('\n',0,start)+1
    line_end = text.find('\n',start)
    if line_end < 0:
        return start,end
    line = text[line_start:line_end].strip()
    if len(line)>240 or not (re.match(r'^#{1,6}\s',line) or line.isupper()):
        return start,end
    following = text[line_end+1:min(len(text),line_end+1600)]
    explicit = re.search(r'(?im)^[ \t]*(?:status|summary|purpose|problem|outcome|result)\s*:\s*\S',following)
    if explicit:
        position=line_end+1+explicit.start()
        word=re.search(r'\S+',text[position:])
        return position+word.start(),position+word.end()
    offset=line_end+1
    for candidate in following.splitlines(keepends=True):
        stripped=candidate.strip()
        if (len(stripped)>=40 and not stripped.isupper() and not stripped.startswith('#') and
                not re.match(r'(?i)^(requested pickup|prepared (?:for|by)|evidence collected|date\s*:|title\s*:)',stripped)):
            prefix=len(candidate)-len(candidate.lstrip())
            word=re.match(r'\S+',candidate[prefix:])
            return offset+prefix,offset+prefix+word.end()
        offset+=len(candidate)
    return start,end


_OUTCOME_QUERY = re.compile(
    r'\b(?:outcome|result|worked|fixed|resolved|confirmed|verified|tested|'
    r'succeed(?:ed)?|fail(?:ed)?|what happened|root cause|end up|'
    r'test\b.*\brun|run\b.*\btest)\b', re.I)
_REPORTED_OUTCOME = re.compile(
    r'\b(?:observed|confirmed|verified|passed|failed|worked|restored|persisted|'
    r'resolved|unresolved|unknown|untested|unconfirmed|inconclusive|'
    r'(?:not|never) (?:yet )?(?:established|confirmed|tested|run|fixed|resolved)|'
    r'(?:status|outcome|result)s?\s*:)', re.I)
_PASSAGE_LEADS = re.compile(r'(?:\A|(?<=[.!?])\s+|[\r\n]+)[ \t]*(\S+)')


def _select_passage(record, revision, query, hit, spans):
    """Choose a source anchor using the lexical windows and the verified semantic chunk."""
    from memory_search import _tokens, STOP_WORDS
    text = record['text']
    lexical = [(m['start_char'], m['end_char']) for m in spans]
    if lexical:
        first = lexical[0]
    else:
        hint = max(0, text.find(hit.get('text') or ''))
        first = (hint, hint)
    default = _content_anchor(text, *first)
    question = '?' in query or re.match(r'(?i)^\s*(how|why|what|when|which|did|does|is|was)\b', query)
    wants_outcome = bool(_OUTCOME_QUERY.search(query))
    literals = re.findall(r'[`"]([^`"\n]{1,160})[`"]', query)
    literals.extend(re.findall(r'\b[\w]+(?:[_.:/\\-][\w]+)+\b', query))
    literal = None
    if literals and not question:
        literal = re.compile(r'(?<!\w)(?:' + '|'.join(map(re.escape, literals)) + r')(?!\w)', re.I)
        for start, end in lexical:
            for found in literal.finditer(text, max(0, start-160), min(len(text), end+161)):
                if found.start() < end and start < found.end():
                    if found.start() <= default[0] < found.end():
                        return default
                    return found.span()
    if query.strip() == record['id'] or (default != first and not question and not wants_outcome):
        return default

    evidence = hit.get('_semantic_passage')
    if not isinstance(evidence, dict):
        return default
    left, right = evidence.get('start_char'), evidence.get('end_char')
    semantic = (evidence.get('revision') == revision and type(left) is int and
                type(right) is int and 0 <= left < right <= len(text) and right-left <= MAX_BLOCK)
    if not semantic:
        return default

    anchors = dict.fromkeys([default] + [_content_anchor(text, *pair) for pair in lexical])
    for match in _PASSAGE_LEADS.finditer(text[left:right]):
        start, stop = left + match.start(1), left + match.end(1)
        if start == left and start and not text[start-1].isspace():
            # Overlapping chunks sometimes begin halfway through a word.
            word = re.search(r'\s+(\S+)', text[stop:right])
            if word is None:
                continue
            start, stop = stop + word.start(1), stop + word.end(1)
        anchors.setdefault((start, stop), None)

    topic = set(_tokens(query)) - STOP_WORDS - {'happen','happened','actually','ever'}
    if wants_outcome:
        topic = {word for word in topic if not _OUTCOME_QUERY.fullmatch(word)}
    original_preview = _sentence_preview(text, default[0])
    original_is_question = original_preview.rstrip().endswith('?')
    def score(anchor):
        start, end = anchor
        preview = _sentence_preview(text, start)
        words = set(_tokens(preview))
        shared = len(topic & words)
        in_chunk = left <= start < right
        asks = preview.rstrip().endswith('?')
        heading = preview.startswith('#') or (preview.isupper() and ':' not in preview)
        answered = (wants_outcome and not asks and _REPORTED_OUTCOME.search(preview)
                    and (shared or not topic or original_is_question or anchor == default))
        relevance = 3 * shared / max(1, len(topic)) + int(in_chunk)
        if answered:
            relevance += 2
        if question and asks:
            relevance -= 2
        if question and heading:
            relevance -= 3
        exact = bool(literal and literal.search(preview))
        return exact, relevance
    chosen = max(anchors, key=score)
    if chosen == default:
        return default
    # A zero-context peek of a newly selected passage can return the same
    # source sentence the user saw. For an ellipsized preview keep the word anchor.
    position = chosen[0]
    preview = _sentence_preview(text, position)
    begin = text.rfind(preview, max(0, position-240), position+len(preview))
    if begin >= 0 and begin <= position < begin+len(preview):
        return begin, begin+len(preview)
    return chosen


def _navigation_fits(budget, result, maximum):
    return budget.fits(result) and transport_cost(budget.envelope(result)) <= maximum


def _navigation_dates(db, records):
    """Message dates come from message metadata, never chat titles/packet dates.

    created_at is commonly an import date, so label that fallback explicitly.
    Select scalar metadata only; navigation must not load whole source packets.
    """
    from import_metadata import _context
    def timestamp(value):
        if not isinstance(value, str) or len(value) > 40:
            return None
        try:
            parsed = datetime.fromisoformat(value)
            if len(value) == 10:
                return parsed.date().isoformat()
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc)
            return parsed.isoformat().replace('+00:00', 'Z')
        except (ValueError, OverflowError):
            return None

    result = {}
    for record in records:
        date = timestamp(record.get('created_at'))
        if date:
            result[record['id']] = {'record_date': date}
    revisions, _ = _context(db, records)
    if not revisions:
        return result
    canonical = {mid: rev['canonical_memory_id'] for mid, rev in revisions.items()}
    ids = list(dict.fromkeys(canonical.values()))
    marks = ','.join('?' for _ in ids)
    # DISTINCT keeps repeated exports cheap. Revisions remain separate sources.
    rows = db.execute('''SELECT DISTINCT r.canonical_memory_id,
        json_extract(i.metadata_json,'$.source_date') AS source_date
        FROM import_items i JOIN import_revisions r ON r.memory_id=i.memory_id
        WHERE r.canonical_memory_id IN ('''+marks+''')
        AND json_type(i.metadata_json,'$.source_date')='text' ''', ids)
    dates = {}
    for mid, value in rows:
        date = timestamp(value)
        if date:
            dates.setdefault(mid, set()).add(date)
    for mid, cid in canonical.items():
        values = dates.get(cid, set())
        if len(values) == 1:
            result[mid] = {'message_date': next(iter(values))}
        elif len(values) > 1:
            result.setdefault(mid, {})['message_date_status'] = 'conflicting_sources'
    return result


def _navigation_row(budget, record, revision, start=0, end=0, *, include_title=True,
                    preview_characters=240, dates=None):
    title = record.get('title') or record.get('source') or 'Memory'
    item = dict(ref=budget.reference(record['id'], revision, start, end),
        preview=_sentence_preview(record['text'], start, preview_characters))
    if include_title:
        item['title'] = str(title)
    if dates:
        item.update(dates.get(record['id'], {}))
    return item


def _publish_rows(budget, rows):
    for row in rows:
        budget.publish_reference(row['ref'])


def _handoff_windows(text, maximum=240):
    """Adjacent source lines (or pieces of long lines) that fit in one preview."""
    previous = None
    for line in re.finditer(r'\S[^\r\n]*', text):
        start, stop = line.span()
        while start < stop:
            end = min(start + maximum, stop)
            if end < stop and not text[end].isspace():
                spaces = list(re.finditer(r'\s+', text[start:end]))
                if spaces:
                    end = start + spaces[-1].start()
            if previous:
                left, right = previous
                same_paragraph = len(text[right:start].splitlines()) <= 1
                yield left, end if same_paragraph and end-left <= maximum else right
            previous = (start, end)
            start = end
            while start < stop and text[start].isspace():
                start += 1
    if previous:
        yield previous


def _handoff_passage(text, question):
    """Search only the named version. All returned offsets belong to the original."""
    from memory_search import _query_plan
    windows = _handoff_windows(text)
    first = next(windows, (0, 0))
    # For pure navigation, or no matching passage, show just the opening line.
    heading = re.search(r'[^\r\n]+', text[first[0]:first[1]])
    opening_end = first[0] + heading.end() if heading else first[0]
    fallback = (first[0], opening_end, 'metadata')
    _, expressions = _query_plan(question)
    if not expressions or first[0] == first[1]:
        return fallback

    # BM25 statistics are local to this document. This small query-only index
    # is discarded after each lookup and does not touch the archive.
    with closing(sqlite3.connect(':memory:')) as local:
        local.execute("CREATE VIRTUAL TABLE passage USING fts5(body, lo UNINDEXED, hi UNINDEXED, tokenize='porter unicode61')")
        local.executemany('INSERT INTO passage VALUES(?,?,?)',
            ((text[a:b], a, b) for a, b in chain((first,), windows)))
        for expression in expressions:
            row = local.execute('SELECT lo,hi FROM passage WHERE passage MATCH ? ORDER BY rank,rowid LIMIT 1',
                                (expression,)).fetchone()
            if row:
                return row[0], row[1], 'text'
    return fallback


def compact_search(db, root, query, limit=10, full=False, budget_tokens=2000):
    from memory_search import (lexical_search, search, STOP_WORDS, _query_plan,
                               rank_discovery_candidates, resolve_versioned_handoff)
    from import_metadata import compact_source_ids
    from memory_budget import validate_budget
    _int(limit, 'limit', minimum=1, maximum=MAX_HITS)
    validate_budget(budget_tokens)
    if type(full) is not bool:
        raise ValueError('full must be a boolean')
    query, _ = _query_plan(query)
    handoff = resolve_versioned_handoff(db, root, query)
    diagnostics = {}
    if handoff is not None:
        hits = ([dict(id=handoff['id'], discovery=dict(source_kind='saved_handoff'))]
                if handoff['id'] else [])
        diagnostics['semantic_state'] = 'not_requested'
    else:
        # Extra internal candidates let alias deduplication fill the requested page.
        candidates = min(100, max(40, limit*4))
        lexical = lexical_search(db, query, limit=candidates)
        try:
            hybrid = search(db, root, query, limit=candidates, full=False, diagnostics=diagnostics,
                            include_passages=True)
        except Exception:
            hybrid = lexical
            diagnostics['semantic_state'] = 'unavailable'
        words = {term for term in re.findall(r'[^\W_]+', _normalize(query)[0]) if term not in STOP_WORDS}
        strong = [hit for hit in lexical if hit['id'] == query.strip() or
                  (words and words <= set(re.findall(r'[^\W_]+', _normalize(hit.get('text',''))[0])))]
        # A literal mention cannot pin itself ahead of the saved artifact it merely names.
        hits = rank_discovery_candidates(db, query, strong[:1] + hybrid + lexical, root=root)
    records, seen, canonical_seen, groups = [], set(), set(), {}
    for hit in hits:
        if hit['id'] in seen:
            continue
        seen.add(hit['id'])
        record = _record(db, hit['id'])
        if record is not None:
            pointers = compact_source_ids(db, [dict(id=record['id'])])[0].get('source_ids')
            identity = (pointers or {}).get('canonical_source_id') or (pointers or {}).get('canonical_memory_id', record['id'])
            canonical = (identity, source_revision(record['text']))
            if canonical in canonical_seen:
                continue
            canonical_seen.add(canonical)
            info = hit.get('discovery', {})
            # Collapse verified name-only references and verbatim copies, never
            # distinct evidence just because it shares a generic chat title.
            conversation = (info.get('account_id'), info.get('conversation_id'))
            group_key = (info.get('group_key') if info.get('incidental_reference') else
                         ('copy', source_revision(record['text']),
                          conversation if conversation[1] else record['id'])
                         if len(record['text']) < 80 else ('copy', source_revision(record['text'])))
            if group_key in groups and record['id'] != query:
                group = groups[group_key]
                group['matches'] += 1
                if conversation[1]:
                    group['chats'].add(conversation)
                continue
            group = dict(matches=1, chats={conversation} if conversation[1] else set(),
                         kind='mentions' if info.get('incidental_reference') else 'copies')
            groups[group_key] = group
            records.append((record, hit, info, group))
    # Group before slicing so duplicate mentions cannot crowd out other sources.
    records = records[:limit]
    dates = _navigation_dates(db, [row[0] for row in records])
    indexed = []
    for record, hit, info, group in records:
        if handoff is not None:
            revision = source_revision(record['text'])
            start, end, match_kind = _handoff_passage(record['text'], handoff['question'])
            indexed.append((record, revision, start, end, match_kind, info, group))
            continue
        revision, blocks = _index(root, record)
        spans = _matched_spans(root, record, revision, query)
        metadata = ' '.join(str(record.get(k) or '') for k in ('id','title','source'))
        match_kind = 'text' if spans else ('metadata' if lexical_spans(metadata, query) else 'semantic')
        channels = set(hit.get('retrieval_channels') or [])
        if 'embedding' in channels:
            match_kind = 'hybrid' if 'lexical' in channels else 'semantic'
        start, end = _select_passage(record, revision, query, hit, spans)
        indexed.append((record, revision, start, end, match_kind, info, group))
    with lookup_budget(root, budget_tokens) as budget:
        result = dict(status='ok', kind='search', format='compact_refs', hits=[],
                      candidate_count=len(indexed), omitted_hits=0,
                      retrieval={'semantic': diagnostics.get('semantic_state','unavailable')})
        if diagnostics.get('semantic_reason'):
            result['retrieval']['reason'] = diagnostics['semantic_reason']
        if handoff is not None and not handoff['id']:
            result['handoff_status'] = 'version_not_found'
        if full:
            result.update(full_requested=True,
                          next_action='Fetch selected refs with this lookup_id for exact text')
        stage_max = min(budget.capacity-budget.used, max(400, int(budget.capacity*.48)))
        for record, revision, start, end, match_kind, info, group in indexed:
            item = _navigation_row(budget, record, revision, start, end, dates=dates)
            if handoff is not None:
                item['preview'] = record['text'][start:end]
            item['match_kind'] = match_kind
            source_kind = info.get('source_kind')
            if source_kind and source_kind != 'chat':
                item['source_kind'] = source_kind
            if group['matches'] > 1:
                item['group'] = dict(kind=group['kind'],matches=group['matches'])
                if group['chats']:
                    item['group']['chats'] = len(group['chats'])
            result['hits'].append(item)
            if not _navigation_fits(budget, result, stage_max):
                # Cold installations count each UTF-8 byte conservatively. Give
                # a first source ref room when its full title/preview won't fit.
                if len(result['hits']) == 1 and count_method() == 'json_utf8_byte_upper_bound':
                    first_max = max(stage_max, budget.capacity*3//5)
                    for width in (64,32,16):
                        item['preview'] = item['preview'][:width]
                        item['title'] = item['title'][:width]
                        if _navigation_fits(budget, result, first_max):
                            break
                    if _navigation_fits(budget, result, first_max):
                        continue
                result['hits'].pop()
                result['omitted_hits'] += 1
                continue
        _publish_rows(budget, result['hits'])
        return budget.finish(result)
