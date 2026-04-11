from __future__ import annotations

import json
from typing import Any

from certified_turtles.mws_gpt.client import MWSGPTClient

from .storage import MemoryHeader, format_memory_manifest


SELECTOR_SYSTEM_PROMPT = """You are selecting the most relevant memory files for the current user message.
Return only JSON: {"selected_memories":["file.md"]}.
Rules:
- Pick up to 5 files.
- Prefer memories that directly help answer the current message.
- Be selective.
- Use filename, type and description semantically, not by naive keyword match.
"""


def fallback_select(headers: list[MemoryHeader], query: str, *, limit: int = 5) -> list[str]:
    q_words = {w for w in query.lower().split() if len(w) > 2}
    scored: list[tuple[int, str]] = []
    for item in headers:
        hay = f"{item.name} {item.description} {item.type}".lower()
        score = sum(1 for w in q_words if w in hay)
        if score > 0:
            scored.append((score, item.filename))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [name for _, name in scored[:limit]]


def select_relevant_memories(
    client: MWSGPTClient,
    *,
    model: str,
    query: str,
    headers: list[MemoryHeader],
    limit: int = 5,
) -> list[str]:
    if not headers or not query.strip():
        return []
    manifest = format_memory_manifest(headers)
    body = [
        {"role": "system", "content": SELECTOR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Query:\n{query}\n\nAvailable memories:\n{manifest}\n\nReturn JSON only.",
        },
    ]
    try:
        raw = client.chat_completions(
            model,
            body,
            temperature=0.0,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        content = (((raw.get("choices") or [{}])[0].get("message") or {}).get("content")) or "{}"
        parsed = json.loads(content)
        selected = parsed.get("selected_memories", [])
        if isinstance(selected, list):
            valid = [x for x in selected if isinstance(x, str) and x.endswith(".md")]
            return valid[:limit]
    except Exception:
        return fallback_select(headers, query, limit=limit)
    return fallback_select(headers, query, limit=limit)
