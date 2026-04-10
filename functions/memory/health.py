from __future__ import annotations

import math
from typing import List

from .models import MemoryEntry, VALID_CATEGORIES


class MemoryHealth:
    """Compute memory health score for dashboard/demo."""

    def __init__(self, facts: List[MemoryEntry]):
        self.facts = [f for f in facts if f.status == "active"]

    def compute(self) -> dict:
        if not self.facts:
            return {
                "score": 0,
                "grade": "Poor",
                "freshness": 0,
                "coverage": 0,
                "precision": 0,
                "utilization": 0,
                "total_facts": 0,
                "categories": {},
            }

        freshness = self._freshness()
        coverage = self._coverage()
        precision = self._precision()
        utilization = self._utilization()

        score = round(
            0.35 * freshness
            + 0.25 * coverage
            + 0.20 * precision
            + 0.20 * utilization
        ) * 100

        grade = "Excellent" if score >= 80 else \
                "Good" if score >= 60 else \
                "Needs attention" if score >= 40 else "Poor"

        # Category breakdown
        cats = {}
        for f in self.facts:
            cats[f.category] = cats.get(f.category, 0) + 1

        return {
            "score": min(100, max(0, int(score))),
            "grade": grade,
            "freshness": round(freshness * 100),
            "coverage": round(coverage * 100),
            "precision": round(precision * 100),
            "utilization": round(utilization * 100),
            "total_facts": len(self.facts),
            "categories": cats,
        }

    def _freshness(self) -> float:
        if not self.facts:
            return 0
        return sum(f.recency_score() for f in self.facts) / len(self.facts)

    def _coverage(self) -> float:
        unique = {f.category for f in self.facts}
        return min(1.0, len(unique) / len(VALID_CATEGORIES))

    def _precision(self) -> float:
        """Proxy: ratio of facts with access_count > 0 (actually used)."""
        if not self.facts:
            return 0
        used = sum(1 for f in self.facts if f.access_count > 0)
        return used / len(self.facts)

    def _utilization(self) -> float:
        if not self.facts:
            return 0
        counts = [f.access_count for f in self.facts]
        max_count = max(counts)
        if max_count == 0:
            return 0
        avg_count = sum(counts) / len(counts)
        return avg_count / max_count
