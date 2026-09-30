# Build specification: your private ThreadSatchel gateway

Use this with [Create your own plugin](CREATE_YOUR_OWN_PLUGIN.md), route A. It describes the architecture used by the author's working private plugin, generalized for a new owner. It is a specification and staged build instructions for your coding assistant, not an included deployable gateway or a shared service.

The repository implements the archive, importer, and local MCP endpoints. **The gateway, outbound connector, pairing/configuration flow, and remote save/status tools described here are components you must build in your own account.** No step depends on contacting the author or receiving their plugin, credentials, or private files.

## Deliverables and ownership

Create a separate gateway project with:

```text
your-gateway-project/
  app/mcp/route.ts               # Stateless HTTP MCP endpoint
  app/api/agent/route.ts         # Authenticated connector polling/completion
  app/api/status/route.ts        # Owner-only operational status, if UI uses it
  lib/tool-contract.ts          # Tool metadata and actual argument validation
  lib/gateway.ts                # Owner checks, operations, leases, cleanup
  db/schema.ts                  # Private operational tables
  connector/connector.py        # Outbound worker for the archive computer
  connector/configure.py        # Protected local credential setup
  tests/                        # Synthetic gateway/connector tests
  RUNBOOK.md                    # Your exact install, restart, update, recovery commands
```

These paths are a suggested organization, not files already in this repository. Let Sites initialize the supported project/runtime layout. Add MCP capability to its hosting configuration while preserving the other generated settings. A Site runs the HTTP gateway; it does not run the local Python stdio server or hold the authoritative SQLite archive.

Use a private Site and Sites-managed authentication. Its trusted hosting boundary supplies `oai-authenticated-user-id`; bind data access to the intended owner. Email is display information, not the durable authorization key. Configure or deliberately initialize that owner while the Site is private, and reject other identities even if its audience later changes. Do not trust a client-supplied identity header on an unprotected origin.

The connector uses a separate randomly generated installation credential. Store the gateway copy as a secret and the computer's copy in protected local configuration. Private Sites may also require platform-issued service access for the connector to pass the hosting boundary. Use the Sites-supported service credential flow and verify its scope; never relax the Site's audience or fabricate a user identity to get a machine call through. A service credential for `/api/agent` must not give an anonymous caller owner access to `/mcp`.

## Tool contract

Keep tool names and arguments identical through discovery, validation, queued JSON, connector dispatch, and the underlying Python call. Descriptions should mention **the user's archive** rather than a particular person's computer.

| Tool | Arguments | Execution |
| --- | --- | --- |
| `search_memory` | `query`: nonempty string, at most 2,000 characters; `limit`: integer 1–100, default 20; `full`: boolean, default false | `server_readonly.search_memory(**args)` |
| `list_memories` | `limit`: integer 1–100, default 100; `cursor`: omitted/null or opaque string up to 4,096 characters | `server_readonly.list_memories(**args)` |
| `get_memory` | `id`: nonempty string, at most 128 characters | `server_readonly.get_memory(**args)` |
| `connection_status` | No arguments | Gateway heartbeat/status only; no archive read |
| `get_operation` | `operation_id`: the ID returned by this gateway | Owner-scoped operation lookup |
| `save_memory` (optional write) | `request_key`: 1–128 characters matching `[A-Za-z0-9][A-Za-z0-9_.:-]*`; `packet`: a supported `threadsatchel/1` object | Local receipt handling followed by the existing importer |

Reject unknown tools/arguments and wrong JSON types. Apply the same defaults on discovery and execution. In particular, do not retain an old 20-result maximum in a gateway validator after updating the backend to 100.

For example, the inventory's advertised input schema is:

```json
{
  "type": "object",
  "properties": {
    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
    "cursor": {"type": ["string", "null"], "maxLength": 4096}
  },
  "additionalProperties": false
}
```

Use this description:

> List original archive records in stable ID order with compact previews. Follow next_cursor until null and check returned_total against total_count. Use get_memory(id) for full text. Counts include retained revisions, not AI chunks. New arrivals join a fresh scan; restart if membership changes.

A successful inventory value has this shape; the IDs/text below are synthetic:

```json
{
  "records": [{
    "id": "synthetic-record-1",
    "title": "SYNTHETIC test",
    "source": "SYNTHETIC example",
    "created_at": "2000-01-01T00:00:00Z",
    "text": "SYNTHETIC original text.",
    "text_truncated": false
  }],
  "returned_count": 1,
  "returned_total": 1,
  "total_count": 1,
  "has_more": false,
  "next_cursor": null
}
```

Preserve the Python result unchanged inside the gateway's operation envelope. Do not recalculate counts from search matches, strip the cursor, or collapse revisions into one record.

