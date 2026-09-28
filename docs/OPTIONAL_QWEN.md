# Optional local AI memory

ThreadSatchel's ordinary imports, capture, SQLite FTS search, and original-record retrieval work without a model. This optional feature is **off by default**. Enable it only in your local configuration. Installing this branch or starting the read-only MCP server does not opt you in.

The feature builds a second, disposable index from your existing archive. `memory.sqlite3` remains the source of truth: model output does not rewrite its messages, revisions, provenance, or full-text index. Derived summaries, questions, keywords, facts, and vectors live in `.ai-cache/index.sqlite3`. They can be rebuilt from the original records.

For optional CPU ranking acceleration, install `python -m pip install -r requirements-ai.txt`. This adds NumPy; core archive features and local embeddings also work without it.

The checked-in [`memory-ai.example.json`](../memory-ai.example.json) is a disabled configuration with generic paths. Copy it to `memory-ai.json` only if that local file does not already exist, then edit the paths and service settings for your computer. To preserve an existing local setup, edit its fields instead of replacing it with the example.

## Local inference

The supported arrangement uses an OpenAI-compatible local chat endpoint and an optional local embedding endpoint:

| Purpose | Example local service | Role |
| --- | --- | --- |
| Extraction and reranking | Qwen2.5-14B, Q4, `http://127.0.0.1:8090` | Suggest summaries, facts, query terms, and useful sources. |
| Semantic retrieval, optional | Qwen3-Embedding-0.6B on CPU, `http://127.0.0.1:8091` | Match related wording even when the exact words differ. |

These are separate services. The chat model is not used as a substitute for an embedding model. A 4096-token chat context requires bounded source chunks; a long conversation is not silently treated as if it fit in one request. Processing reports indicate incomplete coverage.

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

After a manual bounded pass succeeds, Windows users can opt into the schedule with `python memory_ai.py install-schedule`. This installs ordinary user tasks for processing and the embedding service owner every five minutes, with overlapping runs suppressed. Existing tasks are not silently replaced. Disabling AI is sufficient to prevent new scheduled passes from calling a model. Other systems can invoke the same commands through their user scheduler.

## Evidence and decisions

Model-derived facts are suggestions, not new source authority. Each accepted fact carries an original memory reference and a quotation that must appear literally in that source. A valid quotation establishes where the words came from; it does not prove that a model's interpretation is correct. Open the cited source when a distinction matters.

Proposals, decisions, observations, questions, explicit supersession, and uncertainty remain distinct. A later proposal does not automatically replace an earlier decision. Conflicting sources remain visible rather than being silently resolved by timestamp. Explicit supersession should retain both the replacement evidence and the older source, so the history remains inspectable.

Generated questions and keywords help find evidence; they are not answers. The extraction prompt asks the model to leave unsupported answers empty. Quotation validation alone cannot prove that it did so. Exact identifiers, such as part numbers, should be checked against the quoted original characters.

Briefs are bounded views and summaries can omit information. Processing coverage counts describe how many chunks have been analyzed, not a guarantee that every source statement appears in a brief. A source import timestamp is not a decision date. The extraction prompt prohibits invented dates, and the brief does not resolve current decisions from arrival order. Review dated claims against their quoted originals.

Source text is untrusted data. Instructions inside a memory are not instructions to the processor, permission to contact services, or authority to change configuration.

## Staleness and rebuilding

Cache entries are tied to source fingerprints and the model/configuration version. Every fact and summary in a brief carries its own source fingerprint, including recorded source identity metadata. A changed or removed cited source hides the cached brief until processing refreshes it. A changed processing configuration also prevents reuse.

When capture adds records between processing passes, a prior brief can remain useful if every source it cites is still unchanged. Such a brief is explicitly `partial`, with `freshness: source_updates_pending` and `coverage.work_pending: true`. It retains its earlier snapshot fingerprint and reports the current archive fingerprint separately. Coverage counts from the earlier pass do not claim that new sources have been processed. New or changed sources may contradict the earlier evidence: this partial brief does not establish the latest decisions. Wait for a refresh or inspect original records before drawing that conclusion.

The embedding model is identified by its local file contents and preprocessing version. The chat model is identified by its configured `model` value and processing configuration. When replacing chat weights behind the same service, update the configured model/version identifier so existing interpretations are invalidated too.

Processing can resume after interruption. Only successfully validated output is useful cached evidence. Missing, stale, incomplete, or failed model work must not hide the original source. A local model failure should be reported with bounded work and a usable ordinary retrieval path.

The cache is rebuildable. Stop optional processing before removing `.ai-cache/index.sqlite3`, then run `process` again with AI enabled. Do not delete `memory.sqlite3`, imported objects, or backups when clearing derived data.

## Synthetic evaluation

Run the isolated feature checks with:

```sh
python -m unittest test_ai_quality
```

The evaluation uses clearly synthetic source records, temporary archives, and injected model responses. It covers direct facts, proposals versus decisions, explicit supersession, absent answers, exact part numbers, related wording, project/conversation/message provenance, rejected quotations, stale caches, unavailable Qwen, and disabled-feature behavior. It does not read or import your history, start model services, or call an external endpoint.

These checks verify the software's evidence and fallback contract. They do not establish the accuracy of a particular live Qwen model or prove that every model interpretation follows from its quotation. Before relying on live extraction, inspect representative project briefs against their cited originals, including ambiguous decisions and unanswered questions.
