"""Read-only stdio MCP entry point for the existing ThreadSatchel database."""
from contextlib import closing
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from memory_retrieval import compact_search, get_memory_slice, peek_memory as peek_window, timeline_memory as timeline_window
from memory_response import bounded_auxiliary
from memory_listing import list_records

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

DB = Path(__file__).resolve().with_name('memory.sqlite3')
_DEFAULT_DB = DB
from reader_transport import daemon_url, describe_health, proof, reader_key
_DAEMON_URL = daemon_url()
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
mcp = MCPServer('ThreadSatchel Read Only')


def _should_use_daemon() -> bool:
    if (os.environ.get('THREADSATCHEL_SHARED_READER') != '1' or
            os.environ.get('THREADSATCHEL_READER_WORKER') == '1'):
        return False
    try:
        return DB.resolve() == _DEFAULT_DB
    except OSError:
        return False


def _reader_healthy():
    try:
        nonce = os.urandom(20).hex()
        req = urllib.request.Request(_DAEMON_URL + '/health', headers={'X-Challenge': nonce})
        with _OPENER.open(req, timeout=0.6) as response:
            return describe_health(reader_key(DB.parent), nonce, json.loads(response.read(4096)))
    except (OSError, ValueError, RuntimeError):
        return False


def _ensure_daemon_started() -> bool:
    if _reader_healthy():
        return True
    daemon_script = DB.with_name('readonly_daemon.py')
    if not daemon_script.is_file():
        return False
    pyw = Path(sys.executable).with_name('pythonw.exe') if os.name == 'nt' else Path(sys.executable)
    if not pyw.is_file():
        pyw = Path(sys.executable)
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'DETACHED_PROCESS', 0)
    try:
        subprocess.Popen([str(pyw), str(daemon_script)], cwd=str(DB.parent),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags, start_new_session=os.name != 'nt')
    except OSError:
        return False
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline:
        if _reader_healthy():
            return True
        time.sleep(0.1)
    return False


def _call_daemon(tool: str, args: dict):
    payload = json.dumps({'tool': tool, 'args': args}, ensure_ascii=False).encode('utf-8')
    if not _reader_healthy() and not _ensure_daemon_started():
        return False, None
    signature = proof(reader_key(DB.parent), b'rpc', payload)
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                _DAEMON_URL + '/rpc',
                data=payload,
                headers={'Content-Type': 'application/json; charset=utf-8', 'X-Request-Proof': signature},
                method='POST',
            )
            with _OPENER.open(req, timeout=25) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            if data.get('ok') is True:
                return True, data.get('result')
            err_type = data.get('error_type')
            msg = data.get('message', 'Tool execution failed')
            if err_type == 'ToolError':
                raise ToolError(msg)
            raise ValueError(msg)
        except (ToolError, ValueError):
            raise
        except Exception:
            if attempt == 0 and not _ensure_daemon_started():
                break
    return False, None


def _watch_parent_process():
    """Exit cleanly if the parent process dies on Windows so stdio workers never orphan."""
    if os.name != 'nt':
        return
    ppid = os.getppid()
    if ppid <= 0:
        return

    def _watcher():
        import ctypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        SYNCHRONIZE = 0x00100000
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, ppid)
        if not handle:
            return
        try:
            # WaitForSingleObject blocks with 0% CPU until parent exits.
            kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        finally:
            kernel32.CloseHandle(handle)
        os._exit(0)

    threading.Thread(target=_watcher, name='parent-watchdog', daemon=True).start()


