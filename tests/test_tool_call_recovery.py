from __future__ import annotations

import json

from certified_turtles.agents.loop import run_agent_chat
from certified_turtles.agents.tool_call_recovery import recover_tool_calls_from_assistant_message
from certified_turtles.tools.registry import openai_tools_for_names


def test_recovery_fenced_json_wrong_type_field():
    body = json.dumps(
        {
            "tool_calls": [
                {
                    "id": "call_x",
                    "type": "mws_list_models",
                    "arguments": {},
                }
            ]
        },
        ensure_ascii=False,
    )
    text = f"Сначала текст.\n```json\n{body}\n```\nКонец."
    calls, new_c = recover_tool_calls_from_assistant_message(
        {"role": "assistant", "content": text},
        {"mws_list_models", "execute_python"},
    )
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "mws_list_models"
    assert calls[0]["type"] == "function"
    assert new_c is not None
    assert "tool_calls" not in new_c


def test_recovery_ignores_unknown_tools():
    body = json.dumps(
        {"tool_calls": [{"type": "function", "function": {"name": "not_a_real_tool", "arguments": "{}"}}]},
        ensure_ascii=False,
    )
    calls, _ = recover_tool_calls_from_assistant_message(
        {"role": "assistant", "content": f"```json\n{body}\n```"},
        {"mws_list_models"},
    )
    assert calls == []


def test_recovery_execute_python_with_code_field():
    body = json.dumps(
        {
            "tool_calls": [
                {
                    "type": "execute_python",
                    "code": "print(1)",
                }
            ]
        },
        ensure_ascii=False,
    )
    calls, _ = recover_tool_calls_from_assistant_message(
        {"role": "assistant", "content": f"```json\n{body}\n```"},
        {"execute_python"},
    )
    assert len(calls) == 1
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["code"] == "print(1)"


class _FakeClientRecovery:
    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[int] = []

    def chat_completions(self, model: str, messages: list, **kwargs):
        self.calls.append(len(messages))
        return self._responses.pop(0)


def test_agent_loop_runs_tools_after_text_only_tool_json(monkeypatch):
    monkeypatch.setattr("certified_turtles.agents.loop.run_primitive_tool", lambda n, a: "LIST_OK")

    payload = json.dumps(
        {"tool_calls": [{"id": "t1", "type": "mws_list_models", "arguments": {}}]},
        ensure_ascii=False,
    )
    r1 = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": f"Вызов:\n```json\n{payload}\n```",
                },
                "finish_reason": "stop",
            }
        ]
    }
    r2 = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Модели получены."},
                "finish_reason": "stop",
            }
        ]
    }
    fake = _FakeClientRecovery([r1, r2])
    tools = openai_tools_for_names(("mws_list_models",))
    out = run_agent_chat(
        fake,
        "m",
        [{"role": "user", "content": "какие модели"}],
        tools=tools,
        max_tool_rounds=5,
    )
    assert out["truncated"] is False
    assert out["tool_rounds_used"] == 2
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
