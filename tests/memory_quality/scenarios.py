"""Test scenarios for memory quality evaluation.

Each scenario is a dict with:
- id: unique identifier
- category: one of explicit, implicit, feedback, project, reference, negative, edge, multilingual
- messages: list of chat messages to send
- should_save: whether the system should persist a memory
- expected_memory_type: expected type (user/feedback/project/reference) or None
- keywords: keywords expected in saved content
- description: human-readable description
"""

from __future__ import annotations

from typing import Any

Scenario = dict[str, Any]

# ---------------------------------------------------------------------------
# A. Explicit requests to remember (should save)
# ---------------------------------------------------------------------------

EXPLICIT: list[Scenario] = [
    {
        "id": "explicit_pref_python_ru",
        "category": "explicit",
        "messages": [{"role": "user", "content": "Запомни, что я предпочитаю Python"}],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["python"],
        "description": "Явная просьба запомнить предпочтение языка (рус)",
    },
    {
        "id": "explicit_work_mts_en",
        "category": "explicit",
        "messages": [{"role": "user", "content": "Remember: I work at MTS as a backend engineer"}],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["mts", "backend"],
        "description": "Explicit remember — workplace and role (en)",
    },
    {
        "id": "explicit_github_ru",
        "category": "explicit",
        "messages": [{"role": "user", "content": "Сохрани это: мой GitHub — github.com/testuser"}],
        "should_save": True,
        "expected_memory_type": "reference",
        "keywords": ["github"],
        "description": "Явная просьба сохранить ссылку на GitHub (рус)",
    },
    {
        "id": "explicit_dislike_oop_ru",
        "category": "explicit",
        "messages": [{"role": "user", "content": "Пожалуйста, запомни — я не люблю ООП"}],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["ооп", "oop"],
        "description": "Явная просьба запомнить антипредпочтение (рус)",
    },
    {
        "id": "explicit_deadline_ru",
        "category": "explicit",
        "messages": [{"role": "user", "content": "Хочу чтобы ты помнил: дедлайн 15 апреля"}],
        "should_save": True,
        "expected_memory_type": "project",
        "keywords": ["дедлайн", "апрел", "deadline"],
        "description": "Явная просьба запомнить дедлайн (рус)",
    },
    {
        "id": "explicit_name_ru",
        "category": "explicit",
        "messages": [{"role": "user", "content": "Запомни, меня зовут Алексей"}],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["алексей"],
        "description": "Явная просьба запомнить имя (рус)",
    },
    {
        "id": "explicit_framework_en",
        "category": "explicit",
        "messages": [{"role": "user", "content": "Please remember that I always use FastAPI for backends"}],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["fastapi"],
        "description": "Explicit remember — framework preference (en)",
    },
    {
        "id": "explicit_os_en",
        "category": "explicit",
        "messages": [{"role": "user", "content": "Remember: I use macOS with zsh"}],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["macos", "zsh"],
        "description": "Explicit remember — OS and shell (en)",
    },
]

# ---------------------------------------------------------------------------
# B. Implicit preferences (should save)
# ---------------------------------------------------------------------------

IMPLICIT: list[Scenario] = [
    {
        "id": "implicit_typescript",
        "category": "implicit",
        "messages": [{"role": "user", "content": "Я всегда пишу на TypeScript"}],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["typescript"],
        "description": "Неявное предпочтение языка",
    },
    {
        "id": "implicit_functional",
        "category": "implicit",
        "messages": [{"role": "user", "content": "Мне нравится функциональный стиль программирования"}],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["функциональн"],
        "description": "Неявное предпочтение стиля",
    },
    {
        "id": "implicit_vim",
        "category": "implicit",
        "messages": [{"role": "user", "content": "Я обычно использую Vim"}],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["vim"],
        "description": "Неявное предпочтение редактора",
    },
    {
        "id": "implicit_short_answers",
        "category": "implicit",
        "messages": [{"role": "user", "content": "Ненавижу длинные ответы"}],
        "should_save": True,
        "expected_memory_type": "feedback",
        "keywords": ["коротк", "длинн"],
        "description": "Неявный фидбек о длине ответов",
    },
    {
        "id": "implicit_senior_backend",
        "category": "implicit",
        "messages": [{"role": "user", "content": "Я senior backend developer"}],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["senior", "backend"],
        "description": "Неявная информация о роли",
    },
    {
        "id": "implicit_docker_always",
        "category": "implicit",
        "messages": [{"role": "user", "content": "I always deploy with Docker and docker-compose"}],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["docker"],
        "description": "Implicit deployment preference (en)",
    },
    {
        "id": "implicit_testing_pytest",
        "category": "implicit",
        "messages": [{"role": "user", "content": "Для тестов я всегда использую pytest"}],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["pytest"],
        "description": "Неявное предпочтение тестового фреймворка",
    },
    {
        "id": "implicit_linux_server",
        "category": "implicit",
        "messages": [{"role": "user", "content": "Мои серверы все на Ubuntu 22.04"}],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["ubuntu"],
        "description": "Неявная информация об инфраструктуре",
    },
]

