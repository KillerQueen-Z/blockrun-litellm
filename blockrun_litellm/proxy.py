"""
Local OpenAI-compatible proxy for BlockRun.

Run as a sidecar; point any OpenAI client (LiteLLM, langchain, raw SDK,
curl) at ``http://localhost:4001/v1`` and it Just Works. Your x402 wallet
key lives in this process — clients on the same host never see it.

Endpoints
---------
- ``POST /v1/chat/completions``      — OpenAI Chat Completions
- ``POST /v1/messages``              — native Anthropic Messages (Claude Code,
  Anthropic SDK); verbatim x402-signed passthrough — tools/thinking preserved
- ``POST /v1/messages/count_tokens`` — Anthropic token counting (passthrough)
- ``POST /v1beta/models/{model}:generateContent`` — native Gemini JSON
- ``POST /v1beta/models/{model}:streamGenerateContent`` — native Gemini SSE
- ``POST /v1/images/generations``    — OpenAI Image Generations (DALL-E compatible)
- ``POST /v1/videos/generations``    — video generation (xai/grok-imagine-video,
  Seedance); async submit+poll, settles only on completion
- ``POST /v1/videos`` + ``GET /v1/videos/{id}`` + ``GET /v1/videos/{id}/content``
  — OpenAI Videos API (what LiteLLM's video routes call): create returns a
  job object at once; poll status; download bytes on completion
- ``POST /v1/audio/speech``          — OpenAI-compatible TTS (ElevenLabs voices)
- ``POST /v1/audio/generations``     — music generation (minimax/music-2.5+)
- ``POST /v1/audio/sound-effects``   — cinematic sound effects
- ``POST /v1/responses``             — OpenAI Responses API bridge (translated
  to Chat Completions)
- ``GET  /v1/models``                — passthrough to BlockRun's chat catalog
- ``GET  /healthz``                  — liveness probe (no upstream call)

Auth
----
The proxy itself is **unauthenticated by default** — bind to ``127.0.0.1``
in production. Optional shared-secret guard via ``BLOCKRUN_PROXY_TOKEN``;
clients must then send ``Authorization: Bearer <token>``.

CLI
---
::

    $ pip install 'blockrun-litellm[proxy]'
    $ export BLOCKRUN_WALLET_KEY=0x...
    $ blockrun-litellm-proxy --port 4001
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import math
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

try:
    from fastapi import Depends, FastAPI, Header, HTTPException, Request
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import JSONResponse, Response, StreamingResponse
except ImportError as exc:  # pragma: no cover - import-time guard
    raise ImportError(
        "blockrun-litellm[proxy] extras not installed. Run: pip install 'blockrun-litellm[proxy]'"
    ) from exc

import httpx
import json as _json
from pydantic import ValidationError

from blockrun_llm.types import APIError, PaymentError
from blockrun_llm.tx_log import decode_settlement_header

from blockrun_litellm import _adapter
from blockrun_litellm import logger as _logger

# Optional — present when blockrun-llm[solana] is installed alongside solana-py.
# SolanaRpcException wraps httpx / network errors from the Solana JSON-RPC layer
# (e.g. getAccountInfo failure during x402 payment signing). We handle it
# explicitly so it doesn't produce a noisy full-traceback in the log.
try:
    from solana.exceptions import SolanaRpcException as _SolanaRpcException  # type: ignore[import-untyped]
except ImportError:
    _SolanaRpcException = None  # type: ignore[assignment,misc]

log = logging.getLogger("blockrun_litellm.proxy")


def _positive_int_env(name: str, default: int) -> int:
    """Read a positive int from the env, falling back on anything unusable.

    These are read at import, so a bare ``int(os.environ[...])`` turns a typo in
    a deploy script into crash-on-startup with a bare ValueError. A size limit
    is not worth refusing to boot over: warn and use the default.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        log.warning("%s=%r is not a positive integer; using %d", name, raw, default)
        return default
    return value


# Limit concurrent in-flight requests to avoid saturating upstream rate limits.
# Anthropic (claude-opus-*) has strict TPM/RPM caps — raising this too high
# without also raising upstream quota will cause 429s.
# Override with env var BLOCKRUN_MAX_CONCURRENT (int, default 100).
# The httpx pool is configured for 200 connections; each paid request uses 2
# connections (402 probe + authenticated call), so 100 is the practical ceiling
# before the pool itself becomes the bottleneck.
_MAX_CONCURRENT: int = int(os.environ.get("BLOCKRUN_MAX_CONCURRENT", "100"))
_concurrency_sem: asyncio.Semaphore  # initialised in lifespan / lazily on first use


def _get_semaphore() -> asyncio.Semaphore:
    global _concurrency_sem
    try:
        return _concurrency_sem
    except NameError:
        _concurrency_sem = asyncio.Semaphore(_MAX_CONCURRENT)
        return _concurrency_sem


# Media routes (image/video/audio) get their own, smaller admission gate so a
# burst of slow video jobs (60-900s each, sync SDK on a bounded thread pool)
# can only queue up other media — never chat/messages traffic, which keeps
# using the global semaphore above. Override with BLOCKRUN_MEDIA_MAX_CONCURRENT.
_MEDIA_MAX_CONCURRENT: int = int(os.environ.get("BLOCKRUN_MEDIA_MAX_CONCURRENT", "20"))
_media_sem: asyncio.Semaphore


def _get_media_semaphore() -> asyncio.Semaphore:
    global _media_sem
    try:
        return _media_sem
    except NameError:
        _media_sem = asyncio.Semaphore(_MEDIA_MAX_CONCURRENT)
        return _media_sem


app = FastAPI(
    title="blockrun-litellm proxy",
    description="OpenAI-compatible front-end for BlockRun's x402 gateway.",
    version="0.2.0",
    docs_url="/docs",
)


# ---------------------------------------------------------------------------
# Optional shared-secret auth
# ---------------------------------------------------------------------------


def _expected_token() -> Optional[str]:
    return os.environ.get("BLOCKRUN_PROXY_TOKEN") or None


def _require_token(authorization: Optional[str] = Header(default=None)) -> None:
    expected = _expected_token()
    if expected is None:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    if authorization.removeprefix("Bearer ").strip() != expected:
        raise HTTPException(401, "Invalid Bearer token")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models", dependencies=[Depends(_require_token)])
async def list_models() -> Dict[str, Any]:
    # Reuse the cached async client; ``list_models`` does not require payment.
    client = _adapter.get_async_client()
    models = await client.list_models()
    return {
        "object": "list",
        "data": [
            {
                "id": m.get("id"),
                "object": "model",
                "owned_by": m.get("provider", "blockrun"),
            }
            for m in models
        ],
    }


def _is_solana_rpc_exc(exc: Exception) -> bool:
    """Return True when ``exc`` is a ``SolanaRpcException`` (optional dep)."""
    return _SolanaRpcException is not None and isinstance(exc, _SolanaRpcException)


def _solana_rpc_msg(exc: Exception) -> str:
    return str(getattr(exc, "error_msg", exc))


def _payment_error_payload(exc: PaymentError) -> Dict[str, Any]:
    """Render a :class:`PaymentError` into the JSON body for an HTTP 402.

    When the SDK preserves the gateway's original failure reason
    (``status_code`` + ``response.details``), surface it verbatim so
    customers see the real facilitator error (e.g.
    ``transaction_simulation_failed``) instead of just our SDK's
    generic message. Falls back to ``{"error": str(exc)}`` for older
    PaymentError instances that don't carry a response dict.
    """
    payload: Dict[str, Any] = {"error": str(exc)}
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        details = resp.get("details")
        if isinstance(details, str) and details:
            payload["details"] = details
    return payload


def _payment_error_sse_message(exc: PaymentError) -> str:
    """Stream-mode equivalent of :func:`_payment_error_payload` — folds the
    gateway ``details`` into the OpenAI-style ``message`` field so a streaming
    client still sees the real reason in the single error event."""
    msg = str(exc)
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        details = resp.get("details")
        if isinstance(details, str) and details and details not in msg:
            msg = f"{msg} (details: {details})"
    return msg


@app.post("/v1/chat/completions", dependencies=[Depends(_require_token)])
async def chat_completions(request: Request) -> Any:
    # Verbatim x402-signed passthrough to the gateway's native
    # /v1/chat/completions. Previously this went through the SDK's typed
    # chat_completion_stream, which crashes on streamed tool calls
    # ('dict' object has no attribute 'delta' — the strict ToolCall schema
    # rejects streaming argument-fragment frames, falls back to model_construct,
    # then the archive loop reads .delta on a dict). Raw passthrough keeps every
    # OpenAI client — including Codex with wire_api=chat — off that path, so
    # streamed tool_calls survive intact.
    return await _forward_passthrough(
        request, "/v1/chat/completions", _openai_fwd_headers(request), allow_stream=True
    )


# ---------------------------------------------------------------------------
# Native Anthropic /v1/messages passthrough (Claude Code, Anthropic SDK, ...)
# ---------------------------------------------------------------------------
# Claude Code (and anything built on the Anthropic SDK) speaks the Anthropic
# Messages protocol, NOT OpenAI. Rather than translate Anthropic<->OpenAI
# (lossy — silently drops tool_use / thinking / cache_control), we forward the
# request VERBATIM to BlockRun's native /v1/messages endpoint and add ONLY the
# x402 signature. tools / tool_choice / thinking / streaming pass through
# untouched, so agentic tool use works exactly as against api.anthropic.com.
#
# Both chains supported: Base signs via the SDK's EIP-712 httpx transport, Solana
# via _SolanaX402Transport (SVM). The chain is picked from the configured
# api-url (--api-url / BLOCKRUN_API_URL).

