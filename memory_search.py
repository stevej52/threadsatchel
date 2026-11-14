"""Bounded local lexical retrieval with optional, failure-tolerant AI enhancement."""
from pathlib import Path
import hashlib
import json
import re
import unicodedata

from import_metadata import compact_source_ids, enrich_many, _context

MAX_RESULTS = 100
MAX_TEXT = 600
MAX_QUERY = 2048
MAX_TERMS = 32
STOP_WORDS = frozenset('''a an and are as at be been but by can could did do does
    for from had has have how i in is it its me my of on or our please show tell
    than that the their them then there these they this to us was we were what
    when where which who why will with would you your find search
    remember about'''.split())
_ARTIFACT_INTENT = frozenset('handoff handoffs note notes saved save document documents file files '
                            'latest recent current summary summaries instructions'.split())
_REFERENCE_WORDS = frozenset('save saved open read load retrieve find see pickup file files document '
    'documents handoff handoffs note notes please available ready here there attached again start '
    'continue session project named called version link use access located stored reference references '
    'that this it its the for in on from to and of a an my our your is was has have been now'.split())
_SUBSTANTIVE = re.compile(r'```|\b(?:wrong|incorrect|correct(?:ed|ion)?|supersed\w*|retract\w*|'
    r'fail\w*|error\w*|test\w*|result\w*|worked|works|broken|fix\w*|changed|updated|instead|'
    r'not|never|must|should|because|measured|voltage|threshold|deadband)\b', re.I)
_HANDOFF_TITLE = re.compile(r'^Handoff\s*\|\s*([^|]+?)\s*\|\s*(v[1-9][0-9]*)\s*$', re.I)


def _tokens(value):
    normalized = ''.join(char for char in unicodedata.normalize('NFD', str(value or '').casefold())
                         if unicodedata.category(char) != 'Mn')
    return re.findall(r'[^\W_]+', normalized)


def _group_key(kind, *identity):
    raw = json.dumps([kind, *identity], ensure_ascii=False, separators=(',', ':')).encode()
    return kind + ':' + hashlib.sha256(raw).hexdigest()[:20]


def _table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _saved_note_candidates(db, query, maximum=20):
    """Find relevant note sources even when many chat hits fill the main pool."""
    clauses = []
    if _table_exists(db, 'import_revisions') and _table_exists(db, 'import_entities'):
        clauses.append("EXISTS(SELECT 1 FROM import_revisions r JOIN import_entities e ON e.id=r.entity_id "
                       "WHERE r.memory_id=m.id AND e.kind='note')")
    if _table_exists(db, 'memory_write_receipts'):
        clauses.append('EXISTS(SELECT 1 FROM memory_write_receipts w WHERE w.memory_id=m.id)')
    if not clauses:
        return []
    _, expressions = _query_plan(query)
    result, seen = [], set()
    for expression in expressions:
        rows = db.execute('''SELECT m.id,snippet(memory_fts,1,'','',' … ',64) AS text,
            substr(m.source,1,241) AS source,substr(m.title,1,241) AS title,m.created_at
            FROM memory_fts JOIN memories m ON m.id=memory_fts.id WHERE memory_fts MATCH ? AND ('''
            + ' OR '.join(clauses) + ') ORDER BY bm25(memory_fts),m.created_at DESC,m.id LIMIT ?',
            (expression, maximum))
        for row in rows:
            if row['id'] in seen:
                continue
            seen.add(row['id'])
            value = dict(row)
            value['text'] = _bounded(value['text'], MAX_TEXT)
            value['title'] = _bounded(value['title'], 240)
            value['source'] = _bounded(value['source'], 240)
            value['retrieval_channels'] = ['lexical']
            result.append(value)
            if len(result) >= maximum:
                return result
    return result


def _plugin_saves(db, ids, *, max_rows=None):
    saved = {}
    if not all(_table_exists(db, name) for name in ('import_items','import_packet_sources','import_receipts')):
        return saved
    marks = ','.join('?' for _ in ids)
    sql = '''SELECT DISTINCT i.memory_id,r.path FROM import_items i
        JOIN import_packet_sources s ON s.packet_key=i.packet_key
        JOIN import_receipts r ON r.object_sha=s.object_sha
        WHERE i.memory_id IN (''' + marks + ") AND r.options_json='{}' AND r.path LIKE '%plugin-%.json'"
    parameters = list(ids)
    if max_rows is not None:
        sql += ' LIMIT ?'
        parameters.append(max_rows+1)
    for index, row in enumerate(db.execute(sql, parameters)):
        if max_rows is not None and index >= max_rows:
            return None
        name = str(row['path']).replace('\\','/').rsplit('/',1)[-1]
        match = re.fullmatch(r'plugin-([0-9a-f]{64})\.json', name, re.I)
        if match:
            saved.setdefault(row['memory_id'], set()).add(match[1].casefold())
    return saved


