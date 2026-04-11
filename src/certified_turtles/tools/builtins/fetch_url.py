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
    try:
        data = fetch_url_text(url, max_chars=max_chars)
    except RuntimeError as e:
        return json.dumps({"error": "fetch_failed", "detail": str(e)}, ensure_ascii=False)
    return json.dumps(data, ensure_ascii=False)


register_tool(
    ToolSpec(
        name="fetch_url",
        description=(
            "Скачивает указанный URL и возвращает {url, title, text} — plain text страницы. "
            "Используй, когда у тебя УЖЕ есть конкретная ссылка и нужно её содержимое "
            "(например, после web_search или если ссылку дал пользователь). "
            "НЕ используй для поиска — для поиска есть web_search."
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
            },
            "required": ["url"],
        },
        handler=_handle_fetch_url,
    )
)