def connect():
    # mode=ro refuses writes and refuses to create a missing database.
    # Never import server.py: it owns the separate writable Codex endpoint.
    db = sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True, timeout=10)
    db.execute('PRAGMA query_only=ON')
    db.row_factory = sqlite3.Row
    return db


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def search_memory(query: str, limit: int = 10, full: bool = False,
                  budget_tokens: int = 2000) -> dict:
    """Search a light index (default ten, max twenty). Reuse lookup_id and a
    returned ref for exact follow-up reads within one budget. full=True stays bounded.
    Local model descriptions are interpretations; verify original source evidence.
    """
    if _should_use_daemon():
        ok, res = _call_daemon('search_memory', {'query': query, 'limit': limit, 'full': full, 'budget_tokens': budget_tokens})
        if ok:
            return res
    with closing(connect()) as db:
        return compact_search(db, Path(DB).parent, query, limit=limit,
                              full=full, budget_tokens=budget_tokens)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def list_memories(limit: int = 3, cursor: str | None = None,
                  budget_tokens: int = 2000, lookup_id: str | None = None) -> dict:
    """List budgeted original-record previews. Prefer targeted search. Follow
    next_cursor only for requested enumeration; rows are not silently skipped.
    """
    if _should_use_daemon():
        ok, res = _call_daemon('list_memories', {'limit': limit, 'cursor': cursor, 'budget_tokens': budget_tokens, 'lookup_id': lookup_id})
        if ok:
            return res
    with closing(connect()) as db:
        return list_records(db, limit=limit, cursor=cursor, root=Path(DB).parent,
                            budget_tokens=budget_tokens, lookup_id=lookup_id)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def get_memory(id: str | None = None, revision: str | None = None,
               block_id: str | None = None, start_char: int | None = None,
               end_char: int | None = None, full: bool = False,
               budget_tokens: int = 2000, lookup_id: str | None = None,
               ref: str | None = None) -> dict:
    """Fetch an original block/range or compact map. Offsets are zero-based
    Unicode code points [start,end). Use a search ref plus lookup_id, or an
    original ID and revision for exact ranges. Reuse lookup_id;
    request only needed evidence. Full requests never bypass the budget.
    """
    if _should_use_daemon():
        ok, res = _call_daemon('get_memory', {
            'id': id, 'revision': revision, 'block_id': block_id,
            'start_char': start_char, 'end_char': end_char, 'full': full,
            'budget_tokens': budget_tokens, 'lookup_id': lookup_id, 'ref': ref,
        })
        if ok:
            return res
    with closing(connect()) as db:
        return get_memory_slice(db, Path(DB).parent, id, revision=revision,
            block_id=block_id, start_char=start_char, end_char=end_char,
            full=full, budget_tokens=budget_tokens, lookup_id=lookup_id, ref=ref)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def peek_memory(id: str | None = None, revision: str | None = None, match_id: str | None = None,
                start_char: int | None = None, before: int = 500, after: int = 500,
                budget_tokens: int = 2000, lookup_id: str | None = None,
                ref: str | None = None) -> dict:
    """Inspect a bounded original window. Use returned block IDs for exact
    retrieval; never guess unseen boundaries. Reuse the search lookup_id.
    """
    if _should_use_daemon():
        ok, res = _call_daemon('peek_memory', {
            'id': id, 'revision': revision, 'match_id': match_id,
            'start_char': start_char, 'before': before, 'after': after,
            'budget_tokens': budget_tokens, 'lookup_id': lookup_id, 'ref': ref,
        })
        if ok:
            return res
    with closing(connect()) as db:
        return peek_window(db, Path(DB).parent, id, revision=revision,
            match_id=match_id, start_char=start_char, before=before, after=after,
            budget_tokens=budget_tokens, lookup_id=lookup_id, ref=ref)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def timeline_memory(id: str | None = None, revision: str | None = None,
                    before: int = 3, after: int = 3, budget_tokens: int = 2000,
                    lookup_id: str | None = None, ref: str | None = None) -> dict:
    """Return a bounded index of nearby source messages (up to five before
    and five after). Use it to inspect context and later corrections, then
    fetch only needed original evidence by ref with the same lookup_id.
    """
    if _should_use_daemon():
        ok, res = _call_daemon('timeline_memory', {
            'id': id, 'revision': revision, 'before': before, 'after': after,
            'budget_tokens': budget_tokens, 'lookup_id': lookup_id, 'ref': ref,
        })
        if ok:
            return res
    with closing(connect()) as db:
        return timeline_window(db, Path(DB).parent, id, revision=revision,
            before=before, after=after, budget_tokens=budget_tokens,
            lookup_id=lookup_id, ref=ref)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def get_project_brief(project: str) -> dict:
    """Read an optional local project brief with source references and freshness metadata."""
    if _should_use_daemon():
        ok, res = _call_daemon('get_project_brief', {'project': project})
        if ok:
            return res
    from ai_memory import project_brief
    with closing(connect()) as db:
        return bounded_auxiliary(Path(DB).parent, project_brief(Path(DB).parent, db, project))


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def memory_ai_status() -> dict:
    """Report the optional local AI layer's configuration and current index status."""
    if _should_use_daemon():
        ok, res = _call_daemon('memory_ai_status', {})
        if ok:
            return res
    from ai_memory import status
    with closing(connect()) as db:
        return bounded_auxiliary(Path(DB).parent, status(Path(DB).parent, db))


