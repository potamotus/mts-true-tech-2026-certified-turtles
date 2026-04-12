"""Generate REPORT.md from aggregated verdicts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .scenarios import ALL_SCENARIOS, CATEGORIES
from .verifier import AggregatedVerdict


def generate_report(
    aggregated: dict[str, AggregatedVerdict],
    model: str,
    output_path: Path | str,
) -> str:
    """Generate a markdown report and write it to output_path.

    Returns the report text.
    """
    scenarios_by_id = {s["id"]: s for s in ALL_SCENARIOS}
    total = len(aggregated)
    passed = sum(1 for v in aggregated.values() if v.was_correct and not v.disputed)
    failed = sum(1 for v in aggregated.values() if not v.was_correct)
    disputed = sum(1 for v in aggregated.values() if v.disputed and v.was_correct)

    lines: list[str] = []
    lines.append("# Memory Quality Test Report")
    lines.append(f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Model: {model}")
    lines.append(f"Total scenarios: {total}")
    lines.append(f"Passed: {passed} | Failed: {failed} | Disputed: {disputed}")
    lines.append("")

    # Failed cases
    failed_cases = [
        (sid, v) for sid, v in aggregated.items() if not v.was_correct
    ]
    if failed_cases:
        lines.append("## Failed Cases")
        lines.append("")
        for i, (sid, v) in enumerate(failed_cases, 1):
            s = scenarios_by_id.get(sid, {})
            user_msgs = " | ".join(
                m["content"] for m in s.get("messages", []) if m.get("role") == "user"
            )
            expected_type = s.get("expected_memory_type", "N/A")
            should_save = s.get("should_save", "N/A")
            saved = v.judge_verdicts[0].was_correct if v.judge_verdicts else "?"
            correct_count = sum(1 for jv in v.judge_verdicts if jv.was_correct)
            total_judges = len(v.judge_verdicts)

            lines.append(f"### FAIL-{i:03d}: {sid}")
            lines.append(f"- **Category:** {s.get('category', '?')}")
            lines.append(f"- **Message:** \"{user_msgs}\"")
            lines.append(f"- **Expected:** should_save={should_save}, type={expected_type}")
            saved_files = _get_saved_files_summary(v)
            lines.append(f"- **Actual:** {saved_files}")
            lines.append(f"- **Verdict:** {correct_count}/{total_judges} judges say correct")
            issues = "; ".join(v.issues) if v.issues else "No specific issue noted"
            lines.append(f"- **Issue:** {issues}")
            lines.append(f"- **Avg quality:** {v.avg_quality:.1f}/5")
            lines.append("")

    # Disputed cases
    disputed_cases = [
        (sid, v) for sid, v in aggregated.items() if v.disputed and v.was_correct
    ]
    if disputed_cases:
        lines.append("## Disputed Cases")
        lines.append("")
        for sid, v in disputed_cases:
            s = scenarios_by_id.get(sid, {})
            user_msgs = " | ".join(
                m["content"] for m in s.get("messages", []) if m.get("role") == "user"
            )
            correct_count = sum(1 for jv in v.judge_verdicts if jv.was_correct)
            total_judges = len(v.judge_verdicts)
            lines.append(f"### DISPUTED: {sid}")
            lines.append(f"- **Message:** \"{user_msgs}\"")
            lines.append(f"- **Split:** {correct_count}/{total_judges} say correct")
            issues = "; ".join(v.issues) if v.issues else "None"
            lines.append(f"- **Issues:** {issues}")
            lines.append("")

    # Summary by category
    lines.append("## Summary by Category")
    lines.append("")
    lines.append("| Category | Total | Pass | Fail | Disputed | Rate |")
    lines.append("|----------|-------|------|------|----------|------|")
    for cat_name in CATEGORIES:
        cat_ids = {s["id"] for s in CATEGORIES[cat_name]}
        cat_verdicts = {sid: v for sid, v in aggregated.items() if sid in cat_ids}
        cat_total = len(cat_verdicts)
        if cat_total == 0:
            continue
        cat_pass = sum(1 for v in cat_verdicts.values() if v.was_correct and not v.disputed)
        cat_fail = sum(1 for v in cat_verdicts.values() if not v.was_correct)
        cat_disputed = sum(1 for v in cat_verdicts.values() if v.disputed and v.was_correct)
        rate = f"{cat_pass / cat_total * 100:.0f}%" if cat_total else "N/A"
        lines.append(f"| {cat_name} | {cat_total} | {cat_pass} | {cat_fail} | {cat_disputed} | {rate} |")
    lines.append("")

    report_text = "\n".join(lines)
    Path(output_path).write_text(report_text, encoding="utf-8")
    return report_text


def _get_saved_files_summary(v: AggregatedVerdict) -> str:
    """Summarize what was actually saved (from the first judge's perspective)."""
    # This is a simplified summary — the full data is in results.json
    if v.was_correct:
        return "correctly handled"
    return "incorrectly handled (see issues)"


def generate_results_json(
    results: dict[str, dict[str, Any]],
    output_path: Path | str,
) -> None:
    """Write raw results (before verification) to JSON."""
    Path(output_path).write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
