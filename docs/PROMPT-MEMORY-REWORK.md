# Промпт: Ограничения реализации памяти Claude Code в Open WebUI

## Роль

Ты — архитектор системы памяти для чат-приложения на Open WebUI.

## Задача

Изучи как работает система памяти в Claude Code (два файла ниже). Задача — повторить эту архитектуру в Open WebUI. Определи все ограничения, блокеры и компромиссы, которые возникнут при переносе.

## Референсная документация

Прочитай оба файла целиком перед анализом:

1. **`claude-how-it-works/claude-file-system.md`** — полная архитектура файловой системы Claude Code: чтение/запись файлов, PDF, изображения, система памяти (recall, extraction, autoDream), system prompt assembly, CLAUDE.md иерархия, prompts.ts, хранение сессий, все лимиты и константы.

2. **`claude-how-it-works/memory-architecture.md`** — детальный разбор 5 механизмов памяти: inline memory (основной агент пишет через tools), extractMemories (forked agent после каждого хода), session memory (compaction), autoDream (консолидация раз в сутки), team memory sync. Включает: frontmatter parser, system prompt assembly (21 секция), compaction (4 механизма), forked agent механика (cache sharing), write carve-out, security.

## Контекст: Open WebUI

- RAG pipeline: chunking → bge-m3 embeddings → Chroma
- `/api/memories` — встроенные memories (key-value)
- Filter functions (inlet/outlet) — точки инъекции в pipeline
- Кастомные `gpthub_memory_extractor.py` и `gpthub_memory_injector.py` уже есть
- MWS GPT API (OpenAI-compatible)
- Docker deployment

## Что нужно от тебя

Прочитай оба файла. Пойми архитектуру памяти Claude Code целиком. Затем ответь:

**Какие ограничения возникнут при повторении этой архитектуры в Open WebUI?**

Для каждого ограничения:
- Что именно мешает
- Severity: blocker / significant / minor
- Workaround: как обойти
- Рекомендация: что делать

В конце — итоговая таблица и рекомендуемая архитектура.

## Ограничения платформы

- Без форка Open WebUI — только filter functions или внешние сервисы
- MWS GPT API — единственный LLM endpoint
- Docker deployment (volume mount ок, хост-FS — нет)
- Бюджет API ограничен (хакатон)
