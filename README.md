# blockrun-litellm

[![PyPI](https://img.shields.io/pypi/v/blockrun-litellm.svg)](https://pypi.org/project/blockrun-litellm/)
[![Python](https://img.shields.io/pypi/pyversions/blockrun-litellm.svg)](https://pypi.org/project/blockrun-litellm/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

LiteLLM adapter for [BlockRun](https://blockrun.ai) — call x402-paid AI models through [LiteLLM](https://github.com/BerriAI/litellm) with zero changes to your existing code. **Base and Solana chains supported.**

📚 **Full docs in [`docs/`](docs/)** — bilingual (English + 中文):
- [`CUSTOMER-ONBOARDING`](docs/CUSTOMER-ONBOARDING.md) / [`中文`](docs/CUSTOMER-ONBOARDING.zh.md) — 5-minute walkthrough, both modes
- [`PROXY-FULL-SETUP`](docs/PROXY-FULL-SETUP.md) / [`中文`](docs/PROXY-FULL-SETUP.zh.md) — full deploy with admin UI + Postgres + troubleshooting

🌐 **Hosted docs:** [**blockrun.ai/docs**](https://blockrun.ai/docs)
- [Chat Completions API](https://blockrun.ai/docs/api-reference/chat-completions)
- [Models & pricing](https://blockrun.ai/docs/api-reference/models)

> **TL;DR** — BlockRun's `/v1/chat/completions` is already OpenAI-compatible at the protocol level. The only thing that differs is *authentication*: BlockRun uses per-request x402 wallet signatures (non-custodial USDC micropayments on Base / Solana), not a Bearer API key. This package bridges that gap.

[中文文档见底部 / Chinese docs at the bottom](#中文文档)

---

## Two ways to integrate

| Mode | Best for | What it looks like |
|---|---|---|
| **1. Custom provider** (in-process) | Apps using the LiteLLM **Python library** | `litellm.completion(model="blockrun/openai/gpt-5.5", ...)` |
| **2. Local proxy** (sidecar) | Apps using the LiteLLM **Proxy Server** (or any OpenAI client) | `api_base="http://localhost:4001/v1"` |

Both modes share the same underlying wallet/signing flow (via the [`blockrun-llm`](https://github.com/BlockRunAI/blockrun-llm) SDK), so they behave identically. Pick whichever fits your deployment.

### Verified end-to-end against the live BlockRun gateway

Both modes have been validated against `https://blockrun.ai/api` using the free `nvidia/deepseek-v4-flash` model:

```
$ python -c "
> import litellm
> from blockrun_litellm import register; register()
> r = litellm.completion(
>     model='blockrun/nvidia/deepseek-v4-flash',
>     messages=[{'role':'user','content':'Reply with exactly: pong'}],
>     max_tokens=20, temperature=0.0)
> print(r.choices[0].message.content)"
pong

$ curl -sS http://127.0.0.1:4001/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"nvidia/deepseek-v4-flash","messages":[{"role":"user","content":"Reply with exactly: proxy-ok"}]}'
{"id":"a710c144c68c42f7a319fb93e9b9b5a0","object":"chat.completion","model":"nvidia/deepseek-v4-flash",
 "choices":[{"index":0,"message":{"role":"assistant","content":"proxy-ok"},...}],"usage":{...}}
```

---

## Install

```bash
# Base chain only — minimal
pip install blockrun-litellm

# Base chain + local OpenAI-compatible proxy (FastAPI/uvicorn)
pip install 'blockrun-litellm[proxy]'

# Base + Solana (adds the x402 SVM toolchain)
pip install 'blockrun-litellm[proxy,solana]'
```

Requires Python ≥ 3.9.

## Chains supported

| Chain | Gateway URL | Wallet env var | Status |
|---|---|---|---|
| Base (USDC) | `https://blockrun.ai/api` *(default)* | `BLOCKRUN_WALLET_KEY` | sync + async, streaming |
| Solana (USDC) | `https://sol.blockrun.ai/api` | `SOLANA_WALLET_KEY` | sync + async, streaming on both (since 0.3.1) |

To route on Solana, pass `api_base="https://sol.blockrun.ai/api"` plus `api_key=<solana-key>` to `litellm.completion(...)` — the adapter detects the chain from the URL and uses the right SDK client.

---

## Configure your wallet (one-time)

The `blockrun-llm` SDK signs each request locally with an EVM (Base chain) private key. **The key never leaves your machine.** Three ways to provide it:

```bash
# Option A — environment variable (recommended for servers)
export BLOCKRUN_WALLET_KEY=0xYOUR_BASE_CHAIN_PRIVATE_KEY

# Option B — auto-create + fund a new wallet (interactive, shows QR for funding)
python -c "from blockrun_llm import setup_agent_wallet; setup_agent_wallet()"

# Option C — pass per-call (Python lib mode), see examples below
```

> 💡 To validate without spending real USDC, use a free model like `nvidia/deepseek-v4-flash` — same code path, same wallet flow, $0 settlement.

---

## Mode 1 — Custom provider (Python library)

The shortest path if your app already calls `litellm.completion()` directly.

### 1a. Register once at startup

```python
import litellm
from blockrun_litellm import register

register()  # idempotent; adds "blockrun" to litellm.custom_provider_map
```

### 1b. Call with a `blockrun/` model prefix

```python
response = litellm.completion(
    model="blockrun/openai/gpt-5.5",        # blockrun/<provider>/<model>
    messages=[{"role": "user", "content": "What is the capital of France?"}],
    max_tokens=128,
    temperature=0.7,
)

print(response.choices[0].message.content)
print(response.usage)  # prompt_tokens / completion_tokens / total_tokens
```

The `blockrun/` prefix is stripped before being sent to the BlockRun gateway, so `openai/gpt-5.6-terra`, `anthropic/claude-opus-5`, `google/gemini-3.1-pro`, etc. all work — anything in BlockRun's catalog.

For local allowlists and model pickers, the package includes the current 82-model
catalog snapshot (chat, image, video, music, speech, and sound effects):

```python
from blockrun_litellm import model_ids, is_known_model

assert "anthropic/claude-opus-5" in model_ids()
assert is_known_model("blockrun/anthropic/claude-opus-5")
```

The gateway is authoritative and accepts newly released IDs before a package
update; query `https://blockrun.ai/api/v1/models` whenever you need live metadata.

### 1c. Override the wallet per-call (optional)

```python
response = litellm.completion(
    model="blockrun/openai/gpt-5.5",
    messages=[...],
    api_key="0xANOTHER_PRIVATE_KEY",          # passed to blockrun-llm as wallet
)
```

### 1d. Async

```python
import asyncio

async def main():
    response = await litellm.acompletion(
        model="blockrun/openai/gpt-5.5",
        messages=[{"role": "user", "content": "Hi"}],
    )
    print(response.choices[0].message.content)

asyncio.run(main())
```

---

## Mode 2 — Local proxy (LiteLLM Proxy Server, langchain, raw curl, …)

If you're running the **LiteLLM Proxy Server** (`litellm --config config.yaml`), or any client that just speaks OpenAI HTTP, run our proxy as a sidecar.

### 2a. Start the proxy

```bash
export BLOCKRUN_WALLET_KEY=0xYOUR_KEY
blockrun-litellm-proxy --port 4001
# → uvicorn running at http://127.0.0.1:4001
```

Flags:

| Flag | Default | Purpose |
|---|---|---|
| `--host` | `127.0.0.1` | Bind interface. **Keep loopback** unless you set `BLOCKRUN_PROXY_TOKEN`. |
| `--port` | `4001` | Bind port |
| `--api-url` | `https://blockrun.ai/api` | Override BlockRun gateway endpoint |
| `--log-level` | `info` | `critical`/`error`/`warning`/`info`/`debug`/`trace` |

Environment variables (no CLI flag):

| Env var | Default | Purpose |
|---|---|---|
| `BLOCKRUN_MAX_CONCURRENT` | `100` | Max in-flight requests. Excess requests queue inside the sidecar. See table below for tuning guidance. |
| `BLOCKRUN_PROXY_TOKEN` | *(unset)* | Optional Bearer token guard on all sidecar endpoints. |

#### High-concurrency tuning

Streaming requests release their semaphore slot as soon as the first token arrives from upstream (the x402 probe + sign is the only serialised part). For non-streaming requests the slot is held until the full response returns.

| Deployment | `BLOCKRUN_MAX_CONCURRENT` | `uvicorn --workers` | Effective max concurrency |
|---|---|---|---|
| Dev / single user | 20 | 1 | 20 |
| Small team (10–20 concurrent) | 50 | 1 | 50 |
| Production (100 concurrent) | 100 *(default)* | 1 | 100 |
| High-load (500+ concurrent) | 200 | 4 | 800 |

Multi-worker launch example:
```bash
BLOCKRUN_MAX_CONCURRENT=200 uvicorn blockrun_litellm.proxy:app --workers 4 --host 0.0.0.0 --port 4001
```

> **Note:** Each worker has its own semaphore. With `--workers 4` and `BLOCKRUN_MAX_CONCURRENT=200`, up to 800 requests can be in-flight simultaneously. The real ceiling is the upstream provider's RPM/TPM — see the [Enterprise SLA guide](docs/ENTERPRISE-SLA.zh.md) for per-provider limits.

Optional shared-secret guard:

```bash
export BLOCKRUN_PROXY_TOKEN=$(openssl rand -hex 32)
# clients must now send:  Authorization: Bearer $BLOCKRUN_PROXY_TOKEN
```

### 2b. Point LiteLLM Proxy at it

Drop this into your `config.yaml`:

```yaml
model_list:
  - model_name: gpt-5.5
    litellm_params:
      model: openai/openai/gpt-5.5   # first 'openai/' = LiteLLM provider; rest = BlockRun model id
      api_base: http://localhost:4001/v1
      api_key: "dummy"                # ignored if BLOCKRUN_PROXY_TOKEN is unset

  - model_name: claude-fable-5
    litellm_params:
      model: openai/anthropic/claude-fable-5
      api_base: http://localhost:4001/v1
      api_key: "dummy"

  - model_name: gemini-3.1-pro
    litellm_params:
      model: openai/google/gemini-3.1-pro
      api_base: http://localhost:4001/v1
      api_key: "dummy"

litellm_settings:
  drop_params: True   # silently drop OpenAI params BlockRun doesn't support
```

Run LiteLLM Proxy as usual:

```bash
litellm --config config.yaml --port 4000
```

Then call it like any OpenAI endpoint:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.5",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

### 2b-ii. Image / video models through LiteLLM — routing **and** billing

Two things trip people up when they add BlockRun's media models
(`xai/grok-imagine-image`, `xai/grok-imagine-image-pro`,
`xai/grok-imagine-video`, …) to a LiteLLM proxy:

1. **LiteLLM logs $0 spend for them.** LiteLLM prices calls from its bundled
   price map (`model_prices_and_context_window.json`), which has **no**
   BlockRun-routed media models — the cost lookup fails and the request is
   recorded at $0. Fix: declare the price in `litellm_params` (LiteLLM's
   [custom pricing](https://docs.litellm.ai/docs/proxy/custom_pricing)).
   LiteLLM bills images as `input_cost_per_pixel × width × height × n`, so a
   flat per-image price divides by 1024×1024 = 1,048,576.
2. **Video needs the OpenAI Videos API.** LiteLLM never calls the sidecar's
   native `/v1/videos/generations`; it speaks the OpenAI Videos spec
   (`POST /videos` → poll `GET /videos/{id}` → `GET /videos/{id}/content`),
   which the sidecar exposes since 0.6.0.

Working `config.yaml` for all three:

```yaml
model_list:
  # --- images: flat per-image price (1024x1024) ---
  - model_name: grok-imagine-image
    litellm_params:
      model: openai/xai/grok-imagine-image
      api_base: http://localhost:4001/v1
      api_key: "dummy"
      input_cost_per_pixel: 1.9073486328125e-08   # $0.02 / 1048576 px
    model_info:
      mode: image_generation

  - model_name: grok-imagine-image-pro
    litellm_params:
      model: openai/xai/grok-imagine-image-pro
      api_base: http://localhost:4001/v1
      api_key: "dummy"
      input_cost_per_pixel: 6.67572021484375e-08  # $0.07 / 1048576 px
    model_info:
      mode: image_generation

  # --- video: $0.05/second ---
  - model_name: grok-imagine-video
    litellm_params:
      model: openai/xai/grok-imagine-video
      api_base: http://localhost:4001/v1
      api_key: "dummy"
      output_cost_per_second: 0.05
    model_info:
      mode: video_generation
```

Call them through LiteLLM:

```bash
# image
curl http://localhost:4000/v1/images/generations \
  -H "Authorization: Bearer $LITELLM_KEY" -H "Content-Type: application/json" \
  -d '{"model": "grok-imagine-image", "prompt": "a corgi astronaut"}'

# video — create, then poll the returned id until status=completed
curl http://localhost:4000/v1/videos \
  -H "Authorization: Bearer $LITELLM_KEY" -H "Content-Type: application/json" \
  -d '{"model": "grok-imagine-video", "prompt": "a corgi surfing", "seconds": "8"}'
```

Notes:

- Pass `seconds` on video creates — LiteLLM computes video spend from the
  `seconds` echoed on the create response (`output_cost_per_second ×
  seconds`), so omitting it records $0 for that call.
- Video jobs live in the sidecar process' memory (TTL 24h, override with
  `BLOCKRUN_VIDEO_JOB_TTL`); poll the same sidecar instance that accepted
  the create — don't run multiple sidecar replicas behind one LiteLLM
  video model without sticky routing.
- **Chat spend needs no config**: since 0.6.0 the sidecar returns the real
  x402 charge in the `x-litellm-response-cost` response header, which
  LiteLLM reads off openai-compatible upstreams and records as the
  request's spend — the exact wallet deduction, not an estimate.
- The custom-pricing numbers above are BlockRun's list prices; check
  [blockrun.ai/models](https://blockrun.ai/models) if they've moved.

### 2c. Or skip LiteLLM entirely

The proxy speaks OpenAI HTTP, so anything that takes an `api_base` works:

```python
# OpenAI Python SDK pointed straight at the BlockRun proxy
from openai import OpenAI

client = OpenAI(api_key="dummy", base_url="http://localhost:4001/v1")
resp = client.chat.completions.create(
    model="openai/gpt-5.5",
    messages=[{"role": "user", "content": "Hi"}],
)
print(resp.choices[0].message.content)
```

```bash
# Plain curl
curl http://localhost:4001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "openai/gpt-5.5", "messages": [{"role":"user","content":"Hi"}]}'
```

### 2d. Endpoints exposed

| Method | Path | Notes |
|---|---|---|
| `POST` | `/v1/chat/completions` | OpenAI Chat Completions. `stream=True` returns `text/event-stream`; otherwise JSON. |
| `POST` | `/v1beta/models/{model}:generateContent` | Native Gemini JSON request and response, with automatic x402 payment. |
| `POST` | `/v1beta/models/{model}:streamGenerateContent` | Native Gemini SSE, with automatic x402 payment. |
| `POST` | `/v1/responses` | OpenAI Responses API, bridged onto Chat Completions (`input`→`messages`, `output`/`response.*` SSE out). Text-in/text-out; for advanced tool/state flows use `/v1/chat/completions`. |
| `POST` | `/v1/images/generations` | OpenAI Image Generations. Accepts `prompt`, `model`, `size`, `n`, and `quality` (Solana only — see below). |
| `POST` | `/v1/images/edits` | OpenAI-compatible image editing. Accepts JSON data URIs or multipart `image`/`image[]`; supports multiple source images, `mask`, and `quality` (Solana only). `/v1/images/image2image` is an alias. |
| `POST` | `/v1/videos` | OpenAI Videos API create (what LiteLLM's video routes call) — returns a job object immediately |
| `GET`  | `/v1/videos/{id}` | OpenAI Videos API status poll (`queued` → `in_progress` → `completed`/`failed`) |
| `GET`  | `/v1/videos/{id}/content` | Download the finished clip bytes |
| `POST` | `/v1/videos/generations` | Native video generation. Supports `duration_seconds`, image/first-last-frame inputs, reference images, `input_type`, resolution, aspect ratio, audio, seed, and model-specific parameters. |
| `POST` | `/v1/audio/speech` | OpenAI-compatible TTS |
| `POST` | `/v1/audio/generations` | Music generation |
| `POST` | `/v1/audio/sound-effects` | Cinematic sound effects |
| `GET`  | `/v1/models` | BlockRun model catalog |
| `GET`  | `/healthz` | Liveness probe (no upstream call) |
| `GET`  | `/docs` | Auto-generated Swagger UI |

### 2e. Native Gemini protocol

Native Gemini calls use the sidecar root (`http://localhost:4001`), not the
OpenAI `/v1` base. The sidecar preserves Gemini request/response JSON and SSE
frames and adds the x402 payment signature using the configured wallet.

```bash
# Non-streaming
curl http://localhost:4001/v1beta/models/gemini-2.5-flash:generateContent \
  -H "Content-Type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"Reply with native-ok"}]}]}'

# Streaming
curl -N 'http://localhost:4001/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse' \
  -H "Content-Type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"Reply with stream-ok"}]}]}'
```

The official Google Gen AI Python SDK can use the same native protocol. Keep
the SDK itself; point its custom base URL at the sidecar and use any non-empty
placeholder API key (the sidecar removes it before forwarding):

```python
from google import genai
from google.genai import types

client = genai.Client(
    api_key="unused",
    http_options=types.HttpOptions(base_url="http://localhost:4001"),
)

response = client.models.generate_content(
    model="gemini-2.5-flash-lite",
    contents="Reply with native-ok",
)
print(response.text)

for chunk in client.models.generate_content_stream(
    model="gemini-2.5-flash-lite",
    contents="Reply with stream-ok",
):
    print(chunk.text or "", end="")
```

For Solana, start the sidecar with
`BLOCKRUN_API_URL=https://sol.blockrun.ai/api` and a `SOLANA_WALLET_KEY` (or
the wallet already stored at `~/.blockrun/.solana-session`). A client-supplied
Google API key is ignored; the local wallet is the authentication and payment
mechanism.

**Availability.** Native Gemini passthrough on the **Base** gateway (the
default, `https://blockrun.ai/api`) is limited to enterprise/allowlisted
wallets — other payers get `403 PERMISSION_DENIED` and are **not** charged. On
**Solana** (`https://sol.blockrun.ai/api`) it is generally available. If your
wallet is not on the Base allowlist, use Solana for native Gemini, or call the
OpenAI-format `/v1/chat/completions` route with `model="google/gemini-3.1-pro"`,
which works on both chains. Only `generateContent` and `streamGenerateContent`
are proxied; `countTokens`, model list/get, and `embedContent` are not served by
the gateway. Streaming always returns SSE regardless of `alt=` (the client query
string is dropped and the gateway sets `alt=sse` itself), so raw REST clients
must parse SSE, not the chunked-JSON-array form.

If you set `BLOCKRUN_PROXY_TOKEN` to guard the sidecar, the Google SDK cannot
send it (it only sends `x-goog-api-key`), so pass it explicitly as a Bearer
header: `types.HttpOptions(base_url=..., headers={"Authorization": "Bearer <token>"})`.

This is a sidecar HTTP feature. `litellm.completion(...)` remains the
OpenAI-compatible interface and continues to use `/v1/chat/completions`.

#### Image request limits

`n` is bounded to **1–10** locally. The Base `image2image` gateway schema has no
bound, so an out-of-range `n` would pass validation, take payment, and only then
fail at the provider — losing the prepaid USDC.

Multipart uploads are capped by `BLOCKRUN_MAX_IMAGE_BYTES` (default 12MB → 413)
and `BLOCKRUN_MAX_IMAGE_PARTS` (default 4 → 400; 4 is the most any model
accepts). Blank optional fields (`quality=`, `size=`, `model=`, `mask=`, `n=`)
mean "not set", matching what the gateway's own multipart handler does.

#### `quality` and `input_type` (requires `blockrun-llm>=1.7.0`)

**`quality`** (`low`/`medium`/`high`/`auto`, `openai/gpt-image-*`) is **Solana
only** — the Base gateway defines no such field. Sending it on Base still
returns **200** and generates the image, but the parameter is ignored and the
response carries an `x-blockrun-warning` header saying so (a warning is also
logged). Point `BLOCKRUN_API_URL` at the Solana gateway to make it take effect.

**`input_type`** (`text`/`image`/`first_last_frame`/`reference`) declares the
seed mode you intend on `/v1/videos/generations`. The gateway infers the mode
from the seed fields and returns **400 without charging** if your declaration
disagrees. Useful when seed fields are built dynamically: a dropped `image_url`
otherwise degrades silently to text-to-video and still bills you.

Reference-to-video (`reference_videos`/`reference_audios`) is **not** supported:
both gateways currently gate it off and would return 503.

### 2f. Image generation

```python
from openai import OpenAI

client = OpenAI(api_key="dummy", base_url="http://localhost:4001/v1")
resp = client.images.generate(
    model="google/nano-banana",
    prompt="a corgi astronaut on the moon",
    size="1024x1024",
)
print(resp.data[0].url)  # always an HTTPS proxy URL
```

All image models return HTTPS proxy URLs — the BlockRun server handles any provider-level differences (e.g. base64 payloads) transparently before responding.

Available image models: `google/nano-banana`, `google/nano-banana-pro`, `openai/dall-e-3`, `openai/gpt-image-1`, `openai/gpt-image-2`, `xai/grok-imagine-image`, `xai/grok-imagine-image-pro`, `zai/cogview-4`.

---

## Supported parameters

All of these are forwarded to BlockRun unchanged:

| OpenAI param | Supported | Notes |
|---|---|---|
| `model` | ✅ | Any BlockRun model id, e.g. `openai/gpt-5.5` |
| `messages` | ✅ | Full role/content/tool_calls schema |
| `max_tokens` | ✅ | Defaults to 1024 if omitted |
| `temperature` | ✅ | 0–2 |
| `top_p` | ✅ | |
| `tools` / `tool_choice` | ✅ | Function calling |
| `stream` | ✅ | OpenAI-style SSE (`text/event-stream`). Provider mode yields LiteLLM `GenericStreamingChunk` objects; proxy mode emits `data: <json>\n\n` events terminated by `data: [DONE]`. Free models stream directly; paid models stream after the in-band 402-sign-retry dance. |
| `frequency_penalty` / `presence_penalty` / `logprobs` / `n` | ⚠️ | Silently dropped — enable `litellm_settings.drop_params: True` to suppress LiteLLM warnings |

BlockRun-specific extras (also accepted):

| Param | Purpose |
|---|---|
| `search: True` | Enable xAI Live Search (for search-enabled models) |
| `search_parameters: {...}` | Full Live Search config |
| `fallback_models: ["..."]` | Auto-retry on transient upstream errors |

---

## Native fingerprint passthrough

BlockRun returns the **upstream provider's response verbatim** — no model
substitution, no response reshaping. The relay-detection signals a tool like
`cctest.ai` inspects therefore survive end to end, so the call looks like a
genuine **direct** upstream request rather than a proxied one:

| Signal | Where | Models |
|---|---|---|
| `system_fingerprint` (`fp_*`) | top-level response | OpenAI (`openai/gpt-5.5`, …) |
| `service_tier` | top-level response | OpenAI |
| `usage.prompt_tokens_details.cached_tokens` | usage block | OpenAI |
| `usage.cache_read_input_tokens` / `cache_creation_input_tokens` | usage block | Anthropic (`anthropic/claude-fable-5`, …) |
| `reasoning_content` | per message | reasoning models |

Both integration modes preserve these:

- **Proxy mode** returns the OpenAI-shaped JSON verbatim, so every field above
  is present on the wire.
- **Provider mode** keeps them on the `litellm.ModelResponse` (`response.system_fingerprint`,
  `response.usage`, `response.choices[0].message.reasoning_content`). In
  streaming, the lossy `GenericStreamingChunk` carries them on
  `provider_specific_fields`.

```python
resp = litellm.completion(model="blockrun/openai/gpt-5.5", messages=[...])
print(resp.system_fingerprint)   # e.g. "fp_abc123" — the real upstream value
```

> **Note:** Claude's native thinking-block `signature` is an Anthropic
> `/v1/messages`-only field. The litellm package speaks OpenAI
> `/v1/chat/completions`, so it surfaces `reasoning_content` and the cache-token
> usage but not the raw `signature`. For full Anthropic-native passthrough
> (content blocks + `signature`), use the `blockrun-llm-vip` `Anthropic` client.

Verified flagship models: **`openai/gpt-5.5`**, **`anthropic/claude-fable-5`**,
**`google/gemini-3.1-pro`**. The contract is locked by
`tests/test_fingerprint.py`.

---

## Local request log (input/output tokens, latency, cost)

Opt-in JSONL logger captures every call — works on both Base and Solana, sync and async, streaming and non-streaming.

### Where the log lives

| Source | Path |
|---|---|
| Explicit arg to `enable_local_logging("...")` | (whatever you pass) |
| `BLOCKRUN_LITELLM_LOG` env var | (whatever it points to) |
| Otherwise | **`~/.blockrun/litellm_calls.jsonl`** |

### Each row contains

```
ts, iso, model, provider, messages, completion,
usage{prompt_tokens, completion_tokens, total_tokens},
latency_ms, stream, cost_usd, status, error_type, error_message, request_id
```

### Mode 1 — one line

```python
from blockrun_litellm import enable_local_logging
enable_local_logging()                       # default path
# or enable_local_logging("/var/log/calls.jsonl")
```

### Mode 2 — drop a bridge file next to `config.yaml`

```python
# custom_callbacks.py
from blockrun_litellm.logger import JSONLLogger
blockrun_logger = JSONLLogger()
```

```yaml
litellm_settings:
  callbacks: ["custom_callbacks.blockrun_logger"]
```

---

## Where everything is stored

| File / env var | What | Configurable? |
|---|---|---|
| `BLOCKRUN_WALLET_KEY` (env) | Base private key | yes |
| `SOLANA_WALLET_KEY` (env) | Solana private key | yes |
| `~/.blockrun/.session` | Auto-created Base wallet | — |
| `~/.blockrun/.solana-session` | Auto-created Solana wallet | — |
| `~/.blockrun/litellm_calls.jsonl` | LiteLLM request log | `BLOCKRUN_LITELLM_LOG` env or `enable_local_logging(path)` |
| `~/.blockrun/cost_log.jsonl` | USDC cost audit for paid calls (SDK) | — |
| `~/.blockrun/data/*.json` | Full request/response archive for paid calls (SDK) | — |
| `BLOCKRUN_PROXY_TOKEN` (env) | Optional shared-secret guard on sidecar | yes |
| `BLOCKRUN_MAX_CONCURRENT` (env) | Max in-flight requests to upstream (default `100`) | yes |

---

## Examples

The `examples/` directory has copy-paste-ready snippets:

- [`examples/python_lib.py`](examples/python_lib.py) — full LiteLLM Python library usage
- [`examples/litellm_config.yaml`](examples/litellm_config.yaml) — LiteLLM Proxy Server config
- [`examples/raw_openai_sdk.py`](examples/raw_openai_sdk.py) — pointing the OpenAI SDK at the proxy
- [`examples/custom_callbacks.py`](examples/custom_callbacks.py) — JSONL log bridge for Proxy mode

---

## How it works (under the hood)

```
┌─────────────────┐    OpenAI dict     ┌──────────────────────┐    POST /v1/chat/completions  ┌────────────────┐
│ Your app /      │ ─────────────────▶ │  blockrun-litellm    │ ────────────────────────────▶ │  blockrun.ai   │
│ LiteLLM /       │                    │  (provider OR proxy) │ ◀──── 402 + payment-required ─│  gateway       │
│ OpenAI SDK      │                    │  ↓                   │                               │                │
└─────────────────┘                    │  blockrun-llm SDK    │ ───── EIP-712 signed retry ──▶│                │
                                       │  (local signing)     │ ◀──── 200 + chat response ────│                │
                                       └──────────────────────┘                               └────────────────┘
                                                ▲
                                                │ private key (stays local, signs only)
                                       ┌──────────────────────┐
                                       │ BLOCKRUN_WALLET_KEY  │
                                       │   or ~/.blockrun/    │
                                       └──────────────────────┘
```

1. Caller sends an OpenAI Chat Completions dict.
2. `blockrun-litellm` whitelists the params and dispatches through `blockrun-llm`.
3. `blockrun-llm` posts to BlockRun, receives a 402 with payment requirements, signs an EIP-712 payment locally with your wallet, and retries.
4. BlockRun verifies the signature on-chain, settles the USDC micropayment, runs the inference, and returns the response.
5. `blockrun-litellm` returns the dumped pydantic model as a plain OpenAI dict (or `litellm.ModelResponse` in provider mode).

---

## FAQ

**Q: Does this support streaming?**
Yes, as of v0.2.0. Pass `stream=True` and the adapter routes through `blockrun-llm`'s `chat_completion_stream()` (SDK ≥ 0.20.0). The 402 → sign-locally → retry-with-PAYMENT-SIGNATURE dance happens before the first chunk; once the upstream switches to `text/event-stream`, chunks are forwarded straight through (provider mode → `litellm.GenericStreamingChunk`, proxy mode → OpenAI-style `data: <json>\n\n` SSE). Caveats inherited from the gateway: `search_parameters` and the Responses-API models (`codex`, `gpt-5.4-pro`) reject streaming server-side with 400.

**Q: Where does my private key live?**
On your machine only — `BLOCKRUN_WALLET_KEY` env var, or `~/.blockrun/.session` if you used `setup_agent_wallet()`. The proxy and provider both read from those sources via `blockrun-llm`. Only EIP-712 signatures are transmitted.

**Q: How do I switch between Base and Solana?**
Today this adapter wires to BlockRun's Base gateway (USDC on Base). Solana support tracks the `blockrun-llm` `SolanaLLMClient` and will be added in a follow-up release.

**Q: Can I run the proxy in Docker / k8s?**
Yes — it's a vanilla FastAPI app. Pass the wallet key via secret (env var), bind to `0.0.0.0` only inside a private network, and set `BLOCKRUN_PROXY_TOKEN` for an additional auth layer.

**Q: Is this affiliated with LiteLLM (BerriAI)?**
No — this is an independent adapter built by the BlockRun team. LiteLLM is a great project; we're just plugging into its custom-provider hooks.

---

## Development

```bash
git clone https://github.com/BlockRunAI/blockrun-litellm
cd blockrun-litellm
pip install -e '.[proxy,dev]'
pytest
```

---

## License

MIT. See [LICENSE](LICENSE).

---

# 中文文档

[BlockRun](https://blockrun.ai) 的 [LiteLLM](https://github.com/BerriAI/litellm) 适配层 —— 用 LiteLLM 调用 BlockRun 上的 AI 模型，**完全零改动**。

> **一句话：** BlockRun 的 `/v1/chat/completions` 协议层就是 OpenAI 兼容的，唯一区别是认证方式 —— BlockRun 用 x402 钱包签名（按次 USDC 微支付，非托管），不是 Bearer API Key。这个包就是把这层差异填平。

## 两种对接方式

| 模式 | 适用 | 写法 |
|---|---|---|
| **1. 自定义 Provider**（进程内） | 用 LiteLLM **Python 库**的应用 | `litellm.completion(model="blockrun/openai/gpt-5.5", ...)` |
| **2. 本地代理**（sidecar） | 用 LiteLLM **Proxy Server** 的、或任何 OpenAI 客户端 | `api_base="http://localhost:4001/v1"` |

底层都走 [`blockrun-llm`](https://github.com/BlockRunAI/blockrun-llm) SDK 做签名和 x402 支付，两种模式行为一致。按你的部署方式选一种就行。

## 快速上手

### 安装

```bash
# 只装自定义 provider
pip install blockrun-litellm

# 同时装本地代理（带 FastAPI/uvicorn）
pip install 'blockrun-litellm[proxy]'
```

### 配钱包（一次性）

```bash
# 方式 A — 环境变量（服务端推荐）
export BLOCKRUN_WALLET_KEY=0xYOUR_BASE_CHAIN_PRIVATE_KEY

# 方式 B — 自动创建并扫码充值（交互式）
python -c "from blockrun_llm import setup_agent_wallet; setup_agent_wallet()"
```

私钥**只在本地用于 EIP-712 签名**，永远不会离开你的机器。

> 💡 想零成本试一遍？用免费模型 `nvidia/deepseek-v4-flash` —— 代码完全一样，钱包流程一样，结算 $0。

### 模式 1：自定义 Provider

```python
import litellm
from blockrun_litellm import register

register()  # 启动时调一次即可

response = litellm.completion(
    model="blockrun/openai/gpt-5.5",   # blockrun/<provider>/<model>
    messages=[{"role": "user", "content": "你好"}],
    max_tokens=128,
)
print(response.choices[0].message.content)
```

异步版本：`await litellm.acompletion(...)` 同理。

### 模式 2：本地代理

```bash
# 1) 启动 sidecar
export BLOCKRUN_WALLET_KEY=0xYOUR_KEY
blockrun-litellm-proxy --port 4001

# 2) LiteLLM Proxy 配置 (config.yaml)
```

```yaml
model_list:
  - model_name: gpt-5.5
    litellm_params:
      model: openai/openai/gpt-5.5
      api_base: http://localhost:4001/v1
      api_key: "dummy"

litellm_settings:
  drop_params: True
```

或者直接拿任何 OpenAI 客户端用：

```python
from openai import OpenAI
client = OpenAI(api_key="dummy", base_url="http://localhost:4001/v1")
resp = client.chat.completions.create(
    model="openai/gpt-5.5",
    messages=[{"role": "user", "content": "你好"}],
)
```

### 图像 / 视频模型上 LiteLLM：调用 + 计费

把 `xai/grok-imagine-image` / `-image-pro` / `grok-imagine-video` 挂到 LiteLLM Proxy 时有两个坑：

1. **LiteLLM 记账为 $0** —— LiteLLM 按自带价格表算钱，表里没有这些模型，成本查询失败就记 0。解法：在 `litellm_params` 里声明[自定义单价](https://docs.litellm.ai/docs/proxy/custom_pricing)。图像按 `input_cost_per_pixel × 宽 × 高 × n` 计，固定张价除以 1024×1024。
2. **视频要走 OpenAI Videos API** —— LiteLLM 不会调 sidecar 原生的 `/v1/videos/generations`，它打的是 `POST /videos` → 轮询 `GET /videos/{id}` → `GET /videos/{id}/content`，sidecar 0.6.0 起已支持。

```yaml
model_list:
  - model_name: grok-imagine-image
    litellm_params:
      model: openai/xai/grok-imagine-image
      api_base: http://localhost:4001/v1
      api_key: "dummy"
      input_cost_per_pixel: 1.9073486328125e-08   # $0.02/张 ÷ 1048576 像素
    model_info:
      mode: image_generation

  - model_name: grok-imagine-image-pro
    litellm_params:
      model: openai/xai/grok-imagine-image-pro
      api_base: http://localhost:4001/v1
      api_key: "dummy"
      input_cost_per_pixel: 6.67572021484375e-08  # $0.07/张 ÷ 1048576 像素
    model_info:
      mode: image_generation

  - model_name: grok-imagine-video
    litellm_params:
      model: openai/xai/grok-imagine-video
      api_base: http://localhost:4001/v1
      api_key: "dummy"
      output_cost_per_second: 0.05                # $0.05/秒
    model_info:
      mode: video_generation
```

注意事项：

- 视频创建请求**务必带 `seconds`**（如 `"8"`）—— LiteLLM 按创建响应回显的 seconds × 每秒单价计费，不带就记 $0。
- 视频任务存在 sidecar 进程内存里（TTL 24 小时，`BLOCKRUN_VIDEO_JOB_TTL` 可调），轮询要打到接收创建请求的同一个 sidecar 实例。
- **Chat 计费无需任何配置**：0.6.0 起 sidecar 在响应头 `x-litellm-response-cost` 返回真实 x402 扣费，LiteLLM 直接采用，分毫不差。

## 支持的参数

| OpenAI 参数 | 支持 | 备注 |
|---|---|---|
| `model` / `messages` / `max_tokens` / `temperature` / `top_p` | ✅ | |
| `tools` / `tool_choice` | ✅ | 函数调用 |
| `stream` | ✅ | OpenAI 标准 SSE（`text/event-stream`）。Provider 模式 yield LiteLLM `GenericStreamingChunk`；Proxy 模式发 `data: <json>\n\n` 事件并以 `data: [DONE]` 结尾。免费模型直接开流；付费模型走带内 402→签名→重试再开流。 |
| `frequency_penalty` / `presence_penalty` / `logprobs` / `n` | ⚠️ | 静默丢弃 —— 建议 LiteLLM 配 `drop_params: True` 抑制告警 |

BlockRun 额外参数：

| 参数 | 作用 |
|---|---|
| `search: True` | 启用 xAI Live Search（搜索类模型） |
| `search_parameters: {...}` | 完整 Live Search 配置 |
| `fallback_models: ["..."]` | 上游抖动自动重试列表 |

## 常见问题

**Q：支持流式吗？**
v0.2.0 起完全支持。`stream=True` 时适配层走 `blockrun-llm` 的 `chat_completion_stream()`（SDK ≥ 0.20.0），402 → 本地签名 → 带 PAYMENT-SIGNATURE 重试这条链在第一个 chunk 之前完成；上游切到 `text/event-stream` 后 chunks 直接透传（Provider 模式 → `litellm.GenericStreamingChunk`，Proxy 模式 → OpenAI 标准 `data: <json>\n\n`）。后端继承的限制：`search_parameters` 和 Responses-API 模型（`codex`、`gpt-5.4-pro`）在服务端就拒绝流式（400）。

**Q：私钥放哪？**
只在本地 —— `BLOCKRUN_WALLET_KEY` 环境变量，或 `setup_agent_wallet()` 创建的 `~/.blockrun/.session`。Provider 和 Proxy 都通过 `blockrun-llm` 读取。链上只看到签名，看不到私钥。

**Q：Docker / k8s 部署？**
代理是普通的 FastAPI 应用。密钥用 secret 注入，对外只暴露内网，可选 `BLOCKRUN_PROXY_TOKEN` 加一层 Bearer 鉴权。

**Q：和 BerriAI 是什么关系？**
没关系。这是 BlockRun 团队独立维护的适配层，挂在 LiteLLM 的 custom provider 接口上。

## 开发

```bash
git clone https://github.com/BlockRunAI/blockrun-litellm
cd blockrun-litellm
pip install -e '.[proxy,dev]'
pytest
```

## License

MIT
