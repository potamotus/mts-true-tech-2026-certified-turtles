from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def duckduckgo_text_search(query: str, *, max_results: int = 5) -> list[dict[str, Any]]:
    """Текстовый поиск через DuckDuckGo (без API-ключа). Возвращает список {title, href, body}.

    Пустую выдачу возвращает как `[]` (не исключение). Любая другая ошибка библиотеки
    поднимается как `RuntimeError`, чтобы обработчик тула вернул аккуратный JSON-ответ модели,
    а не ронял стек в логи.
    """
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException

    capped = max(1, min(int(max_results), 10))
    results: list[dict[str, Any]] = []
    try:
        with DDGS() as ddgs:
            for item in ddgs.text(query, max_results=capped):
                # У ddgs ключ называется `url` (а не `href` как в legacy `duckduckgo_search`).
                href = item.get("href") or item.get("url") or ""
                results.append(
                    {
                        "title": item.get("title") or "",
                        "href": href,
                        "body": (item.get("body") or "")[:2000],
                    }
                )
    except DDGSException as e:
        msg = str(e)
        if "No results" in msg:
            logger.info("web_search empty result for query=%r", query)
            return []
        logger.warning("web_search failed for query=%r: %s", query, msg)
        raise RuntimeError(f"web_search failed: {msg}") from e
    return results


def format_search_results_for_llm(items: list[dict[str, Any]]) -> str:
    if not items:
        return "Поиск не дал результатов. Сформулируй запрос иначе или скажи пользователю, что данных нет."
    lines: list[str] = []
    for i, it in enumerate(items, 1):
        title = it.get("title") or "(без заголовка)"
        href = it.get("href") or ""
        body = (it.get("body") or "").strip().replace("\n", " ")
        if len(body) > 400:
            body = body[:400] + "…"
        lines.append(f"{i}. {title}\n   URL: {href}\n   {body}")
    return "\n\n".join(lines)
