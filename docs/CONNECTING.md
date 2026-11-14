# Connect an assistant

Run `python setup_memory.py` in the installation before connecting. Use the absolute path to your virtual environment’s Python as the MCP command, and the absolute path to `server_readonly.py` as its argument. For example, in a Codex MCP configuration:

```toml
[mcp_servers.threadsatchel]
command = "/absolute/path/threadsatchel/.venv/bin/python"
args = ["/absolute/path/threadsatchel/server_readonly.py"]
```

On Windows, use paths such as `C:/path/to/threadsatchel/.venv/Scripts/python.exe`. The reader opens `memory.sqlite3` beside the source with `mode=ro` and uses a separate writable directory for lookup state. Refresh tool discovery after updating a client.

## Read tools

| Tool | Meaning |
| --- | --- |
| `search_memory` | Returns a `hits` array of ranked previews. Defaults to 10 candidates and accepts up to 20. The budget can reduce the number returned. |
| `peek_memory` | Reads a small window around the selected match. |
| `timeline_memory` | Finds surrounding messages and their ordering basis within the source conversation when available. |
| `get_memory` | Returns a small original, a structural map, or an exact block or character range. Offsets count Unicode code points, starting at zero and excluding the end. |
| `list_memories` | Enumerates original records, including revisions. Defaults to 3. Follow the opaque `next_cursor` for additional budgeted pages and compare the final `returned_total` with `total_count`. |
| `list_handoffs`, `get_handoff`, `read_handoff_file` | Find a named handoff, inspect its versions and files, and read a selected text file by character offset. These tools use their own paging limits. |
| `memory_ai_status`, `get_project_brief` | Report optional model/index status and read a source-linked project brief. |

Start with a search. For follow-up reads, send the selected hit’s `ref` and `lookup_id`. With a known original ID, specify its revision for exact range or block requests. A stale revision, missing version, or overlarge range has an explicit result. Use the returned map or range guidance. `full=True` remains budgeted. A depleted lookup ends that allowance. A new lookup is a separate allowance.

The default server runs directly over stdio. To share a warm reader between local clients, set `THREADSATCHEL_SHARED_READER=1` in their server environments. The worker listens on loopback and uses a local key stored under `.reader-state/`. Multiple installations can choose distinct `THREADSATCHEL_READER_PORT` values.

## Write and remote access

- `server_save.py` exposes importer-backed notes and summaries, versioned handoffs, and recent save failures. Use the same request key and unchanged content when retrying.
- `server.py` is a separate writable endpoint that also offers `store_memory`. Supplying an idempotency key protects retries. Each call without a key appends a record.
- MCP clients outside the archive computer can use the bundled gateway and outbound connector. Remote save requests can be pending. Confirm the importer result before reporting a successful save. [Remote access](PRIVATE_GATEWAY.md) · [Save results](SAVE_RESULTS.md)
