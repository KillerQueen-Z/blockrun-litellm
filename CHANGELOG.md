# Changelog

## 0.9.0 — 2026-07-24

### Added

- **Bundled model-catalog snapshot** (`blockrun_litellm.catalog`). `BLOCKRUN_MODEL_IDS`
  carries the gateway's 82 published IDs — chat, image, video, music, speech, and
  sound effects — in the gateway's own catalog order, with `model_ids()` and
  `is_known_model()` exported from the package. `is_known_model()` accepts both the
  custom-provider `blockrun/<id>` form and the bare gateway `<id>` form.

  This exists for callers that must render an allowlist or a model picker
  *before* a proxy is running. It is not an admission control list: the gateway
  is the source of truth and accepts newly released IDs before this package is
  republished, so nothing here blocks a forward.

- `CATALOG_SNAPSHOT_DATE` records when the snapshot was taken, so a consumer can
  tell how stale its bundled copy is rather than guessing from the package version.
  Verified against `GET https://blockrun.ai/api/v1/models` on 2026-07-24: 82 live
  IDs, exact match including order.

### Changed

- `examples/litellm_config.yaml` gains `claude-opus-5` ($5 / $25 per 1M, with
  prompt-cache read/write rates) and moves the flagship OpenAI example to
  `gpt-5.6-terra` ($2.50 / $15 per 1M). Both price pairs match the gateway's
  published pricing; the cache ratios follow the existing Anthropic entries
  (0.1× input for reads, 1.25× for writes).

### Compatibility

- Purely additive. No runtime behavior, wire format, or payment path changes —
  the catalog module is a dependency-free constant and two pure functions.

## 0.8.0 — 2026-07-22

### Added

- **Gemini's native `v1beta` protocol now works through the local sidecar.**
  `POST /v1beta/models/{model}:generateContent` and
  `POST /v1beta/models/{model}:streamGenerateContent` are forwarded verbatim
  to BlockRun while the sidecar handles Base or Solana x402 payment locally.
  This lets a customer keep Gemini-native request/response bodies and SSE
  frames instead of translating through OpenAI Chat Completions.

- Streaming is selected from the Gemini URL method, matching Google's wire
  protocol (there is no OpenAI-style `"stream": true` field to inspect).
  Upstream errors keep their native HTTP status and Gemini error envelope.

- Client Google credentials are never forwarded. Google SDKs may require a
  dummy API key and attach it as `x-goog-api-key` or `?key=...`; the sidecar
  removes both because BlockRun authenticates with the local x402 wallet and
  the gateway owns the upstream Google credential.

- The model segment is sanitized before forwarding: `models/`/`google/`
  prefixes are stripped and the path is rebuilt from the clean id, so a crafted
  model name cannot move the signed upstream target outside `/v1beta/models/`.
  Unsupported methods and invalid model ids are rejected with a `400` — and, per
  the 0.7.6 every-exit-logs guarantee, still write an audit row.

### Availability

- Native Gemini passthrough is **enterprise/allowlist-only on the Base gateway**
  (non-allowlisted payers receive `403 PERMISSION_DENIED` and are not charged);
  it is generally available on Solana (`sol.blockrun.ai`). Retail callers on
  Base should use `/v1/chat/completions` with `model="google/gemini-3.1-pro"`.
  Only `generateContent`/`streamGenerateContent` are proxied.

### Compatibility

- Existing OpenAI, Anthropic, image, video, and audio routes are unchanged.
- Native Gemini is available in sidecar/proxy mode. The in-process LiteLLM
  custom provider remains OpenAI-shaped because it returns LiteLLM response
  objects rather than exposing an HTTP protocol surface.

## 0.7.6 — 2026-07-16

### Fixed

- **A failed call is no longer logged as free when it might have been charged.**
  `cost_usd=None` reads as "$0", but it is also what the sidecar writes when it
  simply doesn't know — and on Solana those are different facts worth real
  money. Several Solana routes (chat, search, both image routes, music) settle
  **optimistically**: settle fires in parallel with the upstream work, so a call
  that verifies and then fails upstream **is charged**, and its error response
  carries no settlement header. "No proof of payment" and "you were charged"
  therefore co-occur exactly when it matters.

  The audit row now carries `settlement_status: "unknown"` in that case, so
  reconciliation investigates instead of trusting a zero.

  **Every** Solana failure is flagged, not just 5xx. The settle fires *first*, so
  the charged failures come back as 4xx: a content-filter rejection is **400**
  (blockrun-sol `images/generations:376`) and a rate limit **429** (`:359`) —
  both routine, both client-triggerable, both charged. 402 is not exempt either:
  Solana settles at POST, and the poll returns 402 on wallet-binding *after* the
  money moved. The single exemption is a proof rather than a guess — the SDK's
  own request validation raises before anything goes on the wire.

  Base failures are **not** flagged: Base media routes settle only after a
  successful upstream call, so a failure is genuinely free, and a flag on every
  Base error would be noise that gets ignored. The exception is **504** — our own
  await ceiling, which abandons the wait but leaves the worker running, so the
  call can still complete and settle.

  Flagged on **both** chains: the gateway settles, returns 200, and the body
  won't parse (`ValidationError` *or* `json.JSONDecodeError` → 502).

  **Every exit now logs.** Two paths previously wrote no row at all — worse than
  an unflagged one, because a call that moves money and leaves no trace is
  invisible to reconciliation. `json.JSONDecodeError` subclasses `ValueError`,
  and the SDK parses the paid 200 with a bare `.json()`, so a truncated body
  answered 400 (blaming the caller for a call they paid for) and returned via
  `raise` before the log ran. Transport errors (`httpx.ReadTimeout` on a
  10-minute image call) matched no arm at all.

  Applied to **all five** log sites — media, the OpenAI Videos job route, and the
  chat/messages passthrough. Chat is the highest-volume paid route and Solana
  settles it optimistically: on failure the gateway logs "CHARGED BUT REQUEST
  FAILED — refund manually" and throws, so the error carries no settlement
  header. Reading headers only helps when there is one.

  The check is one bit of chain, not a table of which Solana routes settle
  optimistically — that table lives in the gateway and would drift here. The
  asymmetry decides it: a false "unknown" costs a glance at the ledger, a false
  "$0" loses a real charge. Solana's video route settles on the completed poll
  and will occasionally be flagged for nothing; that is the trade, taken on
  purpose.

  The gateway ledger remains the authority on what actually settled — this flag
  says "go look", not "you were charged".

## 0.7.5 — 2026-07-16

Documentation only — runtime code is byte-identical to 0.7.3 and 0.7.4.

### Fixed

- **0.7.4's "correction" was itself wrong.** It claimed "BlockRun's gateways
  settle **on success** ... so a request that dies at the provider settles
  nothing and costs the caller nothing." That is true for **Base** and false for
  **Solana**, which settles **optimistically**:

  ```ts
  // Optimistic settlement (DEFAULT) — fire settle in parallel with the
  // 10-60s edit so it lands inside Solana's ~60-90s blockhash window.
  const settlementPromise = settlePaymentWithRetry(paymentHeader, priceUsd);
  ```

  Settle fires *before* the outcome is known, so a payload that passes
  verification and then fails upstream **is charged** — the gateway logs it as a
  paid error with a real tx hash. This is systemic on Solana:
  `images/generations`, `image2image`, `messages`, `search`, and
  `audio/generations` all settle this way.

  | | Base | Solana |
  |---|---|---|
  | settle timing | after the upstream call, success only | in parallel, before the outcome |
  | failed request | costs nothing | **can be charged** |

  So the `n` bound (0.7.2) is the difference between a local 400 and a real
  debit for an image that never existed — **on Solana**. On Base it saves a
  pointless signed round-trip. Worth having either way; only the stakes differ.

  **How this went wrong twice.** 0.7.2 claimed the loss was universal (right for
  Solana, wrong for Base). 0.7.4 claimed it never happened (right for Base,
  wrong for Solana) — I verified Base's settle ordering and generalized to "the
  gateways" without checking Solana. Each pass corrected a half-truth into the
  opposite half-truth. The chains genuinely differ; any single sentence about
  "the gateways" and settlement is wrong about one of them.

  Related: blockrun-llm 1.7.1 stops asserting "payment likely not taken" on a
  failed paid request for exactly this reason — on Solana, absence of a
  settlement header and *having been charged* co-occur systematically, because
  the error path returns before settle lands and carries no header.

