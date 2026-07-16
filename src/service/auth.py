"""API-key authentication.

Keys are read from `config.service.api_keys` (or the SERVICE_API_KEYS env var,
comma-separated). Each key maps to a caller id used to scope jobs and meter
usage. If no keys are configured the service runs open (dev mode) with a single
"anonymous" caller — fine locally, but set keys before exposing it.

This is deliberately simple prod-lite auth; front it with real OAuth/JWT at the
gateway for a public deployment.
"""

from __future__ import annotations

import hmac
import os

from ..log import get_logger

log = get_logger(__name__)


def load_keys(cfg) -> dict[str, str]:
    """Return {api_key: caller_id}. Config list items may be "key" or
    "key:caller_id"; the env var SERVICE_API_KEYS is a comma-separated fallback."""
    raw = (getattr(cfg, "service", {}) or {}).get("api_keys")
    if not raw:
        env = os.getenv("SERVICE_API_KEYS", "")
        raw = [k for k in env.split(",") if k.strip()]
    keys: dict[str, str] = {}
    for item in raw or []:
        key, _, caller = str(item).partition(":")
        key = key.strip()
        if key:
            keys[key] = caller.strip() or f"key-{key[:6]}"
    return keys


def resolve_caller(presented: str | None, keys: dict[str, str]) -> str | None:
    """Return the caller id for a presented key, or None if unauthorized.
    Open (dev) mode — no keys configured — always resolves to "anonymous"."""
    if not keys:
        return "anonymous"
    if not presented:
        return None
    for key, caller in keys.items():  # constant-time compare per candidate
        if hmac.compare_digest(presented, key):
            return caller
    return None