def _verified_handoff(root, title, request_ids):
    match = _HANDOFF_TITLE.fullmatch(str(title or ''))
    if root is None or not match or not request_ids:
        return None
    name, version = match[1], match[2]
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}', name) or '..' in name
            or name.endswith(('.', ' '))):
        return None
    try:
        folder = (Path(root) / 'handoffs').resolve()
        target = (folder / name / version).resolve()
        if not target.is_relative_to(folder):
            return None
        manifest = target / 'plugin-manifest.json'
        if not manifest.is_file() or manifest.is_symlink() or manifest.stat().st_size > 65536:
            return None
        data = json.loads(manifest.read_text(encoding='utf-8'))
        if (data.get('format') == 'threadsatchel-handoff/1' and data.get('name') == name
                and data.get('version') == version and data.get('request_id') in request_ids
                and (target / 'instructions.md').is_file()):
            return dict(name=name, version=version)
    except (OSError, ValueError, TypeError):
        pass
    return None


_VERSION_MENTION = re.compile(r'(?<![^\W_])(?:v|version\s+)([0-9]+)(?![^\W_])', re.I)
_COMPARE = re.compile(r'\b(?:compar(?:e|es|ed|ing|ison)|versus|vs|between|differences?)\b', re.I)
_REFERENCE_SEARCH = re.compile(r'\b(?:chats?|conversations?|messages?|references?|mentions?)\s+'
                               r'(?:about|on|to|of|mentioning|discussing)\b', re.I)
_NAVIGATION_WORDS = set('a an the in of from for at on to my your please me show find open get '
                        'load retrieve read locate pull up saved handoff named called file document'.split())
_NAME_PREFIXES = _NAVIGATION_WORDS | set('about using with within under does did is was says '
                                         'review summarize summarise consult explain'.split())
_LABEL_EDGE = ' \t\n\r"`\'“”‘’|,:;()[]'


def _versioned_name_question(query, marker, name):
    """Require all name tokens contiguous and adjacent to the explicit version."""
    words = _tokens(name)
    if not words:
        return None
    before = list(re.finditer(r'[^\W_]+', query[:marker.start()]))
    if before and before[-1][0].casefold() == 'handoff' and words[-1] != 'handoff':
        before.pop()
    if len(before) >= len(words) and [m[0].casefold() for m in before[-len(words):]] == words:
        start, end = before[-len(words)].start(), marker.end()
        # "Unknown Project v2" must not silently resolve a saved "Project v2".
        prefix = query[:start].rstrip()
        previous = _tokens(prefix)
        if previous and previous[-1] not in _NAME_PREFIXES and prefix[-1] not in ':,("\'“‘`':
            return None
    else:
        # Also accept "version 2 of the named handoff".
        suffix = query[marker.end():]
        after = list(re.finditer(r'[^\W_]+', suffix))
        if not after or after[0][0].casefold() != 'of':
            return None
        after = after[1:]
        while after and after[0][0].casefold() in {'the','saved','handoff','named'} and after[0][0].casefold() != words[0]:
            after = after[1:]
        if len(after) < len(words) or [m[0].casefold() for m in after[:len(words)]] != words:
            return None
        start, end = marker.start(), marker.end() + after[len(words)-1].end()
        remainder = query[end:]
        if remainder.strip() and remainder.lstrip()[0] not in '?,:;.!\'")]`”’':
            return None
    left, right = query[:start].strip(_LABEL_EDGE), query[end:].strip(_LABEL_EDGE)
    if _REFERENCE_SEARCH.search(left):
        return None
    if set(_tokens(left)) <= _NAVIGATION_WORDS:
        left = ''
    else:
        left = re.sub(r'(?i)\s+(?:in|from|of|about|for|with|using)(?: the)?$', '', left)
    question = left + right if right == '?' else ' '.join(part for part in (left, right) if part)
    return '' if set(_tokens(question)) <= _NAVIGATION_WORDS else question


