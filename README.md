# Hackathon Team Repo

## AGENT INSTRUCTIONS

If you are an AI agent (Claude, Cursor, Copilot, etc.), read this file carefully and follow all rules.

### Core Rules
1. **Read before writing** — never assume file contents, always read first
2. **Follow existing patterns** — match naming, style, and structure of surrounding code
3. **Feature branches only** — create `feature/<short-description>`, never push to `main` directly
4. **Small commits** — many small commits > one huge commit
5. **No secrets** — never commit `.env` files or credentials
6. **Build before PR** — run the build/lint command and verify it passes before claiming work is done
7. **One task = one branch = one PR**
8. **Don't over-engineer** — hackathon means ship fast, not perfect
9. **Delete unused code** — no commented-out blocks, no dead files
10. **Ask if ambiguous** — if the task is unclear, ask clarifying questions before proceeding

### Conventional Commits
- `feat:` new feature
- `fix:` bug fix
- `chore:` config, deps, tooling
- `docs:` documentation
- `refactor:` code restructuring (no behavior change)

### Task Tracking
- Use GitHub Issues — create issue per task, close on merge
- Reference issues in PR descriptions: `Closes #1`

---

## Karpathy Guidelines

Behavioral guidelines to reduce common LLM coding mistakes, derived from [Andrej Karpathy's observations](https://x.com/karpathy/status/2015883857489522876) on LLM coding pitfalls.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.
- Remove imports/variables/functions that YOUR changes made unused.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

---

## Team Workflow

1. **Clone:** `git clone https://github.com/potamotus/mts-true-tech-2026-certified-turtles.git`
2. **Branch:** `git checkout -b feature/<name>`
3. **Work → Commit → Push → PR** into `main`
4. **Review:** at least 1 approve before merge

## Deploy

Connected to Vercel. Every push to `main` = auto-deploy.
Preview: https://mts-true-hack-certified-turtles.vercel.app


Architecture:

### GPTHub (OpenWebUI + MWS GPT)

1. Скопируйте `.env.example` → `.env`, укажите `MWS_API_KEY` (и при желании `WEBUI_SECRET_KEY`).
2. Запуск: `docker compose up --build`
3. UI: [http://localhost:3000](http://localhost:3000) (порт задаётся `OPEN_WEBUI_PORT`).
4. **Модели:** список подтягивается через наш FastAPI (`GET /v1/models` → MWS). В шапке чата выберите модель **вручную** (автовыбор — отдельная задача).
5. **Архитектура:** Open WebUI → FastAPI-прокси (`api` в compose) → MWS GPT. Открытая точка из UI — `OPENAI_API_BASE_URL=http://api:8000/v1`. Благодаря этому **любой** чат из UI проходит через агент-цикл, и в исходящий запрос к модели автоматически инжектятся все зарегистрированные тулы (`register_tool`, см. `src/certified_turtles/tools/builtins/`) и под-агенты (`agents/registry.py` как `agent_{id}`, напр. `agent_research`).
6. Единая точка входа в LLM — `certified_turtles.services.llm.LLMService`: `list_models()`, `chat(...)` (single-shot с автоинъекцией тулов), `run_agent(...)` (полный tool-calling loop). Все API-эндпоинты и CLI идут через неё, отдельные `MWSGPTClient` по сервису не плодим.
7. Эндпоинты FastAPI (`http://localhost:8000`):
   - `GET /health`
   - `GET /v1/models`, `POST /v1/chat/completions` — OpenAI-совместимый прокси для Open WebUI (`stream` поддерживается псевдо-чанком).
   - `POST /api/v1/agent/chat` — наш собственный шейп агент-цикла (оставлен для CLI/скриптов).
   - `POST /api/v1/uploads` — загрузка файла в рабочую область агента (multipart); дальше тул `read_workspace_file` по полю `file_id`.
   - `GET /files/{filename}` — раздача файлов из `GENERATED_FILES_DIR` (например `.pptx`).
   - `GET /files/python_runs/{run_id}/{filename}` — артефакты после `execute_python` (графики и т.д.). В docker-compose монтируется том `generated-files:/data/generated` (внутри также `uploads/`).
   CLI: `uv run mws-gpt agent --model <id> -p "…"`.

### Доступные тулы и под-агенты

Примитивные тулы (`register_tool`, подключаются автоматически):

- **`web_search`** — текстовый поиск в DuckDuckGo (`ddgs`). Отбрасывает URL-запросы и подсказывает вызвать `fetch_url`.
- **`fetch_url`** — скачивание страницы и преобразование HTML→text (stdlib, без браузера).
- **`generate_image`** — генерация картинки через Pollinations.ai (free-tier, без ключа). Возвращает URL и готовую markdown-вставку `![](…)`, которую Open WebUI рендерит инлайн.
- **`generate_presentation`** — сборка настоящего `.pptx` через `python-pptx`. Файл пишется в `GENERATED_FILES_DIR`, раздаётся через `/files/{name}.pptx`, URL строится из `PUBLIC_API_BASE_URL`.
- **`read_workspace_file`** — чтение текста из файла, предварительно загруженного через `POST /api/v1/uploads` (поле `file_id`).
- **`execute_python`** — запуск ограниченного Python (отдельный процесс, таймаут) для анализа данных и графиков (`numpy` / `matplotlib` / `pandas`); графики сохранять в `CT_RUN_OUTPUT_DIR`, ссылки — `GET /files/python_runs/{run_id}/…`.
- **`google_docs_read` / `google_docs_append`** — чтение текста и дописывание в конец Google Doc через API; нужен JSON service account и расшаривание документа на его email (`GOOGLE_DOCS_CREDENTIALS_JSON`, см. `.env.example`). Зависимости: optional `google` в `pyproject.toml` (в Docker-образе включено).

Под-агенты (`agents/registry.py`, вызываются родительским LLM как `agent_{id}`):

- **`agent_research`** — быстрый ресёрч: `web_search` + `fetch_url`.
- **`agent_writer`** — только текст, без внешних инструментов (сокращение/переписывание).
- **`agent_deep_research`** — многошаговое глубокое исследование: декомпозирует вопрос, итеративно ходит `web_search` → `fetch_url`, в конце отдаёт markdown-отчёт (TL;DR, ключевые выводы, источники). До 16 внутренних раундов.

### Голос и live-режим

MWS GPT не предоставляет audio-эндпоинтов, поэтому STT/TTS не проксируется через бэкенд — вместо этого в `docker-compose.yml` включены переменные Open WebUI `AUDIO_STT_ENGINE=web`, `AUDIO_TTS_ENGINE=web`, `ENABLE_AUDIO_CONVERSATION=True`. Это активирует **Web Speech API** в браузере: распознавание голоса и синтез речи — клиентсайдом. Кнопка «Call» в Open WebUI даёт непрерывный голосовой диалог (live-режим) поверх нашего tool-агента.

**Без Docker (только uv):** из **корня репозитория** — `uv sync --extra openwebui`. В `.env` должен быть `MWS_API_KEY`. Перед запуском WebUI экспортируйте MWS в переменные, которые ждёт Open WebUI (в одной оболочке):

```bash
set -a && source .env && set +a
export OPENAI_API_KEY="${MWS_API_KEY}"
export OPENAI_API_BASE_URL="${MWS_API_BASE:-https://api.gpt.mws.ru}/v1"
export ENABLE_OLLAMA_API=False
export ENABLE_OPENAI_API=True
export BYPASS_MODEL_ACCESS_CONTROL=True
uv run open-webui serve
```

Интерфейс: **http://localhost:8080** (не 3000 — это порт «pip/serve» по умолчанию). Остановка: Ctrl+C.

Отдельно без WebUI: `uv run mws-gpt …` или `uv run uvicorn certified_turtles.main:app --reload --host 0.0.0.0 --port 8000`. Модуль CLI: `certified_turtles.mws_gpt`, не `mws_gpt`.