_messages_http_clients: Dict[str, httpx.Client] = {}


class _SolanaX402Transport(httpx.BaseTransport):
    """httpx transport that signs Solana (SVM) x402 payments on 402 — the Solana
    twin of blockrun_llm's Base/EIP-712 ``_BlockRunX402Transport``.

    Lets ANY path (``/v1/messages``, ``/v1/chat/completions``, ...) stream through
    ``sol.blockrun.ai`` by reusing the SDK's SVM signing helpers. The x402 client
    isn't thread-safe, so
    the brief signing step is lock-guarded (mirrors SolanaLLMClient._sign_payment).
    """

    def __init__(self, private_key: str, api_url: str, base_transport=None):
        # Lazy import: the x402 SVM stack only ships with blockrun-llm[solana].
        from x402 import x402ClientSync
        from blockrun_llm.solana_client import (
            _create_signer,
            _resolve_rpc_config,
            _register_svm_with_headers,
        )

        self._api_url = api_url.rstrip("/")
        self._base = base_transport or httpx.HTTPTransport()
        resolved_url, resolved_headers = _resolve_rpc_config(None, None)
        self._x402_client = x402ClientSync()
        _register_svm_with_headers(
            self._x402_client, _create_signer(private_key), resolved_url, resolved_headers
        )
        self._lock = threading.Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        from blockrun_llm.solana_client import SolanaLLMClient
        from x402.http.utils import (
            decode_payment_required_header,
            encode_payment_signature_header,
        )

        response = self._base.handle_request(request)
        if response.status_code != 402:
            return response

        response.read()
        payment_header = SolanaLLMClient._extract_payment_header(response)
        if not payment_header:
            return response

        payment_required = decode_payment_required_header(payment_header)
        with self._lock:
            payload = self._x402_client.create_payment_payload(payment_required)
        request.headers["PAYMENT-SIGNATURE"] = encode_payment_signature_header(payload)
        return self._base.handle_request(request)

    def close(self) -> None:
        self._base.close()


def _resolve_api_url() -> str:
    return (os.environ.get("BLOCKRUN_API_URL") or _adapter.BASE_API_URL).rstrip("/")


def _messages_client(api_url: str) -> httpx.Client:
    """Cached httpx client whose transport signs x402 for any path on the chain
    implied by ``api_url`` (Base via EIP-712, Solana via SVM)."""
    existing = _messages_http_clients.get(api_url)
    if existing is not None:
        return existing

    if _adapter._is_solana_url(api_url):
        from blockrun_llm.solana_wallet import load_solana_wallet

        key = os.environ.get("SOLANA_WALLET_KEY") or load_solana_wallet()
        if not key:
            raise HTTPException(500, "No Solana wallet configured (set SOLANA_WALLET_KEY)")
        transport: httpx.BaseTransport = _SolanaX402Transport(private_key=key, api_url=api_url)
    else:
        from eth_account import Account
        from blockrun_llm.anthropic_client import _BlockRunX402Transport
        from blockrun_llm.wallet import load_wallet

        key = os.environ.get("BLOCKRUN_WALLET_KEY") or load_wallet()
        if not key:
            raise HTTPException(500, "No Base wallet configured (set BLOCKRUN_WALLET_KEY)")
        if not key.startswith("0x"):
            key = "0x" + key
        transport = _BlockRunX402Transport(account=Account.from_key(key), api_url=api_url)

    # Same baseline as the SDK chat clients (BLOCKRUN_CHAT_TIMEOUT, default
    # 600s). This passthrough is the heavy path for agentic/Claude Code traffic
    # (/v1/messages), which is exactly where reasoning models (opus-4.8 extended
    # thinking, 200–300s+) would otherwise time out mid-generation at the old
    # hardcoded 300s while the gateway kept billing server-side.
    client = httpx.Client(
        transport=_SignedAmountTransport(transport), timeout=_adapter._CHAT_TIMEOUT
    )
    _messages_http_clients[api_url] = client
    return client


def _anthropic_fwd_headers(request: Request) -> Dict[str, str]:
    # Strip the client's proxy-gate auth (Authorization / x-api-key); the
    # transport supplies its own x402 PAYMENT-SIGNATURE. Mirror AnthropicClient,
    # which sets api_key="blockrun". Preserve the Anthropic protocol headers.
    headers = {
        "content-type": "application/json",
        "x-api-key": "blockrun",
        "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
    }
    beta = request.headers.get("anthropic-beta")
    if beta:
        headers["anthropic-beta"] = beta
    return headers


def _openai_fwd_headers(request: Request) -> Dict[str, str]:
    # Drop the client's proxy-gate auth; the x402 PAYMENT-SIGNATURE comes from
    # the transport, and the gateway treats x402 as the auth (no API key needed).
    return {"content-type": request.headers.get("content-type", "application/json")}


def _gemini_fwd_headers(request: Request) -> Dict[str, str]:
    """Headers for native Gemini requests.

    Google SDKs require an ``api_key`` even when pointed at a custom base URL
    and may send it as ``x-goog-api-key`` or ``?key=...``.  The sidecar uses
    the local x402 wallet instead, so neither credential is forwarded.  Keep
    only the content type; the BlockRun gateway supplies its own Google key.

    Same shape as ``_openai_fwd_headers`` today; delegate so a future change to
    the forwarded-header policy can't silently apply to one protocol and not
    the other.
    """
    return _openai_fwd_headers(request)


def _open_upstream_stream(
    client: httpx.Client, target: str, raw: bytes, headers: Dict[str, str]
) -> httpx.Response:
    """Send a streaming POST and return the response with its status known but
    body unread. The caller owns the response and MUST close it. Opening the
    stream up front (rather than inside the response generator) is what lets us
    surface the real upstream status instead of an unconditional ``200``."""
    req = client.build_request("POST", target, content=raw, headers=headers)
    return client.send(req, stream=True)


# Synthetic response header the sidecar's own transport wrapper stamps with
# the exact authorized charge (micro-USDC), decoded from the request's
# PAYMENT-SIGNATURE. Needed because the Base gateway's settlement header
# (x402 v2 ``PAYMENT-RESPONSE``) carries no amount field — without this the
# passthrough knew the tx hash but not the charge.
_SIGNED_AMOUNT_HEADER = "x-blockrun-signed-amount-micro"
# Carries "we served this, but ignored something you sent" — e.g. `quality` on a
# chain that has no such field. A dropped param the caller never hears about is
# the failure mode worth a header.
_WARNING_HEADER = "x-blockrun-warning"


def _decode_signed_amount_micro(header: Optional[str]) -> Optional[str]:
    """Extract the exact micro-USDC charge from an x402 PAYMENT-SIGNATURE
    payload. The SDK signs the 'exact' scheme only, so
    ``payload.authorization.value`` IS the charge (same source as the SDK's
    own ``cost_usd``: the gateway's 402 quote). Returns ``None`` on any
    missing/malformed input — cost surfacing is informational, never fatal."""
    if not header:
        return None
    try:
        data = _json.loads(base64.b64decode(header))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    payload = data.get("payload") or {}
    value = (payload.get("authorization") or {}).get("value")
    return str(value) if value is not None else None


class _SignedAmountTransport(httpx.BaseTransport):
    """Wraps an x402-signing transport and stamps the authorized charge onto
    the response as :data:`_SIGNED_AMOUNT_HEADER`.

    The SDK's signing transports mutate the request in place (they set
    ``PAYMENT-SIGNATURE`` on the original request before the paid retry), so
    after ``handle_request`` returns, the request tells us whether — and for
    exactly how much — this call paid. Free/cached calls never gain the
    signature, so they surface no cost, as before."""

    def __init__(self, inner: httpx.BaseTransport):
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._inner.handle_request(request)
        if response.status_code < 400 and _SIGNED_AMOUNT_HEADER not in response.headers:
            amount = _decode_signed_amount_micro(request.headers.get("PAYMENT-SIGNATURE"))
            if amount is not None:
                response.headers[_SIGNED_AMOUNT_HEADER] = amount
        return response

    def close(self) -> None:
        self._inner.close()


def _proxy_cost_from_headers(
    headers: Any,
) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
    """Decode the real on-chain charge from the gateway's settlement response
    header — ``PAYMENT-RESPONSE`` per the x402 v2 spec (what the Base gateway
    sends), with the legacy ``X-PAYMENT-RESPONSE`` name still accepted.

    The v2 settlement payload carries no amount, so the charge falls back to
    :data:`_SIGNED_AMOUNT_HEADER` (stamped by :class:`_SignedAmountTransport`
    from the exact-scheme authorization this sidecar itself signed). Headers
    are read per-response on the paid call — race-free, and available before
    the stream body is drained. Returns ``(None, None)`` for free / cached
    calls — graceful, like the older-SDK estimate fallback.
    """
    raw = headers.get("x-payment-response") or headers.get("payment-response")
    settlement = decode_settlement_header(raw)
    amount = settlement.get("amount_micro_usdc") if settlement else None
    if amount is None:
        amount = headers.get(_SIGNED_AMOUNT_HEADER)
    cost: Optional[float] = None
    if amount is not None:
        try:
            cost = float(amount) / 1e6
        except (TypeError, ValueError):
            cost = None
    return cost, settlement