Annotate read tools as read-only and non-destructive. Mark `save_memory` as a write, non-destructive append/import action; describe its explicit keyed retry behavior. Tool annotations describe intent; authorization must also be enforced in code. Advertise Qwen's `get_project_brief` and `memory_ai_status` only if your chosen backend actually includes them. The model-free main branch does not.

## Gateway implementation sequence

1. **Initialize the private Site project.** Use current Sites tooling, private operational storage, generated migrations, and secret configuration. Use the MCP SDK/runtime appropriate for that project rather than inventing a new wire protocol.
2. **Implement `/mcp`.** Support protocol initialization, discovery, notifications, ping, and tool calls for the negotiated MCP version. Discovery contains descriptions/schemas, no private archive content. Authenticate and authorize every data-bearing call, including status and operation lookup. Return protocol/tool errors in the correct MCP form.
3. **Create durable operation storage.** Store job metadata separately from bounded input/result blobs. Every job has `id`, `owner`, `action`, `digest`, `status`, creation/expiry times, and a lease token/deadline. A state table holds the pinned owner and heartbeat. Use private D1/R2 or equivalent Sites-supported storage; never a public bucket.
4. **Submit jobs.** Validate before queuing. Derive write operation identity from owner + action + request key; store a SHA-256 digest of deterministic serialized content. Same key/content recovers the same operation. Same key with changed content fails, including under concurrent submissions. Read operations can receive fresh IDs.
5. **Implement `/api/agent`.** Authenticate the connector independently. A poll updates its heartbeat and atomically claims a pending job or an expired lease. Return job ID, lease token, exact serialized payload, and its digest. Completion must match the current unexpired lease and job; an obsolete worker must not overwrite a newer result.
6. **Return durable progress.** Wait briefly for a result, then return `operation_id` and `status: "pending"` if still running. `get_operation` checks both owner and expiry. A finished failure is distinct from a finished success. Make offline/rejected-before-queue responses explicit.
7. **Bound and expire data.** Enforce byte limits while reading streams, not only from `Content-Length`. Set queue capacity, timeouts, job/result expiry, and cleanup. A reference starting point is 2 MiB per request/result, 32 outstanding jobs, a five-minute lease, and 24-hour remote retention. Adapt these consciously and document them. Retain local write receipts separately for retry safety.
8. **Build a small status page.** Show connection health, the configured archive label, and setup/restart guidance. Keep transcript contents and credentials out of the page and operational logs.

Use separate object keys for concurrent result leases and conditionally commit the winning result pointer. Clean orphaned/expired payloads as well as database rows. Never acknowledge completion before its result is durable. Expiring a remote operation must not erase the local receipt for an already successful write.

For an HTTP MCP tool result, include a model-readable JSON representation in `content`. If supplying `structuredContent`/an output schema, ensure they describe the actual envelope. Example payloads inside the MCP result:

```json
{"operation_id":"YOUR_OPERATION_ID","status":"pending","online":true,"message":"Not yet confirmed; check get_operation."}
```

```json
{"operation_id":"YOUR_OPERATION_ID","status":"completed","ok":true,"result":{"records":[],"returned_count":0,"returned_total":0,"total_count":0,"has_more":false,"next_cursor":null}}
```

`completed` alone means processing finished. Only `ok: true` plus the expected successful importer result confirms a save. Never silently cut a large original to fit the envelope: fail explicitly, preserve the source, and offer a separately authorized local retrieval path. Smaller listing pages bound discovery without changing exact originals.

## Local connector implementation sequence

The connector runs under your account on the archive computer. It needs no Desktop Commander, SSH session, model inference, or open inbound port at runtime.

1. **Configure locally.** Read absolute archive/Python paths and your gateway URL. Collect credentials through a local protected setup flow; on Windows the reference approach uses user-scoped DPAPI. Keep secrets, state, and logs outside the public checkout or in explicitly ignored local paths.
2. **Lock and poll.** Take a single-worker lock for the state directory. Make outbound HTTPS calls to the exact configured connector endpoint, with bounded reads, timeouts, and retry backoff. Refuse redirects that could carry credentials to another host.
3. **Validate locally too.** Check the received digest, types, and allowlisted action. Use a fixed dispatch table, never arbitrary `getattr`, shell commands, paths, or database statements from a network request.
4. **Dispatch reads.** Import `server_readonly.py` from the configured archive folder and call its three functions with validated named arguments. That module owns the database path and opens it read-only. Restart the connector after replacing imported Python modules.
5. **Dispatch saves through the importer.** Follow the durable save sequence below. Do not write rows directly, bypass source identity checks, or substitute the append-only `store_memory` tool for conversation import.
6. **Finish and recover.** Post the result with the lease token; retain bounded local successful-operation results to recover a lost completion response. Failures must remain retryable where appropriate. Persist a heartbeat/error timestamp without including transcript bodies or keys.
7. **Install background operation.** Generate start/stop/status commands appropriate to the actual operating system. Run as the account that owns the protected config, prevent overlapping workers, and verify restart/reboot behavior. Do not assume a logon-only task works while logged out.

