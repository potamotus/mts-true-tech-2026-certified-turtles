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
    # Главное: в исходящий запрос к модели подставились тулы.
    kwargs = fake.calls[0]["kwargs"]
    assert "tools" in kwargs and kwargs["tools"]
    names = [t["function"]["name"] for t in kwargs["tools"]]
    assert "web_search" in names


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