def _cost_response_headers(
    cost: Optional[float], settlement: Optional[Dict[str, Any]]
) -> Dict[str, str]:
    """Client-visible headers carrying the real wallet charge for this call, so
    an agent / downstream proxy can track spend per request.

    ``x-litellm-response-cost`` is the header LiteLLM itself emits for proxy
    chaining; a LiteLLM proxy pointed at this sidecar reads it back off the
    upstream response (``additional_headers["llm_provider-x-litellm-response-
    cost"]`` in its cost calculator) and records the REAL x402 charge as the
    request's spend instead of a token×price-map estimate — the price map has
    no BlockRun-routed models, so without this header LiteLLM logs $0.
    """
    out: Dict[str, str] = {}
    if cost is not None:
        out["x-blockrun-cost-usd"] = format(cost, ".10g")
        out["x-litellm-response-cost"] = format(cost, ".10g")
    if settlement:
        out["x-blockrun-settlement"] = _json.dumps(settlement, default=str)
    return out


def _body_model(raw: bytes) -> Optional[str]:
    try:
        return _json.loads(raw or b"{}").get("model")
    except Exception:
        return None


async def _forward_passthrough(
    request: Request,
    path: str,
    headers: Dict[str, str],
    *,
    allow_stream: bool,
    stream_override: Optional[bool] = None,
    model_override: Optional[str] = None,
    forward_query: bool = True,
) -> Response:
    """Verbatim x402-signed passthrough to a BlockRun native endpoint.

    Shared by ``/v1/messages``, ``/v1/messages/count_tokens``,
    ``/v1/chat/completions`` and the native Gemini ``/v1beta/models/...`` route.
    The body is forwarded byte-for-byte — no translation, no SDK typed parsing —
    so OpenAI clients (incl. Codex with wire_api=chat) and Claude Code stay off
    the SDK's ``chat_completion_stream`` path, the source of the
    streamed-tool-call ``'dict' object has no attribute 'delta'`` crash. The
    upstream call is gated by ``_get_semaphore()`` like every other paid route.
    For streaming we open the upstream first to learn its real status, then
    either return a real error ``Response`` (preserving the 4xx/5xx the client
    must see) or stream the body.

    Keyword-only overrides let a caller adapt the shared path to a protocol whose
    conventions differ from the OpenAI body:

    * ``stream_override`` — when not ``None``, decides streaming directly instead
      of sniffing a ``"stream"`` field in the body (Gemini encodes streaming in
      the URL method, not the body). ``allow_stream`` is then irrelevant.
    * ``model_override`` — the model string used for the audit log, when it can't
      be read from the body (e.g. Gemini carries it in the URL).
    * ``forward_query`` — ``False`` drops the client query string before
      forwarding (Gemini strips ``?key=``/``?alt=sse``; the gateway re-derives
      both server-side).
    """
    api_url = _resolve_api_url()
    raw = await request.body()
    client = _messages_client(api_url)
    qs = request.url.query if forward_query else ""
    target = f"{api_url}{path}" + (f"?{qs}" if qs else "")

    _t0 = time.monotonic()
    model = model_override or _body_model(raw)
    req_id = request.headers.get("x-request-id")

    def _latency_ms() -> float:
        return (time.monotonic() - _t0) * 1000.0

    wants_stream = bool(stream_override)
    if allow_stream and stream_override is None:
        try:
            wants_stream = bool(_json.loads(raw or b"{}").get("stream"))
        except Exception:
            wants_stream = False

    if wants_stream:
        # Hold the semaphore across the paid upstream connection (x402 probe +
        # sign + handshake) — released before the body drain so a slow reader
        # reading at 1 tok/s doesn't keep a slot and block other callers.
        async with _get_semaphore():
            try:
                resp = await run_in_threadpool(_open_upstream_stream, client, target, raw, headers)
            except Exception as exc:  # noqa: BLE001
                # A Solana JSON-RPC fault during x402 signing (e.g. getAccountInfo
                # timeout) surfaces here, BEFORE any upstream status exists. Map it
                # to a clean 503 instead of a bare 500 — restoring the behaviour the
                # old typed chat path had, and giving the merged Anthropic path the
                # same treatment.
                if _is_solana_rpc_exc(exc):
                    log.warning("solana rpc error during payment signing: %s", _solana_rpc_msg(exc))
                    return JSONResponse(status_code=503, content={"error": _solana_rpc_msg(exc)})
                raise

        # The real x402 charge rides on the upstream response's PAYMENT-RESPONSE
        # header — readable now, before we drain the body.
        cost, settlement = _proxy_cost_from_headers(resp.headers)

        if resp.status_code >= 400:
            # A real upstream error — surface the true status + body, NOT a 200
            # text/event-stream the Anthropic SDK would mis-parse or hang on.
            body = await run_in_threadpool(resp.read)
            await run_in_threadpool(resp.close)
            _logger.log_proxy_call(
                model=model,
                path=path,
                stream=True,
                http_status=resp.status_code,
                cost_usd=cost,
                settlement=settlement,
                latency_ms=_latency_ms(),
                request_id=req_id,
                # Reading the header only helps when there IS one. Solana chat
                # settles in parallel and, on failure, logs "CHARGED BUT REQUEST
                # FAILED — refund manually" and throws: the error response
                # carries no settlement header at all. Without this the
                # highest-volume paid route records a real charge as $0.
                settlement_status=_settlement_status(resp.status_code, settlement),
            )
            return Response(
                content=body,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/json"),
            )

        _logger.log_proxy_call(
            model=model,
            path=path,
            stream=True,
            http_status=resp.status_code,
            cost_usd=cost,
            settlement=settlement,
            latency_ms=_latency_ms(),
            request_id=req_id,
            # A no-op on this arm (the branch above took every >=400), but every
            # log site computes it — the one that didn't is how /v1/videos drifted.
            settlement_status=_settlement_status(resp.status_code, settlement),
        )

        def _drain():
            try:
                for chunk in resp.iter_raw():
                    yield chunk
            finally:
                resp.close()

        stream_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        stream_headers.update(_cost_response_headers(cost, settlement))
        return StreamingResponse(
            _drain(),
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "text/event-stream"),
            headers=stream_headers,
        )

    def _post():
        resp = client.post(target, content=raw, headers=headers)
        cost, settlement = _proxy_cost_from_headers(resp.headers)
        return (
            resp.status_code,
            resp.headers.get("content-type", "application/json"),
            resp.content,
            cost,
            settlement,
        )

    async with _get_semaphore():
        try:
            status, ctype, content, cost, settlement = await run_in_threadpool(_post)
        except Exception as exc:  # noqa: BLE001
            if _is_solana_rpc_exc(exc):
                log.warning("solana rpc error during payment signing: %s", _solana_rpc_msg(exc))
                return JSONResponse(status_code=503, content={"error": _solana_rpc_msg(exc)})
            raise
    _logger.log_proxy_call(
        model=model,
        path=path,
        stream=False,
        http_status=status,
        cost_usd=cost,
        settlement=settlement,
        latency_ms=_latency_ms(),
        request_id=req_id,
        # Same reason as the streaming arm above: on Solana a failed paid call
        # can be charged and answer without a settlement header, so a bare
        # cost_usd=None here would read as free.
        settlement_status=_settlement_status(status, settlement),
    )
    return Response(
        content=content,
        status_code=status,
        media_type=ctype,
        headers=_cost_response_headers(cost, settlement),
    )


# ---------------------------------------------------------------------------
# Native Gemini /v1beta passthrough (Google GenAI SDK, raw REST clients, ...)
# ---------------------------------------------------------------------------
# Gemini selects streaming in the URL method, not with a JSON ``stream`` field.
# The request and response bodies stay byte-for-byte native; only x402 payment
# signing is added by the shared transport.  A Google SDK's dummy ``key`` query
# and x-goog-api-key header are deliberately dropped before the gateway hop.

_GEMINI_METHODS = {"generateContent", "streamGenerateContent"}

# A native Gemini model id, once the id-only ``models/``/``google/`` prefixes are
# stripped: letters, digits, dot, dash, underscore. Deliberately no slash and no
# dot-segment — those are how a crafted model name (``../../v1/chat/completions``)
# escapes the ``/v1beta/models/`` prefix after httpx normalizes the URL, so we
# reject them locally before the wallet ever signs a payment.
_GEMINI_MODEL_RE = re.compile(r"[A-Za-z0-9._-]+")


def _gemini_error(status: int, message: str, code: str = "INVALID_ARGUMENT") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": status, "message": message, "status": code}},
    )


def _log_gemini_reject(path: str, req_id: Optional[str]) -> None:
    """Log a local, pre-payment 400 the way the media routes do.

    The 0.7.6 invariant is that every exit logs. This reject never reaches the
    gateway, so it's genuinely free (no ``settlement_status`` flag), but it still
    gets a row — a call that leaves no trace is invisible to reconciliation.
    """
    _logger.log_proxy_call(
        model=None,
        path=path,
        stream=False,
        http_status=400,
        cost_usd=None,
        settlement=None,
        latency_ms=0.0,
        request_id=req_id,
    )


