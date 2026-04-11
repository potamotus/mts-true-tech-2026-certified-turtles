from __future__ import annotations

import json

from fastapi.testclient import TestClient

from certified_turtles.main import app
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


def test_openai_proxy_chat_completions_non_stream(monkeypatch):
    final = {
        "id": "x",
        "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
    }
    fake = FakeClient(final)
    _patch_service(monkeypatch, fake)

    client = TestClient(app)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "mws-gpt-alpha", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "hello"
    # Агент-цикл: тулы описаны в системном JSON-протоколе, в kwargs MWS не передаём OpenAI tools.
    kwargs = fake.calls[0]["kwargs"]
    assert "tools" not in kwargs
    sys0 = fake.calls[0]["messages"][0]["content"]
    assert isinstance(sys0, str)
    assert "assistant_markdown" in sys0
    assert "web_search" in sys0


def test_openai_proxy_chat_completions_stream(monkeypatch):
    final = {
        "choices": [{"message": {"role": "assistant", "content": "streamed"}, "finish_reason": "stop"}],
    }
    fake = FakeClient(final)
    _patch_service(monkeypatch, fake)

    client = TestClient(app)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "mws-gpt-alpha",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert r.status_code == 200
    text = r.text
    assert "data:" in text
    assert "streamed" in text
    assert "[DONE]" in text


def test_openai_proxy_list_models(monkeypatch):
    fake = FakeClient({})
    _patch_service(monkeypatch, fake)

    client = TestClient(app)
    r = client.get("/v1/models")
    assert r.status_code == 200
    assert r.json()["data"][0]["id"] == "mws-gpt-alpha"


def test_openai_proxy_plain_models_alias(monkeypatch):
    fake = FakeClient({})
    _patch_service(monkeypatch, fake)

    client = TestClient(app)
    r = client.get("/v1/plain/models")
    assert r.status_code == 200
    assert r.json()["data"][0]["id"] == "mws-gpt-alpha"


def test_openai_proxy_plain_chat_via_use_agent_false(monkeypatch):
    final = {
        "choices": [{"message": {"role": "assistant", "content": "plain"}, "finish_reason": "stop"}],
    }
    fake = FakeClient(final)
    _patch_service(monkeypatch, fake)

    client = TestClient(app)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "mws-gpt-alpha",
            "messages": [{"role": "user", "content": "hi"}],
            "use_agent": False,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "plain"
    assert fake.calls[0]["messages"][-1] == {"role": "user", "content": "hi"}
    assert fake.calls[0]["messages"][0]["role"] == "system"
    assert "# memory" in fake.calls[0]["messages"][0]["content"]
    assert "tools" not in fake.calls[0]["kwargs"]


def test_openwebui_auxiliary_task_forces_plain(monkeypatch):
    """Служебные промпты UI (заголовок, follow-up …) — без агента, иначе JSON-протокол ломается."""
    final = {
        "choices": [{"message": {"role": "assistant", "content": "Title OK"}, "finish_reason": "stop"}],
    }
    fake = FakeClient(final)
    _patch_service(monkeypatch, fake)

    client = TestClient(app)
    meta = (
        "### Task: Generate a concise, 3-5 word title with an emoji summarizing the chat history.\n"
        "### Guidelines:\n- Use emoji."
    )
    r = client.post(
        "/v1/chat/completions",
        json={"model": "mws-gpt-alpha", "messages": [{"role": "user", "content": meta}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "Title OK"
    assert fake.calls[0]["messages"][-1] == {"role": "user", "content": meta}
    assert fake.calls[0]["messages"][0]["role"] == "system"
    assert "tools" not in fake.calls[0]["kwargs"]


def test_openwebui_rag_task_still_uses_agent(monkeypatch):
    """Основной RAG-промпт с <source> — по-прежнему агент (тулы + гидратация)."""
    proto = json.dumps({"assistant_markdown": "готово", "calls": []}, ensure_ascii=False)
    final = {
        "choices": [{"message": {"role": "assistant", "content": proto}, "finish_reason": "stop"}],
    }
    agent_hits: list[int] = []

    def cap_run_agent_chat(client, model, messages, *, max_tool_rounds: int = 10, **kwargs):
        agent_hits.append(len(messages))
        return {
            "messages": messages,
            "completion": final,
            "tool_rounds_used": 1,
            "truncated": False,
        }

    monkeypatch.setattr("certified_turtles.services.llm.run_agent_chat", cap_run_agent_chat)
    fake = FakeClient({})
    _patch_service(monkeypatch, fake)

    client = TestClient(app)
    rag = (
        "### Task: Respond to the user query using the provided context.\n\n"
        '<source id="1" name="x.csv">a,b\n1,2</source>\n\n'
        "Query: привет"
    )
    r = client.post(
        "/v1/chat/completions",
        json={"model": "mws-gpt-alpha", "messages": [{"role": "user", "content": rag}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "готово"
    assert len(agent_hits) == 1


def test_openai_proxy_plain_dedicated_url(monkeypatch):
    final = {
        "choices": [{"message": {"role": "assistant", "content": "plain2"}, "finish_reason": "stop"}],
    }
    fake = FakeClient(final)
    _patch_service(monkeypatch, fake)

    client = TestClient(app)
    r = client.post(
        "/v1/plain/chat/completions",
        json={"model": "mws-gpt-alpha", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "plain2"
    assert fake.calls[0]["messages"][-1] == {"role": "user", "content": "hi"}
    assert fake.calls[0]["messages"][0]["role"] == "system"
    assert "# memory" in fake.calls[0]["messages"][0]["content"]


def test_openai_proxy_chat_clamps_max_tool_rounds(monkeypatch):
    final = {
        "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
    }
    captured: dict[str, int] = {}

    def cap_run_agent_chat(client, model, messages, *, max_tool_rounds: int = 10, **kwargs):
        captured["max_tool_rounds"] = max_tool_rounds
        return {
            "messages": messages,
            "completion": final,
            "tool_rounds_used": 1,
            "truncated": False,
        }

    monkeypatch.setattr("certified_turtles.services.llm.run_agent_chat", cap_run_agent_chat)
    fake = FakeClient({})
    _patch_service(monkeypatch, fake)

    client = TestClient(app)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "mws-gpt-alpha",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tool_rounds": 9999,
        },
    )
    assert r.status_code == 200, r.text
    assert captured["max_tool_rounds"] == 40


def test_openai_proxy_unwraps_json_protocol_in_completion(monkeypatch):
    """Если агент вернул completion с сырым JSON-протоколом, API отдаёт только markdown."""
    proto = json.dumps({"assistant_markdown": "**видно**", "calls": []}, ensure_ascii=False)
    final = {
        "choices": [{"message": {"role": "assistant", "content": proto}, "finish_reason": "stop"}],
    }

    def cap_run_agent_chat(client, model, messages, *, max_tool_rounds: int = 10, **kwargs):
        return {
            "messages": messages,
            "completion": final,
            "tool_rounds_used": 1,
            "truncated": False,
        }

    monkeypatch.setattr("certified_turtles.services.llm.run_agent_chat", cap_run_agent_chat)
    fake = FakeClient({})
    _patch_service(monkeypatch, fake)

    client = TestClient(app)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "mws-gpt-alpha", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "**видно**"
