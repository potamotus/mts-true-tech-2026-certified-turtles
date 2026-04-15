# GPTHub — Архитектура проекта

## Общая схема

```
┌────────────────────┐    ┌──────────────────┐    ┌──────────────┐
│  Open WebUI :3000  │    │  Figma Plugin    │    │  Любой клиент│
│  (Docker, форк)    │    │  (iframe :8000)  │    │  (curl, etc) │
└────────┬───────────┘    └────────┬─────────┘    └──────┬───────┘
         │                         │                      │
         └─────────────────────────┼──────────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────┐
                    │   FastAPI :8000           │
                    │   certified_turtles/main  │
                    │                          │
                    │  /v1/chat/completions     │  ← агент + тулы
                    │  /v1/plain/...            │  ← обычный чат
                    │  /v1/m/{mode}/...         │  ← режимы
                    │  /v1/audio/...            │  ← ASR (Whisper)
                    │  /v1/images/...           │  ← генерация картинок
                    │  /v1/models               │  ← список моделей
                    │  /figma                   │  ← UI Figma-плагина
                    │  /api/v1/memory           │  ← память
                    │  /api/v1/uploads          │  ← загрузка файлов
                    └────────────┬─────────────┘
                                 │
                                 ▼
                    ┌──────────────────────────┐
                    │  MWS GPT API             │
                    │  api.gpt.mws.ru/v1       │
                    │  (OpenAI-совместимый)     │
                    └──────────────────────────┘
```

---

## 1. FastAPI бэкенд

**Точка входа** — `src/certified_turtles/main.py`. CORS открыт, логирование бизнес-запросов, 8 роутеров.

### Ключевые эндпоинты

| Путь | Что делает |
|------|-----------|
| `POST /v1/chat/completions` | Чат **с агентом** — AI сам вызывает тулы |
| `POST /v1/plain/chat/completions` | Обычный чат без тулов |
| `POST /v1/m/{mode}/chat/completions` | Чат в режиме (deep_research, coder...) |
| `POST /v1/audio/transcriptions` | Прокси ASR → MWS Whisper |
| `POST /v1/images/generations` | Генерация картинок (qwen-image) |
| `GET /v1/models` | Список моделей из MWS |
| `GET /figma` | UI Figma-плагина |
| `POST /api/v1/uploads` | Загрузка файлов в workspace |
| `/api/v1/memory` | CRUD для долгосрочной памяти |

### Роутеры

| Модуль | Префикс | Назначение |
|--------|---------|-----------|
| `openai_proxy.py` | `/v1` | Основной OpenAI-совместимый прокси (821 строка) |
| `agent.py` | `/api/v1` | Выделенный endpoint агентного чата |
| `memory.py` | `/api/v1` | Управление долгосрочной памятью |
| `instructions.py` | `/api/v1` | Пользовательские системные промпты |
| `uploads.py` | `/api/v1` | Загрузка файлов |
| `agent_config.py` | `/api/v1` | Конфигурация поведения агента |
| `mws_tables_config.py` | `/api/v1` | Интеграция с MWS Tables |
| `files.py` | `/files` | Раздача сгенерированных файлов |

---

## 2. Агентный цикл

Сердце системы — `agents/loop.py`. Когда приходит запрос на `/v1/chat/completions`:

```
1. Нормализация сообщений
2. Инъекция системного промпта + каталог тулов
3. Цикл (до 40 раундов):
   ├── Отправить в MWS GPT (с tools)
   ├── Получить ответ
   ├── Есть tool_calls? → выполнить тулы
   │   ├── Примитивный тул (web_search, execute_python...)
   │   └── Саб-агент (agent_research, agent_coder...)
   ├── Добавить результат в историю
   └── Повторить
4. Финальный ответ → SSE stream → клиенту
```

### Ключевые функции

- **`stream_agent_chat()`** — стриминговый агент (yields reasoning/status/content/done)
- **`run_agent_chat()`** — блокирующий агент (возвращает полный результат)

### Особенности

- **Forced sub-agent mode** — родительская LLM делегирует всё специалисту
- **Deep research mode** — маршрутизация в GPT Researcher (отдельный subprocess)
- **Tool result injection** — полные tool_calls + "tool" role messages
- **Trace collection** — каждое решение порождает event для стриминга/логирования
- **Sub-agent recursion** — вложенные вызовы агентов до глубины 3

