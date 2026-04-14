#!/usr/bin/env python3
"""
Воркер для subprocess: читает JSON из stdin { "query": "...", "report_type": "..." }.
Пишет JSON в stdout { "ok": true, "report": "..." } или { "ok": false, "error": "..." }.

Запускайте интерпретатором из venv после scripts/bootstrap_gpt_researcher_venv.sh.
Upstream: https://github.com/assafelovic/gpt-researcher (PyPI: gpt-researcher).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys


def _apply_env() -> None:
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("MWS_API_KEY") or os.environ.get("MWS_GPT_API_KEY")
    if key and not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = key
    base = os.environ.get("OPENAI_BASE_URL") or os.environ.get("MWS_API_BASE")
    if base and not os.environ.get("OPENAI_BASE_URL"):
        os.environ["OPENAI_BASE_URL"] = base.rstrip("/")
    if not os.environ.get("TAVILY_API_KEY") and not os.environ.get("RETRIEVER"):
        os.environ.setdefault("RETRIEVER", "duckduckgo")


async def _run() -> dict[str, object]:
    raw = sys.stdin.read()
    req = json.loads(raw) if raw.strip() else {}
    query = str(req.get("query") or "").strip()
    report_type = str(req.get("report_type") or "research_report").strip()
    if not query:
        return {"ok": False, "error": "empty_query"}
    _apply_env()
    from gpt_researcher import GPTResearcher  # noqa: WPS433 — только внутри изолированного venv

    researcher = GPTResearcher(query=query, report_type=report_type)
    await researcher.conduct_research()
    report = await researcher.write_report()
    return {"ok": True, "report": report if isinstance(report, str) else str(report)}


def main() -> None:
    try:
        out = asyncio.run(_run())
    except Exception as e:
        out = {"ok": False, "error": str(e)}
    json.dump(out, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
