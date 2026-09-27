"""Optional read-only metadata for imported records; legacy records stay unchanged."""
import json

def enrich(db, row):
    record = dict(row)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='import_revisions'").fetchone():
        return record
    revision = db.execute('SELECT * FROM import_revisions WHERE memory_id=?', (record['id'],)).fetchone()
    if revision is None:
        return record
    entity = db.execute('SELECT * FROM import_entities WHERE id=?', (revision['entity_id'],)).fetchone()
    while entity['canonical_id']:
        entity = db.execute('SELECT * FROM import_entities WHERE id=?', (entity['canonical_id'],)).fetchone()
    provenance = []
    for item in db.execute('''SELECT i.position,i.metadata_json,p.metadata_json AS packet,s.object_sha,o.imported_at
        FROM import_items i JOIN import_revisions r ON r.memory_id=i.memory_id
        JOIN import_packets p ON p.packet_key=i.packet_key
        JOIN import_packet_sources s ON s.packet_key=p.packet_key
        JOIN import_objects o ON o.sha256=s.object_sha
        WHERE r.canonical_memory_id=?''', (revision['canonical_memory_id'],)):
        packet = json.loads(item['packet'])
        packet.pop('messages',None)
        provenance.append(dict(sha256=item['object_sha'], imported_at=item['imported_at'], packet_position=item['position'], packet=packet, message=json.loads(item['metadata_json']), paths=[r[0] for r in db.execute('SELECT DISTINCT path FROM import_receipts WHERE object_sha=?',(item['object_sha'],))]))
    record['import_metadata'] = dict(kind=entity['kind'], canonical_source_id=entity['id'], canonical_memory_id=revision['canonical_memory_id'], account_id=entity['account_id'], conversation_id=entity['conversation_id'], message_id=entity['message_id'], speaker=entity['speaker'], source_order=entity['source_order'], provenance=provenance, revision_ids=[r[0] for r in db.execute('SELECT memory_id FROM import_revisions WHERE entity_id=?',(entity['id'],))])
    return record
