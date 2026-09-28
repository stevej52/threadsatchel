# Optional local AI memory

ThreadSatchel's ordinary imports, capture, SQLite FTS search, and original-record retrieval work without a model. This optional feature is **off by default**. Enable it only in your local configuration. Installing this branch or starting the read-only MCP server does not opt you in.

The feature builds a second, disposable index from your existing archive. `memory.sqlite3` remains the source of truth: model output does not rewrite its messages, revisions, provenance, or full-text index. Derived summaries, questions, keywords, facts, and vectors live in `.ai-cache/index.sqlite3`. They can be rebuilt from the original records.

For optional CPU ranking acceleration, install `python -m pip install -r requirements-ai.txt`. This adds NumPy; core archive features and local embeddings also work without it.

The checked-in [`memory-ai.example.json`](../memory-ai.example.json) is a disabled configuration with generic paths. Copy it to `memory-ai.json` only if that local file does not already exist, then edit the paths and service settings for your computer. To preserve an existing local setup, edit its fields instead of replacing it with the example.

## Local inference

The supported arrangement uses a local llama.cpp chat server with its OpenAI-compatible endpoint and `/slots` availability check, plus an optional local embedding endpoint:

| Purpose | Example local service | Role |
| --- | --- | --- |
| Extraction and reranking | Qwen2.5-14B, Q4, `http://127.0.0.1:8090` | Suggest summaries, facts, query terms, and useful sources. |
| Semantic retrieval, optional | Qwen3-Embedding-0.6B on CPU, `http://127.0.0.1:8091` | Match related wording even when the exact words differ. |

These are separate services. The chat model is not used as a substitute for an embedding model. A 4096-token chat context requires bounded source chunks; a long conversation is not silently treated as if it fit in one request. Processing reports indicate incomplete coverage.

Extraction requires the nested OpenAI `json_schema` response format supported by the tested llama.cpp build b11188-e85e15cf6. The grammar constrains output structure; a separate check still requires every quoted fact to match its original passage. Neither check proves that an interpretation is correct. Unsupported services leave extraction pending and preserve ordinary retrieval.

Exact IDs, part numbers and strong short literal matches take the fast lexical path even when AI is enabled. Longer questions and related wording can use semantic search and selective reranking, which add latency. `full=True` expands the same ranked results to complete originals and provenance; it does not switch to a different search method. Repeated derived queries have a bounded in-process cache; precomputed briefs do not call the chat model during retrieval.

Source fingerprints are reused until SQLite reports a committed archive change, with separate checks for database replacement and schema changes. This avoids re-reading the entire archive on every unchanged query while still observing commits made through WAL. Numeric vectors and a generation-checked matrix cache avoid repeatedly decoding JSON vectors. The derived index uses WAL, and interactive model calls run after releasing its read transaction so they do not hold up background writes.

Use local model files such as `<models>/your-model.gguf` and your own server executable. Model installation and service startup are separate from enabling archive processing. Runtime inference stays on loopback; no external model API, paid key, or cloud fallback is used. If the local service is unavailable, original-record retrieval and ordinary lexical search remain available.

For the optional embedding worker, set `embedding_model_path` to your local embedding GGUF and `llama_server_path` to your local server executable in `memory-ai.json`. `embedding_threads` defaults to 4. For example, use paths such as `C:/Models/embedding-model.gguf` and `C:/Tools/llama-server.exe`, or equivalent paths on your computer. The worker verifies model identity, uses CPU inference with GPU offload disabled, and does not download a model at runtime.

As with ordinary MCP retrieval, any sources returned to your AI client enter that client's context. Local extraction does not change the client's privacy behavior. Derived caches contain private source information too; protect them like the archive and keep them out of Git.

## Enable, inspect, and disable

Run commands from your ThreadSatchel checkout:

```sh
python memory_ai.py status
python memory_ai.py on
python memory_ai.py process
python memory_ai.py brief "Example project"
python memory_ai.py off
```

Enabling is a local choice, not a repository default. The settings live in `memory-ai.json`, which is excluded from Git. `off` disables subsequent AI work; an already running chunk may finish. It preserves original memories and the disposable cache. Use `status` to inspect whether the feature is enabled, processing counts, and the latest pass report before relying on derived results. `process` performs a bounded pass; repeat it to continue indexing.