# ---------------------------------------------------------------------------
# C. Feedback / corrections (should save)
# ---------------------------------------------------------------------------

FEEDBACK: list[Scenario] = [
    {
        "id": "feedback_no_emoji",
        "category": "feedback",
        "messages": [{"role": "user", "content": "Не используй emoji в ответах"}],
        "should_save": True,
        "expected_memory_type": "feedback",
        "keywords": ["emoji"],
        "description": "Фидбек: не использовать эмодзи",
    },
    {
        "id": "feedback_type_hints",
        "category": "feedback",
        "messages": [{"role": "user", "content": "Всегда добавляй type hints в Python"}],
        "should_save": True,
        "expected_memory_type": "feedback",
        "keywords": ["type hint"],
        "description": "Фидбек: всегда type hints",
    },
    {
        "id": "feedback_too_verbose",
        "category": "feedback",
        "messages": [{"role": "user", "content": "Ты слишком многословный, отвечай короче"}],
        "should_save": True,
        "expected_memory_type": "feedback",
        "keywords": ["коротк", "многословн"],
        "description": "Фидбек: слишком длинные ответы",
    },
    {
        "id": "feedback_comments_english",
        "category": "feedback",
        "messages": [{"role": "user", "content": "Пиши комментарии на английском, а не на русском"}],
        "should_save": True,
        "expected_memory_type": "feedback",
        "keywords": ["комментари", "английск"],
        "description": "Фидбек: язык комментариев",
    },
    {
        "id": "feedback_no_libs",
        "category": "feedback",
        "messages": [{"role": "user", "content": "Не предлагай библиотеки без необходимости"}],
        "should_save": True,
        "expected_memory_type": "feedback",
        "keywords": ["библиотек"],
        "description": "Фидбек: не предлагать лишние библиотеки",
    },
    {
        "id": "feedback_no_docstrings",
        "category": "feedback",
        "messages": [{"role": "user", "content": "Don't add docstrings unless I ask for them"}],
        "should_save": True,
        "expected_memory_type": "feedback",
        "keywords": ["docstring"],
        "description": "Feedback: no unnecessary docstrings (en)",
    },
    {
        "id": "feedback_explain_briefly",
        "category": "feedback",
        "messages": [{"role": "user", "content": "Объясняй коротко, без воды"}],
        "should_save": True,
        "expected_memory_type": "feedback",
        "keywords": ["коротк"],
        "description": "Фидбек: краткие объяснения",
    },
]

# ---------------------------------------------------------------------------
# D. Project context (should save)
# ---------------------------------------------------------------------------

PROJECT: list[Scenario] = [
    {
        "id": "project_wikilive",
        "category": "project",
        "messages": [{"role": "user", "content": "Мы делаем проект WikiLive для хакатона MTS True Tech"}],
        "should_save": True,
        "expected_memory_type": "project",
        "keywords": ["wikilive", "mts"],
        "description": "Проектный контекст: название и хакатон",
    },
    {
        "id": "project_deadline",
        "category": "project",
        "messages": [{"role": "user", "content": "Дедлайн нашего проекта — 15 апреля 2026"}],
        "should_save": True,
        "expected_memory_type": "project",
        "keywords": ["дедлайн", "апрел", "2026"],
        "description": "Проектный контекст: дедлайн",
    },
    {
        "id": "project_stack",
        "category": "project",
        "messages": [{"role": "user", "content": "Мы решили использовать FastAPI + Open WebUI"}],
        "should_save": True,
        "expected_memory_type": "project",
        "keywords": ["fastapi", "webui"],
        "description": "Проектный контекст: технологический стек",
    },
    {
        "id": "project_team",
        "category": "project",
        "messages": [{"role": "user", "content": "В команде 4 человека: Potamotus, AsonglefacMillenium, nikitavivat, timsonyk"}],
        "should_save": True,
        "expected_memory_type": "project",
        "keywords": ["potamotus", "команд"],
        "description": "Проектный контекст: состав команды",
    },
    {
        "id": "project_repo",
        "category": "project",
        "messages": [{"role": "user", "content": "Наш репозиторий: github.com/potamotus/mts-true-tech-2026"}],
        "should_save": True,
        "expected_memory_type": "project",
        "keywords": ["github", "potamotus"],
        "description": "Проектный контекст: репозиторий",
    },
    {
        "id": "project_decision_db",
        "category": "project",
        "messages": [{"role": "user", "content": "We decided to use PostgreSQL for the database"}],
        "should_save": True,
        "expected_memory_type": "project",
        "keywords": ["postgresql", "database"],
        "description": "Project decision: database choice (en)",
    },
    {
        "id": "project_freeze_merges",
        "category": "project",
        "messages": [{"role": "user", "content": "Мы замораживаем мерджи в main до 14 апреля"}],
        "should_save": True,
        "expected_memory_type": "project",
        "keywords": ["мердж", "main", "апрел"],
        "description": "Проектный контекст: заморозка мерджей",
    },
]