---

## 3. Тулы

### Встроенные (`tools/builtins/`)

| Тул | Назначение |
|-----|-----------|
| `web_search` | Поиск через DuckDuckGo |
| `fetch_url` | Скачать и распарсить веб-страницу |
| `execute_python` | Запуск Python в песочнице (numpy, pandas, matplotlib) |
| `read_workspace_file` | Чтение загруженного файла |
| `workspace_file_path` | Путь к файлу для других тулов |
| `generate_image` | Генерация картинок (Pollinations.ai) |
| `generate_presentation` | Создание .pptx из шаблона |
| `transcribe_workspace_audio` | Расшифровка аудио (Whisper) |
| `google_docs_read` / `google_docs_append` | Google Docs (service account) |
| `mws_tables` | MWS Tables (tabs.mts.ru) |

### Мета-тулы — делегация саб-агентам (`parent_tools.py`)

`agent_research`, `agent_coder`, `agent_data_analyst`, `agent_writer`, `agent_deep_research`, `agent_presentation`

### Система регистрации (`tools/registry.py`)

- `ToolSpec` — dataclass: name, description, JSON schema параметров, handler
- `register_tool()` — регистрация тула
- `openai_tools_for_names()` — конвертация в OpenAI tool definitions
- `run_primitive_tool()` — выполнение handler + JSON-сериализация

---

## 4. Саб-агенты

Определены в `agents/registry.py`. Каждый со своим системным промптом из `prompts/subagents/*.md`:

| Агент | Тулы | Раунды | Для чего |
|-------|------|--------|---------|
| `research` | web_search, fetch_url | 8 | Быстрый поиск + фактчекинг |
| `writer` | — | 4 | Только текст (суммаризация, рерайт) |
| `deep_research` | GPT Researcher (subprocess) | 1 | Глубокие исследовательские отчёты |
| `coder` | execute_python, read_file, file_path | 12 | Код + анализ файлов |
| `data_analyst` | execute_python, file_path, read_file | 14 | CSV/XLSX + графики |
| `presentation` | web_search, fetch_url, generate_presentation | 10 | Создание .pptx |

### Механизм делегации

```
1. Агент решает вызвать tool "agent_research"
2. _execute_tool_call() детектирует agent_id="research"
3. _invoke_subagent():
   ├── Получает SubAgentSpec (системный промпт + тулы)
   ├── Извлекает задачу из аргументов
   ├── Формирует inner_messages (system + user)
   ├── Вызывает stream_agent_chat() рекурсивно
   └── Возвращает результат как tool output
4. Родительский агент включает результат в финальный ответ
```

---

## 5. Режимы чата

Определены в `chat_modes.py`. Активируются тремя способами:

- **URL:** `/v1/m/deep_research/chat/completions`
- **JSON-поле:** `ct_mode` в теле запроса
- **Префикс:** `[CT_MODE:coder]` в последнем сообщении

### Доступные режимы

| Режим | Max раундов | Forced агент | Промпт |
|-------|------------|-------------|--------|
| `deep_research` | 36 | deep_research | `prompts/modes/deep_research.md` |
| `research` | 14 | research | `prompts/modes/research.md` |
| `coder` | 16 | coder | `prompts/modes/coder.md` |
| `data_analyst` | 18 | data_analyst | `prompts/modes/data_analyst.md` |
| `writer` | 10 | writer | `prompts/modes/writer.md` |
| `presentation` | 14 | presentation | — |

### Как работает

```
1. prepare_chat_request() копирует messages и извлекает ct_mode
2. Если найден режим → подгружает системный промпт
3. Устанавливает max_tool_rounds override
4. Устанавливает forced_agent_id
```

---

## 6. Авто-роутинг моделей

`mws_gpt/router.py` — когда `model="auto"`:

```
1. Определяет тип входа (текст vs image_url → VLM)
2. Классифицирует задачу через LLM (classify_task())
   └── Vision / Reasoning / Creative / Coding / Analysis
3. Выбирает лучшую модель для задачи (get_best_model_for_task())
4. Фолбэк-цепочка если модель недоступна
5. Возвращает RoutingResult {model, task_type, reason}
```

