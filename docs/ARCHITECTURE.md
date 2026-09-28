# Architecture

## Read path

server_readonly.py exposes search_memory and get_memory over stdio MCP. memory_search.py prioritizes exact IDs, phrases and all-word matches before an OR fallback, using SQLite FTS5. Search returns up to 20 compact excerpts by default; full=True returns complete records and batched provenance. get_memory always returns original text by ID.

The optional ai_memory.py layer reads the archive without modifying it. A separate disposable .ai-cache/index.sqlite3 holds chunk embeddings, validated source quotes, model interpretations, generated search questions and cached project briefs. ai_embeddings.py uses a separately installed local Qwen embedding model. Existing local chat inference supplies bounded extraction and selective reranking. get_project_brief and memory_ai_status expose cached evidence and coverage; memory_ai.py supplies explicit on/off controls and optional limited-user background processing. All AI features default off, and lexical retrieval remains available when models fail. See OPTIONAL_QWEN.md.

## Write path

setup_memory.py initializes the original memories/FTS schema and importer schema. import_memory.py validates packets, retains raw objects by SHA256, records import receipts, reconciles known identities, and commits all changes in one transaction. A failed import rolls back records and search entries. Schema migration uses a verified SQLite online backup; this is not a recurring backup service.

Entities identify sources; revisions identify versions of their text. Canonical references can link verified duplicates without destroying historic IDs or original bytes. reconcile_imports.py performs bounded-rule exact excerpt matching for complete ZIP imports; ambiguous candidates remain in import_uncertain. See IMPORTING.md for the evidence requirements and limits.

## Capture path

codex_capture.py is the original local collector; codex_capture_export.py extends it with export-only mode and is the recommended configurable entry point. Both share bounded JSONL reads and fair file iteration through capture_read.py. They use recorded source session/message IDs, byte offsets, header/edge hashes, a lock, atomic state replacement and deterministic packet hashes. Rewrites/truncation reset a source cursor; retries reuse source identity at import. A held event never advances the import checkpoint, while a separate scan cursor allows other sessions to proceed.

capture_transport.py lists queued filtered packets and acknowledges validated hashes. sync_codex_capture.py runs configured SSH commands, verifies incoming hashes, preserves received packets centrally and calls the importer. Source acknowledgement occurs only after successful commits. It does not need database write access on source machines.

install_schedule.py opts into one-minute capture/sync schedules or a two-minute inbox sweep, using Windows tasks or Linux systemd user timers. It uses ordinary user permissions and refuses to silently replace an existing job. Scheduling never invokes an AI model.

inbox_sweep.py performs one bounded pass over finalized files directly inside inbox/. It uses the existing importer and its committed receipts, keeps content out of operational logs, retains failed files, and creates a verified pre-import SQLite backup before each batch that writes data. It does not execute deposited instructions or change reconciliation rules. See INBOX_SWEEP.md.

## Formats and compatibility

The database is a local SQLite file with FTS5. MCP is pinned to the version tested by the original deployment. Python 3.12+ is the documented target. Capture relies on observed Codex JSONL structures; unsupported formats are counted/omitted rather than guessed. Real full ChatGPT ZIP validation is pending. Claude text can be supplied as threadsatchel/1 excerpts, but no native Claude export adapter is included.

## Validation

scripts/check.py runs isolated synthetic checks for MCP search/retrieval, direct delivery, file-repeat safety, revisions, concurrent imports, transactional rollback, capture cursor behavior, filtering, export queues, whole-ZIP overlap, and remote transfer acknowledgement/retry behavior. No fixtures are imported into an existing user's archive.

The public generic sync transport is tested with a local subprocess fixture. Actual SSH environments and scheduler permissions vary and must be verified during setup. The author's deployment used Windows central/laptop capture and a Linux export timer. A general macOS installation has not been tested.