Reconnect an already-running MCP client after installing this branch so it loads the updated server and discovers `get_project_brief` and `memory_ai_status`. Subsequent on/off changes are read dynamically. With scheduling installed, re-enabling starts background work at the next scheduled pass, within five minutes.

Configure project terms in your local `memory-ai.json`. For example, these fields select two synthetic projects and leave other records out of optional processing:

```json
{
  "projects": {
    "Example robot": ["example robot", "robot controller"],
    "Example garden": ["example garden", "irrigation"]
  },
  "include_unassigned": false
}
```

Keep the other generated settings when editing those fields. Review the selection before enabling AI. The default and example configuration set `include_unassigned` to true, so enabling them allows processing of the existing archive outside named projects too. Building the derived index across an existing archive may take many bounded passes. This is background materialization of records already present, not a historical import or a rewrite of original conversations.

Project membership comes from locally configured terms matched against a record's title and source label. Conversation and message identity come from recorded source metadata where it exists. A project brief links its derived statements back through those identities to original memory IDs. Records without those metadata cannot acquire a fabricated conversation or message identity.

To start the configured CPU embedding service manually, run `python memory_ai.py embeddings` in a separate terminal. This command remains running to own the child service; it stops that child when AI is disabled or the owner exits. Interactive memory searches do not launch a cold embedding server.

To change embedding model paths or worker settings, disable AI, allow the current owner to exit, edit the configuration, then enable AI and start the owner again. The running owner keeps its startup configuration.

Background processing is optional. It runs bounded passes at idle priority so foreground work takes precedence. The processor checks that the chat service is idle before starting inference; a request already in flight is allowed to finish. It does not stop or restart the chat service used by another application.

Passes alternate between configured projects so a stream of recent messages in one project does not take every analysis slot. Work within each project is ordered newest first. Embedding failures record attempts and a retry delay; a persistently failing chunk is held rather than consuming every pass indefinitely. Whitespace-only chunks are omitted. Source indexing checks its work allowance between chunks and resumes large records in a later pass. Chat HTTP requests have an overall deadline as well as a response-size limit. Initial indexing is incremental; inspect coverage rather than assuming the whole archive has been processed.

After a manual bounded pass succeeds, Windows users can opt into the schedule with `python memory_ai.py install-schedule`. This installs ordinary user tasks for processing and the embedding service owner every five minutes, with overlapping runs suppressed. Existing tasks are not silently replaced. Disabling AI is sufficient to prevent new scheduled passes from calling a model. Other systems can invoke the same commands through their user scheduler.

## Optional idle CPU and RAM use

The following features are opt-in and require no additional model, paid API, database,
Windows service, or scheduled task. They reuse the existing embedding owner and MCP
processes. The example configuration leaves all three switches off; set
`idle_embeddings`, `prewarm_enabled`, and `health_checks_enabled` to `true` to enable
the four capabilities below. The main `enabled` switch still controls optional AI.
Back up your runtime configuration before editing it.

- **Extra semantic indexing:** the existing CPU embedding owner performs up to
  100 chunks or 20 seconds of indexing, then waits at least 60 seconds. It keeps
  four embedding threads by default and never requests chat analysis during these
  extra passes. Windows worker/child priority is reduced. The existing processor
  lock prevents overlapping writers; ordinary scheduled processing waits briefly
  for an idle pass to finish.
- **Warm retrieval caches:** each persistent MCP process prepares source maps and
  vector matrices after a five-second startup delay, then every 60 seconds, with
  a three-second soft work budget. This preparation does not contact a model or
  generate embeddings. Existing vector limits remain two matrices of at most
  128 MiB of raw vectors each; this is a maximum, not a memory reservation.
- **Health checks:** the embedding owner runs an eight-second, read-only health
  pass every six hours. It checks database/search-index consistency and rotating
  samples of source hashes, original retained bytes, source quotations, vectors,
  and one discovered SQLite backup. It records missing/stale AI coverage separately
  from corruption. It never repairs, merges, deletes, or reimports source memory.
