"""End-of-stream usage frame regression tests.

The gateway (with ``stream_options.include_usage``) emits a final SSE frame
shaped ``{"choices": [], "usage": {...}}`` *after* the finish-reason chunk.
``_to_generic_chunk`` must forward those real token counts so LiteLLM bills
off them instead of re-estimating with its own tokenizer (tiktoken drifts
~37% vs the gateway's real upstream count).

The forwarding only works end-to-end because of a subtle LiteLLM contract:
once ``CustomStreamWrapper`` has seen a finish reason, it drops any further
chunk whose dict lacks the ``provider_specific_fields`` key
(``litellm/litellm_core_utils/streaming_handler.py``, custom-provider
branch)::

    if self.received_finish_reason is not None:
        if "provider_specific_fields" not in chunk:
            raise StopIteration

``GenericStreamingChunk`` is a TypedDict, so the key only exists if
``_to_generic_chunk`` passes it explicitly — which it does (the native
fingerprint passthrough relies on it too). These tests lock both halves in:
the usage values themselves, and the key-presence contract that lets the
usage frame survive past the finish chunk.
"""

from __future__ import annotations

import time

import litellm
import pytest

from blockrun_llm.types import (
    ChatChunkChoice,
    ChatChunkDelta,
    ChatCompletionChunk,
    ChatUsage,
)

from blockrun_litellm.provider import _to_generic_chunk, register


GATEWAY_USAGE = ChatUsage(
    prompt_tokens=100,
    completion_tokens=20,
    total_tokens=120,
    cache_read_input_tokens=40,
    completion_tokens_details={"reasoning_tokens": 12},
)


def _chunk(
    choices: list[ChatChunkChoice], usage: ChatUsage | None = None
) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="chatcmpl-usage-1",
        object="chat.completion.chunk",
        created=1_700_000_000,
        model="openai/gpt-5.5",
        choices=choices,
        usage=usage,
    )


def _usage_frame() -> ChatCompletionChunk:
    """The OpenAI ``include_usage`` final frame: choices:[] + usage."""
    return _chunk([], usage=GATEWAY_USAGE)


# ---------------------------------------------------------------------------
# Unit: the choice-less usage frame is forwarded, not dropped.
# ---------------------------------------------------------------------------

def test_usage_frame_forwards_token_counts() -> None:
    gchunk = _to_generic_chunk(_usage_frame())
    assert gchunk["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "completion_tokens_details": {"reasoning_tokens": 12},
        "cache_read_input_tokens": 40,
    }
    # The frame must not terminate or mutate the stream state.
    assert gchunk["text"] == ""
    assert gchunk["is_finished"] is False
    assert gchunk["finish_reason"] == ""


def test_choice_less_chunk_without_usage_stays_none() -> None:
    """Older gateways never send the frame — heartbeat path unchanged."""
    gchunk = _to_generic_chunk(_chunk([]))
    assert gchunk["usage"] is None


def test_choice_less_chunk_carries_provider_specific_fields_key() -> None:
    """The usage frame arrives AFTER the finish-reason chunk. LiteLLM's
    CustomStreamWrapper raises StopIteration on any post-finish chunk whose
    dict lacks the ``provider_specific_fields`` key — dropping the usage
    before it is ever read. The key (even with a None value) is what keeps
    the frame alive, so its presence is a hard contract."""
    for gchunk in (_to_generic_chunk(_usage_frame()), _to_generic_chunk(_chunk([]))):
        assert "provider_specific_fields" in gchunk


# ---------------------------------------------------------------------------
# End-to-end: content → finish → usage frame through LiteLLM's real
# CustomStreamWrapper. The built response must carry the gateway's counts.
# ---------------------------------------------------------------------------

def test_usage_survives_custom_stream_wrapper() -> None:
    from litellm.litellm_core_utils.litellm_logging import Logging
    from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
    from litellm.utils import custom_llm_setup

    register()
    custom_llm_setup()  # ensure "blockrun" is in litellm._custom_providers

    raw_stream = [
        _chunk(
            [
                ChatChunkChoice(
                    index=0,
                    delta=ChatChunkDelta(role="assistant", content="Hello"),
                    finish_reason=None,
                )
            ]
        ),
        _chunk(
            [
                ChatChunkChoice(
                    index=0, delta=ChatChunkDelta(), finish_reason="stop"
                )
            ]
        ),
        _usage_frame(),
    ]
    messages = [{"role": "user", "content": "hi"}]
    logging_obj = Logging(
        model="blockrun/openai/gpt-5.5",
        messages=messages,
        stream=True,
        call_type="completion",
        litellm_call_id="test-usage-frame",
        start_time=time.time(),
        function_id="test-usage-frame",
    )
    wrapper = CustomStreamWrapper(
        completion_stream=iter(_to_generic_chunk(c) for c in raw_stream),
        model="blockrun/openai/gpt-5.5",
        logging_obj=logging_obj,
        custom_llm_provider="blockrun",
    )

    chunks = list(wrapper)
    assert any(c.choices[0].delta.content == "Hello" for c in chunks)

    built = litellm.stream_chunk_builder(chunks=wrapper.chunks, messages=messages)
    assert built is not None
    assert built.usage.prompt_tokens == 100
    assert built.usage.completion_tokens == 20
    assert built.usage.total_tokens == 120
    dumped_usage = built.usage.model_dump()
    assert dumped_usage["completion_tokens_details"]["reasoning_tokens"] == 12
    assert dumped_usage["cache_read_input_tokens"] == 40


