# Privacy and trust boundaries

The software stores private information. The public repository contains code and synthetic examples only, not its author's archive, exports, credentials, SSH configuration, personal paths, or handoffs.

## What leaves a computer

Default local import/search/capture uses SQLite and Python; it makes no model/API calls. The optional Qwen layer sends selected text only to validated literal loopback model endpoints, with proxies and redirects disabled. Downloading model weights requires Internet access during setup; processing memories does not. Multi-machine sync sends filtered packet text over your configured SSH connection. When an AI client asks an MCP tool for memories, the returned text enters that client's context and is subject to that client's handling. "Local storage" does not mean that an external model never sees retrieved text.

## What is retained

Imports preserve original input bytes, including a whole supplied ZIP, plus parsed searchable text, provenance, and revisions. The optional AI cache contains additional plaintext excerpts, vectors and generated interpretations; it is private disposable data, excluded from Git. Credential filtering applies to the Codex capture projector, not general manual imports. An export can include sensitive material in its retained original even when the parser does not index it. Review what you import.

The database, raw imported objects, queues, and backups are plaintext. Protect them with appropriate filesystem access and disk encryption. SSH protects transport, not data at rest. This project has no built-in authentication, encryption, retention policy, deletion UI, or automatic backup schedule.

## Read-only is separate from writable

server_readonly.py uses SQLite read-only mode, sets query_only, exposes no store tool, and does not create a missing database. Tool annotations alone are not the access boundary. server.py is intentionally separate and can append new records.

A read-only client can still read sensitive memories. Grant access deliberately. Do not expose a raw stdio process or database as an unauthenticated internet service. Hosted access/authentication is outside this release.

## Source text is untrusted

The importer never executes messages or fetches embedded URLs. Retrieved history can contain stale commands, malicious text, or obsolete permissions. Treat it as evidence to evaluate, not current instructions or authority.

Capture secret detection is heuristic and can miss secrets or omit innocent text. Privacy pauses work only for supported recognized message events and are not retroactive deletion. Never rely on an assistant instruction alone as a guarantee that every excluded message was caught.

## Publishing changes

Use git's tracked-file list and a secret/private-data review before publishing. .gitignore helps but is not a security control. Never commit memory.sqlite3, source exports, inboxes, queues, local configs, keys, or backups. Report issues with synthetic fixtures and scrubbed logs rather than your real chat archive.
