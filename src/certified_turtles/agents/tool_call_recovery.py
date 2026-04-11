"""Восстановление tool_calls из текста ответа (модели без нативного function calling)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_MAX_RECOVERED_CALLS = 8


def assistant_text_content(msg: dict[str, Any]) -> str:
    """Текст из message.content (строка или мультимодальные части)."""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        chunks: list[str] = []
        for p in c:
            if not isinstance(p, dict):
                continue
            if p.get("type") in ("text", "input_text"):
                k = "text" if "text" in p else "input_text"
                chunks.append(str(p.get(k) or ""))
        return "\n".join(chunks)
    if c is None:
        return ""
    return str(c)


def _arguments_json_string(name: str, item: dict[str, Any], fn: dict[str, Any] | None) -> str:
    if isinstance(fn, dict):
        raw = fn.get("arguments")
        if isinstance(raw, str):
            return raw or "{}"
        if isinstance(raw, dict):
            return json.dumps(raw, ensure_ascii=False)
    raw = item.get("arguments")
    if isinstance(raw, str):
        return raw or "{}"
    if isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False)
    if name == "execute_python":
        for key in ("code", "input", "script"):
            v = item.get(key)
            if isinstance(v, str) and v.strip():
                return json.dumps({"code": v}, ensure_ascii=False)
    return "{}"


def _normalize_item(item: Any, allowed: set[str], idx: int) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    fn = item.get("function") if isinstance(item.get("function"), dict) else None
    name: str | None = None
    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
        name = fn["name"]
    if not name:
        t = item.get("type")
        if isinstance(t, str) and t in allowed:
            name = t
    if not name and isinstance(item.get("name"), str):
        name = item["name"]
    if not name or name not in allowed:
        return None
    args_str = _arguments_json_string(name, item, fn)
    tid = item.get("id")
    if not isinstance(tid, str) or not tid.strip():
        tid = f"recovered_{idx}"
    return {
        "id": tid,
        "type": "function",
        "function": {"name": name, "arguments": args_str},
    }


def _tool_calls_from_dict(data: dict[str, Any], allowed: set[str]) -> list[dict[str, Any]]:
    raw = data.get("tool_calls")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw[:_MAX_RECOVERED_CALLS]):
        norm = _normalize_item(item, allowed, i)
        if norm:
            out.append(norm)
    return out


def _json_snippet_candidates(text: str) -> list[str]:
    snippets: list[str] = []
    for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE):
        body = m.group(1).strip()
        if body and "tool_calls" in body:
            snippets.append(body)
    stripped = text.strip()
    if stripped.startswith("{") and "tool_calls" in stripped:
        snippets.append(stripped)
    return snippets


def _strip_fence_matching_snippet(full: str, snippet: str) -> str | None:
    for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", full, re.IGNORECASE):
        if m.group(1).strip() == snippet:
            merged = (full[: m.start()] + full[m.end() :]).strip()
            return merged if merged else ""
    return None


def recover_tool_calls_from_assistant_message(
    msg: dict[str, Any],
    allowed_tool_names: set[str],
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Если в тексте ассистента есть JSON с верхнеуровневым `tool_calls`,
    вернуть список в формате OpenAI и опционально content без этого блока.
    `content` менять только если удалось вырезать ровно совпавший ```json``` блок.
    """
    if not allowed_tool_names:
        return [], None
    text = assistant_text_content(msg)
    if not text.strip():
        return [], None

    for snippet in _json_snippet_candidates(text):
        try:
            data = json.loads(snippet)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        calls = _tool_calls_from_dict(data, allowed_tool_names)
        if not calls:
            continue
        logger.info(
            "recovered %s tool_call(s) from assistant text (tools=%s)",
            len(calls),
            [c["function"]["name"] for c in calls],
        )
        new_content = _strip_fence_matching_snippet(text, snippet)
        return calls, new_content

    return [], None
