"""
Shared adapter between OpenAI-format requests and the blockrun-llm SDK.

Used by both the LiteLLM CustomLLM provider (in-process) and the FastAPI
proxy (sidecar). Keeping the conversion logic in one place ensures the two
modes behave identically.

Design notes
------------
- BlockRun's HTTP API is already OpenAI-shaped. The blockrun-llm SDK
  returns a Pydantic ``ChatResponse`` (or ``ChatCompletionChunk`` in
  stream mode) whose ``.model_dump()`` is a valid OpenAI Chat Completions
  response (or chunk).
- We therefore only translate at the *boundary*: accept an OpenAI dict
  request, dispatch through ``LLMClient.chat_completion(...)`` /
  ``chat_completion_stream(...)`` so x402 signing happens inside the SDK,
  and return the dumped pydantic models.
- The ``model`` string is forwarded verbatim. LiteLLM strips its
  ``blockrun/`` prefix before invoking the handler, so values like
  ``openai/gpt-5.5`` reach this layer unchanged — which is exactly what
  the BlockRun gateway expects.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional

from blockrun_llm import AsyncLLMClient, ImageClient, LLMClient
from ._auth import account_key, account_auth, cache_key, wallet_url
from blockrun_llm.types import APIError, ChatCompletionChunk, PaymentError

try:
    from blockrun_llm import AsyncSolanaLLMClient, SolanaLLMClient

    _HAS_SOLANA = True
except ImportError:
    _HAS_SOLANA = False
    SolanaLLMClient = None  # type: ignore[assignment]
    AsyncSolanaLLMClient = None  # type: ignore[assignment]


# Default endpoints — used to decide chain when no explicit api_url is given.
SOLANA_API_URL = "https://sol.blockrun.ai/api"
BASE_API_URL = "https://blockrun.ai/api"

_log = logging.getLogger(__name__)


def _canonical_video_model(model: Optional[str]) -> Optional[str]:
    """Namespace the short video model ids accepted by OpenAI-style clients.

    Token360 documents ``seedance-2.0-fast`` while BlockRun's gateway catalog
    uses canonical provider-qualified ids.  The proxy must bridge that naming
    difference before calling the SDK's flat ``/videos/generations`` surface.
    The gateway does the same rewrite itself, but only in its ``content[]``
    handler for ``POST /v1/videos`` — never on the flat surface the SDK posts
    to, and the SDK doesn't normalize either.

    The id is lowercased on the way through: the catalog lookup is an exact
    ``==`` match, so a case-preserved ``bytedance/Seedance-2.0-Fast`` would 400
    just like the bare id it replaced.

    Only unambiguous bare ids are mapped, and only ones whose vendor is implied
    by the name itself.  ``sora-2`` is deliberately absent: the catalog ships it
    under *two* vendors (``azure/sora-2``, available; ``openai/sora-2``, not),
    so there is no namespace to infer — guessing would silently pick a vendor on
    the caller's behalf, and the choice would flip meaning the day availability
    does.  An unmapped bare id reaches the gateway untouched and 400s with the
    full list of real ids, which is the better answer than a confident guess.
    """
    if not isinstance(model, str):
        return model
    bare = model.strip().lower()
    if bare.startswith("seedance-"):
        return f"bytedance/{bare}"
    if bare == "grok-imagine-video":
        return f"xai/{bare}"
    return model


def _is_solana_url(api_url: Optional[str]) -> bool:
    """Sniff whether the effective gateway URL points at Solana.

    Falls back to the ``BLOCKRUN_API_URL`` env var when no explicit
    ``api_url`` is passed. This matters for the FastAPI sidecar: the
    request handlers don't forward an ``api_url`` arg, so without the
    env-var fallback we'd silently route Solana traffic to the Base
    async client and crash inside the EVM payment encoder
    (``eth_abi.AddressEncoder`` rejects base58 mint addresses).
    """
    resolved = api_url or os.environ.get("BLOCKRUN_API_URL", "")
    return bool(resolved) and "sol.blockrun.ai" in resolved


# ---------------------------------------------------------------------------
# Client cache (Base + Solana)
# ---------------------------------------------------------------------------
# Constructing a client parses the private key and instantiates an HTTP
# session. We memoize per (chain, api_url, private_key) so high-QPS adapters
# don't re-create wallets for every request. The chain is part of the key
# so a Base call and a Solana call don't collide.

# Default chat HTTP timeout (seconds) for the SDK clients the adapter builds.
# The SDK default was 120s — too low for reasoning models (opus-4.8 /
# deepseek-v4-pro routinely take 200–300s). Passed explicitly so the adapter
# doesn't depend on the installed SDK version's default. Override via
# BLOCKRUN_CHAT_TIMEOUT. NB: for streaming this is a per-chunk read timeout;
# for non-stream it's the whole-call timeout.
_DEFAULT_CHAT_TIMEOUT_S = 600.0


def _chat_timeout() -> float:
    """Resolve the chat timeout, falling back on a malformed env var.

    Mirrors :func:`_solana_image_timeout` — a non-numeric BLOCKRUN_CHAT_TIMEOUT
    (e.g. ``"600s"``) must NOT crash module import, which would take down both
    the provider and the proxy that import this module.
    """
    raw = os.environ.get("BLOCKRUN_CHAT_TIMEOUT")
    if not raw:
        return _DEFAULT_CHAT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_CHAT_TIMEOUT_S


_CHAT_TIMEOUT = _chat_timeout()

_sync_clients: Dict[str, Any] = {}  # may be LLMClient or SolanaLLMClient
_async_clients: Dict[str, AsyncLLMClient] = {}
_image_clients: Dict[str, Any] = {}  # ImageClient (Base) or SolanaLLMClient (Solana)
_lock = threading.Lock()

# Bounded thread pool for image generation (ImageClient is sync-only).
# Capped at 20 so that high-concurrency image requests don't spawn unlimited
# threads and exhaust memory. Matches the default BLOCKRUN_MAX_CONCURRENT.
_image_executor: concurrent.futures.ThreadPoolExecutor = concurrent.futures.ThreadPoolExecutor(
    max_workers=20
)


def _wallet_env_var(api_url: Optional[str]) -> str:
    """Which env var to consult for the default wallet on this chain."""
    return "SOLANA_WALLET_KEY" if _is_solana_url(api_url) else "BLOCKRUN_WALLET_KEY"


def _client_key(api_url, private_key, api_key=None):
    return cache_key(api_url, private_key, api_key)


def get_sync_client(api_url=None, private_key=None, api_key=None):
    key = _client_key(api_url, private_key, api_key)
    with _lock:
        if key not in _sync_clients:
            auth = account_auth(api_key, private_key, api_url)
            if auth:
                client = LLMClient(
                    api_key=account_key(api_key, private_key),
                    api_url=auth.api_url,
                    timeout=_CHAT_TIMEOUT,
                )
            else:
                url = wallet_url(api_url, private_key)
                cls = SolanaLLMClient if _is_solana_url(url) else LLMClient
                if cls is None:
                    raise ImportError("Install blockrun-litellm[solana] for Solana wallets")
                client = cls(private_key=private_key, api_url=url, timeout=_CHAT_TIMEOUT)
            _sync_clients[key] = client
        return _sync_clients[key]


def get_async_client(api_url=None, private_key=None, api_key=None):
    key = _client_key(api_url, private_key, api_key)
    with _lock:
        if key not in _async_clients:
            auth = account_auth(api_key, private_key, api_url)
            if auth:
                client = AsyncLLMClient(
                    api_key=account_key(api_key, private_key),
                    api_url=auth.api_url,
                    timeout=_CHAT_TIMEOUT,
                )
            else:
                url = wallet_url(api_url, private_key)
                cls = AsyncSolanaLLMClient if _is_solana_url(url) else AsyncLLMClient
                if cls is None:
                    raise ImportError("Install blockrun-litellm[solana] for Solana wallets")
                client = cls(private_key=private_key, api_url=url, timeout=_CHAT_TIMEOUT)
            _async_clients[key] = client
        return _async_clients[key]


# ---------------------------------------------------------------------------
# Request normalization
# ---------------------------------------------------------------------------

# OpenAI-style params that blockrun-llm's chat methods accept directly.
# Anything outside this set is dropped — LiteLLM tends to forward
# provider-specific kwargs that don't apply here.
#
# Since blockrun-llm 0.22.1 (Solana) / 0.20.0 (Base), the set is the same
# on both chains — function calling (``tools`` / ``tool_choice``) works on
# either path because the BlockRun gateway forwards them to the upstream
# model unchanged; the chain only differs in the payment leg.
_FORWARDED_KWARGS = {
    "max_tokens",
    "temperature",
    "top_p",
    "tools",
    "tool_choice",
    "search",
    "search_parameters",
    "fallback_models",
    # Reasoning controls — the gateway forwards these to the upstream model
    # (e.g. Anthropic extended thinking). Without them in the whitelist they
    # were silently dropped, so callers could never trigger thinking via litellm.
    "reasoning_effort",
    "thinking",
}


def _filter_kwargs(payload: Dict[str, Any], *, is_solana: bool = False) -> Dict[str, Any]:
    """Whitelist OpenAI-format kwargs into the shape blockrun-llm wants.

    The ``is_solana`` parameter is currently unused — kept in the signature
    for backwards compatibility / future chain-specific filtering. Both
    chains accept the same set of kwargs.

    Does **not** raise on ``stream=True`` — streaming has its own entrypoint.
    """
    return {k: payload[k] for k in _FORWARDED_KWARGS if payload.get(k) is not None}


# ---------------------------------------------------------------------------
# Real-cost extraction
# ---------------------------------------------------------------------------
# LiteLLM bills off a token-count × list-price estimate, which does NOT match
# BlockRun's real x402 charge (the gateway price carries a per-call floor +
# margin). We surface the SDK's real charge so callers can report the actual
# wallet deduction instead. The authoritative source is the per-call value the
# SDK attaches to the response (``response.cost_usd``, since blockrun-llm 1.3);
# ``client._last_call_cost`` is a best-effort fallback for older SDKs (note it
# goes stale on free/cached calls and is racy under shared-client concurrency).
_BLOCKRUN_META_KEY = "_blockrun"


def _strip_real_cost(payload: Dict[str, Any], client: Any) -> Dict[str, Any]:
    """Pop the SDK-attached cost/settlement out of the dumped payload and return
    a ``{cost_usd, settlement}`` meta dict (cost may be ``None`` if unavailable)."""
    cost = payload.pop("cost_usd", None)
    settlement = payload.pop("settlement", None)
    if getattr(client, "auth_mode", None) == "api-key":
        return {"auth_mode": "api-key", "cost_usd": None, "settlement": None}
    if cost is None:
        cost = getattr(client, "_last_call_cost", None)
    return {"cost_usd": cost, "settlement": settlement}


# ---------------------------------------------------------------------------
# Non-streaming entrypoints
# ---------------------------------------------------------------------------


def chat_completion_sync(
    model: str,
    messages: List[Dict[str, Any]],
    *,
    api_url: Optional[str] = None,
    private_key: Optional[str] = None,
    api_key: Optional[str] = None,
    **openai_kwargs: Any,
) -> Dict[str, Any]:
    """
    Run a non-streaming chat completion via blockrun-llm; return an
    OpenAI-format dict.

    Routes to Solana (``SolanaLLMClient``) when ``api_url`` points at
    ``sol.blockrun.ai``, otherwise Base (``LLMClient``). Any LiteLLM
    ``blockrun/`` prefix should already be stripped by the caller.

    For ``stream=True``, use :func:`chat_completion_stream_sync` instead.
    """
    openai_kwargs.pop("stream", None)
    is_solana = _is_solana_url(api_url)
    kwargs = _filter_kwargs(openai_kwargs, is_solana=is_solana)
    client = get_sync_client(
        api_url=api_url,
        private_key=private_key,
        **({"api_key": api_key} if api_key is not None else {}),
    )
    response = client.chat_completion(model=model, messages=messages, **kwargs)
    payload = response.model_dump(exclude_none=True)
    payload[_BLOCKRUN_META_KEY] = _strip_real_cost(payload, client)
    return payload


async def chat_completion_async(
    model: str,
    messages: List[Dict[str, Any]],
    *,
    api_url: Optional[str] = None,
    private_key: Optional[str] = None,
    api_key: Optional[str] = None,
    **openai_kwargs: Any,
) -> Dict[str, Any]:
    """Async variant of :func:`chat_completion_sync`.

    **Base only today.** Solana ``api_url`` raises ``NotImplementedError``
    via :func:`get_async_client` since the SDK has no async Solana client.
    """
    openai_kwargs.pop("stream", None)
    is_solana = _is_solana_url(api_url)
    kwargs = _filter_kwargs(openai_kwargs, is_solana=is_solana)
    client = get_async_client(
        api_url=api_url,
        private_key=private_key,
        **({"api_key": api_key} if api_key is not None else {}),
    )
    response = await client.chat_completion(model=model, messages=messages, **kwargs)
    payload = response.model_dump(exclude_none=True)
    payload[_BLOCKRUN_META_KEY] = _strip_real_cost(payload, client)
    return payload


# ---------------------------------------------------------------------------
# Streaming entrypoints
# ---------------------------------------------------------------------------


def chat_completion_stream_sync(
    model: str,
    messages: List[Dict[str, Any]],
    *,
    api_url: Optional[str] = None,
    private_key: Optional[str] = None,
    api_key: Optional[str] = None,
    **openai_kwargs: Any,
) -> Iterator[ChatCompletionChunk]:
    """
    Stream a chat completion via the SDK's ``chat_completion_stream``.

    Routes to Solana or Base based on ``api_url``. Yields
    :class:`ChatCompletionChunk` objects (OpenAI chunk schema). Caller
    is responsible for downstream formatting (LiteLLM
    ``GenericStreamingChunk``, FastAPI ``data: <json>\\n\\n``, etc.).
    """
    openai_kwargs.pop("stream", None)
    is_solana = _is_solana_url(api_url)
    kwargs = _filter_kwargs(openai_kwargs, is_solana=is_solana)
    client = get_sync_client(
        api_url=api_url,
        private_key=private_key,
        **({"api_key": api_key} if api_key is not None else {}),
    )
    yield from client.chat_completion_stream(model=model, messages=messages, **kwargs)


async def chat_completion_stream_async(
    model: str,
    messages: List[Dict[str, Any]],
    *,
    api_url: Optional[str] = None,
    private_key: Optional[str] = None,
    api_key: Optional[str] = None,
    **openai_kwargs: Any,
) -> AsyncIterator[ChatCompletionChunk]:
    """Async variant of :func:`chat_completion_stream_sync`.

    **Base only today.** A Solana ``api_url`` raises
    ``NotImplementedError`` since the SDK has no async Solana client.
    """
    openai_kwargs.pop("stream", None)
    is_solana = _is_solana_url(api_url)
    kwargs = _filter_kwargs(openai_kwargs, is_solana=is_solana)
    client = get_async_client(
        api_url=api_url,
        private_key=private_key,
        **({"api_key": api_key} if api_key is not None else {}),
    )
    async for chunk in client.chat_completion_stream(model=model, messages=messages, **kwargs):
        yield chunk


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------
# ImageClient is sync-only in blockrun-llm; async callers run it in a thread.


# Per-image-request timeout (seconds) for the Solana image client. Default 300s
# leaves headroom above the SDK's 200s ``image_timeout`` for the slow tail of
# ``openai/gpt-image-2`` (public reports cite 145-280s). Read at call time so
# ``BLOCKRUN_SOLANA_IMAGE_TIMEOUT`` can be tuned without a process restart.
_DEFAULT_SOLANA_IMAGE_TIMEOUT_S = 300.0


def _solana_image_timeout() -> float:
    raw = os.environ.get("BLOCKRUN_SOLANA_IMAGE_TIMEOUT")
    if not raw:
        return _DEFAULT_SOLANA_IMAGE_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_SOLANA_IMAGE_TIMEOUT_S


def get_image_client(api_url=None, private_key=None, api_key=None):
    key = _client_key(api_url, private_key, api_key)
    with _lock:
        if key not in _image_clients:
            auth = account_auth(api_key, private_key, api_url)
            if auth:
                client = ImageClient(
                    api_key=account_key(api_key, private_key), api_url=auth.api_url
                )
            elif _is_solana_url(wallet_url(api_url, private_key)):
                if SolanaLLMClient is None:
                    raise ImportError("Install blockrun-litellm[solana]")
                client = SolanaLLMClient(
                    private_key=private_key,
                    api_url=wallet_url(api_url, private_key),
                    image_timeout=_solana_image_timeout(),
                )
            else:
                client = ImageClient(
                    private_key=private_key, api_url=wallet_url(api_url, private_key)
                )
            _image_clients[key] = client
        return _image_clients[key]


def _is_solana_image_client(client: Any) -> bool:
    return _HAS_SOLANA and SolanaLLMClient is not None and isinstance(client, SolanaLLMClient)


# `quality` exists only on the Solana image surface: the Base gateway defines no
# such field and strips unknown keys, and the SDK's ImageClient raises TypeError
# on it rather than forward it.
#
# 0.7.0 refused it on Base with a 400. That was wrong: 0.6.1 never read `quality`
# at all, so callers using it got a 200 — and `quality` is a first-class OpenAI
# Images parameter on a route documented as DALL-E compatible. Turning a
# previously-working, spec-compliant request into a hard failure is a breaking
# change, not something to slip into a minor bump as a side effect of adding
# Solana support.
#
# So: drop it on Base, as 0.6.1 effectively did, but say so — a warning log plus
# an `x-blockrun-warning` response header, so the value doesn't vanish in silence
# the way it used to.
_QUALITY_UNSUPPORTED_ON_BASE = (
    "`quality` is supported on Solana only (openai/gpt-image-* via "
    "sol.blockrun.ai) and was ignored: the Base gateway has no quality field. "
    "Point BLOCKRUN_API_URL at the Solana gateway to use it."
)


def _invoke_image_generate(client: Any, prompt: str, *, model, size, n, quality=None):
    """Dispatch ``generate`` (Base ImageClient) vs ``image`` (SolanaLLMClient).

    ``ImageClient.generate`` and ``SolanaLLMClient.image`` are intentionally
    named differently in the SDK but accept the same call shape, except for
    ``quality`` — Solana only, see :data:`_QUALITY_UNSUPPORTED_ON_BASE`.
    """
    # Omit optional values instead of passing ``None``. This keeps the adapter
    # compatible with SDK releases from before an optional parameter was added.
    kwargs: Dict[str, Any] = {"n": n}
    if model is not None:
        kwargs["model"] = model
    if size is not None:
        kwargs["size"] = size
    if _is_solana_image_client(client):
        if quality is not None:
            kwargs["quality"] = quality
        # SolanaLLMClient.image requires non-None model/size (no class-level defaults).
        return client.image(prompt, **kwargs)
    if quality is not None:
        _log.warning(_QUALITY_UNSUPPORTED_ON_BASE)
    return client.generate(prompt, **kwargs)


def _invoke_image_edit(
    client: Any,
    prompt: str,
    image: Any,
    *,
    model: Optional[str],
    mask: Optional[str],
    size: Optional[str],
    n: int,
    quality: Optional[str] = None,
):
    kwargs: Dict[str, Any] = {"n": n}
    for key, value in {
        "model": model,
        "mask": mask,
        "size": size,
    }.items():
        if value is not None:
            kwargs[key] = value
    if _is_solana_image_client(client):
        if quality is not None:
            kwargs["quality"] = quality
        return client.image_edit(prompt, image, **kwargs)
    if quality is not None:
        _log.warning(_QUALITY_UNSUPPORTED_ON_BASE)
    return client.edit(prompt, image, **kwargs)


def image_generation_sync(
    prompt: str,
    *,
    model: Optional[str] = None,
    size: Optional[str] = None,
    n: int = 1,
    quality: Optional[str] = None,
    api_url: Optional[str] = None,
    private_key: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    client = get_image_client(
        api_url=api_url,
        private_key=private_key,
        **({"api_key": api_key} if api_key is not None else {}),
    )
    response = _invoke_image_generate(client, prompt, model=model, size=size, n=n, quality=quality)
    return response.model_dump(exclude_none=True)


async def image_generation_async(
    prompt: str,
    *,
    model: Optional[str] = None,
    size: Optional[str] = None,
    n: int = 1,
    quality: Optional[str] = None,
    api_url: Optional[str] = None,
    private_key: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    client = get_image_client(
        api_url=api_url,
        private_key=private_key,
        **({"api_key": api_key} if api_key is not None else {}),
    )
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        _image_executor,
        lambda: _invoke_image_generate(
            client, prompt, model=model, size=size, n=n, quality=quality
        ),
    )
    return response.model_dump(exclude_none=True)


def image_edit_sync(
    prompt: str,
    image: Any,
    *,
    model: Optional[str] = None,
    mask: Optional[str] = None,
    size: Optional[str] = None,
    n: int = 1,
    quality: Optional[str] = None,
    api_url: Optional[str] = None,
    private_key: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    client = get_image_client(
        api_url=api_url,
        private_key=private_key,
        **({"api_key": api_key} if api_key is not None else {}),
    )
    response = _invoke_image_edit(
        client,
        prompt,
        image,
        model=model,
        mask=mask,
        size=size,
        n=n,
        quality=quality,
    )
    return response.model_dump(exclude_none=True)


async def image_edit_async(
    prompt: str,
    image: Any,
    *,
    model: Optional[str] = None,
    mask: Optional[str] = None,
    size: Optional[str] = None,
    n: int = 1,
    quality: Optional[str] = None,
    api_url: Optional[str] = None,
    private_key: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    client = get_image_client(
        api_url=api_url,
        private_key=private_key,
        **({"api_key": api_key} if api_key is not None else {}),
    )
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        _image_executor,
        lambda: _invoke_image_edit(
            client,
            prompt,
            image,
            model=model,
            mask=mask,
            size=size,
            n=n,
            quality=quality,
        ),
    )
    return response.model_dump(exclude_none=True)


# ---------------------------------------------------------------------------
# Video / music / speech generation (Base dedicated clients vs Solana unified)
# ---------------------------------------------------------------------------
# On Base each medium has its own SDK client (VideoClient/MusicClient/
# SpeechClient). On Solana every medium is a method on the one SolanaLLMClient
# (which get_image_client already builds + caches). All of these clients are
# sync-only, so async callers run them in a thread pool. Fast media (speech,
# sound-effects) share _image_executor with images; long-running media (video
# 60-900s, music 60-210s) get their own smaller pool so a burst of video jobs
# can't pin all 20 image/speech threads for 15 minutes.

_media_clients: Dict[str, Any] = {}

_LONG_MEDIA_THREADS: int = int(os.environ.get("BLOCKRUN_LONG_MEDIA_THREADS", "8"))
_long_media_executor: concurrent.futures.ThreadPoolExecutor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_LONG_MEDIA_THREADS
)

# Server-side wall-clock ceilings. budget_seconds/timeout arrive from the
# request body, so without a clamp a caller could pin a worker thread for a
# day. The video cap matches the SDK's DEFAULT_GENERATE_BUDGET_SECONDS; the
# await ceilings add margin over the longest legitimate SDK call so the
# coroutine (and its semaphore permit) is always released even if the SDK
# thread wedges. NB: wait_for can't kill the worker thread — it only frees
# the awaiting coroutine; the orphaned thread exits when the SDK call does.
_VIDEO_BUDGET_CAP_S = 900.0
_VIDEO_AWAIT_CEILING_S = _VIDEO_BUDGET_CAP_S + 120.0
_MUSIC_AWAIT_CEILING_S = 300.0  # MusicClient DEFAULT_TIMEOUT 210s + margin
_SPEECH_AWAIT_CEILING_S = 150.0  # SpeechClient DEFAULT_TIMEOUT 120s + margin

# Lazy imports keep module import working on SDK versions predating a client.
_BASE_MEDIA_CLASSES = {"video": "VideoClient", "music": "MusicClient", "speech": "SpeechClient"}


def _get_media_client(medium, api_url, private_key, api_key=None):
    import blockrun_llm

    auth = account_auth(api_key, private_key, api_url)
    if not auth and _is_solana_url(wallet_url(api_url, private_key)):
        return get_image_client(api_url=api_url, private_key=private_key)
    cls = getattr(blockrun_llm, _BASE_MEDIA_CLASSES[medium])
    key = cls.__name__ + "::" + _client_key(api_url, private_key, api_key)
    with _lock:
        if key not in _media_clients:
            _media_clients[key] = (
                cls(api_key=account_key(api_key, private_key), api_url=auth.api_url)
                if auth
                else cls(private_key=private_key, api_url=wallet_url(api_url, private_key))
            )
        return _media_clients[key]


def _is_solana_client(client: Any) -> bool:
    return _HAS_SOLANA and SolanaLLMClient is not None and isinstance(client, SolanaLLMClient)


def _solana_media_method(client: Any, method: str) -> Any:
    """Resolve a Solana media method, failing with a clear 501 instead of an
    AttributeError-500 when the installed blockrun-llm predates Solana media
    support (SolanaLLMClient.video/music/speech/sound_effect)."""
    fn = getattr(client, method, None)
    if fn is None:
        raise APIError(
            f"Solana {method} generation requires a blockrun-llm release with "
            f"SolanaLLMClient.{method} support. Upgrade: pip install -U 'blockrun-llm[solana]'",
            501,
        )
    return fn


def get_video_client(
    api_url: Optional[str] = None, private_key: Optional[str] = None, api_key: Optional[str] = None
) -> Any:
    """VideoClient (Base) or the unified SolanaLLMClient (Solana)."""
    return _get_media_client("video", api_url, private_key, api_key)


def get_music_client(
    api_url: Optional[str] = None, private_key: Optional[str] = None, api_key: Optional[str] = None
) -> Any:
    """MusicClient (Base) or the unified SolanaLLMClient (Solana)."""
    return _get_media_client("music", api_url, private_key, api_key)


def get_speech_client(
    api_url: Optional[str] = None, private_key: Optional[str] = None, api_key: Optional[str] = None
) -> Any:
    """SpeechClient (Base) or the unified SolanaLLMClient (Solana). Serves both
    TTS (speech) and sound-effects."""
    return _get_media_client("speech", api_url, private_key, api_key)


async def _run_media(
    func: Any,
    *,
    executor: concurrent.futures.ThreadPoolExecutor = _image_executor,
    ceiling: float = _SPEECH_AWAIT_CEILING_S,
) -> Any:
    """Run a sync SDK media call in a worker thread, bounded by ``ceiling``."""
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(executor, func), timeout=ceiling)
    except asyncio.TimeoutError:
        raise APIError(
            f"media generation exceeded the {ceiling:.0f}s server ceiling and the "
            "request was abandoned; the background job may still complete (and "
            "settle payment) — check your wallet history before retrying",
            504,
        )


# Accepted /v1/videos/generations body params, forwarded to the SDK when
# present. Single source of truth — proxy.py imports this.
VIDEO_PARAM_KEYS = (
    "image_url",
    "last_frame_url",
    "reference_image_urls",
    "real_face_asset_id",
    # Declared seed mode, cross-checked by the gateway against the seed fields
    # above (400, unbilled, on disagreement). Needs blockrun-llm >=1.7.0 on both
    # chains — see the floor in pyproject.toml.
    "input_type",
    "duration_seconds",
    "aspect_ratio",
    "resolution",
    "generate_audio",
    "seed",
    "watermark",
    "return_last_frame",
    "budget_seconds",
    "timeout",
)


async def video_generation_async(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_url: Optional[str] = None,
    private_key: Optional[str] = None,
    api_key: Optional[str] = None,
    **params: Any,
) -> Dict[str, Any]:
    """Generate a video. Extra kwargs (see :data:`VIDEO_PARAM_KEYS`) forward to
    the SDK. ``timeout`` is only honored on Solana (Base VideoClient has no such
    arg). Client-supplied ``budget_seconds``/``timeout`` are clamped to the
    server cap so a request body can't pin a worker thread indefinitely; a
    malformed (non-numeric) value raises ValueError → HTTP 400 at the proxy."""
    client = get_video_client(
        api_url=api_url,
        private_key=private_key,
        **({"api_key": api_key} if api_key is not None else {}),
    )
    model = _canonical_video_model(model)
    params = {k: v for k, v in params.items() if v is not None}
    for knob in ("budget_seconds", "timeout"):
        if knob in params:
            params[knob] = min(float(params[knob]), _VIDEO_BUDGET_CAP_S)
    if _is_solana_client(client):
        video = _solana_media_method(client, "video")
        response = await _run_media(
            lambda: video(prompt, model=model, **params),
            executor=_long_media_executor,
            ceiling=_VIDEO_AWAIT_CEILING_S,
        )
    else:
        params.pop("timeout", None)  # Base VideoClient.generate has no timeout kwarg
        response = await _run_media(
            lambda: client.generate(prompt, model=model, **params),
            executor=_long_media_executor,
            ceiling=_VIDEO_AWAIT_CEILING_S,
        )
    return response.model_dump(exclude_none=True)


async def music_generation_async(
    prompt: str,
    *,
    model: Optional[str] = None,
    instrumental: bool = True,
    lyrics: Optional[str] = None,
    api_url: Optional[str] = None,
    private_key: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a music track. Raises ValueError (→ HTTP 400 at the proxy) when
    ``lyrics`` is combined with ``instrumental=True`` — the SDK rejects that."""
    client = get_music_client(
        api_url=api_url,
        private_key=private_key,
        **({"api_key": api_key} if api_key is not None else {}),
    )
    # Same call shape on both chains; only the method name differs.
    media_fn = (
        _solana_media_method(client, "music") if _is_solana_client(client) else client.generate
    )
    response = await _run_media(
        lambda: media_fn(prompt, model=model, instrumental=instrumental, lyrics=lyrics),
        executor=_long_media_executor,
        ceiling=_MUSIC_AWAIT_CEILING_S,
    )
    return response.model_dump(exclude_none=True)


