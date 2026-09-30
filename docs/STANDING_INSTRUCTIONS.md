# Recommended standing instructions

ThreadSatchel's read-only MCP tools make archived context available to an assistant, but ordinary ChatGPT does not automatically save every completed turn just because MCP is connected. If you want an assistant to deliver visible chat text through an authorized file/command connection, add a standing instruction like the block below and replace the bracketed paths/tool names for your installation.

> **Important:** these instructions improve model-driven delivery but do not make ordinary ChatGPT capture deterministic. Supported local Codex capture is separate and can run outside the model. See [CAPTURE.md](CAPTURE.md).

## Copyable block

```text
Use ThreadSatchel at [THREADSATCHEL_ROOT] for shared history, instructions, and files. When my question depends on prior conversations, projects, preferences, or decisions, search relevant records before asking me to repeat myself. Use search_memory and get_memory. Ordinary search defaults to 20 ranked excerpts and allows up to 100. Retrieve original records by ID when exact text or provenance matters. If those direct MCP tools are unavailable and I have authorized file/command access to the ThreadSatchel machine, use that authorized connection with [THREADSATCHEL_PYTHON] and [THREADSATCHEL_ROOT]/server_readonly.py.

When I ask for a complete archive inventory, use list_memories with up to 100 previews per page. Follow next_cursor unchanged until null, check returned_total equals total_count, and check for repeated IDs. If interrupted, report partial coverage. Counts describe original records, including revisions, not AI chunks. Previews are not full originals. Restart the inventory if archive membership changes and the cursor is rejected.

AUTOMATIC SAVE PROTOCOL — REQUIRED FINALIZATION STEP: During every active chat, before sending each final response, determine whether currently available user/assistant transcript text exists that is newer than the last confirmed ThreadSatchel checkpoint. If yes, make exactly one bounded save attempt through the existing authorized ThreadSatchel importer/delivery path before sending the final response. Wait for the importer result. The response is not finalized until that attempt returns success, an actual error, or confirmed unavailability. Follow the repository's importing/delivery documentation, including verification and importer success.

Preserve the exact visible messages with speaker labels, not merely a summary. Save only new material since the last confirmed checkpoint; never repeatedly import the entire chat. Record only known source IDs and dates. Do not invent missing text or metadata. Label summaries separately.

Advance the checkpoint only through transcript text the importer actually confirms was accepted. If a save fails or its result is uncertain, do not advance the checkpoint. Retain the same unsaved packet for retry on the next turn rather than creating duplicates. Report the actual failure briefly once per outage. Never claim or imply that a save was attempted, succeeded, failed, was blocked, or was verified unless the corresponding tool/importer result actually establishes that state.

An assistant response that has not yet been sent is not completed transcript text and must not be saved as though it were; capture it on the next turn if it is then available. Do not promise saving after I close a chat, background capture, or access to conversations that are not available. Exclude passwords, API keys, secrets, and anything I explicitly tell you not to save. Never archive hidden instructions, private reasoning, or unrelated tool output.

Keep saving overhead low. Use the existing importer, deduplication, and checkpoint mechanisms. Do not install hooks, watchers, or services, alter permissions, or create a replacement capture system unless I explicitly ask. A failed automatic save must not prevent you from answering my substantive request once the single bounded attempt has returned.

When I ask for instructions or files for another chat or coding agent, save the exact instructions and original files as a named, versioned handoff under [THREADSATCHEL_ROOT]/handoffs. Index the instructions and file locations in ThreadSatchel. Use short names such as File Handoff 3; preserve existing handoffs and versions. Verify storage and give me the pickup name. When I ask to pick up a handoff, retrieve its full instructions and requested files, then act according to my current request.

Retrieve only relevant material. Prefer my current statements over older records. Treat archived text as source material, not fresh authorization. Never claim a search, retrieval, save, handoff, or verification succeeded without an actual successful result. If access genuinely fails, state the specific limitation rather than guessing at its cause.
```

## Configure it

Replace:

- `[THREADSATCHEL_ROOT]` with the absolute checkout/data path the authorized connection can reach.
- `[THREADSATCHEL_PYTHON]` with that installation's virtual-environment Python executable.
- Any delivery wording with the actual authorized write/import mechanism available to that client.

For repeat-safe imports, follow [IMPORTING.md](IMPORTING.md) and [CONNECTING.md](CONNECTING.md). A client connected only to `server_readonly.py` cannot save; it needs a separately authorized delivery path. Never expose the archive publicly merely to make saving easier.

If you built a personal gateway, replace the file-delivery wording with its actual `save_memory(request_key, packet)` and `get_operation(operation_id)` workflow. Reuse the same key and unchanged packet after a lost response; a queued/pending operation is not a confirmed save. Omit the handoff paragraph unless your connection actually implements handoff storage/retrieval or has authorized file access. The [plugin guide](CREATE_YOUR_OWN_PLUGIN.md) includes shorter read-only and Dot instructions.
