from __future__ import annotations

import json
from typing import Any

from certified_turtles.tools.registry import ToolSpec, register_tool
from certified_turtles.tools.web_search import duckduckgo_text_search, format_search_results_for_llm


def _handle_web_search(arguments: dict[str, Any]) -> str:
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return json.dumps({"error": "Нужен непустой строковый параметр query."}, ensure_ascii=False)
    q = query.strip()
    if q.startswith("http://") or q.startswith("https://"):
        return json.dumps(
            {
                "error": "bad_query",
                "detail": (
                    "web_search принимает текстовый запрос, а не URL. "
                    "Для получения содержимого конкретной ссылки вызови инструмент `fetch_url` с параметром url."
                ),
            },
            ensure_ascii=False,
        )
    raw_max = arguments.get("max_results", 5)
    try:
        max_results = int(raw_max)
    except (TypeError, ValueError):
        max_results = 5
    try:
        items = duckduckgo_text_search(q, max_results=max_results)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "search_failed", "detail": str(e)}, ensure_ascii=False)
    return format_search_results_for_llm(items)


register_tool(
    ToolSpec(
        name="web_search",
        description=(
            "Поиск в интернете по ТЕКСТОВОМУ запросу. Возвращает список {заголовок, URL, сниппет}. "
            "Используй для фактов, новостей, документации. "
            "ВАЖНО: в `query` передавай обычные слова/фразы на естественном языке, НЕ URL. "
            "Если у тебя уже есть конкретная ссылка и нужно её содержимое — используй инструмент `fetch_url`, "
            "а не `web_search`."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Текстовый поисковый запрос на естественном языке (не URL).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Сколько результатов вернуть (1–10).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        handler=_handle_web_search,
    )
)
