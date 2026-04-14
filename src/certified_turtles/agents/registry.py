from __future__ import annotations

from dataclasses import dataclass

from certified_turtles.prompts import load_prompt


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
DEEP_RESEARCH_AGENT_ID = "deep_research"
CODER_AGENT_ID = "coder"
DATA_ANALYST_AGENT_ID = "data_analyst"
PRESENTATION_AGENT_ID = "presentation"

SUB_AGENTS: dict[str, SubAgentSpec] = {
    RESEARCH_AGENT_ID: SubAgentSpec(
        id=RESEARCH_AGENT_ID,
        system_prompt=load_prompt("subagents/research.md").strip(),
        tool_names=("web_search", "fetch_url"),
        max_inner_rounds=8,
        blurb="Поиск в сети, фактчекинг, разбор ссылок.",
    ),
    WRITER_AGENT_ID: SubAgentSpec(
        id=WRITER_AGENT_ID,
        system_prompt=load_prompt("subagents/writer.md").strip(),
        tool_names=(),
        max_inner_rounds=4,
        blurb="Только текст, без web_search.",
    ),
    DEEP_RESEARCH_AGENT_ID: SubAgentSpec(
        id=DEEP_RESEARCH_AGENT_ID,
        system_prompt=load_prompt("subagents/deep_research.md").strip(),
        tool_names=(),
        max_inner_rounds=1,
        blurb="Отчёт через GPT Researcher (assafelovic/gpt-researcher), отдельный venv.",
    ),
    CODER_AGENT_ID: SubAgentSpec(
        id=CODER_AGENT_ID,
        system_prompt=load_prompt("subagents/coder.md").strip(),
        tool_names=("execute_python", "read_workspace_file", "workspace_file_path"),
        max_inner_rounds=12,
        blurb="Запуск Python, графики, разбор загруженных файлов.",
    ),
    DATA_ANALYST_AGENT_ID: SubAgentSpec(
        id=DATA_ANALYST_AGENT_ID,
        system_prompt=load_prompt("subagents/data_analyst.md").strip(),
        tool_names=("workspace_file_path", "execute_python", "read_workspace_file"),
        max_inner_rounds=14,
        blurb="CSV/XLSX: путь к файлу + Python, результат в stdout/артефактах.",
    ),
    PRESENTATION_AGENT_ID: SubAgentSpec(
        id=PRESENTATION_AGENT_ID,
        system_prompt=load_prompt("subagents/presentation.md").strip(),
        tool_names=("web_search", "fetch_url", "generate_presentation"),
        max_inner_rounds=10,
        blurb="Создание .pptx: исследование темы + генерация презентации.",
    ),
}


def list_subagent_ids() -> list[str]:
    return sorted(SUB_AGENTS.keys())


def get_subagent(agent_id: str) -> SubAgentSpec | None:
    return SUB_AGENTS.get(agent_id)


def subagents_catalog_for_tool_description() -> str:
    lines = [s.summary_line for s in SUB_AGENTS.values()]
    return "\n".join(lines)
