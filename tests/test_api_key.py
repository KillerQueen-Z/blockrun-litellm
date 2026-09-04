import asyncio
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from blockrun_litellm import _adapter, proxy
from blockrun_litellm._auth import account_key, cache_key, wallet_url
from blockrun_litellm.provider import BlockRunLLM

KEY = "brk_live_account_test"
CHAT = {
    "id": "chat-test",
    "model": "openai/gpt-4o-mini",
    "object": "chat.completion",
    "created": 1,
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
}


@pytest.fixture(autouse=True)
def account_fixture(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BLOCKRUN_API_KEY", KEY)
    monkeypatch.delenv("BLOCKRUN_API_BASE_URL", raising=False)
    monkeypatch.delenv("BLOCKRUN_PROXY_TOKEN", raising=False)
    # A leftover wallet URL must not redirect account traffic onto an x402 gateway.
    monkeypatch.setenv("BLOCKRUN_API_URL", "https://sol.blockrun.ai/api")
    for d in (
        _adapter._sync_clients,
        _adapter._async_clients,
        _adapter._image_clients,
        _adapter._media_clients,
        proxy._messages_http_clients,
    ):
        d.clear()
    yield
    for d in (
        _adapter._sync_clients,
        _adapter._async_clients,
        _adapter._image_clients,
        _adapter._media_clients,
        proxy._messages_http_clients,
    ):
        d.clear()


def mock_http(monkeypatch, respond):
    calls = []

    def handle(self, r):
        calls.append(r)
        return respond(r)

    async def ahandle(self, r):
        return handle(self, r)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", ahandle)
    return calls


def test_provider_sync_async_uses_account_key_and_no_wallet_cost(monkeypatch):
    calls = mock_http(monkeypatch, lambda r: httpx.Response(200, json=CHAT, request=r))
    handler = BlockRunLLM()
    r = handler.completion(
        model="openai/gpt-4o-mini", messages=[{"role": "user", "content": "hi"}], api_key=KEY
    )
    assert r.choices[0].message.content == "OK"
    assert r._hidden_params["blockrun_auth_mode"] == "api-key"
    assert "blockrun_cost_usd" not in r._hidden_params
    r = asyncio.run(
        handler.acompletion(
            model="openai/gpt-4o-mini", messages=[{"role": "user", "content": "hi"}], api_key=KEY
        )
    )
    assert r.choices[0].message.content == "OK"
    assert len(calls) == 2
    for c in calls:
        assert str(c.url) == "https://api.blockrun.ai/v1/chat/completions"
        assert c.headers["authorization"] == "Bearer " + KEY
        assert not any("payment" in h for h in c.headers)


@pytest.mark.parametrize("status", [401, 402, 429])
def test_provider_preserves_account_errors(monkeypatch, status):
    calls = mock_http(
        monkeypatch,
        lambda r: httpx.Response(
            status,
            json={"error": {"message": KEY, "code": "quota"}},
            headers={"retry-after": "12"},
            request=r,
        ),
    )
    with pytest.raises(Exception) as err:
        BlockRunLLM().completion(
            model="openai/gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
        )
    assert err.value.status_code == status
    assert err.value.retry_after == "12"
    assert KEY not in str(err.value)
    assert len(calls) == 1


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/messages", "/v1/responses"])
@pytest.mark.parametrize("stream", [False, True])
def test_sidecar_native_protocol_and_headers(monkeypatch, path, stream):
    payload = {
        "model": "openai/gpt-4o-mini",
        "input": "hi",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
        "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
    }
    wire = (
        b'event: response.completed\ndata: {"type":"response.completed","response":{"output":[]}}\n\n'
        if stream
        else json.dumps(
            {
                "id": "native-id",
                "output": [],
                "content": [{"type": "text", "text": "OK"}],
                "choices": [],
            }
        ).encode()
    )
    calls = mock_http(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            stream=httpx.ByteStream(wire),
            headers={"content-type": "text/event-stream" if stream else "application/json"},
            request=r,
        ),
    )
    with TestClient(proxy.app) as client:
        r = client.post(
            path, json=payload, headers={"x-api-key": "placeholder", "payment-signature": "remove"}
        )
    assert r.status_code == 200
    assert r.content == wire
    assert len(calls) == 1
    assert str(calls[0].url) == "https://api.blockrun.ai" + path
    assert json.loads(calls[0].content) == payload
    assert calls[0].headers["authorization"] == "Bearer " + KEY
    assert "x-api-key" not in calls[0].headers