async def speech_generation_async(
    input: str,
    *,
    model: Optional[str] = None,
    voice: Optional[str] = None,
    response_format: Optional[str] = None,
    speed: Optional[float] = None,
    api_url: Optional[str] = None,
    private_key: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Synthesize speech (TTS)."""
    client = get_speech_client(
        api_url=api_url,
        private_key=private_key,
        **({"api_key": api_key} if api_key is not None else {}),
    )
    kw = {"model": model, "voice": voice, "response_format": response_format, "speed": speed}
    kw = {k: v for k, v in kw.items() if v is not None}
    media_fn = (
        _solana_media_method(client, "speech") if _is_solana_client(client) else client.generate
    )
    response = await _run_media(lambda: media_fn(input, **kw), ceiling=_SPEECH_AWAIT_CEILING_S)
    return response.model_dump(exclude_none=True)


async def sound_effect_async(
    text: str,
    *,
    model: Optional[str] = None,
    duration_seconds: Optional[float] = None,
    prompt_influence: Optional[float] = None,
    response_format: Optional[str] = None,
    api_url: Optional[str] = None,
    private_key: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a cinematic sound effect."""
    client = get_speech_client(
        api_url=api_url,
        private_key=private_key,
        **({"api_key": api_key} if api_key is not None else {}),
    )
    kw = {
        "model": model,
        "duration_seconds": duration_seconds,
        "prompt_influence": prompt_influence,
        "response_format": response_format,
    }
    kw = {k: v for k, v in kw.items() if v is not None}
    # Both SolanaLLMClient and SpeechClient expose .sound_effect with the same
    # shape; the guard only matters on SDK versions predating Solana media.
    if _is_solana_client(client):
        sound_effect = _solana_media_method(client, "sound_effect")
    else:
        sound_effect = client.sound_effect
    response = await _run_media(lambda: sound_effect(text, **kw), ceiling=_SPEECH_AWAIT_CEILING_S)
    return response.model_dump(exclude_none=True)


__all__ = [
    "chat_completion_sync",
    "chat_completion_async",
    "chat_completion_stream_sync",
    "chat_completion_stream_async",
    "get_sync_client",
    "get_async_client",
    "get_image_client",
    "image_generation_sync",
    "image_generation_async",
    "image_edit_sync",
    "image_edit_async",
    "get_video_client",
    "get_music_client",
    "get_speech_client",
    "VIDEO_PARAM_KEYS",
    "video_generation_async",
    "music_generation_async",
    "speech_generation_async",
    "sound_effect_async",
    "APIError",
    "PaymentError",
]
