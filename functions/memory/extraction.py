from __future__ import annotations

import json
import re
import time
import aiohttp
from typing import List, Optional

from .models import MemoryEntry
from .dedup import DedupChecker, ConflictResolver

TRIVIAL_PATTERNS = [
    r"привет", r"здравствуй", r"спасибо", r"пока", r"до свидания",
    r"\bok\b", r"\bok\b", r"хорошо", r"ладно", r"понятно",
    r"да\b", r"нет\b", r"угу", r"ага", r"спс",
]

EXPLICIT_TRIGGER_PATTERNS = [
    r"запомни", r"запомнить", r"remember", r"сохрани в память",
    r"добавь в память", r"запиши", r"зафиксируй",
]

EXTRACTION_SYSTEM_PROMPT = """You are a memory extraction assistant. Analyze the conversation and extract NEW facts about the user that should be remembered long-term.

Rules:
- Extract ONLY new information not already known
- Categories: project, preference, contact, decision, deadline, skill, role
- Return a JSON array (can be empty if no new facts)
- Each fact: {"category": "...", "fact": "...", "confidence": 0.0-1.0}
- Confidence: 0.9+ for explicit statements, 0.7+ for implied, below 0.7 skip
- If no new facts, return []

Return ONLY valid JSON, no markdown, no explanations."""


class MemoryExtractor:
    """Batched memory extraction with rule-based pre-filtering.

    Buffers messages and calls LLM only when:
    - Buffer reaches batch_size, OR
    - Timeout expires, OR
    - Explicit "запомни" trigger detected
    """

    def __init__(self, valves: dict):
        self.valves = valves
        self._message_buffer: dict = {}  # user_id -> list
        self._last_extracted: dict = {}  # user_id -> timestamp
        self._dedup = DedupChecker(threshold=0.85)
        self._conflict_resolver = ConflictResolver()

    # ── Public API ──────────────────────────────────────────────

    def should_extract(self, user_id: str) -> bool:
        buffer = self._message_buffer.get(user_id, [])
        last = self._last_extracted.get(user_id, 0)
        now = time.time()
        return (
            len(buffer) >= self.valves.get("batch_size", 5)
            or (now - last) > self.valves.get("batch_timeout_sec", 300)
        )

    def is_explicit_trigger(self, text: str) -> bool:
        if not text:
            return False
        lower = text.lower()
        return any(re.search(p, lower) for p in EXPLICIT_TRIGGER_PATTERNS)

    def is_trivial(self, text: str) -> bool:
        if not text:
            return True
        lower = text.lower()
        return any(re.search(p, lower) for p in TRIVIAL_PATTERNS)

    def buffer_message(self, user_id: str, chat_id: str, messages: list, response: dict):
        if user_id not in self._message_buffer:
            self._message_buffer[user_id] = []
        self._message_buffer[user_id].append({
            "chat_id": chat_id,
            "messages": messages[-3:],
            "response": response,
            "timestamp": time.time(),
        })

    def clear_buffer(self, user_id: str):
        self._message_buffer.pop(user_id, None)
        self._last_extracted[user_id] = time.time()

    async def extract_and_store(
        self,
        user_id: str,
        chat_id: str,
        messages: list,
        response: dict,
        user_token: str,
        base_url: str,
        force: bool = False,
    ) -> List[MemoryEntry]:
        """Extract facts from conversation and store with dedup."""
        try:
            # Build context from buffer or current messages
            if force:
                context_messages = messages[-10:]
            else:
                buffer = self._message_buffer.get(user_id, [])
                context_messages = []
                for item in buffer:
                    context_messages.extend(item["messages"])
                context_messages = context_messages[-10:]

            if not context_messages:
                return []

            # Call LLM for extraction
            facts = await self._call_extraction_model(context_messages)

            stored = []
            for fact in facts:
                confidence = fact.get("confidence", 0)
                threshold = self.valves.get("confidence_threshold", 0.7)
                if confidence < threshold:
                    continue

                entry = MemoryEntry(
                    user_id=user_id,
                    content=f"[{fact['category']}] {fact['fact']}",
                    category=fact.get("category", "project"),
                    confidence=confidence,
                    source="explicit" if force else "auto_extract",
                    source_chat_id=chat_id,
                )

                # Store with dedup
                stored_entry = await self._store_with_dedup(
                    entry, user_token, base_url
                )
                if stored_entry:
                    stored.append(stored_entry)

            if not force:
                self.clear_buffer(user_id)

            return stored

        except Exception:
            # Graceful degradation — return empty, don't crash
            return []

    # ── Internal ────────────────────────────────────────────────

    async def _call_extraction_model(self, messages: list) -> list:
        endpoint = self.valves.get(
            "mws_gpt_endpoint", "https://api.gpt.mws.ru/v1"
        )
        model = self.valves.get("extraction_model", "mws-gpt-alpha")
        api_key = self.valves.get("api_key", "")

        if not api_key:
            return []

        conversation = json.dumps(messages, ensure_ascii=False, indent=2)
        prompt = f"Conversation:\n{conversation}\n\nExtract facts as JSON array."

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{endpoint}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 500,
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                # Strip markdown code fences if present
                content = re.sub(r"^```(?:json)?\s*", "", content.strip())
                content = re.sub(r"\s*```$", "", content.strip())
                try:
                    result = json.loads(content)
                    return result if isinstance(result, list) else []
                except json.JSONDecodeError:
                    return []

    async def _store_with_dedup(
        self, entry: MemoryEntry, user_token: str, base_url: str
    ) -> Optional[MemoryEntry]:
        """Store via OpenWebUI Memories API with dedup check."""
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {user_token}"}

            # Dedup: query existing similar memories
            try:
                async with session.post(
                    f"{base_url}/api/memories/query",
                    headers=headers,
                    json={"query": entry.content, "k": 3},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        existing_mems = data.get("memories", [])
                        existing_entries = [
                            MemoryEntry.from_dict(m) for m in existing_mems
                        ]
                    else:
                        existing_mems = []
                        existing_entries = []
            except Exception:
                existing_mems = []
                existing_entries = []

            # Check string-level dedup
            dup = self._dedup.find_duplicate(entry.content, existing_entries)
            if dup:
                # Update access count instead of inserting
                dup.touch()
                try:
                    await session.patch(
                        f"{base_url}/api/memories/{dup.id}",
                        headers=headers,
                        json={"metadata": dup.metadata},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
                except Exception:
                    pass
                return dup

            # Check conflicts
            conflicts = self._conflict_resolver.find_conflicts(entry, existing_entries)
            if conflicts:
                self._conflict_resolver.resolve(entry, conflicts)
                # Mark superseded in DB
                for c in conflicts:
                    try:
                        await session.patch(
                            f"{base_url}/api/memories/{c.id}",
                            headers=headers,
                            json={
                                "metadata": {
                                    "status": "superseded",
                                    "superseded_by": entry.id,
                                }
                            },
                            timeout=aiohttp.ClientTimeout(total=10),
                        )
                    except Exception:
                        pass

            # Insert new
            try:
                async with session.post(
                    f"{base_url}/api/memories",
                    headers=headers,
                    json={
                        "content": entry.content,
                        "metadata": entry.to_dict(),
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status in (200, 201):
                        data = await resp.json()
                        return MemoryEntry.from_dict(data)
            except Exception:
                pass

            return entry
