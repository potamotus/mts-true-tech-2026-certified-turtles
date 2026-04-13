from __future__ import annotations

import copy
import json

from certified_turtles.agents.loop import _parent_dialog_snippet, run_agent_chat
from certified_turtles.tools.registry import openai_tools_for_names


class FakeMWSClient:
    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def chat_completions(self, model: str, messages: list, **kwargs):
        self.calls.append(
            {
                "model": model,
                "n_msg": len(messages),
                "messages": copy.deepcopy(messages),
                "has_tools": "tools" in kwargs,
            }
        )
        if not self._responses:
            raise RuntimeError("unexpected extra chat_completions call")
        return self._responses.pop(0)


def test_agent_finishes_after_tool_round(monkeypatch):
    monkeypatch.setattr("certified_turtles.agents.loop.run_primitive_tool", lambda name, args: "MOCK_RESULT")
    r1 = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "assistant_markdown": "",
                            "calls": [{"name": "web_search", "arguments": {"query": "x"}}],
                        },
                        ensure_ascii=False,
                    ),
                },
                "finish_reason": "stop",
            }
        ]
    }
    r2 = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {"assistant_markdown": "Финал", "calls": []},
                        ensure_ascii=False,
                    ),
                },
                "finish_reason": "stop",
            }
        ]
    }
    fake = FakeMWSClient([r1, r2])
    out = run_agent_chat(fake, "mws-gpt-alpha", [{"role": "user", "content": "hi"}], max_tool_rounds=5)
    assert out["truncated"] is False
    assert out["tool_rounds_used"] == 2
    assert fake.calls[0]["has_tools"] is False
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["system", "user", "assistant", "user", "assistant"]
    assert out["completion"]["choices"][0]["message"]["content"] == "Финал"


def test_agent_truncates_when_token_budget_exhausted(monkeypatch):
    monkeypatch.setattr("certified_turtles.agents.loop.run_primitive_tool", lambda n, a: "x")
    r_tool = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "assistant_markdown": "",
                            "calls": [{"name": "web_search", "arguments": {}}],
                        },
                        ensure_ascii=False,
                    ),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"total_tokens": 5000},
    }
    fake = FakeMWSClient([r_tool])
    out = run_agent_chat(fake, "m", [{"role": "user", "content": "hi"}], max_agent_tokens=1000)
    assert out["truncated"] is True
    assert out["tool_rounds_used"] == 1


def test_protocol_system_is_first_and_lists_execute_python(monkeypatch):
    monkeypatch.setattr("certified_turtles.agents.loop.run_primitive_tool", lambda n, a: "x")
    final = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {"assistant_markdown": "ok", "calls": []},
                        ensure_ascii=False,
                    ),
                }
            }
        ]
    }
    fake = FakeMWSClient([final])
    tools = openai_tools_for_names(("execute_python",))
    run_agent_chat(
        fake,
        "m",
        [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "hi"}],
        tools=tools,
        max_tool_rounds=1,
    )
    sent = fake.calls[0]["messages"]
    assert sent[0]["role"] == "system"
    proto = sent[0]["content"]
    assert "assistant_markdown" in proto
    assert "execute_python" in proto
    assert "You are helpful." in proto
    assert sent[1]["role"] == "user"


def test_protocol_system_lists_web_search_only(monkeypatch):
    monkeypatch.setattr("certified_turtles.agents.loop.run_primitive_tool", lambda n, a: "x")
    final = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {"assistant_markdown": "ok", "calls": []},
                        ensure_ascii=False,
                    ),
                }
            }
        ]
    }
    fake = FakeMWSClient([final])
    tools = openai_tools_for_names(("web_search",))
    run_agent_chat(fake, "m", [{"role": "user", "content": "hi"}], tools=tools, max_tool_rounds=1)
    sent = fake.calls[0]["messages"]
    assert len(sent) == 2
    assert sent[0]["role"] == "system"
    assert "assistant_markdown" in sent[0]["content"]
    assert "web_search" in sent[0]["content"]
    assert "plt.savefig" not in sent[0]["content"]


def test_execute_python_autobinds_file_id_from_workspace_tool(monkeypatch):
    calls_seen: list[tuple[str, dict]] = []

    def fake_tool(name: str, args: dict):
        calls_seen.append((name, copy.deepcopy(args)))
        if name == "workspace_file_path":
            return json.dumps(
                {
                    "file_id": "abc_data.csv",
                    "absolute_path": "/tmp/abc_data.csv",
                    "suffix": ".csv",
                },
                ensure_ascii=False,
            )
        if name == "execute_python":
            return json.dumps({"returncode": 0, "stdout": "ok", "stderr": ""}, ensure_ascii=False)
        return "x"

    monkeypatch.setattr("certified_turtles.agents.loop.run_primitive_tool", fake_tool)
    r1 = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "assistant_markdown": "",
                            "calls": [
                                {"name": "workspace_file_path", "arguments": {"file_id": "abc_data.csv"}},
                                {"name": "execute_python", "arguments": {"code": "print('x')"}},
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
                "finish_reason": "stop",
            }
        ]
    }
    r2 = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"assistant_markdown": "Готово", "calls": []}, ensure_ascii=False),
                },
                "finish_reason": "stop",
            }
        ]
    }
    fake = FakeMWSClient([r1, r2])
    out = run_agent_chat(fake, "m", [{"role": "user", "content": "hi"}], max_tool_rounds=3)
    assert out["truncated"] is False
    assert calls_seen[1][0] == "execute_python"
    assert calls_seen[1][1]["file_id"] == "abc_data.csv"


def test_parent_dialog_snippet_drops_protocol_catalog_from_system():
    body = (
        "ПРИОРИТЕТ ФОРМАТА\n"
        "assistant_markdown\n"
        "agent_data_analyst\n"
        "\n--- Контекст и инструкции чата (Open WebUI / RAG) ---\n\n"
        '<source id="1" name="x.csv">a,b\n1,2</source>\n'
        '[CT: RAG-источник сохранён для тулов. file_id="abc.csv"]'
    )
    out = _parent_dialog_snippet([{"role": "system", "content": body}])
    assert "assistant_markdown" not in out
    assert "agent_data_analyst" not in out
    assert "file_id=" in out
    assert "<source" in out
