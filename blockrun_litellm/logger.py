"""
Local JSONL logging for LiteLLM calls — opt-in observability for partners
who want a per-request audit trail without standing up Langfuse / Helicone /
S3 etc. Captures input messages, completion content, token usage, latency,
and (when available) per-call cost.

The implementation hooks LiteLLM's :class:`CustomLogger` interface so we
catch sync **and** async, streaming **and** non-streaming, success **and**
failure on the same code path — the older ``success_callback`` /
``failure_callback`` lists don't fire reliably for streaming errors.

Two integration paths:

1. **Python library mode** — call :func:`enable_local_logging` once at
   startup::

       import litellm
       from blockrun_litellm import register, enable_local_logging
       register()
       enable_local_logging()                # writes ~/.blockrun/litellm_calls.jsonl
       # or enable_local_logging("/tmp/calls.jsonl")
       # or set env var BLOCKRUN_LITELLM_LOG=/tmp/calls.jsonl

       litellm.completion(model="blockrun/openai/gpt-5.5", messages=[...])

2. **LiteLLM Proxy Server mode** — point ``callbacks`` at our module in
   ``config.yaml``::

       litellm_settings:
         callbacks: ["blockrun_litellm.logger.proxy_logger"]

   The same env var (``BLOCKRUN_LITELLM_LOG``) controls the destination
   path; default is ``~/.blockrun/litellm_calls.jsonl``.

Each JSONL line is one row with these fields:

    ts             — Unix epoch seconds (float)
    iso            — ISO-8601 timestamp string
    model          — model the request was routed to
    provider       — provider prefix ("blockrun" for our adapter)
    messages       — full request messages array (input)
    completion     — assistant response content (output); ``None`` on failure
    usage          — {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
                     populated when the upstream returns usage; ``None`` otherwise
    latency_ms     — wall-clock duration of the call
    stream         — bool: True if the caller asked for streaming
    cost_usd       — real x402 wallet charge for this call when known
                     (``cost_source == "blockrun_x402"``); otherwise LiteLLM's
                     token×list-price estimate. ``0.0`` for free models,
                     ``None`` on failure.
    cost_source    — "blockrun_x402" (real on-chain charge) or
                     "litellm_estimate" (fallback token×list-price guess)
    estimated_cost_usd — LiteLLM's token×list-price estimate, always recorded
                     alongside so the estimate vs real gap is auditable
    settlement     — decoded on-chain receipt {tx_hash, amount_micro_usdc,
                     network, ...} when the gateway returned one; else ``None``
    status         — "success" or "failure"
    error_type     — class name (failure only)
    error_message  — exception ``str`` (failure only)
    request_id     — pass-through of LiteLLM's ``litellm_call_id`` for joining logs

Rotation, retention, and shipping to centralized systems are intentionally
out of scope — this is a primitive you can ``tail -f`` or load into pandas.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger("blockrun_litellm.logger")

DEFAULT_LOG_PATH = Path.home() / ".blockrun" / "litellm_calls.jsonl"

# Single lock so concurrent callbacks don't interleave JSONL rows.
_write_lock = threading.Lock()


def _resolve_path(path: Optional[str | Path] = None) -> Path:
    """Pick the log destination — explicit arg > env var > default."""
    resolved = Path(path or os.environ.get("BLOCKRUN_LITELLM_LOG") or DEFAULT_LOG_PATH)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _write_entry(path: Path, entry: Dict[str, Any]) -> None:
    """Append one JSONL row. Swallows OSError so logging never breaks calls."""
    try:
        line = json.dumps(entry, default=str, ensure_ascii=False)
        with _write_lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except (OSError, TypeError, ValueError) as exc:  # pragma: no cover - defensive
        log.warning("blockrun-litellm log write failed: %s", exc)


def _extract_usage(response_obj: Any) -> Optional[Dict[str, int]]:
    """LiteLLM ModelResponse exposes ``usage`` as either a pydantic model or
    a dict, depending on version. Normalize to a plain dict."""
    usage = getattr(response_obj, "usage", None)
    if usage is None and isinstance(response_obj, dict):
        usage = response_obj.get("usage")
    if usage is None:
        return None
    if isinstance(usage, dict):
        return {k: v for k, v in usage.items() if v is not None}
    out: Dict[str, int] = {}
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        v = getattr(usage, k, None)
        if v is not None:
            out[k] = int(v)
    return out or None


def _extract_completion(response_obj: Any) -> Optional[str]:
    """Pull the assistant text out of LiteLLM's ModelResponse / dict."""
    try:
        return response_obj.choices[0].message.content
    except Exception:
        pass
    try:
        return response_obj["choices"][0]["message"]["content"]
    except Exception:
        return None


def _hidden_params(response_obj: Any) -> Optional[Dict[str, Any]]:
    hp = getattr(response_obj, "_hidden_params", None)
    if hp is None and isinstance(response_obj, dict):
        hp = response_obj.get("_hidden_params")
    return hp if isinstance(hp, dict) else None