def resolve_versioned_handoff(db, root, query):
    """Resolve one explicit verified name/version, independently of result rank.

    None preserves ordinary discovery. id=None means the name is verified but
    the requested version has no matching archived handoff title.
    """
    if not isinstance(query, str) or len(query) > MAX_QUERY or _COMPARE.search(query):
        return None
    mentions = list(_VERSION_MENTION.finditer(query))
    if len(mentions) != 1:
        return None
    marker = mentions[0]
    if not re.fullmatch(r'[1-9][0-9]{0,8}', marker[1]) or re.match(r'[._-][\w]', query[marker.end():]):
        return None
    if re.match(r'\s*(?:[/+&,]|or\b|and\b|through\b|to\b)\s*(?:[0-9]+|newer|later)\b',
                query[marker.end():], re.I):
        return None
    version = 'v' + marker[1]
    before = _tokens(query[:marker.start()])
    after = _tokens(query[marker.end():])
    anchors = before[-2:-1] if before[-1:] == ['handoff'] and len(before)>1 else before[-1:]
    if after[:1] == ['of']:
        anchors += [word for word in after[1:7] if word not in _NAVIGATION_WORDS][:1]
    if not anchors or not all(_table_exists(db, name) for name in ('memory_fts','import_revisions','import_entities')):
        return None
    # FTS restricts the lookup to title tokens. Fail closed if the cap could
    # conceal another normalized name. Read no memory body or user-supplied path.
    terms = ' OR '.join('"'+word+'"' for word in dict.fromkeys(anchors))
    rows = db.execute('''SELECT m.id,m.title,r.canonical_memory_id FROM memory_fts
        JOIN memories m ON m.id=memory_fts.id JOIN import_revisions r ON r.memory_id=m.id
        JOIN import_entities e ON e.id=r.entity_id
        WHERE memory_fts MATCH ? AND e.kind='note' AND length(m.title)<=128
        LIMIT 129''', ('title : ("handoff" AND ('+terms+'))',)).fetchall()
    if len(rows) > 128:
        return None
    candidates = []
    for row in rows:
        match = _HANDOFF_TITLE.fullmatch(row['title'] or '')
        if match:
            question = _versioned_name_question(query, marker, match[1])
            if question is not None:
                candidates.append((row, match[2].casefold(), question))
    if not candidates:
        return None
    ids = {ident for row, _, _ in candidates for ident in (row['id'],row['canonical_memory_id'])}
    receipts = _plugin_saves(db, sorted(ids), max_rows=256)
    if receipts is None:
        return None
    names, selected = {}, set()
    for row, saved_version, question in candidates:
        keys = receipts.get(row['id'], set()) | receipts.get(row['canonical_memory_id'], set())
        artifact = _verified_handoff(root, row['title'], keys)
        if artifact:
            names[artifact['name'].casefold()] = (artifact['name'],question)
            if saved_version == version:
                selected.add(row['canonical_memory_id'])
    if len(names) != 1 or len(selected) > 1:
        return None
    if not selected and any(saved_version == version for _, saved_version, _ in candidates):
        return None  # An archived row exists but its provenance could not be verified.
    name, question = next(iter(names.values()))
    if not selected:
        # The name came from a verified manifest and version is a validated vN.
        # Publication may precede indexing; even an incomplete directory means
        # the archive cannot conclusively answer that this version is missing.
        try:
            (Path(root) / 'handoffs' / name / version).lstat()
        except FileNotFoundError:
            pass
        except OSError:
            return None
        else:
            return None
    return dict(id=next(iter(selected),None), name=name, version=version, question=question)


def _artifact_relevance(query, title, body):
    terms = set(_tokens(query)) - STOP_WORDS
    subject = terms - _ARTIFACT_INTENT
    title_terms = set(_tokens(title))
    body_words = _tokens(body)
    if not subject:
        return 'source_type' if terms & _ARTIFACT_INTENT else None
    if subject <= title_terms:
        return 'title'
    # An isolated mention inside an unrelated note is insufficient. A complete
    # multi-term body match, repeated single term, or untitled concise note is
    # still useful source evidence when a saved document has a generic title.
    if subject <= set(body_words):
        generic_title = not (title_terms - _ARTIFACT_INTENT - {'memory','untitled'})
        if len(subject) > 1 or generic_title or all(body_words.count(term) >= 2 for term in subject):
            return 'body'
    return None


