from __future__ import annotations

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
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "hi"}]
    assert "tools" not in fake.calls[0]["kwargs"]


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
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "hi"}]


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