@app.post("/v1beta/models/{model_and_method:path}", dependencies=[Depends(_require_token)])
async def gemini_native(request: Request, model_and_method: str) -> Any:
    req_id = request.headers.get("x-request-id")
    log_path = f"/v1beta/models/{model_and_method}"
    raw_model, separator, method = model_and_method.rpartition(":")
    if not separator or method not in _GEMINI_METHODS:
        _log_gemini_reject(log_path, req_id)
        return _gemini_error(
            400,
            "This endpoint only supports generateContent and streamGenerateContent.",
        )

    # Strip the id-only prefixes the gateway also accepts, then require a clean
    # model token. The forwarded path is rebuilt from the sanitized parts — never
    # the raw segment — so no slash or ``..`` in the model can move the signed
    # upstream target outside ``/v1beta/models/``.
    model = raw_model.strip().removeprefix("models/").removeprefix("google/")
    if not _GEMINI_MODEL_RE.fullmatch(model):
        _log_gemini_reject(log_path, req_id)
        return _gemini_error(400, f"Invalid Gemini model id: {raw_model.strip()!r}.")

    return await _forward_passthrough(
        request,
        f"/v1beta/models/{model}:{method}",
        _gemini_fwd_headers(request),
        allow_stream=True,
        stream_override=method == "streamGenerateContent",
        model_override=f"google/{model}",
        # Google SDKs append ?key=... and ?alt=sse.  The BlockRun gateway derives
        # streaming from the URL method and re-adds ?alt=sse itself on the real
        # Google call, so it ignores the client query entirely; dropping it here
        # loses nothing and keeps a stray real Google key out of gateway logs.
        forward_query=False,
    )


@app.post("/v1/messages", dependencies=[Depends(_require_token)])
async def anthropic_messages(request: Request) -> Any:
    return await _forward_passthrough(
        request, "/v1/messages", _anthropic_fwd_headers(request), allow_stream=True
    )


@app.post("/v1/messages/count_tokens", dependencies=[Depends(_require_token)])
async def anthropic_count_tokens(request: Request) -> Any:
    # Claude Code calls this to size requests; count_tokens is never streamed.
    return await _forward_passthrough(
        request, "/v1/messages/count_tokens", _anthropic_fwd_headers(request), allow_stream=False
    )


# ---------------------------------------------------------------------------
# Media endpoints (image / video / speech / music / sound-effects)
# ---------------------------------------------------------------------------


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        return await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")


def _media_settlement(result: Any) -> Optional[Dict[str, Any]]:
    """Media SDK responses carry the x402 tx hash in-body (``txHash``) rather
    than via the PAYMENT-RESPONSE header; normalise to the settlement shape
    :func:`decode_settlement_header` produces so audit rows stay uniform."""
    if isinstance(result, dict) and result.get("txHash"):
        return {"tx_hash": result["txHash"]}
    return None


def _settlement_status(
    http_status: int,
    settlement: Optional[Dict[str, Any]],
    *,
    parse_failed_after_settlement: bool = False,
    reached_gateway: bool = True,
    is_solana: Optional[bool] = None,
) -> Optional[str]:
    """Was this failure free, or might it have cost money? Returns None or "unknown".

    ``cost_usd=None`` cannot answer that on its own — it reads as "$0" and is
    also what we log when we simply don't know. A ledger that quietly under-
    reports spend is worse than one that admits a gap, so say which it is.

    Whether a failed paid call settles is the gateway's decision, and the two
    gateways are opposites:

    * **Base media routes** settle only after a successful upstream call
      (verified: images/generations:333, audio/generations:423, audio/speech:292,
      image2image:389), so a failure settles nothing. Same for non-streaming
      chat. Base *streaming* chat is the exception — it settles inside
      ``metadata.then(...)``, which resolves even when the stream loop's catch
      fires, so a stream that opens and then dies IS charged. That can't reach
      this function: a mid-stream death never produces a >=400 row (the 200 was
      logged when the headers arrived), and a >=400 at header time means the
      stream never ran. Noted so the "Base failures are free" line isn't later
      read as license it doesn't grant.
    * **Solana** settles *optimistically* on chat, search, both image routes and
      music: settle fires in parallel with the upstream work, so a call that
      verifies and then fails **is charged** — and the error response carries no
      settlement header, so "no proof" and "you were charged" co-occur exactly
      when it matters.

    **Any** Solana failure is flagged, not just 5xx. An earlier cut gated on
    ``>= 500``, reasoning that a 4xx is the gateway refusing before settle. That
    is false and it was the most expensive kind of wrong: the settle fires
    first, so a content-filter rejection comes back **400** (blockrun-sol
    images/generations:376) and a rate limit **429** (:359) — both charged, both
    routine, both client-triggerable. 402 is not exempt either: Solana settles at
    POST, and the poll route returns 402 on wallet-binding failure *after* the
    money moved (images/generations/[id]:74 "the POST already settled").

    On Base, a 504 is flagged too: it's our own await ceiling (``_run_media``),
    which abandons the wait but leaves the worker running — so the call can
    complete and settle after we've answered.

    ``parse_failed_after_settlement`` covers what bites on *both* chains: the
    gateway settled, returned 200, and the body wouldn't parse.

    ``reached_gateway=False`` is the one exemption from the Solana rule, and it
    is a proof rather than a guess: the SDK's own request validation raises
    before anything goes on the wire, so there is no payment to wonder about.
    Everything else that failed left this process, and on Solana that is enough
    to be unsure.

    ``is_solana`` should be the chain the call actually ran on, snapshotted when
    it started. Resolving it here would re-read ``BLOCKRUN_API_URL`` — a mutable
    global — minutes later on a long media call, and if it changed in flight a
    Solana charge would be classified as Base and written off as free. Defaults
    to resolving now for callers whose request is short enough that it cannot
    drift.

    Deliberately one bit of chain rather than a per-route table of which Solana
    endpoints settle optimistically — that table lives in the gateway and would
    drift here. The asymmetry decides it: a false "unknown" costs a glance at the
    ledger; a false "$0" loses a real charge. Solana's video route settles on the
    completed poll and will be flagged for nothing. That is the trade, on purpose.
    """
    if settlement and settlement.get("tx_hash"):
        # Proven settled: the row already carries the tx hash and the real cost,
        # so there is nothing to go looking for. Dead weight when called from
        # _media_endpoint (``result`` is None on every error arm there), but live
        # for the passthrough, which reads the settlement header off the response
        # and so CAN see a settled error.
        return None
    if http_status < 400:
        return None  # succeeded — nothing failed, nothing to reconcile
    if parse_failed_after_settlement:
        return "unknown"  # settle already ran; only the parse failed
    if not reached_gateway:
        return None  # SDK refused it locally — nothing was ever sent to pay for
    if _adapter._is_solana_url(None) if is_solana is None else is_solana:
        return "unknown"  # optimistic settle: ANY failure here may be charged
    if http_status == 504:
        return "unknown"  # our await ceiling; the worker may still settle
    return None