def _keyword_preference(query, title, body):
    """A small rank adjustment for direct subject evidence, not a new gate.

    Generic artifact words do not count. Multiword queries need more than one
    matching subject word, except a named identifier such as LABNODE or AX123.
    Complete subject coverage can move a result past a nearby partial match.
    The adjustment stays below three positions in the incoming relevance order.
    """
    subject = set(_tokens(query)) - STOP_WORDS - _ARTIFACT_INTENT
    if not subject:
        return 0.0
    title_matches = subject & set(_tokens(title))
    matched = subject & (set(_tokens(body)) | title_matches)
    if not matched:
        return 0.0
    if len(subject) == 1:
        return 1.25 if title_matches else 1.0
    identifiers = {term.casefold() for term in re.findall(r'[^\W_]+', query)
                   if len(term) >= 3 and (term.isupper() or any(char.isdigit() for char in term))}
    if len(matched) < 2:
        return 1.0 if matched & identifiers else 0.0
    if subject <= matched:
        return 2.75 if title_matches else 2.5
    return min(2.0, .5 + 1.25*len(matched)/len(subject) + (.25 if title_matches else 0))


def _reference_only(record, artifact):
    text = record['_profile_text']
    if record['_text_length'] > 400 or '\x00' in text or _SUBSTANTIVE.search(text):
        return False
    words = _tokens(text)
    subject = set(_tokens(artifact['name'])) - STOP_WORDS - _ARTIFACT_INTENT
    if not subject or not subject <= set(words):
        return False
    versions = {word for word in words if re.fullmatch(r'v[1-9][0-9]*', word)}
    if versions and versions != {artifact['version'].casefold()}:
        return False
    allowed = subject | _REFERENCE_WORDS | STOP_WORDS | versions
    return bool(set(words) & {'handoff','handoffs','saved','pickup','file','document'}) and set(words) <= allowed


def _navigation_notice(record):
    """Recognize complete UI notices, never a matching fragment of a message."""
    if record['_text_length'] > 400 or '\x00' in record['_profile_text']:
        return None
    text = re.sub(r'[*`_]', '', record['_profile_text']).strip()
    match = re.fullmatch(r'Did you mean\s*:?\s*(.{1,120}?)\?\s*', text, re.I)
    if match:
        return 'clarification', match[1].strip()
    match = re.fullmatch(r'(?:Claude|Codex|ChatGPT|Gemini|Copilot|Grok|Qwen)\s*(?:→|->)\s*(.+?)\s*·[^\r\n]*'
                         r'\b(?:new|existing)\s+conversation[.!]?'
                         r'(?:\s+It finishes its current reply in [^\r\n.!?]{1,100} first\.)?', text, re.I)
    if match:
        return 'conversation_opened', match[1].strip()
    return None