---

## 7. LLM Service и MWS GPT Client

### LLMService (`services/llm.py`)

Единый фасад для всех LLM операций:

```python
class LLMService:
    list_models()           # GET /v1/models
    chat()                  # Чат с авто-тулами
    chat_plain()            # Чат без тулов
    chat_plain_stream()     # Стриминговый чат (SSE)
    run_agent()             # Полный агентный цикл (blocking)
    stream_agent()          # Стриминговый агент (yields events)
    images_generations()    # Генерация изображений
```

### MWS GPT Client (`mws_gpt/client.py`)

HTTP-клиент для OpenAI-совместимого API MWS:

- **Timeout:** 30–600 секунд (настраивается `MWS_HTTP_TIMEOUT_SEC`)
- **Retries:** 0–5 (настраивается `MWS_HTTP_RETRIES`)
- **Методы:** `list_models()`, `chat_completions()`, `chat_completions_stream()`, `audio_transcriptions()`, `images_generations()`

---

## 8. Память

### API (`api/memory.py`)

| Метод | Путь | Назначение |
|-------|------|-----------|
| GET | `/api/v1/memory` | Список memories для scope |
| GET | `/api/v1/memory/{filename}` | Чтение одной записи |
| PUT | `/api/v1/memory/{filename}` | Создание/обновление |
| DELETE | `/api/v1/memory/{filename}` | Удаление |
| GET | `/api/v1/memory-events` | SSE поток событий |
| POST | `/api/v1/memory-dream` | Консолидация памяти |

### Хранение

Frontmatter markdown файлы в scope-директориях. Async event bus для real-time обновлений.

### Runtime (`memory_runtime/`)

- `prepare_messages()` — инъекция контекста памяти перед запросом
- `after_response()` — post-hook для консолидации после ответа

---

## 9. Docker

### Сервисы (`docker-compose.yml`)

```
┌─────────────────────────────────────────────┐
│  docker-compose                              │
│                                              │
│  ┌─────────────┐      ┌──────────────────┐  │
│  │ api :8000   │◄─────│ open-webui :3000  │  │
│  │ FastAPI     │      │ (форк)            │  │
│  │ + uvicorn   │      │ depends_on: api   │  │
│  └──────┬──────┘      └──────────────────┘  │
│         │                                    │
│  Volumes:                                    │
│  - generated-files:/data/generated           │
│  - open-webui-data:/app/backend/data         │
│  - ./src:/app/src (hot-reload)               │
└─────────────────────────────────────────────┘
```

### Dev override (`docker-compose.dev.yml`)

Open WebUI подключается к `host.docker.internal:8000` — API работает локально.

```bash
# Запуск в dev-режиме
./dev.sh
# → Open WebUI в Docker на :3000
# → API локально на :8000 с --reload
# → Figma plugin на :8000/figma
```

### Dockerfile

- Python 3.12-slim + uv
- Отдельный venv для GPT Researcher (`/opt/gpt-researcher-venv`)

---

## 10. Open WebUI форк

Кастомизации относительно стокового Open WebUI:

- **Селектор режимов чата** — dropdown (Deep Research, Web, Slides, Code, Data, Text), инжектирует `ct_mode`
- **Dual API:** основной (`api:8000/v1` с агентом) + plain (`/v1/plain`)
- **Голос:** STT через `AUDIO_STT_ENGINE=openai` → прокси `/v1/audio/transcriptions`, TTS — Web Speech API
- **Все модели доступны** (`BYPASS_MODEL_ACCESS_CONTROL: True`)
- **Locale:** `ru-RU` по умолчанию

---

## 11. Figma-плагин

Встроен в основной API — UI раздаётся с `GET /figma`, без отдельного сервера.

### Возможности

**Текстовые операции:**
- Rewrite, Shorten, Expand, Fix grammar
- Перевод (EN ↔ RU)
- Replace selected — замена текста в слое на месте

**Генерация дизайна (JSON → Figma элементы):**
- Slide — один слайд по теме
- Presentation — набор из 4–5 слайдов
- Card — UI-карточка
- Hero section — заголовок + CTA + декор
- Features — секция с фичами
- Mobile screen — wireframe мобильного экрана

### Архитектура