async def _media_endpoint(
    path: str, model: Optional[str], call: Any, *, warning: Optional[str] = None
) -> Any:
    """Shared admission gate + error mapping + audit logging for media routes.

    ``warning`` rides out on ``x-blockrun-warning`` when the request was served
    but something the caller sent was ignored — a param the configured chain
    doesn't have. The call still succeeds; the header is what keeps "ignored"
    from meaning "silent".

    ``call`` is a zero-arg coroutine factory invoking the adapter. Maps
    ValueError → 400 (SDK request validation, e.g. lyrics + instrumental),
    PaymentError → 402 with gateway details, APIError → its status (502 when
    out of range). Every settled outcome is logged via log_proxy_call — media
    calls are the priciest per-request traffic, so they must show up in spend
    reconciliation. The SDK doesn't expose the wallet charge for media yet
    (cost_usd=None); the settlement tx hash from the response body is logged
    and surfaced via the x-blockrun-settlement header.

    **Every exit logs.** That is the invariant, and it is load-bearing: a media
    call that moves money and leaves no audit row is invisible to reconciliation,
    which is worse than one recorded with an unknown cost. So no arm here
    ``raise``s past ``log_proxy_call`` — each sets ``status``/``payload`` and
    falls through — and a trailing ``except Exception`` catches what the SDK
    lets escape unwrapped (transport timeouts on long media calls).

    ``ValidationError`` and ``JSONDecodeError`` are caught FIRST and
    deliberately: both subclass ``ValueError``, and both mean the gateway
    answered 200 (settle already ran) and the SDK couldn't read the body. Left to
    the ValueError arm they'd answer 400 — blaming the caller for a call they
    paid for. It is upstream's fault: 502, flagged, and logged.
    """
    _t0 = time.monotonic()
    req_id = uuid.uuid4().hex
    result: Optional[Dict[str, Any]] = None
    parse_failed_after_settlement = False
    reached_gateway = True
    # Snapshot the chain NOW, not at log time: BLOCKRUN_API_URL is a mutable
    # global and these calls run for minutes. If it flipped mid-flight we would
    # classify a Solana charge as Base and write it off as free.
    is_solana = _adapter._is_solana_url(None)
    async with _get_media_semaphore():
        try:
            result = await call()
            status, payload = 200, result
        except (ValidationError, _json.JSONDecodeError) as exc:
            # Both mean the gateway answered 200 and the SDK couldn't read it —
            # settle already ran. JSONDecodeError has to be caught alongside
            # ValidationError and *before* ValueError: both subclass it, and the
            # SDK parses the paid 200 with a bare ``.json()``, so a truncated
            # body would otherwise land in the ValueError arm below — 400,
            # blaming the caller for a call they paid for.
            parse_failed_after_settlement = True
            status = 502
            payload = {
                "error": "Upstream returned a response the SDK could not parse. "
                "The call may already have settled — check the audit log.",
                "detail": str(exc)[:500],
            }
        except ValueError as exc:
            # SDK request validation (lyrics + instrumental, bad enum). Raised
            # before anything goes on the wire, so nothing could have been paid —
            # the one failure we can call free on Solana without guessing.
            # Set status/payload rather than raise: every exit from here must
            # reach log_proxy_call below, and `raise` skips it.
            reached_gateway = False
            status, payload = 400, {"detail": str(exc)}
        except PaymentError as exc:
            status, payload = 402, _payment_error_payload(exc)
        except APIError as exc:
            status = exc.status_code if 400 <= getattr(exc, "status_code", 0) < 600 else 502
            payload = {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - a missing row is worse than a broad catch
            # Transport errors (httpx.ReadTimeout on a 10-minute image call,
            # connection resets) escape the SDK unwrapped. If one lands after the
            # payment header went out, money may have moved — and without this
            # arm the request left no audit row at all, which is strictly worse
            # than an unflagged one. "We don't know" is exactly the answer.
            parse_failed_after_settlement = True
            status = 502
            payload = {
                "error": f"Media call failed in transport: {type(exc).__name__}. "
                "The call may already have settled — check the audit log.",
                "detail": str(exc)[:500],
            }
    settlement = _media_settlement(result)
    _logger.log_proxy_call(
        model=model,
        path=path,
        stream=False,
        http_status=status,
        cost_usd=None,
        settlement=settlement,
        latency_ms=(time.monotonic() - _t0) * 1000.0,
        request_id=req_id,
        settlement_status=_settlement_status(
            status,
            settlement,
            parse_failed_after_settlement=parse_failed_after_settlement,
            reached_gateway=reached_gateway,
            is_solana=is_solana,
        ),
    )
    headers = _cost_response_headers(None, settlement)
    if warning:
        headers[_WARNING_HEADER] = warning
    return JSONResponse(status_code=status, content=payload, headers=headers)


def _optional_str(value: Any) -> Optional[str]:
    """Normalize an optional string field to a value or absence.

    A blank string means "not set", not "set to empty" — multipart clients
    routinely emit every optional field, blank ones included, and a JSON caller
    templating `""` means the same thing. The Solana gateway's own multipart
    handler strips blanks the same way.

    Applies to every optional string on these routes, not just one: 0.7.0 used
    it for `quality` alone, so a blank `size`/`model` still travelled as `""` —
    which Base turned into a *billed* default image and Solana turned into a
    400. Same input, different chain, neither what the caller meant.
    """
    if isinstance(value, str) and value.strip():
        return value
    return None


def _require_optional_str(value: Any, field: str) -> Optional[str]:
    """Blank/absent → None; a real string → itself; anything else → 400.

    The distinction matters for money. Silently coercing a wrong-typed value to
    None (0.7.0's `x if isinstance(x, str) else None`) lets the SDK substitute
    its default and **bill for it**: `{"size": 512}` meaning 512x512 quietly
    rendered a 1024x1024 image at the default model. Refusing is free; guessing
    costs the caller a generation they didn't ask for.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return _optional_str(value)
    raise HTTPException(400, f"`{field}` must be a string")


def _require_named_model(value: Any) -> Optional[str]:
    """Absent → None (the gateway default applies); present-but-blank → 400.

    `_require_optional_str` folds a blank string into "not set", which is right
    for a field the caller may legitimately omit. `model` on a JSON body is the
    exception, because "not set" is not free here: the SDK coalesces with
    `model or DEFAULT_MODEL`, so a blank string is falsy, silently becomes the
    default model, and **bills for it** — ~$0.40 for the 8s Grok video default.

    Every JSON route that names a billed model uses this. The multipart
    `/v1/images/edits` branch deliberately does not: a form emits every field it
    knows about, so a blank there really does mean "unset" (see the comment at
    that call site). Transport decides what `""` means; JSON has no such excuse.

    The gateway can't catch this on our behalf: its schema is
    `z.string().default(...)`, and a zod default only fires when the key is
    *absent*. `""` is a present, valid string, so it sails through — and by then
    the SDK has already substituted the default anyway.

    Omitting `model` still opts into the default; that stays documented and
    free. Sending an empty one is a caller bug — it's what a client templating an
    unset variable emits — so it now says so instead of quietly charging for a
    model nobody named.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(400, "`model` must be a string")
    if not value.strip():
        raise HTTPException(
            400,
            "`model` must not be empty — omit the field entirely to use the "
            "gateway's default model, which is a billed generation either way",
        )
    return value


def _require_image_n(value: Any) -> int:
    """Bound `n` locally so a doomed request never reaches the payment dance.

    Base's `image2image` gateway schema is `z.number().optional().default(1)` —
    no int, no bounds — so `n=1000` passes validation, earns a 402, gets
    payment-verified, and only then 400s at the provider. Solana already bounds
    it (`.int().min(1).max(10)`); 10 is the ceiling the Base *text-to-image*
    schema uses.

    Whether that costs money depends on the chain, and the two are opposites:

    * **Base** settles only on success — `settlePaymentWithRetry` runs after the
      upstream call — so a request that dies at the provider settles nothing.
    * **Solana** settles **optimistically**: settle fires in parallel with the
      upstream work so it lands inside the ~60-90s blockhash window. A payload
      that passes verification and then fails upstream **is charged** — the
      gateway logs it as a paid error with a real tx hash.

    So on Solana this bound is the difference between a local 400 and a real
    debit for an image that never existed. On Base it saves a pointless signed
    round-trip. Worth having either way; only the stakes differ.

    (This docstring has been wrong twice: first claiming the loss was universal,
    then that it never happened. The gateways genuinely differ — don't collapse
    them into one sentence again.)
    """
    if isinstance(value, str) and not value.strip():
        return 1  # blank multipart field means "unset"
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "`n` must be an integer")
    if not 1 <= n <= 10:
        raise HTTPException(400, "`n` must be between 1 and 10")
    return n


