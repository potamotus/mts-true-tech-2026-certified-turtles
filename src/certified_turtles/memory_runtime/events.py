from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MemoryEvent:
    action: str  # "created" | "updated" | "deleted"
    filename: str
    memory_type: str
    name: str
    scope_id: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "filename": self.filename,
            "type": self.memory_type,
            "name": self.name,
            "scope_id": self.scope_id,
            "timestamp": self.timestamp,
        }

    def to_sse(self) -> str:
        return f"data: {json.dumps(self.to_dict(), ensure_ascii=False)}\n\n"


class MemoryEventBus:
    """Simple broadcast bus: subscribers get events via asyncio.Queue."""

    def __init__(self, maxlen: int = 50):
        self._subscribers: list[asyncio.Queue[MemoryEvent]] = []
        self._recent: list[MemoryEvent] = []
        self._maxlen = maxlen

    def publish(self, event: MemoryEvent) -> None:
        self._recent.append(event)
        if len(self._recent) > self._maxlen:
            self._recent = self._recent[-self._maxlen :]
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue[MemoryEvent]:
        q: asyncio.Queue[MemoryEvent] = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[MemoryEvent]) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def recent(self, *, scope_id: str | None = None, limit: int = 10) -> list[MemoryEvent]:
        items = self._recent
        if scope_id:
            items = [e for e in items if e.scope_id == scope_id]
        return items[-limit:]


_BUS: MemoryEventBus | None = None


def get_event_bus() -> MemoryEventBus:
    global _BUS
    if _BUS is None:
        _BUS = MemoryEventBus()
    return _BUS