# ---------------------------------------------------------------------------
# E. References (should save)
# ---------------------------------------------------------------------------

REFERENCES: list[Scenario] = [
    {
        "id": "ref_mws_api_docs",
        "category": "reference",
        "messages": [{"role": "user", "content": "Документация MWS API: https://api.gpt.mws.ru/docs"}],
        "should_save": True,
        "expected_memory_type": "reference",
        "keywords": ["mws", "api", "docs"],
        "description": "Ссылка на документацию API",
    },
    {
        "id": "ref_issue_tracker",
        "category": "reference",
        "messages": [{"role": "user", "content": "Наш трекер задач: https://github.com/potamotus/mts-true-tech-2026/issues"}],
        "should_save": True,
        "expected_memory_type": "reference",
        "keywords": ["github", "issues"],
        "description": "Ссылка на трекер задач",
    },
    {
        "id": "ref_figma",
        "category": "reference",
        "messages": [{"role": "user", "content": "Вот Figma макет: https://figma.com/file/abc123"}],
        "should_save": True,
        "expected_memory_type": "reference",
        "keywords": ["figma"],
        "description": "Ссылка на Figma",
    },
    {
        "id": "ref_confluence",
        "category": "reference",
        "messages": [{"role": "user", "content": "Our design docs are at https://confluence.example.com/wiki/project"}],
        "should_save": True,
        "expected_memory_type": "reference",
        "keywords": ["confluence", "design"],
        "description": "Reference: design docs link (en)",
    },
    {
        "id": "ref_staging_url",
        "category": "reference",
        "messages": [{"role": "user", "content": "Staging environment: https://staging.wikilive.example.com"}],
        "should_save": True,
        "expected_memory_type": "reference",
        "keywords": ["staging"],
        "description": "Reference: staging URL (en)",
    },
]

# ---------------------------------------------------------------------------
# F. Negative cases (should NOT save)
# ---------------------------------------------------------------------------

NEGATIVE: list[Scenario] = [
    {
        "id": "neg_for_loop",
        "category": "negative",
        "messages": [{"role": "user", "content": "Как написать цикл for в Python?"}],
        "should_save": False,
        "expected_memory_type": None,
        "keywords": [],
        "description": "Обычный вопрос о синтаксисе",
    },
    {
        "id": "neg_rest_vs_graphql",
        "category": "negative",
        "messages": [{"role": "user", "content": "Объясни разницу между REST и GraphQL"}],
        "should_save": False,
        "expected_memory_type": None,
        "keywords": [],
        "description": "Технический вопрос без предпочтения",
    },
    {
        "id": "neg_greeting",
        "category": "negative",
        "messages": [{"role": "user", "content": "Привет, как дела?"}],
        "should_save": False,
        "expected_memory_type": None,
        "keywords": [],
        "description": "Обычное приветствие",
    },
    {
        "id": "neg_thanks",
        "category": "negative",
        "messages": [{"role": "user", "content": "Спасибо за помощь!"}],
        "should_save": False,
        "expected_memory_type": None,
        "keywords": [],
        "description": "Благодарность",
    },
    {
        "id": "neg_write_sort",
        "category": "negative",
        "messages": [{"role": "user", "content": "Напиши функцию сортировки"}],
        "should_save": False,
        "expected_memory_type": None,
        "keywords": [],
        "description": "Обычная кодовая задача",
    },
    {
        "id": "neg_di_explain",
        "category": "negative",
        "messages": [{"role": "user", "content": "Что такое dependency injection?"}],
        "should_save": False,
        "expected_memory_type": None,
        "keywords": [],
        "description": "Теоретический вопрос",
    },
    {
        "id": "neg_fix_error",
        "category": "negative",
        "messages": [{"role": "user", "content": "У меня ошибка TypeError: cannot read property 'map' of undefined, как исправить?"}],
        "should_save": False,
        "expected_memory_type": None,
        "keywords": [],
        "description": "Вопрос об ошибке",
    },
    {
        "id": "neg_weather",
        "category": "negative",
        "messages": [{"role": "user", "content": "What's the weather like today?"}],
        "should_save": False,
        "expected_memory_type": None,
        "keywords": [],
        "description": "Off-topic question (en)",
    },
]

