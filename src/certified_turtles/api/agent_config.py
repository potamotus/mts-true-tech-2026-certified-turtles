"""GET/POST /api/v1/agent/config — настройка лимита токенов агента из UI."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter

router = APIRouter(prefix="/agent", tags=["agent-config"])

_DEFAULT_MAX_AGENT_TOKENS = 128_000


def get_max_agent_tokens() -> int:
    try:
        return int(os.environ.get("CT_MAX_AGENT_TOKENS", str(_DEFAULT_MAX_AGENT_TOKENS)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_AGENT_TOKENS


@router.get("/config")
def get_config() -> dict[str, Any]:
    return {"max_agent_tokens": get_max_agent_tokens()}


@router.post("/config")
def update_config(body: dict[str, Any]) -> dict[str, Any]:
    value = body.get("max_agent_tokens")
    if value is not None:
        try:
            n = int(value)
            n = max(1000, min(1_000_000, n))
            os.environ["CT_MAX_AGENT_TOKENS"] = str(n)
        except (TypeError, ValueError):
            pass
    return {"max_agent_tokens": get_max_agent_tokens()}
