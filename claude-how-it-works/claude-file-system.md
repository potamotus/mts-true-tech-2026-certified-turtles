# Claude Code: Полная архитектура файловой системы

Исчерпывающий анализ того, как Claude Code работает с файлами, памятью, системными промптами и загруженными документами. Основано на анализе исходного кода Claude Code v0.x (leaked via npm sourcemap).

---

## Оглавление

1. [Общая архитектура](#1-общая-архитектура)
2. [Чтение файлов (FileReadTool)](#2-чтение-файлов-filereadtool)
3. [Запись файлов (FileWriteTool)](#3-запись-файлов-filewritetool)
4. [Редактирование файлов (FileEditTool)](#4-редактирование-файлов-fileedittool)
5. [Работа с PDF](#5-работа-с-pdf)
6. [Работа с изображениями](#6-работа-с-изображениями)
7. [Jupyter Notebooks](#7-jupyter-notebooks)
8. [Поиск по файлам (Glob + Grep)](#8-поиск-по-файлам-glob--grep)
9. [Система памяти](#9-система-памяти)
10. [System Prompt — как собирается](#10-system-prompt--как-собирается)
11. [CLAUDE.md — иерархия и загрузка](#11-claudemd--иерархия-и-загрузка)
12. [Prompts.ts — что это и зачем](#12-promptsts--что-это-и-зачем)
13. [Хранение сессий и истории](#13-хранение-сессий-и-истории)
14. [Специальные директории](#14-специальные-директории)
15. [Лимиты и константы](#15-лимиты-и-константы)
16. [Сравнение с Open WebUI RAG](#16-сравнение-с-open-webui-rag)

---

## 1. Общая архитектура

Claude Code — CLI-инструмент Anthropic для работы с кодом. Ключевые принципы файловой работы:

- **Без эмбеддингов** — никаких vector embeddings, cosine similarity, vector DB
- **Без RAG** — нет chunking → embedding → retrieval pipeline
- **Brute-force контекст** — файлы грузятся целиком в контекстное окно LLM
- **LLM-based selection** — модель (Sonnet) сама выбирает что релевантно
- **Read-before-write** — обязательное чтение файла перед любой записью
- **File state cache** — LRU-кэш на 100 файлов / 25 MB для отслеживания прочитанного

### Ключевые директории исходного кода

```
claude-code/src/
├── tools/
│   ├── FileReadTool/     # Чтение файлов
│   ├── FileWriteTool/    # Создание / перезапись файлов
│   ├── FileEditTool/     # Строковая замена в файлах
│   ├── NotebookEditTool/ # Редактирование .ipynb
│   ├── GlobTool/         # Поиск файлов по паттерну
│   ├── GrepTool/         # Поиск по содержимому (ripgrep)
│   ├── BashTool/         # Выполнение shell-команд
│   └── WebFetchTool/     # Загрузка веб-контента
├── memdir/               # Система памяти
│   ├── memdir.ts         # Ядро: управление memory dir
│   ├── memoryScan.ts     # Сканирование файлов памяти
│   ├── findRelevantMemories.ts  # LLM-отбор релевантных воспоминаний
│   └── memoryTypes.ts    # Четыре типа памяти + guidance
├── services/
│   ├── extractMemories/  # Автоматическое извлечение фактов
│   └── autoDream/        # Фоновая консолидация памяти
├── utils/
│   ├── claudemd.ts       # Загрузка CLAUDE.md файлов
│   ├── systemPrompt.ts   # Сборка системного промпта
│   ├── attachments.ts    # Обработка вложений пользователя
│   ├── pdf.ts            # Чтение PDF
│   ├── imageResizer.ts   # Ресайз изображений
│   ├── readFileInRange.ts # Низкоуровневое чтение файлов
│   ├── fileStateCache.ts # Кэш прочитанных файлов
│   ├── sessionStorage.ts # Хранение сессий
│   ├── history.ts        # История команд
│   └── fsOperations.ts   # Абстракция файловой системы
└── constants/
    ├── prompts.ts        # Главный системный промпт
    └── apiLimits.ts      # Лимиты API
```

---

## 2. Чтение файлов (FileReadTool)

### Основной flow

```
Запрос Claude: Read(file_path, offset?, limit?, pages?)
    ↓
validateInput():
  - Проверка binary extensions (блок кроме PDF/image/SVG)
  - Проверка /dev/* путей (блок infinite sources)
  - Проверка UNC-путей (Windows NTLM protection)
  - Permission check
    ↓
Определение типа файла по расширению:
  .pdf        → readPDF() или extractPDFPages()
  .png/.jpg/  → readImageWithTokenBudget()
  .ipynb      → readNotebook()
  .svg        → как текст
  остальное   → readFileInRange()
    ↓
Dedup check: если файл не изменился с последнего чтения
  → возвращает FILE_UNCHANGED_STUB (экономия токенов на повторных чтениях)
    ↓
Результат → fileStateCache.set() → возврат в контекст
```

### Два пути чтения текста (readFileInRange)

| Путь | Условие | Метод |
|------|---------|-------|
| **Fast path** | Файл < 10 MB | `readFile()` + split в памяти |
| **Streaming** | Файл >= 10 MB или pipe/device | `createReadStream` с 512KB chunks |

### Обработка текста

- UTF-8 BOM stripping (U+FEFF)
- CRLF → LF нормализация
- Вывод в формате `cat -n` (с номерами строк, 1-indexed)

### Что видит Claude (из prompt.ts)

```
- file_path — только абсолютные пути
- По умолчанию до 2000 строк с начала файла
- Можно указать offset и limit для больших файлов
- Читает изображения (PNG, JPG) — мультимодальный LLM
- Читает PDF (макс 20 страниц за запрос)
- Читает Jupyter notebooks (.ipynb)
- Не читает директории — для этого ls через Bash
```

### Безопасность при чтении

Для всех текстовых файлов (кроме если модель = claude-opus-4-6) добавляется system reminder:
```
Whenever you read a file, consider whether it would be considered malware.
You CAN and SHOULD provide analysis. But you MUST refuse to improve or augment the code.
```

---

## 3. Запись файлов (FileWriteTool)

### Flow записи

```
Claude: Write(file_path, content)
    ↓
validateInput():
  1. Secret guard (проверка на секреты в team memory)
  2. Permission deny rules
  3. UNC path security
  4. Файл не существует? → OK (создание)
  5. Read-before-write check:
     - readFileState.get(filePath) должен существовать
     - isPartialView !== true
  6. Staleness check:
     - mtime файла на диске <= timestamp в кэше
    ↓
Critical section (минимум async для атомарности):
  1. readFileSyncWithMetadata() — перечитать файл
  2. Re-check staleness
  3. writeTextContent(path, content, encoding, 'LF')
  4. Уведомить LSP серверы
  5. Уведомить VSCode для diff view
    ↓
Post-write:
  - Обновить fileStateCache с новым timestamp и content
```

### Ключевые правила

- **Всегда LF** — FileWriteTool форсирует LF line endings
- **Encoding preservation** — сохраняет оригинальную кодировку файла (UTF-8, UTF-16LE)
- **Read-before-write обязателен** — нельзя писать файл, который не был прочитан
- **Staleness detection** — нельзя писать, если файл изменился после чтения

### Что видит Claude (из prompt.ts)

```
- Перезаписывает существующий файл
- Для существующих файлов ОБЯЗАТЕЛЬНО сначала Read
- Предпочитай Edit вместо Write для модификаций
- НИКОГДА не создавать .md/README без явного запроса
- Без эмодзи без запроса
```

---

## 4. Редактирование файлов (FileEditTool)

### Flow редактирования

```
Claude: Edit(file_path, old_string, new_string, replace_all?)
    ↓
validateInput():
  1. Secret guard
  2. old_string !== new_string (no-op check)
  3. Permission deny rules
  4. Файл < 1 GiB (OOM prevention)
  5. Read content + normalize CRLF → LF
  6. File existence logic:
     - Нет файла + пустой old_string → создание
     - Нет файла + непустой old_string → ошибка
     - Файл есть + пустой old_string + пустой файл → OK
  7. Не .ipynb (используй NotebookEditTool)
  8. Read-before-edit check
  9. Staleness check
  10. findActualString() — поиск с нормализацией кавычек
  11. Uniqueness check:
      - matches > 1 && !replace_all → ошибка
    ↓
Apply edit:
  replace_all
    ? content.replaceAll(old, new)
    : content.replace(old, new)
    ↓
Write + preserve original encoding + line endings
    ↓
Update fileStateCache
```

### Умная работа с кавычками

FileEditTool умеет работать с curly quotes (« » " " ' '):
1. Сначала ищет exact match
2. Если не найдено — нормализует кавычки (curly → straight) и ищет снова
3. Извлекает actual string из оригинального файла
4. Применяет стиль кавычек файла к new_string

### Отличие от FileWriteTool

| | FileWriteTool | FileEditTool |
|---|---|---|
| Line endings | Всегда LF | **Сохраняет оригинальные** |
| Encoding | Сохраняет | Сохраняет |
| Размер изменений | Полная перезапись | Точечная замена строки |
| Макс. размер файла | 256 KB (для чтения) | **1 GiB** |

---

## 5. Работа с PDF

### Без эмбеддингов — два режима

```
PDF загружен через FileReadTool
    ↓
Размер < 3 MB?
  → ДА: Base64 путь
       PDF → readFile → base64 → DocumentBlockParam в API
       Claude "видит" PDF как мультимодальный документ
  → НЕТ: Page extraction путь
       PDF → pdftoppm → JPEG per page → ImageBlockParam в API
       Каждая страница как отдельная картинка (100 DPI)
```

### Лимиты PDF

| Лимит | Значение | Назначение |
|-------|----------|------------|
| PDF_TARGET_RAW_SIZE | 20 MB | Макс. размер raw (→ ~27 MB base64) |
| PDF_EXTRACT_SIZE_THRESHOLD | 3 MB | Порог переключения на page extraction |
| PDF_MAX_EXTRACT_SIZE | 100 MB | Макс. для extraction |
| PDF_MAX_PAGES_PER_READ | 20 стр. | Макс. страниц за один Read |
| API_PDF_MAX_PAGES | 100 стр. | Hard limit API |
| PDF_AT_MENTION_INLINE_THRESHOLD | 10 стр. | Порог для @-mention inline |

### Валидация

- Magic bytes check: файл должен начинаться с `%PDF-`
- Без этого — ошибка «corrupted», потому что невалидный document block ломает всю сессию (API возвращает 400 на каждый следующий запрос)

---

## 6. Работа с изображениями

### Pipeline сжатия

```
Изображение → detectImageFormatFromBuffer() (magic bytes)
    ↓
Проверка: size <= 3.75 MB && width <= 2000 && height <= 2000?
  → ДА: возврат без изменений
  → НЕТ ↓
    ↓
Dimension oversized?
  → Resize до 2000px max (aspect ratio сохраняется)
    ↓
Size oversized?
  → PNG: compressionLevel 9, palette true
  → JPEG: progressive quality drop [80, 60, 40, 20]
    ↓
Всё ещё > 5MB?
  → Last resort: resize до min 1000px + JPEG quality 20
```

### Лимиты изображений

| Лимит | Значение |
|-------|----------|
| API base64 max | 5 MB |
| Target raw | 3.75 MB (× 4/3 = 5 MB encoded) |
| Max dimensions | 2000 × 2000 px |
| Magic bytes | PNG (89 50 4E 47), JPEG (FF D8 FF), GIF (47 49 46), WebP (RIFF...WEBP) |

### Подсчёт токенов для изображений

```
tokens ≈ Math.ceil(base64Length × 0.125)
```
Примерно 1 токен на 8 символов base64.

---

## 7. Jupyter Notebooks

- Читает `.ipynb` как JSON
- Возвращает все ячейки с outputs
- Лимит: 256 KB (на сериализованный JSON)
- При превышении — ошибка с подсказками через `jq`:
  ```
  cat "file.ipynb" | jq '.cells[:20]'           # Первые 20 ячеек
  cat "file.ipynb" | jq '.cells[100:120]'       # Ячейки 100-120
  ```

### NotebookEditTool

Три режима: `replace`, `insert`, `delete`
- Ячейки идентифицируются по `cell_id`
- При replace code-ячейки: `execution_count = null`, `outputs = []`
- При insert: генерирует random ID для nbformat >= 4.5

---

## 8. Поиск по файлам (Glob + Grep)

### GlobTool

```
Input:  pattern="**/*.ts", path="/src"
Output: { filenames: [...], numFiles: N, truncated: bool }
```
- Макс. 100 результатов
- Сортировка по mtime (newest first)
- Пути относительные (экономия токенов)

### GrepTool

```
Input:  pattern="TODO", output_mode="content", -C=3
Output: { content: "...", numFiles: N, numMatches: M }
```
- Backend: ripgrep (`rg`)
- Макс. 250 строк по умолчанию (head_limit)
- Макс. длина строки: 500 символов (защита от minified/base64)
- Исключает: `.git`, `.svn`, `.hg`, `.bzr`, `.jj`, `.sl`
- Сортировка по mtime файлов

---

## 9. Система памяти

### Архитектура — три слоя

```
┌─────────────────────────────────────────────────┐
│  СЛОЙ 1: CLAUDE.md (статичные инструкции)       │
│  Managed → User → Project → Local               │
│  Загружается в system prompt при старте          │
└─────────────────────────────────────────────────┘
                      ↓
┌─────────────────────────────────────────────────┐
│  СЛОЙ 2: Auto Memory (динамическая память)      │
│  MEMORY.md (индекс) + topic files               │
│  ~/.claude/projects/{slug}/memory/               │
└─────────────────────────────────────────────────┘
                      ↓
┌─────────────────────────────────────────────────┐
│  СЛОЙ 3: Session transcript (текущая сессия)    │
│  JSONL в ~/.claude/projects/{cwd}/{sessionId}/   │
│  Автоматическое сжатие при приближении к лимиту │
└─────────────────────────────────────────────────┘
```

### Структура директории памяти

```
~/.claude/projects/{slug}/memory/
├── MEMORY.md                    # Индекс (макс 200 строк / 25 KB)
├── user_preferences.md          # Тип: user
├── project_context.md           # Тип: project
├── code_review_feedback.md      # Тип: feedback
├── api_references.md            # Тип: reference
└── logs/                        # Дневные логи (KAIROS mode)
    └── 2026/04/
        └── 2026-04-11.md
```

### Формат файла памяти

```markdown
---
name: "Название"
description: "Однострочное описание — используется для определения релевантности"
type: "user | feedback | project | reference"
---

Содержимое памяти.

**Why:** Причина сохранения
**How to apply:** Когда и где применять
```

### Четыре типа памяти

| Тип | Scope | Что сохранять | Когда |
|-----|-------|---------------|-------|
| **user** | Всегда private | Роль, цели, знания, предпочтения | Узнал о пользователе |
| **feedback** | Default private | Что делать/не делать (коррекции) | Пользователь поправил подход |
| **project** | Bias к team | Работа, цели, баги, дедлайны | Узнал кто/что/зачем/когда |
| **reference** | Обычно team | Указатели на внешние системы | Узнал про Linear, Slack, Grafana |

### Что НЕ сохранять

- Паттерны кода, архитектуру, структуру файлов
- Git-историю, кто что менял
- Рецепты дебаггинга
- То что уже в CLAUDE.md
- Эфемерный контекст текущей задачи

### Чтение памяти (READ path)

```
1. MEMORY.md → ВСЕГДА в system prompt (целиком, до 200 строк / 25 KB)
    ↓
2. Запрос пользователя
    ↓
3. scanMemoryFiles() — читает первые 30 строк каждого .md файла
   Извлекает: filename, description, type, mtime
    ↓
4. formatMemoryManifest() — одна строка на файл:
   "- [user] preferences.md (2026-04-11T14:30:00.000Z): Предпочитает tabs"
    ↓
5. selectRelevantMemories() — sideQuery к Sonnet:
   System: "Select up to 5 memories clearly useful for the query"
   User: "Query: {запрос}\n\nAvailable memories:\n{манифест}"
   Output: JSON { selected_memories: ["file1.md", "file2.md"] }
    ↓
6. Выбранные файлы (до 5) загружаются ЦЕЛИКОМ как context blocks
   Макс: 4 KB на файл, 60 KB на сессию
```

**Ключевой нюанс:** 30 строк читаются ТОЛЬКО для отбора (frontmatter). После отбора файл грузится целиком. Качество description в frontmatter критично — плохое описание = файл не будет выбран.

### Запись памяти — два механизма

#### Механизм 1: Ручной (tool call)

```
Пользователь: "Запомни что я предпочитаю tabs"
    ↓
Claude вызывает FileWrite/FileEdit → пишет .md файл
    ↓
Видно в чате, пользователь может отклонить
```

#### Механизм 2: Автоматический (extractMemories)

```
Ход Claude завершился (нет tool calls)
    ↓
handleStopHooks() [stopHooks.ts]
    ↓
Проверка гейтов (cheapest first):
  1. Feature gate: tengu_passport_quail
  2. autoMemoryEnabled в настройках
  3. Не remote mode
  4. Не subagent
  5. EXTRACT_MEMORIES feature
  6. Не --bare mode
    ↓
MUTUAL EXCLUSION CHECK:
  hasMemoryWritesSince()
  → Если main agent УЖЕ писал в память этот ход → ПРОПУСК
    ↓
Форкнутый агент (fire-and-forget):
  - Наследует весь контекст (perfect fork, shared cache)
  - Получает манифест файлов памяти (pre-injected)
  - Анализирует последние ~N сообщений
  - Макс 5 ходов
  - Стратегия: Turn 1 → все Read параллельно, Turn 2 → все Write параллельно
  - Пишет ТОЛЬКО в memory/, read-only для всего остального
  - Скрыт от транскрипта (skipTranscript: true)
```

**Промпт извлечения:**
```
You are now acting as the memory extraction subagent.
Analyze the most recent ~N messages above and use them to update
your persistent memory systems.

You MUST only use content from the last ~N messages.
Do not investigate further — no grepping source files,
no reading code to confirm a pattern exists, no git commands.
```

#### Механизм 3: autoDream (консолидация)

```
Конец хода → handleStopHooks()
    ↓
Гейты:
  1. Не KAIROS mode
  2. Не remote mode
  3. Auto memory enabled
  4. Auto dream enabled
  5. Time gate: >= 24ч с последней консолидации
  6. Scan throttle: >= 10 мин с последнего сканирования
  7. Session gate: >= 5 сессий с последней консолидации
  8. Lock file: нет другого процесса консолидации
    ↓
Форкнутый агент, 4 фазы:
  Phase 1 — Orient: ls memory/, read MEMORY.md
  Phase 2 — Gather: читать daily logs, grep transcripts
  Phase 3 — Consolidate: merge, update, delete stale
  Phase 4 — Prune: MEMORY.md < 200 строк / 25 KB
```

### Сравнение механизмов

| | Ручной | Авто (extract) | autoDream |
|---|---|---|---|
| **Триггер** | Пользователь просит | Конец каждого хода | Конец хода + time/session gates |
| **Кто решает** | Пользователь | Форкнутый агент | Форкнутый агент |
| **Видно в чате** | Да | Нет | Нет |
| **Mutual exclusion** | — | Да (с ручным) | — |
| **Макс ходов** | Нет | 5 | Нет |
| **Права** | Полные | Только memory/ | Только memory/ |
| **Частота** | По запросу | Каждый ход | ~1 раз в сутки |

---

## 10. System Prompt — как собирается

### Pipeline сборки

```
buildEffectiveSystemPrompt()
    ↓
Приоритеты (first match wins):
  0. overrideSystemPrompt (loop mode) → ЗАМЕНЯЕТ всё
  1. coordinatorSystemPrompt (COORDINATOR_MODE) → ЗАМЕНЯЕТ
  2. agentSystemPrompt (agent definition) → ЗАМЕНЯЕТ или ДОПОЛНЯЕТ
  3. customSystemPrompt (--system-prompt flag) → ЗАМЕНЯЕТ
  4. defaultSystemPrompt → стандартный промпт
    ↓
appendSystemPrompt → всегда добавляется в конец
```

### Структура стандартного промпта

**Статическая часть (кэшируется глобально):**
1. `getSimpleIntroSection()` — идентичность + стиль вывода
2. `getSimpleSystemSection()` — инструменты, permissions, auto-compression
3. `getSimpleDoingTasksSection()` — редактирование кода, тестирование
4. `getActionsSection()` — действия vs. побочные эффекты
5. `getUsingYourToolsSection()` — приоритет dedicated tools > bash
6. `getSimpleToneAndStyleSection()` — стиль коммуникации
7. `getOutputEfficiencySection()` — краткость

**--- DYNAMIC BOUNDARY ---** (разделяет кэшируемое от динамического)

**Динамическая часть (пересчитывается по необходимости):**
1. `session_guidance` — напоминания о skills, agent tool
2. `memory` — автоматическая память (loadMemoryPrompt())
3. `env_info_simple` — CWD, git status, модель, knowledge cutoff
4. `language` — язык (если задан)
5. `output_style` — стиль вывода (если задан)
6. `mcp_instructions` — инструкции MCP серверов
7. `scratchpad` — доступ к scratchpad (если включен)
8. `frc` — function result clearing (для определённых моделей)
9. `summarize_tool_results` — «будь краток при суммаризации»
10. `token_budget` — бюджет токенов (если TOKEN_BUDGET feature)
11. `brief` — brief tool (если KAIROS mode)

### Кэширование секций

```typescript
// Кэшируемая секция (переиспользуется между ходами)
systemPromptSection('section_name', () => computeContent())

// Волатильная секция (пересчитывается каждый ход, ломает кэш)
DANGEROUS_uncachedSystemPromptSection('section_name', () => content, 'reason')
```

Кэш сбрасывается при `/clear` и `/compact`.

---

## 11. CLAUDE.md — иерархия и загрузка

### Порядок загрузки (от низшего к высшему приоритету)

```
1. MANAGED (~/.claude/.../CLAUDE.md или /etc/claude-code/CLAUDE.md)
   └─ Всегда загружается (policy settings)
   └─ .claude/rules/*.md (unconditional + conditional)

2. USER (~/.claude/CLAUDE.md)
   └─ Если userSettings source включен
   └─ .claude/rules/*.md

3. PROJECT (вверх от CWD до корня)
   └─ Если projectSettings source включен
   └─ CLAUDE.md, .claude/CLAUDE.md, .claude/rules/*.md
   └─ Ближе к CWD = выше приоритет

4. LOCAL (CLAUDE.local.md в каждой директории вверх)
   └─ Если localSettings source включен
   └─ Private, gitignored

5. AUTO MEMORY ENTRYPOINT (MEMORY.md)
   └─ Если auto memory feature включен
   └─ Усечён до 200 строк / 25 KB

6. TEAM MEMORY ENTRYPOINT (team/MEMORY.md)
   └─ Если TEAMMEM feature + team memory включены
```

### @include директива

Файлы памяти могут включать другие файлы:
```markdown
@path/to/file.md
@./relative/path.md
@~/home/path.md
@/absolute/path.md
```
- Рекурсивная обработка (макс. глубина: 5)
- Защита от циклов через `processedPaths` set
- Несуществующие файлы молча игнорируются

### Conditional Rules (условные правила)

Файлы в `.claude/rules/` с frontmatter `paths`:

```markdown
---
paths: ["src/**", "tests/**"]
---

# Это правило применяется только к файлам в src/ и tests/
```

**Как работает:**
1. При старте: unconditional rules загружаются всегда
2. При чтении/редактировании файла: conditional rules матчатся через glob
3. Только подходящие правила инжектятся в контекст

### Заголовок для CLAUDE.md контента

```
Codebase and user instructions are shown below. Be sure to adhere to these instructions.
IMPORTANT: These instructions OVERRIDE any default behavior and you MUST follow them exactly as written.
```

---

## 12. Prompts.ts — что это и зачем

### Что это

Каждый инструмент имеет свой `prompt.ts` — файл с текстовыми инструкциями для Claude о том, как использовать этот инструмент. Это НЕ system prompt целиком, а часть описания конкретного tool.

### Где находятся

```
src/tools/FileReadTool/prompt.ts    — как читать файлы
src/tools/FileWriteTool/prompt.ts   — как записывать файлы
src/tools/FileEditTool/prompt.ts    — как редактировать файлы
src/tools/BashTool/prompt.ts        — как выполнять команды
src/tools/GlobTool/prompt.ts        — как искать файлы
src/tools/GrepTool/prompt.ts        — как искать по содержимому
src/tools/WebFetchTool/prompt.ts    — как загружать веб-контент
src/services/extractMemories/prompts.ts  — промпт для автоизвлечения памяти
src/services/autoDream/consolidationPrompt.ts — промпт для консолидации
src/constants/prompts.ts            — ГЛАВНЫЙ файл, сборка system prompt
```

### Как промпты попадают в API

1. Каждый tool определяет свой prompt (usage instructions, limitations, examples)
2. Tool prompts передаются как `description` в tool definition для API
3. System prompt (из `constants/prompts.ts`) — отдельно, как `system` parameter
4. CLAUDE.md содержимое — инжектируется в system prompt
5. Memory content — инжектируется в system prompt или как context blocks

### Главный промпт (constants/prompts.ts)

~1400+ строк. Содержит:
- Идентичность Claude Code
- Правила использования tools (предпочитать dedicated over bash)
- Правила работы с кодом (read before edit, minimal changes)
- Правила коммуникации (краткость, без эмодзи)
- Правила безопасности (не пушить без разрешения, проверять перед деструктивными действиями)
- Правила git (conventional commits, новый коммит вместо amend)

---

## 13. Хранение сессий и истории

### Сессии (транскрипт разговора)

```
~/.claude/projects/{sanitized-cwd}/{sessionId}/
├── session.jsonl              # Транскрипт (JSONL)
├── tool-results/
│   ├── {toolUseId}.txt        # Большие результаты (порог зависит от tool)
│   └── {toolUseId}.json
└── ...
```

- Формат: JSONL (одна JSON-строка на сообщение)
- Типы: user, assistant, attachment, system (progress не сохраняется)
- Связь через parentUuid

### Персистенция результатов инструментов

Большие результаты автоматически сохраняются на диск:
```
Результат tool > порог → persist to disk → <persisted-output> XML tag в контексте
```
- Глобальный cap: `DEFAULT_MAX_RESULT_SIZE_CHARS = 50,000` символов
- Пороги per-tool: GrepTool = 20K, BashTool = 30K, остальные = 100K
- Эффективный порог: `Math.min(declared, 50_000)`
- Можно override через GrowthBook

### История команд

```
~/.claude/history.jsonl   # Глобальный файл, общий для всех проектов
```

```typescript
{
  display: string,           // Текст промпта пользователя
  pastedContents: {...},     // Вставленный контент (inline < 1KB или hash-reference)
  timestamp: number,
  project: string,
  sessionId?: string
}
```

- Lock file для concurrent-safe записи (10s stale timeout, 3 retries)
- Макс 100 записей на проект
- Дедупликация по display text (newest first)

---

## 14. Специальные директории

| Путь | Назначение |
|------|-----------|
| `~/.claude/` | Корень конфигурации (override: `CLAUDE_CONFIG_DIR`) |
| `~/.claude/settings.json` | Пользовательские настройки |
| `~/.claude/CLAUDE.md` | Глобальные инструкции |
| `~/.claude/rules/*.md` | Глобальные правила |
| `~/.claude/history.jsonl` | История команд |
| `~/.claude/projects/` | Корень сессий |
| `~/.claude/projects/{slug}/memory/` | Автоматическая память проекта |
| `~/.claude/projects/{slug}/memory/MEMORY.md` | Индекс памяти |
| `.claude/` | Конфигурация проекта (в корне репо) |
| `.claude/settings.json` | Настройки проекта (checked in) |
| `.claude/settings.local.json` | Локальные настройки (gitignored) |
| `.claude/commands/` | Пользовательские команды |
| `.claude/skills/` | Пользовательские skills |
| `.claude/rules/` | Правила проекта (conditional + unconditional) |
| `.claude/worktrees/` | Git worktrees |

### File State Cache

```typescript
LRUCache<string, FileState> {
  max: 100 entries,
  maxSize: 25 MB,
  sizeCalculation: Buffer.byteLength(content)
}
```

Хранит: content, timestamp, offset, limit, isPartialView.
`isPartialView: true` блокирует Edit/Write — нужен полный Read.

---

## 15. Лимиты и константы

### Чтение файлов

| Сущность | Лимит | Бросает ошибку? |
|----------|-------|-----------------|
| Text files | 256 KB (maxSizeBytes) | Да |
| Text files | 25,000 tokens (maxTokens) | Да |
| Text files | 2,000 строк (дефолт display) | Нет |
| Images | 5 MB base64 (API) | Да |
| Images | 3.75 MB raw (target) | Нет |
| Images | 2000×2000 px | Да (если сжатие не помогло) |
| PDF | 20 MB raw (target) | Нет |
| PDF | 3 MB (порог page extraction) | Нет |
| PDF | 100 MB (макс для extraction) | Да |
| PDF | 20 стр. per read | Да |
| PDF | 100 стр. API hard limit | Да |
| Notebooks | 256 KB serialized JSON | Да |

### Память

| Сущность | Лимит |
|----------|-------|
| MEMORY.md | 200 строк / 25 KB |
| Frontmatter scan | 30 строк per file |
| Max memory files scanned | 200 |
| Memory file per-turn | 4 KB |
| Memory session budget | 60 KB |
| Relevant memories per query | до 5 файлов |
| extractMemories max turns | 5 |
| autoDream min hours | 24 |
| autoDream min sessions | 5 |
| Session scan throttle | 10 мин |

### Запись файлов

| Сущность | Лимит |
|----------|-------|
| FileEditTool max file size | 1 GiB |
| FileStateCache max entries | 100 |
| FileStateCache max size | 25 MB |
| Tool result global cap | 50K chars (per-tool: Grep 20K, Bash 30K, default 100K) |
| Tool result max size | 50 KB |

### Поиск

| Сущность | Лимит |
|----------|-------|
| Glob max results | 100 файлов |
| Grep head_limit default | 250 строк |
| Grep max line length | 500 символов |

---

## 16. Сравнение с Open WebUI RAG

| Аспект | Claude Code | Open WebUI |
|--------|-------------|------------|
| **Эмбеддинги** | Нет | Да (bge-m3, 1024-dim) |
| **Vector DB** | Нет | Chroma |
| **Chunking** | Нет | RecursiveCharacterTextSplitter, 500 tokens, overlap 50 |
| **Поиск** | LLM читает заголовки → выбирает файлы | Cosine similarity по эмбеддингам |
| **Загрузка документа** | Целиком в контекст (base64 или page images) | Chunking → embedding → top-K retrieval |
| **Макс. документ** | ~20 MB / 100 стр. | Не ограничен (чанкуется) |
| **Стоимость поиска** | ~$0.01 (Sonnet sideQuery) | ~$0.001 (embedding API) |
| **Латентность** | ~1-2 сек (LLM call) | ~50 мс (vector search) |
| **Масштаб** | Десятки файлов | Тысячи документов |
| **Точность** | Высокая (LLM понимает контекст) | Средняя (cosine similarity) |
| **Гранулярность** | Файл целиком | Чанк (500 токенов) |

### Когда какой подход лучше

| Юзкейс | Лучший подход |
|---------|---------------|
| Персональная память (факты, предпочтения) | Claude (LLM-selection) |
| Анализ одного документа целиком | Claude (brute-force) |
| Поиск по базе из 100+ документов | RAG (Open WebUI) |
| FAQ-бот по документации | RAG (Open WebUI) |
| Суммаризация / перевод | Claude (brute-force) |
| Точечный поиск факта в большом корпусе | RAG (Open WebUI) |

---

*Документ создан на основе анализа исходного кода Claude Code (leaked npm sourcemap). Актуален на апрель 2026.*
