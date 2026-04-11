from __future__ import annotations

from fastapi.testclient import TestClient

from certified_turtles.main import app
from certified_turtles.memory_runtime.storage import read_transcript_events, scan_memory_headers, write_memory_file, write_session_memory
from certified_turtles.services.llm import LLMService


class FakeClient:
    def __init__(self, response: dict):
        self._response = response
        self.calls: list[dict] = []

    def chat_completions(self, model: str, messages: list, **kwargs):
        self.calls.append({"model": model, "messages": messages, "kwargs": kwargs})
        return self._response

    def list_models(self):
        return {"data": [{"id": "mws-gpt-alpha"}]}


def _patch_service(monkeypatch, fake: FakeClient) -> None:
    monkeypatch.setattr(
        LLMService,
        "from_env",
        classmethod(lambda cls: cls(fake)),  # type: ignore[arg-type]
    )


def test_openai_proxy_injects_auto_memory_and_writes_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("CT_CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("GENERATED_FILES_DIR", str(tmp_path / "generated"))
    write_memory_file(
        "demo-scope",
        name="user_pref",
        description="Prefers concise answers",
        type_="user",
        body="User prefers concise answers and terse summaries.",
    )
    write_session_memory("chat-1", "# Current State\nWorking on Claude-like memory runtime.")
    final = {
        "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
    }
    fake = FakeClient(final)
    _patch_service(monkeypatch, fake)

    client = TestClient(app)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "mws-gpt-alpha",
            "ct_session_id": "chat-1",
            "ct_scope_id": "demo-scope",
            "messages": [{"role": "user", "content": "напомни мой стиль ответов"}],
        },
    )
    assert r.status_code == 200, r.text
    sys0 = next(
        call["messages"][0]["content"]
        for call in fake.calls
        if call["messages"] and call["messages"][0].get("role") == "system" and "# memory" in call["messages"][0].get("content", "")
    )
    assert "# memory" in sys0
    assert "MEMORY.md" in sys0
    assert "# session_memory" in sys0
    transcript = read_transcript_events("chat-1")
    assert transcript
    assert any(item.get("role") == "user" for item in transcript)


def test_recursive_memory_scan_surfaces_nested_topic_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CT_CLAUDE_HOME", str(tmp_path / "claude"))
    write_memory_file(
        "demo-scope",
        name="nested ref",
        description="Nested memory entry",
        type_="reference",
        body="URL and dashboard references.",
        filename="team/nested_ref.md",
    )
    headers = scan_memory_headers("demo-scope")
    filenames = [h.filename for h in headers]
    assert "team/nested_ref.md" in filenames
