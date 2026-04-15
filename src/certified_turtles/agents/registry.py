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
    max_total_tokens: int = 0
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
MEMORY_EXTRACTOR_AGENT_ID = "memory_extractor"
SESSION_MEMORY_AGENT_ID = "session_memory"
AUTO_DREAM_AGENT_ID = "auto_dream"

SUB_AGENTS: dict[str, SubAgentSpec] = {
    RESEARCH_AGENT_ID: SubAgentSpec(
        id=RESEARCH_AGENT_ID,
        system_prompt=load_prompt("subagents/research.md").strip(),
        tool_names=("web_search", "fetch_url"),
        max_total_tokens=50_000,
        blurb="Поиск в сети, фактчекинг, разбор ссылок.",
    ),
    WRITER_AGENT_ID: SubAgentSpec(
        id=WRITER_AGENT_ID,
        system_prompt=load_prompt("subagents/writer.md").strip(),
        tool_names=(),
        max_total_tokens=20_000,
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
        max_total_tokens=80_000,
        blurb="Запуск Python, графики, разбор загруженных файлов.",
    ),
    DATA_ANALYST_AGENT_ID: SubAgentSpec(
        id=DATA_ANALYST_AGENT_ID,
        system_prompt=load_prompt("subagents/data_analyst.md").strip(),
        tool_names=("workspace_file_path", "execute_python", "read_workspace_file"),
        max_total_tokens=100_000,
        blurb="CSV/XLSX: путь к файлу + Python, результат в stdout/артефактах.",
    ),
    PRESENTATION_AGENT_ID: SubAgentSpec(
        id=PRESENTATION_AGENT_ID,
        system_prompt=load_prompt("subagents/presentation.md").strip(),
        tool_names=("web_search", "fetch_url", "generate_presentation"),
        max_total_tokens=60_000,
        blurb="Создание .pptx: исследование темы + генерация презентации.",
    ),
    MEMORY_EXTRACTOR_AGENT_ID: SubAgentSpec(
        id=MEMORY_EXTRACTOR_AGENT_ID,
        system_prompt=load_prompt("subagents/memory_extractor.md").strip(),
        tool_names=("file_read", "file_write", "file_edit", "grep_search", "glob_search"),
        max_total_tokens=30_000,
        blurb="Извлечение воспоминаний из диалога в файлы памяти.",
    ),
    SESSION_MEMORY_AGENT_ID: SubAgentSpec(
        id=SESSION_MEMORY_AGENT_ID,
        system_prompt=load_prompt("subagents/session_memory.md").strip(),
        tool_names=("file_read", "file_edit"),
        max_total_tokens=15_000,
        blurb="Обновление заметок текущей сессии.",
    ),
    AUTO_DREAM_AGENT_ID: SubAgentSpec(
        id=AUTO_DREAM_AGENT_ID,
        system_prompt=load_prompt("subagents/auto_dream.md").strip(),
        tool_names=("file_read", "file_write", "file_edit", "grep_search", "glob_search"),
        max_total_tokens=40_000,
        blurb="Консолидация и очистка файлов памяти.",
    ),
}


def list_subagent_ids() -> list[str]:
    return sorted(SUB_AGENTS.keys())


def get_subagent(agent_id: str) -> SubAgentSpec | None:
    return SUB_AGENTS.get(agent_id)


def subagents_catalog_for_tool_description() -> str:
    lines = [s.summary_line for s in SUB_AGENTS.values()]
    return "\n".join(lines)
