# Claude Code — Полная архитектура системы памяти

Анализ на основе утёкшего исходного кода Claude Code (claude-leaked/src/).

---

## Обзор: 5 механизмов памяти

| # | Механизм | Кто пишет | Когда | Куда | Видимость |
|---|----------|-----------|-------|------|-----------|
| 1 | **Inline memory** (основной агент) | Основная LLM | Во время ответа пользователю | `~/.claude/projects/<slug>/memory/*.md` | Видно как tool_use (Write/Edit) |
| 2 | **Extract memories** (forked agent) | Фоновый форк | После каждого завершённого ответа | Та же `memory/` директория | Невидимо, только уведомление "memory saved" |
| 3 | **Session memory** | Фоновый форк | По порогу токенов/tool calls | `~/.claude/session-memory/session.md` | Невидимо |
| 4 | **Auto Dream** (consolidation) | Фоновый форк | Раз в 24ч при ≥5 сессий | `memory/` директория | Фоновая задача |
| 5 | **Team memory sync** | HTTP sync | При pull/push | `memory/team/` поддиректория | Невидимо |

---

## 1. Inline Memory — основной агент

### Как работает

Основной агент получает секцию `# auto memory` в системном промпте (из `memdir/memdir.ts:buildMemoryLines()`). Эта секция содержит:

- Путь к директории: `~/.claude/projects/<sanitized-git-root>/memory/`
- 4 типа памяти: `user`, `feedback`, `project`, `reference`
- Правила когда сохранять / не сохранять
- Формат frontmatter для файлов
- Инструкции по ведению `MEMORY.md` индекса

**Никаких специальных memory-tool нет.** Модель использует стандартные `Write` и `Edit` для записи `.md` файлов в директорию памяти.

### Путь к директории (paths.ts)

Резолюция по приоритету:
1. `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` env var
2. `autoMemoryDirectory` в settings.json (только trusted sources: policy/flag/local/user; НЕ projectSettings — защита от вредоносных репо)
3. `<memoryBase>/projects/<sanitized-git-root>/memory/`
   - `memoryBase` = `CLAUDE_CODE_REMOTE_MEMORY_DIR` или `~/.claude`
   - Git worktrees одного репо делят одну директорию (через `findCanonicalGitRoot`)

### Формат файлов памяти

```markdown
---
name: {{memory name}}
description: {{one-line description — used to decide relevance}}
type: {{user, feedback, project, reference}}
---

{{memory content}}
```

### 4 ��ипа памяти (memoryTypes.ts)

**user** — роль, цели, знания пользователя. Пример: "deep Go expertise, new to React"

**feedback** — коррекции и подтверждения подхода. Структура: правило → **Why:** → **How to apply:**. Записывает и ошибки ("don't mock DB") и успехи ("bundled PR was right call").

**project** — контекст работы, не выводимый из кода. Дедлайны, решения, кто что делает. Относительные даты конвертируются в абсолютные.

**reference** — указатели на внешние системы (Linear, Grafana, Slack каналы).

### Что НЕ сохранять (WHAT_NOT_TO_SAVE_SECTION)

- Паттерны кода, архитектура, пути — выводимы из кодобазы
- Git history — есть git log/blame
- Решения дебага — фикс в коде, контекст в коммите
- Что уже в CLAUDE.md
- Эфемерный контекст текущей задачи
- Даже если пользователь просит сохранить PR list — спросить "что было неожиданного?"

### MEMORY.md индекс (memdir.ts)

- Лимит: 200 строк И 25KB
- Каждая запись — одна строка: `- [Title](file.md) — one-line hook`
- Truncation с предупреждением при превышении
- Загружается в system prompt каждый раз (через claudemd.ts)
- Модель обновляет его вручную при создании новых memory-файлов

### Загрузка в контекст

**Статически (system prompt):**
- `MEMORY.md` индекс — всегда (через `claudemd.ts:getMemoryFiles()`)

**Динамически (per-query recall):**
- `findRelevantMemories()` сканирует все `.md` файлы в `memory/`, читает frontmatter
- Вызывает **Sonnet** (sideQuery) для выбора до 5 релевантных файлов
- Промпт: "Query: {user_query}\n\nAvailable memories:\n{manifest}"
- Sonnet возвращает JSON: `{selected_memories: ["file1.md", "file2.md"]}`
- Отфильтровывает уже показанные файлы (`alreadySurfaced`)
- ��ыбранные файлы читаются и вставляются как system-reminder

### Staleness (memoryAge.ts)

Для памяти старше **>1 дня** (2+ дней) добавляется предупреждение (memories 0-1 дней давности — без warning):
> "This memory is N days old. Memories are point-in-time observations — claims about code may be outdated. Verify before asserting as fact."

### Секция "Before recommending from memory" (memoryTypes.ts)

- Если память называет файл — проверить что файл существует
- Если называет функцию — grep
- "The memory says X exists" ≠ "X exists now"

---

## 2. Extract Memories — фоновый forked agent

Файл: `services/extractMemories/extractMemories.ts`

### Триггер

Вызывается из `stopHooks.ts` (строка 149):
```typescript
void extractMemoriesModule!.executeExtractMemories(stopHookContext, ...)
```
- Fire-and-forget (void) — не блокирует ответ
- Только для main agent (не subagents): `!toolUseContext.agentId`
- Только если feature gate `tengu_passport_quail` включён
- Только если `isAutoMemoryEnabled()`
- Только если не `--bare` mode

### Throttling

- Счётчик `turnsSinceLastExtraction` — запускается каждые N ходов (по умолчанию 1)
- Overlap guard: `inProgress` flag — если уже работает, контекст stash-ится для trailing run
- Trailing run: после завершения текущего, запускает ещё один с последним stashed контекстом

### Mutual Exclusion с основным агентом

```typescript
if (hasMemoryWritesSince(messages, lastMemoryMessageUuid)) {
    // основной агент уже записал в memory/ — пропускаем
    return
}
```
Проверяет: есть ли в assistant-сообщениях tool_use блоки с `Write`/`Edit` на пути в `memory/`. Если да — курсор сдвигается, forked agent не запускается.