@pytest.mark.parametrize("status", [401, 402, 429])
@pytest.mark.parametrize("stream", [False, True])
def test_sidecar_error_code_retry_after_no_x402(monkeypatch, status, stream):
    calls = mock_http(
        monkeypatch,
        lambda r: httpx.Response(
            status,
            json={"error": {"message": KEY, "code": "quota"}},
            headers={"retry-after": "12"},
            request=r,
        ),
    )
    with TestClient(proxy.app) as client:
        r = client.post("/v1/chat/completions", json={"model": "m", "stream": stream})
    assert r.status_code == status
    assert r.headers["retry-after"] == "12"
    assert KEY not in r.text
    assert len(calls) == 1


def test_cache_uses_key_fingerprint_and_rotation(monkeypatch):
    a = _adapter.get_sync_client()
    key1 = cache_key()
    assert KEY not in key1
    monkeypatch.setenv("BLOCKRUN_API_KEY", "brk_live_second_account")
    b = _adapter.get_sync_client()
    assert a is not b
    assert key1 != cache_key()
    assert proxy._resolve_api_url() == "https://api.blockrun.ai"


def test_media_clients_are_account_mode():
    for f in (
        _adapter.get_image_client,
        _adapter.get_video_client,
        _adapter.get_music_client,
        _adapter.get_speech_client,
    ):
        assert f().auth_mode == "api-key"


@pytest.mark.asyncio
async def test_account_image_202_polling(monkeypatch):
    def response(r):
        if r.method == "POST":
            return httpx.Response(
                202,
                json={"status": "queued", "poll_url": "/api/v1/images/generations/test"},
                request=r,
            )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "created": 1,
                "data": [{"url": "https://cdn.example/test.png"}],
            },
            request=r,
        )

    calls = mock_http(monkeypatch, response)
    result = await _adapter.image_generation_async("cat", api_key=KEY)
    assert result["data"]
    assert [r.method for r in calls] == ["POST", "GET"]
    assert calls[1].url.path == "/v1/images/generations/test"


def test_logger_marks_account_cost_as_unavailable_not_x402(monkeypatch, tmp_path):
    from blockrun_litellm import logger

    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BLOCKRUN_LITELLM_LOG", str(path))
    logger.log_proxy_call(
        model="m",
        path="/v1/messages",
        stream=True,
        http_status=200,
        cost_usd=99,
        settlement={"tx_hash": "mock"},
        latency_ms=1,
        auth_mode="api-key",
    )
    row = json.loads(path.read_text())
    assert row["cost_source"] == "account_portal"
    assert row["cost_usd"] is None
    assert row["settlement"] is None


def test_wallet_selection_preserves_explicit_base_and_prefers_new_solana(monkeypatch, tmp_path):
    monkeypatch.delenv("BLOCKRUN_API_URL")
    monkeypatch.delenv("BLOCKRUN_API_KEY")
    monkeypatch.delenv("BLOCKRUN_CHAIN", raising=False)
    monkeypatch.delenv("SOLANA_WALLET_KEY", raising=False)
    assert wallet_url() == "https://sol.blockrun.ai/api"
    assert wallet_url(private_key="0x" + "1" * 64) == "https://blockrun.ai/api"
    with pytest.raises(ValueError):
        account_key("", "0x" + "1" * 64)
