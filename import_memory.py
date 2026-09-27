"""Local, transactional imports. Input content is data; no commands are evaluated."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from uuid import uuid4
import zipfile

ROOT = Path(__file__).resolve().parent
KINDS = {'excerpt', 'summary', 'note'}

def is_temporary_file(path):
    """Reserved direct-drop names are never importer inputs, even explicitly."""
    name = Path(path).name.lower()
    return name.startswith('.incoming-') or name.endswith(('.part', '.tmp'))

def now():
    return datetime.now(timezone.utc).isoformat()

def digest(data):
    return hashlib.sha256(data).hexdigest()

def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

SCHEMA = '''
CREATE TABLE import_schema(version INTEGER PRIMARY KEY CHECK(version=1));
INSERT INTO import_schema VALUES(1);
CREATE TABLE import_objects(sha256 TEXT PRIMARY KEY, raw BLOB NOT NULL, imported_at TEXT NOT NULL);
CREATE TABLE import_receipts(object_sha TEXT NOT NULL REFERENCES import_objects(sha256), path TEXT NOT NULL, options_json TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, PRIMARY KEY(object_sha,path,options_json));
CREATE TABLE import_packets(packet_key TEXT PRIMARY KEY, metadata_json TEXT NOT NULL, imported_at TEXT NOT NULL);
CREATE TABLE import_packet_sources(packet_key TEXT NOT NULL REFERENCES import_packets(packet_key), object_sha TEXT NOT NULL REFERENCES import_objects(sha256), PRIMARY KEY(packet_key,object_sha));
CREATE TABLE import_entities(id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('excerpt','summary','note')), account_id TEXT, conversation_id TEXT, message_id TEXT, speaker TEXT, source_order INTEGER, canonical_id TEXT REFERENCES import_entities(id));
CREATE UNIQUE INDEX import_identity ON import_entities(kind,ifnull(account_id,''),conversation_id,message_id) WHERE conversation_id IS NOT NULL AND message_id IS NOT NULL AND canonical_id IS NULL;
CREATE TABLE import_revisions(memory_id TEXT PRIMARY KEY REFERENCES memories(id), entity_id TEXT NOT NULL REFERENCES import_entities(id), text_sha TEXT NOT NULL, canonical_memory_id TEXT NOT NULL REFERENCES memories(id), UNIQUE(entity_id,text_sha));
CREATE TABLE import_items(packet_key TEXT NOT NULL REFERENCES import_packets(packet_key), position INTEGER NOT NULL, memory_id TEXT NOT NULL REFERENCES memories(id), metadata_json TEXT NOT NULL, PRIMARY KEY(packet_key,position));
CREATE TABLE import_uncertain(packet_key TEXT NOT NULL, position INTEGER NOT NULL, candidate_id TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(packet_key,position,candidate_id,reason));
CREATE TABLE import_warnings(object_sha TEXT NOT NULL REFERENCES import_objects(sha256), warning_json TEXT NOT NULL, PRIMARY KEY(object_sha,warning_json));
'''

def migrate(db_path):
    db_path = Path(db_path).resolve()
    with closing(sqlite3.connect(db_path)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='import_schema'").fetchone():
            if db.execute('SELECT version FROM import_schema').fetchall() != [(1,)]:
                raise ValueError('Unsupported importer schema version')
            return None
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='memories'").fetchone():
            raise ValueError('Expected the existing ThreadSatchel database')
        backups = db_path.parent / 'backups'
        backups.mkdir(exist_ok=True)
        backup = backups / ('memory-before-import-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.sqlite3')
        with closing(sqlite3.connect(backup)) as dest:
            db.backup(dest)
            if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Backup integrity check failed')
        db.execute('BEGIN IMMEDIATE')
        try:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='import_schema'").fetchone():
                for statement in SCHEMA.split(';'):
                    if statement.strip():
                        db.execute(statement)
            db.commit()
        except Exception:
            db.rollback()
            raise
        return str(backup)

def validate_packet(p):
    if not isinstance(p, dict):
        raise ValueError('Packet must be an object')
    allowed = {'format', 'kind', 'title', 'account_id', 'conversation_id', 'source_url', 'source_date', 'derived_from', 'messages'}
    if set(p) - allowed:
        raise ValueError('Unknown packet fields: ' + ', '.join(sorted(set(p)-allowed)))
    if p.get('format', 'threadsatchel/1') not in ('threadsatchel/1', 'steve-memory/1'):
        raise ValueError('Expected format threadsatchel/1')
    # Keep the legacy canonical marker stable so existing packet hashes still deduplicate.
    p = dict(p, format='steve-memory/1', kind=p.get('kind', 'excerpt'))
    if p['kind'] not in KINDS:
        raise ValueError('kind must be excerpt, summary, or note')
    for k in allowed - {'messages', 'derived_from'}:
        if k in p and p[k] is not None and (not isinstance(p[k], str) or not p[k].strip()):
            raise ValueError(k + ' must be a nonempty string or null')
    if 'derived_from' in p and not isinstance(p['derived_from'], list):
        raise ValueError('derived_from must be a list of source references')
    messages = p.get('messages')
    if not isinstance(messages, list) or not messages:
        raise ValueError('messages must be a nonempty list')
    seen_ids, seen_orders = set(), set()
    for m in messages:
        if not isinstance(m, dict) or set(m) - {'text', 'speaker', 'message_id', 'source_order', 'source_date', 'source_url'}:
            raise ValueError('Invalid message fields')
        if not isinstance(m.get('text'), str) or not m['text'].strip():
            raise ValueError('Every message needs nonempty text')
        for k in ('speaker', 'message_id', 'source_date', 'source_url'):
            if m.get(k) is not None and (not isinstance(m[k], str) or not m[k].strip()):
                raise ValueError(k + ' must be a nonempty string or null')
        order = m.get('source_order')
        if order is not None and (type(order) is not int or order < 0):
            raise ValueError('source_order must be a known nonnegative integer, or omitted')
        for value, seen, label in ((m.get('message_id'), seen_ids, 'message_id'), (order, seen_orders, 'source_order')):
            if value is not None:
                if value in seen:
                    raise ValueError('Duplicate ' + label + ' in packet; split revisions into separate packets')
                seen.add(value)
    return p

def source_time(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    if isinstance(value, str):
        return value
    raise ValueError('Unsupported export timestamp')

def export_packets(raw, account_id=None):
    """Known conversations.json graph shape; real-user ZIP validation is pending."""
    warnings = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = [n for n in archive.namelist() if n.replace('\\', '/').split('/')[-1] == 'conversations.json']
        if len(names) != 1:
            raise ValueError('ZIP must contain exactly one conversations.json')
        conversations = json.loads(archive.read(names[0]).decode('utf-8-sig'))
    if not isinstance(conversations, list):
        raise ValueError('Expected conversations.json list')
    packets = []
    for c in conversations:
        mapping = c.get('mapping')
        if not isinstance(mapping, dict):
            raise ValueError('Unsupported export: missing mapping')
        conv = c.get('conversation_id') or c.get('id')
        base = dict(format='threadsatchel/1', kind='excerpt', title=c.get('title'), account_id=account_id, conversation_id=conv, source_date=source_time(c.get('create_time')))
        # Preserve graph order of the current branch; other nodes remain separate.
        branch, visited = [], set()
        node_id = c.get('current_node')
        while node_id is not None:
            if node_id in visited or node_id not in mapping:
                raise ValueError('Export graph has a cycle or missing parent')
            visited.add(node_id)
            branch.append(node_id)
            node_id = mapping[node_id].get('parent')
        branch.reverse()
        def read_message(key):
            msg = mapping[key].get('message')
            if not msg:
                return None
            content = msg.get('content') or {}
            parts = content.get('parts', [])
            if content.get('content_type') != 'text' or not isinstance(parts, list) or not all(isinstance(x, str) for x in parts):
                warnings.append({'conversation_id': conv, 'node_id': key, 'reason': 'Non-text/unsupported content retained in original ZIP only'})
                return None
            # A multipart text message has no specified separator. Do not fabricate one.
            if len(parts) != 1:
                warnings.append({'conversation_id': conv, 'node_id': key, 'reason': 'Multipart text retained in ZIP; separator not assumed'})
                return None
            if not parts[0].strip():
                return None
            return dict(text=parts[0], speaker=(msg.get('author') or {}).get('role'), message_id=msg.get('id'), source_date=source_time(msg.get('create_time')))
        messages = []
        for key in branch:
            msg = read_message(key)
            if msg:
                messages.append(msg)
            elif messages:
                # Never assert adjacency across unsupported/empty graph nodes.
                packets.append(validate_packet(dict(base, messages=messages)))
                messages = []
        if messages:
            packets.append(validate_packet(dict(base, messages=messages)))
        for key in mapping:
            if key not in visited:
                msg = read_message(key)
                if msg:
                    packets.append(validate_packet(dict(base, messages=[msg])))
    return packets, warnings

def parse(path, raw, options):
    suffix = path.suffix.lower()
    if suffix == '.zip':
        return export_packets(raw, options.get('account_id'))
    if suffix == '.json':
        return [validate_packet(json.loads(raw.decode('utf-8-sig')))], []
    if suffix not in {'.txt', '.md', '.markdown'}:
        raise ValueError('Supported files: .txt, .md, .markdown, .json, .zip')
    p = {k: v for k, v in options.items() if v is not None and k not in {'speaker', 'message_id', 'source_order'}}
    p['kind'] = p.get('kind', 'note')
    p['messages'] = [dict(text=raw.decode('utf-8-sig'), **{k: options[k] for k in ('speaker', 'message_id', 'source_order') if options.get(k) is not None})]
    return [validate_packet(p)], []

def compatible(old, new):
    return old['text'] == new['text'] and (not old.get('speaker') or not new.get('speaker') or old['speaker'] == new['speaker']) and (not old.get('message_id') or not new.get('message_id') or old['message_id'] == new['message_id'])

def candidates(db, p):
    """Only reconcile excerpts inside an explicitly identified conversation/account scope."""
    matches, uncertain = {}, []
    if p['kind'] != 'excerpt' or not p.get('conversation_id'):
        return matches, uncertain
    old_packets = db.execute('SELECT packet_key,metadata_json FROM import_packets').fetchall()
    for packet in old_packets:
        old = json.loads(packet['metadata_json'])
        if old['kind'] != 'excerpt' or old.get('account_id') != p.get('account_id') or old.get('conversation_id') != p['conversation_id']:
            continue
        items = db.execute('''SELECT i.position,r.entity_id,e.* FROM import_items i JOIN import_revisions r ON r.memory_id=i.memory_id JOIN import_entities e ON e.id=r.entity_id WHERE i.packet_key=? ORDER BY i.position''', (packet['packet_key'],)).fetchall()
        def root(e):
            while e['canonical_id']:
                e = db.execute('SELECT * FROM import_entities WHERE id=?', (e['canonical_id'],)).fetchone()
            return e['id']
        oldms, newms = old['messages'], p['messages']
        verified = set()
        # Explicit source order is stronger than matching local list indexes.
        for j, n in enumerate(newms):
            for i, o in enumerate(oldms):
                if compatible(o, n) and o.get('source_order') is not None and o.get('source_order') == n.get('source_order'):
                    matches.setdefault(j, set()).add(root(items[i]))
                    verified.add((i,j))
        # Match the entire shorter contiguous excerpt only when it occurs once,
        # has >=2 messages, and there is no contradictory source order.
        short, long, old_short = (oldms, newms, True) if len(oldms) <= len(newms) else (newms, oldms, False)
        offsets = [start for start in range(len(long)-len(short)+1) if all(compatible(s, long[start+k]) and (s.get('source_order') is None or long[start+k].get('source_order') is None or s['source_order']==long[start+k]['source_order']) for k,s in enumerate(short))]
        if len(short) >= 2 and len(offsets) == 1:
            for k in range(len(short)):
                i,j = (k,offsets[0]+k) if old_short else (offsets[0]+k,k)
                matches.setdefault(j,set()).add(root(items[i]))
                verified.add((i,j))
        for j,n in enumerate(newms):
            for i,o in enumerate(oldms):
                if compatible(o,n) and (i,j) not in verified:
                    uncertain.append((j,root(items[i]),'Exact text without verified unique order; retained separately'))
    # One old message cannot stand for two incoming messages.
    counts = {}
    for ids in matches.values():
        for entity in ids:
            counts[entity] = counts.get(entity,0)+1
    for j in list(matches):
        bad = {e for e in matches[j] if counts[e] > 1}
        for e in bad:
            uncertain.append((j,e,'Repeated phrase/order has multiple possible positions'))
        matches[j] -= bad
    return matches, uncertain

def merge_entity(db, old_id, target_id):
    if old_id == target_id:
        return
    db.execute('UPDATE import_entities SET canonical_id=? WHERE id=?', (target_id,old_id))
    for r in db.execute('SELECT * FROM import_revisions WHERE entity_id=?', (old_id,)).fetchall():
        same = db.execute('SELECT * FROM import_revisions WHERE entity_id=? AND text_sha=?', (target_id,r['text_sha'])).fetchone()
        if same:
            canonical = same['canonical_memory_id']
            db.execute('UPDATE import_revisions SET canonical_memory_id=? WHERE canonical_memory_id=?', (canonical,r['memory_id']))
            db.execute('DELETE FROM memory_fts WHERE id=?', (r['memory_id'],))
        else:
            db.execute('UPDATE import_revisions SET entity_id=? WHERE memory_id=?', (target_id,r['memory_id']))

def import_file(path, db_path=ROOT/'memory.sqlite3', **options):
    if is_temporary_file(path):
        raise ValueError('Temporary input is not finalized; rename it before importing')
    path = Path(path).resolve()
    if is_temporary_file(path):
        raise ValueError('Temporary input is not finalized; rename it before importing')
    raw = path.read_bytes()
    packets, warnings = parse(path, raw, options)
    backup = migrate(db_path)
    result = dict(file=str(path), sha256=digest(raw), backup=backup, added_revisions=0, reused_messages=0, linked_entities=0, repeated_packets=0, uncertain=[], warnings=warnings)
    with closing(sqlite3.connect(db_path, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        try:
            stamp, sha = now(), digest(raw)
            db.execute('INSERT OR IGNORE INTO import_objects VALUES(?,?,?)', (sha,raw,stamp))
            for warning in warnings:
                db.execute('INSERT OR IGNORE INTO import_warnings VALUES(?,?)', (sha,encoded(warning).decode()))
            db.execute('INSERT INTO import_receipts VALUES(?,?,?,?,?) ON CONFLICT(object_sha,path,options_json) DO UPDATE SET last_seen=excluded.last_seen', (sha,str(path),encoded(options).decode(),stamp,stamp))
            for p in packets:
                key = digest(encoded(p))
                if db.execute('SELECT 1 FROM import_packets WHERE packet_key=?', (key,)).fetchone():
                    db.execute('INSERT OR IGNORE INTO import_packet_sources VALUES(?,?)', (key,sha))
                    result['repeated_packets'] += 1
                    result['uncertain'].extend(dict(row) for row in db.execute('SELECT * FROM import_uncertain WHERE packet_key=?',(key,)))
                    continue
                matches, uncertain = candidates(db,p)
                db.execute('INSERT INTO import_packets VALUES(?,?,?)', (key,encoded(p).decode(),stamp))
                db.execute('INSERT INTO import_packet_sources VALUES(?,?)', (key,sha))
                for pos,m in enumerate(p['messages']):
                    identity = None
                    if p.get('conversation_id') and m.get('message_id'):
                        identity = db.execute('SELECT * FROM import_entities WHERE kind=? AND ifnull(account_id,\'\')=? AND conversation_id=? AND message_id=? AND canonical_id IS NULL', (p['kind'],p.get('account_id') or '',p['conversation_id'],m['message_id'])).fetchone()
                    ids = matches.get(pos,set())
                    # Never coalesce conflicting known message IDs on text evidence.
                    eligible = []
                    for eid in ids:
                        e = db.execute('SELECT * FROM import_entities WHERE id=?',(eid,)).fetchone()
                        if e['message_id'] and m.get('message_id') and m['message_id'] != e['message_id']:
                            uncertain.append((pos,eid,'Known message identity not confirmed by incoming message'))
                        else:
                            eligible.append(e)
                    if len(eligible)>1:
                        uncertain.extend((pos,e['id'],'Multiple candidate source records; retained separately') for e in eligible)
                        eligible=[]
                    entity = identity or (eligible[0] if eligible else None)
                    if entity:
                        eid = entity['id']
                        for e in eligible:
                            if e['id'] != eid:
                                merge_entity(db,e['id'],eid)
                                result['linked_entities'] += 1
                        db.execute('UPDATE import_entities SET message_id=coalesce(message_id,?),speaker=coalesce(speaker,?),source_order=coalesce(source_order,?) WHERE id=?', (m.get('message_id'),m.get('speaker'),m.get('source_order'),eid))
                    else:
                        eid = str(uuid4())
                        db.execute('INSERT INTO import_entities VALUES(?,?,?,?,?,?,?,NULL)', (eid,p['kind'],p.get('account_id'),p.get('conversation_id'),m.get('message_id'),m.get('speaker'),m.get('source_order')))
                    text_sha = digest(m['text'].encode('utf-8'))
                    revision = db.execute('SELECT * FROM import_revisions WHERE entity_id=? AND text_sha=?',(eid,text_sha)).fetchone()
                    if revision:
                        memory_id = revision['canonical_memory_id']
                        result['reused_messages'] += 1
                    else:
                        memory_id = str(uuid4())
                        source = p['kind'] + ' | ' + (m.get('source_url') or p.get('source_url') or p.get('conversation_id') or 'manual import')
                        title = p.get('title')
                        db.execute('INSERT INTO memories VALUES(?,?,?,?,?)',(memory_id,m['text'],source,title,stamp))
                        db.execute('INSERT INTO memory_fts(id,text,source,title) VALUES(?,?,?,?)',(memory_id,m['text'],source,title))
                        db.execute('INSERT INTO import_revisions VALUES(?,?,?,?)',(memory_id,eid,text_sha,memory_id))
                        result['added_revisions'] += 1
                    db.execute('INSERT INTO import_items VALUES(?,?,?,?)',(key,pos,memory_id,encoded(m).decode()))
                    for j,candidate,reason in uncertain:
                        if j == pos and candidate != eid:
                            db.execute('INSERT OR IGNORE INTO import_uncertain VALUES(?,?,?,?)',(key,j,candidate,reason))
                            result['uncertain'].append(dict(position=j,candidate_id=candidate,reason=reason))
            if path.suffix.lower() == '.zip' and result['added_revisions']:
                from reconcile_imports import reconcile_anonymous
                reconciled = reconcile_anonymous(db, merge_entity)
                result['linked_entities'] += reconciled['linked_entities']
                result['ambiguous_excerpt_packets'] = reconciled['ambiguous_excerpt_packets']
            db.commit()
        except Exception:
            db.rollback()
            raise
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='*', help='Files or directories; default: project inbox (nonrecursive)')
    parser.add_argument('--db', type=Path, default=ROOT/'memory.sqlite3')
    parser.add_argument('--init', action='store_true', help='Back up/migrate only')
    for key in ('kind','title','account-id','conversation-id','message-id','source-url','source-date','speaker'):
        parser.add_argument('--'+key, choices=sorted(KINDS) if key=='kind' else None)
    parser.add_argument('--source-order', type=int)
    args = vars(parser.parse_args())
    paths, db_path, init = args.pop('paths'),args.pop('db'),args.pop('init')
    options = {k:v for k,v in args.items() if v is not None}
    if init:
        print(json.dumps({'backup':migrate(db_path)},indent=2))
        return
    inputs = []
    for value in paths or [str(ROOT/'inbox')]:
        p = Path(value)
        inputs.extend(sorted(x for x in p.iterdir() if x.is_file() and not is_temporary_file(x) and x.suffix.lower() in {'.txt','.md','.markdown','.json','.zip'}) if p.is_dir() else [p])
    failed = False
    for path in inputs:
        try:
            print(json.dumps(import_file(path,db_path,**options),indent=2,ensure_ascii=False))
        except Exception as exc:
            failed=True
            print(json.dumps({'file':str(path),'error':str(exc)},ensure_ascii=False))
    raise SystemExit(1 if failed else 0)

if __name__ == '__main__':
    main()
