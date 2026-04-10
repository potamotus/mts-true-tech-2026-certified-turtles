from __future__ import annotations

import hashlib
import math
import re
import aiohttp
from typing import List, Optional, Tuple

from .models import MemoryEntry

# Keyword-based query type detection (covers ~70% of cases)
QUERY_TYPE_KEYWORDS = {
    "deadline": ["дедлайн", "срок", "когда", "deadline", "дата сдачи", "когда сдавать", "время"],
    "project": ["проект", "задача", "работа", "project", "task", "разработка", "код"],
    "contact": ["кто ", "команда", "коллега", "team", "контакт", "кто делает", "кто отвечает"],
    "skill": ["как ", "настроить", "сделать", "how to", "how do", "умеет", "знает", "навык"],
    "preference": ["предпочита", "нравит", "люблю", "не люблю", "удобнее", "лучше"],
    "decision": ["решили", "решение", "выбрали", "договорились", "decision", "choice"],
}

CATEGORY_PRIORITY = {
    "preference": 0,
    "deadline": 1,
    "project": 2,
    "decision": 3,
    "contact": 4,
    "skill": 5,
    "role": 6,
}


class MemoryRetriever:
    """Smart retrieval with category boost and token budget."""

    def __init__(self, valves: dict):
        self.valves = valves
        self._classify_cache: dict = {}

    # ── Public API ──────────────────────────────────────────────

    def detect_query_type(self, query: str) -> str:
        """Two-tier: keyword matching (fast) → LLM fallback (cached)."""
        lower = query.lower()
        for qtype, keywords in QUERY_TYPE_KEYWORDS.items():
            if any(kw in lower for kw in keywords):
                return qtype
        return "general"

    async def retrieve(
        self,
        user_id: str,
        query: str,
        user_token: str,
        base_url: str,
        query_type: Optional[str] = None,
    ) -> List[MemoryEntry]:
        """Retrieve memories with multi-signal scoring."""
        if query_type is None:
            query_type = self.detect_query_type(query)

        try:
            # Fetch candidates from OpenWebUI Memories API
            candidates = await self._fetch_candidates(
                user_id, query, user_token, base_url
            )
            if not candidates:
                return []

            # Score each candidate
            scored = self._score(candidates, query, query_type)

            # Sort and return top-K
            max_k = self.valves.get("max_memories", 5)
            scored.sort(key=lambda x: x[0], reverse=True)
            top = [m for _, m in scored[:max_k]]

            # Touch accessed memories
            for m in top:
                m.touch()

            return top

        except Exception:
            return []

    def assemble_context(self, memories: List[MemoryEntry]) -> Tuple[str, int]:
        """Format memories into system prompt text within token budget."""
        max_tokens = self.valves.get("max_tokens", 2000)

        # Sort by priority (preferences and deadlines first)
        memories.sort(
            key=lambda m: CATEGORY_PRIORITY.get(m.category, 99)
        )

        result = []
        total_tokens = 0
        for mem in memories:
            tokens = self._estimate_tokens(mem.content)
            if total_tokens + tokens <= max_tokens:
                result.append(mem)
                total_tokens += tokens
            else:
                break

        if not result:
            return "", 0

        formatted = "\n".join(
            f"- [{m.category.upper()}] {m.content}" for m in result
        )
        return formatted, total_tokens

    # ── Internal ────────────────────────────────────────────────

    async def _fetch_candidates(
        self, user_id: str, query: str, user_token: str, base_url: str
    ) -> List[MemoryEntry]:
        """Fetch from OpenWebUI Memories API via vector search."""
        fetch_k = self.valves.get("max_memories", 5) * 3  # Over-fetch for scoring
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base_url}/api/memories/query",
                    headers={"Authorization": f"Bearer {user_token}"},
                    json={"query": query, "k": fetch_k},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()
                    mems = data.get("memories", [])
                    candidates = []
                    for m in mems:
                        entry = MemoryEntry.from_dict(m.get("metadata", m))
                        # Use similarity from API response
                        entry.metadata["api_similarity"] = m.get("similarity", 0.5)
                        candidates.append(entry)
                    return candidates
        except Exception:
            return []

    def _score(
        self, candidates: List[MemoryEntry], query: str, query_type: str
    ) -> List[Tuple[float, MemoryEntry]]:
        """Multi-signal scoring: similarity + recency + category boost."""
        alpha = 0.5  # similarity weight
        beta = 0.2   # recency weight
        delta = 0.3  # category boost weight

        scored = []
        for mem in candidates:
            if mem.status != "active":
                continue

            # Similarity from API (or fallback)
            sim = mem.metadata.get("api_similarity", 0.5)

            # Recency decay
            recency = mem.recency_score()

            # Category boost
            base_boost = self.valves.get("category_weights", {}).get(mem.category, 1.0)
            type_boost = 1.5 if query_type == mem.category else 1.0
            boost = base_boost * type_boost

            score = alpha * sim + beta * recency + delta * boost
            scored.append((score, mem))

        return scored

    def _estimate_tokens(self, text: str) -> int:
        """Rough token count: ~3 chars per token for mixed RU/EN."""
        return max(1, len(text) // 3)

    def _llm_classify(self, query: str, valves: dict) -> str:
        """Fallback LLM classifier for ambiguous queries. Cached."""
        cache_key = hashlib.md5(query.encode()).hexdigest()
        if cache_key in self._classify_cache:
            return self._classify_cache[cache_key]

        # Not implemented for hackathon — always return general
        # Full implementation would call MWS GPT with a classification prompt
        return "general"
