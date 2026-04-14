"""
Запуск [GPT Researcher](https://github.com/assafelovic/gpt-researcher) в **отдельном venv** (subprocess),
чтобы не смешивать зависимости с основным приложением.

1. Один раз: `bash scripts/bootstrap_gpt_researcher_venv.sh`
2. По умолчанию используется `<repo>/.venv-gpt-researcher/bin/python`
3. Или задайте `GPT_RESEARCHER_PYTHON=/path/to/python`
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _worker_script() -> Path:
    return _repo_root() / "scripts" / "gpt_researcher_worker.py"


def _resolve_python() -> str | None:
    env = os.environ.get("GPT_RESEARCHER_PYTHON", "").strip()
    if env and Path(env).is_file():
        return env
    candidate = _repo_root() / ".venv-gpt-researcher" / "bin" / "python"
    if candidate.is_file():
        return str(candidate)
    return None


def run_gpt_researcher_sync_with_meta(query: str, *, report_type: str = "research_report") -> dict[str, Any]:
    py = _resolve_python()
    worker = _worker_script()
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "empty_query"}
    if not py:
        return {
            "ok": False,
            "error": (
                "Не найден интерпретатор GPT Researcher. Выполните: "
                "bash scripts/bootstrap_gpt_researcher_venv.sh "
                "или задайте GPT_RESEARCHER_PYTHON."
            ),
        }
    if not worker.is_file():
        return {"ok": False, "error": f"Не найден воркер: {worker}"}

    payload = json.dumps({"query": q, "report_type": report_type}, ensure_ascii=False)
    env = os.environ.copy()
    try:
        proc = subprocess.run(
            [py, str(worker)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("GPT_RESEARCHER_TIMEOUT_SEC", "3600")),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "gpt_researcher_timeout"}
    if proc.returncode != 0:
        _log.warning("gpt_researcher worker stderr: %s", proc.stderr[:2000])
        return {"ok": False, "error": proc.stderr.strip() or f"exit {proc.returncode}"}
    try:
        out = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": f"bad_json: {proc.stdout[:500]!r}"}
    if not isinstance(out, dict):
        return {"ok": False, "error": "invalid_worker_response"}
    return out


def run_gpt_researcher_sync(query: str, *, report_type: str = "research_report") -> str:
    meta = run_gpt_researcher_sync_with_meta(query, report_type=report_type)
    if meta.get("ok"):
        return str(meta.get("report") or "")
    return f"[GPT Researcher] Ошибка: {meta.get('error', 'unknown')}"
