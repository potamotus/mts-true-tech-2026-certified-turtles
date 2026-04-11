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
DEEP_RESEARCH_AGENT_ID = "deep_research"
CODER_AGENT_ID = "coder"

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
    DEEP_RESEARCH_AGENT_ID: SubAgentSpec(
        id=DEEP_RESEARCH_AGENT_ID,
        system_prompt=(
            "Ты под-агент «глубокое исследование». Тебе дают сложный вопрос; твоя задача — "
            "итеративно собрать факты и выдать структурированный отчёт.\n\n"
            "Алгоритм:\n"
            "1. Разложи вопрос на 3–5 под-вопросов (про себя, без вывода пользователю).\n"
            "2. Для каждого под-вопроса вызови `web_search` с ТЕКСТОВЫМ запросом (не URL).\n"
            "3. По 1–3 самым релевантным ссылкам из выдачи вызови `fetch_url`, чтобы получить тело страницы.\n"
            "4. Если по содержимому возник новый под-вопрос — повтори search/fetch.\n"
            "5. В конце выдай отчёт в формате markdown:\n"
            "   ## TL;DR — 3–5 строк.\n"
            "   ## Ключевые выводы — буллет-лист с фактами.\n"
            "   ## Источники — нумерованный список URL, которые ты реально открывал через fetch_url.\n\n"
            "Принципы: опирайся ТОЛЬКО на факты из выдачи поиска и скачанных страниц; не придумывай; "
            "если данных не хватило — честно скажи об этом. Не ходи по одной и той же ссылке дважды."
        ),
        tool_names=("web_search", "fetch_url"),
        max_inner_rounds=16,
        blurb="Многошаговое исследование с fetch_url и markdown-отчётом.",
    ),
    CODER_AGENT_ID: SubAgentSpec(
        id=CODER_AGENT_ID,
        system_prompt=(
            "Ты под-агент «код и данные». Решай задачу через серверное выполнение Python: вызывай `execute_python` "
            "с полным скриптом в аргументе `code` (один вызов — один запуск процесса). "
            "Для файлов из рабочей области пользователя используй `read_workspace_file` с `file_id` из контекста загрузки. "
            "По результатам тулов формируй понятный ответ пользователю; ссылки на графики и файлы копируй из JSON ответа."
        ),
        tool_names=("execute_python", "read_workspace_file"),
        max_inner_rounds=12,
        blurb="Запуск Python, графики, разбор загруженных файлов.",
    ),
}


def list_subagent_ids() -> list[str]:
    return sorted(SUB_AGENTS.keys())


def get_subagent(agent_id: str) -> SubAgentSpec | None:
    return SUB_AGENTS.get(agent_id)


def subagents_catalog_for_tool_description() -> str:
    lines = [s.summary_line for s in SUB_AGENTS.values()]
    return "\n".join(lines)