def test_usage_dropped_without_provider_specific_fields_key() -> None:
    """Documents WHY the key contract matters: strip the key from the usage
    frame (simulating a 'simplified' choice-less return). Older LiteLLM falls
    back to tokenizer estimates; newer releases accept the usage frame without
    the key, in which case this historical compatibility guard is unnecessary."""
    from litellm.litellm_core_utils.litellm_logging import Logging
    from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
    from litellm.utils import custom_llm_setup

    register()
    custom_llm_setup()

    raw_stream = [
        _chunk(
            [
                ChatChunkChoice(
                    index=0,
                    delta=ChatChunkDelta(role="assistant", content="Hello"),
                    finish_reason=None,
                )
            ]
        ),
        _chunk(
            [
                ChatChunkChoice(
                    index=0, delta=ChatChunkDelta(), finish_reason="stop"
                )
            ]
        ),
        _usage_frame(),
    ]

    def _stripped(c: ChatCompletionChunk):
        g = dict(_to_generic_chunk(c))
        if not c.choices:
            g.pop("provider_specific_fields", None)
        return g

    messages = [{"role": "user", "content": "hi"}]
    logging_obj = Logging(
        model="blockrun/openai/gpt-5.5",
        messages=messages,
        stream=True,
        call_type="completion",
        litellm_call_id="test-usage-frame-stripped",
        start_time=time.time(),
        function_id="test-usage-frame-stripped",
    )
    wrapper = CustomStreamWrapper(
        completion_stream=iter(_stripped(c) for c in raw_stream),
        model="blockrun/openai/gpt-5.5",
        logging_obj=logging_obj,
        custom_llm_provider="blockrun",
    )
    list(wrapper)

    built = litellm.stream_chunk_builder(chunks=wrapper.chunks, messages=messages)
    got = (
        built.usage.prompt_tokens if built is not None and built.usage else None
    )
    if got == 100:
        pytest.skip(
            "This LiteLLM release accepts post-finish usage frames without "
            "provider_specific_fields; the compatibility guard is no longer required."
        )
    assert got != 100, (
        "LiteLLM consumed the usage frame even without the "
        "provider_specific_fields key — its post-finish guard changed, so "
        "_to_generic_chunk may no longer need to emit the key. Re-check "
        "streaming_handler.py before relaxing the contract."
    )


# ---------------------------------------------------------------------------
# Detail-field passthrough: both usage branches, and the absent-details case.
# ---------------------------------------------------------------------------

def test_usage_frame_without_details_does_not_crash() -> None:
    """Regression: prompt_tokens_details / completion_tokens_details are
    pydantic EXTRAS on ChatUsage (extra="allow"), not declared fields.
    Attribute access on an absent extra raises AttributeError — it does not
    return None. A gateway usage frame carrying only the three counts (or only
    the Anthropic cache fields) must convert cleanly; before the model_extra
    fix this raised, LiteLLM wrapped it into APIConnectionError, and clients
    retried an already-settled stream."""
    bare = ChatUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120)
    gchunk = _to_generic_chunk(_chunk([], usage=bare))
    assert gchunk["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
    }

    anthropic_shape = ChatUsage(
        prompt_tokens=2160,
        completion_tokens=20,
        total_tokens=2180,
        cache_read_input_tokens=2048,
        cache_creation_input_tokens=100,
    )
    gchunk = _to_generic_chunk(_chunk([], usage=anthropic_shape))
    assert gchunk["usage"]["cache_read_input_tokens"] == 2048
    assert gchunk["usage"]["cache_creation_input_tokens"] == 100
    assert "prompt_tokens_details" not in gchunk["usage"]


def test_choices_bearing_chunk_carries_same_usage_details() -> None:
    """A provider that attaches usage to a choices-bearing chunk (instead of a
    separate include_usage frame) must emit the SAME detail fields as the
    choice-less branch. LiteLLM's chunk builder takes prompt_tokens_details
    unconditionally from the LAST usage-bearing chunk, so an asymmetric bare
    dict here could null out detail delivered earlier."""
    chunk = _chunk(
        [
            ChatChunkChoice(
                index=0, delta=ChatChunkDelta(), finish_reason="stop"
            )
        ],
        usage=GATEWAY_USAGE,
    )
    gchunk = _to_generic_chunk(chunk)
    assert gchunk["usage"]["completion_tokens_details"] == {"reasoning_tokens": 12}
    assert gchunk["usage"]["cache_read_input_tokens"] == 40
    assert gchunk["is_finished"] is True