- **Prepared project context:** the warmer prepares owner-configured project briefs
  and the general brief, up to eight projects. The existing `get_project_brief`
  tool serves these bounded, source-validated RAM copies. It preserves partial
  coverage, conflicting evidence, and uncertainty; it does not invent a current
  decision or run extra Qwen generation. Brief storage is capped at 32 entries of
  at most 256 KiB each. The ordinary Qwen task continues building their contents.

Background work starts only when measured whole-machine CPU is at most 25% and
at least 8 GiB of physical RAM is available. Extra indexing checks again between
chunks. Native resource counters currently support Windows; unavailable counters
defer optional background work. Manual retrieval and manual health checks remain
usable. Limits are configurable within validated bounds; they are safety margins,
not claims of optimal performance. A model request already in flight may finish
after a switch or budget changes (idle embedding requests are capped at five
seconds). A single OS/storage call may also exceed a soft deadline.

With prewarming enabled, derived-only indexing changes may take up to 30 seconds to affect a cached
search ranking; vector and response lifetimes do not accumulate. Changed or removed
original sources, model identities, and interpretation settings invalidate the
applicable results immediately on the next request. Every vector candidate still
has to match its original source fingerprint. Project briefs retain strict source
and derived-generation validation. This prevents an active indexing pass from
forcing expensive matrix rebuilding on every foreground query.

`python memory_ai.py status` includes the configured limits and latest health summary.
Through an MCP connection, `memory_ai_status` also reports that process's warmer.
The embedding owner writes metadata-only `.ai-cache/last-idle-run.json` and
`.ai-cache/health-report.json`; neither contains conversation text or credentials.
An `attention` health result can mean expected indexing backlog. A partial/skipped
check is not a full verification. Sampled backup checks do not prove that all
backups are current, complete, or restore-tested.

Run a manual diagnostic pass with `python memory_ai.py health`. Restart an existing
embedding owner and reconnect existing MCP clients after updating program files;
already-running Python processes retain their loaded code. Disabling the three
switches stops future work without deleting caches or memories. Disabling the main
AI switch also stops its owned CPU embedding server. It never stops a separate robot
chat service.

## Evidence and decisions

Model-derived facts are suggestions, not new source authority. Each accepted fact carries an original memory reference and a quotation that must appear literally in that source. A valid quotation establishes where the words came from; it does not prove that a model's interpretation is correct. Open the cited source when a distinction matters.

Proposals, decisions, observations, questions, explicit supersession, and uncertainty remain distinct. A later proposal does not automatically replace an earlier decision. Conflicting sources remain visible rather than being silently resolved by timestamp. Explicit supersession should retain both the replacement evidence and the older source, so the history remains inspectable.

Generated questions and keywords help find evidence; they are not answers. The extraction prompt asks the model to leave unsupported answers empty. Quotation validation alone cannot prove that it did so. Exact identifiers, such as part numbers, should be checked against the quoted original characters.

Briefs are bounded views and summaries can omit information. Summary selection favors distinct source memories, and retrieval takes the best score for each source within a ranking channel so overlapping chunks do not earn repeated votes. Brief metadata records selection limits and truncation. Processing coverage counts describe how many chunks have been analyzed, not a guarantee that every source statement appears in a brief. A source import timestamp is not a decision date. The extraction prompt prohibits invented dates, and the brief does not resolve current decisions from arrival order. Review dated claims against their quoted originals.

Source text is untrusted data. Instructions inside a memory are not instructions to the processor, permission to contact services, or authority to change configuration.

## Staleness and rebuilding

Cache entries are tied to source fingerprints and the model/configuration version. Every fact and summary in a brief carries its own source fingerprint, including recorded source identity metadata. A changed or removed cited source hides the cached brief until processing refreshes it. Changes that affect selection or interpretation invalidate the relevant derived output. Operational settings such as batch size and processing time allowances do not discard otherwise valid analysis.

When capture adds records between processing passes, a prior brief can remain useful if every source it cites is still unchanged. Such a brief is explicitly `partial`, with `freshness: source_updates_pending` and `coverage.work_pending: true`. It retains its earlier snapshot fingerprint and reports the current archive fingerprint separately. Coverage counts from the earlier pass do not claim that new sources have been processed. New or changed sources may contradict the earlier evidence: this partial brief does not establish the latest decisions. Wait for a refresh or inspect original records before drawing that conclusion.