# Named handoffs (handoffs/<name>/v<N>/ files, or legacy handoffs/<name>.md|.txt|.zip), saved through the
# plugin connector. Read here with the connector's path rules: relative, no dot parts, no links, inside handoffs.
# Their refusals are ToolErrors so the AI sees why (the MCP library hides other exceptions' text); the connector
# never calls these tools.
HANDOFFS = DB.with_name('handoffs')
TEXT_SUFFIXES = {'.txt', '.md', '.json', '.csv', '.log', '.yaml', '.yml', '.py', '.ps1', '.js', '.mjs', '.html', '.xml', '.ini', '.toml'}
FILLER = {'a', 'an', 'and', 'the', 'of', 'for', 'to', 'in', 'on', 'handoff', 'handoffs'}


def _linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _handoff_path(relative: str) -> Path:
    relative = str(relative).replace('\\', '/')
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or any(p in ('.', '..') or p.startswith('.') for p in parts):
        raise ToolError('Invalid handoff path; use a path from get_handoff')
    if _linked(HANDOFFS) or any(":" in part for part in parts):
        raise ToolError("Invalid handoff directory or path")
    current = HANDOFFS
    for part in parts:
        current = current / part
        if _linked(current):
            raise ToolError('Links are not permitted')
    resolved = current.resolve()
    if not resolved.is_relative_to(HANDOFFS.resolve()):
        raise ToolError('Path outside handoffs')
    return resolved


def _versions(folder: Path) -> list:
    if not folder.is_dir():
        return []
    return sorted((p.name for p in folder.iterdir() if p.is_dir() and not _linked(p) and re.fullmatch(r'v[1-9][0-9]*', p.name)),
                  key=lambda v: int(v[1:]))


def _handoff_names() -> dict:
    found = {}
    if _linked(HANDOFFS) or not HANDOFFS.is_dir():
        return found
    for item in HANDOFFS.iterdir():
        if item.name.startswith('.') or _linked(item):
            continue
        name = item.name if item.is_dir() else item.stem
        versions = _versions(item)
        newest = item / versions[-1] if versions else item
        found[name] = max(found.get(name, 0), newest.stat().st_mtime)
    return found


def _plain(text: str) -> str:
    return re.sub(r'[\s_-]+', ' ', str(text)).strip().lower()


def _words(text: str) -> set:
    return {w for w in re.findall(r'[a-z0-9]+', str(text).lower()) if w not in FILLER}


