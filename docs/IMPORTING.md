# Imports, overlap, and duplicates

## Import an excerpt

```sh
python import_memory.py examples/example-excerpt.json
python import_memory.py /path/to/notes.md --kind note
python import_memory.py /path/to/export.zip
```

The database must already exist (setup_memory.py). The importer keeps original input bytes, records import receipts, and commits records plus search entries transactionally. Renamed identical files are safe to repeat. Failed imports return exit code 1; keep the original file for diagnosis.

Ordinary manual imports are limited to 32 MiB of input. ZIP inputs allow at most 10,000 entries and 128 MiB of expanded conversation JSON in total, across all selected parts; attachments are never extracted or executed. Limits apply before reading the entire input or expanding the selected ZIP members. The automatic inbox sweep keeps its stricter 8 MiB limit and does not accept ZIP files.

For a reviewed larger official export, first preserve the original and back up the archive, then make the allowance explicit. For example:

```sh
python import_memory.py /path/to/export.zip --max-file-mib 256 --max-expanded-mib 512 --max-zip-members 20000
```

The manual allowance has hard ceilings of 1 GiB for input/expansion and 100,000 ZIP entries. Raising it does not validate an unfamiliar export schema; test the actual export on an archive copy first. Oversized or invalid inputs fail without advancing receipts or deleting the source.

A threadsatchel/1 packet has kind excerpt, summary, or note, an optional title/account_id/conversation_id, and a messages list. Each message has text and optional speaker, message_id, source_order, source_date, and source_url. examples/example-excerpt.json is synthetic.

Omit IDs, dates, and source order you do not know. Never substitute local array positions for real source order. Keep message text exact. Label summaries separately. Split edits with the same message ID into separate packets.

## Extra fields and diagnostics

Additional packet and message fields are preserved unchanged as inert metadata. For example, a `provenance` or `notes` field does not reject an otherwise valid packet. Exact input bytes remain preserved. Full retrieval exposes extras inside the original packet/message metadata and separate generated `import_notes`; callers cannot overwrite those generated notices.

Results add `has_warnings` and `import_status` (`imported`, `already_present`, or `imported_with_warnings`). The `extra_fields_preserved` warning identifies the structural location and field count without copying private values into logs. The usual counts still show whether any new records were added. A warning-bearing retry can say `imported_with_warnings` while adding zero records.

The command returns safe structured `error_detail` for failures, and inbox sweep records that detail for invalid packets. Invalid required structure, empty message text, conflicting source IDs, nonfinite/deep metadata, unsafe temporary/executable filenames and size limits remain errors. Automatic inbox and personal connector credential checks may retain a packet for review. Extra metadata is not executed, promoted into authoritative IDs/dates, or inserted into original message text. Reuse identical content and the same request key when retrying a connector save. See [save results and troubleshooting](SAVE_RESULTS.md).

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

Deduplication reduces duplicate searchable records. It does not delete original source archives: raw inputs, provenance, and revisions consume space intentionally. This is not a storage compactor. `store_memory` on the optional writable MCP endpoint is append-only and does not perform conversation reconciliation. Prefer the importer for repeat-safe conversation delivery. See [the writable tool contract](CONNECTING.md) and the separately built gateway's [save protocol](PRIVATE_GATEWAY.md#durable-save-sequence).

## OpenAI ZIP status

The parser supports graph-shaped `conversations.json` or numbered parts such as `conversations-000.json` and `conversations-001.json`. Numbered parts must start at zero, be contiguous and unique, and share one directory. Mixed legacy/split layouts and encrypted files are rejected. The size allowance applies to their combined expanded bytes.

It retains current-branch text and other branch records with their recorded conversation/message IDs and timestamps. A `multimodal_text` message can contribute exactly one existing inline string or recognized `audio_transcription.text`. Text is preserved verbatim; the importer performs no speech recognition, image interpretation, pointer access, or invented joining of multiple text parts. Unsupported/ambiguous content and attachments remain in the unchanged original ZIP with warnings.

