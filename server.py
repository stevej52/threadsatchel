"""ThreadSatchel: a bounded, local SQLite/FTS5 MCP proof-of-concept."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
from import_metadata import enrich
from memory_search import search
from memory_listing import list_records

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

DB = Path(__file__).resolve().with_name('memory.sqlite3')
mcp = MCPServer('ThreadSatchel')
MAX_TEXT_BYTES = 1024 * 1024
MAX_SOURCE_BYTES = 4096
MAX_TITLE_BYTES = 1024
IDEMPOTENCY_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')


def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def initialize():
    with closing(connect()) as db, db:
        db.execute('''CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY, text TEXT NOT NULL, source TEXT NOT NULL,
            title TEXT, created_at TEXT NOT NULL)''')
        db.execute('''CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
            id UNINDEXED, text, source, title, tokenize='unicode61')''')
        db.execute('''CREATE TABLE IF NOT EXISTS memory_write_receipts (
            idempotency_key TEXT PRIMARY KEY, request_sha TEXT NOT NULL,
            memory_id TEXT NOT NULL REFERENCES memories(id))''')


def _request_hash(text, source, title):
    return hashlib.sha256(json.dumps([text, source, title], ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _bounded_text(value, name, limit, optional=False):
    if optional and value is None:
        return
    if not isinstance(value, str):
        raise ValueError(name + ' must be nonempty text')
    # Check character length before encoding to avoid allocating huge input copies.
    if len(value) > limit or len(value.encode('utf-8')) > limit:
        raise ValueError(name + ' exceeds the UTF-8 byte limit of ' + str(limit))
    if not optional and not value.strip():
        raise ValueError(name + ' must be nonempty text')


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                    idempotent_hint=False, open_world_hint=False))
def store_memory(text: str, source: str, optional_title: str | None = None,
                 idempotency_key: str | None = None) -> dict:
    """Store original text (up to 1 MiB). Reuse a caller-generated idempotency_key
    for retries of the same write; it returns the original record, or rejects
    changed content. Without a key, each call intentionally creates a new record.
    """
    _bounded_text(text, 'text', MAX_TEXT_BYTES)
    _bounded_text(source, 'source', MAX_SOURCE_BYTES)
    _bounded_text(optional_title, 'optional_title', MAX_TITLE_BYTES, optional=True)
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or
                                      not IDEMPOTENCY_KEY.fullmatch(idempotency_key)):
        raise ValueError('idempotency_key must be 1..128 ASCII letters, digits, dots, underscores, colons or hyphens, starting with a letter or digit')
    request_sha = _request_hash(text, source, optional_title)
    record = dict(id=str(uuid4()), text=text, source=source, title=optional_title,
                  created_at=datetime.now(timezone.utc).isoformat())
    with closing(connect()) as db, db:
        # Serialize key lookup and both inserts. A lost response can replay the
        # committed receipt; a failed transaction leaves neither row behind.
        db.execute('BEGIN IMMEDIATE')
        if idempotency_key is not None:
            receipt = db.execute('SELECT request_sha,memory_id FROM memory_write_receipts '
                                 'WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if receipt is not None:
                if receipt['request_sha'] != request_sha:
                    raise ValueError('Idempotency key already belongs to different content')
                original = db.execute('SELECT * FROM memories WHERE id=?', (receipt['memory_id'],)).fetchone()
                if original is None or _request_hash(original['text'], original['source'], original['title']) != request_sha:
                    raise ValueError('Idempotency receipt no longer matches its original record')
                return dict(original)
        db.execute('INSERT INTO memories VALUES (:id,:text,:source,:title,:created_at)', record)
        db.execute('INSERT INTO memory_fts (id,text,source,title) VALUES (:id,:text,:source,:title)', record)
        if idempotency_key is not None:
            db.execute('INSERT INTO memory_write_receipts VALUES (?,?,?)',
                       (idempotency_key, request_sha, record['id']))
    return record


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def search_memory(query: str, limit: int = 20, full: bool = False) -> list[dict]:
    """Search ranked excerpts (default 20, maximum 100); full=True expands the same results to original text and provenance."""
    with closing(connect()) as db:
        return search(db, Path(DB).parent, query, limit=limit, full=full)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def list_memories(limit: int = 100, cursor: str | None = None) -> dict:
    """List all original records in pages of 1..100 previews, independent of search. Follow next_cursor until null; total_count counts records, not AI chunks. Use get_memory(id) for full text. New arrivals join the next scan."""
    with closing(connect()) as db:
        return list_records(db, limit=limit, cursor=cursor)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def get_memory(id: str) -> dict:
    """Retrieve the exact original text and source plus metadata by memory ID."""
    with closing(connect()) as db:
        row = db.execute('SELECT * FROM memories WHERE id=?', (id,)).fetchone()
        if row is None:
            raise ValueError('Memory ID not found')
        return enrich(db, row)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def get_project_brief(project: str) -> dict:
    """Read an optional local project brief with source references and freshness metadata."""
    from ai_memory import project_brief
    with closing(connect()) as db:
        return project_brief(Path(DB).parent, db, project)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def memory_ai_status() -> dict:
    """Report the optional local AI layer's configuration and current index status."""
    from ai_memory import status
    with closing(connect()) as db:
        return status(Path(DB).parent, db)


if __name__ == '__main__':
    initialize()
    try:
        from memory_prewarm import start
    except ImportError:
        prewarm = None  # Core-only installations do not require optional AI files.
    else:
        prewarm = start(Path(DB).parent)
    try:
        mcp.run(transport='stdio')
    finally:
        if prewarm is not None:
            prewarm.stop()
