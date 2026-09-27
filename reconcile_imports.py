"""Conservative reconciliation of unidentified exact conversation excerpts."""
import json
def reconcile_anonymous(db, merge):
    packets=[(r['packet_key'],json.loads(r['metadata_json'])) for r in db.execute('SELECT packet_key,metadata_json FROM import_packets')]
    known=[(k,p) for k,p in packets if p['kind']=='excerpt' and p.get('conversation_id')]
    anonymous=[(k,p) for k,p in packets if p['kind']=='excerpt' and not p.get('conversation_id')]
    linked,ambiguous=0,0
    def entities(key):
        return [r[0] for r in db.execute('SELECT r.entity_id FROM import_items i JOIN import_revisions r ON r.memory_id=i.memory_id WHERE i.packet_key=? ORDER BY i.position',(key,))]
    def root(eid):
        e=db.execute('SELECT * FROM import_entities WHERE id=?',(eid,)).fetchone()
        while e['canonical_id']:e=db.execute('SELECT * FROM import_entities WHERE id=?',(e['canonical_id'],)).fetchone()
        return e
    for key,p in anonymous:
        ms=p['messages']
        if len(ms)<2 or sum(len(m['text']) for m in ms)<200 or not all(m.get('speaker') in ('user','assistant') for m in ms):continue
        old=[root(e) for e in entities(key)]
        if all(e['conversation_id'] for e in old):continue
        matches={}
        for kk,kp in known:
            if p.get('account_id') and p['account_id']!=kp.get('account_id'):continue
            kms=kp['messages']
            for start in range(len(kms)-len(ms)+1):
                subset=kms[start:start+len(ms)]
                if not all(a['text']==b['text'] and a['speaker']==b.get('speaker') and (not a.get('message_id') or a['message_id']==b.get('message_id')) for a,b in zip(ms,subset)):continue
                es=[root(e) for e in entities(kk)[start:start+len(ms)]]
                if not all(e['message_id'] for e in es):continue
                vector=tuple(e['id'] for e in es)
                matches[vector]=es
        if len(matches)!=1:
            if len(matches)>1:
                ambiguous+=1
                for vector in matches:
                    for pos,candidate in enumerate(vector):
                        db.execute('INSERT OR IGNORE INTO import_uncertain VALUES(?,?,?,?)',(key,pos,candidate,'Unidentified excerpt matches multiple source sequences; review required'))
            continue
        targets=next(iter(matches.values()))
        if len({e['id'] for e in old})!=len(old):continue
        if any(o['conversation_id'] and o['id']!=t['id'] for o,t in zip(old,targets)):continue
        for o,t in zip(old,targets):
            if o['id']!=t['id']:
                merge(db,o['id'],t['id']);linked+=1
    return {'linked_entities':linked,'ambiguous_excerpt_packets':ambiguous}
