"""LLM-based verdict structures and verification prompt for memory quality judges.

Verification is performed externally by Claude Opus agents (via Claude Code Agent tool).
This module defines the data structures and the judge prompt template.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

JUDGE_SYSTEM_PROMPT = """\
You are a memory quality judge. For each test case, evaluate whether the
MWS GPT memory system correctly handled the user's message.

For each case you receive:
- scenario: category, user message, expected behavior (should_save, expected_type, keywords)
- actual result: what files were saved, their content

Evaluate:
1. was_correct (bool): Did the system do the right thing?
   - If should_save=true: at least one relevant memory file was created
   - If should_save=false: no memory files were created
2. type_correct (bool): Is the memory type (user/feedback/project/reference) appropriate?
   Only relevant when should_save=true and expected_type is not null.
   Set to true if expected_type is null.
3. content_quality (int 1-5): How accurate and useful is the saved content?
   - 5: Perfect capture of the user's intent
   - 4: Good, minor wording issues
   - 3: Acceptable but missing nuance
   - 2: Partially wrong or misleading
   - 1: Completely wrong or useless
   Only relevant when should_save=true. Set to 5 for correct negative cases.
4. issue (str | null): Describe any problem, or null if everything is fine.

Output a JSON array of verdict objects, one per scenario, in the same order as input.
Each verdict: {"scenario_id": str, "was_correct": bool, "type_correct": bool, "content_quality": int, "issue": str | null}
Output ONLY the JSON array, no other text.
"""


@dataclass
class Verdict:
    scenario_id: str
    was_correct: bool
    type_correct: bool
    content_quality: int  # 1-5
    issue: str | None = None
    judge_id: int | None = None  # which judge produced this

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Verdict:
        return cls(
            scenario_id=d["scenario_id"],
            was_correct=d["was_correct"],
            type_correct=d["type_correct"],
            content_quality=d["content_quality"],
            issue=d.get("issue"),
            judge_id=d.get("judge_id"),
        )


@dataclass
class AggregatedVerdict:
    """Majority-vote verdict from multiple judges."""

    scenario_id: str
    was_correct: bool  # majority vote
    type_correct: bool
    avg_quality: float
    issues: list[str]
    judge_verdicts: list[Verdict] = field(default_factory=list)
    disputed: bool = False  # True if judges disagree on was_correct

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["judge_verdicts"] = [v.to_dict() for v in self.judge_verdicts]
        return d


def aggregate_verdicts(
    verdicts_by_judge: list[list[Verdict]],
) -> dict[str, AggregatedVerdict]:
    """Aggregate verdicts from multiple judges using majority vote.

    Args:
        verdicts_by_judge: list of verdict lists, one per judge

    Returns:
        dict mapping scenario_id to AggregatedVerdict
    """
    # Group by scenario_id
    by_scenario: dict[str, list[Verdict]] = {}
    for judge_verdicts in verdicts_by_judge:
        for v in judge_verdicts:
            by_scenario.setdefault(v.scenario_id, []).append(v)

    results: dict[str, AggregatedVerdict] = {}
    for sid, verdicts in by_scenario.items():
        correct_votes = sum(1 for v in verdicts if v.was_correct)
        type_votes = sum(1 for v in verdicts if v.type_correct)
        total = len(verdicts)
        majority = total / 2

        was_correct = correct_votes > majority
        type_correct = type_votes > majority
        avg_quality = sum(v.content_quality for v in verdicts) / total
        issues = [v.issue for v in verdicts if v.issue]
        disputed = 0 < correct_votes < total  # not unanimous

        results[sid] = AggregatedVerdict(
            scenario_id=sid,
            was_correct=was_correct,
            type_correct=type_correct,
            avg_quality=avg_quality,
            issues=issues,
            judge_verdicts=verdicts,
            disputed=disputed,
        )

    return results


def build_judge_input(
    scenarios: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
) -> str:
    """Build the input text for a judge, given scenarios and their results."""
    cases = []
    for s in scenarios:
        sid = s["id"]
        result = results.get(sid, {})
        cases.append({
            "scenario_id": sid,
            "category": s["category"],
            "user_messages": [m["content"] for m in s["messages"] if m["role"] == "user"],
            "should_save": s["should_save"],
            "expected_memory_type": s["expected_memory_type"],
            "keywords": s["keywords"],
            "description": s["description"],
            "actual_saved_files": result.get("saved_files", []),
        })
    return json.dumps(cases, ensure_ascii=False, indent=2)
