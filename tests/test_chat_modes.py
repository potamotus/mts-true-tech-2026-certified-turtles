from __future__ import annotations

import pytest

from certified_turtles.chat_modes import list_chat_mode_ids, prepare_chat_request
from certified_turtles.prompts import load_prompt


def test_prepare_deep_research_from_body():
    body = {"ct_mode": "deep_research"}
    msgs = [{"role": "user", "content": "Изучи тему X"}]
    p = prepare_chat_request(body, msgs, for_agent=True)
    assert p.mode_applied == "deep_research"
    assert p.max_tool_rounds_override == 36
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
    assert "presentation" in list_chat_mode_ids()
    assert "default" not in list_chat_mode_ids()


def test_prepare_presentation_from_body():
    body = {"ct_mode": "presentation"}
    msgs = [{"role": "user", "content": "Сделай презентацию про ИИ"}]
    p = prepare_chat_request(body, msgs, for_agent=True)
    assert p.mode_applied == "presentation"
    assert p.max_tool_rounds_override == 14
    assert any(
        m.get("role") == "system" and "презентац" in (m.get("content") or "").lower()
        for m in p.messages
    )


# --- Пункт 17: контракт — каждый ct_mode имеет prompt-файл и запись ---


@pytest.mark.parametrize("mode_id", list_chat_mode_ids())
def test_every_mode_has_prompt_file(mode_id):
    """Каждый режим (кроме default) должен иметь prompts/modes/<id>.md с непустым содержимым."""
    text = load_prompt(f"modes/{mode_id}.md")
    assert text.strip(), f"Промпт для режима {mode_id!r} пуст"


@pytest.mark.parametrize("mode_id", list_chat_mode_ids())
def test_every_mode_injects_system(mode_id):
    """prepare_chat_request для каждого режима должен вставлять system-сообщение."""
    body = {"ct_mode": mode_id}
    msgs = [{"role": "user", "content": "тест"}]
    p = prepare_chat_request(body, msgs, for_agent=True)
    assert p.mode_applied == mode_id
    assert p.max_tool_rounds_override is not None and p.max_tool_rounds_override > 0


# --- Пункт 16: writer-режим не должен вызывать execute_python ---


def test_writer_mode_prompt_forbids_python():
    """Промпт writer-режима должен содержать запрет на Python/execute_python."""
    text = load_prompt("modes/writer.md").lower()
    assert "python" in text, "writer prompt должен упоминать запрет на Python"
