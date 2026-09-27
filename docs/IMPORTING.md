# Imports, overlap, and duplicates

## Import an excerpt

```sh
python import_memory.py examples/example-excerpt.json
python import_memory.py /path/to/notes.md --kind note
python import_memory.py /path/to/export.zip
```

The database must already exist (setup_memory.py). The importer keeps original input bytes, records import receipts, and commits records plus search entries transactionally. Renamed identical files are safe to repeat. Failed imports return exit code 1; keep the original file for diagnosis.

A threadsatchel/1 packet has kind excerpt, summary, or note, an optional title/account_id/conversation_id, and a messages list. Each message has text and optional speaker, message_id, source_order, source_date, and source_url. examples/example-excerpt.json is synthetic.

Omit IDs, dates, and source order you do not know. Never substitute local array positions for real source order. Keep message text exact. Label summaries separately. Split edits with the same message ID into separate packets.

## Duplicate protection

| Situation | Behavior |
| --- | --- |
| Exact same packet, even renamed | No additional searchable message |
| Same known account/conversation/message ID and same text | Reuses the existing revision |
| Same source identity, edited text | Preserves a new revision |
| Different known conversation IDs | Keeps distinct source records |
| Identified conversation with a unique exact multi-message overlap | Can reconcile excerpts within that scope |
| Anonymous excerpt followed by a complete supported ZIP | Conservative exact-sequence reconciliation |
| Short repeated phrases or ambiguous matches | Retains separately; ambiguous candidates are recorded for review |
| Paraphrased notes or summaries | No semantic deduplication |

For anonymous-to-export reconciliation, the excerpt must contain at least two messages and 200 total characters, with matching speakers and nonconflicting IDs/accounts, and match only one known source sequence. The check happens after all packets in that ZIP have been imported. It is evidence-based matching, not proof about unseen future data; originals and IDs remain available for audit.

The importer reports added_revisions, reused_messages, repeated_packets, linked_entities, uncertain, warnings, and (when reconciliation runs) ambiguous_excerpt_packets. Review warnings and ambiguous cases; do not interpret them as fully resolved coverage.

Deduplication reduces duplicate searchable records. It does not delete original source archives: raw inputs, provenance, and revisions consume space intentionally. This is not a storage compactor. store_memory on the optional writable MCP endpoint is append-only and bypasses these importer protections.

## OpenAI ZIP status

The parser supports a graph-shaped conversations.json with text parts, current branch traversal and other branch records. Original ZIP bytes are retained. Unsupported/nontext content is retained only in the original ZIP and reported. Multiple text parts are not joined with an invented separator.

A real full OpenAI export has not yet been validated. Before your first full export, keep its original unchanged, make a SQLite online backup, inspect its actual schema, and validate on a copy of your archive. Synthetic tests are not a guarantee that all current/future export formats work. There is no native Claude-export parser; convert authorized excerpts to the documented packet format.

## Safe direct delivery

Write the complete packet under inbox/.incoming-<unique-id>.json.part. Read the entire file back and verify the bytes. Atomically rename within that same directory to a new, nonexisting final .json filename. Then run python import_memory.py with that exact path and inspect exit status/output. Do not use a read-only MCP server for writes. Temporary names are explicitly rejected.

If delivery is interrupted, retry the same finalized file rather than generating another packet. The importer never executes input text or follows URLs embedded in it.