### Механика fork

Использует `runForkedAgent()` — создаёт **идентичную копию контекста** основного разговора. Делит prompt cache (одинаковый префикс = cache hit). Дешёвый по input-токенам.

### Промпт (prompts.ts:buildExtractAutoOnlyPrompt)

```
You are now acting as the memory extraction subagent.
Analyze the most recent ~N messages above and use them
to update your persistent memory systems.

Available tools: Read, Grep, Glob, read-only Bash, Edit/Write
for paths inside memory directory only.

You have a limited turn budget. Efficient strategy:
turn 1 — all Read calls in parallel;
turn 2 — all Write/Edit calls in parallel.

You MUST only use content from the last ~N messages.
Do not waste turns investigating or verifying.
```

Плюс:
- Existing memory files manifest (pre-injected чтобы не тратить ход на `ls`)
- Типы памяти (те же 4)
- Что не сохранять
- Инструкции по формату

### Разрешённые тулы (createAutoMemCanUseTool)

| Тул | Доступ |
|-----|--------|
| Read, Grep, Glob | Без ограничений |
| Bash | Только read-only (ls, find, grep, cat, stat, wc, head, tail) |
| Edit, Write | Только файлы в `memory/` директории |
| Agent, MCP, и др. | Запрещены |

### Бюджет: maxTurns = 5

### Результат

Записанные файлы извлекаются через `extractWrittenPaths()` — сканирует assistant-сообщения на Write/Edit tool_use. Пользователю показывается уведомление "memory saved" (через `createMemorySavedMessage`).

---

## 3. Session Memory

Файл: `services/SessionMemory/sessionMemory.ts`

### Отличие от auto memory

| | Auto Memory | Session Memory |
|---|---|---|
| **Цель** | Долгосрочная память между сессиями | Контекст текущей сессии |
| **Формат** | Отдельные topic-файлы с frontmatter | Один структурированный markdown |
| **Применение** | Загружается в system prompt | Используется при compaction |
| **Жизнь** | Persistent forever | До конца сессии |

### Структура файла (шаблон)

```markdown
# Session Title
# Current State
# Task specification
# Files and Functions
# Workflow
# Errors & Corrections
# Codebase and System Documentation
# Learnings
# Key results
# Worklog
```

Каждая секция: header → italic description (не трогать!) → контент. Лимит: 2000 токенов/секция, 12000 токенов всего.

### Триггеры

Регистрируется как `postSamplingHook`. Запускается когда:
1. Порог токенов контекста достигнут (инициализация: `minimumMessageTokensToInit`)
2. Между обновлениями: `minimumTokensBetweenUpdate` токенов И `toolCallsBetweenUpdates` tool calls
3. Или: порог токенов + нет tool calls в последнем assistant-ходе (естественная пауза)

### Механика

Тоже `runForkedAgent()`. Разрешён только `Edit` на конкретный файл session memory. Промпт:
- "Based on the user conversation above, update the session notes file"
- Текущее содержимое файла pre-injected
- "Use Edit tool in parallel and stop"

### Использование при compaction (sessionMemoryCompact.ts)

Когда контекстное окно переполняется:
1. Ждёт завершения текущей extraction (`waitForSessionMemoryExtraction`)
2. Находит `lastSummarizedMessageId` — граница "уже обобщённых" сообщений
3. Оставляет min 10K токенов / 5 text-block сообщений после границы
4. Старые сообщения заменяются на session memory content
5. Tool_use/tool_result пары не разрываются

---

## 4. Auto Dream (Consolidation)

Файл: `services/autoDream/autoDream.ts`

### Назначение

"Ночная" консолидация памяти. Forked agent ревьюит накопленные session transcripts и улучшает memory-файлы.

### Триггеры (gate order, cheapest first)

1. **Time gate**: ≥24 часов с последней консолидации (одна stat-проверка)
2. **Session gate**: ≥5 сессий с mtime > lastConsolidatedAt (после scan throttle 10 мин)
3. **Lock**: файловый замок (не overlap с другими процессами)

### Промпт (consolidationPrompt.ts)

4 фазы:
1. **Orient** — ls memory dir, read MEMORY.md, skim topic files
2. **Gather signal** — daily logs, drifted memories, grep transcripts (узко!)
3. **Consolidate** — write/update memory files, merge signal, convert relative→absolute dates
4. **Prune and index** — обновить MEMORY.md, удалить устаревшее, разрешить конфлик��ы

### Тулы: те же что у extractMemories (createAutoMemCanUseTool). Bash read-only.

---

## 5. Team Memory Sync

Файл: `services/teamMemorySync/index.ts`

### Концепция

Разделяемая память команды. Файлы в `memory/team/` синхронизируются с сервером Anthropic API.

### API

```
GET  /api/claude_code/team_memory?repo={owner/repo}             → данные + checksums
GET  /api/claude_code/team_memory?repo={owner/repo}&view=hashes → только checksums
PUT  /api/claude_code/team_memory?repo={owner/repo}             → upload (upsert)
```

### Семантика

- **Pull**: сервер побеждает (server wins per-key)
- **Push**: delta upload — только файлы с изменённым hash
- **Удаление НЕ распространяется**: удалил локально → pull восстановит

### Scope в типах памяти

В combined mode (team+personal) каждый тип имеет `<scope>`:
- `user` → always private
- `feedback` → default private, team only для project-wide conventions
- `project` �� bias toward team
- `reference` → usually team

---

## 6. Agent Memory (для sub-agents)

Файл: `tools/AgentTool/agentMemory.ts`

Отдельная память для кастомных агентов (не основного Claude). 3 scope:

| Scope | Путь | VCS |
|-------|------|-----|
| user | `~/.claude/agent-memory/<agentType>/` | Нет |
| project | `<cwd>/.claude/agent-memory/<agentType>/` | Да |
| local | `<cwd>/.claude/agent-memory-local/<agentType>/` | Нет |