def _extract_cost(response_obj: Any, kwargs: Dict[str, Any]) -> Optional[float]:
    """LiteLLM stuffs a computed cost in a few possible places."""
    try:
        hp = _hidden_params(response_obj)
        if hp:
            c = hp.get("response_cost")
            if c is not None:
                return float(c)
    except Exception:
        pass
    c = kwargs.get("response_cost") if isinstance(kwargs, dict) else None
    return float(c) if c is not None else None


def _extract_real_cost(response_obj: Any) -> Dict[str, Any]:
    """Pull BlockRun's real x402 charge + settlement off the response.

    Returns ``{"cost_usd": <real|None>, "settlement": <dict|None>}``. When a
    real charge is present we record it as the authoritative ``cost_usd`` and
    tag ``cost_source="blockrun_x402"``; otherwise the row falls back to
    LiteLLM's token×list-price estimate (``cost_source="litellm_estimate"``).
    """
    try:
        hp = _hidden_params(response_obj)
        if hp and hp.get("blockrun_cost_usd") is not None:
            return {
                "cost_usd": float(hp["blockrun_cost_usd"]),
                "settlement": hp.get("blockrun_settlement"),
            }
    except Exception:
        pass
    return {"cost_usd": None, "settlement": None}


def _latency_ms(start_time: Any, end_time: Any) -> Optional[float]:
    try:
        if hasattr(start_time, "isoformat") and hasattr(end_time, "isoformat"):
            return (end_time - start_time).total_seconds() * 1000.0
        return (float(end_time) - float(start_time)) * 1000.0
    except Exception:
        return None


def _iso(ts: Any) -> str:
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    try:
        return datetime.fromtimestamp(float(ts)).isoformat()
    except Exception:
        return str(ts)


def _provider_for(model: Any) -> Optional[str]:
    if not isinstance(model, str):
        return None
    return model.split("/", 1)[0] if "/" in model else None


def _build_entry(
    kwargs: Dict[str, Any],
    response_obj: Any,
    start_time: Any,
    end_time: Any,
    *,
    failure: Optional[BaseException] = None,
) -> Optional[Dict[str, Any]]:
    """Build the JSONL row, or return ``None`` if this is an intermediate
    streaming chunk that should be dropped."""
    if not isinstance(kwargs, dict):
        kwargs = {}

    stream = bool(kwargs.get("stream"))
    model = kwargs.get("model")

    entry: Dict[str, Any] = {
        "ts": time.time(),
        "iso": _iso(end_time),
        "model": model,
        "provider": _provider_for(model),
        "messages": kwargs.get("messages"),
        "stream": stream,
        "latency_ms": _latency_ms(start_time, end_time),
        "request_id": (
            kwargs.get("litellm_call_id") or (kwargs.get("metadata") or {}).get("litellm_call_id")
        ),
    }

    if failure is not None:
        entry.update(
            {
                "status": "failure",
                "completion": None,
                "usage": None,
                "cost_usd": None,
                "cost_source": None,
                "estimated_cost_usd": None,
                "settlement": None,
                "error_type": type(failure).__name__,
                "error_message": str(failure),
            }
        )
        return entry

    usage = _extract_usage(response_obj)
    completion = _extract_completion(response_obj)
    # During streaming, LiteLLM fires the success hook *per chunk* with an
    # accumulator that has empty usage/content until the final fire. Drop
    # those intermediate rows.
    if stream and usage is None and not completion:
        return None
    estimate = _extract_cost(response_obj, kwargs)
    real = _extract_real_cost(response_obj)
    hidden = getattr(response_obj, "_hidden_params", {}) or {}
    account_mode = hidden.get("blockrun_auth_mode") == "api-key"
    if account_mode:
        real = {"cost_usd": None, "settlement": None}
    if real["cost_usd"] is not None:
        cost_usd = real["cost_usd"]
        cost_source = "blockrun_x402"
    else:
        cost_usd = estimate
        cost_source = "account_estimate" if account_mode else "litellm_estimate"
    entry.update(
        {
            "status": "success",
            "completion": completion,
            "usage": usage,
            # Real wallet deduction when known (x402), else LiteLLM's estimate.
            "cost_usd": cost_usd,
            "cost_source": cost_source,
            # Keep LiteLLM's token×list-price estimate alongside for comparison.
            "estimated_cost_usd": estimate,
            "settlement": real["settlement"],
        }
    )
    return entry


