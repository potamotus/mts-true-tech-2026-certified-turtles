from __future__ import annotations

from typing import Any

from certified_turtles.services.llm import LLMService, clamp_agent_tool_rounds


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


def test_chat_plain_skips_tools():
    captured: dict[str, Any] = {}

    class Fake:
        def chat_completions(self, model: str, messages: list, **kwargs):
            captured["kwargs"] = kwargs
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    svc = LLMService(Fake())  # type: ignore[arg-type]
    svc.chat_plain("m", [{"role": "user", "content": "x"}])
    assert "tools" not in captured["kwargs"]


def test_chat_respects_explicit_empty_tools():
    fake = FakeClient({"choices": [{"message": {"role": "assistant", "content": "hi"}}]})
    svc = LLMService(fake)  # type: ignore[arg-type]
    svc.chat("m", [{"role": "user", "content": "hi"}], tools=[])
    kwargs = fake.calls[0]["kwargs"]
    assert "tools" not in kwargs


def test_clamp_agent_tool_rounds():
    assert clamp_agent_tool_rounds(1) == 1
    assert clamp_agent_tool_rounds(40) == 40
    assert clamp_agent_tool_rounds(999) == 40
    assert clamp_agent_tool_rounds(0) == 1
    assert clamp_agent_tool_rounds("nope") == 10
    assert clamp_agent_tool_rounds(15.7) == 15


def test_run_agent_json_protocol_no_openai_tools_kwarg(monkeypatch):
    monkeypatch.setattr(
        "certified_turtles.agents.loop.run_primitive_tool",
        lambda name, args: "MOCK",
    )
    final = {"choices": [{"message": {"role": "assistant", "content": "done"}}]}
    fake = FakeClient(final)
    svc = LLMService(fake)  # type: ignore[arg-type]
    out = svc.run_agent("mws-gpt-alpha", [{"role": "user", "content": "hi"}], max_tool_rounds=1)
    assert out["completion"]["choices"][0]["message"]["content"] == "done"
    kwargs = fake.calls[0]["kwargs"]
    assert "tools" not in kwargs


def test_run_agent_clamps_rounds_before_loop(monkeypatch):
    seen: dict[str, int] = {}

    def fake_run_agent_chat(client, model, messages, *, max_tool_rounds: int = 10, **kwargs):
        seen["max_tool_rounds"] = max_tool_rounds
        return {
            "messages": messages,
            "completion": {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            "tool_rounds_used": 1,
            "truncated": False,
        }

    monkeypatch.setattr("certified_turtles.services.llm.run_agent_chat", fake_run_agent_chat)
    fake = FakeClient({"choices": [{"message": {"role": "assistant", "content": "x"}}]})
    svc = LLMService(fake)  # type: ignore[arg-type]
    svc.run_agent("mws-gpt-alpha", [{"role": "user", "content": "hi"}], max_tool_rounds=500)
    assert seen["max_tool_rounds"] == 40