Использует тот же `buildMemoryPrompt()` — те же 4 типа, тот же формат. Поддерживает snapshot sync (из `.claude/agent-memory-snapshots/`).

---

## 7. CLAUDE.md — статическая память

Файл: `utils/claudemd.ts`

### Иерархия загрузки (от низкого к высокому приоритету)

1. **Managed** — `/etc/claude-code/CLAUDE.md` (Linux); `/Library/Application Support/ClaudeCode/CLAUDE.md` (macOS); `C:\Program Files\ClaudeCode\CLAUDE.md` (Windows)
2. **User** — `~/.claude/CLAUDE.md` (приватные глобальные инструкции)
3. **Project** — `CLAUDE.md`, `.claude/CLAUDE.md`, `.claude/rules/*.md` (в git)
4. **Local** — `CLAUDE.local.md` (приватные project-specific)

### Discovery

- User memory: из home directory
- Project/Local: traverse от CWD до root; ближе к CWD = выше приоритет

### @include directive

Файлы могут включать другие: `@path`, `@./relative`, `@~/home`, `@/absolute`.
Только в text nodes (не в code blocks). Circular references предотвращаются. Max 40,000 символов на файл.

---

## Как всё работает вместе

### При старте сессии

1. Загружаются CLAUDE.md файлы (иерархия) → в system prompt
2. Загружается `MEMORY.md` индекс �� в system prompt как "auto memory"
3. Инъектируются инструкции по работе с памятью (buildMemoryLines)

### При каждом запросе пользов��теля

1. `findRelevantMemories()` вызывает Sonnet для выбора до 5 topic-файлов → инъектируются как system-reminder
2. Основной агент видит инструкции и может сам вызвать Write/Edit для memory/

### После каждого ответа (stopHooks)

1. **extractMemories** — fire-and-forget, если основной агент не писал в memory/
2. **autoDream** — проверка time/session gate, если пора — консолидация
3. **sessionMemory** — если порог достигнут, обновляет session notes

### При compaction (переполнение конт��кста)

1. Session memory подставляется как сводка
2. Старые сообщения удаляются, свежие сохраняются
3. CLAUDE.md перезагружается

---

## Ключевые детали реализации

### Prompt cache sharing

Forked agents (extract, session, dream) используют `runForkedAgent()` с `createCacheSafeParams()`. Они **делят prefix cache** с основным агентом — тот же system prompt, те же tools, те же начальные сообщения. Платят только за output.

### Scan и manifest

`scanMemoryFiles()` — рекурсивный readdir, читает первые 30 строк каждого .md для frontmatter, сортирует по mtime, лимит 200 файлов. Результат используется и для recall (findRelevantMemories) и для extraction agent (pre-inject manifest).

### Security

- `validateMemoryPath()` — отклоняет relative, root, UNC, null byte пути
- `isAutoMemPath()` — normalize + startsWith проверка
- projectSettings НЕ может задать `autoMemoryDirectory` (защита от вредоносных репо)
- Write carve-out в filesystem.ts для memory/ path — обходит DANGEROUS_DIRECTORIES check

### /remember skill

Интерактивный ревью: читает все слои памяти, классифицирует куда что перенести (CLAUDE.md vs auto-memory vs team), предлагает cleanup. Не применяет изменения без подтверждения.

---

## Глубокий анализ: Исходный код

> Секции ниже — детальный разбор конкретных модулей из `claude-leaked/src/`.

---

### A. Frontmatter Parser (`utils/frontmatterParser.ts`)

#### Формат и парсинг

Единый парсер YAML frontmatter для **всех** `.md` файлов в системе: memory-файлы, skills, commands, agents, output styles.

**Regex:** `FRONTMATTER_REGEX = /^---\s*\n([\s\S]*?)---\s*\n?/`

**Двухпроходный парсинг:**
1. Пытается напрямую через `parseYaml(frontmatterText)`
2. Если YAML-ошибка — вызывает `quoteProblematicValues()` (авто-экранирование спецсимволов) → повторная попытка
3. Если оба прохода провалились — логирует warn, возвращает пустой `{}`

#### YAML-спецсимволы

Regex для детекции проблемных значений:
```
/[{}[\]*&#!|>%@`]|: /
```
Включает: `{ } [ ] * & # ! | > % @ \`` и `: ` (двоеточие с пробелом).