Some official account-data downloads are outer ZIPs containing a separate conversation ZIP alongside profile/billing exports. The outer wrapper is not automatically unpacked or imported. Preserve it, inspect its member list, copy only the reviewed conversation ZIP to a safe generated local filename, and import that ZIP after backup and validation on a database copy. Do not bulk-extract or execute archive contents or assume that account/billing files belong in searchable conversation memory.

A full real OpenAI conversation export (the memory dump) was successfully imported into the author's archive on September 28, 2026, after validation on a copy. It included numbered conversation parts and provided voice transcripts. A full repeat import added zero new records, and imported originals were retrieved through MCP. This validates the observed layout, not every past or future export format. Before your first full export, keep its original unchanged, make a SQLite online backup, inspect its actual schema, and validate on a copy of your archive.

“Full export imported” refers to processing the conversation export, preserving the supplied ZIP, and indexing its supported text. It does not mean every attachment, unsupported multimodal item, or outer billing/account file became searchable. Review the import warnings for the exact coverage.

Exact text shared with a prior anonymous capture can remain separately searchable when the source identity or required full sequence cannot be verified. A successful repeat-import test establishes retry safety; it does not prove that all earlier summaries or partial manual captures have been reconciled. Review such overlap without merging unrelated conversations or inventing source IDs.

## Claude imports: working packet workflow

Claude conversation material is being successfully imported. A September 29, 2026 delivery check verified nine files containing 893 records: 845 conversation messages and 48 summaries/notes. All stored text and metadata matched the delivered files, all records were indexed, and that batch had no import warnings, missing records, or held files.

The tested path uses Claude material exported or converted to `threadsatchel/1` packets. There is no native parser for an arbitrary Claude account-export ZIP. Do not pass a raw Claude ZIP to the OpenAI ZIP parser and assume it will work.

1. Obtain the actual visible messages from your Claude conversation or authorized local export. Preserve the source file. If an assistant can access only part of a chat, label the result as an excerpt and do not call it a complete transcript.
2. Convert exact visible user/assistant text to `messages`. Keep known speaker labels, IDs, dates, and conversation identity. Omit unavailable metadata rather than inventing it. Separate summaries/notes from verbatim excerpts.
3. Save UTF-8 JSON using the example below. Break large deliveries into bounded packets while preserving source identity/order where known; the automatic inbox limit is stricter than manual import.
4. For direct delivery, finalize the file using the protocol below, run `python import_memory.py /absolute/path/claude-excerpt.json`, and inspect the successful result. The automatic inbox sweep is an alternative for finalized JSON files; it does not accept ZIPs.
5. Search a distinctive phrase, retrieve its ID with `get_memory`, and compare exact text and provenance. Reimport the same file to verify no new revisions are added. A summary is not a substitute for a missing original.

Synthetic packet example:

```json
{
  "format": "threadsatchel/1",
  "kind": "excerpt",
  "title": "SYNTHETIC Claude conversation excerpt",
  "messages": [
    {"speaker": "user", "text": "SYNTHETIC: What did we decide about the archive?"},
    {"speaker": "assistant", "text": "SYNTHETIC: Keep the original text and its source."}
  ]
}
```

Copyable conversion instruction for an assistant with authorized access to the source:

```text
Export the visible conversation text you actually have as UTF-8
threadsatchel/1 excerpt packets following docs/IMPORTING.md. Preserve exact
wording and speaker labels. Keep source IDs/dates only when supplied by the
source; never fabricate them or reconstruct unseen history. Put summaries
in separate summary packets. Report gaps, omitted attachments, and which
part of the conversation was accessible. Exclude credentials and anything
I asked not to save. Validate the packets and use the existing authorized
import path, then verify exact stored text before claiming success.
```

The core importer's legacy `steve-memory/1` compatibility remains for older packets; new generic integrations should emit `threadsatchel/1`. The import check does not imply automatic access to all Claude chats.

## Safe direct delivery

Write the complete packet under inbox/.incoming-<unique-id>.json.part. Read the entire file back and verify the bytes. Atomically rename within that same directory to a new, nonexisting final .json filename. Then run python import_memory.py with that exact path and inspect exit status/output. Do not use a read-only MCP server for writes. Temporary names are explicitly rejected.

If delivery is interrupted, retry the same finalized file rather than generating another packet. The importer never executes input text or follows URLs embedded in it.
