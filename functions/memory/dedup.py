from __future__ import annotations

import math
from typing import List, Optional

from .models import MemoryEntry


class DedupChecker:
    """Near-duplicate detection via string similarity.

    Uses a lightweight n-gram Jaccard similarity instead of vector search
    to avoid LLM/API calls for dedup — skip the LLM when possible.
    """

    def __init__(self, threshold: float = 0.85):
        self.threshold = threshold
        self._n = 3  # trigram

    def _char_ngrams(self, text: str) -> set:
        t = text.lower().strip()
        if len(t) < self._n:
            return {t}
        return {t[i : i + self._n] for i in range(len(t) - self._n + 1)}

    def jaccard(self, a: str, b: str) -> float:
        na, nb = self._char_ngrams(a), self._char_ngrams(b)
        if not na and not nb:
            return 1.0
        if not na or not nb:
            return 0.0
        return len(na & nb) / len(na | nb)

    def find_duplicate(
        self, new_content: str, existing: List[MemoryEntry]
    ) -> Optional[MemoryEntry]:
        for entry in existing:
            if self.jaccard(new_content, entry.content) >= self.threshold:
                return entry
        return None


class ConflictResolver:
    """Detect and resolve contradictory facts.

    Conflicts = same category + high similarity + contradictory keywords.
    Old facts are marked superseded, not deleted (audit trail).
    """

    CONTRADICTORY_PAIRS = [
        ("не ", "да"),
        ("нет", "да"),
        ("не знаю", "знаю"),
        ("отменили", "решили"),
        ("перенесли", "назначили"),
    ]

    def find_conflicts(
        self, new_fact: MemoryEntry, existing: List[MemoryEntry]
    ) -> List[MemoryEntry]:
        conflicts = []
        for entry in existing:
            if entry.status != "active":
                continue
            if entry.category != new_fact.category:
                continue
            # Same category + moderate similarity = potential conflict
            checker = DedupChecker(threshold=0.5)
            if checker.jaccard(new_fact.content, entry.content) > 0.5:
                # Check for contradictory keywords
                new_lower = new_fact.content.lower()
                old_lower = entry.content.lower()
                for neg, pos in self.CONTRADICTORY_PAIRS:
                    if (neg in new_lower and pos in old_lower) or (
                        pos in new_lower and neg in old_lower
                    ):
                        conflicts.append(entry)
                        break
        return conflicts

    def resolve(self, new_fact: MemoryEntry, conflicts: List[MemoryEntry]):
        for old in conflicts:
            old.status = "superseded"
            old.superseded_by = new_fact.id
            old.updated_at = new_fact.updated_at
