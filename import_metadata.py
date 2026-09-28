"""Read-only imported metadata, loaded once per result batch."""
from collections import defaultdict
import json


def _marks(values):
    return ','.join('?' for _ in values)


def _context(db, records):
    """Load revisions and resolve their canonical entities without per-hit queries."""
    if not records or not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='import_revisions'").fetchone():
        return {}, {}
    ids = list(dict.fromkeys(record['id'] for record in records))
    revisions = {row['memory_id']: dict(row) for row in db.execute(
        'SELECT * FROM import_revisions WHERE memory_id IN (' + _marks(ids) + ')', ids)}
    entities = {}
    pending = {revision['entity_id'] for revision in revisions.values()}
    while pending:
        loaded = {row['id']: dict(row) for row in db.execute(
            'SELECT * FROM import_entities WHERE id IN (' + _marks(pending) + ')',
            list(pending))}
        entities.update(loaded)
        pending = {row['canonical_id'] for row in loaded.values()
                   if row['canonical_id'] and row['canonical_id'] not in entities}
    resolved = {}
    for memory_id, revision in revisions.items():
        entity = entities.get(revision['entity_id'])
        visited = set()
        while entity is not None and entity['canonical_id']:
            if entity['id'] in visited:
                raise ValueError('Canonical import entity cycle')
            visited.add(entity['id'])
            entity = entities.get(entity['canonical_id'])
        if entity is None:
            raise ValueError('Canonical import entity not found')
        resolved[memory_id] = entity
    return revisions, resolved


def compact_source_ids(db, records):
    """Add only identity pointers; never load source packets, receipts, or raw bytes."""
    records = [dict(record) for record in records]
    revisions, entities = _context(db, records)
    for record in records:
        if record['id'] not in revisions:
            continue
        entity = entities[record['id']]
        record['source_ids'] = {
            key: value for key, value in dict(
                canonical_source_id=entity['id'],
                canonical_memory_id=revisions[record['id']]['canonical_memory_id'],
                conversation_id=entity['conversation_id'],
                message_id=entity['message_id']).items() if value is not None}
    return records


def enrich_many(db, rows):
    """Preserve the full-record schema while batching provenance and receipt reads."""
    records = [dict(row) for row in rows]
    revisions, entities = _context(db, records)
    if not revisions:
        return records
    canonical_ids = list(dict.fromkeys(r['canonical_memory_id'] for r in revisions.values()))
    provenance_rows = list(db.execute('''
        SELECT r.canonical_memory_id,i.position,i.metadata_json,
               p.metadata_json AS packet,s.object_sha,o.imported_at
        FROM import_items i JOIN import_revisions r ON r.memory_id=i.memory_id
        JOIN import_packets p ON p.packet_key=i.packet_key
        JOIN import_packet_sources s ON s.packet_key=p.packet_key
        JOIN import_objects o ON o.sha256=s.object_sha
        WHERE r.canonical_memory_id IN (''' + _marks(canonical_ids) + ')', canonical_ids))
    object_shas = list(dict.fromkeys(item['object_sha'] for item in provenance_rows))
    paths = defaultdict(list)
    if object_shas:
        for row in db.execute('SELECT DISTINCT object_sha,path FROM import_receipts '
                              'WHERE object_sha IN (' + _marks(object_shas) + ')', object_shas):
            paths[row['object_sha']].append(row['path'])
    provenance = defaultdict(list)
    for item in provenance_rows:
        packet = json.loads(item['packet'])
        packet.pop('messages', None)
        provenance[item['canonical_memory_id']].append(dict(
            sha256=item['object_sha'], imported_at=item['imported_at'],
            packet_position=item['position'], packet=packet,
            message=json.loads(item['metadata_json']), paths=list(paths[item['object_sha']])))
    entity_ids = list(dict.fromkeys(entity['id'] for entity in entities.values()))
    revision_ids = defaultdict(list)
    for row in db.execute('SELECT entity_id,memory_id FROM import_revisions WHERE entity_id IN ('
                          + _marks(entity_ids) + ')', entity_ids):
        revision_ids[row['entity_id']].append(row['memory_id'])
    for record in records:
        revision = revisions.get(record['id'])
        if revision is None:
            continue
        entity = entities[record['id']]
        record['import_metadata'] = dict(
            kind=entity['kind'], canonical_source_id=entity['id'],
            canonical_memory_id=revision['canonical_memory_id'], account_id=entity['account_id'],
            conversation_id=entity['conversation_id'], message_id=entity['message_id'],
            speaker=entity['speaker'], source_order=entity['source_order'],
            provenance=provenance[revision['canonical_memory_id']],
            revision_ids=revision_ids[entity['id']])
    return records


def enrich(db, row):
    """Retrieve the original full-record contract for a single memory."""
    return enrich_many(db, [row])[0]