def _score(query: str, name: str) -> int:
    # Count prefix matches, treating "diagnostic" and "diagnostics" as the same word.
    have = _words(name)
    return sum(any(h.startswith(w) or (len(h) >= 3 and w.startswith(h)) for h in have) for w in _words(query))


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def list_handoffs(query: str | None = None, offset: int = 0, limit: int = 20) -> dict:
    """List named handoffs (files saved for an AI to pick up), newest first.
    With query, those sharing the most words with it come first. Open one
    with get_handoff.
    """
    if _should_use_daemon():
        ok, res = _call_daemon('list_handoffs', {'query': query, 'offset': offset, 'limit': limit})
        if ok:
            return res
    names = _handoff_names()
    if query and _words(query):
        scored = {n: _score(query, n) for n in names}
        rows = sorted((n for n in names if scored[n]), key=lambda n: (-scored[n], -names[n]))
    else:
        rows = sorted(names, key=lambda n: -names[n])
    offset, limit = max(0, int(offset)), max(1, min(int(limit), 50))
    page = [{'name': n, 'latest_version': (_versions(HANDOFFS / n) or [None])[-1]} for n in rows[offset:offset + limit]]
    return {'handoffs': page, 'total_count': len(rows), 'offset': offset,
            'next_offset': offset + len(page) if offset + len(page) < len(rows) else None}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def get_handoff(name: str, version: str | None = None) -> dict:
    """List one named handoff's files (latest version unless version, e.g.
    "v1", is given). name may be the pickup name or a title with the same
    distinctive words. Read each file with read_handoff_file(path).
    """
    if _should_use_daemon():
        ok, res = _call_daemon('get_handoff', {'name': name, 'version': version})
        if ok:
            return res
    asked, names = name, list(_handoff_names())
    if name not in names:
        matches = [n for n in names if _plain(n) == _plain(name)] or [n for n in names if _plain(name) in _plain(n)]
        if len(matches) != 1:
            scored = sorted(((_score(name, n), n) for n in names), reverse=True)
            top = [n for s, n in scored if s >= 2 and s == scored[0][0]] if scored else []
            if len(top) != 1:
                close = [n for s, n in scored[:5] if s]
                raise ToolError('No handoff named that' + (f'; closest: {", ".join(close)}' if close else '') + '. Use list_handoffs.')
            matches = top
        name = matches[0]
    if version and not re.fullmatch(r'v[1-9][0-9]{0,5}', version):
        raise ToolError('Invalid version; use one like "v1"')
    folder = _handoff_path(name) if (HANDOFFS / name).is_dir() else None
    versions = _versions(folder) if folder else []
    if version and version not in versions:
        raise ToolError(f'No version {version}; this handoff has {", ".join(versions) or "no numbered versions"}')
    chosen = version or (versions[-1] if versions else None)
    files = []
    if folder:
        base = _handoff_path(f'{name}/{chosen}') if chosen else folder
        for directory, folders, filenames in os.walk(base):
            parent = Path(directory)
            folders[:] = sorted(d for d in folders if not d.startswith('.') and not _linked(parent / d))
            for filename in sorted(filenames):
                item = parent / filename
                if not filename.startswith('.') and not _linked(item) and item.is_file():
                    files.append(item)
    if not version:
        for ext in ('.md', '.txt', '.zip'):
            item = HANDOFFS / (name + ext)
            if not _linked(item) and item.is_file():
                files.append(item)
    if not files:
        raise ToolError('This handoff has no files')
    result = {'name': name, 'version': chosen, 'versions': versions, 'file_count': len(files),
              'files': [{'path': f.relative_to(HANDOFFS).as_posix(), 'size': f.stat().st_size} for f in files[:100]]}
    if name != asked:
        result['matched_from'] = asked
    return result


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def read_handoff_file(path: str, offset: int = 0, length: int = 8000) -> dict:
    """Read a text file from a handoff (path as get_handoff gives it). Offsets
    count characters; continue from next_offset until eof is true.
    """
    if _should_use_daemon():
        ok, res = _call_daemon('read_handoff_file', {'path': path, 'offset': offset, 'length': length})
        if ok:
            return res
    item = _handoff_path(path)
    if not item.is_file():
        raise ToolError('File not found; use a path from get_handoff')
    size = item.stat().st_size
    if item.suffix.lower() not in TEXT_SUFFIXES or size > 4 * 1024 * 1024:
        return {'path': path, 'size': size, 'text': None, 'note': 'Not a readable text file (binary or over 4 MB).'}
    text = item.read_text(encoding='utf-8-sig', errors='replace')
    offset, length = max(0, int(offset)), max(1, min(int(length), 12000))
    end = min(len(text), offset + length)
    return {'path': path, 'size': size, 'total_chars': len(text), 'offset': offset, 'next_offset': end,
            'eof': end >= len(text), 'text': text[offset:end]}


if __name__ == '__main__':
    _watch_parent_process()
    if _should_use_daemon():
        threading.Thread(target=_ensure_daemon_started, name='daemon-starter', daemon=True).start()
        mcp.run(transport='stdio')
    else:
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
