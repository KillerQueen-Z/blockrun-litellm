"""Tests for the OpenAI Responses API bridge (`POST /v1/responses`).

The BlockRun gateway only speaks Chat Completions; the sidecar bridges the
Responses API (`input` in, `response`/`response.*` SSE out) onto it. These tests
stub the adapter so they never hit the gateway or a wallet.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from unittest.mock import patch  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from blockrun_llm.types import ChatCompletionChunk  # noqa: E402

import blockrun_litellm.proxy as P  # noqa: E402

client = TestClient(P.app)


# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------

def test_responses_to_chat_string_input_and_instructions() -> None:
    model, messages, kwargs, stream = P._responses_to_chat(
        {"model": "gpt-5.5", "instructions": "be brief", "input": "hi",
         "temperature": 0.1, "max_output_tokens": 64, "stream": False}
    )
    assert model == "gpt-5.5"
    assert messages == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]
    assert kwargs == {"max_tokens": 64, "temperature": 0.1}
    assert stream is False


def test_responses_to_chat_item_list_and_developer_role() -> None:
    _, messages, _, _ = P._responses_to_chat(
        {"model": "m", "input": [
            {"role": "developer", "content": "sys"},
            {"role": "user", "content": [{"type": "input_text", "text": "a"},
                                          {"type": "input_text", "text": "b"}]},
        ]}
    )
    assert messages == [
        {"role": "system", "content": "sys"},   # developer → system
        {"role": "user", "content": "ab"},       # content parts concatenated
    ]


# ---------------------------------------------------------------------------
# Non-streaming endpoint
# ---------------------------------------------------------------------------

async def _fake_chat(model, messages, **kw):
    return {
        "id": "chatcmpl-x", "object": "chat.completion", "created": 123, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "我是助手"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


def test_responses_non_streaming_shape() -> None:
    with patch.object(P._adapter, "chat_completion_async", _fake_chat):
        r = client.post("/v1/responses", json={"model": "gpt-5.5", "input": "你好"})
    assert r.status_code == 200
    j = r.json()
    assert j["object"] == "response"
    assert j["status"] == "completed"
    assert j["output"][0]["type"] == "message"
    assert j["output"][0]["content"][0] == {"type": "output_text", "text": "我是助手", "annotations": []}
    assert j["output_text"] == "我是助手"
    # input/output_tokens_details are REQUIRED by the Responses spec
    # (openai.types.responses.ResponseUsage) — always present, zero-defaulted.
    assert j["usage"] == {
        "input_tokens": 5,
        "output_tokens": 3,
        "total_tokens": 8,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
    }
    assert j["id"].startswith("resp_")


def test_responses_missing_input_is_400() -> None:
    r = client.post("/v1/responses", json={"model": "gpt-5.5"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Streaming endpoint
# ---------------------------------------------------------------------------

async def _fake_stream(model, messages, **kw):
    for t in ["我", "是", "助手"]:
        yield ChatCompletionChunk(
            id="c", object="chat.completion.chunk", created=1, model=model,
            choices=[{"index": 0, "delta": {"content": t}, "finish_reason": None}],
        )
    yield ChatCompletionChunk(
        id="c", object="chat.completion.chunk", created=1, model=model,
        choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
        usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    )


def test_responses_streaming_event_sequence() -> None:
    with patch.object(P._adapter, "chat_completion_stream_async", _fake_stream):
        with client.stream("POST", "/v1/responses",
                           json={"model": "gpt-5.5", "input": "hi", "stream": True}) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
            events = [ln[len("event: "):] for ln in r.iter_lines() if ln.startswith("event: ")]
    assert events[0] == "response.created"
    assert events.count("response.output_text.delta") == 3
    assert events[-1] == "response.completed"
    # canonical ordering of the lifecycle wrappers
    for e in ("response.output_item.added", "response.content_part.added",
              "response.output_text.done", "response.output_item.done"):
        assert e in events
