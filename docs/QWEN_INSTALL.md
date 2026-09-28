# Install the optional Qwen helper

This guide is for `feature/optional-qwen-memory`. The ordinary ThreadSatchel archive does not need Qwen. AI is disabled until you explicitly turn it on, and no model is downloaded automatically.

The optional helper reads your existing archive and writes derived material to `.ai-cache/index.sqlite3`. It does not replace the importer, change original messages, or move the archive to a cloud service. See [behavior and evidence limits](OPTIONAL_QWEN.md) and [privacy](PRIVACY.md).

## Requirements and tested configuration

| Item | Reference configuration |
| --- | --- |
| Core software | Python 3.12+ with SQLite FTS5; `requirements.txt` installs the pinned MCP library |
| Model runtime | llama.cpp **b11188 / e85e15cf6**; prebuilt Windows x64 Vulkan package |
| Chat model | Qwen2.5-14B-Instruct, **Q4_K_M**, 4096-token context, one request slot |
| Embedding model | **Qwen3-Embedding-0.6B-Q8_0.gguf**, CPU only, four threads |
| Optional CPU acceleration | NumPy from `requirements-ai.txt`; not required for embeddings or core operation |
| Tested computer | Windows, Intel i9-7920X (12 cores / 24 threads), 64 GiB RAM, AMD RX 9060 XT (16 GiB VRAM) |

That computer is a tested reference, **not a minimum specification**. Model weights require about 9 GB for chat plus 610 MiB for embeddings on disk; leave additional space for the runtime, your archive, backups, derived index, and download staging. RAM/VRAM use also includes model context and runtime buffers. A 14B model is a substantial optional workload; there is no validated low-memory hardware minimum for this project yet.

Vulkan needs a compatible graphics driver. The prebuilt runtime does not require installing a compiler or Vulkan SDK. llama.cpp also offers CPU and other GPU builds, but those combinations have not been validated here as a complete ThreadSatchel installation. CPU-only 14B chat may exceed the default bounded response times. Changing to a smaller model requires testing extraction quality and server compatibility; it is not a documented drop-in guarantee.

**What we added:** integration code, a separate small embedding model, optional NumPy acceleration, source-validated derived analysis, hybrid retrieval, caching, and bounded background work. **Qwen's weights were not modified or fine-tuned.** No Ollama, PyTorch, paid API key, separate reranker model, or external vector database is required.

## 1. Get the optional branch and core dependencies

These PowerShell examples use a **new checkout** under your Windows user profile. Git and Python 3.12+ must already be installed. If `py -3.12` is unavailable, substitute the full path to your installed Python 3.12+ executable.

```powershell
$ErrorActionPreference = 'Stop'
$checkout = Join-Path $env:USERPROFILE 'ThreadSatchel'
if (Test-Path -LiteralPath $checkout) { throw 'Use a new directory; do not overwrite an existing installation.' }
git clone --branch feature/optional-qwen-memory --single-branch https://github.com/stevej52/threadsatchel.git $checkout
if ($LASTEXITCODE -ne 0) { throw 'Clone failed.' }
Set-Location $checkout
py -3.12 -m venv .venv
& ./.venv/Scripts/python.exe -m pip install -r requirements.txt
& ./.venv/Scripts/python.exe -m pip install -r requirements-ai.txt
& ./.venv/Scripts/python.exe setup_memory.py
& ./.venv/Scripts/python.exe import_memory.py examples/example-excerpt.json
```

NumPy installation is optional; omitting the `requirements-ai.txt` line retains a slower pure-Python ranking fallback. Using the venv executable directly avoids changing PowerShell execution policy.

For an **existing archive**, back up the SQLite database using SQLite's backup facility and preserve local configuration/raw objects before updating. Do not overwrite an existing checkout, rerun initial setup over working configuration, or copy only a live database file while ignoring its WAL. Each checkout uses the database beside its own source files; a fresh clone does not automatically point at your existing archive. Test a new checkout with the synthetic example first. Keep `memory.sqlite3`, `.ai-cache`, `memory-ai.json`, model files, source packets, and secrets out of Git.

## 2. Install llama.cpp and the two models

### Runtime