def _image_quality(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """Resolve `quality` for the configured chain. Returns (value, warning).

    `quality` only exists on the Solana gateway. On Base it is dropped and the
    caller is told via ``x-blockrun-warning`` — see the rationale on
    :data:`_adapter._QUALITY_UNSUPPORTED_ON_BASE` for why this is a warning and
    not the 400 that 0.7.0 shipped.

    Chain is read with the adapter's own ``_is_solana_url(None)``, the identical
    rule ``get_image_client`` routes on (it falls back to BLOCKRUN_API_URL), so
    the header can't disagree with which client actually ran.
    """
    quality = _require_optional_str(value, "quality")
    if quality is not None and not _adapter._is_solana_url(None):
        return None, _adapter._QUALITY_UNSUPPORTED_ON_BASE
    return quality, None


@app.post("/v1/images/generations", dependencies=[Depends(_require_token)])
async def image_generations(request: Request) -> Any:
    body = await _json_body(request)

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(400, "`prompt` is required and must be text")

    n = _require_image_n(body.get("n", 1))
    model = _require_named_model(body.get("model"))
    size = _require_optional_str(body.get("size"), "size")
    quality, warning = _image_quality(body.get("quality"))
    return await _media_endpoint(
        "/v1/images/generations",
        model,
        lambda: _adapter.image_generation_async(
            prompt=prompt,
            model=model,
            size=size,
            n=n,
            quality=quality,
        ),
        warning=warning,
    )


def _require_image_payload(value: Any, field: str) -> None:
    """Reject non-string image payloads before they reach the SDK.

    The multipart path already produces data URIs via ``_image_form_value``; the
    JSON path forwarded whatever was sent, so ``{"image": 12345}`` travelled to
    the gateway to be rejected there — a wasted round-trip on a request we can
    refuse locally. ``model``/``size``/``quality`` are guarded the same way.
    """
    values = value if isinstance(value, list) else [value]
    if not values:
        raise HTTPException(400, f"`{field}` is required")
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise HTTPException(400, f"`{field}` must be a data URI string, or a list of them")


# Starlette spools uploads over ~1MB to disk precisely so a large body isn't
# held in RAM; converting to a data URI reads it back and base64 inflates it
# ~1.33x, which undoes that. Cap each upload so a mistaken 2GB POST fails fast
# instead of being buffered and encoded before the gateway rejects it.
_MAX_IMAGE_BYTES: int = _positive_int_env("BLOCKRUN_MAX_IMAGE_BYTES", 12 * 1024 * 1024)
# A resource guard, not a contract rule: per-model limits (openai/* up to 4,
# google/* up to 3) stay the gateway's call so this can't drift as the catalog
# changes. This only bounds how much we buffer before it gets to say so.
#
# 4 = the most any model accepts today. The old 16 meant parts 5-16 were read,
# base64-inflated ~1.33x, and uploaded TWICE (the unpaid 402 probe, then the
# signed retry) purely to earn a 400 — up to ~256MB of traffic for a request
# that could never succeed. Raise it if the catalog ever allows more.
_MAX_IMAGE_PARTS: int = _positive_int_env("BLOCKRUN_MAX_IMAGE_PARTS", 4)


async def _image_form_value(value: Any, field: str) -> str:
    """Convert a multipart image upload (or an existing data URI) to a data URI."""
    if isinstance(value, str):
        return value
    read = getattr(value, "read", None)
    if read is None:
        raise HTTPException(400, f"`{field}` must be an image upload or data URI")
    content_type = getattr(value, "content_type", None) or "application/octet-stream"
    if not content_type.startswith("image/"):
        raise HTTPException(400, f"`{field}` must have an image content type")
    # Check the spooled size before read() pulls the whole thing into memory.
    size = getattr(value, "size", None)
    if isinstance(size, int) and size > _MAX_IMAGE_BYTES:
        raise HTTPException(413, f"`{field}` exceeds the {_MAX_IMAGE_BYTES} byte upload limit")
    raw = await read()
    if len(raw) > _MAX_IMAGE_BYTES:
        raise HTTPException(413, f"`{field}` exceeds the {_MAX_IMAGE_BYTES} byte upload limit")
    return f"data:{content_type};base64,{base64.b64encode(raw).decode('ascii')}"


@app.post("/v1/images/edits", dependencies=[Depends(_require_token)])
@app.post("/v1/images/image2image", dependencies=[Depends(_require_token)])
async def image_edits(request: Request) -> Any:
    """OpenAI-compatible image editing, accepting JSON or multipart image[]."""
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        try:
            form = await request.form()
        except AssertionError as exc:
            # Starlette asserts python-multipart is importable. That's a server
            # install problem (it's in the [proxy] extra) — 400 would blame the
            # caller for our missing dependency.
            raise HTTPException(
                500,
                "Multipart support requires python-multipart. Install with: "
                "pip install 'blockrun-litellm[proxy]'",
            ) from exc
        except Exception as exc:
            raise HTTPException(400, f"Invalid multipart form: {exc}") from exc
        prompt = form.get("prompt")
        # multi_items() preserves wire order across BOTH field names; getlist()
        # per name would group all `image` before all `image[]` and silently
        # reorder a mixed client's inputs. Order is load-bearing for fusion —
        # prompts say "the logo from image 2 on image 1".
        values = [v for k, v in form.multi_items() if k in ("image", "image[]")]
        if not values:
            raise HTTPException(400, "`image` is required")
        if len(values) > _MAX_IMAGE_PARTS:
            raise HTTPException(
                400, f"at most {_MAX_IMAGE_PARTS} image parts (got {len(values)})"
            )
        images = [await _image_form_value(value, "image") for value in values]
        image: Any = images[0] if len(images) == 1 else images
        mask_value = form.get("mask")
        # A blank `mask=` field means "no mask", not "an empty mask" — same
        # reasoning as _optional_str, which the rest of these fields use.
        if isinstance(mask_value, str) and not mask_value.strip():
            mask_value = None
        mask = await _image_form_value(mask_value, "mask") if mask_value is not None else None
        # Blank `model` stays "not set" HERE, unlike every JSON path, which
        # refuses it (see _require_named_model). The transport changes what an
        # empty string means: a form emits every field it knows about, so a blank
        # one is how "unset" is spelled on the wire — the same reason `mask` is
        # nulled just above, and what the Solana gateway's own multipart handler
        # does. A blank in a JSON body has no such excuse; it's a caller
        # templating a variable that wasn't set.
        model = _require_optional_str(form.get("model"), "model")
        size = _require_optional_str(form.get("size"), "size")
        quality_raw = form.get("quality")
        n_raw = form.get("n", 1)
    else:
        if "application/x-www-form-urlencoded" in content_type:
            # Only multipart and JSON are branched on; without this a urlencoded
            # POST falls through to _json_body and is told its JSON is invalid,
            # which points at the wrong thing entirely.
            raise HTTPException(
                400,
                "Send this route as application/json or multipart/form-data; "
                "application/x-www-form-urlencoded is not supported (it cannot "
                "carry image uploads).",
            )
        body = await _json_body(request)
        prompt = body.get("prompt")
        image = body.get("image")
        if image is None:
            raise HTTPException(400, "`image` is required")
        mask = body.get("mask")
        model = _require_named_model(body.get("model"))
        size = _require_optional_str(body.get("size"), "size")
        quality_raw = body.get("quality")
        n_raw = body.get("n", 1)

    # Must be a *string*, not merely truthy. A `prompt` sent as a multipart file
    # part arrives as an UploadFile, which is truthy — str() would then bill
    # "UploadFile(filename='p.txt', ...)" as the generation prompt.
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(400, "`prompt` is required and must be text")
    _require_image_payload(image, "image")
    if mask is not None:
        _require_image_payload(mask, "mask")
    n = _require_image_n(n_raw)
    quality, warning = _image_quality(quality_raw)

    return await _media_endpoint(
        request.url.path,
        model,
        lambda: _adapter.image_edit_async(
            prompt=prompt,
            image=image,
            model=model,
            mask=mask,
            size=size,
            n=n,
            quality=quality,
        ),
        warning=warning,
    )


@app.post("/v1/videos/generations", dependencies=[Depends(_require_token)])
async def video_generations(request: Request) -> Any:
    """Generate a video (gateway default model, e.g. xai/grok-imagine-video).
    Submits an async job and blocks until the clip is ready (typ. 60-180s); the
    SDK polls and only settles on completion. Optional params: see
    ``_adapter.VIDEO_PARAM_KEYS``; ``budget_seconds``/``timeout`` are clamped
    server-side."""
    body = await _json_body(request)

    prompt = body.get("prompt")
    if not prompt:
        raise HTTPException(400, "`prompt` is required")

    params = {k: body[k] for k in _adapter.VIDEO_PARAM_KEYS if body.get(k) is not None}
    model = _require_named_model(body.get("model"))
    return await _media_endpoint(
        "/v1/videos/generations",
        model,
        lambda: _adapter.video_generation_async(prompt=prompt, model=model, **params),
    )


# ---------------------------------------------------------------------------
# OpenAI-compatible Videos API (/v1/videos) — what LiteLLM's video routes call
# ---------------------------------------------------------------------------
# LiteLLM's video generation (litellm.video_generation / proxy /v1/videos)
# speaks the OpenAI Videos spec against an ``openai/``-provider api_base:
#
#   POST {api_base}/videos              -> video JOB object (returns at once)
#   GET  {api_base}/videos/{id}         -> poll status
#   GET  {api_base}/videos/{id}/content -> download bytes
#
# The sidecar's native ``/v1/videos/generations`` blocks until the clip is
# ready and is invisible to LiteLLM — which is why ``xai/grok-imagine-video``
# "couldn't be called" through a LiteLLM proxy. These routes bridge the gap:
# create spawns the blocking SDK submit+poll as a background task and returns
# a job id immediately; status reads the job; content streams the finished
# CDN URL. Jobs live in-memory (the sidecar is a single-process uvicorn), so
# poll the same sidecar instance that took the create.

_VIDEO_JOB_TTL_S: float = float(os.environ.get("BLOCKRUN_VIDEO_JOB_TTL", "86400"))
_video_jobs: Dict[str, Dict[str, Any]] = {}

# Short side of the requested WxH -> the gateway's resolution tier.
_RESOLUTION_LADDER = ((2160, "4K"), (1080, "1080p"), (720, "720p"), (480, "480p"))
_SUPPORTED_ASPECT_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "9:21"}


def _map_openai_video_size(size: str) -> Dict[str, Any]:
    """OpenAI ``size`` ("720x1280") -> gateway ``resolution`` (+ ``aspect_ratio``
    when the WxH reduces to a ratio the gateway accepts; otherwise omitted —
    Grok ignores both fields, Seedance validates them)."""
    try:
        width, height = (int(p) for p in size.lower().split("x"))
        if width <= 0 or height <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        raise HTTPException(400, "`size` must look like 720x1280")
    short = min(width, height)
    resolution = "360p"
    for floor, tier in _RESOLUTION_LADDER:
        if short >= floor:
            resolution = tier
            break
    out: Dict[str, Any] = {"resolution": resolution}
    divisor = math.gcd(width, height)
    ratio = f"{width // divisor}:{height // divisor}"
    if ratio in _SUPPORTED_ASPECT_RATIOS:
        out["aspect_ratio"] = ratio
    return out


def _openai_video_kwargs(body: Dict[str, Any]) -> Dict[str, Any]:
    """Translate an OpenAI Videos create body into ``video_generation_async``
    kwargs. OpenAI params (``seconds``/``size``) are mapped; BlockRun-native
    params (``image_url`` etc., see VIDEO_PARAM_KEYS) pass through for direct
    callers and win over the mapped values."""
    kwargs: Dict[str, Any] = {}
    seconds = body.get("seconds")
    if seconds is not None:
        try:
            kwargs["duration_seconds"] = int(float(seconds))
        except (TypeError, ValueError):
            raise HTTPException(400, "`seconds` must be numeric (e.g. \"8\")")
    if body.get("size") is not None:
        kwargs.update(_map_openai_video_size(body["size"]))
    kwargs.update({k: body[k] for k in _adapter.VIDEO_PARAM_KEYS if body.get(k) is not None})
    return kwargs


def _prune_video_jobs() -> None:
    now = time.time()
    for job_id in [
        jid for jid, j in _video_jobs.items() if now - j["created_at"] > _VIDEO_JOB_TTL_S
    ]:
        _video_jobs.pop(job_id, None)


def _video_object(job: Dict[str, Any]) -> Dict[str, Any]:
    """Render a job as the OpenAI video object LiteLLM's ``VideoObject`` parses
    (required: id / object / status; ``seconds`` feeds its cost usage)."""
    out: Dict[str, Any] = {
        "id": job["id"],
        "object": "video",
        "status": job["status"],
        "created_at": int(job["created_at"]),
        "model": job["model"],
    }
    if job.get("seconds") is not None:
        out["seconds"] = str(job["seconds"])
    if job.get("size") is not None:
        out["size"] = job["size"]
    if job.get("completed_at") is not None:
        out["completed_at"] = int(job["completed_at"])
    if job.get("error") is not None:
        out["error"] = job["error"]
    if job["status"] == "completed":
        out["progress"] = 100
    return out