Have the builder provide a real `configure.py` or equivalent, a `--once` diagnostic mode, a continuous run mode, a credential rotation procedure, and the exact paths used by the scheduled task/service. Commands depend on the generated project; documentation with placeholder commands alone is not a finished connector.

### Durable save sequence

The save tool takes a packet such as:

```json
{
  "request_key": "synthetic-client:save-001",
  "packet": {
    "format": "threadsatchel/1",
    "kind": "note",
    "title": "SYNTHETIC plugin connection check",
    "messages": [{"speaker": "user", "text": "SYNTHETIC violet-satchel connection check."}]
  }
}
```

For real saves, generate a new unique key for each intended packet, then keep that key and the exact packet on retries. Preserve known conversation/message IDs and source timestamps; omit unknown ones. Distinguish excerpts, notes, and summaries.

Implement these steps:

1. Validate the packet and cap its UTF-8 serialization (the reference uses 1 MiB). Compute a deterministic request digest and safe delivery ID; never use caller text as a filesystem path.
2. Check a permanent local receipt indexed by action/key (and owner if supporting more than one owner). A matching digest returns its previous result. A different digest fails. Serialize conflicting writes so two workers cannot race past this check.
3. Create `inbox/` if needed. Write the exact packet to a unique `.part` in that directory, flush it, verify its bytes, then atomically finalize to the deterministic delivery filename. If that final file already exists, require an exact byte match instead of overwriting it.
4. Invoke the configured Python and `import_memory.py` with an argument array and `shell=False`. Capture exit status and structured output. Exit code 0 and a valid importer result establish success; a timeout leaves the outcome uncertain.
5. Persist the successful receipt atomically before reporting success. A crash after the import but before the receipt must be recoverable by reimporting the identical file: the importer uses its existing receipts/deduplication.
6. Keep failed/uncertain content and retry identity. A gateway retry after remote cache expiry must still consult the local receipt. An unchanged packet can be retried without inventing another conversation or new IDs.

The existing optional `server.py` on main is a different append interface. Its `store_memory` does not provide this remote `save_memory` contract or conversation reconciliation.

## Staged prompts for the builder

After the initial prompt in the main guide, use these if work needs to be split between a cloud builder and a local coding task.

**Gateway task:**

```text
Build my private ThreadSatchel Sites gateway from PRIVATE_GATEWAY.md.
Implement owner authorization, the complete six-tool contract with writes
initially disabled, durable bounded operations, and a separately authenticated
poll/complete connector API. Keep tool discovery free of private data.
Test with a fake local worker and synthetic payloads. Include concurrent
request-key conflicts, stale lease completions, other-owner denial, expiry,
oversized streams, and full cursor round trips. Produce the exact connector
wire contract, configuration field names, private deployment, and runbook.
Use Sites' canonical private plugin, without a second wrapper plugin.
```

**Local task:**

```text
Build my outbound ThreadSatchel connector against the gateway contract at
[LOCAL_CONTRACT_PATH], using archive [ARCHIVE_ROOT] and Python [PYTHON_PATH].
Implement protected configuration, a single-worker lock, allowlisted reads,
and durable importer-backed saves as described in PRIVATE_GATEWAY.md.
Use a disposable archive for tests. Verify exact text, retry safety after
lost responses and process restarts, changed-content conflicts, and offline
recovery. Produce a working configuration helper and startup/status/stop
commands, then connect to my private gateway and verify a harmless read.
```

**Integration task:**

```text
Verify my deployed gateway and local connector together using the acceptance
checks in CREATE_YOUR_OWN_PLUGIN.md. Confirm read-only access first, then
enable save_memory only after the durable save tests pass. Reuse the current
private plugin, verify its discovered schemas, and give me its install link.
Record which checks actually passed in a connected chat and which still
need testing in Dot. Do not report queued operations as completed saves.
```

## Optional file handoffs

The author's installation also supports named, versioned file handoffs. They are not needed for search, inventory, Dot access, or conversation saves. If you want them, separately implement `list_handoffs`, `get_handoff`, `read_handoff_file`, and `save_handoff` with explicit versions, byte limits, hashes, and keyed retries.

Keep all files under a dedicated `handoffs/` root; reject absolute/UNC paths, traversal, symlinks, junctions, hidden staging files, and reserved filenames. Return bounded byte ranges for large files and never execute retrieved files. Preserve prior versions, index the instructions through the importer, and give the user a pickup name only after both storage and indexing are confirmed. Test interrupted writes before enabling these extra tools.

## Completion criteria

Your build is complete when your private Site is deployed, the connector restarts reliably under your account, the personal plugin is installed, and the applicable acceptance checks pass through that plugin. Record Dot verification separately. Preserve your source, dependency lockfile, migration history, and private runbook so you can maintain the integration without the original author's involvement.
