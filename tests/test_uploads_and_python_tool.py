from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import certified_turtles.tools.builtins  # noqa: F401 - регистрация тулов
from certified_turtles.main import app
from certified_turtles.tools.registry import run_primitive_tool


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "up"))
    monkeypatch.setenv("GENERATED_FILES_DIR", str(tmp_path / "gen"))
    return TestClient(app)


def test_upload_then_read_workspace_file(client):
    r = client.post("/api/v1/uploads", files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 200, r.text
    fid = r.json()["file_id"]
    raw = run_primitive_tool("read_workspace_file", {"file_id": fid})
    data = json.loads(raw)
    assert "1,2" in data["content"]


def test_execute_python_simple_stdout(client, monkeypatch, tmp_path):
    monkeypatch.setenv("GENERATED_FILES_DIR", str(tmp_path / "gen"))
    raw = run_primitive_tool("execute_python", {"code": "print(2 + 2)"})
    data = json.loads(raw)
    assert data.get("returncode") == 0
    assert "4" in (data.get("stdout") or "")


def test_execute_python_rejects_bad_import(client, monkeypatch, tmp_path):
    monkeypatch.setenv("GENERATED_FILES_DIR", str(tmp_path / "gen"))
    raw = run_primitive_tool("execute_python", {"code": "import os\nprint(os.name)"})
    data = json.loads(raw)
    assert data.get("error") == "validation_failed"
