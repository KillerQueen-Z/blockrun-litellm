"""Resolve account vs wallet credentials without touching process-wide auth state."""

from __future__ import annotations
import hashlib
import os
from pathlib import Path
from typing import Optional


def account_key(api_key: Optional[str] = None, private_key: Optional[str] = None) -> Optional[str]:
    if api_key is not None and private_key is not None:
        raise ValueError("Pass either api_key or private_key, not both")
    key = (
        api_key
        if api_key is not None
        else (os.getenv("BLOCKRUN_API_KEY") if private_key is None else None)
    )
    if key is not None and (
        not key.startswith("brk_live_") or len(key) <= 9 or any(c.isspace() for c in key)
    ):
        raise ValueError(
            "Invalid BlockRun API key; create one at https://user.blockrun.ai/dashboard/keys"
        )
    return key


def account_auth(api_key=None, private_key=None, api_url=None):
    key = account_key(api_key, private_key)
    if key is None:
        return None
    try:
        from blockrun_llm.api_key import resolve_api_auth
    except ImportError as exc:
        raise RuntimeError(
            "Account API mode requires the SDK from BlockRunAI/blockrun-llm#58; use requirements-api-preview.txt until its release"
        ) from exc
    return resolve_api_auth(key, None, api_url or os.getenv("BLOCKRUN_API_BASE_URL"))


def wallet_url(api_url=None, private_key=None):
    explicit = api_url or os.getenv("BLOCKRUN_API_URL")
    if explicit:
        return explicit.rstrip("/")
    chain = os.getenv("BLOCKRUN_CHAIN")
    home = Path.home() / ".blockrun"
    if private_key:
        chain = "base" if private_key.startswith("0x") or len(private_key) == 64 else "solana"
    if not chain:
        for name in ("payment-chain", ".chain"):
            path = home / name
            if path.exists():
                chain = path.read_text().strip()
                if chain:
                    break
    if not chain:
        base = (
            os.getenv("BLOCKRUN_WALLET_KEY")
            or os.getenv("BASE_CHAIN_WALLET_KEY")
            or (home / ".session").exists()
        )
        sol = os.getenv("SOLANA_WALLET_KEY") or (home / ".solana-session").exists()
        chain = "base" if base and not sol else "solana"
    if chain not in ("base", "solana"):
        raise ValueError("BLOCKRUN_CHAIN must be solana or base")
    return "https://sol.blockrun.ai/api" if chain == "solana" else "https://blockrun.ai/api"


def cache_key(api_url=None, private_key=None, api_key=None):
    key = account_key(api_key, private_key)
    if key is not None:
        url = (
            (api_url or os.getenv("BLOCKRUN_API_BASE_URL") or "https://api.blockrun.ai")
            .rstrip("/")
            .removesuffix("/v1")
        )
        mode = "api-key"
    else:
        url = wallet_url(api_url, private_key)
        mode = "wallet"
        key = (
            private_key
            or os.getenv("SOLANA_WALLET_KEY" if "sol.blockrun.ai" in url else "BLOCKRUN_WALLET_KEY")
            or os.getenv("BASE_CHAIN_WALLET_KEY")
            or ""
        )
    return mode + "::" + url + "::" + hashlib.sha256(key.encode()).hexdigest()


def provider_credentials(api_key, kwargs):
    private = kwargs.get("private_key") or (kwargs.get("optional_params") or {}).get("private_key")
    if api_key is not None:
        if private is not None:
            raise ValueError("Pass either api_key or private_key, not both")
        if api_key.startswith("brk_"):
            account_key(api_key)
            return {"api_key": api_key}
        # Backward compatibility for the old api_key-as-wallet interface.
        return {"private_key": api_key}
    return {"private_key": private}