def _video_job_settlement(job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return _media_settlement(job.get("result"))


async def _run_video_job(job: Dict[str, Any], prompt: str, kwargs: Dict[str, Any]) -> None:
    """Drive the blocking SDK submit+poll for one video job and record the
    outcome on the job dict. Errors are folded into the OpenAI ``error`` shape
    so a poller sees status=failed instead of a hung queue."""
    _t0 = time.monotonic()
    status = 200
    result: Optional[Dict[str, Any]] = None
    parse_failed_after_settlement = False
    reached_gateway = True
    # Snapshot the chain NOW, not at log time: BLOCKRUN_API_URL is a mutable
    # global and these calls run for minutes. If it flipped mid-flight we would
    # classify a Solana charge as Base and write it off as free.
    is_solana = _adapter._is_solana_url(None)
    async with _get_media_semaphore():
        job["status"] = "in_progress"
        try:
            result = await _adapter.video_generation_async(
                prompt=prompt, model=job["model"], **kwargs
            )
            job["result"] = result
            job["status"] = "completed"
            job["completed_at"] = time.time()
            clips = result.get("data") or []
            duration = clips[0].get("duration_seconds") if clips else None
            if duration is not None:
                job["seconds"] = duration
        except (ValidationError, _json.JSONDecodeError) as exc:
            # Same ordering _media_endpoint documents, and for the same reason:
            # both subclass ValueError, and both mean the gateway settled and
            # answered 200 with a body the SDK couldn't read. Left to the arm
            # below they'd log a 400 with no flag — a settled call recorded as a
            # caller mistake. This site drifted from its sibling once already.
            parse_failed_after_settlement = True
            job["status"], status = "failed", 502
            job["error"] = {"code": "upstream_error", "message": str(exc)[:500]}
        except ValueError as exc:
            reached_gateway = False  # SDK refused it before anything was sent
            job["status"], status = "failed", 400
            job["error"] = {"code": "invalid_request", "message": str(exc)}
        except PaymentError as exc:
            job["status"], status = "failed", 402
            job["error"] = {"code": "payment_error", "message": _payment_error_sse_message(exc)}
        except APIError as exc:
            status = exc.status_code if 400 <= getattr(exc, "status_code", 0) < 600 else 502
            job["status"] = "failed"
            job["error"] = {"code": "upstream_error", "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 - background task must not die silently
            log.exception("video job %s crashed", job["id"])
            parse_failed_after_settlement = True  # transport died; may have settled
            job["status"], status = "failed", 500
            job["error"] = {"code": "server_error", "message": str(exc)}
    settlement = _video_job_settlement(job)
    _logger.log_proxy_call(
        model=job["model"],
        path="/v1/videos",
        stream=False,
        http_status=status,
        cost_usd=None,
        settlement=settlement,
        latency_ms=(time.monotonic() - _t0) * 1000.0,
        request_id=job["id"],
        settlement_status=_settlement_status(
            status,
            settlement,
            parse_failed_after_settlement=parse_failed_after_settlement,
            reached_gateway=reached_gateway,
            is_solana=is_solana,
        ),
    )


@app.post("/v1/videos", dependencies=[Depends(_require_token)])
async def openai_videos_create(request: Request) -> Any:
    """OpenAI Videos create. Returns the job object immediately; poll
    ``GET /v1/videos/{id}`` until ``status=completed`` (typ. 60-180s — the
    gateway settles payment only on completion), then fetch
    ``GET /v1/videos/{id}/content``."""
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        # LiteLLM only sends multipart when `input_reference` (a raw image
        # file) is passed. BlockRun's gateway takes image URLs, not uploads.
        raise HTTPException(
            400,
            "input_reference file uploads are not supported — pass a public "
            "image URL via the `image_url` body field for image-to-video",
        )
    body = await _json_body(request)

    prompt = body.get("prompt")
    if not prompt:
        raise HTTPException(400, "`prompt` is required")

    kwargs = _openai_video_kwargs(body)
    _prune_video_jobs()

    job: Dict[str, Any] = {
        "id": f"video_{uuid.uuid4().hex}",
        "status": "queued",
        "created_at": time.time(),
        "model": _require_named_model(body.get("model")),
        "seconds": body.get("seconds"),
        "size": body.get("size"),
        "result": None,
        "error": None,
    }
    _video_jobs[job["id"]] = job
    # Snapshot the response BEFORE scheduling — a fast job may flip the dict
    # to in_progress/completed between create_task and serialization.
    response_obj = _video_object(job)
    asyncio.get_running_loop().create_task(_run_video_job(job, prompt, kwargs))
    return JSONResponse(response_obj)


def _get_video_job_or_404(video_id: str) -> Dict[str, Any]:
    job = _video_jobs.get(video_id)
    if job is None:
        raise HTTPException(404, f"video job '{video_id}' not found (jobs expire after "
                                 f"{int(_VIDEO_JOB_TTL_S)}s and live on the sidecar instance "
                                 "that accepted the create)")
    return job


@app.get("/v1/videos/{video_id}", dependencies=[Depends(_require_token)])
async def openai_videos_status(video_id: str) -> Any:
    job = _get_video_job_or_404(video_id)
    return JSONResponse(
        _video_object(job),
        headers=_cost_response_headers(None, _video_job_settlement(job)),
    )


async def _fetch_video_content(url: str) -> Tuple[bytes, str]:
    """Download the finished clip from the gateway's CDN URL."""
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content, resp.headers.get("content-type", "video/mp4")


@app.get("/v1/videos/{video_id}/content", dependencies=[Depends(_require_token)])
async def openai_videos_content(video_id: str) -> Any:
    job = _get_video_job_or_404(video_id)
    if job["status"] != "completed":
        raise HTTPException(
            409, f"video job is '{job['status']}', not completed — poll GET /v1/videos/{video_id}"
        )
    clips = (job.get("result") or {}).get("data") or []
    url = clips[0].get("url") if clips else None
    if not url:
        raise HTTPException(502, "completed job has no video URL")
    try:
        content, media_type = await _fetch_video_content(url)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"failed to fetch video content: {exc}")
    return Response(
        content=content,
        media_type=media_type,
        headers=_cost_response_headers(None, _video_job_settlement(job)),
    )


@app.post("/v1/audio/speech", dependencies=[Depends(_require_token)])
async def audio_speech(request: Request) -> Any:
    """Synthesize speech (OpenAI-compatible TTS). Accepts ``input`` (or
    ``prompt``/``text``), ``model``, ``voice``, ``response_format``, ``speed``."""
    body = await _json_body(request)

    text = body.get("input") or body.get("prompt") or body.get("text")
    if not text:
        raise HTTPException(400, "`input` is required")

    model = _require_named_model(body.get("model"))
    return await _media_endpoint(
        "/v1/audio/speech",
        model,
        lambda: _adapter.speech_generation_async(
            input=text,
            model=model,
            voice=body.get("voice"),
            response_format=body.get("response_format"),
            speed=body.get("speed"),
        ),
    )


@app.post("/v1/audio/generations", dependencies=[Depends(_require_token)])
async def audio_generations(request: Request) -> Any:
    """Generate a music track (gateway default model). Takes 1-3 min; returns
    a CDN URL valid ~24h. ``lyrics`` requires ``instrumental: false`` — the
    SDK rejects the combination with a 400."""
    body = await _json_body(request)

    prompt = body.get("prompt")
    if not prompt:
        raise HTTPException(400, "`prompt` is required")

    model = _require_named_model(body.get("model"))
    return await _media_endpoint(
        "/v1/audio/generations",
        model,
        lambda: _adapter.music_generation_async(
            prompt=prompt,
            model=model,
            instrumental=body.get("instrumental", True),
            lyrics=body.get("lyrics"),
        ),
    )


@app.post("/v1/audio/sound-effects", dependencies=[Depends(_require_token)])
async def audio_sound_effects(request: Request) -> Any:
    """Generate a cinematic sound effect (gateway default model). Accepts
    ``text`` (or ``prompt``), ``duration_seconds``, ``prompt_influence``,
    ``response_format``."""
    body = await _json_body(request)

    text = body.get("text") or body.get("prompt")
    if not text:
        raise HTTPException(400, "`text` is required")

    model = _require_named_model(body.get("model"))
    return await _media_endpoint(
        "/v1/audio/sound-effects",
        model,
        lambda: _adapter.sound_effect_async(
            text=text,
            model=model,
            duration_seconds=body.get("duration_seconds"),
            prompt_influence=body.get("prompt_influence"),
            response_format=body.get("response_format"),
        ),
    )


# ---------------------------------------------------------------------------
# OpenAI Responses API bridge (/v1/responses)
# ---------------------------------------------------------------------------
# BlockRun's gateway only speaks Chat Completions. This endpoint accepts an
# OpenAI Responses API request (``input`` instead of ``messages``), translates
# it to a chat completion, and translates the result back to the Responses API
# shape (non-stream JSON, or the typed ``response.*`` SSE event sequence when
# ``stream=True``). Text in / text out is fully bridged; Responses-only inputs
# (tools-as-state, reasoning items, ``previous_response_id``, ``store``) are not
# round-tripped — use /v1/chat/completions for advanced tool/state flows.

_RESPONSES_INPUT_ROLES = {"system", "user", "assistant", "tool"}


def _responses_to_chat(
    body: Dict[str, Any],
) -> Tuple[Optional[str], List[Dict[str, Any]], Dict[str, Any], bool]:
    """Translate a Responses API request → (model, messages, openai_kwargs, stream)."""
    model = body.get("model")
    messages: List[Dict[str, Any]] = []

    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    inp = body.get("input")
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            role = item.get("role", "user")
            if role == "developer":
                role = "system"
            if role not in _RESPONSES_INPUT_ROLES:
                role = "user"
            content = item.get("content")
            if isinstance(content, list):
                # content parts: [{type: input_text|output_text|text, text: "..."}]
                content = "".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and isinstance(p.get("text"), str)
                )
            messages.append({"role": role, "content": content if isinstance(content, str) else ""})

    openai_kwargs: Dict[str, Any] = {}
    if body.get("max_output_tokens") is not None:
        openai_kwargs["max_tokens"] = body["max_output_tokens"]
    for k in ("temperature", "top_p", "tools", "tool_choice"):
        if body.get(k) is not None:
            openai_kwargs[k] = body[k]

    return model, messages, openai_kwargs, bool(body.get("stream"))


