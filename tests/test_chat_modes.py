from __future__ import annotations

from certified_turtles.chat_modes import list_chat_mode_ids, prepare_chat_request


def test_prepare_deep_research_from_body():
    body = {"ct_mode": "deep_research"}
    msgs = [{"role": "user", "content": "Изучи тему X"}]
    p = prepare_chat_request(body, msgs, for_agent=True)
    assert p.mode_applied == "deep_research"
    assert p.max_tool_rounds_override == 28
    assert any(
        m.get("role") == "system" and "Deep Research" in (m.get("content") or "")
        for m in p.messages
    )


def test_prepare_strip_prefix_deep_alias():
    body = {}
    msgs = [{"role": "user", "content": "[CT_MODE:deep]\n\nЗапрос"}]
    p = prepare_chat_request(body, msgs, for_agent=True)
    assert p.mode_applied == "deep_research"
    last_user = [m for m in p.messages if m.get("role") == "user"][-1]
    assert last_user["content"] == "Запрос"


def test_prepare_plain_only_strips_prefix():
    body = {}
    msgs = [{"role": "user", "content": "[CT_MODE:coder]\n\nHi"}]
    p = prepare_chat_request(body, msgs, for_agent=False)
    assert p.mode_applied is None
    assert p.messages[0]["content"] == "Hi"


def test_list_modes_nonempty():
    assert "deep_research" in list_chat_mode_ids()
    assert "default" not in list_chat_mode_ids()
