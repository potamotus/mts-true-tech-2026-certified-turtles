from __future__ import annotations

import json
import re
from typing import Any

from certified_turtles.mws_gpt.client import MWSGPTClient

from .storage import MemoryHeader, format_memory_manifest


SELECTOR_SYSTEM_PROMPT = """You are selecting memories that will be useful to Claude Code as it processes a user's query. You will be given the user's query and a list of available memory files with their filenames and descriptions.

Return a list of filenames for the memories that will clearly be useful to Claude Code as it processes the user's query (up to 5). Only include memories that you are certain will be helpful based on their name and description.
- If you are unsure if a memory will be useful in processing the user's query, then do not include it in your list. Be selective and discerning.
- If there are no memories in the list that would clearly be useful, feel free to return an empty list.
- If a list of recently-used tools is provided, do not select memories that are usage reference or API documentation for those tools (Claude Code is already exercising them). DO still select memories containing warnings, gotchas, or known issues about those tools — active use is exactly when those matter.
"""


_TOKEN_RE = re.compile(r"[\w/-]{3,}", re.UNICODE)
_WARNING_HINTS = ("warning", "gotcha", "incident", "issue", "pitfall", "avoid", "bug", "danger")


def _tokenize(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text)}


def fallback_select(
    headers: list[MemoryHeader],
    query: str,
    *,
    limit: int = 5,
    recent_tools: list[str] | None = None,
) -> list[str]:
    q_words = _tokenize(query)
    recent = {x.lower() for x in (recent_tools or [])}
    scored: list[tuple[int, str]] = []
    for item in headers:
        hay = f"{item.name} {item.description} {item.type}".lower()
        if recent and item.type == "reference" and any(tool in hay for tool in recent):
            if not any(hint in hay for hint in _WARNING_HINTS):
                continue
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
    recent_tools: list[str] | None = None,
    already_surfaced: set[str] | None = None,
) -> list[str]:
    if not headers or not query.strip():
        return []
    headers = [h for h in headers if h.filename not in (already_surfaced or set())]
    if not headers:
        return []
    manifest = format_memory_manifest(headers)
    tools_suffix = f"\n\nRecently used tools: {', '.join(recent_tools)}" if recent_tools else ""
    body = [
        {"role": "system", "content": SELECTOR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Query: {query}\n\nAvailable memories:\n{manifest}{tools_suffix}",
        },
    ]
    available = {h.filename for h in headers}
    try:
        raw = client.chat_completions(
            model,
            body,
            temperature=0.0,
            max_tokens=256,
            response_format={"type": "json_object"},
        )
        content = (((raw.get("choices") or [{}])[0].get("message") or {}).get("content")) or "{}"
        parsed = json.loads(content)
        selected = parsed.get("selected_memories", [])
        if isinstance(selected, list):
            valid = [x for x in selected if isinstance(x, str) and x in available]
            return valid[:limit]
        return []
    except Exception:
        return []
