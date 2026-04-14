from __future__ import annotations

from typing import Any

from certified_turtles.agents.registry import CODER_AGENT_ID, SUB_AGENTS, SubAgentSpec
from certified_turtles.prompts import load_prompt
from certified_turtles.tools.registry import openai_definitions_all_primitives

AGENT_TOOL_PREFIX = "agent_"


def agent_openai_tool(spec: SubAgentSpec) -> dict[str, Any]:
    """Одна функция в `tools` на под-агента: имя `agent_{id}` (родительская LLM вызывает как тул)."""
    desc_parts = [
        f"Под-агент «{spec.id}».",
        spec.blurb.strip() if spec.blurb else "",
        "Передай поле task: что сделать. Опционально context — выдержка из основного диалога.",
    ]
    if spec.id == CODER_AGENT_ID:
        desc_parts.append(load_prompt("parent_tools_agent_coder_note.txt").strip())
    return {
        "type": "function",
        "function": {
            "name": f"{AGENT_TOOL_PREFIX}{spec.id}",
            "description": " ".join(p for p in desc_parts if p).strip(),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Задача для под-агента (одним сообщением user).",
                    },
                    "context": {
                        "type": "string",
                        "description": "Необязательно: краткий контекст с главного чата.",
                    },
                },
                "required": ["task"],
            },
        },
    }


def get_parent_tools() -> list[dict[str, Any]]:
    """
    Все вызовы, доступные стартовой LLM: зарегистрированные примитивные тулы + по одному тулу на каждого под-агента.
    Новый тул: register_tool(...) в своём модуле и импорт модуля из `tools/builtins` или из точки входа приложения.
    Новый агент: добавить запись в `agents/registry.py` SUB_AGENTS.
    """
    primitives = openai_definitions_all_primitives()
    agents = [agent_openai_tool(s) for s in sorted(SUB_AGENTS.values(), key=lambda x: x.id)]
    return [*primitives, *agents]


def parse_agent_tool_name(function_name: str) -> str | None:
    """Если имя — `agent_{id}` и id известен, вернуть id, иначе None."""
    if not function_name.startswith(AGENT_TOOL_PREFIX):
        return None
    aid = function_name[len(AGENT_TOOL_PREFIX) :]
    if aid in SUB_AGENTS:
        return aid
    return None