## 0.7.4 — 2026-07-16

Documentation only — runtime code is byte-identical to 0.7.3. Released so the
corrected record ships with the package rather than sitting unreleased on main.

### Fixed

- **Corrected a false claim in the 0.7.2 changelog.** It said an out-of-range
  `n` "took payment, then 400'd at the provider and lost the prepaid USDC".
  BlockRun's gateways settle **on success** — `settlePaymentWithRetry` runs
  after the upstream call — so a request that dies at the provider settles
  nothing and costs the caller nothing. Verified on every path, including the
  async one: the poll endpoint answers `payment_status: "not_charged"` with
  "Upstream generation failed. No payment was taken." for failed jobs.

  The `n` bound stays — it saves a pointless signed round-trip, a facilitator
  verify, and an upstream call, and answers 400 locally — it was just never
  about losing money. A changelog that cries wolf about funds is worse than one
  that says nothing.

  The mistake came from the SDK's `"API error after payment"` wording, which
  reads as *funds gone* on failures that took nothing; it has now misled two
  reviewers and this changelog. Fixed upstream in blockrun-llm#25, which reports
  whether settlement actually happened.

## 0.7.3 — 2026-07-16

### Fixed

- **A blank `model` no longer bills a default generation** (#21). The media
  routes read `model` straight off the body, and the SDK coalesces with
  `model or DEFAULT_MODEL` — so `{"model": ""}`, which is what a client
  templating an unset variable sends, is falsy, silently became the default
  model, and **charged for it** (~$0.40 for the 8s Grok video default). The
  gateway can't catch it: its schema is `z.string().default(...)`, and a zod
  default only fires when the key is *absent*, so a present-but-empty string
  sails through. Every JSON route that names a billed model now refuses it with
  a 400 before dispatch: `/v1/videos/generations`, `/v1/videos`,
  `/v1/audio/speech`, `/v1/audio/generations`, `/v1/audio/sound-effects`, plus
  `/v1/images/generations` and `/v1/images/edits`. Omitting `model` still opts
  into the default and is unchanged.

  The multipart `/v1/images/edits` branch is the deliberate exception: transport
  decides what `""` means. A form emits every field it knows about, so a blank
  one is how "unset" is spelled on the wire — the same reason a blank `mask=` is
  nulled there, and what the Solana gateway's own multipart handler does. A blank
  in a JSON body has no such excuse. 0.7.2 read blank `model` as "unset"
  everywhere; that was right for `size` (unset is free) and wrong for `model`
  (unset is billed), so the two split here.

### Added

- **`grok-imagine-video` joins the bare-id bridge** (#20 follow-up). 0.7.1
  namespaced short Seedance ids but left the other family behind, so a bare
  `grok-imagine-video` still 400'd. Bare ids are also whitespace-trimmed now.
  `sora-2` is deliberately **not** mapped: the catalog ships it under two
  vendors (`azure/sora-2`, available; `openai/sora-2`, not), so there is no
  namespace to infer — guessing would pick a vendor for the caller, and the
  guess would flip meaning the day availability does. It reaches the gateway
  bare and 400s with the real list, which beats a confident wrong answer.

## 0.7.2 — 2026-07-16

Post-merge audit of 0.7.0 (three independent reviews of the shipped code). One
regression, two ways to bill a caller for something they didn't ask for, and a
test that couldn't fail.

### Fixed

- **Regression: `quality` on Base no longer 400s.** 0.6.1 never read `quality`,
  so a Base caller sending it got a 200; 0.7.0 read it and refused the request.
  `quality` is a first-class OpenAI Images parameter on a route documented as
  DALL-E compatible, so that turned working, spec-compliant code into a hard
  failure — an announced change at best, not a minor-bump side effect.

  It is dropped again on Base, but no longer in silence: a warning is logged and
  the response carries `x-blockrun-warning`. The original concern (a paid-for
  latency knob vanishing unnoticed) is answered by the header, not by breaking
  the caller.

- **`/v1/images/edits` silently billed for substituted defaults.** A wrong-typed
  `model`/`size` was coerced to `None` rather than rejected, so the SDK filled
  its default and charged: `{"size": 512}` meaning 512x512 quietly rendered a
  1024x1024 image at the default model. Wrong types are refused now — the same
  silent-drop-then-bill failure this release line exists to eliminate.

- **`n` is bounded 1–10 before payment.** Base's `image2image` schema is
  `z.number().optional().default(1)` — no int, no bounds — so `n=1000` passed
  validation, earned a 402, got payment-verified, and only then 400'd at the
  provider. (Solana already bounds it. The gateway-side fix belongs in
  `blockrun`; this closes it at the sidecar for both chains today.)

  **Correction (2026-07-16, superseded — see 0.7.5):** as first published this
  entry said the round-trip "lost the prepaid USDC", which is right for Solana
  and wrong for Base. The 0.7.4 correction then claimed the opposite —
  universally no loss — which is right for Base and wrong for Solana. Both
  passes collapsed two genuinely different gateways into one sentence. The real
  split: **Base** settles after the upstream call (a failed request costs
  nothing); **Solana** settles optimistically, in parallel (a failed request
  **can be charged**). 0.7.5 has the details.

- **Post-settlement parse failures no longer answer 400 or skip the audit log.**
  `pydantic.ValidationError` subclasses `ValueError`, and the SDK builds its
  response model *after* settlement — so a gateway that took payment and
  returned an unparseable body hit the `ValueError` arm, told the caller their
  request was bad, and `raise`d before `log_proxy_call`, keeping real spend out
  of reconciliation. Now 502, and logged like every other settled outcome.

- **Blank optional fields mean "not set" everywhere**, not just for `quality`.
  Blank `size`/`model` previously travelled as `""`: Base billed a default
  image, Solana 400'd — same input, different chain, neither intended.

- **Multipart image order follows the wire**, not field name. `getlist()` per
  name grouped every `image` before every `image[]`, silently reordering a
  client that mixed both spellings; order is load-bearing for fusion prompts.

### Changed

- `BLOCKRUN_MAX_IMAGE_PARTS` defaults to **4** (was 16) — the most any model
  accepts. Parts 5–16 were read, base64-inflated ~1.33x, and uploaded twice (the
  unpaid 402 probe, then the signed retry) only to earn a 400.
- A missing `python-multipart` now returns **500**, not 400 — it's a server
  install problem, not the caller's fault.
- An `application/x-www-form-urlencoded` POST to the image routes now says so,
  instead of reporting "Invalid JSON body".
- `BLOCKRUN_MAX_IMAGE_*` env vars warn and fall back instead of crashing at
  import on a malformed value.

- **Version drift: `__version__` said `0.7.0` while `pyproject.toml` said
  `0.7.1`.** 0.7.1 bumped only one of the two files, so an installed copy would
  have under-reported itself. Caught only because 0.7.1 hadn't reached PyPI.

### Testing

- **`tests/test_version_consistency.py`** (new) fails if the two version
  declarations ever diverge again. blockrun-llm has carried this guard since its
  own 1.4.6 drift; the sidecar had none, and drifted twice (0.4.2, then 0.7.1).

- **The contract test was vacuous again**, by a second mechanism. `_rejects()`
  ended in `except Exception: return False`, so deleting `ImageClient.edit` was
  swallowed into `False` and `assert not _rejects(...)` passed green while every
  Base edit call would 500. Unexpected exceptions propagate now, the method's
  existence is asserted up front, and the transport stub uses `raising=True` so
  a renamed SDK method can't leave a probe hitting the live gateway.

  That file has now been blind twice — first by reading signatures that
  `**kwargs` hides, now by swallowing exceptions. Both mutations are verified to
  fail.

## 0.7.1 — 2026-07-16

### Fixed

- **Short Seedance model ids now reach the gateway namespaced** (#20).
  Token360 documents `seedance-2.0-fast`, but the gateway's video catalog is an
  exact-match allowlist of provider-qualified ids (`getVideoModel` does
  `m.id === modelId`), so a bare id returned 400 "Unknown video model" — before
  payment, but also before generating anything. The gateway *does* rewrite bare
  ids to namespaced ones, yet only in `normalizeContentToFlat`, which is wired
  to the `content[]` route `/v1/videos` and never to the flat
  `/v1/videos/generations` the SDK posts to; the SDK doesn't normalize either.
  `_adapter.video_generation_async` now bridges it for both proxy video routes,
  mirroring the gateway's own `seedance-*` → `bytedance/seedance-*` rule. The id
  is lowercased on the way through, since the catalog match is case-sensitive.

## 0.7.0 — 2026-07-15

### Added

- **OpenAI-compatible image editing** — `POST /v1/images/edits` (alias
  `/v1/images/image2image`), accepting JSON data URIs or multipart
  `image`/`image[]`, multiple source images, and `mask`. Routes to
  `ImageClient.edit` on Base and `SolanaLLMClient.image_edit` on Solana. Adds
  `python-multipart` to the `proxy` extra.
- **`input_type` on `/v1/videos/generations`** — `text` / `image` /
  `first_last_frame` / `reference`. Declares the intended seed mode; the gateway
  cross-checks it against the seed fields and returns 400 **without charging**
  on disagreement, so a dropped `image_url` becomes an error instead of a
  text-to-video clip you paid for.
- **`quality` on the image endpoints** — `low` / `medium` / `high` / `auto` for
  `openai/gpt-image-*`. **Solana only**: the Base gateway has no `quality` field
  and strips unknown keys, so routing it there would silently drop it. Sending
  `quality` on Base returns a 400 naming the constraint rather than a 500.

### Changed

- **Dependency floors raised to `blockrun-llm>=1.7.0`** (base and `[solana]`),
  the release that adds `input_type` and Solana `quality`
  (BlockRunAI/blockrun-llm#24). The base floor moves too — unlike the Solana-only
  re-signing fix, `input_type` is a Base concern (`VideoClient.generate` has a
  closed signature, so an older core raises `TypeError` on it).

### Fixed

- **Dropped four params no published SDK accepted.** An earlier revision of this
  branch forwarded `quality`, `reference_videos`, `reference_audios`, and
  `input_type` to SDK clients with closed signatures — every one a `TypeError`
  on the first real call, on both chains. `input_type` and Solana `quality` are
  now real (SDK 1.7.0); reference-to-video stays out because both gateways gate
  it behind `R2V_ENABLED` and return 503.

- **A `prompt` sent as a multipart file part was billed as a Python repr.**
  `UploadFile` is truthy, so the required-field check passed and `str()` sent
  `"UploadFile(filename='p.txt', ...)"` upstream as the prompt — a paid
  generation against garbage, returning 200. `prompt` must now be non-blank
  text.
- **Non-string `image`/`mask` payloads on the JSON path** were forwarded
  verbatim for the gateway to reject; they are refused locally now.

### Security / limits

- **Multipart uploads are capped** — `BLOCKRUN_MAX_IMAGE_BYTES` (default 12MB,
  → 413) and `BLOCKRUN_MAX_IMAGE_PARTS` (default 16, → 400). Starlette spools
  large bodies to disk to keep them out of RAM; converting to a data URI reads
  them back and base64 inflates them ~1.33x, so an unbounded POST was fully
  buffered and encoded before the gateway could reject it. The part cap is a
  buffering guard only — per-model limits (openai/* up to 4, google/* up to 3)
  stay the gateway's call so they can't drift as the catalog changes.

### Testing

- `tests/test_sdk_param_contract.py` binds every forwarded param against the
  installed SDK's real signatures. The media tests fake the clients with
  `**kwargs`, which swallowed the invented params and let 96 green tests certify
  a surface that could not work; this contract test is what catches that class
  of bug. It also pins the Base-rejects-`quality` asymmetry so a future SDK
  change fails loudly instead of leaving the adapter's guard stale.

  Note the file needs **two** techniques: closed signatures (`VideoClient`, the
  Solana clients) can be read with `inspect`, but `ImageClient` declares
  `**kwargs` and validates at runtime, so it must be probed by *calling*.
  Reading it instead — the first attempt here — made the Base assertions
  unreachable, leaving the anti-vacuous-test file vacuous in exactly the way it
  exists to prevent.

## 0.6.1 — 2026-07-15

### Changed

- **Solana extra floor bumped to `blockrun-llm[solana]>=1.6.1`**
  (BlockRunAI/blockrun-llm#23). That release stops the SDK retrying payments a
  wallet can never make: a payer whose USDC token account was never created
  fails simulation with `InvalidAccountData`, which the gateway's coarse
  `invalidReason` collapses to `transaction_simulation_failed` — a reason the
  SDK classifies as *recoverable*, so it burned all 5 payment attempts. Each
  cost the gateway its own 4 verify retries: **20 facilitator calls per doomed
  request**, which is how one unfunded wallet showed up as a ~260-call storm.

  This is a floor rather than a preference — a pinned older SDK reintroduces
  the amplification in full. The fix also needs the gateway half
  (BlockRunAI/blockrun-sol#48) deployed to return the `invalidMessage` the SDK
  classifies on; against an older gateway the SDK degrades to the previous
  retry behaviour rather than misbehaving.

  Base-chain users are unaffected, so the base `blockrun-llm` floor is
  unchanged.

## 0.6.0 — 2026-07-08

Fixes the two LiteLLM-integration gaps a partner hit with the
`xai/grok-imagine-*` media models: image calls that LiteLLM recorded at **$0
spend**, and a video model that LiteLLM **could not call at all**.

### Added
- **OpenAI-compatible Videos API on the sidecar.** LiteLLM's video routes
  speak the OpenAI Videos spec against an `openai/`-provider `api_base` and
  never call the sidecar's native blocking `/v1/videos/generations` — which is
  why `xai/grok-imagine-video` was unreachable through a LiteLLM proxy. New
  routes:
  - `POST /v1/videos` — returns a video job object immediately; the blocking
    SDK submit+poll runs as a background task (gated by the media semaphore)
  - `GET /v1/videos/{id}` — status poll (`queued` → `in_progress` →
    `completed`/`failed`, OpenAI `error` shape on failure)
  - `GET /v1/videos/{id}/content` — streams the finished clip bytes

  OpenAI params are mapped to the gateway shape (`seconds` →
  `duration_seconds`; `size` "720x1280" → `resolution` + `aspect_ratio`);
  BlockRun-native params (`image_url`, `generate_audio`, …) pass through for
  direct callers and win over the mapped values. Jobs are in-memory with a
  24h TTL (`BLOCKRUN_VIDEO_JOB_TTL`) — poll the sidecar instance that took
  the create. `input_reference` file uploads are rejected with a clear 400
  pointing at `image_url` (the gateway takes URLs, not uploads).
- **`x-litellm-response-cost` response header** wherever the sidecar knows
  the real x402 charge (chat/messages passthrough). LiteLLM reads this exact
  header off openai-compatible upstreams (`additional_headers["llm_provider-
  x-litellm-response-cost"]`) and records it as the request's spend — so a
  LiteLLM proxy pointed at the sidecar now bills chat at the exact wallet
  deduction with zero config, instead of $0 (BlockRun models aren't in
  LiteLLM's price map).

### Fixed
- **Cost surfacing on the Base passthrough never fired in production**
  (live-verified during this release). Two gaps, found by exercising a real
  paid call:
  - the Base gateway emits the settlement header under its x402 v2 spec name
    `PAYMENT-RESPONSE` — the sidecar only checked the legacy
    `X-PAYMENT-RESPONSE`, so it decoded nothing (both names now accepted);
  - the v2 settlement payload carries **no amount field**, so even a decoded
    header couldn't price the call. The passthrough transport is now wrapped
    (`_SignedAmountTransport`): after the SDK signs a 402 retry, the exact
    authorized charge is decoded from the request's own `PAYMENT-SIGNATURE`
    (`payload.authorization.value`, 'exact' scheme — the same 402-quote
    number the SDK reports as `cost_usd`) and used as the cost fallback.

  Verified live on Base: a paid `deepseek/deepseek-chat` call now returns
  `x-blockrun-cost-usd: 0.001` + `x-litellm-response-cost: 0.001`.

### Docs
- README (EN + 中文), `docs/PROXY-FULL-SETUP.md(.zh)` and
  `examples/litellm_config.yaml`: LiteLLM `config.yaml` blocks for
  `xai/grok-imagine-image` / `-pro` / `grok-imagine-video` with the
  custom-pricing keys LiteLLM needs to bill media at BlockRun's list prices
  (`input_cost_per_pixel` = flat per-image price ÷ 1048576;
  `output_cost_per_second: 0.05`), `model_info.mode`
  (`image_generation`/`video_generation`), and the always-pass-`seconds`
  caveat for video spend.

## 0.5.0 — 2026-07-06

### Added
- **Media endpoints on the FastAPI sidecar** (#14). The proxy only exposed
  `/v1/images/generations`, so OpenAI-compatible clients (LiteLLM) could not
  reach the gateway's video/audio models. Adds:
  - `POST /v1/videos/generations`
  - `POST /v1/audio/speech`
  - `POST /v1/audio/generations` (music)
  - `POST /v1/audio/sound-effects`

  The adapter dispatches Base (dedicated `VideoClient`/`MusicClient`/
  `SpeechClient`) vs Solana (unified `SolanaLLMClient`) per request; sync SDK
  clients run in a shared thread pool. Validated live end-to-end on Solana
  mainnet.

### Changed
- **Require `blockrun-llm[solana]>=1.5.0`** for the `solana` extra — Solana
  media (`SolanaLLMClient.video/music/speech/sound_effect`) landed in the SDK's
  1.5.0 release (blockrun-llm #16; also carries #17 rpc_batch cache fix, #18
  `solana<0.40` pin, #19 media hardening). Older SDKs degrade Solana media to a
  clear 501, not a 500. The core `blockrun-llm>=1.4.7` floor is unchanged — Base
  media clients predate it.
- Synced `__version__` (was stale at 0.4.2).

### Hardened (#15, media-endpoints review follow-up)
- `ValueError → 400` on **all** media routes via a shared `_media_endpoint`
  helper (was video-only; music-with-lyrics 500'd instead of returning the SDK's
  clear message).
- Solana media on a pre-1.5.0 SDK degrades to a clear **501** upgrade hint
  instead of an `AttributeError` 500.
- Long media (video 60–900s, music 60–210s) moved to a dedicated 8-thread pool
  (`BLOCKRUN_LONG_MEDIA_THREADS`); all media routes gated behind their own
  semaphore (`BLOCKRUN_MEDIA_MAX_CONCURRENT`, default 20) so a video burst can no
  longer starve images or brick chat/messages.
- Client-supplied `budget_seconds`/`timeout` clamped to the 900s server cap
  (was forwarded verbatim — one request body could pin a worker for a day);
  every media call wrapped in `asyncio.wait_for` so the coroutine + semaphore
  permit always release even if the SDK thread wedges (504).
- Media calls now audit-log via `log_proxy_call` and surface the in-body
  settlement txHash as `x-blockrun-settlement` (paid media traffic was invisible
  to spend reconciliation). NB: media SDK responses don't expose `cost_usd` yet,
  so media audit rows log `cost_usd=None` with only the settlement txHash.
- Full negative-path pytest suites for all 5 media routes + adapter
  dispatch/clamp/guard/ceiling tests.

## 0.4.7 — 2026-06-26

### Added
- **Real x402 cost on the FastAPI sidecar passthrough** (#12, proxy half — closes
  #12). The raw `/v1/chat/completions` + `/v1/messages` passthrough relayed bytes
  and surfaced no cost. It now decodes the gateway's `X-PAYMENT-RESPONSE` header
  (the real on-chain charge) on the upstream response and:
  - adds `x-blockrun-cost-usd` + `x-blockrun-settlement` **response headers** so a
    calling agent / downstream proxy can track real spend per request (streaming
    and non-streaming), and
  - writes an **opt-in JSONL audit row** (`logger.log_proxy_call`, gated on
    `BLOCKRUN_LITELLM_LOG`, `mode='proxy_passthrough'`, `cost_source='blockrun_x402'`)
    mirroring the custom-provider schema.

  Race-free: the charge is read from the per-response header, not a shared signing
  transport. Free / cached calls (no header) surface no cost — graceful. The
  passthrough body is forwarded byte-for-byte, unchanged.

## 0.4.6 — 2026-06-26

### Added
- **Real x402 cost on streaming calls** (#12, streaming half). The provider now
  threads the SDK's per-call charge (`ChatCompletionChunk.cost_usd`, blockrun-llm
  >=1.4.7) onto the **assembled** streamed response's `_hidden_params`, so:
  - LiteLLM's `response_cost` (spend / `max_budget`) reflects the real wallet
    deduction — set via `additional_headers['llm_provider-x-litellm-response-cost']`,
    the only channel that survives LiteLLM's stream aggregation (which drops
    cache fields and never recomputes a provider charge), and
  - the JSONL audit records `blockrun_cost_usd` / `cost_source='blockrun_x402'`
    on streamed rows, matching the non-stream path.

  Race-free: the charge rides on the per-call chunk, not the shared
  `client._last_call_cost`. Free/cached streams report `0.0`; an older SDK
  without `cost_usd` falls back to LiteLLM's estimate.

### Changed
- **Require `blockrun-llm>=1.4.7`** for the streamed `ChatCompletionChunk.cost_usd`.

### Not yet covered
- Proxy-server mode (`/v1/chat/completions` + `/v1/messages` raw passthrough)
  still bills off config pricing, not the real charge — tracked in #12 (proxy
  half). The passthrough bypasses the SDK's cost path entirely.

## 0.4.5 — 2026-06-26

### Added
- **Forward `thinking` / `reasoning_effort` through the custom provider** (#13).
  These were silently dropped by the kwarg whitelist, so callers could never
  trigger Anthropic extended thinking (or OpenAI reasoning effort) via the
  LiteLLM custom provider. The gateway forwards them to the upstream model.

### Changed
- **Raise the chat HTTP timeout to 600s** (#13), overridable via
  `BLOCKRUN_CHAT_TIMEOUT`. The SDK default of 120s was too low for reasoning
  models (opus-4.8 / deepseek routinely take 200–300s+); non-stream calls timed
  out mid-generation while the gateway kept billing server-side. Applied to all
  four SDK chat clients (sync/async × Base/Solana) **and** the proxy-server
  `/v1/chat/completions` + `/v1/messages` passthrough (previously a hardcoded
  300s) — the latter is the heavy agentic / Claude Code path.

### Fixed
- A malformed `BLOCKRUN_CHAT_TIMEOUT` (e.g. `"600s"`) no longer crashes
  `_adapter` import; it falls back to the 600s default, mirroring
  `BLOCKRUN_SOLANA_IMAGE_TIMEOUT`.

## 0.4.4 — 2026-06-24

### Changed
- **Require `blockrun-llm>=1.4.6`** — guarantees the SDK attaches the real
  per-call x402 charge to `ChatResponse` (`cost_usd` / `settlement`, race-free).
  The exact-cost reporting added in 0.4.3 now uses the authoritative on-chain
  charge rather than the best-effort `_last_call_cost` fallback.

## 0.4.3 — 2026-06-24

### Added
- **Report the real x402 wallet charge instead of LiteLLM's estimate** (#11).
  In-process custom-provider mode surfaces the SDK's real per-call charge
  (`response.cost_usd`) into `_hidden_params["response_cost"]`, so LiteLLM's
  spend / `max_budget` reflect the **actual wallet deduction** (which carries
  the per-call floor + margin) rather than a token×list-price estimate. Also
  exposes `blockrun_cost_usd` / `blockrun_settlement`, and the JSONL log now
  records `cost_usd` (real when known), `cost_source`
  (`blockrun_x402` vs `litellm_estimate`), `estimated_cost_usd`, and
  `settlement`.
  - Non-streaming in-process path only. Needs a `blockrun-llm` that attaches
    `response.cost_usd` for the race-free path; otherwise falls back to a
    best-effort (`_last_call_cost`) value. Proxy-server mode is unchanged —
    see the per-model pricing in 0.4.2.

## 0.4.2 — 2026-06-24

### Changed
- **Require `blockrun-llm>=1.4.1`** (#8) so `pip install -U blockrun-litellm`
  pulls the SDK fix.

### Docs / examples
- **Example LiteLLM proxy config now sets per-model pricing**
  (`input_cost_per_token` / `output_cost_per_token`, plus cache costs for Claude)
  so team/key budgets (e.g. a `$200` cap) actually enforce — without these the
  proxy prices blockrun model ids at `$0` and the cap never trips. Documents the
  requirement, the unpriced `blockrun/*` wildcard hole, and the cache/streaming
  accuracy caveats; points at the gateway reconciliation report as the exact
  cost source of truth (#10).
- README links back to blockrun.ai/docs (#9).

## 0.4.1 — 2026-06-14

### Fixed
- **`/v1/chat/completions` is now a verbatim x402-signed passthrough — streamed
  tool calls no longer crash.** The endpoint previously went through the SDK's
  typed `chat_completion_stream`, which rejects streaming tool-call
  argument-fragment frames, falls back to `model_construct` (leaving `choices` as
  raw dicts), then crashes in the archive loop with `'dict' object has no
  attribute 'delta'`. It now rides the same `_forward_passthrough` helper as
  `/v1/messages`, so every OpenAI client — including **Codex with
  `wire_api=chat`** — keeps streamed `tool_calls` intact.
- **A Solana RPC fault during x402 signing maps to `503`, not a bare `500`.**
  `_forward_passthrough` (used by `/v1/chat/completions`, `/v1/messages`, and
  `/v1/messages/count_tokens`) now catches a `SolanaRpcException` raised before
  any upstream status exists and surfaces a clean `503`; non-Solana faults still
  propagate. Restores the behaviour the old typed chat path had.
- **`__version__` was stale at `0.3.14`** while `pyproject.toml` had moved to
  `0.4.0`; the two are back in sync.

### Internal
- Generalized `_forward_anthropic` → `_forward_passthrough(headers)` and removed
  the now-dead `_sse_event_stream` / `_openai_error_event` SDK-streaming helpers.
- The litellm `/v1/messages` end-to-end tool-call test auto-skips when the
  installed litellm validates the custom provider against its `LlmProviders`
  enum (it isn't in that enum), so a plain `pytest` is green by default. Added
  passthrough coverage for `/v1/chat/completions` and the new `503` mapping.

## 0.4.0 — 2026-06-12

### Added
- **Native Anthropic `/v1/messages` passthrough (Base + Solana).** The sidecar
  now exposes `POST /v1/messages` and `POST /v1/messages/count_tokens` and
  forwards them verbatim to BlockRun's native Anthropic endpoint, adding only the
  x402 signature — `tools` / `tool_choice` / `thinking` / streaming pass through
  untouched. One sidecar now serves Claude Code (`/v1/messages`), Codex, and any
  OpenAI client with no lossy Anthropic↔OpenAI translation layer. Solana signing
  goes through a new lock-guarded `_SolanaX402Transport`.
- **Streaming tool calls reach the Anthropic bridge with their arguments.** The
  in-process custom provider (`litellm.completion(model="blockrun/…")`) now splits
  each complete SDK tool call into the name/arguments frames LiteLLM's Anthropic
  adapter expects (`_iter_stream_chunks`).

### Fixed
- **Parallel tool calls no longer collapse onto one content block.** BlockRun's
  `ToolCall` carries no per-call `index`, so the previous `getattr(tc, "index", 0)`
  was always `0` — every parallel tool call landed on Anthropic block index 0 and
  all but the last were dropped (the agentic pattern Claude Code uses most). The
  stream now assigns a stream-scoped monotonic block index, fixed across chunks.
- **Streaming upstream errors keep their real status.** A 4xx/5xx on a streaming
  `/v1/messages` was delivered as HTTP 200 `text/event-stream` with a non-SSE
  error body, which the Anthropic SDK would mis-parse or hang on. The upstream is
  now opened before headers are committed, so a genuine error returns a real error
  `Response` with the upstream status and body.
- **The Anthropic routes now respect the concurrency cap.** `/v1/messages` and
  `/v1/messages/count_tokens` were the only paid routes not gated by
  `_get_semaphore()`; under agentic load they could stampede the gateway past
  `BLOCKRUN_MAX_CONCURRENT`. Both now hold the semaphore for the paid upstream
  call (released before the streaming body drain), and `count_tokens` now forwards
  the inbound query string like `/v1/messages`.

## 0.3.14 — 2026-06-11

### Fixed
- **Slow Solana image models no longer time out mid-generation.** The image
  `SolanaLLMClient` is now constructed with an explicit `image_timeout` (default
  300s, overridable via `BLOCKRUN_SOLANA_IMAGE_TIMEOUT`). `openai/gpt-image-2`
  can take well past the SDK's 200s `image_timeout` default on the synchronous
  Solana path; under the default the sidecar threw `httpx.ReadTimeout` before
  the gateway returned the image, surfacing as a 500 (and LiteLLM-Proxy retries)
  even though the gateway had produced the result.
  - This supersedes the original community PR (#1, thanks @KillerQueen-Z), which
    set the general `timeout=` kwarg. Against the current `blockrun-llm` SDK that
    is the **chat** baseline and is overridden per-request for images
    (`_request_image_with_payment` applies `image_timeout`), so it never reached
    image calls. The dedicated `image_timeout=` is the knob that governs them.
  - `tests/test_adapter_solana.py` asserts `image_timeout` is passed (≥ the slow
    tail) and that the env override applies (read at call time, no module reload).

## 0.3.13 — 2026-06-11

### Fixed
- **Streaming `reasoning_content` is now forwarded instead of dropped.**
  Thinking-enabled Claude (Bedrock / direct Anthropic) and native reasoning
  models (DeepSeek R1, GLM thinking) emit their chain-of-thought on the streamed
  delta (`choices[0].delta.reasoning_content`). LiteLLM's
  `GenericStreamingChunk` has no reasoning field and the custom-provider stream
  handler never promotes it onto `delta.reasoning_content`, so
  `_to_generic_chunk` previously lost it on every streamed call. It now routes
  the delta's reasoning into `provider_specific_fields` — the only channel a
  CustomLLM provider has to a live streaming consumer, readable at
  `delta.provider_specific_fields["reasoning_content"]`. Non-stream reasoning is
  unchanged (it already survives verbatim on `message.reasoning_content`). New
  `tests/test_reasoning.py` covers the unit mapping and an end-to-end pass
  through the real `CustomStreamWrapper`.
  - Pairs with the gateway change (blockrun-sol) that forwards extended
    `thinking` to the Bedrock path and emits `reasoning_content` deltas, so a
    Bedrock-served Claude stream now carries reasoning end-to-end.
  - Caveat (LiteLLM limitation): `stream_chunk_builder` does not preserve
    per-chunk `provider_specific_fields`, so a reassembled message will not carry
    `reasoning_content`. Read it from the live delta, or use non-stream for the
    assembled field.

## 0.3.12 — 2026-06-10

### Fixed
- **Provider-mode streaming now forwards the gateway's end-of-stream `usage`
  frame.** The gateway (paired change: blockrun-sol #20 / blockrun #137) emits
  the OpenAI `include_usage` final frame (`choices: [] + usage`) carrying the
  real upstream token counts. `_to_generic_chunk` previously returned
  `usage=None` on the choice-less branch, so LiteLLM never saw those counts and
  re-estimated the prompt with its own tokenizer (~37% drift vs the real
  Anthropic count). The choice-less branch now forwards `chunk.usage` when
  present; older gateways that never send the frame hit the `usage=None` path
  unchanged.
- **The forwarding contract is locked in by `tests/test_stream_usage.py`.**
  The usage frame arrives *after* the finish-reason chunk, and LiteLLM's
  `CustomStreamWrapper` drops any post-finish chunk whose dict lacks the
  `provider_specific_fields` key — the frame only survives because
  `_to_generic_chunk` always emits that key (added in 0.3.10's fingerprint
  passthrough). New tests cover the token counts (unit + end-to-end through
  the real `CustomStreamWrapper` / `stream_chunk_builder`), the key-presence
  contract, and a canary that fails if LiteLLM's post-finish guard ever
  changes.

## 0.3.11 — 2026-06-01

### Added
- **OpenAI Responses API bridge — `POST /v1/responses`.** The sidecar previously
  404'd on `/v1/responses` (only Chat Completions was implemented), so LiteLLM
  clients calling the Responses API got `NotFoundError`. It now accepts a
  Responses request (`input` as string or item list, `instructions`,
  `temperature`, `top_p`, `max_output_tokens`, `tools`), translates it to a chat
  completion against the gateway, and translates the result back: a `response`
  object (with `output[]`, `output_text`, `usage.input/output/total_tokens`) for
  non-streaming, or the canonical `response.*` SSE event sequence
  (`response.created` → `output_item.added` → `content_part.added` →
  `output_text.delta*` → `*.done` → `response.completed`) when `stream=True`.
  Text-in/text-out is fully bridged; Responses-only state (tools-as-state,
  reasoning items, `previous_response_id`, `store`) is not round-tripped — use
  `/v1/chat/completions` for those. New `tests/test_responses.py`.

## 0.3.10 — 2026-06-01

### Added
- **Native fingerprint passthrough is now guaranteed and tested.** The gateway
  returns the upstream provider's response verbatim, so relay-detection signals
  (`system_fingerprint`, `service_tier`, `usage.prompt_tokens_details` /
  `cache_read_input_tokens` / `cache_creation_input_tokens`, per-message
  `reasoning_content`) survive both integration modes. New
  `tests/test_fingerprint.py` locks the contract so a future LiteLLM / SDK bump
  can't silently strip them. New README section "Native fingerprint passthrough".
- **Provider-mode streaming now carries the native fingerprint.** The lossy
  `GenericStreamingChunk` previously dropped everything but text/usage; chunk
  extras (`system_fingerprint`, `service_tier`) are now surfaced via
  `provider_specific_fields` (new `_native_extras()` helper in `provider.py`).

### Changed
- **`blockrun-llm` floor raised to `>=0.37.0`** (runtime + `[solana]` extra) for
  concurrency-safe Solana payments. The adapter shares one cached SDK client
  across the proxy's concurrent requests; with the older floor that shared client
  raced on x402 nonce/auth state under load and returned a small fraction of
  `Payment verification failed` / `authorization already used` rejections. 0.37.0
  adds a per-client signing lock + whole-request payment retry, taking concurrent
  single-wallet load to ~100% (verified opus-4.7 / gemini-3.1-pro / gpt-5.5
  100/100 at concurrency 10). No proxy code change needed — the bump is enough.
- **Model ids aligned to the current gateway flagships** across the example
  config, README, and example scripts: `anthropic/claude-opus-4-5` →
  `anthropic/claude-opus-4-7`, `google/gemini-3-pro` → `google/gemini-3.1-pro`.
  `openai/gpt-5.5` unchanged.

## 0.3.8 — 2026-05-27

### Fixed
- **Solana image generation now uses `SolanaLLMClient`.** `get_image_client()`
  previously returned the EVM-only `ImageClient` regardless of `api_url`, so
  Solana image requests signed EIP-712 payments and crashed at x402 settlement
  with `transaction_simulation_failed`. Branches on `_is_solana_url(api_url)`
  the same way `get_sync_client()` / `get_async_client()` already did.
- New `_invoke_image_generate()` dispatcher calls `SolanaLLMClient.image()`
  on Solana and `ImageClient.generate()` on Base from both sync and async
  paths.

### Changed
- `blockrun-llm` floor raised to `>=0.31.1` (sync + solana extras) for the
  new `SolanaLLMClient.image()` method.

## 0.3.9 — 2026-05-28

### Fixed
- **Sidecar now surfaces the gateway's real 402 reason.** Previously a
  `PaymentError` from the SDK was returned as a flat
  `{"detail": "Payment rejected. Check your Solana USDC balance."}`,
  even when the real failure was an x402 facilitator settlement error
  (`transaction_simulation_failed`, `insufficient_funds`,
  `payment_expired`, etc.). With `blockrun-llm >= 0.32.0` the SDK
  preserves the gateway body in `PaymentError.response`, and the proxy
  now returns it as `{"error": "...", "details": "..."}` on both the
  `/v1/chat/completions` and `/v1/images/generations` routes, plus
  folds it into the streaming error event so SSE clients see it too.
- **Slow image models (`openai/gpt-image-2`, `openai/dall-e-3`,
  `google/nano-banana-pro` at 4K) now complete via the SDK's new
  202 + poll loop instead of crashing with a Pydantic ValidationError
  on the job stub.** No proxy code changed for this — bumping the
  `blockrun-llm` floor to `>=0.32.0` is enough.

### Changed
- **`blockrun-llm` floor raised to `>=0.32.0`** (for both the runtime
  install and the `[solana]` extra) so the PaymentError surfacing and
  image-poll fix are guaranteed to be present.

## 0.3.7 — 2026-05-19

### Changed

- **Default `BLOCKRUN_MAX_CONCURRENT` raised from 20 → 100.**
  The previous default was a conservative development-time value. The httpx
  pool is configured for 200 connections; each paid request uses 2 connections
  (402 probe + authenticated call), so 100 concurrent requests is the natural
  ceiling before the pool itself becomes the bottleneck.

- **Streaming semaphore released after first chunk (early release).**
  Previously the semaphore was held for the entire stream duration, meaning a
  slow client reading at 1 token/s occupied a concurrency slot for the full
  generation time (potentially 30–60 s). Now the semaphore is released as soon
  as the first chunk arrives from upstream — covering only the x402 probe +
  sign + HTTP handshake. Remaining chunks stream freely without holding a slot.
  In practice this means 100 concurrent streams can proceed simultaneously even
  with very slow clients; the upstream provider's RPM/TPM is the actual limit.

- **Image generation uses a bounded thread pool (max 20 workers).**
  Previously `image_generation_async` used `loop.run_in_executor(None, ...)`,
  which dispatches to Python's default unbounded thread pool. Under heavy image
  load this could spawn hundreds of threads. Now uses a module-level
  `ThreadPoolExecutor(max_workers=20)` that caps image concurrency and prevents
  memory exhaustion.

## 0.3.6 — 2026-05-18

### Added

- **`POST /v1/images/generations` endpoint in the sidecar proxy.**
  The proxy now exposes an OpenAI-compatible image generation endpoint.
  Accepts `prompt`, `model`, `size`, `n`; returns OpenAI `ImageResponse`
  format (`created` + `data[].url`). Supports any model in BlockRun's
  image catalog: `google/nano-banana`, `google/nano-banana-pro`,
  `openai/dall-e-3`, `openai/gpt-image-1`, `openai/gpt-image-2`,
  `xai/grok-imagine-image`, `zai/cogview-4`, etc.
  Uses `ImageClient` under the hood (sync, run via thread executor for
  async compatibility). Cached per wallet/url like all other clients.

## 0.3.5 — 2026-05-15

### Fixed

- **Sidecar proxy: `MidStreamFallbackError` on paid models under concurrent load.**
  Two root causes, both fixed:

  1. **Wrong SSE error format.** When BlockRun's upstream returned a 5xx
     mid-stream, the sidecar embedded the error as
     `data: {"error": {"type": ..., "status": ...}}`.  The `openai` Python SDK
     (v1.x, Rust parser) only recognises error events that follow OpenAI's
     exact schema: `{"error": {"message", "type", "param", "code"}}`.  The
     non-conforming `"status"` key caused the Rust untagged-enum parser to fail
     with *"data did not match any variant of untagged enum Resp"*, which
     LiteLLM then wrapped as a confusing `MidStreamFallbackError`.  Fixed by
     adding `_openai_error_event()` that always emits the correct four-field
     schema.

  2. **No concurrency cap.** 100 simultaneous requests to a paid model like
     `claude-opus-4.7` all signed x402 payments and hit the Anthropic upstream
     concurrently, exhausting its TPM/RPM limits and causing 500s.  The sidecar
     now acquires an `asyncio.Semaphore` before each request (streaming and
     non-streaming alike), limiting in-flight requests to `BLOCKRUN_MAX_CONCURRENT`
     (default 20, configurable via env var).  Excess requests queue inside the
     sidecar rather than flooding the upstream.

### Added

- `BLOCKRUN_MAX_CONCURRENT` env var — set on the sidecar to tune the
  concurrency cap.  Default is 20, which keeps Anthropic's paid-tier limits
  comfortable.  Free models (e.g. `nvidia/deepseek-v4-flash`) are not subject
  to Anthropic's caps so you can raise this for free-only workloads.

## 0.3.4 — 2026-05-14

### Changed
- **Dependency bump:** `blockrun-llm>=0.24.0` (`solana` extra:
  `blockrun-llm[solana]>=0.24.0`). The new SDK switches the default
  Solana RPC endpoint to BlockRun's own multi-region Tatum-backed
  proxy (`https://sol.blockrun.ai/api/v1/solana/rpc`) — partners no
  longer need to register their own Helius / Tatum / QuickNode
  account. See `blockrun-llm 0.24.0` changelog for the migration
  details. Zero code change in this adapter for the change to take
  effect; just upgrade and the default URL flows through.

### Verified e2e
- `litellm.completion(model="blockrun/zai/glm-5.1", api_base="https://sol.blockrun.ai/api", api_key=<solana_key>)`
  — paid Solana settlement succeeded, $0.001 USDC debited on-chain,
  cost log recorded `network=solana-mainnet` /
  `client=SolanaLLMClient`. Blockhash fetched via the BlockRun
  proxy (no partner-side RPC config needed).

## 0.3.3 — 2026-05-13

### Fixed
- **Critical: sidecar (Mode B) Solana paid calls were silently routing
  through the Base async client.** Caused
  ``EncodingTypeError: Value 'EPjFW...' of type <class 'str'> cannot be
  encoded by AddressEncoder`` when the EVM EIP-712 encoder met the
  Solana USDC mint address (base58, not 0x-hex).

  Root cause: ``_is_solana_url(api_url)`` only checked the function
  argument, never the ``BLOCKRUN_API_URL`` env var. The FastAPI
  sidecar's request handlers don't forward an ``api_url`` arg to the
  adapter — they configure the chain via env var at startup
  (``blockrun-litellm-proxy --api-url https://sol.blockrun.ai/api``).
  So ``_is_solana_url(None)`` always returned ``False`` and routed
  every request to ``AsyncLLMClient`` (Base) regardless of the
  sidecar's actual chain. Mode A wasn't affected because callers pass
  ``api_base`` explicitly.

  Fix: ``_is_solana_url`` now falls back to ``BLOCKRUN_API_URL`` when
  no argument is passed, so sidecar requests reach
  ``AsyncSolanaLLMClient`` correctly.

  Free-model calls escaped detection because they skip the payment
  encoder entirely; only paid Solana calls hit the bug.

## 0.3.2 — 2026-05-12

### Fixed
- **Tool calling works on Solana now.** The adapter previously dropped
  ``tools`` / ``tool_choice`` on the Solana path because the SDK didn't
  accept them. Combined with ``blockrun-llm 0.22.1`` (which adds
  ``tools`` / ``tool_choice`` to the Solana SDK methods), function
  calling now works uniformly on Base **and** Solana. The adapter's
  kwarg filter no longer special-cases the Solana chain — every chain
  forwards the same OpenAI-style params.

### Dependency bump
- ``blockrun-llm>=0.22.1``.

## 0.3.1 — 2026-05-12

### New
- **Async Solana is now supported.** ``litellm.acompletion(...)`` and
  ``litellm.acompletion(stream=True)`` with ``api_base="https://sol.blockrun.ai/api"``
  no longer raise ``NotImplementedError``. The adapter routes async
  Solana calls to the new ``AsyncSolanaLLMClient`` introduced in
  ``blockrun-llm 0.22.0``, completing parity with Base across both
  sync/async × stream/non-stream.

### Improved
- **Paid streaming now writes the local cost log + archive.** This is
  inherited from ``blockrun-llm 0.22.0``: every paid streaming call —
  Base or Solana, sync or async — produces a row in
  ``~/.blockrun/cost_log.jsonl`` and a full request/response archive
  in ``~/.blockrun/data/`` once the stream finishes. Closes the audit
  gap that streaming had relative to non-streaming.

### Dependency bump
- ``blockrun-llm>=0.22.0``.

### Verified e2e
- ``await litellm.acompletion(model="blockrun/nvidia/deepseek-v4-flash",
  api_base="https://sol.blockrun.ai/api", api_key=solana_key,
  stream=True)`` returned ``"Hello! How can I"`` — full async Solana
  stream chain through LiteLLM, first attempt.

## 0.3.0 — 2026-05-12

### New
- **Solana chain support.** The adapter now dispatches to
  ``SolanaLLMClient`` when ``api_url`` / ``api_base`` points at the
  Solana gateway (``sol.blockrun.ai``), and to ``LLMClient`` (Base)
  otherwise. Both modes (Python library + LiteLLM Proxy sidecar)
  understand both chains.

  ```python
  # Base (default)
  litellm.completion(
      model="blockrun/openai/gpt-5.5",
      messages=[...],
      api_key="0xBASE_PRIVATE_KEY",
  )

  # Solana
  litellm.completion(
      model="blockrun/openai/gpt-5.5",
      messages=[...],
      api_base="https://sol.blockrun.ai/api",   # ← decides chain
      api_key="solana-private-key",
  )
  ```

- **Streaming on Solana** end-to-end. Requires
  ``blockrun-llm>=0.21.0`` which adds
  ``SolanaLLMClient.chat_completion_stream``.
- ``tools`` / ``tool_choice`` are silently dropped on the Solana path
  (the Solana SDK doesn't support function calling yet) instead of
  raising — same UX as LiteLLM's ``drop_params``.

### Constraints
- **Solana async is not implemented.** ``litellm.acompletion(...)`` with
  a Solana ``api_base`` raises ``NotImplementedError`` because the SDK
  has no async Solana client. Use sync, or wrap in
  ``asyncio.to_thread()``. Roadmap.
- Solana support requires the optional extra: ``pip install
  'blockrun-litellm[solana]'`` (pulls the x402 SVM toolchain). A clear
  ``ImportError`` fires at first call if it's missing.

### Tests
- 8 new unit tests in ``tests/test_adapter_solana.py`` covering URL
  recognition, the ``tools`` drop on Solana, async-Solana
  ``NotImplementedError``, sync routing to ``SolanaLLMClient``, and a
  clear error when the extra is missing.

### Verified e2e
- Live ``litellm.completion(api_base="https://sol.blockrun.ai/api",
  stream=True)`` returned `"Hello! How can I"` over a real Solana
  wallet (loaded from ``~/.blockrun/.solana-session``).

### Dependency bump
- ``blockrun-llm>=0.21.0`` (Solana streaming).
- New ``[solana]`` extra → ``pip install 'blockrun-litellm[solana]'``.

## 0.2.2 — 2026-05-12

### New
- **Local JSONL request logger.** Opt-in observability that captures
  every LiteLLM call — input messages, completion content, token
  usage (`prompt_tokens` / `completion_tokens` / `total_tokens`),
  latency, `stream` flag, optional `cost_usd`, and on failures the
  `error_type` + `error_message`. Writes one JSON object per line to
  `~/.blockrun/litellm_calls.jsonl` (override via
  `BLOCKRUN_LITELLM_LOG` env var or explicit `path` arg).
- **Two integration paths:**
  - **Python library mode** — one line at startup:
    ```python
    from blockrun_litellm import enable_local_logging
    enable_local_logging()
    ```
  - **LiteLLM Proxy Server mode** — drop a tiny `custom_callbacks.py`
    bridge next to `config.yaml` (LiteLLM Proxy loads callbacks by
    filename, not by installed package), then reference
    `["custom_callbacks.blockrun_logger"]` in `litellm_settings.callbacks`.
    See [`examples/custom_callbacks.py`](examples/custom_callbacks.py).
- Built on LiteLLM's `CustomLogger` interface, so **streaming failures
  are captured uniformly** alongside success and non-stream paths.
  Intermediate streaming-chunk fires are deduplicated — exactly one
  row per call.

### Tests
- 13 new unit tests in `tests/test_logger.py` covering: success/
  failure rows, intermediate-chunk dedupe, dict-vs-pydantic response
  shapes, async hook delegation, env-var path resolution,
  `enable_local_logging` idempotency, and the module-level
  `proxy_logger` singleton.

### Verified e2e
- **Mode A** — `enable_local_logging()` + `litellm.completion()`
  writes one JSONL row per call (live BlockRun + LiteLLM 1.83.14).
- **Mode B** — full chain `LiteLLM Proxy (4000) → BlockRun sidecar
  (4001) → blockrun.ai` with `callbacks: ["custom_callbacks.blockrun_logger"]`
  produces a row with `completion="Serendipity"`,
  `usage={prompt_tokens:10, completion_tokens:4, total_tokens:14}`.

## 0.2.1 — 2026-05-12

### Improved
- **Transient errors now translate to LiteLLM's retriable exception
  hierarchy** so the router's own fallback and retry machinery picks
  up where the SDK leaves off. Mapping (in `_translate_to_litellm`):
  - `APIError(500)` → `litellm.InternalServerError`
  - `APIError(502)` / `APIError(504)` → `litellm.APIConnectionError`
  - `APIError(503)` → `litellm.ServiceUnavailableError`
  - `APIError(429)` → `litellm.RateLimitError`
  - `httpx.TimeoutException` → `litellm.Timeout`
  - `httpx.NetworkError` → `litellm.APIConnectionError`
  Everything else propagates unchanged so callers see the real error.
  Applied uniformly to `completion()`, `acompletion()`, `streaming()`,
  and `astreaming()`. Combined with the SDK's in-band 5xx retry (added
  in `blockrun-llm` 0.20.1), most transient hiccups self-heal before
  ever reaching the caller — and the ones that don't are now marked
  retriable so `litellm.Router` can switch providers automatically.
- **Dependency bump:** `blockrun-llm>=0.20.1` (the SDK release that
  ships the matching retry / `fallback_models` improvements).

### Tests
- Seven new unit tests in `tests/test_provider.py` covering the 5xx /
  429 / Timeout / Network translations and verifying that
  non-transient errors (e.g. `RuntimeError`) pass through unchanged.
- Replaced the stale `test_streaming_request_is_rejected` test
  (which asserted v0.1.0's `StreamingNotSupported`) with a kwarg-leak
  check that the non-streaming entrypoint silently drops `stream=True`.

## 0.2.0 — 2026-05-12

### New
- **Streaming (`stream=True`) end-to-end.** Both integration modes now
  speak SSE:
  - **Provider mode** — `BlockRunLLM` implements
    `CustomLLM.streaming()` and `astreaming()`, yielding LiteLLM
    `GenericStreamingChunk` objects so `litellm.completion(..., stream=True)`
    just works.
  - **Proxy mode** — `POST /v1/chat/completions` returns
    `text/event-stream` when `stream=True`, emitting OpenAI-style
    `data: <json>\n\n` events and a terminating `data: [DONE]`. Errors
    raised mid-stream are surfaced as a final `data: {"error": ...}`
    event rather than HTTP errors (headers already flushed).
- **Adapter API:** `_adapter.chat_completion_stream_sync()` and
  `chat_completion_stream_async()` are the new public entrypoints.
  `StreamingNotSupported` is removed — the previous "fail-fast at the
  adapter" behavior is no longer needed.
- **Dependency bumped:** `blockrun-llm>=0.20.0` (this is the SDK release
  that introduces `chat_completion_stream()`).

### Removed
- `_adapter.StreamingNotSupported` — replaced by real streaming support.

### Notes
- Caveats inherited from the BlockRun gateway: `search_parameters` and
  the Responses-API models (`codex`, `gpt-5.4-pro`) reject streaming
  server-side with 400. The adapter does not pre-filter those — clients
  see the same 400 their own LiteLLM call would surface.

## 0.1.0 — 2026-05-11

Initial release. Published to PyPI as
[`blockrun-litellm`](https://pypi.org/project/blockrun-litellm/0.1.0/) and
GitHub at [BlockRunAI/blockrun-litellm](https://github.com/BlockRunAI/blockrun-litellm).

### Verified
- Provider mode (`litellm.completion(model="blockrun/nvidia/deepseek-v4-flash", ...)`)
  returns a real `litellm.ModelResponse` from the live BlockRun gateway.
- Proxy mode (`POST http://127.0.0.1:4001/v1/chat/completions`) returns a
  valid OpenAI Chat Completions JSON response from the live gateway.
- Fresh-venv install (`pip install 'blockrun-litellm[proxy]'`) imports
  cleanly and registers the `blockrun-litellm-proxy` CLI.

### Added
- `BlockRunLLM` — LiteLLM `CustomLLM` handler routing through the BlockRun gateway.
- `register()` — idempotent registration of the `blockrun` provider in `litellm.custom_provider_map`.
- `blockrun-litellm-proxy` — local OpenAI-compatible FastAPI proxy (`pip install 'blockrun-litellm[proxy]'`).
- Endpoints on the proxy: `POST /v1/chat/completions`, `GET /v1/models`, `GET /healthz`, `GET /docs`.
- Optional shared-secret guard via `BLOCKRUN_PROXY_TOKEN`.
- Examples: `python_lib.py`, `raw_openai_sdk.py`, `litellm_config.yaml`.
- Bilingual README (English + 中文).

### Known limitations
- **Streaming (`stream=True`) is not wired in this adapter** — surfaces as HTTP 400. The BlockRun gateway itself fully supports SSE (`text/event-stream`) for both free and paid models; the gap is on the `blockrun-llm` SDK client side, which this adapter wraps. Earlier copy in this file blamed "x402 per-request settlement" — that was wrong, x402 is orthogonal to SSE.
- During e2e the pydantic serializer emits a warning that LiteLLM's `Message` model expects 9 fields and BlockRun returns 5 (extras like `function_call`, `audio`, `annotations` are absent). Functionally harmless — LiteLLM fills defaults — but apps with strict pydantic validation may want to suppress the warning.
- **Solana wallet path not wired** — Base only for v0.1. Solana support will land alongside `SolanaLLMClient` integration.
- Image / video / music generation endpoints are not exposed by the proxy yet; only `/v1/chat/completions`.
