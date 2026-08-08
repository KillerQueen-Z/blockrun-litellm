"""
LiteLLM ``CustomLLM`` handler for BlockRun.

Usage
-----
::

    import litellm
    from blockrun_litellm import register

    register()  # adds the "blockrun" provider to LiteLLM

    response = litellm.completion(
        model="blockrun/openai/gpt-5.5",
        messages=[{"role": "user", "content": "Hello"}],
        max_tokens=128,
    )
    print(response.choices[0].message.content)

The ``register()`` call appends an entry to ``litellm.custom_provider_map``.
Calling it twice is idempotent.

Wallet
------
The underlying ``blockrun-llm`` SDK reads the private key from (in order):

1. ``private_key`` kwarg forwarded via ``optional_params``
2. ``BLOCKRUN_WALLET_KEY`` env var
3. ``BASE_CHAIN_WALLET_KEY`` env var
4. ``~/.blockrun/.session`` (created by ``setup_agent_wallet()``)

The key never leaves the host — only EIP-712 signatures travel over the wire.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Iterator, List, Optional

import httpx
import litellm
from litellm import CustomLLM
from litellm.types.utils import GenericStreamingChunk

from blockrun_llm.types import APIError as BlockRunAPIError
from blockrun_llm.types import ChatCompletionChunk, ChatUsage

from blockrun_litellm import _adapter


# Provider name surfaced to LiteLLM exception classes so the router knows
# this is BlockRun-routed when it logs / retries / falls back.
_LITELLM_PROVIDER = "blockrun"


def _translate_to_litellm(exc: Exception, model: str) -> Optional[Exception]:
    """Map a transient BlockRun / network error to LiteLLM's retriable
    exception hierarchy so the router's own fallback machinery kicks in.

    Returns the LiteLLM-compatible exception to raise, or ``None`` if the
    exception is not transient (caller should re-raise as-is).

    Mappings:
      * ``httpx.TimeoutException``        → ``litellm.Timeout``
      * ``httpx.NetworkError``            → ``litellm.APIConnectionError``
      * ``APIError`` 500                  → ``litellm.InternalServerError``
      * ``APIError`` 502 / 504            → ``litellm.APIConnectionError``
      * ``APIError`` 503                  → ``litellm.ServiceUnavailableError``
      * ``APIError`` 429                  → ``litellm.RateLimitError``
    """
    if isinstance(exc, httpx.TimeoutException):
        return litellm.Timeout(
            message=f"BlockRun upstream timed out: {exc}",
            model=model,
            llm_provider=_LITELLM_PROVIDER,
        )
    if isinstance(exc, httpx.NetworkError):
        return litellm.APIConnectionError(
            message=f"BlockRun upstream network error: {exc}",
            model=model,
            llm_provider=_LITELLM_PROVIDER,
        )
    if isinstance(exc, BlockRunAPIError):
        status = getattr(exc, "status_code", 0)
        if status == 429:
            return litellm.RateLimitError(
                message=str(exc), model=model, llm_provider=_LITELLM_PROVIDER
            )
        if status == 500:
            return litellm.InternalServerError(
                message=str(exc), model=model, llm_provider=_LITELLM_PROVIDER
            )
        if status == 503:
            return litellm.ServiceUnavailableError(
                message=str(exc), model=model, llm_provider=_LITELLM_PROVIDER
            )
        if status in (502, 504):
            return litellm.APIConnectionError(
                message=str(exc), model=model, llm_provider=_LITELLM_PROVIDER
            )
    return None


# LiteLLM passes the provider-stripped model name *and* an "optional_params"
# dict containing the OpenAI-style params (temperature, max_tokens, ...).
# It also forwards ``api_base`` / ``api_key`` from the call site, which we
# repurpose: ``api_base`` → BlockRun ``api_url``, ``api_key`` → wallet key.
_OPTIONAL_KEYS = (
    "max_tokens",
    "temperature",
    "top_p",
    "tools",
    "tool_choice",
    "stream",
)


def _collect_openai_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    optional = kwargs.get("optional_params") or {}
    for k in _OPTIONAL_KEYS:
        if k in optional and optional[k] is not None:
            out[k] = optional[k]
        elif k in kwargs and kwargs[k] is not None:
            out[k] = kwargs[k]
    # BlockRun-specific extras may be passed via ``litellm_params`` /
    # ``optional_params``. Surface them if present.
    for k in ("search", "search_parameters", "fallback_models"):
        if k in optional and optional[k] is not None:
            out[k] = optional[k]
    return out


def _build_response(model: str, payload: Dict[str, Any]) -> litellm.ModelResponse:
    """Wrap a BlockRun-dumped dict into a ``litellm.ModelResponse``.

    Native-fingerprint passthrough: the gateway returns the upstream
    provider's response verbatim, so the dumped dict carries the real
    relay-detection signals — ``system_fingerprint`` (GPT ``fp_*``),
    ``service_tier``, ``usage.prompt_tokens_details`` /
    ``usage.cache_read_input_tokens`` / ``cache_creation_input_tokens``,
    and per-message ``reasoning_content``. ``litellm.ModelResponse``
    preserves all of these as first-class or extra fields, so a relay
    detector (e.g. cctest.ai) sees a genuine direct upstream call. The
    regression suite in ``tests/test_fingerprint.py`` locks this in so a
    future LiteLLM / SDK bump can't silently strip them.
    """
    # The payload from blockrun-llm already includes the model field. We pop
    # it to avoid the duplicate-kwarg TypeError, then re-inject the
    # caller-supplied id so callers see the model they actually asked for
    # (BlockRun may rewrite e.g. "openai/gpt-5.5" → bare "gpt-5.5").
    payload = dict(payload)
    blockrun_meta = payload.pop("_blockrun", None)
    payload.pop("model", None)
    response = litellm.ModelResponse(**payload, model=model)
    _attach_real_cost(response, blockrun_meta)
    return response


def _attach_real_cost(response: litellm.ModelResponse, meta: Optional[Dict[str, Any]]) -> None:
    """Override LiteLLM's token×list-price estimate with BlockRun's real x402
    charge, and expose the real consumption to callers.

    LiteLLM reads ``_hidden_params["response_cost"]`` when present instead of
    re-estimating from its price table, so setting it makes ``response_cost``
    (and the proxy's spend tracking) reflect the actual wallet deduction. We
    also stash ``blockrun_cost_usd`` / ``blockrun_settlement`` as explicit,
    unambiguous fields so a caller can read the real spend alongside ``usage``
    even if a future LiteLLM version reclaims ``response_cost``.
    """
    if not meta:
        return
    cost = meta.get("cost_usd")
    if cost is None:
        return
    hidden = getattr(response, "_hidden_params", None)
    if not isinstance(hidden, dict):
        hidden = {}
        response._hidden_params = hidden
    cost = float(cost)
    hidden["response_cost"] = cost
    hidden["blockrun_cost_usd"] = cost
    if meta.get("settlement"):
        hidden["blockrun_settlement"] = meta["settlement"]


# Header key LiteLLM's streaming cost path reads off the assembled response's
# ``_hidden_params`` (``get_response_cost_from_hidden_params``) when deciding
# ``response_cost``. Setting it makes streamed spend reflect the real x402
# charge instead of a token×list-price estimate.
_COST_HEADER = "llm_provider-x-litellm-response-cost"


def _cost_hidden_params(cost_usd: float) -> Dict[str, Any]:
    """The ``_hidden_params`` payload that carries the real x402 charge through
    LiteLLM's stream aggregation.

    Why ``_hidden_params`` and not a plain field: ``stream_chunk_builder``
    copies a chunk's ``_hidden_params`` onto the assembled response verbatim
    (``update_model_response_with_hidden_params``) but drops arbitrary
    ``provider_specific_fields`` and never recomputes a provider charge. So the
    real cost only survives aggregation if it rides inside ``_hidden_params``.

    Two consumers read it off the assembled response:
      * ``additional_headers[_COST_HEADER]`` → LiteLLM's ``response_cost`` (spend
        / ``max_budget``), and
      * ``blockrun_cost_usd`` → our JSONL audit (``cost_source='blockrun_x402'``).
    Mirrors :func:`_attach_real_cost` on the non-streaming path.
    """
    cost = float(cost_usd)
    return {
        "response_cost": cost,
        "blockrun_cost_usd": cost,
        "additional_headers": {_COST_HEADER: cost},
    }


def _inject_real_cost(gchunk: Dict[str, Any], cost_usd: float) -> None:
    """Thread the real x402 charge onto a ``GenericStreamingChunk`` in place.

    LiteLLM's custom-provider stream handler ``setattr``s every
    ``provider_specific_fields`` key onto the per-chunk ``ModelResponseStream``
    — so stashing ``_hidden_params`` there lands the cost in the chunk's
    ``_hidden_params``, which ``stream_chunk_builder`` then copies onto the
    assembled response. Existing fields (native fingerprint, reasoning_content)
    are preserved.
    """
    psf = gchunk.get("provider_specific_fields") or {}
    gchunk["provider_specific_fields"] = {
        **psf,
        "_hidden_params": _cost_hidden_params(cost_usd),
    }


def _native_extras(chunk: ChatCompletionChunk) -> Dict[str, Any]:
    """Collect the upstream-native fingerprint fields carried on a stream
    chunk (e.g. ``system_fingerprint``, ``service_tier``) so they survive
    the lossy :class:`GenericStreamingChunk` contract.

    ``ChatCompletionChunk`` is declared ``extra = "allow"`` in blockrun-llm,
    so any top-level field the gateway forwards that isn't part of the
    OpenAI chunk schema lands in ``model_extra``. We surface those plus the
    usage-level cache/details extras through ``provider_specific_fields`` so
    in-process LiteLLM streaming callers can still read the genuine signals.
    """
    extras: Dict[str, Any] = dict(chunk.model_extra or {})
    # The SDK attaches the per-call x402 charge as ``chunk.cost_usd`` (extra),
    # which lands in ``model_extra``. It is surfaced deliberately through
    # ``_inject_real_cost`` (-> _hidden_params), so drop it here to avoid
    # leaking a stray ``cost_usd`` field into ``provider_specific_fields``.
    extras.pop("cost_usd", None)
    if chunk.usage is not None:
        # NB: ``prompt_tokens_details`` / ``completion_tokens_details`` are NOT
        # declared fields on ChatUsage — they are pydantic extras (``extra =
        # "allow"``), so they already live in ``model_extra`` and are picked up
        # by the dict() copy below. Never read them by attribute: access to an
        # absent pydantic extra raises AttributeError, it does not return None.
        usage_details: Dict[str, Any] = dict(chunk.usage.model_extra or {})
        if chunk.usage.cache_read_input_tokens is not None:
            usage_details["cache_read_input_tokens"] = chunk.usage.cache_read_input_tokens
        if chunk.usage.cache_creation_input_tokens is not None:
            usage_details["cache_creation_input_tokens"] = chunk.usage.cache_creation_input_tokens
        if usage_details:
            extras.setdefault("usage_details", {}).update(usage_details)
    return extras


def _usage_dict(usage: ChatUsage) -> Dict[str, Any]:
    """Map a :class:`ChatUsage` → the plain usage dict LiteLLM's streaming
    handler feeds into ``litellm.Usage``.

    Carries the token-detail passthrough: ``prompt_tokens_details`` /
    ``completion_tokens_details`` (pydantic extras — read from ``model_extra``,
    see :func:`_native_extras`) and the Anthropic cache split. The gateway
    folds cache reads INTO ``prompt_tokens`` (matching LiteLLM's own
    convention), so forwarding ``cache_read_input_tokens`` lets LiteLLM apply
    the cache-read discount instead of billing the full prompt rate.

    Used by BOTH ``_to_generic_chunk`` branches: the choice-less
    ``include_usage`` final frame AND a choices-bearing chunk that carries
    usage inline. Keeping one builder means the two paths can never disagree
    about which detail fields survive.
    """
    out: Dict[str, Any] = {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }
    extra = usage.model_extra or {}
    for key in ("prompt_tokens_details", "completion_tokens_details"):
        if extra.get(key) is not None:
            out[key] = extra[key]
    if usage.cache_read_input_tokens is not None:
        out["cache_read_input_tokens"] = usage.cache_read_input_tokens
    if usage.cache_creation_input_tokens is not None:
        out["cache_creation_input_tokens"] = usage.cache_creation_input_tokens
    return out


def _to_generic_chunk(chunk: ChatCompletionChunk) -> GenericStreamingChunk:
    """Map a BlockRun :class:`ChatCompletionChunk` → LiteLLM
    :class:`GenericStreamingChunk` (a ``TypedDict``).

    BlockRun's chunk schema is the OpenAI ``chat.completion.chunk`` schema.
    LiteLLM's ``GenericStreamingChunk`` is a simpler, provider-agnostic
    structure that the streaming handler stitches into a final response.
    """
    # Native fingerprint fields ride on every chunk (OpenAI sends
    # ``system_fingerprint`` per chunk), so collect them regardless of whether
    # this chunk carries a choice, and surface via ``provider_specific_fields``.
    extras = _native_extras(chunk)
    provider_specific_fields = extras or None

    if not chunk.choices:
        # A choice-less chunk is the OpenAI `include_usage` final frame
        # (choices:[] + usage). Forward its real token counts so LiteLLM bills
        # off them instead of re-estimating the prompt with its own tokenizer
        # (tiktoken drifts ~37% vs the gateway's real upstream count). Older
        # gateways that never send this frame still hit the usage=None path.
        usage = _usage_dict(chunk.usage) if chunk.usage is not None else None
        # NOTE: deliberately do NOT set tool_use here. This is the post-finish
        # usage frame; adding the key changes LiteLLM's CustomStreamWrapper
        # post-finish guard and lets the frame survive even without
        # provider_specific_fields, breaking the key contract that
        # test_usage_dropped_without_provider_specific_fields_key locks in.
        return GenericStreamingChunk(
            text="",
            is_finished=False,
            finish_reason="",
            usage=usage,
            index=0,
            provider_specific_fields=provider_specific_fields,
        )

    choice = chunk.choices[0]
    text = choice.delta.content or ""
    finish_reason = choice.finish_reason or ""

    # Reasoning-model output (thinking-enabled Claude, DeepSeek R1, GLM thinking)
    # rides on the delta as ``reasoning_content``. ``GenericStreamingChunk`` has
    # no reasoning field and LiteLLM's custom-provider stream handler never
    # promotes it onto ``delta.reasoning_content``, so route it through
    # ``provider_specific_fields`` — the only channel a CustomLLM provider has to
    # a live streaming consumer (readable at
    # ``delta.provider_specific_fields["reasoning_content"]``). Without this it is
    # dropped on the floor for every streamed call.
    reasoning = getattr(choice.delta, "reasoning_content", None)
    if reasoning:
        extras = {**(extras or {}), "reasoning_content": reasoning}
        provider_specific_fields = extras

    # NB: tool calls (``delta.tool_calls``) are intentionally NOT handled here.
    # They need to be split into separate name/arguments frames for LiteLLM's
    # Anthropic adapter — see _iter_stream_chunks(), which wraps this function.

    # BlockRun's per-chunk usage is rarely populated; LiteLLM tolerates None.
    # When a provider DOES attach usage to a choices-bearing chunk (instead of
    # a separate include_usage frame), it must carry the same detail fields as
    # the choice-less branch — a bare three-count dict here would contradict
    # the full detail riding on provider_specific_fields, and LiteLLM's chunk
    # builder resets prompt_tokens_details unconditionally from the LAST
    # usage-bearing chunk (streaming_chunk_builder_utils), so an asymmetric
    # frame can null out detail a previous frame delivered.
    usage = _usage_dict(chunk.usage) if chunk.usage is not None else None

    return GenericStreamingChunk(
        text=text,
        is_finished=bool(finish_reason),
        finish_reason=finish_reason,
        usage=usage,
        index=choice.index,
        provider_specific_fields=provider_specific_fields,
    )


def _tool_use_frame(*, id, name, arguments, index):
    """A single-object ``tool_use`` payload (bedrock-style shape).

    LiteLLM's custom-provider → Anthropic /v1/messages adapter is a state
    machine: a frame carrying a function *name* opens a ``tool_use`` content
    block (``content_block_start``); a later frame carrying only *arguments*
    streams them as ``input_json_delta``. It does NOT handle name+arguments in
    one frame (the args are silently dropped → tool_use block with empty input).
    """
    return {
        "text": "",
        "is_finished": False,
        "finish_reason": "",
        "usage": None,
        "index": index,
        "provider_specific_fields": None,
        "tool_use": {
            "id": id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
            "index": index,
        },
    }


def _iter_stream_chunks(chunk, state=None):
    """Expand one SDK chunk into the GenericStreamingChunk(s) LiteLLM consumes.

    Plain text / usage / finish chunks pass straight through. A chunk carrying
    tool calls is SPLIT, per tool call, into a name frame (opens the Anthropic
    ``tool_use`` block) followed by an arguments frame (streams ``input_json_
    delta``). BlockRun's SDK delivers each tool call complete (id+name+full
    arguments) in one chunk — the opposite of OpenAI's incremental tool
    streaming that LiteLLM's adapter expects — so we re-shape it here. Without
    the split, agentic clients (Claude Code via /v1/messages) get a tool_use
    block with no input and can't act.

    ``state`` is a per-stream mutable dict carrying the running tool-block index
    counter. BlockRun's ``ToolCall`` has NO per-call ``index`` field, and a model
    can emit several parallel tool calls (in one chunk or across several). Each
    must land on a DISTINCT Anthropic content-block index or the adapter collapses
    them onto one block and drops all but the last. We therefore assign a
    stream-scoped monotonic index (0, 1, 2, …) — the 0-based tool-array position
    LiteLLM's adapter expects. Callers must pass one fresh dict per stream.
    """
    if state is None:
        state = {}

    tool_calls = None
    if chunk.choices:
        tool_calls = getattr(chunk.choices[0].delta, "tool_calls", None)

    if not tool_calls:
        yield _to_generic_chunk(chunk)
        return

    for tc in tool_calls:
        fn = getattr(tc, "function", None)
        index = state.get("tool_index", 0)
        state["tool_index"] = index + 1
        # Frame 1 — name: opens the tool_use content block.
        yield _tool_use_frame(
            id=getattr(tc, "id", None),
            name=getattr(fn, "name", None) if fn else None,
            arguments="",
            index=index,
        )
        # Frame 2 — arguments: streamed as input_json_delta. Emit even when the
        # SDK gives no args (empty string is a harmless no-op delta).
        yield _tool_use_frame(
            id=None,
            name=None,
            arguments=(getattr(fn, "arguments", None) or "") if fn else "",
            index=index,
        )

    # A tool-call chunk may also carry a finish_reason / usage frame alongside
    # the calls; forward the plain conversion too so those aren't lost. (When it
    # carries only tool calls, this is a benign empty text frame.)
    if chunk.choices[0].finish_reason or chunk.usage is not None:
        yield _to_generic_chunk(chunk)


class BlockRunLLM(CustomLLM):
    """LiteLLM custom provider that routes through BlockRun's x402 gateway."""

    # NOTE: signature must match litellm.CustomLLM exactly. LiteLLM passes a
    # *lot* of kwargs; we only consume what we recognize.
    def completion(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> litellm.ModelResponse:
        openai_kwargs = _collect_openai_kwargs(kwargs)
        try:
            payload = _adapter.chat_completion_sync(
                model=model,
                messages=messages,
                api_url=api_base,
                private_key=api_key,
                **openai_kwargs,
            )
        except Exception as exc:
            translated = _translate_to_litellm(exc, model)
            if translated is not None:
                raise translated from exc
            raise
        return _build_response(model, payload)

    async def acompletion(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> litellm.ModelResponse:
        openai_kwargs = _collect_openai_kwargs(kwargs)
        try:
            payload = await _adapter.chat_completion_async(
                model=model,
                messages=messages,
                api_url=api_base,
                private_key=api_key,
                **openai_kwargs,
            )
        except Exception as exc:
            translated = _translate_to_litellm(exc, model)
            if translated is not None:
                raise translated from exc
            raise
        return _build_response(model, payload)

    # ----- Streaming -------------------------------------------------------

    def streaming(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> Iterator[GenericStreamingChunk]:
        """Sync streaming. Yields :class:`GenericStreamingChunk` per delta.

        Errors from the SDK during stream setup or mid-flight are
        translated to LiteLLM's retriable exception types via
        :func:`_translate_to_litellm` so the router's own fallback
        machinery can pick the next provider on transient upstream issues.
        """
        openai_kwargs = _collect_openai_kwargs(kwargs)
        # ``stream`` is implicit at this entrypoint; drop it so the SDK doesn't
        # see a duplicate kwarg.
        openai_kwargs.pop("stream", None)
        stream_state: Dict[str, Any] = {}
        try:
            for chunk in _adapter.chat_completion_stream_sync(
                model=model,
                messages=messages,
                api_url=api_base,
                private_key=api_key,
                **openai_kwargs,
            ):
                # Per-call x402 charge the SDK attaches to each chunk (race-free,
                # vs the shared client._last_call_cost). ``None`` on older SDKs
                # that don't attach it -> estimate fallback, no injection.
                cost = getattr(chunk, "cost_usd", None)
                for gchunk in _iter_stream_chunks(chunk, stream_state):
                    if cost is not None:
                        _inject_real_cost(gchunk, cost)
                    yield gchunk
        except Exception as exc:
            translated = _translate_to_litellm(exc, model)
            if translated is not None:
                raise translated from exc
            raise

    async def astreaming(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncIterator[GenericStreamingChunk]:
        """Async streaming. Same semantics as :meth:`streaming`."""
        openai_kwargs = _collect_openai_kwargs(kwargs)
        openai_kwargs.pop("stream", None)
        stream_state: Dict[str, Any] = {}
        try:
            async for chunk in _adapter.chat_completion_stream_async(
                model=model,
                messages=messages,
                api_url=api_base,
                private_key=api_key,
                **openai_kwargs,
            ):
                cost = getattr(chunk, "cost_usd", None)
                for gchunk in _iter_stream_chunks(chunk, stream_state):
                    if cost is not None:
                        _inject_real_cost(gchunk, cost)
                    yield gchunk
        except Exception as exc:
            translated = _translate_to_litellm(exc, model)
            if translated is not None:
                raise translated from exc
            raise


_PROVIDER_NAME = "blockrun"
_handler: Optional[BlockRunLLM] = None


def register() -> BlockRunLLM:
    """
    Register the ``blockrun`` provider with LiteLLM.

    Idempotent — calling twice does not duplicate the entry.

    Returns the singleton handler instance so callers can swap in a custom
    subclass via ``litellm.custom_provider_map`` if they need to.
    """
    global _handler
    if _handler is None:
        _handler = BlockRunLLM()

    existing = getattr(litellm, "custom_provider_map", None) or []
    for entry in existing:
        if isinstance(entry, dict) and entry.get("provider") == _PROVIDER_NAME:
            return _handler

    existing.append({"provider": _PROVIDER_NAME, "custom_handler": _handler})
    litellm.custom_provider_map = existing
    return _handler