Если значение не заквочено и содержит спецсимволы — оборачивается в двойные кавычки с экранированием `"` и `\`.

Это позволяет glob-паттерны типа `src/**/*.{ts,tsx}` корректно парситься без ручного квотинга.

#### Полная структура FrontmatterData

```typescript
type FrontmatterData = {
  // Общие поля
  description?: string | null
  type?: string | null           // 'user' | 'feedback' | 'project' | 'reference' (для memory)
  version?: string | null

  // Тулы и permissions
  'allowed-tools'?: string | string[] | null
  'hide-from-slash-command-tool'?: string | null

  // Модель и execution
  model?: string | null          // 'haiku', 'sonnet', 'opus', конкретные model names, 'inherit'
  effort?: string | null         // 'low' | 'medium' | 'high' | 'max' | integer
  context?: 'inline' | 'fork' | null  // inline = expand в текущий разговор, fork = sub-agent
  agent?: string | null          // Тип агента при context='fork'

  // Skill-specific
  'user-invocable'?: string | null   // 'true' = /skill-name доступен пользователю
  'argument-hint'?: string | null
  when_to_use?: string | null
  skills?: string | null         // Comma-separated список skills для preload
  hooks?: HooksSettings | null   // Хуки при вызове skill

  // Пути и shell
  paths?: string | string[] | null   // Glob-паттерны файлов
  shell?: 'bash' | 'powershell' | null  // Shell для !-блоков в .md

  [key: string]: unknown         // Произвольные поля
}
```

#### Brace expansion для путей

`splitPathInFrontmatter()` парсит пути с glob-синтаксисом:
```
"src/*.{ts,tsx}"       → ["src/*.ts", "src/*.tsx"]
"{a,b}/{c,d}"          → ["a/c", "a/d", "b/c", "b/d"]
"a, src/*.{ts,tsx}"    → ["a", "src/*.ts", "src/*.tsx"]
```
Запятые внутри `{}` не являются разделителями. Рекурсивный expand для вложенных brace-групп.

#### Вспомогательные парсеры

| Функция | Логика |
|---------|--------|
| `parsePositiveIntFromFrontmatter(value)` | Только integers > 0; число или строка → `parseInt` |
| `coerceDescriptionToString(value)` | string/number/boolean → string; array/object → `null` + warn |
| `parseBooleanFrontmatter(value)` | Строго: только `true` или `"true"` |
| `parseShellFrontmatter(value)` | Enum `['bash', 'powershell']`; невалидное → `undefined` + warn, fallback bash |

#### Кто использует (14 файлов)

- `memdir/memoryScan.ts` — сканирование memory-файлов (description + type)
- `skills/loadSkillsDir.ts` — загрузка skills (paths, shell, user-invocable, hooks)
- `tools/AgentTool/loadAgentsDir.ts` — загрузка agent-определений (effort)
- `tools/SkillTool/SkillTool.ts` — исполнение skills (shell, context)
- `utils/markdownConfigLoader.ts` — generic loader markdown-конфигов
- Плагины: `loadPluginCommands.ts`, `loadPluginAgents.ts`, `loadPluginOutputStyles.ts`

---

### B. System Prompt Assembly (`context.ts` + `constants/prompts.ts`)

#### Три режима сборки

| Режим | Условие | Содержимое |
|-------|---------|------------|
| **Simple** | `CLAUDE_CODE_SIMPLE=true` | Минимум: CWD + дата |
| **Proactive** | Feature `PROACTIVE`/`KAIROS` + `isProactiveActive()` | Автономный агент с tick-циклами |
| **Standard** | По умолчанию | Полная сборка (описана ниже) |

#### Контексты (`context.ts`)

**System Context** (`getSystemContext()`) — мемоизирован, один раз за разговор:
- Git status: ветка, main branch, пользователь, `git status --short` (max 2000 chars), последние коммиты (`git log --oneline -n 5`, без лимита символов)
- Пропускается в CCR (`CLAUDE_CODE_REMOTE`) или когда git-инструкции отключены

**User Context** (`getUserContext()`) — мемоизирован, один раз за разговор:
- Все CLAUDE.md файлы (auto-discovery от CWD вверх)
- Текущая дата ISO
- Фильтрация через `filterInjectedMemoryFiles()` (AutoMem/TeamMem → attachment при feature flag)

#### Кеширование секций (`systemPromptSections.ts`)

Два типа:

| Тип | Поведение | Применение |
|-----|-----------|------------|
| `systemPromptSection(name, compute)` | Мемоизирован, кеш до `/clear` или `/compact` | Большинство секций |
| `DANGEROUS_uncachedSystemPromptSection(name, compute, reason)` | Пересчитывается каждый ход | Только volatile (MCP-подключения) |

Разрешение: `resolveSystemPromptSections()` — все секции параллельно через `Promise.all()`.

#### Полный порядок сборки (Standard mode)

**Статические секции (кешируемые глобально):**

1. **Intro** — роль ("interactive agent"), cyber risk instruction, output style note
2. **System** — отображение текста, permission mode, hooks, авто-сжатие
3. **Doing Tasks** — стиль кода (минимальные изменения, нет overengineering), комментарии (ant-only)
4. **Actions** — reversibility/blast radius assessment, risky actions, эскалация
5. **Using Tools** — предпочтение dedicated tools над Bash; условно для REPL mode
6. **Tone & Style** — emoji policy, conciseness, file references, формат PR/issue
7. **Output Efficiency** — ant: подробные правила коммуникации; 3P: тerse/direct

**Граница кеша:**
```
SYSTEM_PROMPT_DYNAMIC_BOUNDARY = '__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__'
```
Всё ДО — глобальный scope cache. Всё ПОСЛЕ — per-user/per-session.

**Динамические секции (мемоизированные per-session):**

| # | Секция | Кеш | Условие |
|---|--------|-----|---------|
| 8 | `session_guidance` | Cached | Всегда |
| 9 | `memory` (auto memory + CLAUDE.md) | Cached | Всегда |
| 10 | `ant_model_override` | Cached | `USER_TYPE === 'ant'` |
| 11 | `env_info_simple` | Cached | Всегда |
| 12 | `language` | Cached | Если установлен язык |
| 13 | `output_style` | Cached | Если есть output style config |
| 14 | `mcp_instructions` | **UNCACHED** | Если есть MCP-серверы |
| 15 | `scratchpad` | Cached | Если scratchpad включён |
| 16 | `frc` (function result clearing) | Cached | Если `CACHED_MICROCOMPACT` feature |
| 17 | `summarize_tool_results` | Cached | Всегда |
| 18 | `numeric_length_anchors` | Cached | Ant-only |
| 19 | `token_budget` | Cached | Если `TOKEN_BUDGET` feature |
| 20 | `brief` | Cached | Если `KAIROS`/`KAIROS_BRIEF` |

#### Инъекция памяти в system prompt

Memory попадает двумя путями:

1. **CLAUDE.md файлы** → через `getUserContext()` → `filterInjectedMemoryFiles()` → `getClaudeMds()` → в bootstrap state → label "claudeMd" в system-reminder
2. **Auto memory prompt** → через `loadMemoryPrompt()` (memdir) → мемоизированная секция #9

При feature flag `tengu_moth_copse` AutoMem/TeamMem вырезаются из system prompt и загружаются как attachments (для экономии prompt cache).

---

### C. Compaction — полная логика (`services/compact/`)

#### Три механизма compaction + microcompact

| Механизм | Триггер | Стратегия | API-вызов? |
|----------|---------|-----------|------------|
| **Session Memory Compact** | Auto-порог + feature flags | Использует session memory как summary, сохраняет недавние сообщения | Нет |
| **Legacy Compact** | Auto-порог / ручной `/compact` / API 413 | 9-секционная суммаризация через LLM | Да |
| **Reactive Compact** | API `prompt_too_long` (413) | Те же Legacy, с PTL retry | Да |
| **Microcompact** | Time gap >60 мин / cached MC | Очистка содержимого tool_result (не удаление сообщений) | Нет |

#### Auto-compact порог

```typescript
threshold = effectiveContextWindow - AUTOCOMPACT_BUFFER_TOKENS (13,000)
effectiveContextWindow = contextWindowSize - min(maxOutputTokens, 20,000)
// Пример: 200K - min(16K, 20K) = 184K; порог = 184K - 13K = 171K токенов
```

**Отключается при:**
- `DISABLE_COMPACT` / `DISABLE_AUTO_COMPACT` env vars
- `autoCompactEnabled: false` в настройках
- Режим `REACTIVE_COMPACT` (ant-only, ждёт 413 от API)
- Режим context collapse
- Query source = `session_memory`, `compact`, `marble_origami`

#### Session Memory Compact (sessionMemoryCompact.ts)

Самый продвинутый алгоритм — **не вызывает API** для суммаризации:

```typescript
calculateMessagesToKeepIndex(messages, lastSummarizedIndex):
  1. Начать от lastSummarizedIndex (последнее обобщённое сообщение)
  2. Расширять назад для выполнения минимумов:
     - minTokens: 10,000
     - minTextBlockMessages: 5 (реальных user/assistant с текстом)
  3. Стоп при maxTokens: 40,000 (жёсткий лимит)
  4. adjustIndexToPreserveAPIInvariants(): не разрывать tool_use/tool_result пары
