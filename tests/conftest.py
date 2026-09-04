"""
Shared test fixtures.

We never hit the real BlockRun gateway or the EVM in unit tests — every
test stubs out ``blockrun_litellm._adapter.get_sync_client`` /
``get_async_client`` with a fake that returns a canned ``ChatResponse``.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from blockrun_llm.types import ChatChoice, ChatMessage, ChatResponse, ChatUsage


# ---------------------------------------------------------------------------
# Canned response builder
# ---------------------------------------------------------------------------

def make_chat_response(
    *,
    model: str = "openai/gpt-5.5",
    content: str = "stub-response",
    tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> ChatResponse:
    return ChatResponse(
        id="chatcmpl-stub-123",
        object="chat.completion",
        created=1_700_000_000,
        model=model,
        choices=[
            ChatChoice(
                index=0,
                message=ChatMessage(
                    role="assistant",
                    content=content,
                    tool_calls=tool_calls,
                ),
                finish_reason="stop",
            )
        ],
        usage=ChatUsage(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        ),
    )


# ---------------------------------------------------------------------------
# Auto-patch the client cache so no real wallet is ever needed
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_wallet_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure tests run without `BLOCKRUN_WALLET_KEY` set."""
    monkeypatch.delenv("BLOCKRUN_WALLET_KEY", raising=False)
    monkeypatch.delenv("BASE_CHAIN_WALLET_KEY", raising=False)
    monkeypatch.delenv("BLOCKRUN_API_KEY", raising=False)
    monkeypatch.setenv("BLOCKRUN_API_URL", "https://blockrun.ai/api")


@pytest.fixture
def stub_sync_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch the cached sync client with a MagicMock returning canned data."""
    mock = MagicMock()
    mock.chat_completion.return_value = make_chat_response()

    def _get(api_url=None, private_key=None):  # noqa: ANN001 - test stub
        return mock

    monkeypatch.setattr("blockrun_litellm._adapter.get_sync_client", _get)
    return mock


@pytest.fixture
def stub_async_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch the cached async client with an AsyncMock-style stub."""
    mock = MagicMock()

    async def _chat_completion(model, messages, **kwargs):  # noqa: ANN001
        return make_chat_response(model=model)

    mock.chat_completion = _chat_completion

    def _get(api_url=None, private_key=None):  # noqa: ANN001 - test stub
        return mock

    monkeypatch.setattr("blockrun_litellm._adapter.get_async_client", _get)
    return mock
