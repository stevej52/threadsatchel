"""Read-only stdio MCP entry point for the existing ThreadSatchel database."""
from contextlib import closing
from pathlib import Path
import sqlite3
from import_metadata import enrich
from memory_search import search

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
def search_memory(query: str, limit: int = 20, full: bool = False) -> list[dict]:
    """Search ranked excerpts (limit 1..20); full=True expands the same search results to original text and provenance."""
    with closing(connect()) as db:
        return search(db, Path(DB).parent, query, limit=limit, full=full)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def get_memory(id: str) -> dict:
    """Retrieve the complete original memory and metadata by ID."""
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
    mcp.run(transport='stdio')