def log_proxy_call(
    *,
    model: Optional[str],
    path: str,
    stream: bool,
    http_status: Optional[int],
    cost_usd: Optional[float],
    settlement: Optional[Dict[str, Any]],
    latency_ms: Optional[float],
    request_id: Optional[str] = None,
    settlement_status: Optional[str] = None,
    auth_mode: Optional[str] = None,
) -> None:
    """Append a JSONL audit row for a raw FastAPI sidecar passthrough call.

    The sidecar relays bytes and has no LiteLLM callback to ride, so this is the
    audit hook for ``/v1/chat/completions`` + ``/v1/messages``. **Opt-in** via
    ``BLOCKRUN_LITELLM_LOG`` — unset means the sidecar never touches disk (no
    surprise writes to ``~/.blockrun``). ``cost_usd`` is the real x402 charge
    decoded from the gateway's ``PAYMENT-RESPONSE`` header (``None`` for free /
    cached calls); the row mirrors the custom-provider schema with a
    ``mode='proxy_passthrough'`` marker so both sources coexist in one file.

    ``settlement_status`` distinguishes "this cost nothing" from "we don't know
    what this cost" — a distinction ``cost_usd=None`` alone cannot carry:

    * ``None``      — nothing to flag (a success, or a failure that never paid).
    * ``"unknown"`` — the call reached the gateway with payment and failed, on a
      chain where that can still settle. Do not read ``cost_usd=None`` as $0;
      confirm against the gateway ledger, which is the authority.

    Without this a Solana charge for a failed call is indistinguishable from a
    free failure, and a ledger that silently under-reports spend is worse than
    one that admits a gap.
    """
    if not os.environ.get("BLOCKRUN_LITELLM_LOG"):
        return
    try:
        now = time.time()
        entry = {
            "ts": now,
            "iso": _iso(now),
            "model": model,
            "provider": "blockrun",
            "mode": "proxy_passthrough",
            "path": path,
            "stream": bool(stream),
            "latency_ms": latency_ms,
            "status": "success" if (http_status or 0) < 400 else "failure",
            "http_status": http_status,
            "cost_usd": cost_usd,
            "cost_source": "blockrun_x402" if cost_usd is not None else None,
            "settlement": settlement,
            "request_id": request_id,
        }
        if auth_mode == "api-key":
            entry.update(
                auth_mode="api-key", cost_usd=None, cost_source="account_portal", settlement=None
            )
            settlement_status = None
        # Omitted when there's nothing to flag, so existing rows keep their shape
        # and anything parsing this file sees the key only when it means something.
        if settlement_status is not None:
            entry["settlement_status"] = settlement_status
        _write_entry(_resolve_path(), entry)
    except Exception:  # pragma: no cover - logging never breaks the proxy
        pass


# ---------------------------------------------------------------------------
# CustomLogger implementation
# ---------------------------------------------------------------------------


class JSONLLogger(CustomLogger):
    """LiteLLM :class:`CustomLogger` that appends one JSONL row per call.

    Instantiate with an explicit ``path``, or rely on the env var
    ``BLOCKRUN_LITELLM_LOG`` / the default ``~/.blockrun/litellm_calls.jsonl``.
    """

    def __init__(self, path: Optional[str | Path] = None) -> None:
        super().__init__()
        self._path = _resolve_path(path)

    @property
    def path(self) -> Path:
        return self._path

    # Sync hooks --------------------------------------------------------
    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        entry = _build_entry(kwargs, response_obj, start_time, end_time)
        if entry is not None:
            _write_entry(self._path, entry)

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        # LiteLLM passes the exception object as the second positional arg in
        # the failure hook (it's called ``response_obj`` for legacy reasons).
        exc = (
            response_obj
            if isinstance(response_obj, BaseException)
            else (kwargs.get("exception") if isinstance(kwargs, dict) else None)
        )
        entry = _build_entry(
            kwargs, None, start_time, end_time, failure=exc or Exception("unknown")
        )
        if entry is not None:
            _write_entry(self._path, entry)

    # Async hooks (LiteLLM Proxy + async users go through these) --------
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self.log_success_event(kwargs, response_obj, start_time, end_time)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self.log_failure_event(kwargs, response_obj, start_time, end_time)


# Module-level singleton for the Proxy Server config.yaml path. Resolves its
# destination on instantiation (so the env var read happens at import).
proxy_logger = JSONLLogger()


# ---------------------------------------------------------------------------
# Convenience for Mode A
# ---------------------------------------------------------------------------


def enable_local_logging(path: Optional[str | Path] = None) -> Path:
    """Attach a :class:`JSONLLogger` to ``litellm.callbacks`` and return the
    resolved log path.

    Idempotent for a given ``path`` — calling twice with the same path is a
    no-op; calling with a different path adds a second logger that writes
    to the new destination too.
    """
    import litellm

    resolved = _resolve_path(path)

    # Idempotency check.
    for cb in getattr(litellm, "callbacks", None) or []:
        if isinstance(cb, JSONLLogger) and cb.path == resolved:
            return resolved

    logger = JSONLLogger(resolved)
    if not isinstance(litellm.callbacks, list):
        litellm.callbacks = list(litellm.callbacks or [])
    litellm.callbacks.append(logger)
    return resolved


__all__ = [
    "JSONLLogger",
    "enable_local_logging",
    "proxy_logger",
    "DEFAULT_LOG_PATH",
]