def _int_or_zero(value: Any) -> int:
    """Coerce an upstream token count to a non-negative int (0 on garbage).

    The detail blocks arrive as untyped pydantic extras — whatever JSON the
    upstream sent. This is the guard between that and our typed public
    Responses surface (LiteLLM has the same guard in ``Usage.__init__``).
    """
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float) and value == int(value):
        return max(int(value), 0)
    return 0


def _usage_to_responses(usage: Dict[str, Any]) -> Dict[str, Any]:
    """chat.completion ``usage`` dict → Responses API ``usage`` object.

    Spec-correct per ``openai.types.responses.ResponseUsage``: BOTH
    ``input_tokens_details`` and ``output_tokens_details`` are REQUIRED, as are
    ``cached_tokens`` / ``reasoning_tokens`` inside them — so they are always
    emitted, defaulting to 0, never conditionally omitted. Only the spec keys
    are projected: splatting the chat-shaped dicts verbatim would leak
    ``audio_tokens`` / ``accepted_prediction_tokens`` etc. into a surface that
    doesn't define them, and would pass through non-dict garbage unchecked.

    Anthropic models carry cache reads ONLY in the top-level
    ``cache_read_input_tokens`` (no ``prompt_tokens_details``), so that field
    is the fallback for ``cached_tokens``. Safe to equate: the gateway folds
    cache reads into ``prompt_tokens``, matching what OpenAI's own
    ``cached_tokens`` is a subset of.
    """
    ptd = usage.get("prompt_tokens_details")
    ctd = usage.get("completion_tokens_details")
    ptd = ptd if isinstance(ptd, dict) else {}
    ctd = ctd if isinstance(ctd, dict) else {}
    cached = _int_or_zero(ptd.get("cached_tokens"))
    if cached == 0:
        cached = _int_or_zero(usage.get("cache_read_input_tokens"))
    return {
        "input_tokens": _int_or_zero(usage.get("prompt_tokens")),
        "output_tokens": _int_or_zero(usage.get("completion_tokens")),
        "total_tokens": _int_or_zero(usage.get("total_tokens")),
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens_details": {
            "reasoning_tokens": _int_or_zero(ctd.get("reasoning_tokens"))
        },
    }


def _chat_payload_to_response(payload: Dict[str, Any], model: str) -> Dict[str, Any]:
    """chat.completion dict → Responses API ``response`` object (non-streaming)."""
    choice = (payload.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    usage = payload.get("usage") or {}
    msg_id = f"msg_{uuid.uuid4().hex}"
    return {
        "id": f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "created_at": payload.get("created") or int(time.time()),
        "model": payload.get("model") or model,
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": msg_id,
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "output_text": text,
        "usage": _usage_to_responses(usage),
    }


def _responses_event(seq: int, event_type: str, payload: Dict[str, Any]) -> str:
    """Render one Responses API SSE event (both ``event:`` and ``data:`` lines)."""
    data = {"type": event_type, "sequence_number": seq, **payload}
    return f"event: {event_type}\ndata: {_json.dumps(data)}\n\n"


async def _responses_sse_stream(
    model: str, messages: List[Dict[str, Any]], openai_kwargs: Dict[str, Any]
):
    """Bridge a chat-completion stream into the Responses API ``response.*``
    SSE event sequence (created → output_item/content_part added →
    output_text.delta* → *.done → completed)."""
    rid = f"resp_{uuid.uuid4().hex}"
    msg_id = f"msg_{uuid.uuid4().hex}"
    created = int(time.time())
    seq = 0

    def base(
        status: str, output: List[Dict[str, Any]], usage: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        r: Dict[str, Any] = {
            "id": rid,
            "object": "response",
            "created_at": created,
            "model": model,
            "status": status,
            "output": output,
        }
        if usage is not None:
            r["usage"] = usage
        return r

    yield _responses_event(seq, "response.created", {"response": base("in_progress", [])})
    seq += 1
    item_stub = {
        "id": msg_id,
        "type": "message",
        "status": "in_progress",
        "role": "assistant",
        "content": [],
    }
    yield _responses_event(
        seq, "response.output_item.added", {"output_index": 0, "item": item_stub}
    )
    seq += 1
    yield _responses_event(
        seq,
        "response.content_part.added",
        {
            "item_id": msg_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
    )
    seq += 1

    parts: List[str] = []
    usage_out: Dict[str, Any] = _usage_to_responses({})

    async with _get_semaphore():
        try:
            async for chunk in _adapter.chat_completion_stream_async(
                model=model, messages=messages, **openai_kwargs
            ):
                cd = chunk.model_dump(exclude_none=True)
                ch = (cd.get("choices") or [{}])[0]
                delta = (ch.get("delta") or {}).get("content")
                if delta:
                    parts.append(delta)
                    yield _responses_event(
                        seq,
                        "response.output_text.delta",
                        {
                            "item_id": msg_id,
                            "output_index": 0,
                            "content_index": 0,
                            "delta": delta,
                        },
                    )
                    seq += 1
                u = cd.get("usage")
                if u:
                    # MERGE, don't replace: the gateway emits one canonical
                    # include_usage final frame, but if a provider ever sends a
                    # later bare usage frame (three counts, no details), a
                    # wholesale reassignment would wipe detail captured
                    # earlier. Scalar counts take the latest value; the two
                    # detail blocks only advance, never regress to zero.
                    fresh = _usage_to_responses(u)
                    for block in ("input_tokens_details", "output_tokens_details"):
                        prev = usage_out.get(block) or {}
                        if any(fresh[block].values()) or not any(prev.values()):
                            continue
                        fresh[block] = prev
                    usage_out = fresh
        except PaymentError as exc:
            yield _responses_event(
                seq,
                "response.failed",
                {
                    "response": base("failed", []),
                    "error": {"code": "payment_error", "message": _payment_error_sse_message(exc)},
                },
            )
            return
        except APIError as exc:
            yield _responses_event(
                seq,
                "response.failed",
                {
                    "response": base("failed", []),
                    "error": {"code": "upstream_error", "message": str(exc)},
                },
            )
            return
        except Exception as exc:  # noqa: BLE001
            if _is_solana_rpc_exc(exc):
                log.warning("solana rpc error during responses stream: %s", _solana_rpc_msg(exc))
                msg = _solana_rpc_msg(exc)
            else:
                log.exception("responses stream error")
                msg = str(exc)
            yield _responses_event(
                seq,
                "response.failed",
                {
                    "response": base("failed", []),
                    "error": {"code": "server_error", "message": msg},
                },
            )
            return

    text = "".join(parts)
    yield _responses_event(
        seq,
        "response.output_text.done",
        {
            "item_id": msg_id,
            "output_index": 0,
            "content_index": 0,
            "text": text,
        },
    )
    seq += 1
    yield _responses_event(
        seq,
        "response.content_part.done",
        {
            "item_id": msg_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": text, "annotations": []},
        },
    )
    seq += 1
    final_item = {
        "id": msg_id,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    yield _responses_event(
        seq, "response.output_item.done", {"output_index": 0, "item": final_item}
    )
    seq += 1
    yield _responses_event(
        seq, "response.completed", {"response": base("completed", [final_item], usage_out)}
    )


@app.post("/v1/responses", dependencies=[Depends(_require_token)])
async def responses(request: Request) -> Any:
    """OpenAI Responses API bridge → BlockRun Chat Completions."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    model, messages, openai_kwargs, stream = _responses_to_chat(body)
    if not model or not messages:
        raise HTTPException(400, "`model` and `input` are required")

    if stream:
        return StreamingResponse(
            _responses_sse_stream(model, messages, openai_kwargs),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    async with _get_semaphore():
        try:
            payload = await _adapter.chat_completion_async(
                model=model, messages=messages, **openai_kwargs
            )
        except PaymentError as exc:
            return JSONResponse(status_code=402, content=_payment_error_payload(exc))
        except APIError as exc:
            status = exc.status_code if 400 <= getattr(exc, "status_code", 0) < 600 else 502
            return JSONResponse(status_code=status, content={"error": str(exc)})
        except Exception as exc:
            if _is_solana_rpc_exc(exc):
                log.warning("solana rpc error: %s", _solana_rpc_msg(exc))
                raise HTTPException(503, _solana_rpc_msg(exc))
            raise

    return _chat_payload_to_response(payload, model)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="blockrun-litellm-proxy",
        description="OpenAI-compatible local proxy for BlockRun (x402 gateway).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=4001, help="Bind port (default: 4001)")
    parser.add_argument(
        "--api-url",
        default=None,
        help="Override BlockRun API URL (default: https://blockrun.ai/api)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
    )
    args = parser.parse_args()

    if args.api_url:
        os.environ["BLOCKRUN_API_URL"] = args.api_url

    # Fail fast if no wallet — better than waiting for first request.
    try:
        _adapter.get_sync_client()
    except ValueError as exc:
        parser.exit(2, f"\nWallet not configured:\n  {exc}\n")

    import uvicorn

    uvicorn.run(
        "blockrun_litellm.proxy:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
