"""Решение оркестратора: вызывать ли execute_python — через отдельный LLM-вызов (без регэкспов)."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from certified_turtles.agents.json_agent_protocol import message_text_content
from certified_turtles.mws_gpt.client import MWSGPTClient, MWSGPTError
from certified_turtles.prompts import load_prompt

logger = logging.getLogger(__name__)

_INTENT_USER_MAX_CHARS = 12000


def _intent_llm_enabled() -> bool:
    v = os.environ.get("CT_EXECUTE_PYTHON_INTENT_LLM", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _intent_model(model: str) -> str:
    override = (os.environ.get("CT_EXECUTE_PYTHON_INTENT_MODEL") or "").strip()
    return override if override else model


def _strip_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def parse_skip_execute_python_flag(assistant_text: str) -> bool | None:
    """Извлекает skip_execute_python из ответа модели; None — если не удалось."""
    s = _strip_fences(assistant_text)
    dec = json.JSONDecoder()
    for i, c in enumerate(s):
        if c != "{":
            continue
        try:
            obj, _ = dec.raw_decode(s[i:])
            if isinstance(obj, dict) and "skip_execute_python" in obj:
                return bool(obj["skip_execute_python"])
        except json.JSONDecodeError:
            continue
    return None


def _completion_assistant_text(raw: dict[str, Any]) -> str:
    try:
        ch = raw["choices"][0]
        msg = ch.get("message") or {}
        return message_text_content(msg)
    except (KeyError, IndexError, TypeError):
        return ""


def llm_should_skip_execute_python(
    client: MWSGPTClient,
    model: str,
    user_text: str,
) -> bool:
    """
    True — не вызывать execute_python (ответ пользователю должен быть кодом в markdown без запуска).
    При ошибке API или парсинга — False (разрешить запуск).
    """
    if not (user_text or "").strip():
        return False
    if not _intent_llm_enabled():
        return False
    system = load_prompt("intent_execute_python_system.md").strip()
    user_body = load_prompt("intent_execute_python_user.md").format(
        user_message=user_text.strip()[:_INTENT_USER_MAX_CHARS],
    )
    intent_model = _intent_model(model)
    try:
        raw = client.chat_completions(
            intent_model,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_body},
            ],
            temperature=0,
            max_tokens=128,
        )
    except MWSGPTError as e:
        logger.warning("execute_python intent LLM failed: %s", e)
        return False
    text = _completion_assistant_text(raw)
    parsed = parse_skip_execute_python_flag(text)
    if parsed is None:
        logger.warning(
            "execute_python intent: unparseable assistant text, allow execution. preview=%s",
            (text or "")[:240],
        )
        return False
    return parsed
