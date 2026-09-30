"""ThreadSatchel: a bounded, local SQLite/FTS5 MCP proof-of-concept."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from uuid import uuid4
from import_metadata import enrich
from memory_search import search
from memory_listing import list_records

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

DB = Path(__file__).resolve().with_name('memory.sqlite3')
mcp = MCPServer('ThreadSatchel')


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


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                    idempotent_hint=False, open_world_hint=False))
def store_memory(text: str, source: str, optional_title: str | None = None) -> dict:
    """Persist complete original text and its source locally; return the complete record."""
    if not text.strip() or not source.strip():
        raise ValueError('text and source must be nonempty')
    record = dict(id=str(uuid4()), text=text, source=source, title=optional_title,
                  created_at=datetime.now(timezone.utc).isoformat())
    with closing(connect()) as db, db:
        db.execute('INSERT INTO memories VALUES (:id,:text,:source,:title,:created_at)', record)
        db.execute('INSERT INTO memory_fts (id,text,source,title) VALUES (:id,:text,:source,:title)', record)
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


if __name__ == '__main__':
    initialize()
    mcp.run(transport='stdio')
