"""Native-fingerprint passthrough regression tests.

BlockRun's gateway returns the upstream provider's response verbatim, so the
relay-detection signals a tool like cctest.ai inspects — GPT
``system_fingerprint`` (``fp_*``), ``service_tier``, the ``usage`` cache /
token-detail breakdown, and per-message ``reasoning_content`` — must survive
*both* litellm integration modes:

  * the in-process ``CustomLLM`` provider (``_build_response`` /
    ``_to_generic_chunk``), and
  * the OpenAI-shaped dict the sidecar proxy returns verbatim
    (``ChatResponse.model_dump``).

These tests lock that in so a future LiteLLM or ``blockrun-llm`` bump can't
silently strip the fingerprint and turn a genuine direct upstream call into
something a relay detector flags as proxied.
"""

from __future__ import annotations

import litellm

from blockrun_llm.types import (
    ChatChoice,
    ChatCompletionChunk,
    ChatChunkChoice,
    ChatChunkDelta,
    ChatMessage,
    ChatResponse,
    ChatUsage,
)

from blockrun_litellm.provider import _build_response, _native_extras, _to_generic_chunk


# ---------------------------------------------------------------------------
# Builders — a ChatResponse / chunk that carries the upstream-native extras
# the gateway forwards verbatim (all stored via ``extra = "allow"``).
# ---------------------------------------------------------------------------

def _fingerprinted_response() -> ChatResponse:
    return ChatResponse(
        id="chatcmpl-fp-1",
        object="chat.completion",
        created=1_700_000_000,
        model="openai/gpt-5.5",
        system_fingerprint="fp_abc123",          # GPT relay-detection signal
        service_tier="default",                   # OpenAI extra
        choices=[
            ChatChoice(
                index=0,
                message=ChatMessage(
                    role="assistant",
                    content="hello",
                    reasoning_content="because reasons",  # reasoning-model signal
                ),
                finish_reason="stop",
            )
        ],
        usage=ChatUsage(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            cache_read_input_tokens=4,
            # OpenAI-native nested breakdown (extra → must survive)
            prompt_tokens_details={"cached_tokens": 4},
            completion_tokens_details={"reasoning_tokens": 3},
        ),
    )


def _fingerprinted_chunk(*, with_choice: bool = True) -> ChatCompletionChunk:
    choices = []
    if with_choice:
        choices = [
            ChatChunkChoice(
                index=0,
                delta=ChatChunkDelta(role="assistant", content="hi"),
                finish_reason=None,
            )
        ]
    return ChatCompletionChunk(
        id="chatcmpl-fp-1",
        object="chat.completion.chunk",
        created=1_700_000_000,
        model="openai/gpt-5.5",
        system_fingerprint="fp_abc123",
        service_tier="default",
        choices=choices,
    )


# ---------------------------------------------------------------------------
# Provider (non-streaming) — ModelResponse must keep the fingerprint fields.
# ---------------------------------------------------------------------------

def test_build_response_preserves_system_fingerprint() -> None:
    resp = _build_response("openai/gpt-5.5", _fingerprinted_response().model_dump(exclude_none=True))
    assert isinstance(resp, litellm.ModelResponse)
    assert resp.system_fingerprint == "fp_abc123"


def test_build_response_preserves_top_level_extras() -> None:
    dumped = _build_response(
        "openai/gpt-5.5", _fingerprinted_response().model_dump(exclude_none=True)
    ).model_dump()
    assert dumped.get("service_tier") == "default"


def test_build_response_preserves_usage_cache_details() -> None:
    dumped = _build_response(
        "openai/gpt-5.5", _fingerprinted_response().model_dump(exclude_none=True)
    ).model_dump()
    usage = dumped["usage"]
    assert usage["cache_read_input_tokens"] == 4
    assert usage["prompt_tokens_details"]["cached_tokens"] == 4
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 3


def test_build_response_preserves_reasoning_content() -> None:
    resp = _build_response(
        "openai/gpt-5.5", _fingerprinted_response().model_dump(exclude_none=True)
    )
    assert resp.choices[0].message.reasoning_content == "because reasons"


# ---------------------------------------------------------------------------
# Provider (streaming) — GenericStreamingChunk is lossy, so the fingerprint
# rides on ``provider_specific_fields``.
# ---------------------------------------------------------------------------

def test_native_extras_collects_chunk_fingerprint() -> None:
    extras = _native_extras(_fingerprinted_chunk())
    assert extras["system_fingerprint"] == "fp_abc123"
    assert extras["service_tier"] == "default"


def test_stream_chunk_carries_fingerprint_with_choice() -> None:
    gchunk = _to_generic_chunk(_fingerprinted_chunk(with_choice=True))
    assert gchunk["text"] == "hi"
    psf = gchunk["provider_specific_fields"]
    assert psf is not None
    assert psf["system_fingerprint"] == "fp_abc123"


def test_stream_heartbeat_chunk_still_carries_fingerprint() -> None:
    """A choice-less heartbeat chunk must not drop the fingerprint."""
    gchunk = _to_generic_chunk(_fingerprinted_chunk(with_choice=False))
    assert gchunk["text"] == ""
    psf = gchunk["provider_specific_fields"]
    assert psf is not None
    assert psf["system_fingerprint"] == "fp_abc123"


def test_stream_chunk_without_extras_has_none_psf() -> None:
    plain = ChatCompletionChunk(
        id="c",
        object="chat.completion.chunk",
        created=1,
        model="openai/gpt-5.5",
        choices=[ChatChunkChoice(index=0, delta=ChatChunkDelta(content="x"), finish_reason=None)],
    )
    assert _to_generic_chunk(plain)["provider_specific_fields"] is None


# ---------------------------------------------------------------------------
# Proxy (sidecar) — the verbatim dict the route returns keeps the fingerprint.
# ---------------------------------------------------------------------------

def test_proxy_dump_preserves_fingerprint() -> None:
    """The sidecar returns ``response.model_dump(exclude_none=True)`` straight
    to the client; assert that dump still carries the native signals."""
    dumped = _fingerprinted_response().model_dump(exclude_none=True)
    assert dumped["system_fingerprint"] == "fp_abc123"
    assert dumped["service_tier"] == "default"
    assert dumped["usage"]["cache_read_input_tokens"] == 4
    assert dumped["usage"]["prompt_tokens_details"]["cached_tokens"] == 4
    assert dumped["usage"]["completion_tokens_details"]["reasoning_tokens"] == 3
    assert dumped["choices"][0]["message"]["reasoning_content"] == "because reasons"
