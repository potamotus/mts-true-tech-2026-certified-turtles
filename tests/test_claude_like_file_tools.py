from __future__ import annotations

import json

from certified_turtles.memory_runtime import RequestContext, use_request_context
from certified_turtles.tools.registry import run_primitive_tool


def test_file_write_requires_read_for_existing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CT_CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("GENERATED_FILES_DIR", str(tmp_path / "generated"))
    path = tmp_path / "claude" / "projects" / "demo" / "memory" / "note.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("old", encoding="utf-8")

    with use_request_context(RequestContext(session_id="s1", scope_id="demo")):
        raw = run_primitive_tool("file_write", {"file_path": str(path), "content": "new"})
    data = json.loads(raw)
    assert data["error"] == "read_before_write_required"


def test_file_edit_after_full_read_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("CT_CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("GENERATED_FILES_DIR", str(tmp_path / "generated"))
    path = tmp_path / "claude" / "projects" / "demo" / "memory" / "note.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("hello world", encoding="utf-8")

    with use_request_context(RequestContext(session_id="s1", scope_id="demo")):
        read_raw = run_primitive_tool("file_read", {"file_path": str(path)})
        edit_raw = run_primitive_tool(
            "file_edit",
            {"file_path": str(path), "old_string": "world", "new_string": "memory"},
        )

    assert json.loads(read_raw)["truncated"] is False
    assert json.loads(edit_raw)["ok"] is True
    assert path.read_text(encoding="utf-8") == "hello memory"


def test_partial_read_blocks_write(tmp_path, monkeypatch):
    monkeypatch.setenv("CT_CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("GENERATED_FILES_DIR", str(tmp_path / "generated"))
    path = tmp_path / "claude" / "projects" / "demo" / "memory" / "note.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("a\nb\nc\n", encoding="utf-8")

    with use_request_context(RequestContext(session_id="s1", scope_id="demo")):
        run_primitive_tool("file_read", {"file_path": str(path), "offset": 0, "limit": 1})
        write_raw = run_primitive_tool("file_write", {"file_path": str(path), "content": "replaced"})

    data = json.loads(write_raw)
    assert data["error"] == "partial_read_not_enough"


def test_file_read_returns_unchanged_stub_on_repeat(tmp_path, monkeypatch):
    monkeypatch.setenv("CT_CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("GENERATED_FILES_DIR", str(tmp_path / "generated"))
    path = tmp_path / "claude" / "projects" / "demo" / "memory" / "note.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("same", encoding="utf-8")

    with use_request_context(RequestContext(session_id="s1", scope_id="demo")):
        first = json.loads(run_primitive_tool("file_read", {"file_path": str(path)}))
        second = json.loads(run_primitive_tool("file_read", {"file_path": str(path)}))

    assert ("unchanged" not in first) or (first["unchanged"] is False)
    assert second["unchanged"] is True
    assert second["content"] == "[FILE_UNCHANGED_STUB]"


def test_file_write_preserves_crlf_from_read_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CT_CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("GENERATED_FILES_DIR", str(tmp_path / "generated"))
    path = tmp_path / "claude" / "projects" / "demo" / "memory" / "note.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"a\r\nb\r\n")

    with use_request_context(RequestContext(session_id="s1", scope_id="demo")):
        run_primitive_tool("file_read", {"file_path": str(path)})
        run_primitive_tool("file_write", {"file_path": str(path), "content": "x\ny\n"})

    assert path.read_bytes() == b"x\r\ny\r\n"
