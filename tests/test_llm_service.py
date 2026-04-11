from __future__ import annotations

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


def test_chat_injects_tools_by_default():
    fake = FakeClient({"choices": [{"message": {"role": "assistant", "content": "hi"}}]})
    svc = LLMService(fake)  # type: ignore[arg-type]
    svc.chat("mws-gpt-alpha", [{"role": "user", "content": "hi"}])
    assert fake.calls, "ожидали вызов chat_completions"
    kwargs = fake.calls[0]["kwargs"]
    tools = kwargs.get("tools") or []
    names = [t["function"]["name"] for t in tools if t.get("type") == "function"]
    assert "web_search" in names, "web_search должен автоинжектиться"
    assert any(n.startswith("agent_") for n in names), "под-агенты должны быть в каталоге"


def test_chat_respects_explicit_empty_tools():
    fake = FakeClient({"choices": [{"message": {"role": "assistant", "content": "hi"}}]})
    svc = LLMService(fake)  # type: ignore[arg-type]
    svc.chat("m", [{"role": "user", "content": "hi"}], tools=[])
    kwargs = fake.calls[0]["kwargs"]
    assert "tools" not in kwargs


def test_run_agent_passes_tools_to_model(monkeypatch):
    monkeypatch.setattr(
        "certified_turtles.agents.loop.run_primitive_tool",
        lambda name, args: "MOCK",
    )
    final = {"choices": [{"message": {"role": "assistant", "content": "done"}}]}
    fake = FakeClient(final)
    svc = LLMService(fake)  # type: ignore[arg-type]
    out = svc.run_agent("mws-gpt-alpha", [{"role": "user", "content": "hi"}], max_tool_rounds=1)
    assert out["completion"] is final
    kwargs = fake.calls[0]["kwargs"]
    assert "tools" in kwargs
    names = [t["function"]["name"] for t in kwargs["tools"]]
    assert "web_search" in names
