from __future__ import annotations

import json
from typing import Any

from certified_turtles.tools.fetch_url import fetch_url_text
from certified_turtles.tools.registry import ToolSpec, register_tool


def _handle_fetch_url(arguments: dict[str, Any]) -> str:
    url = arguments.get("url")
    if not isinstance(url, str) or not url.strip():
        return json.dumps({"error": "Нужен непустой строковый параметр url."}, ensure_ascii=False)
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return json.dumps(
            {"error": "url должен начинаться с http:// или https://."},
            ensure_ascii=False,
        )
    raw_max = arguments.get("max_chars", 8000)
    try:
        max_chars = int(raw_max)
    except (TypeError, ValueError):
        max_chars = 8000
    max_chars = max(500, min(max_chars, 20000))
    raw_to = arguments.get("timeout_seconds", 15)
    try:
        timeout_sec = int(raw_to)
    except (TypeError, ValueError):
        timeout_sec = 15
    timeout_sec = max(5, min(timeout_sec, 60))
    try:
        data = fetch_url_text(url, max_chars=max_chars, timeout=timeout_sec)
    except RuntimeError as e:
        return json.dumps({"error": "fetch_failed", "detail": str(e)}, ensure_ascii=False)
    text = data.get("text") or ""
    truncated = text.endswith("…")
    out = {**data, "chars": len(text), "truncated": truncated, "timeout_seconds": timeout_sec}
    return json.dumps(out, ensure_ascii=False)


register_tool(
    ToolSpec(
        name="fetch_url",
        description=(
            "HTTP-загрузка страницы (не браузер): возвращает {url, title, text}. "
            "Обязателен, если пользователь дал https://… и спрашивает «что за сайт», «зайди», «открой ссылку» — "
            "без этого ответ будет выдумкой. "
            "После web_search — для выбранных URL из выдачи. "
            "Не заменяй этот тул на web_search по имени домена."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Абсолютный URL (http/https).",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Максимум символов текста (500–20000, по умолчанию 8000).",
                    "default": 8000,
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Таймаут HTTP в секундах (5–60, по умолчанию 15).",
                    "default": 15,
                },
            },
            "required": ["url"],
        },
        handler=_handle_fetch_url,
    )
)
