"""Responses API usage conversion — token detail passthrough.

The Responses spec (``openai.types.responses.ResponseUsage``) makes BOTH
``input_tokens_details`` and ``output_tokens_details`` REQUIRED, as are
``cached_tokens`` / ``reasoning_tokens`` inside them. These tests lock in:

- details present → carried through (spec keys only, nothing leaked)
- details absent  → blocks still emitted, zero-defaulted (openai-python
  crashes on a usage object missing them: ``'NoneType' has no attribute
  'cached_tokens'``)
- Anthropic shape → ``cache_read_input_tokens`` (top-level, no
  prompt_tokens_details) lands in ``cached_tokens``
- garbage extras  → non-dict details / non-int counts coerced to 0, and
  chat-only keys (``audio_tokens`` …) are NOT leaked into the Responses
  surface
- the SSE stream path emits the same detail on ``response.completed``
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from unittest.mock import patch  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from blockrun_llm.types import ChatCompletionChunk  # noqa: E402

import blockrun_litellm.proxy as P  # noqa: E402
from blockrun_litellm.proxy import _chat_payload_to_response, _usage_to_responses  # noqa: E402

client = TestClient(P.app)


def _payload(usage: dict) -> dict:
    return {
        "id": "chatcmpl-1",
        "model": "openai/gpt-5.5",
        "choices": [{"message": {"content": "ok"}}],
        "usage": usage,
    }


def test_chat_to_responses_preserves_token_details() -> None:
    response = _chat_payload_to_response(
        _payload(
            {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
                "prompt_tokens_details": {"cached_tokens": 4},
                "completion_tokens_details": {"reasoning_tokens": 12},
            }
        ),
        "openai/gpt-5.5",
    )

    assert response["usage"] == {
        "input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 30,
        "input_tokens_details": {"cached_tokens": 4},
        "output_tokens_details": {"reasoning_tokens": 12},
    }


def test_detail_blocks_always_present_even_when_gateway_omits_them() -> None:
    """openai-python's ResponseUsage requires both blocks; omitting them breaks
    typed clients. Absent upstream detail → zero-defaulted, never missing."""
    response = _chat_payload_to_response(
        _payload({"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}),
        "openai/gpt-5.5",
    )
    assert response["usage"]["input_tokens_details"] == {"cached_tokens": 0}
    assert response["usage"]["output_tokens_details"] == {"reasoning_tokens": 0}


def test_anthropic_cache_read_maps_to_cached_tokens() -> None:
    """Anthropic models carry cache reads ONLY top-level (no
    prompt_tokens_details); the gateway folds them into prompt_tokens, so
    cached_tokens can safely equal cache_read_input_tokens."""
    usage = _usage_to_responses(
        {
            "prompt_tokens": 2160,
            "completion_tokens": 20,
            "total_tokens": 2180,
            "cache_read_input_tokens": 2048,
            "cache_creation_input_tokens": 100,
        }
    )
    assert usage["input_tokens"] == 2160
    assert usage["input_tokens_details"] == {"cached_tokens": 2048}


def test_explicit_cached_tokens_wins_over_cache_read_fallback() -> None:
    usage = _usage_to_responses(
        {
            "prompt_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 40},
            "cache_read_input_tokens": 99,  # OpenAI-style detail is authoritative
        }
    )
    assert usage["input_tokens_details"] == {"cached_tokens": 40}


def test_only_spec_keys_are_projected() -> None:
    """Chat-shaped detail dicts carry keys the Responses spec doesn't define
    (audio_tokens, accepted/rejected_prediction_tokens) — they must not leak."""
    usage = _usage_to_responses(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 40, "audio_tokens": 7},
            "completion_tokens_details": {
                "reasoning_tokens": 12,
                "accepted_prediction_tokens": 3,
                "rejected_prediction_tokens": 1,
            },
        }
    )
    assert usage["input_tokens_details"] == {"cached_tokens": 40}
    assert usage["output_tokens_details"] == {"reasoning_tokens": 12}


def test_garbage_details_are_coerced_not_crashed() -> None:
    """The detail fields are untyped pydantic extras — whatever JSON the
    upstream sent. Lists, strings, bools, negatives all coerce to 0."""
    usage = _usage_to_responses(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_tokens_details": ["not", "a", "dict"],
            "completion_tokens_details": {"reasoning_tokens": "twelve"},
            "cache_read_input_tokens": -3,
        }
    )
    assert usage["input_tokens_details"] == {"cached_tokens": 0}
    assert usage["output_tokens_details"] == {"reasoning_tokens": 0}


# ---------------------------------------------------------------------------
# SSE stream path — the same detail must reach response.completed
# ---------------------------------------------------------------------------

async def _stream_with_details(model, messages, **kw):
    for t in ["ok"]:
        yield ChatCompletionChunk(
            id="c", object="chat.completion.chunk", created=1, model=model,
            choices=[{"index": 0, "delta": {"content": t}, "finish_reason": None}],
        )
    yield ChatCompletionChunk(
        id="c", object="chat.completion.chunk", created=1, model=model,
        choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
    )
    # The include_usage final frame (choices:[] + usage) with full detail.
    yield ChatCompletionChunk(
        id="c", object="chat.completion.chunk", created=1, model=model,
        choices=[],
        usage={
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 40},
            "completion_tokens_details": {"reasoning_tokens": 12},
        },
    )


def _completed_usage(raw_lines: list[str]) -> dict:
    for ln in raw_lines:
        if ln.startswith("data: "):
            data = json.loads(ln[len("data: "):])
            if data.get("type") == "response.completed":
                return data["response"]["usage"]
    raise AssertionError("no response.completed event seen")


def test_responses_sse_stream_preserves_token_details() -> None:
    with patch.object(P._adapter, "chat_completion_stream_async", _stream_with_details):
        with client.stream(
            "POST", "/v1/responses",
            json={"model": "gpt-5.5", "input": "hi", "stream": True},
        ) as r:
            assert r.status_code == 200
            usage = _completed_usage(list(r.iter_lines()))

    assert usage == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "input_tokens_details": {"cached_tokens": 40},
        "output_tokens_details": {"reasoning_tokens": 12},
    }


async def _stream_detail_then_bare(model, messages, **kw):
    """Detail-bearing usage frame followed by a bare one — the bare frame must
    not wipe the detail (merge, not replace)."""
    yield ChatCompletionChunk(
        id="c", object="chat.completion.chunk", created=1, model=model,
        choices=[{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
    )
    yield ChatCompletionChunk(
        id="c", object="chat.completion.chunk", created=1, model=model,
        choices=[],
        usage={
            "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
            "completion_tokens_details": {"reasoning_tokens": 12},
        },
    )
    yield ChatCompletionChunk(
        id="c", object="chat.completion.chunk", created=1, model=model,
        choices=[],
        usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    )


def test_responses_sse_later_bare_usage_frame_does_not_wipe_details() -> None:
    with patch.object(P._adapter, "chat_completion_stream_async", _stream_detail_then_bare):
        with client.stream(
            "POST", "/v1/responses",
            json={"model": "gpt-5.5", "input": "hi", "stream": True},
        ) as r:
            usage = _completed_usage(list(r.iter_lines()))

    assert usage["output_tokens_details"] == {"reasoning_tokens": 12}
    assert usage["total_tokens"] == 120