The embedding model is identified by its local file contents and preprocessing version. The chat model is identified by its configured `model` value and processing configuration. When replacing chat weights behind the same service, update the configured model/version identifier so existing interpretations are invalidated too.

Processing can resume after interruption. Only successfully validated output is useful cached evidence. Missing, stale, incomplete, or failed model work must not hide the original source. A local model failure should be reported with bounded work and a usable ordinary retrieval path.

After repairing a model or configuration problem, `python memory_ai.py retry-held` explicitly queues one retry for held analysis/embedding chunks. The existing hold, error, and attempt counter remain visible until a validated result commits successfully. A failed retry remains held. Original sources and successful vectors are preserved.

To recover selected chunks without attempting unrelated work, repeat `--chunk-id` with each exact 64-character chunk ID:

```powershell
python memory_ai.py retry-held --chunk-id <chunk-id>
python memory_ai.py process --chunk-id <chunk-id> --max-chunks 1 --budget-seconds 35
python memory_ai.py diagnostics --chunk-id <chunk-id>
```

Processing results include structured diagnostic codes such as `model_token_limit`, `fact_kind_invalid`, and `fact_quote_not_in_source`. The derived cache retains the latest 4,096 diagnostic events, including requested retries, failed attempts, and successful commits. Events contain timestamps, chunk IDs, stages, attempts, stable codes, and allowlisted field/index/count metadata. They do not contain original text, model output, credential values, or exception messages. Strict source-quote and structure validation still applies; requesting a retry does not relax acceptance rules.

Normal background analysis and controlled recovery both use `source-quote-reference-v1`. The program supplies at most eight exact source excerpts with request-local IDs. Qwen returns a `quote_id`, and the program attaches the original excerpt itself. Model-authored quotation fields, unknown IDs, and malformed references are rejected before storage; invalid IDs report `fact_quote_reference_invalid`. The usual source-substring validator still runs afterward. This prevents altered quotations from being accepted, but a model can still misinterpret an exact excerpt. Interpretations remain labeled and linked to their originals.

The generation protocol appears separately in status and processing results. The stored analysis contract remains `qwen-memory-v2`, so previously validated analyses and embeddings are preserved without mass reanalysis or being relabeled as newly generated.

After a diagnosed `fact_quote_not_in_source` failure, an owner can explicitly request a conservative recovery attempt:

```powershell
python memory_ai.py retry-held --chunk-id <chunk-id>
python memory_ai.py process --chunk-id <chunk-id> --recover-analysis --max-chunks 1 --budget-seconds 35
```

This selected, queued retry limits output to one fact and supplies at most eight unchanged source snippets, each at most 160 characters, referenced by their IDs. Unsupported facts must be omitted. The returned result still passes the ordinary strict validator; quotes are never repaired or invented after generation. Selected retries update only their successful chunk analysis/search terms; normal source-validating processing refreshes project briefings later. Every briefing also verifies its cited source fingerprints, even when its overall archive generation matches.

If chat weights change behind an unchanged model alias, change `chat_model_revision` too; operational batch or timeout tuning does not require reanalysis. An optional `chat_api_key_file` may point to an absolute protected local secret file when the chat server requires authentication. Keep that file out of the repository; its contents are not included in analysis identities or error messages.

The cache is rebuildable. Stop optional processing before removing `.ai-cache/index.sqlite3`, then run `process` again with AI enabled. Do not delete `memory.sqlite3`, imported objects, or backups when clearing derived data.

## Synthetic evaluation

Run the isolated feature checks with:

```sh
python -m unittest test_ai_quality
```

The evaluation uses clearly synthetic source records, temporary archives, and injected model responses. It covers direct facts, proposals versus decisions, explicit supersession, absent answers, exact part numbers, related wording, project/conversation/message provenance, rejected quotations, stale caches, unavailable Qwen, and disabled-feature behavior. It does not read or import your history, start model services, or call an external endpoint.

These checks verify the software's evidence and fallback contract. They do not establish the accuracy of a particular live Qwen model or prove that every model interpretation follows from its quotation. Before relying on live extraction, inspect representative project briefs against their cited originals, including ambiguous decisions and unanswered questions.