def rank_discovery_candidates(db, query, hits, root=None):
    """Prefer relevant saved artifacts; attach conservative navigation groups.

    Grouping is source identity, not a claim that different passages agree.
    Original hits and retrieval channels are retained; exact IDs always win.
    """
    query, _ = _query_plan(query)
    incoming, seen = [], {}
    for hit in list(hits) + _saved_note_candidates(db, query):
        if not isinstance(hit, dict) or not isinstance(hit.get('id'), str):
            continue
        if hit['id'] in seen:
            first = seen[hit['id']]
            channels = set(first.get('retrieval_channels') or []) | set(hit.get('retrieval_channels') or [])
            if channels:
                first['retrieval_channels'] = [channel for channel in ('lexical','embedding','analysis') if channel in channels]
            if '_semantic_passage' in hit and '_semantic_passage' not in first:
                first['_semantic_passage'] = hit['_semantic_passage']
            continue
        value = dict(hit)
        seen[hit['id']] = value
        incoming.append(value)
        if len(incoming) >= 200:
            break
    if not incoming:
        return []
    ids = [hit['id'] for hit in incoming]
    marks = ','.join('?' for _ in ids)
    records = {row['id']: dict(row) for row in db.execute('''SELECT id,title,source,
        substr(text,1,801) AS _profile_text,length(text) AS _text_length FROM memories WHERE id IN ('''+marks+')', ids)}
    revisions, entities = _context(db, list(records.values()))
    canonical_ids = list(dict.fromkeys(ids + [row['canonical_memory_id'] for row in revisions.values()]))
    saves = _plugin_saves(db, canonical_ids)
    direct = set()
    if _table_exists(db, 'memory_write_receipts'):
        direct = {row[0] for row in db.execute('SELECT memory_id FROM memory_write_receipts WHERE memory_id IN ('+marks+')',ids)}
    if root is None:
        path = db.execute('PRAGMA database_list').fetchone()[2]
        root = Path(path).parent if path else None
    decorated, artifacts = [], []
    for position, hit in enumerate(incoming):
        record = records.get(hit['id'])
        if record is None:
            continue
        entity = entities.get(hit['id'], {})
        revision = revisions.get(hit['id'], {})
        requests = saves.get(hit['id'], set()) | saves.get(revision.get('canonical_memory_id'), set())
        kind = entity.get('kind')
        saved = bool(requests) or hit['id'] in direct
        artifact = _verified_handoff(root, record['title'], requests) if kind == 'note' else None
        source_kind = ('saved_handoff' if artifact else 'saved_note' if saved and (kind == 'note' or hit['id'] in direct)
                       else 'note' if kind == 'note' else 'chat' if kind == 'excerpt'
                       else 'summary' if kind == 'summary' else 'memory')
        metadata = dict(source_kind=source_kind, subject_title=artifact['name'] if artifact else record['title'])
        for field in ('speaker','conversation_id','account_id'):
            if entity.get(field) is not None:
                metadata[field] = entity[field]
        if artifact:
            metadata.update(artifact_name=artifact['name'], artifact_version=artifact['version'])
            metadata['group_key'] = _group_key('handoff', artifact['name'].casefold(), artifact['version'])
        elif kind == 'excerpt' and entity.get('conversation_id'):
            metadata['group_key'] = _group_key('chat', entity.get('account_id'), entity['conversation_id'])
        else:
            metadata['group_key'] = _group_key('source', hit['id'])
        relevance = _artifact_relevance(query, record['title'], hit.get('text',''))
        preference = {'saved_handoff':3,'saved_note':2,'note':1}.get(source_kind,0) if relevance else 0
        if preference:
            metadata['preference_basis'] = relevance
        notice = _navigation_notice(record) if source_kind == 'chat' else None
        if notice:
            event, subject = notice
            metadata.update(source_kind='navigation_notice', navigation_notice=True,
                incidental_reference=True, notice_event=event, subject_title=subject,
                group_key=_group_key('notice', event, _tokens(subject), entity.get('account_id')))
        hit['discovery'] = metadata
        decorated.append((hit, record, preference, position))
        if artifact and preference:
            artifacts.append(artifact)
    for hit, record, _, _ in decorated:
        if hit['discovery']['source_kind'] != 'chat':
            continue
        matches = [artifact for artifact in artifacts if _reference_only(record, artifact)]
        names = {artifact['name'].casefold() for artifact in matches}
        if len(names) == 1:
            artifact = matches[0]
            versions = sorted({word for word in _tokens(record['_profile_text']) if re.fullmatch(r'v[1-9][0-9]*',word)})
            hit['discovery'].update(group_key=_group_key('mentions', next(iter(names)), versions,
                                                       hit['discovery'].get('account_id')),
                incidental_reference=True, referenced_artifact=artifact['name'])
    def rank_key(item):
        hit, record, preference, position = item
        keyword = _keyword_preference(query, record['title'], hit.get('text',''))
        return (hit['id'] != query, bool(hit['discovery'].get('navigation_notice')), -preference,
                position-keyword, -keyword, position, hit['id'])
    decorated.sort(key=rank_key)
    return [item[0] for item in decorated]


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
            record['retrieval_channels'] = ['lexical']
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


def search(db, root, query, limit=20, full=False, diagnostics=None, *, include_passages=False):
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
                extra = {'diagnostics': diagnostics} if diagnostics is not None else {}
                if include_passages and not full:
                    extra['include_passages'] = True
                enhanced = enhance_search(Path(root), db, query, hits, limit,
                                          release_snapshot=owns_snapshot, **extra)
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
        if diagnostics is None and not include_passages:
            # Keep the original Python API shape. The bounded discovery API
            # requests diagnostics and uses retrieval channels internally.
            return [{k:v for k,v in hit.items() if k != 'retrieval_channels'} for hit in hits]
        return hits
    finally:
        if owns_snapshot and db.in_transaction:
            db.rollback()


def compact_search(db, root, query, limit=10, full=False, budget_tokens=2000):
    """Public bounded navigation API; internal search preserves AI compatibility."""
    from memory_retrieval import compact_search as retrieve
    return retrieve(db, root, query, limit=limit, full=full, budget_tokens=budget_tokens)