```

Конфиг (`tengu_sm_compact_config`) — удалённо управляется через GrowthBook.

**Fallback на Legacy если:**
- Session memory extraction не завершена
- `lastSummarizedMessageId` не найден в сообщениях
- Post-compact tokens превышают autocompact threshold

#### Legacy Compact (compact.ts)

**9-секционная суммаризация через LLM:**
1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections (со сниппетами!)
4. Errors and fixes
5. Problem Solving
6. All user messages (non-tool-use; "List ALL user messages that are not tool results")
7. Pending Tasks
8. Current Work
9. Optional Next Step

Модель генерирует `<analysis>` (scratchpad, отбрасывается) + `<summary>` (вставляется).

**При `prompt_too_long` на сам compact запрос:**
`truncateHeadForPTLRetry()` — удаляет самые старые API-round группы, повтор до 3 раз.

#### Microcompact (microCompact.ts)

**НЕ удаляет сообщения** — только заменяет содержимое tool_result:

```
tool_result content → '[Old tool result content cleared]'
```

**Compactable tools:** FILE_READ, BASH, POWERSHELL, GREP, GLOB, WEB_SEARCH, WEB_FETCH, FILE_EDIT, FILE_WRITE

**Два режима:**
- **Time-based:** gap > 60 мин с последнего assistant-сообщения → очистить все кроме последних N результатов
- **Cached MC:** через `cache_edits` API-блоки (не мутирует сообщения локально, API применяет)

#### Полный flow auto-compact

```
1. shouldAutoCompact() → tokenCount >= threshold?

2. Попытка Session Memory Compact:
   a) Проверка feature flags
   b) waitForSessionMemoryExtraction()
   c) Загрузить session memory content
   d) calculateMessagesToKeepIndex()
   e) Если OK → return CompactionResult (без API-вызова!)
   f) Если fail → fallback на Legacy

3. Legacy Compact:
   a) PRE-COMPACT HOOKS
   b) Стрим суммаризации (с PTL retry до 3 раз)
   c) Очистка кешей: readFileState, loadedNestedMemoryPaths
   d) POST-COMPACT ATTACHMENTS (параллельно):
      - File attachments (50K token budget, 5K/file)
      - Skill attachments (25K budget, 5K/skill)
      - Plan/PlanMode attachments
      - Async agent attachments
      - Re-announce tools/agents/MCP deltas
   e) SESSION START HOOKS
   f) Boundary marker + summary message
   g) Телеметрия + cache notifications
   h) POST-COMPACT HOOKS + cleanup

4. buildPostCompactMessages():
   → [boundary, summary, keptMessages, attachments, hookResults]
```

#### Ключевые константы

| Константа | Значение | Назначение |
|-----------|----------|------------|
| `AUTOCOMPACT_BUFFER_TOKENS` | 13,000 | Резерв до триггера |
| `WARNING_THRESHOLD_BUFFER_TOKENS` | 20,000 | UI предупреждение |
| `COMPACT_MAX_OUTPUT_TOKENS` | 20,000 | Max output для суммаризации |
| `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES` | 3 | Circuit breaker |
| `POST_COMPACT_TOKEN_BUDGET` | 50,000 | Бюджет file-attachments |
| `POST_COMPACT_MAX_TOKENS_PER_FILE` | 5,000 | Лимит на файл |
| `POST_COMPACT_SKILLS_TOKEN_BUDGET` | 25,000 | Бюджет skill-attachments |
| `POST_COMPACT_MAX_TOKENS_PER_SKILL` | 5,000 | Лимит на skill |
| SM `minTokens` | 10,000 | Минимум сохраняемых токенов |
| SM `minTextBlockMessages` | 5 | Минимум текстовых сообщений |
| SM `maxTokens` | 40,000 | Максимум сохраняемых токенов |

#### Оценка токенов

```typescript
// Грубая: ~4 символа = 1 токен
roughTokenCountEstimation(text): text.length / 4

// Точная: берёт из API response, fallback на грубую
tokenCountWithEstimation(messages): apiTokens ?? estimation * 4/3  // +33% buffer