# ---------------------------------------------------------------------------
# G. Edge cases
# ---------------------------------------------------------------------------

EDGE: list[Scenario] = [
    {
        "id": "edge_multiple_facts",
        "category": "edge",
        "messages": [{"role": "user", "content": "Я — ML-инженер из Москвы, использую PyTorch, люблю vim, дедлайн 20 апреля"}],
        "should_save": True,
        "expected_memory_type": None,
        "keywords": ["ml", "pytorch", "vim"],
        "description": "Множество фактов в одном сообщении",
    },
    {
        "id": "edge_long_technical_with_pref",
        "category": "edge",
        "messages": [{"role": "user", "content": (
            "Вот архитектура нашего сервиса: у нас есть API gateway на nginx, "
            "за ним FastAPI-бекенд, который ходит в PostgreSQL и Redis. "
            "Для очередей используем RabbitMQ. Фронтенд на Next.js. "
            "Кстати, я предпочитаю использовать SQLAlchemy вместо raw SQL."
        )}],
        "should_save": True,
        "expected_memory_type": None,
        "keywords": ["sqlalchemy"],
        "description": "Длинное техническое сообщение с предпочтением внутри",
    },
    {
        "id": "edge_forget_vim",
        "category": "edge",
        "messages": [{"role": "user", "content": "Забудь что я использую Vim, я перешёл на VS Code"}],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["vs code", "vscode"],
        "description": "Просьба забыть старое и запомнить новое",
    },
    {
        "id": "edge_ambiguous_fact",
        "category": "edge",
        "messages": [{"role": "user", "content": "Python — хороший язык"}],
        "should_save": False,
        "expected_memory_type": None,
        "keywords": [],
        "description": "Неоднозначная формулировка — факт, не предпочтение",
    },
    {
        "id": "edge_contradiction",
        "category": "edge",
        "messages": [
            {"role": "user", "content": "Я люблю Python"},
            {"role": "assistant", "content": "Хорошо, я запомню что вы предпочитаете Python."},
            {"role": "user", "content": "На самом деле я больше люблю Rust"},
        ],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["rust"],
        "description": "Противоречие — последнее высказывание важнее",
    },
    {
        "id": "edge_conditional_pref",
        "category": "edge",
        "messages": [{"role": "user", "content": "Для бекенда я использую Go, а для скриптов — Python"}],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["go", "python"],
        "description": "Условные предпочтения (разные инструменты для разных задач)",
    },
    {
        "id": "edge_sarcasm",
        "category": "edge",
        "messages": [{"role": "user", "content": "О да, я просто обожаю писать на Java (сарказм)"}],
        "should_save": False,
        "expected_memory_type": None,
        "keywords": [],
        "description": "Саркастическое высказывание — не должно сохраняться как предпочтение",
    },
]

# ---------------------------------------------------------------------------
# H. Multilingual
# ---------------------------------------------------------------------------

MULTILINGUAL: list[Scenario] = [
    {
        "id": "multi_ru_workplace",
        "category": "multilingual",
        "messages": [{"role": "user", "content": "Запомни: я работаю в МТС"}],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["мтс", "mts"],
        "description": "Русский: место работы",
    },
    {
        "id": "multi_en_functional",
        "category": "multilingual",
        "messages": [{"role": "user", "content": "Remember that I prefer functional programming"}],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["functional"],
        "description": "English: programming paradigm preference",
    },
    {
        "id": "multi_mixed_react",
        "category": "multilingual",
        "messages": [{"role": "user", "content": "Я люблю использовать React для frontend-а"}],
        "should_save": True,
        "expected_memory_type": "user",
        "keywords": ["react", "frontend"],
        "description": "Смешанный: React для фронтенда",
    },
    {
        "id": "multi_mixed_tech_stack",
        "category": "multilingual",
        "messages": [{"role": "user", "content": "Мы используем Next.js, TailwindCSS и Prisma в нашем проекте"}],
        "should_save": True,
        "expected_memory_type": "project",
        "keywords": ["next.js", "tailwind", "prisma"],
        "description": "Смешанный: технологический стек",
    },
]

# ---------------------------------------------------------------------------
# All scenarios combined
# ---------------------------------------------------------------------------

ALL_SCENARIOS: list[Scenario] = (
    EXPLICIT + IMPLICIT + FEEDBACK + PROJECT + REFERENCES
    + NEGATIVE + EDGE + MULTILINGUAL
)

CATEGORIES = {
    "explicit": EXPLICIT,
    "implicit": IMPLICIT,
    "feedback": FEEDBACK,
    "project": PROJECT,
    "reference": REFERENCES,
    "negative": NEGATIVE,
    "edge": EDGE,
    "multilingual": MULTILINGUAL,
}
