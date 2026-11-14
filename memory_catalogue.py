"""Persistent, source-linked navigation aids built by the existing Qwen worker.

No model calls or archive writes occur in readers. Generated descriptions are
interpretations, never proof. Evidence labels describe statements in a quote;
they do not certify that a test ran or that a purported correction is correct.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

VERSION = 'catalogue-v2'
CONVERSATION_VERSION = 'conversation-v1'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS catalogue (
 memory_id TEXT PRIMARY KEY, revision TEXT NOT NULL, source_fingerprint TEXT NOT NULL,
 analysis_version TEXT NOT NULL, catalogue_version TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS catalogue_dirty (memory_id TEXT PRIMARY KEY);
CREATE INDEX IF NOT EXISTS records_conversation ON records(json_extract(source_ids,'$.conversation_id'));
CREATE TABLE IF NOT EXISTS catalogue_members (
 memory_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, revision TEXT NOT NULL,
 source_fingerprint TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS catalogue_members_conversation ON catalogue_members(conversation_id);
CREATE TABLE IF NOT EXISTS catalogue_conversations (
 conversation_id TEXT PRIMARY KEY, analysis_version TEXT NOT NULL,
 catalogue_version TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS catalogue_conversation_dirty (conversation_id TEXT PRIMARY KEY);
'''


def text_revision(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def invalidate_catalogue(cache, memory_id):
    previous=cache.execute('SELECT conversation_id FROM catalogue_members WHERE memory_id=?',(memory_id,)).fetchone()
    if previous:cache.execute('INSERT OR IGNORE INTO catalogue_conversation_dirty VALUES(?)',(previous[0],))
    cache.execute('DELETE FROM catalogue_members WHERE memory_id=?',(memory_id,))
    cache.execute('DELETE FROM catalogue WHERE memory_id=?',(memory_id,))
    cache.execute('DELETE FROM catalogue_dirty WHERE memory_id=?',(memory_id,))


def queue_catalogue(cache, memory_id):
    cache.execute('INSERT OR IGNORE INTO catalogue_dirty VALUES(?)',(memory_id,))


def _short(value, limit):
    return value[:limit] if isinstance(value,str) else ''


def _title(analysis):
    title=analysis.get('suggested_title')
    if isinstance(title,str) and title.strip():return title.strip()[:120]
    # Reuse earlier validated analyses without re-inferencing the entire archive.
    summary=analysis.get('summary','')
    return re.split(r'(?<=[.!?])\s|[\r\n]',summary,maxsplit=1)[0][:120]


def _evidence_label(quote, kind):
    """Conservative textual cues; absent/negated/ambiguous claims stay unknown."""
    lower=quote.casefold()
    # Never promote a quote containing a qualification into successful-test proof.
    qualified=('?' in quote or kind=='question' or bool(re.search(
        r"\b(?:not|never|no|failed|fails|failure|unverified|untested|maybe|might|could|if|would|should|will|plan|planned|proposed|says?|prints?|example|expected|hypothetical)\b|n't\b",lower)))
    if not qualified and re.search(r'\b(?:tests? passed|all tests pass|verified working|tested successfully|test succeeded|confirmed working)\b',lower):
        return 'tested_as_reported'
    if not qualified and re.search(r'\b(?:correction:|corrected (?:the|my|that|this)|previous (?:answer|claim|statement) was (?:wrong|incorrect))',lower):
        return 'corrected_as_reported'
    if not qualified and re.search(r'\b(?:superseded|replaced by|obsolete|deprecated)\b',lower):
        return 'superseded_as_reported'
    if kind=='proposed':return 'proposed'
    return 'unknown'


def _outcome_role(quote, kind, label):
    """Index explicit source language, never manufacture missing outcome stages."""
    lower=quote.casefold()
    if '?' in quote or kind=='question':return None
    if re.search(r'\b(?:if|would|might|could|maybe|hypothetical|example)\b',lower):return None
    if label in ('tested_as_reported','corrected_as_reported','superseded_as_reported'):
        return 'result'
    if re.search(r'\b(?:tests? (?:failed|fails)|result(?:s)? (?:was|were|showed)|observed (?:that|the))\b',lower):
        return 'result'
    if kind=='decided' and re.search(r'\b(?:decided to|approved (?:the|this)|chose to|agreed to)\b',lower):
        if not re.search(r"\b(?:not|never|no)\b|n't\b",lower):return 'decision'
    if re.search(r'\b(?:tried|attempted|attempting)\b',lower) or kind=='proposed':return 'attempt'
    if re.search(r'\b(?:problem|error|exception|crash|failure|fails|broken)\b',lower):return 'problem'
    return None


def build_catalogue(cache, source, memory_id, analysis_version):
    """Revalidate a limited spread of chunk analyses against one current record."""
    from ai_memory import iter_source_records, source_fingerprint, validate_analysis
    original=next(iter_source_records(source,memory_id=memory_id),None)
    saved=cache.execute('SELECT fingerprint,source_ids FROM records WHERE id=?',(memory_id,)).fetchone()
    if original is None or saved is None or source_fingerprint(original)!=saved['fingerprint']:
        return None
    text=original['text'];revision=text_revision(text)
    counts=cache.execute('SELECT count(*) AS total,sum(analysis_version=?) AS ready FROM chunks WHERE memory_id=?',
                         (analysis_version,memory_id)).fetchone()
    # Include beginning and ending evidence instead of hiding late corrections in
    # a huge record. Coverage remains explicit; this is never an exhaustive view.
    rows=cache.execute('''SELECT * FROM chunks WHERE id IN (
        SELECT id FROM (SELECT id FROM chunks WHERE memory_id=? AND analysis_version=? ORDER BY part LIMIT 8)
        UNION SELECT id FROM (SELECT id FROM chunks WHERE memory_id=? AND analysis_version=? ORDER BY part DESC LIMIT 24))
        ORDER BY part''',(memory_id,analysis_version,memory_id,analysis_version)).fetchall()
    descriptions=[];topics=[];evidence=[];seen=set();valid=0;outcomes={}
    for row in rows:
        start,end=row['start'],row['end']
        if type(start) is not int or type(end) is not int or not 0<=start<end<=len(text):continue
        if text[start:end]!=row['text']:continue
        try:analysis=validate_analysis(json.loads(row['analysis']),dict(row))
        except (ValueError,TypeError,KeyError,RecursionError):continue
        valid+=1
        descriptions.append(dict(chunk_id=row['id'],start_char=start,end_char=end,
                                 suggested_title=_title(analysis),summary=analysis['summary'][:300]))
        # Source-derived function/error names supplement generated keywords.
        names=re.findall(r'(?m)^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)|\b([A-Za-z_]\w*(?:Error|Exception))\b',row['text'])
        for value in [*analysis['keywords'],*(a or b for a,b in names)]:
            value=value.strip()[:60]
            if value and value.casefold() not in {topic.casefold() for topic in topics} and len(topics)<8:topics.append(value)
        for fact in analysis['facts']:
            quote=fact['quote'];relative=row['text'].find(quote)
            if relative<0:continue
            offset=start+relative
            if text[offset:offset+len(quote)]!=quote:continue
            key=(offset,offset+len(quote))
            if key in seen:continue
            seen.add(key)
            item=dict(label=_evidence_label(quote,fact['kind']),quote=quote,
                start_char=offset,end_char=offset+len(quote),chunk_id=row['id'])
            evidence.append(item)
            role=_outcome_role(quote,fact['kind'],item['label'])
            if role:outcomes[role]=dict(item)  # Latest sampled quote per role, not a verdict.
    if not descriptions:return None
    # Surface explicit correction/test claims with their quotes while preserving
    # uncertainty. Readers must not treat this ordering as a truth score.
    order={'corrected_as_reported':0,'superseded_as_reported':1,'tested_as_reported':2,'proposed':3,'unknown':4}
    evidence.sort(key=lambda item:(order[item['label']],-item['start_char']))
    selected=evidence[:4]
    description=descriptions[0]
    source_ids=json.loads(saved['source_ids'])
    incomplete=cache.execute('SELECT 1 FROM indexing WHERE id=?',(memory_id,)).fetchone() is not None
    return dict(memory_id=memory_id,revision=revision,offset_unit='unicode_code_points',
        interpretation=True,action_authority=False,suggested_title=description['suggested_title'],
        summary=description['summary'],summary_source=dict(chunk_id=description['chunk_id'],
            start_char=description['start_char'],end_char=description['end_char']),
        topics=topics,evidence=selected,conversation_id=_conversation_id(source_ids.get('conversation_id')),
        outcome_card=dict(stages=outcomes,interpretation=True,partial=True,
            scope='Selected source claims from this record; missing stages are unknown, not absent.'),
        coverage=dict(indexed_chunks=counts['total'],analyzed_chunks=counts['ready'] or 0,
            sampled_chunks=valid,indexing_complete=not incomplete,
            partial=incomplete or valid<counts['total'] or len(evidence)>len(selected)),
        evidence_policy='Labels describe source claims, not verified correctness; inspect originals and conflicting evidence.')


def _conversation_id(value):
    # IDs must remain exact; truncation could join unrelated conversations.
    return value if isinstance(value,str) and 0<len(value)<=512 else None


def _member(cache, memory_id, payload, fingerprint):
    previous=cache.execute('SELECT conversation_id FROM catalogue_members WHERE memory_id=?',(memory_id,)).fetchone()
    conversation_id=_conversation_id(payload.get('conversation_id'))
    if previous and previous[0]!=conversation_id:
        cache.execute('INSERT OR IGNORE INTO catalogue_conversation_dirty VALUES(?)',(previous[0],))
    cache.execute('DELETE FROM catalogue_members WHERE memory_id=?',(memory_id,))
    if conversation_id:
        cache.execute('INSERT INTO catalogue_members VALUES(?,?,?,?)',
                      (memory_id,conversation_id,payload['revision'],fingerprint))
        cache.execute('INSERT OR IGNORE INTO catalogue_conversation_dirty VALUES(?)',(conversation_id,))


def refresh_catalogues(cache, source, analysis_version, *, limit=16, deadline=None, should_continue=None):
    """Bounded incremental backfill/rebuild. No inference or additional worker."""
    if type(limit) is not int or not 1<=limit<=64:raise ValueError('Invalid catalogue batch limit')
    candidates=cache.execute('''SELECT r.id FROM records r LEFT JOIN catalogue d ON d.memory_id=r.id
        LEFT JOIN catalogue_dirty q ON q.memory_id=r.id
        WHERE (q.memory_id IS NOT NULL OR d.memory_id IS NULL OR d.catalogue_version!=? OR d.analysis_version!=?)
        AND EXISTS(SELECT 1 FROM chunks c WHERE c.memory_id=r.id AND c.analysis_version=?)
        ORDER BY q.memory_id IS NULL,r.id LIMIT ?''',(VERSION,analysis_version,analysis_version,limit)).fetchall()
    updated=0
    for item in candidates:
        if deadline is not None and time.monotonic()>=deadline:break
        if should_continue is not None and not should_continue():break
        payload=build_catalogue(cache,source,item['id'],analysis_version)
        with cache:
            if payload is None:
                invalidate_catalogue(cache,item['id']);continue
            fingerprint=cache.execute('SELECT fingerprint FROM records WHERE id=?',(item['id'],)).fetchone()[0]
            encoded=json.dumps(payload,ensure_ascii=False,separators=(',',':'),sort_keys=True)
            cache.execute('''INSERT INTO catalogue VALUES(?,?,?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET
                revision=excluded.revision,source_fingerprint=excluded.source_fingerprint,
                analysis_version=excluded.analysis_version,catalogue_version=excluded.catalogue_version,payload=excluded.payload
                WHERE catalogue.payload!=excluded.payload OR catalogue.analysis_version!=excluded.analysis_version
                    OR catalogue.catalogue_version!=excluded.catalogue_version''',
                (item['id'],payload['revision'],fingerprint,analysis_version,VERSION,encoded))
            cache.execute('DELETE FROM catalogue_dirty WHERE memory_id=?',(item['id'],))
            _member(cache,item['id'],payload,fingerprint)
        updated+=1
    return updated


def refresh_conversations(cache, source, analysis_version, *, limit=8, deadline=None, should_continue=None):
    """Group existing descriptions; never ask a model to reread whole chats."""
    if type(limit) is not int or not 1<=limit<=32:raise ValueError('Invalid conversation batch limit')
    # Incremental compatibility backfill for catalogues built before grouping.
    missing=cache.execute('''SELECT d.memory_id,d.payload,d.source_fingerprint FROM catalogue d
        LEFT JOIN catalogue_members m ON m.memory_id=d.memory_id
        WHERE m.memory_id IS NULL AND d.analysis_version=?
        AND json_extract(d.payload,'$.conversation_id') IS NOT NULL LIMIT 32''',(analysis_version,)).fetchall()
    with cache:
        for row in missing:
            if deadline is not None and time.monotonic()>=deadline:break
            _member(cache,row['memory_id'],json.loads(row['payload']),row['source_fingerprint'])
        cache.execute('''INSERT OR IGNORE INTO catalogue_conversation_dirty
            SELECT conversation_id FROM catalogue_conversations WHERE analysis_version!=? OR catalogue_version!=?''',
            (analysis_version,CONVERSATION_VERSION))
    groups=cache.execute('SELECT conversation_id FROM catalogue_conversation_dirty ORDER BY conversation_id LIMIT ?',
                         (limit,)).fetchall()
    has_imports=source.execute("SELECT 1 FROM sqlite_master WHERE name='import_revisions'").fetchone()
    updated=0
    for group in groups:
        if deadline is not None and time.monotonic()>=deadline:break
        if should_continue is not None and not should_continue():break
        cid=group[0]
        rows=cache.execute('''SELECT d.memory_id,d.revision,d.source_fingerprint,d.payload
            FROM catalogue d JOIN catalogue_members m ON m.memory_id=d.memory_id
            JOIN records r ON r.id=d.memory_id AND r.fingerprint=d.source_fingerprint
            WHERE m.conversation_id=? AND d.analysis_version=? AND d.catalogue_version=?
            ORDER BY d.memory_id LIMIT 12''',(cid,analysis_version,VERSION)).fetchall()
        if not rows:
            with cache:
                cache.execute('DELETE FROM catalogue_conversations WHERE conversation_id=?',(cid,))
                cache.execute('DELETE FROM catalogue_conversation_dirty WHERE conversation_id=?',(cid,))
            continue
        total_indexed=cache.execute("SELECT count(*) FROM records WHERE json_extract(source_ids,'$.conversation_id')=?",(cid,)).fetchone()[0]
        total_catalogued=cache.execute('''SELECT count(*) FROM catalogue d JOIN catalogue_members m ON m.memory_id=d.memory_id
            JOIN records r ON r.id=d.memory_id AND r.fingerprint=d.source_fingerprint
            WHERE m.conversation_id=? AND d.analysis_version=? AND d.catalogue_version=?''',(cid,analysis_version,VERSION)).fetchone()[0]
        total_archive=source.execute('''SELECT count(DISTINCT m.id) FROM memories m
            JOIN import_revisions r ON r.memory_id=m.id JOIN import_entities e ON e.id=r.entity_id
            WHERE e.conversation_id=? AND m.id IN(SELECT id FROM memory_fts)''',(cid,)).fetchone()[0] if has_imports else total_indexed
        entries=[(row,json.loads(row['payload'])) for row in rows]
        # Select useful, distinct descriptions without inferring a new global claim.
        chosen=[];seen=set()
        for row,item in entries:
            text=item.get('summary','')
            if text and text not in seen:
                chosen.append((row,item));seen.add(text)
            if len(chosen)>=3:break
        if not chosen:chosen=entries[:1]
        summaries=[];members=[];fingerprints={}
        for row,item in chosen:
            members.append(dict(memory_id=row['memory_id'],revision=row['revision']))
            fingerprints[row['memory_id']]=row['source_fingerprint']
            summaries.append(dict(text=item.get('summary','')[:200],memory_id=row['memory_id'],revision=row['revision'],
                                  source=item.get('summary_source',{})))
        payload=dict(conversation_id=cid,suggested_title=chosen[0][1].get('suggested_title','')[:120],
            summaries=summaries,members=members,interpretation=True,action_authority=False,
            coverage=dict(archive_records=total_archive,indexed_records=total_indexed,
                catalogued_records=total_catalogued,represented_records=len(members),
                partial=len(members)<total_archive or any(item.get('coverage',{}).get('partial',True) for _,item in chosen)),
            scope='Selected source-linked descriptions; not an exhaustive summary or correctness verdict.',
            _source_fingerprints=fingerprints)
        encoded=json.dumps(payload,ensure_ascii=False,separators=(',',':'),sort_keys=True)
        with cache:
            cache.execute('''INSERT INTO catalogue_conversations VALUES(?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET
                analysis_version=excluded.analysis_version,catalogue_version=excluded.catalogue_version,payload=excluded.payload
                WHERE catalogue_conversations.payload!=excluded.payload OR catalogue_conversations.analysis_version!=excluded.analysis_version
                    OR catalogue_conversations.catalogue_version!=excluded.catalogue_version''',
                (cid,analysis_version,CONVERSATION_VERSION,encoded))
            cache.execute('DELETE FROM catalogue_conversation_dirty WHERE conversation_id=?',(cid,))
        updated+=1
    return updated


def conversation_for_record(root, memory_id, revision):
    """Read a bounded chat map and recheck its few cited originals, never all chats."""
    from ai_memory import archive_connect, iter_source_records, source_fingerprint, load_config, config_fingerprint
    if not isinstance(revision,str) or not re.fullmatch(r'[0-9a-f]{64}',revision):return None
    try:
        config=load_config(root)
        if not config['enabled']:return None
        path=Path(root)/'.ai-cache'/'index.sqlite3'
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=0)) as cache:
            cache.execute('PRAGMA query_only=ON')
            row=cache.execute('''SELECT c.payload,m.source_fingerprint FROM catalogue_conversations c
                JOIN catalogue_members m ON m.conversation_id=c.conversation_id
                WHERE m.memory_id=? AND m.revision=? AND c.analysis_version=? AND c.catalogue_version=?''',
                (memory_id,revision,config_fingerprint(config),CONVERSATION_VERSION)).fetchone()
            if row is None or len(row[0].encode('utf-8'))>16384:return None
            payload=json.loads(row[0]);fingerprints=payload.pop('_source_fingerprints',{})
            if not isinstance(fingerprints,dict) or len(fingerprints)>3:return None
            fingerprints[memory_id]=row[1]
            with closing(archive_connect(root)) as source:
                source.execute('BEGIN')
                for mid,fingerprint in fingerprints.items():
                    original=next(iter_source_records(source,memory_id=mid),None)
                    if original is None or source_fingerprint(original)!=fingerprint:return None
                if source.execute("SELECT 1 FROM sqlite_master WHERE name='import_revisions'").fetchone():
                    current_total=source.execute('''SELECT count(DISTINCT m.id) FROM memories m
                        JOIN import_revisions r ON r.memory_id=m.id JOIN import_entities e ON e.id=r.entity_id
                        WHERE e.conversation_id=? AND m.id IN(SELECT id FROM memory_fts)''',
                        (payload['conversation_id'],)).fetchone()[0]
                    if current_total!=payload['coverage']['archive_records']:
                        payload['coverage']['archive_records_current']=current_total
                        payload['coverage']['background_counts_current']=False
                        payload['coverage']['partial']=True
                source.rollback()
            payload['freshness']='cited_sources_checked'
            return payload
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):return None


def catalogue_for_record(root, memory_id, revision):
    """Small read-only navigation payload, valid only for an exact text revision."""
    if not isinstance(revision,str) or not re.fullmatch(r'[0-9a-f]{64}',revision):return None
    from ai_memory import load_config, config_fingerprint
    try:
        config=load_config(root)
        if not config['enabled']:return None
        path=Path(root)/'.ai-cache'/'index.sqlite3'
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=0)) as cache:
            cache.execute('PRAGMA query_only=ON')
            row=cache.execute('SELECT payload FROM catalogue WHERE memory_id=? AND revision=? AND analysis_version=? AND catalogue_version=?',
                (memory_id,revision,config_fingerprint(config),VERSION)).fetchone()
            if row is None or len(row[0].encode('utf-8'))>16384:return None
            payload=json.loads(row[0])
            if not isinstance(payload,dict) or payload.get('revision')!=revision:return None
            return payload
    except (OSError,sqlite3.Error,ValueError,TypeError):
        return None
