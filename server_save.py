"""Stdio MCP server for saving to ThreadSatchel: notes, summaries and named handoffs.

Saving goes through the plugin connector's Archive, so it gets the same checks as Codex's connector
(credential guard, importer receipts, write-once request keys, versioned handoff folders). Reading stays
in server_readonly.py. State (write receipts, failure records) is kept apart from the connector's own.
"""
import base64
import hashlib
from pathlib import Path
from typing import TypedDict

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'save-state'


from connector import connector

archive = connector.Archive(ROOT, STATE)
mcp = MCPServer('ThreadSatchel Save')
SAVE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


class HandoffFile(TypedDict):
    name: str
    text: str


def _name(value: str) -> None:
    try:
        connector.safe_name(value)
    except ValueError:
        raise ToolError(f'Invalid name {value!r}: use letters, digits, spaces, "_", "." and "-", up to 80') from None


def _key(args: dict) -> str:
    # The same content saved again maps to the same key, so a retry never saves twice.
    return 'mcp-' + hashlib.sha256(connector.encode(args)).hexdigest()[:40]


def _run(action: str, args: dict) -> dict:
    # The connector's run_job envelope and failure records, so recent_save_errors sees failures here too.
    operation = hashlib.sha256(connector.encode({'action': action, 'args': args})).hexdigest()
    archive.diagnostics.record(operation, action, 'started')
    try:
        result = connector.public_result({'ok': True, 'result': archive.execute(action, args)}, action, operation)
    except Exception as exc:
        detail = connector.error_detail(exc, action)
        archive.diagnostics.record(operation, action, 'failed', detail)
        return {'ok': False, 'error': detail['message'], 'error_detail': detail}
    archive.diagnostics.record(operation, action, 'succeeded')
    return result


@mcp.tool(annotations=SAVE)
def save_memory(title: str, text: str, kind: str = 'note', speaker: str = 'assistant',
                request_key: str | None = None) -> dict:
    """Save a note or summary into ThreadSatchel; it is searchable as soon as
    this returns. kind: "note" or "summary". Title session summaries
    "Handoff: <topic>, <date>". Never include passwords, keys or tokens (a save
    that looks like one is held back for review).
    """
    if kind not in ('note', 'summary'):
        raise ToolError('kind must be "note" or "summary"')
    if not str(title).strip() or not str(text).strip():
        raise ToolError('title and text must not be empty')
    packet = {'format': 'threadsatchel/1', 'kind': kind, 'title': str(title).strip(),
              'messages': [{'speaker': str(speaker or 'assistant'), 'text': str(text)}]}
    args = {'packet': packet}
    args['request_key'] = request_key or _key(args)
    return _run('save_memory', args)


@mcp.tool(annotations=SAVE)
def save_handoff(name: str, instructions: str, files: list[HandoffFile] | None = None,
                 request_key: str | None = None) -> dict:
    """Save a named handoff that another AI opens with get_handoff(name):
    instructions (saved as instructions.md and indexed in memory) plus up to
    10 text files, 1 MiB in all, e.g. [{"name": "HANDOFF.txt", "text": "..."}].
    Names: letters, digits, spaces, "_", "." and "-", up to 80. Saving under an
    existing name adds the next version (v2, v3, ...); nothing is overwritten.
    """
    _name(name)
    if len(files or []) > 10:
        raise ToolError('At most 10 files; put more into fewer files')
    if sum(len(str(f['text']).encode('utf-8')) for f in files or []) > 1024 * 1024:
        raise ToolError('Files exceed 1 MiB in all')
    entries = []
    for f in files or []:
        _name(f['name'])
        entries.append({'name': f['name'], 'base64': base64.b64encode(str(f['text']).encode('utf-8')).decode('ascii')})
    args = {'name': name, 'instructions': str(instructions), 'files': entries}
    args['request_key'] = request_key or _key(args)
    return _run('save_handoff', args)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def recent_save_errors(limit: int = 10) -> dict:
    """Show the latest failed saves made through these tools (newest first)."""
    return archive.recent_save_errors(max(1, min(int(limit), 20)))


if __name__ == '__main__':
    mcp.run(transport='stdio')
