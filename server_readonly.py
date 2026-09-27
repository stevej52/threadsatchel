"""Read-only stdio MCP entry point for the existing ThreadSatchel database."""
from contextlib import closing
from pathlib import Path
import re
import sqlite3
from import_metadata import enrich

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

DB = Path(__file__).resolve().with_name('memory.sqlite3')
mcp = MCPServer('ThreadSatchel Read Only')


def connect():
    # mode=ro refuses writes and refuses to create a missing database.
    # Never import server.py: it owns the separate writable Codex endpoint.
    db = sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True, timeout=10)
    db.execute('PRAGMA query_only=ON')
    db.row_factory = sqlite3.Row
    return db


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def search_memory(query: str) -> list[dict]:
    """Search literal words in existing memories; return up to 20 complete records."""
    terms = list(dict.fromkeys(re.findall(r'[^\W_]+', query, flags=re.UNICODE)))
    if not terms:
        return []
    expression = ' OR '.join('"' + term + '"' for term in terms)
    with closing(connect()) as db:
        rows = db.execute('''SELECT m.* FROM memory_fts
            JOIN memories AS m ON m.id=memory_fts.id
            WHERE memory_fts MATCH ? ORDER BY bm25(memory_fts), m.created_at DESC LIMIT 20''',
            (expression,)).fetchall()
        return [enrich(db, row) for row in rows]


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def get_memory(id: str) -> dict:
    """Retrieve the complete original memory and metadata by ID."""
    with closing(connect()) as db:
        row = db.execute('SELECT * FROM memories WHERE id=?', (id,)).fetchone()
        if row is None:
            raise ValueError('Memory ID not found')
        return enrich(db, row)


if __name__ == '__main__':
    mcp.run(transport='stdio')
