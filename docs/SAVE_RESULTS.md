# Save results, extra fields, and recovery

ThreadSatchel imports conversation packets into the existing local archive. A valid packet may contain extra packet or message fields. For example:

```json
{
  "format": "threadsatchel/1",
  "kind": "note",
  "provenance": {"capture_method": "synthetic example"},
  "messages": [
    {"text": "SYNTHETIC: the copper kite has six ribbons.", "notes": {"reviewed": false}}
  ]
}
```

`provenance` and the message's `notes` are additional fields. Their names and values remain in the preserved packet/item metadata. The complete original input bytes are also retained, and the message text is unchanged. The importer does not execute extra values, follow paths or URLs in them, promote them into source IDs/dates, or append them to searchable message text. Never include credentials in packets.

## Read a successful result

| Field | Meaning |
| --- | --- |
| `import_status: "imported"` | The transaction committed and added records or linked verified source entities. |
| `import_status: "already_present"` | The transaction committed without adding or linking records. Read the reuse/repeat counts. |
| `import_status: "imported_with_warnings"` | The transaction committed with notices. This can also describe an unchanged repeat; inspect the counts. |
| `has_warnings` and `warnings` | Whether notices exist and their safe details. Warnings are not a claim that every source item was indexed. |
| `added_revisions`, `reused_messages`, `repeated_packets`, `linked_entities` | What the existing importer added, reused, recognized, or reconciled. |
| `uncertain` | Source matches that remain unresolved; success does not mean every overlap was merged. |

An additional-field notice uses code `extra_fields_preserved`, a location such as `$` or `$.messages[0]`, and `field_count`. Notices omit private extra field names and values. They are recorded in the existing import warning table together with the transaction.

The importer metadata layer exposes the supplied fields in `import_metadata.provenance[].packet` and `.message`. Generated notices appear separately in that provenance entry's `import_notes`, with `import_status: "imported_with_warnings"`. An incoming field also named `import_notes` stays inside its packet/message and cannot overwrite those generated notes. Compact search results do not include this full metadata.

## What still fails

Malformed JSON, empty or invalid message text, unsupported formats/kinds, invalid known identity/order fields, unsupported files, temporary filenames, and size violations still fail. NaN/Infinity and metadata nesting beyond 100 levels are rejected. Additional fields do not bypass those checks or the delivery transport's own size/path restrictions. The original failed file remains available for diagnosis; it has not been confirmed as stored searchable memory.

The importer CLI exits with code 1 on failure. Its JSON retains the `error` string and adds `error_detail`, containing a stable code, fixed safe message, and a structural location or JSON line/column when available. For example, `invalid_text` at `$.messages[0].text` identifies a required-field failure without copying conversation text into a log. Other codes distinguish invalid metadata/JSON, input limits, changed input bytes, access failures, and database failures. Unexpected exceptions receive a safe generic explanation.

## Retry safely

For direct imports, rerun the same completed file with the same import options. Its original bytes, packet identity, and existing reconciliation rules make exact repeats safe. Do not rewrite valid content merely because an acknowledgment was lost.

For the remote gateway, reuse the same request key and unchanged packet, then inspect that operation and its durable importer receipt. The gateway must reject changed content under an existing key. An accepted or queued request is not proof of local persistence. Confirm a successful importer result and retrieve an expected record before declaring delivery complete.

Additional metadata is part of the packet hash. Changing metadata on an anonymous packet is not an exact retry and may create a separate source record. Existing known source IDs and verified sequence rules remain authoritative; the importer does not semantically merge arbitrary extras, summaries, or guesses about missing IDs. See [imports and duplicates](IMPORTING.md).

## Remote delivery

The included outbound connector records operations, retries, confirmed importer receipts, and bounded diagnostic details. The authenticated gateway exposes operation status and recent save errors. See [gateway setup](PRIVATE_GATEWAY.md) for its configuration and trust boundaries.
