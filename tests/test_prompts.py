from __future__ import annotations

from certified_turtles.prompts import load_prompt


def test_load_prompt_protocol_spec():
    text = load_prompt("protocol_spec.md")
    assert "JSON-протокол" in text
    assert "assistant_markdown" in text


def test_load_prompt_subagent():
    assert "исследователь" in load_prompt("subagents/research.md")
