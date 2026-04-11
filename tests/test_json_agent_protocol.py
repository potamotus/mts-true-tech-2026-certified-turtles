from __future__ import annotations

import json

from certified_turtles.agents.json_agent_protocol import (
    PROTOCOL_USER_PREFIX,
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


def test_parse_fenced_json():
    body = '{"assistant_markdown":"","calls":[{"name":"mws_list_models","arguments":{}}]}'
    s = f"Пояснение\n```json\n{body}\n```\n"
    p = parse_agent_response(s)
    assert p is not None
    assert p["calls"][0]["name"] == "mws_list_models"


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
                "message": {"role": "assistant", "content": '{"assistant_markdown":"x","calls":[]}'},
                "finish_reason": "stop",
            }
        ]
    }
    out = patch_completion_assistant_markdown(comp, "Для пользователя")
    assert out["choices"][0]["message"]["content"] == "Для пользователя"


def test_tool_outputs_user_message_shape():
    calls = [{"name": "a", "arguments": {}}]
    outputs = ['{"r":1}']
    u = tool_outputs_user_message(calls, outputs)
    assert u.startswith(PROTOCOL_USER_PREFIX)
    data = json.loads(u.split("\n", 1)[1])
    assert "tool_outputs" in data