```
Figma Desktop
  └── plugin/code.js (sandbox)
        └── iframe → http://localhost:8000/figma
              ├── figmaAPI bridge (postMessage)
              ├── fetch POST /v1/chat/completions (same origin)
              └── Рендеринг: JSON → figma.createFrame/Text/Rectangle/Ellipse
```

---

## 12. Полный путь сообщения

```
Пользователь: "Найди что нового в AI за неделю"
  │
  ▼ Open WebUI (:3000)
POST http://api:8000/v1/chat/completions
  {model: "gpt-4o-mini", messages: [...], stream: true}
  │
  ▼ openai_proxy.py
  ├── prepare_chat_request() → определяет ct_mode, инжектит промпт
  ├── runtime.prepare_messages() → добавляет контекст памяти
  └── Решение: plain или agent? → agent
  │
  ▼ agents/loop.py — stream_agent_chat()
  │
  │  Раунд 1: MWS GPT решает вызвать tool "agent_research"
  │  │
  │  ▼ Саб-агент research (рекурсивный вызов)
  │  ├── Раунд 1: web_search("AI news this week") → результаты
  │  ├── Раунд 2: fetch_url(топ-3 ссылки) → текст страниц
  │  └── Раунд 3: формирует структурированный ответ
  │  │
  │  ▼ Результат саб-агента → в историю родительского агента
  │
  │  Раунд 2: MWS GPT формирует финальный ответ пользователю
  │
  ▼ patch_completion_assistant_markdown()
  ├── Извлекает user-visible текст
  ├── Убирает tool/reasoning протокольный шум
  └── Форматирует как SSE chunks (≤400 char)
  │
  ▼ Open WebUI получает SSE stream
  ├── Рендерит markdown
  └── Показывает reasoning steps + финальный ответ
  │
  ▼ Пользователь видит ответ с источниками
```

---

## 13. Deep Research — отдельный путь

```
1. ct_mode=deep_research → forced_agent_id="deep_research"
2. stream_agent_chat() вызывает _stream_deep_research_gpt_researcher()
3. gpt_researcher_runner.run_gpt_researcher_sync_with_meta()
4. Subprocess:
   ├── Python: /opt/gpt-researcher-venv/bin/python
   ├── Worker: scripts/gpt_researcher_worker.py
   ├── Input: JSON {query, report_type, llm_model}
   └── Output: JSON {ok, report} (prefix "__CT_GPTR_JSON__:")
5. Отчёт → final_event → SSE → пользователь
```

---

## 14. Структура проекта

```
certified-turtles/
├── src/certified_turtles/
│   ├── main.py                         # FastAPI app + роутеры
│   ├── agents/
│   │   ├── loop.py                     # Агентный цикл (1020 строк)
│   │   ├── registry.py                 # Спецификации саб-агентов
│   │   ├── json_agent_protocol.py      # Парсинг ответов агента
│   │   └── execute_python_intent.py    # Контроль Python-выполнения
│   ├── api/
│   │   ├── openai_proxy.py             # OpenAI-совместимый прокси (821 строка)
│   │   ├── agent.py                    # /api/v1/agent/chat
│   │   ├── memory.py                   # Управление памятью
│   │   ├── uploads.py                  # Загрузка файлов
│   │   ├── instructions.py             # Системные промпты
│   │   ├── files.py                    # Раздача файлов
│   │   ├── agent_config.py             # Конфигурация агента
│   │   └── mws_tables_config.py        # MWS Tables
│   ├── tools/
│   │   ├── registry.py                 # Система регистрации тулов
│   │   ├── builtins/                   # 10+ встроенных тулов
│   │   ├── parent_tools.py             # Тулы делегации саб-агентам
│   │   └── workspace_storage.py        # Файловые операции
│   ├── services/
│   │   ├── llm.py                      # LLMService — единый фасад
│   │   └── message_normalize.py        # Нормализация сообщений
│   ├── mws_gpt/
│   │   ├── client.py                   # HTTP-клиент к MWS GPT
│   │   ├── router.py                   # Авто-выбор модели
│   │   ├── classifier.py              # Классификация задач
│   │   └── model_config.py            # Конфигурация моделей
│   ├── integrations/
│   │   └── gpt_researcher_runner.py    # GPT Researcher обёртка
│   ├── chat_modes.py                   # Система режимов чата
│   ├── model_mode.py                   # Виртуальные модели
│   ├── memory_runtime/                 # Post-hook система памяти
│   ├── prompts/                        # Все системные промпты
│   │   ├── agent_streaming_system.md   # Базовый промпт агента
│   │   ├── subagents/                  # Промпты саб-агентов
│   │   └── modes/                      # Промпты режимов
│   ├── static/                         # HTML (memory, figma)
│   └── assets/                         # Шаблон презентации
├── open-webui/                         # Форк Open WebUI
├── figma-plugin/                       # Figma-плагин (legacy Next.js)
│   └── plugin/
│       ├── manifest.json               # Манифест плагина
│       └── dist/code.js                # Собранный код (→ :8000/figma)
├── scripts/
│   ├── bootstrap_gpt_researcher_venv.sh
│   ├── gpt_researcher_worker.py
│   └── inject-memory-toast.sh
├── externals/
│   └── requirements-gpt-researcher.txt
├── tests/                              # Pytest тесты
├── benchmarks/                         # Бенчмарки
├── docker-compose.yml                  # Основной compose
├── docker-compose.dev.yml              # Dev override
├── Dockerfile                          # Образ API
├── dev.sh                              # Скрипт запуска для разработки
├── pyproject.toml                      # uv конфигурация
└── uv.lock                             # Lock-файл зависимостей
```

