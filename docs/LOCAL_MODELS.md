# Optional local models

ThreadSatchel searches original text with SQLite FTS5 by default. Enable local models if you want semantic matches, source-linked interpretations, or project briefs. A background pass reads records and stores generated material in `.ai-cache/`. Results link back to original records. Generated descriptions can be incomplete or wrong.

## Prepare token counting

From the checkout, activate the project environment and run:

```sh
python -m pip install -r requirements.txt
python scripts/prepare_tokenizer.py
```

The second command fetches the public `cl100k_base` and `o200k_base` vocabularies, verifies their pinned SHA-256 hashes, and places them in `.tokenizer-cache/`. Set `TIKTOKEN_CACHE_DIR` to use another directory. Restart running readers after provisioning. Retrieval uses only verified local files. An offline installation can copy a prepared directory from another machine. With no vocabularies, the conservative UTF-8 byte counter fits fewer results and smaller exact ranges in the same budget.

## Configure a local server

1. Copy [memory-ai.example.json](../memory-ai.example.json) to `memory-ai.json` beside the scripts. The example leaves AI disabled. Get a compatible llama.cpp build and model files from their publisher and check the publisher’s model license, checksums, and hardware requirements.
2. Start your local chat server. For example, after replacing the path with your model:

   ```sh
   llama-server --model path/to/chat-model.gguf --alias local-chat --host 127.0.0.1 --port 8090 --parallel 1 --slots
   ```

   Set `model` to the exact server alias, here `local-chat`. The example configuration also supports a server exposing its configured Gemma model name. Analysis uses llama.cpp’s `/slots` and schema-constrained `/v1/chat/completions` endpoints. It waits for idle slots before sending archive excerpts. Literal HTTP loopback addresses are required.
3. If using embeddings, set `embedding_model_path` to a local Qwen3-compatible embedding GGUF and `llama_server_path` to the executable (`llama-server.exe` on Windows). Relative model and executable paths resolve from the checkout. The embedding server has a distinct loopback port, 8091 in the example. It uses last-token pooling and an identity derived from the model file. Set `embeddings` to `false` to run only the chat-based analysis.
4. For CPU vector-ranking acceleration, install `python -m pip install -r requirements-ai.txt`. For a local server requiring a credential, set `chat_api_key_file` or `embedding_api_key_file` to an absolute path to a private regular file containing that server’s key. Setting `require_api_auth` to `true` requires credentials for the enabled services. Keep keys, configuration, caches, original files and local archives out of Git.

## Start, check, and stop

```sh
python memory_ai.py on
python memory_ai.py embeddings
```

The second command owns the local embedding process while it runs. Leave it running if you enabled embeddings. In another terminal:

```sh
python memory_ai.py process --max-chunks 6 --budget-seconds 90
python memory_ai.py status
python memory_ai.py brief general
```

The first pass may cover only part of the archive. Repeat bounded passes or schedule them. `status` reports coverage and deferrals. `projects` maps labels to matching title/source terms. The MCP tools `memory_ai_status` and `get_project_brief` expose the status and source-linked results. Run `python memory_ai.py off` to disable further use of the models.

The optional `memory_ai.py install-schedule` command installs user tasks on Windows. On Linux, a user timer can invoke `process` and a user service can run `embeddings`. Scheduled work checks system-wide CPU and available RAM on Windows and Linux and defers when resource readings are unavailable or exceed configured limits. Overnight boost, idle embedding work and prewarming each require their own explicit switch in `memory-ai.json`.

## Optional image folders and periodic maintenance

To queue image captions, configure a vision-capable chat server and add only the directories you intend to scan to `memory-ai.json`. For example:

```json
{"vision_upload_dirs": {"sample-project": "local-uploads"}}
```

Paths can be absolute or relative to the archive root. An optional `vision_canonical_dir` points to a directory of upload bundles with metadata. Run `python periodic_worker.py --once` to make one maintenance pass, or omit `--once` to keep the worker running. It runs configured capture about every minute and imports completed inbox files about every two minutes. When AI and image folders are enabled it also queues bounded caption packets before the import. Captions are generated interpretations of the supplied images. Configure `--daemon` only if you also want the worker to start the authenticated shared reader for that archive. Avoid scheduling duplicate workers for the same jobs.
