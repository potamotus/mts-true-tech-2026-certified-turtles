from __future__ import annotations

import json

from certified_turtles.agents.json_agent_protocol import (
    PROTOCOL_USER_PREFIX,
    diagnose_protocol_parse_failure,
    extract_user_visible_assistant_text,
    parse_agent_response,
    patch_completion_assistant_markdown,
    tool_outputs_user_message,
)


def test_parse_valid_minimal():
    s = '{"assistant_markdown":"hi","calls":[]}'
    p = parse_agent_response(s)
    assert p is not None
    assert p["assistant_markdown"] == "hi"
    assert p["calls"] == []


def test_parse_null_assistant_markdown_and_calls_coerced():
    s = '{"assistant_markdown":null,"calls":null}'
    p = parse_agent_response(s)
    assert p is not None
    assert p["assistant_markdown"] == ""
    assert p["calls"] == []


def test_parse_null_with_execute_python():
    s = (
        '{"assistant_markdown":null,"calls":['
        '{"name":"execute_python","arguments":{"code":"print(1)"}}]}'
    )
    p = parse_agent_response(s)
    assert p is not None
    assert p["assistant_markdown"] == ""
    assert p["calls"][0]["name"] == "execute_python"


def test_diagnose_protocol_parse_failure_includes_keys():
    s = '{"assistant_markdown":"x","calls":[]}'
    d = diagnose_protocol_parse_failure(s)
    assert "json.loads(целиком): ok" in d
    assert "ключи верхнего уровня" in d


def test_parse_lenient_second_object_in_stream():
    # Первый {…} — не протокол; второй — да (raw_decode со сдвигом).
    s = '{"noise": true} затем {"assistant_markdown":"ok","calls":[]}'
    p = parse_agent_response(s)
    assert p is not None
    assert p["assistant_markdown"] == "ok"


def test_parse_fenced_json():
    body = '{"assistant_markdown":"","calls":[{"name":"web_search","arguments":{"query":"пример"}}]}'
    s = f"Пояснение\n```json\n{body}\n```\n"
    p = parse_agent_response(s)
    assert p is not None
    assert p["calls"][0]["name"] == "web_search"


def test_parse_after_redacted_thinking():
    body = '{"assistant_markdown":"ok","calls":[]}'
    s = f"<think>шум</think>\n{body}"
    p = parse_agent_response(s)
    assert p is not None
    assert p["assistant_markdown"] == "ok"


def test_parse_brace_inside_string_not_truncated():
    # Старый алгоритм «первый { … последний }» ломался на «}» внутри строки.
    inner = '{"assistant_markdown": "символ } в тексте", "calls": []}'
    s = f"Вот ответ:\n{inner}\nспасибо"
    p = parse_agent_response(s)
    assert p is not None
    assert p["assistant_markdown"] == "символ } в тексте"


def test_parse_picks_protocol_when_multiple_json_objects():
    noise = '{"foo": 1}'
    body = '{"assistant_markdown":"x","calls":[]}'
    s = f"{noise}\n{body}"
    p = parse_agent_response(s)
    assert p is not None
    assert p["assistant_markdown"] == "x"


def test_orchestrator_prefers_first_tool_round_extract_prefers_last_answer():
    t = '{"assistant_markdown":"","calls":[{"name":"fetch_url","arguments":{"url":"https://x"}}]}'
    a = '{"assistant_markdown":"Итог для пользователя","calls":[]}'
    raw = f"```json\n{t}\n```\n```json\n{a}\n```"
    orch = parse_agent_response(raw)
    assert orch is not None
    assert orch["calls"]
    assert extract_user_visible_assistant_text(raw) == "Итог для пользователя"


def test_parse_arguments_string():
    args_inner = json.dumps({"query": "x"}, ensure_ascii=False)
    s = json.dumps(
        {
            "assistant_markdown": "",
            "calls": [{"name": "web_search", "arguments": args_inner}],
        },
        ensure_ascii=False,
    )
    # JSON сериализация положит arguments как вложенную строку — модель так иногда шлёт.
    assert isinstance(json.loads(s)["calls"][0]["arguments"], str)
    p = parse_agent_response(s)
    assert p is not None
    assert p["calls"][0]["arguments"] == {"query": "x"}


def test_extract_user_visible():
    raw = json.dumps({"assistant_markdown": "Видимый текст", "calls": []}, ensure_ascii=False)
    assert extract_user_visible_assistant_text(raw) == "Видимый текст"
    assert extract_user_visible_assistant_text("plain") == "plain"


def test_patch_completion():
    comp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": '{"assistant_markdown":"x","calls":[]}',
                    "tool_calls": [{"id": "1", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
                },
                "finish_reason": "stop",
            }
        ]
    }
    out = patch_completion_assistant_markdown(comp, "Для пользователя")
    assert out["choices"][0]["message"]["content"] == "Для пользователя"
    assert "tool_calls" not in out["choices"][0]["message"]


def test_tool_outputs_user_message_shape():
    calls = [{"name": "a", "arguments": {}}]
    outputs = ['{"r":1}']
    u = tool_outputs_user_message(calls, outputs)
    assert u.startswith(PROTOCOL_USER_PREFIX)
    data = json.loads(u.split("\n", 1)[1])
    assert "tool_outputs" in data
