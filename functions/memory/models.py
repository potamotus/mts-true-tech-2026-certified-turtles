from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


VALID_CATEGORIES = {"project", "preference", "contact", "decision", "deadline", "skill", "role"}
VALID_SOURCES = {"auto_extract", "manual", "chat_digest", "file_analysis", "explicit"}


@dataclass
class MemoryEntry:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""
    content: str = ""
    category: str = "project"
    confidence: float = 0.8
    source: str = "auto_extract"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_accessed: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    access_count: int = 0
    source_chat_id: str = ""
    status: str = "active"  # active | superseded | pending
    superseded_by: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryEntry":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def days_since_created(self) -> float:
        try:
            dt = datetime.fromisoformat(self.created_at)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except (ValueError, TypeError):
            return 0

    @property
    def days_since_accessed(self) -> float:
        try:
            dt = datetime.fromisoformat(self.last_accessed)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except (ValueError, TypeError):
            return 0

    def recency_score(self) -> float:
        return math.exp(-self.days_since_created / 90)

    def importance_score(self) -> float:
        return math.log(1 + self.access_count) * math.exp(-self.days_since_accessed / 180)

    def touch(self):
        self.access_count += 1
        self.last_accessed = datetime.now(timezone.utc).isoformat()
