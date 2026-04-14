"""
Ручные режимы чата (Deep Research, Coder, …): префикс [CT_MODE:…] в последнем user
или поле JSON ct_mode в теле POST /v1/chat/completions (Open WebUI / API).

Приоритет: **явный ct_mode в JSON** > **виртуальная модель `режим::<id>` из селектора Open WebUI** > префикс `[CT_MODE:…]` в сообщении.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from certified_turtles.prompts import load_prompt

# Значения ct_mode и алиасы из префикса
_MODE_ALIASES: dict[str, str] = {
    "default": "default",
    "normal": "default",
    "deep_research": "deep_research",
    "deep": "deep_research",
    "deep-search": "deep_research",
    "deepsearch": "deep_research",
    "research": "research",
    "web": "research",
    "coder": "coder",
    "code": "coder",
    "data_analyst": "data_analyst",
    "data": "data_analyst",
    "analyst": "data_analyst",
    "writer": "writer",
    "text": "writer",
    "presentation": "presentation",
    "pptx": "presentation",
    "slides": "presentation",
}

_MODE_MAX_ROUNDS: dict[str, int] = {
    "default": 0,  # 0 = не менять тело запроса
    "deep_research": 36,
    "research": 14,
    "coder": 16,
    "data_analyst": 18,
    "writer": 10,
    "presentation": 14,
}

_PREFIX_RE = re.compile(
    r"^\s*\[CT_MODE:\s*([a-zA-Z0-9_\-]+)\s*\]\s*",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class PreparedChatRequest:
    messages: list[dict[str, Any]]
    """Скопированные/изменённые сообщения."""

    max_tool_rounds_override: int | None
    """Если не None — заменить max_tool_rounds (после clamp в прокси)."""

    mode_applied: str | None
    """Какой режим применён (canonical id) или None."""

    forced_agent_id: str | None
    """Если задано — вместо общего родительского цикла сразу запускать этого под-агента."""


def _canonical_mode(raw: str | None) -> str | None:
    if raw is None:
        return None
    key = raw.strip().lower().replace(" ", "_")
    if not key:
        return None
    return _MODE_ALIASES.get(key, key if key in _MODE_MAX_ROUNDS else None)


def _mode_prompt_path(mode: str) -> str | None:
    if mode == "default" or mode not in _MODE_MAX_ROUNDS:
        return None
    return f"modes/{mode}.md"


def _mode_forced_agent_id(mode: str | None) -> str | None:
    if mode in {"research", "deep_research", "presentation"}:
        return mode
    return None


def _inject_mode_system(messages: list[dict[str, Any]], text: str) -> None:
    """Вставляет system с режимом сразу перед первым не-system (обычно перед user)."""
    i = 0
    while i < len(messages) and messages[i].get("role") == "system":
        i += 1
    messages.insert(i, {"role": "system", "content": text.strip()})


def _strip_prefix_from_last_user(messages: list[dict[str, Any]]) -> str | None:
    """Снимает [CT_MODE:…] с последнего user-сообщения со строковым content. Возвращает найденный режим или None."""
    for idx in range(len(messages) - 1, -1, -1):
        m = messages[idx]
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if not isinstance(c, str):
            continue
        match = _PREFIX_RE.match(c)
        if not match:
            continue
        raw_mode = match.group(1)
        rest = c[match.end() :].lstrip()
        mm = dict(m)
        mm["content"] = rest if rest else "(режим активирован; уточни задачу.)"
        messages[idx] = mm
        return raw_mode
    return None


def prepare_chat_request(
    body: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    for_agent: bool = True,
) -> PreparedChatRequest:
    """
    Копирует messages. Если for_agent=False (plain-чат) — только снимает префикс [CT_MODE:…] с последнего user.
    Иначе применяет ct_mode из body или префикс, вставляет system из prompts/modes/<id>.md.
    """
    out = copy.deepcopy(messages)
    if not for_agent:
        _strip_prefix_from_last_user(out)
        return PreparedChatRequest(
            messages=out,
            max_tool_rounds_override=None,
            mode_applied=None,
            forced_agent_id=None,
        )

    mode_raw: str | None = None
    if isinstance(body.get("ct_mode"), str) and body["ct_mode"].strip():
        mode_raw = body["ct_mode"].strip()
        _strip_prefix_from_last_user(out)
    else:
        mode_raw = _strip_prefix_from_last_user(out)

    mode = _canonical_mode(mode_raw)
    if mode is None and mode_raw:
        # неизвестный режим — игнорируем, не ломаем чат
        mode = "default"

    if not mode or mode == "default":
        return PreparedChatRequest(
            messages=out,
            max_tool_rounds_override=None,
            mode_applied=None,
            forced_agent_id=None,
        )

    path = _mode_prompt_path(mode)
    if not path:
        return PreparedChatRequest(
            messages=out,
            max_tool_rounds_override=None,
            mode_applied=None,
            forced_agent_id=None,
        )

    try:
        text = load_prompt(path).strip()
    except OSError:
        return PreparedChatRequest(
            messages=out,
            max_tool_rounds_override=None,
            mode_applied=None,
            forced_agent_id=None,
        )

    if text:
        _inject_mode_system(out, text)

    rounds = _MODE_MAX_ROUNDS.get(mode, 0)
    max_override = rounds if rounds > 0 else None
    return PreparedChatRequest(
        messages=out,
        max_tool_rounds_override=max_override,
        mode_applied=mode,
        forced_agent_id=_mode_forced_agent_id(mode),
    )


def list_chat_mode_ids() -> list[str]:
    """Идентификаторы режимов для /health и документации (без default)."""
    return [k for k in sorted(_MODE_MAX_ROUNDS) if k != "default"]


def canonical_mode_ids() -> frozenset[str]:
    """Множество имён режимов для виртуальных моделей `режим::<id>`."""
    return frozenset(list_chat_mode_ids())


def resolve_mode_path_segment(raw: str) -> str:
    """
    Сегмент URL /v1/m/{segment}/ — должен быть каноническим режимом (deep_research, …).
    Алиасы: deep → deep_research. Иначе ValueError.
    """
    m = _canonical_mode((raw or "").strip())
    if not m or m not in canonical_mode_ids():
        raise ValueError(f"Неизвестный режим: {raw!r}")
    return m
