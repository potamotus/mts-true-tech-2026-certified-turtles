from __future__ import annotations

import copy
import json
from typing import Any

from certified_turtles.agents.loop import (
    _parent_dialog_snippet,
    _strip_openwebui_tool_router_noise,
    run_agent_chat,
    stream_agent_chat,
)
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

    def chat_completions_stream(self, model: str, messages: list, **kwargs):
        self.calls.append(
            {
                "model": model,
                "n_msg": len(messages),
                "messages": copy.deepcopy(messages),
                "has_tools": "tools" in kwargs,
                "stream": True,
            }
        )
        if not self._responses:
            raise RuntimeError("unexpected extra chat_completions_stream call")
        full = self._responses.pop(0)
        ch = full["choices"][0]
        msg = ch["message"]
        content = msg.get("content") if isinstance(msg.get("content"), str) else ""
        fr = ch.get("finish_reason")
        tcs = msg.get("tool_calls")
        if content:
            mid = max(1, len(content) // 2)
            yield {"choices": [{"index": 0, "delta": {"content": content[:mid]}}]}
            delta2: dict[str, Any] = {"content": content[mid:]}
            if tcs:
                indexed = []
                for i, tc in enumerate(tcs):
                    if isinstance(tc, dict):
                        tc2 = copy.deepcopy(tc)
                        tc2["index"] = i
                        indexed.append(tc2)
                delta2["tool_calls"] = indexed
            yield {"choices": [{"index": 0, "delta": delta2, "finish_reason": fr}]}
        elif tcs:
            indexed = []
            for i, tc in enumerate(tcs):
                if isinstance(tc, dict):
                    tc2 = copy.deepcopy(tc)
                    tc2["index"] = i
                    indexed.append(tc2)
            yield {"choices": [{"index": 0, "delta": {"tool_calls": indexed}, "finish_reason": fr or "tool_calls"}]}
        else:
            yield {"choices": [{"index": 0, "delta": {}, "finish_reason": fr or "stop"}]}


def _tool_response(text: str, name: str, arguments: dict) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


def _tool_pair_response(text: str, first: tuple[str, dict], second: tuple[str, dict]) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": first[0],
                                "arguments": json.dumps(first[1], ensure_ascii=False),
                            },
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": second[0],
                                "arguments": json.dumps(second[1], ensure_ascii=False),
                            },
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


def _final_response(text: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": text,
                },
                "finish_reason": "stop",
            }
        ]
    }


def test_stream_does_not_emit_reasoning_duplicate_of_final(monkeypatch):
    """Финальный ответ без tool_calls не дублируется как reasoning + final в одном тексте."""
    final = _final_response("Только финал")
    fake = FakeMWSClient([final])
    events = list(stream_agent_chat(fake, "m", [{"role": "user", "content": "hi"}], max_tool_rounds=3))
    reasoning_texts = [e["text"] for e in events if isinstance(e, dict) and e.get("type") == "reasoning"]
    assert "Только финал" not in reasoning_texts
    finals = [e for e in events if isinstance(e, dict) and e.get("type") == "final"]
    assert finals and finals[0].get("text") == "Только финал"


def test_agent_finishes_after_tool_round(monkeypatch):
    monkeypatch.setattr("certified_turtles.agents.loop.run_primitive_tool", lambda name, args: "MOCK_RESULT")
    r1 = _tool_response("Сначала посмотрю результаты поиска.", "web_search", {"query": "x"})
    r2 = _final_response("Финал")
    fake = FakeMWSClient([r1, r2])
    out = run_agent_chat(fake, "mws-gpt-alpha", [{"role": "user", "content": "hi"}], max_tool_rounds=5)
    assert out["truncated"] is False
    assert out["tool_rounds_used"] == 2
    assert fake.calls[0]["has_tools"] is True
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    visible = out["completion"]["choices"][0]["message"]["content"]
    assert "### Размышление" in visible
    assert "Финал" in visible


def test_agent_truncates_when_token_budget_exhausted(monkeypatch):
    monkeypatch.setattr("certified_turtles.agents.loop.run_primitive_tool", lambda n, a: "x")
    r_tool = _tool_response("Сначала поищу данные.", "web_search", {})
    fake = FakeMWSClient([r_tool])
    out = run_agent_chat(fake, "m", [{"role": "user", "content": "hi"}], max_agent_tokens=1000)
    assert out["truncated"] is True
    assert out["tool_rounds_used"] == 1
    assert "неполным" in out["completion"]["choices"][0]["message"]["content"]


def test_agent_system_is_first_and_keeps_existing_system(monkeypatch):
    monkeypatch.setattr("certified_turtles.agents.loop.run_primitive_tool", lambda n, a: "x")
    final = _final_response("ok")
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
    assert "публичным reasoning-потоком" in sent[0]["content"]
    assert sent[1]["role"] == "system"
    assert "You are helpful." in sent[1]["content"]
    assert sent[2]["role"] == "user"


def test_agent_uses_tools_kwarg_with_explicit_catalog(monkeypatch):
    monkeypatch.setattr("certified_turtles.agents.loop.run_primitive_tool", lambda n, a: "x")
    final = _final_response("ok")
    fake = FakeMWSClient([final])
    tools = openai_tools_for_names(("web_search",))
    run_agent_chat(fake, "m", [{"role": "user", "content": "hi"}], tools=tools, max_tool_rounds=1)
    assert fake.calls[0]["has_tools"] is True


def test_execute_python_autobinds_file_id_from_workspace_tool(monkeypatch):
    monkeypatch.setattr(
        "certified_turtles.agents.loop.llm_should_skip_execute_python",
        lambda client, model, text: False,
    )
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
    r1 = _tool_pair_response(
        "Сначала определю путь к файлу, затем выполню код.",
        ("workspace_file_path", {"file_id": "abc_data.csv"}),
        ("execute_python", {"code": "print('x')"}),
    )
    r2 = _final_response("Готово")
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


def test_strip_openwebui_tool_router_noise_removes_available_tools_block():
    raw = (
        "Available Tools: []\n\n"
        "Your task is to choose and return the correct tool(s) from the list of available tools "
        "based on the query. Follow these guidelines:\n"
        "- Return only the JSON object.\n\n"
        '[CT: note file_id="z.csv"]'
    )
    out = _strip_openwebui_tool_router_noise(raw)
    assert "Available Tools" not in out
    assert "choose and return the correct tool" not in out.lower()
    assert "file_id=" in out


def test_parent_dialog_snippet_no_openwebui_router_after_marker():
    body = (
        "IGNORED_BEFORE_MARKER\n"
        "--- Контекст и инструкции чата (Open WebUI / RAG) ---\n\n"
        "Available Tools: []\n\n"
        "Your task is to choose and return the correct tool(s) from the list of available tools "
        "based on the query.\n\n"
        "History:\nUSER: hi\n"
    )
    out = _parent_dialog_snippet([{"role": "system", "content": body}])
    assert "Available Tools" not in out
    assert "History:" in out
