from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SubAgentSpec:
    """Описание под-агента: отдельный системный промпт и свой набор примитивных тулов (без invoke_agent)."""

    id: str
    system_prompt: str
    tool_names: tuple[str, ...]
    max_inner_rounds: int = 8
    blurb: str = ""

    @property
    def summary_line(self) -> str:
        extra = f" {self.blurb}" if self.blurb else ""
        tools = ", ".join(self.tool_names) if self.tool_names else "без тулов"
        return f"- `{self.id}`: тулы [{tools}].{extra}"


# Имена тулов под-агента — из реестра примитивов (`register_tool`, см. tools/builtins).
RESEARCH_AGENT_ID = "research"
WRITER_AGENT_ID = "writer"

SUB_AGENTS: dict[str, SubAgentSpec] = {
    RESEARCH_AGENT_ID: SubAgentSpec(
        id=RESEARCH_AGENT_ID,
        system_prompt=(
            "Ты под-агент «исследователь». Твоя задача — ответить на запрос пользователя, "
            "опираясь на актуальные данные из интернета. Сначала формулируй запросы для `web_search` "
            "(обычным языком, без URL). Для ссылок из выдачи или от пользователя вызывай `fetch_url` — "
            "это даёт текст страницы. В конце дай краткий сжатый вывод без воды и укажи источники."
        ),
        tool_names=("web_search", "fetch_url"),
        max_inner_rounds=8,
        blurb="Поиск в сети, фактчекинг, разбор ссылок.",
    ),
    WRITER_AGENT_ID: SubAgentSpec(
        id=WRITER_AGENT_ID,
        system_prompt=(
            "Ты под-агент «редактор». Переформулируй, сократи или улучши текст задачи "
            "без вызова внешних инструментов: работай только с тем, что передано в сообщении."
        ),
        tool_names=(),
        max_inner_rounds=4,
        blurb="Только текст, без web_search.",
    ),
}


def list_subagent_ids() -> list[str]:
    return sorted(SUB_AGENTS.keys())


def get_subagent(agent_id: str) -> SubAgentSpec | None:
    return SUB_AGENTS.get(agent_id)


def subagents_catalog_for_tool_description() -> str:
    lines = [s.summary_line for s in SUB_AGENTS.values()]
    return "\n".join(lines)
