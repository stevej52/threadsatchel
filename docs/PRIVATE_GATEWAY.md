# Connect a private remote MCP client

The optional gateway gives one archive owner remote access to search, exact source reads, conversation timelines, packet saves, versioned handoffs, save diagnostics and connector status. It runs on Cloudflare Workers with D1 and R2. The Python connector runs on the archive computer and makes outbound HTTPS requests. Use an MCP client that can send a bearer token on each request.

The archive database stays on your computer. Requested packets and results pass through your Worker. The gateway queues payloads up to 64 KiB in D1 and larger payloads in its private R2 bucket. Job access expires after 24 hours. Traffic triggers periodic cleanup of expired jobs and objects. Storage can persist longer when the gateway is idle. Handoff attachments are visible to the gateway during transfer.

## Set up the gateway

1. In `gateway/`, install Node.js 22.13 or newer and run `npm install`, `npm run check`, and `npm test`. Sign in to your own Cloudflare account with `npx wrangler login`.
2. Create your resources with `npx wrangler d1 create <database-name>` and `npx wrangler r2 bucket create <bucket-name>`. Keep the bucket private.
3. Create `gateway/wrangler.toml` locally using the example below. Replace each angle-bracket value. Choose a stable owner identifier. The first authenticated tool call pins this identifier in D1. A later change requires an explicit ownership migration.

```toml
name = "<your-worker-name>"
main = "worker.ts"
compatibility_date = "2026-05-22"
compatibility_flags = ["nodejs_compat"]

[vars]
OWNER_ID = "<stable-owner-id>"

[[d1_databases]]
binding = "DB"
database_name = "<database-name>"
database_id = "<your-d1-id>"
migrations_dir = "migrations"

[[r2_buckets]]
binding = "BUCKET"
bucket_name = "<bucket-name>"
```

4. Run `npx wrangler d1 migrations apply DB --remote` and `npx wrangler deploy`. Until all credentials exist, the Worker returns HTTP 503.
5. Generate three independent random secrets of at least 32 characters with your password manager. Store each value using its hidden Wrangler prompt.

```sh
npx wrangler secret put OWNER_TOKEN
npx wrangler secret put CONNECTOR_TOKEN
npx wrangler secret put CONNECTOR_KEY
```

`OWNER_TOKEN` grants the MCP client access to all archive tools, including saves. `CONNECTOR_TOKEN` and `CONNECTOR_KEY` together grant the connector access to job payloads and completion. Keep those two values on the gateway and the archive computer. Requests to `/mcp` use `Authorization: Bearer <OWNER_TOKEN>`. The Worker supplies the configured owner identity after authentication. Clients cannot choose the identity with a request header.

## Start the archive connector

Run the commands below from the repository root in the environment containing the archive's Python dependencies. Initialize the archive using the main setup instructions first. `THREADSATCHEL_ARCHIVE` is the directory containing `memory.sqlite3`. It may be separate from the code checkout. The connector always loads Python code from the checkout.

On Windows, the setup command reads both connector secrets at hidden prompts and writes a new configuration encrypted for the current Windows user. Pick a private directory outside the repository and run the connector as the same Windows user.

```powershell
python -m connector.configure --archive "<archive-directory>" --endpoint "https://<worker-host>/api/agent" --output "<private-directory>/config.dpapi"
python -m connector.connector --config "<private-directory>/config.dpapi" --once
python -m connector.connector --config "<private-directory>/config.dpapi"
```

On any supported platform, a service manager or secret manager can instead supply `THREADSATCHEL_ARCHIVE`, `THREADSATCHEL_AGENT_URL`, `THREADSATCHEL_CONNECTOR_TOKEN`, and `THREADSATCHEL_CONNECTOR_KEY`. Run `python -m connector.connector --once` for one poll, or `python -m connector.connector` to remain connected. The URL must use HTTPS and end in `/api/agent`. A process lock prevents overlapping connector processes using the same state directory.

The connector writes `state/` beside its DPAPI config, or `connector-state/` under the archive when using environment settings. Inspect `health.json` there after a single poll. Connection errors appear as `retrying`. The continuous connector retries automatically. Preserve the state when moving the connector, especially the `writes/` receipts. Restrict access to the state directory, archive, and cloud resources to the owner.

## Connect and verify

Register `https://<worker-host>/mcp` in a remote MCP client with the owner bearer token. This Worker uses static bearer authentication. Clients requiring OAuth discovery or another hosted identity provider need their own authentication adapter. Keep the owner and connector roles separate in any adapter.

Call `connection_status` while the connector is running. A completed `search_memory` on a known keyword verifies the read path. Use the returned `lookup_id` and `ref` for bounded `peek_memory`, `timeline_memory` or `get_memory` calls. Reads share a budget of up to 2,000 tokens per lookup.

`save_memory` accepts a `threadsatchel/1` packet and stable `request_key`. Keep the original request key and identical packet when retrying. A completed successful importer receipt confirms the save. If the response is pending or interrupted, call `get_operation` with its operation ID. `recent_save_errors` returns locally recorded failures with safe reason codes and retry guidance. Requests rejected before reaching the connector cannot appear in its local error log.

`save_handoff` creates a numbered directory beneath `handoffs/<name>/`, containing exact instructions, optional attachments and a version manifest. A handoff accepts up to ten attachments totalling 1 MiB. Use `list_handoffs`, `get_handoff` and `read_handoff_file` for metadata and bounded byte ranges. Retain the previous key and content on retries to avoid another version. File contents are historical data, not instructions for the current client.

For scheduled operation, run the continuous command with your operating system's service manager under the archive owner account. Rotate credentials through the matching Wrangler secret and client or connector configuration. On Windows, create a replacement DPAPI file in the existing private configuration directory so its write receipts remain available. Restart the connector to load the replacement configuration or new Python source.