Download the pinned [llama.cpp b11188 release](https://github.com/ggml-org/llama.cpp/releases/tag/b11188), specifically the [Windows x64 Vulkan ZIP](https://github.com/ggml-org/llama.cpp/releases/download/b11188/llama-b11188-bin-win-vulkan-x64.zip) for the reference setup. Extract the **whole ZIP together**, including DLLs, into a user-writable directory such as:

```text
C:/Users/YOUR_NAME/AppData/Local/ThreadSatchel/llama-b11188/
```

Locate `llama-server.exe` inside the extracted directory; the archive may contain a nested folder. Do not copy the executable alone. Later examples assume it is directly under the directory above; adjust them if necessary. Confirm the runtime without loading a model:

```powershell
$runtime = Join-Path $env:LOCALAPPDATA 'ThreadSatchel/llama-b11188'
& (Join-Path $runtime 'llama-server.exe') --version
```

The tested output identifies build **11188**, commit **e85e15cf6**. Prefer this known version when reproducing the setup. Upstream releases can change flags and response behavior.

### Model downloads with SHA-256 verification

Run this from your new checkout. It downloads public model files from the official Qwen repositories, writes incomplete downloads to `.download` files, verifies SHA-256, and refuses to replace a mismatching existing final file. Approximately 9.6 GB will be downloaded. No Hugging Face token is required for these public files.

```powershell
$ErrorActionPreference = 'Stop'
$modelDir = Join-Path (Get-Location) 'models'
New-Item -ItemType Directory -Path $modelDir -Force | Out-Null
$chatBase = 'https://huggingface.co/Qwen/Qwen2.5-14B-Instruct-GGUF/resolve/2b6a96d780143b4e8e3b970394e39e3774551f29'
$embeddingBase = 'https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/370f27d7550e0def9b39c1f16d3fbaa13aa67728'
$downloads = @(
    @{ Name='qwen2.5-14b-instruct-q4_k_m-00001-of-00003.gguf'; Base=$chatBase; SHA256='a09ea5e7b1eafb1b30b241726c3cc3c905c96f14ad41e246ffa5f44e53904f68' },
    @{ Name='qwen2.5-14b-instruct-q4_k_m-00002-of-00003.gguf'; Base=$chatBase; SHA256='21b9457d079680d284e90ef69607c4b2d8ef64a09d4729cb7b5e1357bdba41ae' },
    @{ Name='qwen2.5-14b-instruct-q4_k_m-00003-of-00003.gguf'; Base=$chatBase; SHA256='c8d37006760a387a35216e070e6664d7da927f10be8eb870fef2e3d4833d9976' },
    @{ Name='Qwen3-Embedding-0.6B-Q8_0.gguf'; Base=$embeddingBase; SHA256='06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439' }
)
foreach ($item in $downloads) {
    $target = Join-Path $modelDir $item.Name
    if (Test-Path -LiteralPath $target) {
        if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $item.SHA256) {
            throw "Existing model has a different hash: $($item.Name). Inspect it before replacing anything."
        }
        continue
    }
    $partial = $target + '.download'
    & curl.exe --fail --location --retry 3 --output $partial ($item.Base + '/' + $item.Name)
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $($item.Name)" }
    if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -ne $item.SHA256) {
        throw "Download checksum mismatch: $($item.Name). File retained for inspection."
    }
    Move-Item -LiteralPath $partial -Destination $target
}
```

Keep **all three chat shards with their original names in the same directory**. llama.cpp loads the siblings when given the first shard. The official [chat model card](https://huggingface.co/Qwen/Qwen2.5-14B-Instruct-GGUF) also documents merging shards if needed; merging requires roughly another 9 GB temporarily. The [embedding file and checksum](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/blob/370f27d7550e0def9b39c1f16d3fbaa13aa67728/Qwen3-Embedding-0.6B-Q8_0.gguf) match the embedding artifact tested here exactly.

**Validation boundary:** the working reference computer reused an existing single-file Qwen2.5-14B Q4_K_M chat model whose original download provenance was not recorded. The pinned official three-shard distribution above is an upstream-sourced installation of the same model family/quantization; it was verified against upstream metadata but has not been downloaded and run as a fresh full installation in this documentation update. We do not claim byte-identical chat artifacts or universal hardware validation.

## 3. Start chat and configure ThreadSatchel

In a dedicated PowerShell terminal, start the chat server and leave it running:

```powershell
Set-Location (Join-Path $env:USERPROFILE 'ThreadSatchel')
$runtime = Join-Path $env:LOCALAPPDATA 'ThreadSatchel/llama-b11188'
$chatModel = Join-Path (Get-Location) 'models/qwen2.5-14b-instruct-q4_k_m-00001-of-00003.gguf'
& (Join-Path $runtime 'llama-server.exe') --model $chatModel --alias Qwen2.5-14B-Instruct --host 127.0.0.1 --port 8090 --ctx-size 4096 --parallel 1 --n-gpu-layers 99 --slots --offline --no-webui
```

This requests GPU offload for the chat model; check the server's startup output to confirm your driver/device and that the model fits. The embedding owner separately disables GPU offload. Bind to `127.0.0.1`; no public port or firewall change is needed. If you already run a compatible local chat server, reuse it and adapt the endpoint/alias instead of starting another service on an occupied port.

In another terminal, wait for model loading to finish and check:

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8090/health' -TimeoutSec 5
Invoke-RestMethod -Uri 'http://127.0.0.1:8090/slots' -TimeoutSec 5
```

Health should be ready; `/slots` must return a nonempty list with `is_processing` values. All reported slots must be idle before ThreadSatchel starts a chat request. ThreadSatchel uses this advisory check before chat requests; it does not reserve the GPU or preempt another application.

The server must support `/v1/chat/completions` and nested OpenAI `response_format: {"type":"json_schema","json_schema":{"name":"...","strict":true,"schema":{...}}}`. The [pinned server documentation](https://github.com/ggml-org/llama.cpp/blob/b11188/tools/server/README.md) describes these interfaces. A generic claim of OpenAI compatibility is insufficient; other servers, including Ollama/LM Studio, have not been validated as substitutes here.

Create a **new disabled** local configuration. This snippet refuses to overwrite existing settings:

```powershell
Set-Location (Join-Path $env:USERPROFILE 'ThreadSatchel')
$configPath = Join-Path (Get-Location) 'memory-ai.json'
if (Test-Path -LiteralPath $configPath) { throw 'Existing configuration: back it up and edit it instead.' }
$config = Get-Content -LiteralPath './memory-ai.example.json' -Raw | ConvertFrom-Json
$config.embedding_model_path = Join-Path (Get-Location) 'models/Qwen3-Embedding-0.6B-Q8_0.gguf'
$config.llama_server_path = Join-Path $env:LOCALAPPDATA 'ThreadSatchel/llama-b11188/llama-server.exe'
$config | Add-Member -NotePropertyName chat_model_revision -NotePropertyValue 'qwen2.5-14b-q4-k-m-official-2b6a96d'
[IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
```

Review the generated file before enabling. `endpoint` is a literal-loopback base URL, **without `/v1`**, and `model` must match `--alias`. Adjust `llama_server_path` if the extracted ZIP placed the executable in a subfolder. `include_unassigned: true` permits processing the entire existing archive. To restrict selection, configure `projects` and set `include_unassigned: false`; project terms match source labels and titles, as explained in [project selection](OPTIONAL_QWEN.md#enable-inspect-and-disable).

If using different chat weights, set a descriptive `chat_model_revision`; change it again whenever replacing weights behind the same alias. The embedding model's identity is computed from its file contents automatically.

## 4. Enable and test in the right order

From your checkout, with the chat server already ready:

```powershell
& ./.venv/Scripts/python.exe memory_ai.py on
& ./.venv/Scripts/python.exe memory_ai.py status
& ./.venv/Scripts/python.exe memory_ai.py embeddings
```

The final command **stays running** in that terminal. It owns a separate CPU-only llama.cpp child at `127.0.0.1:8091`. Keep it running while testing. It uses four CPU threads by default, a 2048-token embedding context, and its own fingerprint-based service alias. Do not manually start an arbitrary embedding server on that port. Neither `process` nor interactive searches start a missing embedding service.

In a third terminal, run a bounded pass after the embedding service is ready:

```powershell
Set-Location (Join-Path $env:USERPROFILE 'ThreadSatchel')
Invoke-RestMethod -Uri 'http://127.0.0.1:8091/v1/models' -TimeoutSec 5
```

Wait for this to succeed and show a `data` entry whose model ID starts with `threadsatchel-`. A loading/connection error means the service is not ready yet; the processing client also checks its exact expected identity. Then run:

```powershell
& ./.venv/Scripts/python.exe memory_ai.py process
& ./.venv/Scripts/python.exe memory_ai.py status
& ./.venv/Scripts/python.exe memory_ai.py brief general
```

Read the JSON results, including coverage, errors, held work, and deferred states. A successful process exit alone is not proof that every chunk was analyzed. `process` normally allows an initial CPU embedding pass followed by a bounded analysis pass (defaults: six analysis chunks / 90 seconds, plus the initial embedding allowance). Large archives need many passes. The synthetic example should produce source-linked derived output; inspect its quoted originals. A short exact-match search may correctly bypass Qwen entirely.

Connect or reconnect your local MCP client using [CONNECTING.md](CONNECTING.md). Confirm all four tools:

- `search_memory`: ask for **sample archive**; results should point to the imported originals.
- `get_memory`: retrieve one returned ID and verify the synthetic text and provenance.
- `get_project_brief`: request `general` and inspect coverage and source links.
- `memory_ai_status`: inspect enablement, processing status, and any worker/cache reports.

No ChatGPT phone/web integration is installed by these steps. Your particular client still needs a supported, explicitly authorized connection to the tools; a URL or these files alone does not provide one.

## 5. Optional scheduling and spare CPU/RAM

Only after manual processing works, Windows users can install the existing limited-user tasks:

First close the manually started embedding owner with Ctrl+C in its terminal, so the scheduled task can take ownership immediately. Only its child is stopped. Keep the independently managed chat server running.

```powershell
& ./.venv/Scripts/python.exe memory_ai.py install-schedule
```

This creates `ThreadSatchel-AIEmbeddings` (owns the persistent CPU service) and `ThreadSatchel-QwenMemory` (bounded processing), with logon triggers and five-minute repetition. Overlapping instances are suppressed; existing tasks are not silently replaced. **Your Windows account must remain logged in.** This is not a service that runs before login. The installer does **not** start or supervise the chat server; leave it running or use your existing separately managed local model service. It also does not install inbox/capture schedules.

For the additional spare-capacity features, back up `memory-ai.json` and set these existing fields to `true`:

```json
{
  "idle_embeddings": true,
  "prewarm_enabled": true,
  "health_checks_enabled": true
}
```

These are **fields to edit, not a replacement configuration**. They enable extra CPU indexing, warm search caches, prepared project briefs, and sampled health checks. Restart the embedding owner after code/worker-setting updates and reconnect MCP after first enabling prewarming. Defaults defer these extras above 25% whole-machine CPU or below 8 GiB available RAM. The resource monitor currently supports Windows; unavailable counters defer opportunistic work. These guards do not cover every ordinary manual or scheduled chat-analysis request.

Automatic health checks run inside the embedding owner, so `enabled` and `embeddings` must both be true. Cache limits are per MCP process, not RAM reservations. For budgets, cache freshness, coverage, and diagnostic files, see [optional idle CPU and RAM use](OPTIONAL_QWEN.md#optional-idle-cpu-and-ram-use).

## Stop, security, and troubleshooting

```powershell
& ./.venv/Scripts/python.exe memory_ai.py off
```

This backs up the existing configuration, disables new AI work, and lets the embedding owner stop its owned child. A request already in progress may finish. The independently managed chat server stays running; original memory and derived caches are preserved. Core imports, capture, and lexical retrieval continue working. Re-enable with `on`, restart the embedding owner (or let its schedule start it), and reconnect MCP if its warmer was never started.

Inference uses loopback HTTP only, with no cloud fallback. This does not isolate the service from other processes/users on the same computer. Optional `chat_api_key_file` supports a protected absolute local file containing a 32–512-character ASCII letters/digits/underscore/hyphen bearer token, matching your chat server's authentication. Protect the file with OS permissions, keep it outside Git, and never place its contents in packets, examples, or shell arguments. Do not broaden the binding to a LAN/public address as part of this setup. The reference owner's separate robot network guard is installation-specific and **not included or required** here.

Model extraction is local, but sources returned through MCP enter the requesting client's context and follow that client's privacy rules. Original and derived SQLite files contain private text in plaintext. Source text is data, not executable instructions. Quotation checks establish source wording, not the correctness of every model interpretation.

| Symptom | Check or next step |
| --- | --- |
| `enabled: false` / disabled brief | Confirm the checkout you configured, then use `on` if intended. |
| Model executable fails to start | Keep all ZIP DLLs together; check the executable path, driver, and `--version`. |
| Chat never considered idle | Check the exact base URL, alias, `/slots`, credentials if enabled, and other applications using the slot. |
| Embedding unavailable / model identity mismatch | Start `memory_ai.py embeddings`; verify its paths/hash and that an unrelated service is not occupying port 8091. |
| Search works but analysis is incomplete | Original FTS retrieval is independent. Read status, pending/held counts and errors; repeat bounded processing. |
| CPU-only chat or model times out | Defaults are 25 seconds for extraction and four seconds for selective reranking; this hardware/model combination may not be suitable. Lexical fallback remains available. |
| Idle work/prewarming does not run | Check all switches, keep the owner running, reconnect MCP, and inspect CPU/RAM guard reports. Each MCP process has its own cache. |
| Replaced a model but old derived output remains | Change `chat_model_revision` for changed chat weights; stop/restart the owner after embedding settings change. |
| Repeated extraction failure / held chunks | Run `memory_ai.py diagnostics`; fix the underlying cause before [queuing one controlled retry](OPTIONAL_QWEN.md#staleness-and-rebuilding). Validation is never relaxed. |

For software checks without models or private data, run `python scripts/check.py` in the project venv. These isolated tests validate the software contract using synthetic inputs; they do not substitute for verifying live model output or your own hardware.
