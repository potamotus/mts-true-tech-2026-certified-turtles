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
DATA_ANALYST_AGENT_ID = "data_analyst"
MEMORY_EXTRACTOR_AGENT_ID = "memory_extractor"
SESSION_MEMORY_AGENT_ID = "session_memory"
AUTO_DREAM_AGENT_ID = "auto_dream"
MEMORY_TESTER_AGENT_ID = "memory_tester"

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
            "Для табличных файлов сначала `workspace_file_path` с `file_id`, затем pandas по полю `absolute_path`. "
            "Для небольших текстовых файлов можно `read_workspace_file`. "
            "По результатам тулов формируй понятный ответ пользователю; ссылки на графики и файлы копируй из JSON ответа."
        ),
        tool_names=("execute_python", "read_workspace_file", "workspace_file_path"),
        max_inner_rounds=12,
        blurb="Запуск Python, графики, разбор загруженных файлов.",
    ),
    DATA_ANALYST_AGENT_ID: SubAgentSpec(
        id=DATA_ANALYST_AGENT_ID,
        system_prompt=(
            "Ты под-агент «аналитика табличных данных» (без отдельных метрик/дашбордов — только вывод из кода).\n\n"
            "Рабочий цикл:\n"
            "1. Узнай `file_id`: в тексте user после нормализации чата (вложение сохраняется на сервер автоматически) "
            "или из явной загрузки POST /api/v1/uploads.\n"
            "2. Вызови `workspace_file_path` с этим file_id и возьми `absolute_path`, **или** сразу `execute_python` "
            "с тем же `file_id` в аргументах тула и в `code`: "
            "`import pandas as pd; df = pd.read_csv(CT_DATA_FILE_ABSPATH, encoding='utf-8', on_bad_lines='skip')` "
            "(для .xlsx — read_excel(..., engine='openpyxl') с тем же путём из absolute_path).\n"
            "3. Выполни анализ (агрегации, фильтры, сводные, графики), выведи итоги через print() в stdout.\n"
            "4. Несколько шагов — последовательные вызовы execute_python.\n\n"
            "Ограничения кода: без open() и .open(); только разрешённые импорты; чтение CSV через pd.read_csv(path, encoding='utf-8', on_bad_lines='skip'). "
            "Если execute_python вернул returncode≠0 — исправь код по stderr и повтори вызов. "
            "Графики сохраняй в CT_RUN_OUTPUT_DIR и давай пользователю URL из поля artifacts.\n\n"
            "В финальном ответе кратко резюмируй выводы по цифрам из stdout; не выдумывай то, чего нет в выводе тулов."
        ),
        tool_names=("workspace_file_path", "execute_python", "read_workspace_file"),
        max_inner_rounds=14,
        blurb="CSV/XLSX: путь к файлу + Python, результат в stdout/артефактах.",
    ),
    MEMORY_EXTRACTOR_AGENT_ID: SubAgentSpec(
        id=MEMORY_EXTRACTOR_AGENT_ID,
        system_prompt=(
            "Ты под-агент извлечения памяти в стиле Claude Code. "
            "Анализируй только недавний диалог, сохраняй долговечные факты в memory/*.md, "
            "используя file_read/file_write/file_edit/glob_search/grep_search. "
            "Обновляй MEMORY.md при появлении новых memory files. "
            "Не сохраняй кодовые паттерны, временные шаги дебага или секреты."
        ),
        tool_names=("file_read", "file_write", "file_edit", "glob_search", "grep_search"),
        max_inner_rounds=5,
        blurb="Фоновое извлечение долговечной памяти в topic-файлы.",
    ),
    SESSION_MEMORY_AGENT_ID: SubAgentSpec(
        id=SESSION_MEMORY_AGENT_ID,
        system_prompt=(
            "Ты под-агент session memory. Поддерживай один session.md файл, суммируя текущее состояние работы: "
            "task, files, workflow, errors, learnings, next steps. "
            "Используй file_read/file_write/file_edit и держи summary компактной."
        ),
        tool_names=("file_read", "file_write", "file_edit"),
        max_inner_rounds=4,
        blurb="Поддержка session memory и compaction surrogate.",
    ),
    AUTO_DREAM_AGENT_ID: SubAgentSpec(
        id=AUTO_DREAM_AGENT_ID,
        system_prompt=(
            "Ты под-агент Auto Dream. Консолидируй накопленную память: объединяй дубликаты, "
            "улучшай описания, удаляй устаревшее, пересобирай MEMORY.md. "
            "Используй file_read/file_write/file_edit/glob_search/grep_search."
        ),
        tool_names=("file_read", "file_write", "file_edit", "glob_search", "grep_search"),
        max_inner_rounds=10,
        blurb="Низкочастотная консолидация памяти и индексирование.",
    ),
    MEMORY_TESTER_AGENT_ID: SubAgentSpec(
        id=MEMORY_TESTER_AGENT_ID,
        system_prompt=(
            "Ты под-агент тестирования памяти. Проверяй, что memory prompt, MEMORY.md, topic files и session memory "
            "согласованы. При необходимости читай файлы через file_read и собирай отчёт о пропусках, конфликтах и drift."
        ),
        tool_names=("file_read", "glob_search", "grep_search"),
        max_inner_rounds=6,
        blurb="Диагностика recall, extraction и session-memory.",
    ),
}


def list_subagent_ids() -> list[str]:
    return sorted(SUB_AGENTS.keys())


def get_subagent(agent_id: str) -> SubAgentSpec | None:
    return SUB_AGENTS.get(agent_id)


def subagents_catalog_for_tool_description() -> str:
    lines = [s.summary_line for s in SUB_AGENTS.values()]
    return "\n".join(lines)