// estimateMessageTokens: text, tool_results, images, thinking → pad ×4/3
```

---

### D. Forked Agent — полная механика (`utils/forkedAgent.ts`)

690 строк. Ядро фоновых процессов: extractMemories, sessionMemory, autoDream, promptSuggestion, speculation.

#### CacheSafeParams — 5 компонентов prompt cache key

```typescript
type CacheSafeParams = {
  systemPrompt: SystemPrompt         // System prompt (идентичный родителю)
  userContext: { [k: string]: string } // Prepended к сообщениям
  systemContext: { [k: string]: string } // Appended к system prompt
  toolUseContext: ToolUseContext      // Tools, model, options
  forkContextMessages: Message[]     // Полная история родителя
}
```

**Cache key API состоит из:** system prompt + tools + model + messages prefix + thinking config.
Если все 5 параметров совпадают → **100% cache hit** на input-токены.

#### Global slot pattern

```typescript
// После каждого хода main thread:
saveCacheSafeParams(createCacheSafeParams(context))

// Фоновые форки подхватывают:
const params = getLastCacheSafeParams()
```

Сохраняются только для `querySource === 'repl_main_thread'` или `'sdk'`. Форки не перезаписывают слот.

#### createSubagentContext() — изоляция

| Свойство | Дефолт для форка | Почему |
|----------|------------------|--------|
| `readFileState` | **Клонирован** | Мутации не аффектят родителя |
| `abortController` | **Новый child** | Abort родителя → abort child, но не наоборот |
| `getAppState` | Wrapped: `shouldAvoidPermissionPrompts: true` | Форк не должен промптить пользователя |
| `setAppState` | **no-op** | Изоляция состояния |
| `contentReplacementState` | **Клонирован** (не fresh!) | Иначе форк увидит tool_use_ids родителя как "новые" → другие решения замены → другой prefix → cache miss |
| `toolDecisions` | **Fresh** per-subagent | Каждый форк трекит свои отказы |

**Opt-in sharing:**
- `shareSetAppState` — для интерактивных sub-agents
- `shareAbortController` — для agents, которые пользователь может отменить
- `shareSetResponseLength` — для метрик

#### Жизненный цикл `runForkedAgent()`

```typescript
async function runForkedAgent({
  promptMessages,     // Новые сообщения форка (промпт)
  cacheSafeParams,    // 5 cache-safe компонентов
  canUseTool,         // Функция проверки прав на тулы
  querySource,        // Идентификатор ('session_memory', 'extract_memories', etc)
  forkLabel,          // Для аналитики
  maxTurns?,          // Лимит API roundtrips
  maxOutputTokens?,   // ОСТОРОЖНО: меняет budget_tokens → сбивает cache!
  onMessage?,         // Callback на каждое сообщение
  skipTranscript?,    // Не записывать в sidechain
  skipCacheWrite?,    // Не создавать новые cache entries
}): Promise<ForkedAgentResult>
```

**Шаги:**
1. `createSubagentContext()` — изоляция
2. Объединить `forkContextMessages + promptMessages`
3. Записать в sidechain transcript (если не `skipTranscript`)
4. Query loop: вызов `query()` generator с идентичными cache-safe params
5. Аккумуляция usage из `message_delta` stream events
6. Cleanup: очистка клонированного readFileState
7. Логирование: `tengu_fork_agent_query` с cache hit rate

#### Cache hit rate

```typescript
cacheHitRate = cache_read_input_tokens / (input_tokens + cache_creation + cache_read)
```

#### Важные нюансы

**Не фильтрует incomplete tool calls:** В отличие от main loop, `filterIncompleteToolCalls()` не вызывается. Dangling tool_uses починятся downstream в `ensureToolResultPairing` (claude.ts). Одинаковое поведение = одинаковый prefix = cache hit.

**`maxOutputTokens` ломает cache:** На старых моделях (без adaptive thinking) установка maxOutputTokens меняет `budget_tokens` — часть cache key. Использовать только когда cache sharing НЕ нужен.

#### Реальные примеры использования

**Prompt Suggestion** — запрещает все тулы через `canUseTool: () => deny`, НЕ через `tools: []` (пустой tools-список сломает cache key):
```typescript
canUseTool: async () => ({ behavior: 'deny', message: '...' })
```

**Session Memory** — разрешает только `Edit` на конкретный файл:
```typescript
canUseTool: createMemoryFileCanUseTool(memoryPath)
```

**Speculation** — copy-on-write изоляция: перенаправляет file writes в overlay директорию через canUseTool.

---

### E. Команда `/memory` и детекция memory-файлов

#### Команда `/memory` (`commands/memory/`)

**Регистрация:**
```typescript
const memory: Command = {
  type: 'local-jsx',
  name: 'memory',
  description: 'Edit Claude memory files',
}
```

**Поведение:** Открывает интерактивный React-диалог (Ink framework) с `MemoryFileSelector`.

**MemoryFileSelector** (437 строк) показывает:
- User memory: `~/.claude/CLAUDE.md`
- Project memory: `./CLAUDE.md`
- Вложенные/импортированные файлы (помечены "(new)" если не существуют)
- Папки: "Open auto-memory folder", "Open team memory folder", agent memory folders
- Статусы: auto-memory on/off, Auto-Dream status, last consolidation time

**При выборе файла:**
1. Создаёт файл если не существует (`writeFile` с `wx` flag — fail if exists)
2. Открывает в редакторе через `editFileInEditor()` (уважает `$VISUAL` / `$EDITOR`)
3. `clearMemoryFileCaches()` при открытии

#### MemoryUpdateNotification

Показывает: `"Memory updated in {path} · /memory to edit"`

Путь форматируется: предпочитает `~/` нотацию, fallback на `./` relative to cwd, показывает кратчайший вариант.

#### Memory File Detection (`utils/memoryFileDetection.ts`, 290 строк)

| Функция | Возвращает | Назначение |
|---------|------------|------------|
| `detectSessionFileType(path)` | `'session_memory'` \| `'session_transcript'` \| `null` | `.md` в `~/.claude/session-memory/` или `.jsonl` в `~/.claude/projects/` |
| `isAutoManagedMemoryFile(path)` | `boolean` | `true` для auto-memory, agent memory, session memory/transcripts; `false` для user-managed (CLAUDE.md, .claude/rules/) |
| `isMemoryDirectory(dirPath)` | `boolean` | Проверяет agent-memory, team memory, auto-memory, session-memory, project transcripts |
| `isShellCommandTargetingMemory(cmd)` | `boolean` | Извлекает absolute path tokens из shell-команды, проверяет через `isMemoryDirectory()`. Для collapse/badge в UI |
| `memoryScopeForPath(path)` | `'team'` \| `'personal'` \| `null` | team для `memory/team/`, personal для auto-memory |

#### Memory Types в claudemd.ts

```typescript
const MEMORY_TYPE_VALUES = [
  'User',      // ~/.claude/CLAUDE.md
  'Project',   // ./CLAUDE.md или ./.claude/CLAUDE.md
  'Local',     // ./CLAUDE.local.md
  'Managed',   // /etc/claude-code/CLAUDE.md
  'AutoMem',   // Auto-memory (memdir)
  'TeamMem',   // Team memory
]
```

#### @include директива — полная механика

**Синтаксис:**
```
@path                    → relative (= @./path)
@./relative/path         → от директории включающего файла
@~/home/path             → tilde expansion
@/absolute/path          → абсолютный
@path#heading            → fragment stripped (не используется)
@path\ with\ spaces     → escaped пробелы
```

**Ограничения:**
- Max depth: 5 уровней вложенности
- Только в leaf text nodes (не в code blocks)
- Circular references предотвращаются через `processedPaths` set
- Несуществующие файлы молча игнорируются
- 100+ поддерживаемых расширений (TEXT_FILE_EXTENSIONS): `.md`, `.txt`, `.ts`, `.py`, `.json`, `.yaml`, ...
- Бинарные файлы блокируются

**Regex:** `/(?:^|\s)@((?:[^\s\\]|\\ )+)/g`

#### Ключевые константы claudemd.ts

| Константа | Значение | Назначение |
|-----------|----------|------------|
| `MAX_MEMORY_CHARACTER_COUNT` | 40,000 | Рекомендованный макс на файл |
| `MAX_INCLUDE_DEPTH` | 5 | Вложенность @include |
| Max file size (raw) | 40,000 символов (`MAX_MEMORY_CHARACTER_COUNT`) | Лимит на один CLAUDE.md |

---

### F. Write Carve-Out для Memory (`FileWriteTool` + `filesystem.ts`)

#### DANGEROUS_DIRECTORIES

```typescript
const DANGEROUS_DIRECTORIES = ['.git', '.vscode', '.idea', '.claude'] as const
```

Запись в эти директории блокируется по умолчанию. `.claude` в списке потому что содержит конфигурацию, исполняемые файлы, настройки — потенциальный вектор атаки.

#### Двухфазная проверка permissions

**Фаза 1: Pre-safety carve-outs** (`filesystem.ts:1500-1602`)
- Проверка до DANGEROUS_DIRECTORIES
- Agent memory paths → `allow`
- **Memdir carve-out** (строки 1565-1581):

```typescript
// Carve-out exists because default path is under ~/.claude/,
// which is in DANGEROUS_DIRECTORIES.
// CLAUDE_COWORK_MEMORY_PATH_OVERRIDE gets NO special treatment —
// writes go through normal permission flow.
if (!hasAutoMemPathOverride() && isAutoMemPath(normalizedPath)) {
  return {
    behavior: 'allow',
    decisionReason: {
      type: 'other',
      reason: 'auto memory files are allowed for writing',
    },
  }
}
```

**Фаза 2: DANGEROUS_DIRECTORIES check** (`filesystem.ts:440-488`)
- Только если carve-out не сработал
- Итерирует сегменты пути, ищет `.git`, `.vscode`, `.idea`, `.claude`
- Спецкейс: `.claude/worktrees/` пропускается

#### Ключевая логика: carve-out условен

| Сценарий | Поведение |
|----------|-----------|
| Write в `~/.claude/projects/{cwd}/memory/MEMORY.md` (дефолтный path) | **ALLOWED** через carve-out |
| Write в `~/.claude/settings.json` | **BLOCKED** — DANGEROUS_DIRECTORIES |
| Write через `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE=/data/mem` | **REQUIRES PERMISSION** — carve-out отключён |
| Write в `~/.claude/skills/...` | **Narrower scope** (getClaudeSkillScope) |
| Write в `~/.claude/worktrees/...` | **SKIPPED** в DANGEROUS check |

#### validateMemoryPath() — полные проверки безопасности

```typescript
function validateMemoryPath(raw, expandTilde):
  1. Reject relative paths (!isAbsolute)
  2. Reject root/near-root (length < 3)
  3. Reject Windows drive-root (/^[A-Za-z]:$/)
  4. Reject UNC paths (\\server\share или //server/share)
  5. Reject null bytes (\0)
  6. Reject bare tilde (~, ~/, ~/., ~/..) — expansion к $HOME слишком опасна
  7. Tilde expansion ТОЛЬКО в settings.json, НЕ в env var overrides
  8. Normalize + trailing sep + NFC normalization
```

#### isAutoMemPath()

```typescript
function isAutoMemPath(absolutePath: string): boolean {
  const normalizedPath = normalize(absolutePath) // SECURITY: prevent path traversal
  return normalizedPath.startsWith(getAutoMemPath())
}
```

Простая prefix-проверка после нормализации. Предотвращает `../` bypass.

#### Резолюция memory path (getAutoMemPath)

```typescript
1. CLAUDE_COWORK_MEMORY_PATH_OVERRIDE env var (no tilde, always absolute)
2. autoMemoryDirectory в settings.json (policy/flag/local/user; НЕ project!)
3. Default: {memoryBase}/projects/{sanitize(gitRoot)}/memory/
   - memoryBase = CLAUDE_CODE_REMOTE_MEMORY_DIR || ~/.claude
```

Мемоизирован с ключом `getProjectRoot()` — один path на project.

---

### G. Сводка: что нового относительно исходного документа

| Тема | Что добавлено |
|------|---------------|
| **Frontmatter** | Полный тип FrontmatterData (17 полей), двухпроходный парсинг, YAML-спецсимволы, brace expansion, 14 файлов-потребителей |
| **System Prompt** | 21 секция в точном порядке, SYSTEM_PROMPT_DYNAMIC_BOUNDARY, cached vs uncached sections, 3 режима сборки, feature gates |
| **Compaction** | 4 механизма (SM compact, legacy, reactive, microcompact), алгоритм calculateMessagesToKeepIndex(), 9-секционная суммаризация, PTL retry, circuit breaker, все пороги |
| **Forked Agent** | CacheSafeParams тип, global slot pattern, createSubagentContext() изоляция (contentReplacementState!), cache hit rate, maxOutputTokens ломает cache, реальные примеры |
| **/memory** | React-диалог, MemoryFileSelector (437 строк), редактор через $VISUAL/$EDITOR, 6 MEMORY_TYPE_VALUES |
| **Memory Detection** | 5 функций детекции, isShellCommandTargetingMemory(), memoryScopeForPath(), @include полный flow |
| **Write Carve-Out** | DANGEROUS_DIRECTORIES = 4 элемента, двухфазная проверка, carve-out условен на !hasAutoMemPathOverride(), validateMemoryPath() 8 проверок |

---

### H. Верификация: аудит точности документа

Каждое утверждение документа проверено против исходного кода в `claude-leaked/src/`. Проверено ~90 утверждений.

#### Найденные и исправленные ошибки

| # | Секция | Было | Стало | Файл-источник |
|---|--------|------|-------|---------------|
| 1 | §1 Путь к директории | `policy/local/user` | `policy/flag/local/user` — пропущен `flagSettings` | `memdir/paths.ts:179-186` |
| 2 | §1 Staleness | "старше 1 дня" | "старше >1 дня (2+ дней)" — `if (d <= 1) return ''` | `memdir/memoryAge.ts:35` |
| 3 | §7 CLAUDE.md Managed | `/etc/claude-code/CLAUDE.md` только | Добавлены пути macOS и Windows | `managedPath.ts:19-23` |
| 4 | §B System Context | "последние коммиты (max 2000 chars)" | 2000 chars — только для `git status --short`; коммиты без лимита | `context.ts:20 (MAX_STATUS_CHARS)` |
| 5 | §C Auto-compact порог | `max(maxOutputTokens, 20000)` | `min(maxOutputTokens, 20000)` — резервирует меньшее | `autoCompact.ts:33-48` |
| 6 | §C Legacy compact §6 | "All user messages (verbatim)" | "List ALL user messages that are not tool results" — verbatim не в этой секции | `compact/prompt.ts:68-76` |
| 7 | §C Microcompact tools | "BASH" | "BASH, POWERSHELL" — `SHELL_TOOL_NAMES` включает оба | `microCompact.ts:41-50` |
| 8 | §7 + §E | "Max 40KB" | "40,000 символов" — `MAX_MEMORY_CHARACTER_COUNT = 40000` (символы, не байты) | `claudemd.ts:92` |

#### Подтверждённые утверждения (выборка ключевых)

| Утверждение | Подтверждено | Строка |
|-------------|--------------|--------|
| `buildMemoryLines()` в `memdir/memdir.ts` | Да | `memdir.ts:199` |
| MEMORY.md: 200 строк, 25KB | Да | `memdir.ts:35,38` |
| `findRelevantMemories()` → Sonnet → до 5 файлов → JSON `{selected_memories}` | Да | `findRelevantMemories.ts:20,99,109-118` |
| extractMemories: `stopHooks.ts:149`, void, `!agentId`, `tengu_passport_quail` | Да | `stopHooks.ts:143,149`, `paths.ts:70` |
| `maxTurns = 5` для extract | Да | `extractMemories.ts:426` |
| `hasMemoryWritesSince()` — mutual exclusion | Да | `extractMemories.ts:121-148` |
| Session memory: 10 секций, 2000 tok/section, 12000 total | Да | `SessionMemory/prompts.ts:8-9,11-41` |
| Auto Dream: ≥24ч, ≥5 sessions, file lock, 4 фазы | Да | `autoDream.ts:63,65,56`, `consolidationLock.ts:16`, `consolidationPrompt.ts:26-63` |
| Team sync: server wins, delta push, deletion не propagates | Да | `teamMemorySync/index.ts:15,18-19,872-873` |
| `FRONTMATTER_REGEX`, двухпроходный парсинг, 17 полей | Да | `frontmatterParser.ts:123,148-169,10-59` |
| `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` = `'__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__'` | Да | `prompts.ts:114-115` |
| 7 статических + 13 динамических секций, MCP единственный UNCACHED | Да | `prompts.ts:560-571,491-554` |
| SM compact: `minTokens=10K`, `minTextBlockMessages=5`, `maxTokens=40K` | Да | `sessionMemoryCompact.ts:57-61` |
| `AUTOCOMPACT_BUFFER_TOKENS=13000`, `POST_COMPACT_TOKEN_BUDGET=50000` | Да | `autoCompact.ts:62`, `compact.ts:123` |
| `CacheSafeParams`: 5 полей, global slot, `saveCacheSafeParams` | Да | `forkedAgent.ts:57-68,73-81` |
| `contentReplacementState` клонируется (не fresh) — для cache hit | Да | `forkedAgent.ts:388-403` |
| `DANGEROUS_DIRECTORIES = ['.git', '.vscode', '.idea', '.claude']` | Да | `filesystem.ts:74-79` |
| Carve-out: `!hasAutoMemPathOverride() && isAutoMemPath()` | Да | `filesystem.ts:1572` |
| `validateMemoryPath()`: 8 проверок безопасности | Да | `paths.ts:109-150` |

#### Вердикт

**~90 утверждений проверено, 8 неточностей найдено и исправлено.** Документ отражает актуальное состояние кодовой базы.
