# ThreadSatchel

ThreadSatchel gives an assistant a searchable local archive of conversations, notes, and versioned handoffs. It keeps the original text and can return a few lines of evidence without putting an entire conversation into the assistant’s context.

**[Read the brief handoff](HANDOFF.md)** for a plain-language explanation of what gets saved, how answers are found, and what happens after a restore.

## Try it locally

Use Python 3.12 or newer with SQLite FTS5. In an activated virtual environment, run:

```sh
python -m pip install -r requirements.txt
python setup_memory.py
python import_memory.py examples/example-excerpt.json
```

Configure your MCP client to launch that environment’s Python with the absolute path to `server_readonly.py`. Ask it to find “sample archive” and show the matching passage. The archive works offline. To enable more precise token counts, run `python scripts/prepare_tokenizer.py` once with network access. Without local dictionaries the reader uses a conservative byte bound. [Client configuration and tool contract](docs/CONNECTING.md)

## What you can use

- **Search and read.** Search returns ranked previews with short references. Look at the surrounding exchange, peek at a match, or fetch an exact block or character range. The tools share a budget when you pass the same lookup ID. [Retrieval and inventory](docs/CONNECTING.md)
- **Capture and import.** Import text, Markdown, structured conversation packets, and supported OpenAI conversation exports. Local Codex capture and SSH queue sync are optional. An assistant needs an authorized delivery path to save an ordinary chat. [Import formats](docs/IMPORTING.md) · [Capture](docs/CAPTURE.md) · [Inbox delivery](docs/INBOX_SWEEP.md)
- **Save and share handoffs.** Use the separate save endpoint for receipt-backed notes and named, versioned handoffs. Remote clients can use a self-hosted authenticated gateway with an outbound archive connector. [Connection options](docs/CREATE_YOUR_OWN_PLUGIN.md) · [Save results](docs/SAVE_RESULTS.md)
- **Optional local models.** Enable semantic search, background summaries, project briefs, or captioning of explicitly selected image folders. The default installation runs without a model. [Local models](docs/LOCAL_MODELS.md)
- **Back up and recover.** The inbox sweep makes verified hourly pre-import snapshots as needed. A separate scheduled job can copy verified backups to a destination you choose. Inbox replay supports file-by-file review. [Backups and recovery](docs/BACKUPS.md)

## Existing installations

Back up your archive before updating. Existing clients that consume the old list of full search results need to read `hits` from the response and use `lookup_id` with the selected `ref`. The new default is ten search candidates, up to twenty on request, subject to the response budget. Even `full=True` respects that budget. Restart the server and refresh your client’s tool definitions after updating. [Current read contract](docs/CONNECTING.md)

Run `python scripts/check.py` for the synthetic Python release checks. The gateway has a separate test suite. [Architecture](docs/ARCHITECTURE.md) · [Privacy](docs/PRIVACY.md) · [Gateway](docs/PRIVATE_GATEWAY.md)

MIT licensed. ThreadSatchel is an independent project.