---

## 15. Переменные окружения

### MWS GPT

| Переменная | По умолчанию | Назначение |
|-----------|-------------|-----------|
| `MWS_API_KEY` | — | Bearer token (обязательно) |
| `MWS_API_BASE` | `https://api.gpt.mws.ru` | Endpoint MWS GPT |
| `MWS_HTTP_TIMEOUT_SEC` | 120 | Таймаут HTTP (30–600) |
| `MWS_HTTP_RETRIES` | 2 | Кол-во ретраев (0–5) |

### Агент

| Переменная | По умолчанию | Назначение |
|-----------|-------------|-----------|
| `CT_AGENT_DEBUG` | 1 | Подробное логирование |
| `CT_AGENT_DEBUG_MAX_CHARS` | — | Обрезка логов |
| `CT_AGENT_STREAM_CHAT` | 1 | SSE стриминг |
| `CT_MWS_IMAGE_CHAT_MODELS` | `qwen-image,...` | Модели для images endpoint |
| `CT_LIST_MODE_VARIANTS` | 0 | Показывать режимы в /v1/models |

### ASR / Голос

| Переменная | По умолчанию | Назначение |
|-----------|-------------|-----------|
| `CT_ASR_MODEL` | `whisper-medium` | ID модели Whisper в MWS |
| `AUDIO_STT_ENGINE` | `web` | Web или openai |

### Хранилище

| Переменная | По умолчанию | Назначение |
|-----------|-------------|-----------|
| `GENERATED_FILES_DIR` | `/data/generated` | Артефакты |
| `UPLOADS_DIR` | `/data/generated/uploads` | Загрузки |
| `PUBLIC_API_BASE_URL` | `http://localhost:8000` | Публичный URL для файлов |

### Интеграции (опционально)

| Переменная | Назначение |
|-----------|-----------|
| `GOOGLE_DOCS_CREDENTIALS_JSON` | Путь к service account JSON |
| `MWS_TABLES_API_TOKEN` | Токен MWS Tables |
| `MWS_TABLES_API_BASE` | Endpoint MWS Tables |
| `GPT_RESEARCHER_PYTHON` | Путь к Python в venv |
| `GPT_RESEARCHER_TIMEOUT_SEC` | Таймаут subprocess (default: 3600) |

### Open WebUI (в docker-compose)

| Переменная | По умолчанию | Назначение |
|-----------|-------------|-----------|
| `OPENAI_API_KEY` | из `MWS_API_KEY` | Ключ для Open WebUI |
| `OPENAI_API_BASE_URL` | `http://api:8000/v1` | Куда ходить за моделями |
| `WEBUI_NAME` | `GPTHub` | Название в UI |
| `WEBUI_SECRET_KEY` | `change-me-in-production` | Секрет сессии |
| `DEFAULT_LOCALE` | `ru-RU` | Язык интерфейса |
