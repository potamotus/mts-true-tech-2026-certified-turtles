from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from certified_turtles.integrations import gpt_researcher_runner as gr


def test_run_gpt_researcher_ok_monkeypatch_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GPT_RESEARCHER_PYTHON", sys.executable)

    def _fake_run(cmd: list, **kwargs: object) -> MagicMock:
        assert "gpt_researcher_worker.py" in cmd[-1] or any("gpt_researcher_worker" in str(c) for c in cmd)
        r = MagicMock()
        r.returncode = 0
        r.stdout = json.dumps({"ok": True, "report": "REPORT_TEXT"}, ensure_ascii=False) + "\n"
        r.stderr = ""
        return r

    monkeypatch.setattr(subprocess, "run", _fake_run)
    out = gr.run_gpt_researcher_sync_with_meta("тестовый запрос")
    assert out == {"ok": True, "report": "REPORT_TEXT"}


def test_run_gpt_researcher_missing_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GPT_RESEARCHER_PYTHON", raising=False)
    monkeypatch.setattr(gr, "_repo_root", lambda: Path("/nonexistent/absolute/repo"))
    out = gr.run_gpt_researcher_sync_with_meta("x")
    assert out["ok"] is False
    assert out.get("error")


def test_run_gpt_researcher_bad_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GPT_RESEARCHER_PYTHON", sys.executable)

    def _fake_run(*a: object, **k: object) -> MagicMock:
        r = MagicMock()
        r.returncode = 1
        r.stdout = ""
        r.stderr = "worker crashed"
        return r

    monkeypatch.setattr(subprocess, "run", _fake_run)
    out = gr.run_gpt_researcher_sync_with_meta("q")
    assert out["ok"] is False
