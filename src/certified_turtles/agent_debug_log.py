"""Подробные логи агента на stderr при CT_AGENT_DEBUG=1 (не зависит от уровня uvicorn)."""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from certified_turtles.agents.json_agent_protocol import message_text_content

_CONFIGURED = False
_CLIP_CHARS = 24_000

_PARENT_NAME = "certified_turtles.agent"


def configure_agent_debug_from_env() -> None:
    """Вызывать при старте приложения. Включает DEBUG для дерева certified_turtles.agent.*."""
    global _CONFIGURED, _CLIP_CHARS
    if _CONFIGURED:
        return
    _CONFIGURED = True
    try:
        v = int(os.environ.get("CT_AGENT_DEBUG_MAX_CHARS", "24000"))
    except (TypeError, ValueError):
        v = 24_000
    # 0 или отрицательное — без обрезки (полный ответ модели / stdout тула в agent-debug)
    if v <= 0:
        _CLIP_CHARS = 10**9
    else:
        _CLIP_CHARS = max(2_000, min(500_000, v))

    enabled = os.environ.get("CT_AGENT_DEBUG", "").strip().lower() in ("1", "true", "yes", "on", "debug")
    parent = logging.getLogger(_PARENT_NAME)
    parent.handlers.clear()
    parent.propagate = False

    if enabled:
        parent.setLevel(logging.DEBUG)

        class _FlushStreamHandler(logging.StreamHandler):
            """Чтобы строки сразу попадали в `docker compose logs -f`, без буфера до конца запроса."""

            def emit(self, record: logging.LogRecord) -> None:
                super().emit(record)
                self.flush()

        h = _FlushStreamHandler(sys.stderr)
        h.setLevel(logging.DEBUG)
        h.setFormatter(logging.Formatter("[agent-debug] %(levelname)s %(name)s: %(message)s"))
        parent.addHandler(h)
    else:
        parent.setLevel(logging.CRITICAL + 1)


def debug_clip(text: str | None) -> str:
    if not text:
        return ""
    if len(text) <= _CLIP_CHARS:
        return text
    return text[:_CLIP_CHARS] + f"\n… [обрезано CT_AGENT_DEBUG_MAX_CHARS={_CLIP_CHARS}]"


def _message_preview_limit(explicit: int) -> int:
    """CT_AGENT_DEBUG_MESSAGE_PREVIEW=0 — полный текст каждого сообщения в agent-debug (осторожно, очень длинно)."""
    raw = os.environ.get("CT_AGENT_DEBUG_MESSAGE_PREVIEW")
    if raw is not None and raw.strip() == "0":
        return 10**9
    if raw is not None and raw.strip():
        try:
            return max(50, min(10**9, int(raw.strip())))
        except (TypeError, ValueError):
            pass
    return explicit


def summarize_messages(messages: list[dict[str, Any]], *, preview: int = 200) -> str:
    """Компактное описание истории для лога (роль, длина, урезанный текст)."""
    cap = _message_preview_limit(preview)
    lines: list[str] = []
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            lines.append(f"  [{i}] <not a dict>")
            continue
        role = m.get("role", "?")
        body = message_text_content(m)
        prev = body[:cap] + ("…" if len(body) > cap else "")
        lines.append(f"  [{i}] {role} len={len(body)} preview={json.dumps(prev, ensure_ascii=False)}")
    return "\n".join(lines)


def agent_logger(suffix: str) -> logging.Logger:
    """Логгер certified_turtles.agent.<suffix>; сообщения идут в stderr только при CT_AGENT_DEBUG=1."""
    configure_agent_debug_from_env()
    return logging.getLogger(f"{_PARENT_NAME}.{suffix}")
