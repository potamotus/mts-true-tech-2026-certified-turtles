# Claude Code — Система плагинов и навыков (Skills & Plugins)

Анализ на основе утёкшего исходного кода Claude Code (claude-leaked/src/).

---

## Часть 1: Система навыков (Skills)

---

## 1. Обзор: что такое навык

Навык (Skill) — это **markdown-файл с YAML-frontmatter**, содержащий инструкции для модели. Навыки расширяют возможности Claude Code без написания кода: пользователь описывает процесс в `.md` файле, и модель следует этим инструкциям при вызове.

Навык можно вызвать двумя способами:
1. **Пользователь** вводит `/skill-name` в CLI (если `user-invocable: true`)
2. **Модель** вызывает `Skill` tool программно (если `disable-model-invocation` не установлен)

Типы навыков по источнику (`LoadedFrom`):

| Тип | Источник | Пример |
|-----|----------|--------|
| `bundled` | Встроены в бинарник CLI | `/verify`, `/simplify`, `/remember` |
| `skills` | Директория `.claude/skills/` | Пользовательские навыки |
| `commands_DEPRECATED` | Директория `.claude/commands/` | Legacy-формат |
| `plugin` | Из установленного плагина | Plugin skills |
| `mcp` | Из MCP-сервера | MCP skill builders |
| `managed` | Корпоративная политика | Policy skills |

---

## 2. Формат навыка — frontmatter и структура

### Структура файлов на диске

Навыки хранятся **только** в формате директорий:

```
~/.claude/skills/
  my-skill/
    SKILL.md          # Обязательный файл
    helper.sh         # Вспомогательные файлы (опционально)
```

Файл `SKILL.md` — единственный обязательный файл. Одиночные `.md` файлы в `/skills/` **НЕ поддерживаются** (`loadSkillsDir.ts:425-427`).

Legacy-формат `/commands/` поддерживает и директории с `SKILL.md`, и одиночные `.md` файлы.

### YAML Frontmatter

Из `loadSkillsDir.ts:185-265`, полный набор полей:

```yaml
---
name: My Skill                    # Отображаемое имя (опционально)
description: What the skill does  # Описание для модели и пользователя
user-invocable: true              # Доступен ли через /skill-name (default: true)
disable-model-invocation: false   # Запретить модели вызывать через Skill tool
argument-hint: "[file] [options]" # Подсказка по аргументам
arguments: [file, mode]           # Именованные аргументы для подстановки
when_to_use: "When user wants..." # Когда использовать (для модели)
model: sonnet                     # Override модели (sonnet/opus/haiku/inherit)
effort: high                      # Уровень усилий (low/medium/high/max/integer)
context: fork                     # Режим исполнения: inline (default) или fork
agent: custom-agent               # Тип агента при context=fork
allowed-tools: [Read, Grep, Bash] # Тулы, авто-разрешённые при вызове
paths: "src/**/*.{ts,tsx}"        # Glob-паттерны для условной активации
shell: bash                       # Shell для !`cmd` блоков (bash/powershell)
hooks:                            # Хуки при вызове навыка
  PreToolUse: [...]
version: "1.0.0"                  # Версия навыка
---
```

### Тело навыка

Тело — обычный markdown. Поддерживает:

- **`${CLAUDE_SKILL_DIR}`** — заменяется на директорию навыка (`loadSkillsDir.ts:359-363`)
- **`${CLAUDE_SESSION_ID}`** — ID текущей сессии (`loadSkillsDir.ts:366-369`)
- **`$1`, `$2` и именованные аргументы** — подстановка через `substituteArguments()` (`loadSkillsDir.ts:349-354`)
- **`!`cmd``** — inline shell-команды, исполняются при загрузке навыка (`executeShellCommandsInPrompt`, `loadSkillsDir.ts:374-396`). Для MCP-навыков shell-команды **запрещены** из соображений безопасности (`loadSkillsDir.ts:371-373`)

---

## 3. Загрузка навыков — loadSkillsDir.ts

### Иерархия источников

`getSkillDirCommands()` (`loadSkillsDir.ts:638-804`) загружает навыки из 5 источников **параллельно**:

| # | Источник | Путь | Условие |
|---|----------|------|---------|
| 1 | **Managed** (политика) | `/etc/claude-code/.claude/skills/` | `!CLAUDE_CODE_DISABLE_POLICY_SKILLS` |
| 2 | **User** | `~/.claude/skills/` | `isSettingSourceEnabled('userSettings')` |
| 3 | **Project** | `.claude/skills/` (от CWD вверх до home) | `isSettingSourceEnabled('projectSettings')` |
| 4 | **Additional dirs** | `--add-dir` пути | `projectSettingsEnabled` |
| 5 | **Legacy commands** | `.claude/commands/` (от CWD вверх) | `!skillsLocked` |

### Алгоритм загрузки из директории

`loadSkillsFromSkillsDir()` (`loadSkillsDir.ts:407-480`):

1. `readdir(basePath)` — список entries
2. Фильтрация: только директории и symlinks (строка 425)
3. Для каждой: читает `<name>/SKILL.md`
4. `parseFrontmatter()` — извлечение YAML + содержимое
5. `parseSkillFrontmatterFields()` — парсинг всех полей
6. `createSkillCommand()` — создание объекта `Command`

### Дедупликация

Дедупликация по **resolved path** через `realpath()` (`loadSkillsDir.ts:726-763`):

```typescript
const fileIds = await Promise.all(
  allSkillsWithPaths.map(({ skill, filePath }) =>
    skill.type === 'prompt' ? getFileIdentity(filePath) : Promise.resolve(null)
  )
)
```

Используется `realpath()` для разрешения symlinks. Первый найденный навык с данным path побеждает. Это обрабатывает overlapping parent directories и symlinks.

### Conditional Skills (навыки с paths)

Навыки с frontmatter `paths` не загружаются сразу — они хранятся в `conditionalSkills` Map (`loadSkillsDir.ts:771-797`).

Активация — `activateConditionalSkillsForPaths()` (`loadSkillsDir.ts:997-1058`):

```typescript
const skillIgnore = ignore().add(skill.paths)
if (skillIgnore.ignores(relativePath)) {
  dynamicSkills.set(name, skill)
  conditionalSkills.delete(name)
  activatedConditionalSkillNames.add(name)
}
```

Использует библиотеку `ignore` (gitignore-style matching). Когда модель обращается к файлу, matching по glob-паттернам, при совпадении навык перемещается в `dynamicSkills` и становится доступен модели.

### Dynamic Skill Discovery

`discoverSkillDirsForPaths()` (`loadSkillsDir.ts:861-915`) — обнаружение навыков из вложенных директорий:

1. Для каждого file path идёт вверх от parent directory до CWD
2. Проверяет наличие `.claude/skills/` в каждой директории
3. Пропускает gitignored директории
4. Сортирует deepest-first (ближе к файлу = выше приоритет)
5. Вызывает `addSkillDirectories()` для загрузки

### Bare Mode

`--bare` режим (`loadSkillsDir.ts:658-675`): пропускает автообнаружение (managed/user/project/legacy). Загружает **только** из явно указанных `--add-dir` путей. Bundled skills регистрируются отдельно.

---

## 4. Каталог встроенных навыков (Bundled Skills)

Встроенные навыки регистрируются через `registerBundledSkill()` (`bundledSkills.ts:53-100`) и инициализируются в `initBundledSkills()` (`bundled/index.ts:24-79`).

### Всегда доступные навыки

| Навык | Описание | Ant-only | Файл |
|-------|----------|----------|------|
| `/update-config` | Помощь в настройке settings.json с полным JSON Schema | Нет | `updateConfig.ts` |
| `/keybindings` | Настройка горячих клавиш с таблицей всех биндингов | Нет | `keybindings.ts` |
| `/verify` | Верификация кода: запуск приложения и проверка | Да | `verify.ts` |
| `/debug` | Включение debug-логов, чтение последних 20 строк | Нет | `debug.ts` |
| `/lorem-ipsum` | Генерация точного количества токенов (калибровочный навык) | Нет | `loremIpsum.ts` |
| `/skillify` | Превращение текущей сессии в reusable skill | Нет | `skillify.ts` |
| `/remember` | Ревью auto-memory, предложение промоции в CLAUDE.md | Да | `remember.ts` |
| `/simplify` | Code review: 3 параллельных агента (reuse, quality, efficiency) | Нет | `simplify.ts` |
| `/batch` | Параллельная оркестрация 5-30 sub-agents с worktrees | Нет | `batch.ts` |
| `/stuck` | Диагностика зависших сессий Claude Code (ps, pgrep, Slack) | Нет | `stuck.ts` |

### Навыки за feature flags

| Навык | Feature Flag | Описание |
|-------|-------------|----------|
| `/dream` | `KAIROS` / `KAIROS_DREAM` | Ночная консолидация |
| `/hunter` | `REVIEW_ARTIFACT` | Code review артефакт |
| `/loop` | `AGENT_TRIGGERS` | Cron-запуск агентов |
| `/schedule-remote-agents` | `AGENT_TRIGGERS_REMOTE` | Удалённое расписание агентов |
| `/claude-api` | `BUILDING_CLAUDE_APPS` | Помощь с Claude API |
| `/claude-in-chrome` | `shouldAutoEnableClaudeInChrome()` | Chrome-интеграция |
| `/run-skill-generator` | `RUN_SKILL_GENERATOR` | Генерация навыков |

### Механика bundled skills

`registerBundledSkill()` создаёт объект `Command` с `source: 'bundled'` и `loadedFrom: 'bundled'` (`bundledSkills.ts:75-100`).

Ключевая особенность — **reference files** (`bundledSkills.ts:59-73`):

```typescript
if (files && Object.keys(files).length > 0) {
  skillRoot = getBundledSkillExtractDir(definition.name)
  getPromptForCommand = async (args, ctx) => {
    extractionPromise ??= extractBundledSkillFiles(definition.name, files)
    const extractedDir = await extractionPromise
    const blocks = await inner(args, ctx)
    if (extractedDir === null) return blocks
    return prependBaseDir(blocks, extractedDir)
  }
}
```

Bundled skills могут содержать вспомогательные файлы (Record<path, content>). При первом вызове файлы извлекаются на диск в `getBundledSkillsRoot()/<name>/`. Используется `O_EXCL | O_NOFOLLOW` для защиты от symlink-атак (`bundledSkills.ts:176-193`). Модель может затем `Read`/`Grep` эти файлы.

---

## 5. SkillTool — исполнение навыков

Файл: `tools/SkillTool/SkillTool.ts`

### Два режима исполнения

| Режим | Условие | Поведение |
|-------|---------|-----------|
| **Inline** | `context` не задан или `context: inline` | Промпт навыка инъектируется в **текущий** разговор как user message |
| **Fork** | `context: fork` | Навык выполняется в **изолированном sub-agent** через `runAgent()` |

### Inline исполнение

`SkillTool.call()` (`SkillTool.ts:580-841`):

1. `findCommand()` — поиск команды по имени (с поддержкой aliases)
2. `recordSkillUsage()` — трекинг для ранжирования
3. Если `command.context === 'fork'` → переход к fork-исполнению
4. `processPromptSlashCommand()` — обработка навыка: подстановка аргументов, shell-команды, формирование сообщений
5. Возврат `newMessages` + `contextModifier` для модификации контекста

`contextModifier` (`SkillTool.ts:775-839`) модифицирует контекст:
- **allowed-tools** → добавляются в `alwaysAllowRules`
- **model** → переопределяет `mainLoopModel`
- **effort** → переопределяет `effortValue`

### Fork исполнение

`executeForkedSkill()` (`SkillTool.ts:122-289`):

1. `prepareForkedCommandContext()` — подготовка изолированного контекста
2. `runAgent()` — запуск sub-agent с:
   - Модифицированным `getAppState` (инъекция skill permissions)
   - Опциональным override модели (`command.model`)
   - Объединённым effort (`command.effort`)
3. Сбор `agentMessages`, progress reporting
4. `extractResultText()` — извлечение результата
5. Возврат текстового результата (не newMessages)

### Валидация и permissions

**Валидация** (`validateInput`, строки 354-430):
- Trim + удаление ведущего `/`
- Проверка существования команды
- `disableModelInvocation` → reject
- Тип должен быть `'prompt'`

**Permissions** (`checkPermissions`, строки 432-578):
- Deny rules проверяются первыми
- Allow rules проверяются вторыми
- Auto-allow для навыков с **только safe properties** (`SAFE_SKILL_PROPERTIES` — 28 свойств, строки 875-908)
- Если ни deny, ни allow, ни auto-allow → `ask` пользователя

### Safe Properties логика

`skillHasOnlySafeProperties()` (`SkillTool.ts:910-933`) — если навык НЕ содержит свойств за пределами SAFE_SKILL_PROPERTIES (или все дополнительные null/undefined/пустые), permission не требуется. Это обеспечивает forward-compatibility: новые свойства по умолчанию требуют permission.

---

## 6. Skill Hooks

Навыки поддерживают хуки через frontmatter поле `hooks`:

```yaml
---
hooks:
  PreToolUse:
    - matcher: "Bash"
      hooks:
        - type: command
          command: "echo pre-hook"
  PostToolUse:
    - matcher: ".*"
      hooks:
        - type: command
          command: "echo post-hook"
---
```

Парсинг: `parseHooksFromFrontmatter()` (`loadSkillsDir.ts:136-153`):

```typescript
function parseHooksFromFrontmatter(frontmatter, skillName) {
  if (!frontmatter.hooks) return undefined
  const result = HooksSchema().safeParse(frontmatter.hooks)
  if (!result.success) {
    logForDebugging(`Invalid hooks in skill '${skillName}': ${result.error.message}`)
    return undefined
  }
  return result.data
}
```

Валидация через `HooksSchema` (Zod). Невалидные хуки **тихо игнорируются** (логируются в debug).

Хуки навыка **регистрируются при вызове** навыка (не при загрузке) — через `registerSkillHooks()` внутри `processPromptSlashCommand()`.

---

## 7. ToolSearchTool — отложенная загрузка и поиск

Файл: `tools/ToolSearchTool/ToolSearchTool.ts`

### Концепция deferred tools

Не все тулы загружаются сразу — MCP-тулы и тулы с `shouldDefer: true` доступны только по имени. Модель видит их имена в `<system-reminder>` сообщениях, но не может вызвать без загрузки полной схемы.

`isDeferredTool()` (`prompt.ts:62-108`) определяет, откладывается ли тул:

| Условие | Deferred? |
|---------|-----------|
| `tool.alwaysLoad === true` (MCP opt-out) | **Нет** |
| `tool.isMcp === true` | **Да** |
| `tool.name === 'ToolSearch'` | **Нет** (нужен для загрузки остальных) |
| `tool.name === 'Agent'` + FORK_SUBAGENT | **Нет** |
| `tool.name === 'Brief'` + KAIROS | **Нет** |
| `tool.shouldDefer === true` | **Да** |

### Два режима запроса

**1. Direct selection** — `select:<tool_name>`:
```
query: "select:Read,Edit,Grep"
```
Ищет по точному имени среди deferred tools, поддерживает comma-separated multi-select. Если тул уже загружен — возвращает его (harmless no-op).

**2. Keyword search** — свободный текст:
```
query: "slack send message"
query: "+slack send"        # +prefix = required term
```

Алгоритм скоринга (`searchToolsWithKeywords`, строки 186-302):

| Match type | Score | Условие |
|-----------|-------|---------|
| Exact part match (MCP) | 12 | Точное совпадение части имени |
| Exact part match (regular) | 10 | Точное совпадение части имени |
| Partial part match (MCP) | 6 | Часть имени содержит term |
| Partial part match (regular) | 5 | Часть имени содержит term |
| searchHint match | 4 | Word boundary match в searchHint |
| Full name fallback | 3 | Полное имя содержит term |
| Description match | 2 | Word boundary match в описании |

Required terms (`+prefix`) используются как pre-filter — только тулы, содержащие ВСЕ required terms, проходят в scoring.

### Результат

`mapToolResultToToolResultBlockParam` возвращает `tool_reference` блоки:

```typescript
content: content.matches.map(name => ({
  type: 'tool_reference',
  tool_name: name,
}))
```

Это заставляет API раскрыть полные JSONSchema определения для найденных тулов, делая их вызываемыми.

---

## 8. MCP Skills — mcpSkillBuilders.ts

### Проблема: циклические зависимости

MCP skills загружаются из `mcpSkills.ts`, который зависит от `client.ts → ... → loadSkillsDir.ts`. Прямой импорт создаёт циклическую зависимость.

### Решение: write-once registry

`mcpSkillBuilders.ts` (45 строк) — **leaf module** без import-ов:

```typescript
let builders: MCPSkillBuilders | null = null

export function registerMCPSkillBuilders(b: MCPSkillBuilders): void {
  builders = b
}

export function getMCPSkillBuilders(): MCPSkillBuilders {
  if (!builders) throw new Error('MCP skill builders not registered')
  return builders
}
```

Тип `MCPSkillBuilders`:
```typescript
type MCPSkillBuilders = {
  createSkillCommand: typeof createSkillCommand
  parseSkillFrontmatterFields: typeof parseSkillFrontmatterFields
}
```

Регистрация происходит при module init `loadSkillsDir.ts` (`loadSkillsDir.ts:1083-1086`):
```typescript
registerMCPSkillBuilders({
  createSkillCommand,
  parseSkillFrontmatterFields,
})
```

`loadSkillsDir.ts` импортируется статически из `commands.ts`, что гарантирует регистрацию **до** подключения любого MCP-сервера.

### Безопасность MCP skills

MCP skills **не выполняют inline shell-команды** (`loadSkillsDir.ts:371-373`):
```typescript
if (loadedFrom !== 'mcp') {
  finalContent = await executeShellCommandsInPrompt(...)
}
```

MCP skills являются remote и untrusted — shell-инъекция через `!`cmd`` запрещена.

---

## Часть 2: Система плагинов (Plugins)

---

## 9. Обзор архитектуры плагинов

Плагин — это **пакет из навыков, агентов, команд, хуков, MCP-серверов и output styles**, распространяемый через marketplace. В отличие от навыков (отдельные .md файлы), плагин — это полноценная директория с манифестом.

### Ключевые отличия от навыков

| Аспект | Навыки | Плагины |
|--------|--------|---------|
| Формат | Один .md файл | Директория с plugin.json |
| Установка | Копирование файла | Marketplace → git clone → cache |
| Компоненты | Только промпт | Skills + agents + hooks + MCP + output styles |
| Версионирование | Нет | Semver + git SHA |
| Marketplace | Нет | Да |
| Policy control | `skillsLocked` | Blocklist, allowlist, policy |

### Жизненный цикл плагина

```
1. Marketplace → settings.json (enabledPlugins)
2. Marketplace manifest → git clone → cache/
3. Plugin cache → loadAllPluginsCacheOnly()
4. Plugin → commands, agents, skills, hooks, MCP servers, output styles
5. Компоненты → AppState (mcp.commands, hooks, etc.)
```

---

## 10. Структура плагина — формат и манифест

### Структура директории

Из `pluginLoader.ts:14-27`:

```
my-plugin/
├── .claude-plugin/
│   └── plugin.json          # Манифест (обязателен)
├── skills/                  # Навыки (директории с SKILL.md)
│   └── my-skill/
│       └── SKILL.md
├── commands/                # Команды (legacy формат)
│   ├── build.md
│   └── deploy.md
├── agents/                  # Кастомные агенты
│   └── test-runner.md
├── output-styles/           # Стили вывода
│   └── concise.md
└── hooks/                   # Хуки
    └── hooks.json
```

### Plugin Manifest (plugin.json)

Из `schemas.ts:274-319` — `PluginManifestSchema`:

```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "description": "What the plugin does",
  "author": {
    "name": "Author Name",
    "email": "author@example.com",
    "url": "https://example.com"
  },
  "homepage": "https://docs.example.com",
  "repository": "https://github.com/org/repo",
  "license": "MIT",
  "keywords": ["testing", "ci"],
  "dependencies": ["other-plugin@my-marketplace"],
  "commands": ["./extra-cmd.md"],
  "agents": ["./extra-agent.md"],
  "skills": ["./extra-skills/"],
  "outputStyles": ["./extra-styles/"],
  "hooks": { "hooks": "./hooks/custom.json" },
  "mcpServers": { "server-name": { "command": "node", "args": ["server.js"] } },
  "mcpb": ["./server.mcpb"]
}
```

Ключевые поля:

| Поле | Schema | Описание |
|------|--------|----------|
| `name` | `string`, min 1, без пробелов | Уникальный идентификатор |
| `version` | `string`, опционально | Semver |
| `commands` | `path | path[] | Record<name, CommandMetadata>` | Дополнительные команды/навыки |
| `agents` | `path | path[]` | Дополнительные агенты |
| `skills` | `path | path[]` | Дополнительные skill-директории |
| `outputStyles` | `path | path[]` | Стили вывода |
| `hooks` | `path | HooksSettings | array` | Хуки |
| `mcpServers` | `Record<name, McpServerConfig>` | MCP-серверы |
| `mcpb` | `path[]` | MCP Bundle файлы (.mcpb/.dxt) |

### CommandMetadata

`CommandMetadataSchema` (`schemas.ts:385-416`) позволяет marketplace-записям предоставлять rich metadata:

```json
{
  "commands": {
    "about": {
      "source": "./README.md",
      "description": "Override description",
      "argumentHint": "[topic]",
      "model": "sonnet",
      "allowedTools": ["Read", "Grep"]
    }
  }
}
```

Альтернативно — `content` вместо `source` для inline markdown (но не оба одновременно).

---

## 11. Установка плагинов — PluginInstallationManager

Файл: `services/plugins/PluginInstallationManager.ts`

### Marketplace-модель

Плагины распространяются через **marketplaces** — репозитории GitHub с манифестом `marketplace.json`:

```
marketplace-repo/
├── .claude-plugin/
│   └── marketplace.json
└── plugins/
    ├── plugin-a/
    │   └── .claude-plugin/
    │       └── plugin.json
    └── plugin-b/
        └── ...
```

### Marketplace Manifest (marketplace.json)

```json
{
  "name": "my-marketplace",
  "metadata": {
    "description": "Collection of plugins"
  },
  "plugins": [
    {
      "name": "plugin-a",
      "description": "What it does",
      "source": "./plugins/plugin-a",
      "version": "1.0.0",
      "category": "testing"
    }
  ]
}
```

### Резервированные имена marketplace

`ALLOWED_OFFICIAL_MARKETPLACE_NAMES` (`schemas.ts:19-28`):
- `claude-code-marketplace`
- `claude-code-plugins`
- `claude-plugins-official`
- `anthropic-marketplace`
- `anthropic-plugins`
- `agent-skills`
- `life-sciences`
- `knowledge-work-plugins`

Имена блокируются через `BLOCKED_OFFICIAL_NAME_PATTERN` (`schemas.ts:72`) и защиту от homograph-атак через `NON_ASCII_PATTERN` (`schemas.ts:79`).

### Фоновая установка

`performBackgroundPluginInstallations()` (`PluginInstallationManager.ts:60-184`):

1. `getDeclaredMarketplaces()` — сбор из settings
2. `loadKnownMarketplacesConfig()` — текущий cache
3. `diffMarketplaces()` — diff (missing, sourceChanged)
4. `reconcileMarketplaces()` — clone/update с progress callback
5. Если new installs → `refreshActivePlugins()` (auto-refresh)
6. Если updates → `setAppState({ needsRefresh: true })` → уведомление `/reload-plugins`

### Plugin cache path

```
~/.claude/plugins/cache/{marketplace}/{plugin}/{version}/
```

Из `pluginLoader.ts:139-162`:

```typescript
function getVersionedCachePathIn(baseDir, pluginId, version): string {
  const { name: pluginName, marketplace } = parsePluginIdentifier(pluginId)
  const sanitizedMarketplace = (marketplace || 'unknown').replace(/[^a-zA-Z0-9\-_]/g, '-')
  const sanitizedPlugin = (pluginName || pluginId).replace(/[^a-zA-Z0-9\-_]/g, '-')
  const sanitizedVersion = version.replace(/[^a-zA-Z0-9\-_.]/g, '-')
  return join(baseDir, 'cache', sanitizedMarketplace, sanitizedPlugin, sanitizedVersion)
}
```

### Seed directories

`CLAUDE_CODE_PLUGIN_SEED_DIR` (`pluginDirectories.ts:65-90`) — pre-baked директории для контейнеров:
- Read-only fallback layer
- Поддержка нескольких seed через `path delimiter` (`:` на Unix, `;` на Windows)
- Структура зеркалирует primary plugins directory

### Plugin Data Directory

`getPluginDataDir()` (`pluginDirectories.ts:119-123`):
```
~/.claude/plugins/data/{sanitized-plugin-id}/
```
Persistent per-plugin данные, переживают обновления плагина. Доступны через `${CLAUDE_PLUGIN_DATA}`.

---

## 12. Загрузка компонентов плагина

### Навыки (loadPluginCommands.ts)

Мемоизированная функция сканирует:
1. Стандартные директории: `commands/` и `skills/`
2. Дополнительные пути из `plugin.json` (`commands`, `skills`)

Каждый навык получает namespace: `{plugin-name}:{skill-name}`.

Поддерживаются **plugin variables** через `substitutePluginVariables()`:
- `${CLAUDE_PLUGIN_ROOT}` — путь к директории плагина
- `${CLAUDE_PLUGIN_DATA}` — persistent data directory
- `${CLAUDE_SESSION_ID}` — ID сессии

И **user config variables** через `substituteUserConfigInContent()` — значения из plugin options storage.

### Агенты (loadPluginAgents.ts)

Из `loadPluginAgents.ts:37-63`:

Сканирует `agents/` директорию + дополнительные пути из м��нифеста. Каждый `.md` файл парсится как agent definition с полями:
- `description`, `effort`, `max-turns`
- `allowed-tools`, `disallowed-tools`
- `memory-scope` (user/project/local)
- `color` — цвет агента в UI

Namespace: `{plugin-name}:{agent-name}`.

### Output Styles (loadPluginOutputStyles.ts)

Из `loadPluginOutputStyles.ts:15-78`:

Сканирует `output-styles/` + дополнительные пути. Каждый `.md` файл становится `OutputStyleConfig`:

```typescript
return {
  name: `${pluginName}:${baseStyleName}`,
  description,
  prompt: markdownContent.trim(),
  source: 'plugin',
  forceForPlugin,  // может быть принудительным для плагина
}
```

### Хуки (loadPluginHooks.ts)

`convertPluginHooksToMatchers()` (`loadPluginHooks.ts:28-59`) — конвертирует plugin hooks в native matchers с plugin context.

Поддерживаемые события хуков (28 типов):

```typescript
const pluginMatchers: Record<HookEvent, PluginHookMatcher[]> = {
  PreToolUse: [], PostToolUse: [], PostToolUseFailure: [],
  PermissionDenied: [], Notification: [], UserPromptSubmit: [],
  SessionStart: [], SessionEnd: [], Stop: [], StopFailure: [],
  SubagentStart: [], SubagentStop: [],
  PreCompact: [], PostCompact: [],
  PermissionRequest: [], Setup: [],
  TeammateIdle: [], TaskCreated: [], TaskCompleted: [],
  Elicitation: [], ElicitationResult: [],
  ConfigChange: [], WorktreeCreate: [], WorktreeRemove: [],
  InstructionsLoaded: [], CwdChanged: [], FileChanged: [],
}
```

Каждый matcher хранит `pluginRoot`, `pluginName`, `pluginId` — для контекста при исполнении.

### MCP серверы (mcpPluginIntegration.ts)

Из `mcpPluginIntegration.ts:30-79`:

Поддерживает два формата:
1. **JSON config** — стандартный MCP server config (`mcpServers` в plugin.json)
2. **MCPB/DXT** — MCP Bundle файлы, скачиваемые и извлекаемые

MCPB flow:
1. `loadMcpbFile()` — загрузка + извлечение DXT манифеста
2. Если `needs-config` → ожидание user configuration
3. Если success → конвертация в `McpServerConfig`

---

## 13. Валидация плагинов — validatePlugin.ts

### Валидация plugin.json

`validatePluginManifest()` (`validatePlugin.ts:129-305`):

1. Чтение файла (ENOENT/EISDIR/permission handling)
2. JSON parse
3. **Path traversal check** — `checkPathTraversal()` для commands, agents, skills (`..` в путях)
4. **Marketplace-only fields warning** — `MARKETPLACE_ONLY_MANIFEST_FIELDS` (category, source, tags, strict, id)
5. **Strict schema validation** — `PluginManifestSchema().strict().safeParse()`
6. **Warnings** — kebab-case name, missing version/description/author

### Валидация marketplace.json

`validateMarketplaceManifest()` (`validatePlugin.ts:310-507`):

1. JSON parse + path traversal в plugin sources
2. Strict schema validation (с strict entries)
3. Duplicate plugin name detection
4. Version mismatch detection (entry vs plugin.json)
5. Missing description warning

### Валидация компонентов

`validatePluginContents()` (`validatePlugin.ts:763-809`) — рекурсивная валидация:
- `skills/` — только `<name>/SKILL.md` (matches runtime loader)
- `agents/`, `commands/` — все `.md` файлы рекурсивно
- `hooks/hooks.json` — через `PluginHooksSchema`

Для .md файлов — `validateComponentFile()` (`validatePlugin.ts:517-639`):
- YAML frontmatter parse (surfacing runtime-silent errors)
- description: scalar type check
- name: string type check
- allowed-tools: string or array validation
- shell: enum validation (bash/powershell)

### Auto-detection

`validateManifest()` (`validatePlugin.ts:814-903`):
- Директория → ищет `.claude-plugin/marketplace.json` → `.claude-plugin/plugin.json`
- Файл → определение по имени (plugin.json / marketplace.json)
- Неизвестно → эвристика по наличию `plugins` array

---

## 14. Plugin Cache

### Структура кеша

```
~/.claude/plugins/
├── known_marketplaces.json       # Конфигурация marketplace-ов
├── installed_plugins.json        # Метаданные установленных плагинов (V2)
├── marketplaces/                 # Клонированные marketplace-ы
│   ├── my-marketplace/           # Git clone
│   │   └── .claude-plugin/
│   │       └── marketplace.json
│   └── official-marketplace.json # URL-marketplace (JSON cache)
├── cache/                        # Кеш версий плагинов
│   └── {marketplace}/
│       └── {plugin}/
│           └── {version}/        # Распакованный плагин
└── data/                         # Persistent plugin data
    └── {plugin-id}/
```

### Zip Cache (zipCache.ts)

Для headless-режимов (контейнеры) — `CLAUDE_CODE_PLUGIN_USE_ZIP_CACHE`:

```
/mnt/plugins-cache/
├── known_marketplaces.json
├── installed_plugins.json
├── marketplaces/
│   └── *.json
└── plugins/
    └── {marketplace}/
        └── {plugin}/
            └── {version}.zip
```

Плагины хранятся как ZIP и извлекаются в session-local temp directory при старте. Поддерживает только `strict:true` marketplace entries.

### Installed Plugins Manager (installedPluginsManager.ts)

`installed_plugins.json` хранит:
- Какие плагины установлены глобально
- Метаданные установки (версия, timestamps, пути)
- **Не** enabled/disabled state (он в settings.json, per-repo)

Два уровня кеша:
1. **In-memory** (`inMemoryInstalledPlugins`) — snapshot при старте сессии
2. **File cache** (`installedPluginsCacheV2`) — мемоизированный read файла

Background updates модифицируют **только файл** — in-memory state сессии не меняется.

### Plugin Directories (pluginDirectories.ts)

Приоритет определения plugins directory:

1. `CLAUDE_CODE_PLUGIN_CACHE_DIR` env var
2. `~/.claude/plugins` (default)
3. `~/.claude/cowork_plugins` (при `--cowork` flag или `CLAUDE_CODE_USE_COWORK_PLUGINS`)

---

## 15. Built-in Plugins

Файл: `plugins/builtinPlugins.ts`

### Концепция

Built-in plugins — промежуточный слой между bundled skills и marketplace plugins:

| Аспект | Bundled Skills | Built-in Plugins | Marketplace Plugins |
|--------|---------------|------------------|-------------------|
| Появление в /plugin UI | Нет | Да | Да |
| Enable/disable | Нет (всегда) | Да | Да |
| Persistence | Нет | User settings | Per-repo settings |
| Компоненты | Только prompt | Skills + hooks + MCP | Всё |
| Установка | Не нужна | Не нужна | Git clone |

### Текущий статус

`initBuiltinPlugins()` (`plugins/bundled/index.ts:20-23`):

```typescript
export function initBuiltinPlugins(): void {
  // No built-in plugins registered yet — this is the scaffolding for
  // migrating bundled skills that should be user-toggleable.
}
```

Инфраструктура готова, но **ни один built-in plugin ещё не зарегистрирован**. Это scaffolding для будущей миграции bundled skills, которые должны быть toggleable.

### API

`registerBuiltinPlugin()` (`builtinPlugins.ts:28-32`) — регистрация определения.

`getBuiltinPlugins()` (`builtinPlugins.ts:57-101`) — возвращает enabled/disabled разделение на основе `settings.enabledPlugins[pluginId]`. Plugin ID формат: `{name}@builtin`.

`getBuiltinPluginSkillCommands()` (`builtinPlugins.ts:108-121`) — skill commands из enabled built-in plugins. Source выставляется как `'bundled'` (не `'builtin'`) для совместимости с Skill tool listing и analytics.

---

## 16. Marketplace Security

### Защита от impersonation

Многоуровневая защита:

1. **Reserved names** (`ALLOWED_OFFICIAL_MARKETPLACE_NAMES`) — 8 имён зарезервированы для Anthropic
2. **Pattern blocking** (`BLOCKED_OFFICIAL_NAME_PATTERN`) — regex блокирует вариации (official-claude, claude-marketplace-new)
3. **Homograph protection** (`NON_ASCII_PATTERN`) — блокировка non-ASCII символов
4. **Source verification** (`validateOfficialNameSource`) — reserved names только от `github.com/anthropics/`
5. **Marketplace policy** — `isSourceAllowedByPolicy()`, `isSourceInBlocklist()`

### Path Traversal Protection

`checkPathTraversal()` (`validatePlugin.ts:92-106`) — проверка `..` во всех путях plugin manifest:
- commands, agents, skills — security concern (escaping plugin dir)
- marketplace sources — resolution-base misunderstanding

`validatePathWithinBase()` (`pluginInstallationHelpers.ts`) — гарантия что resolved path остаётся внутри base directory.

---

## Как всё работает вместе

### При старте CLI

1. `initBundledSkills()` — регистрация встроенных навыков
2. `initBuiltinPlugins()` — регистрация built-in плагинов (пока пусто)
3. `getSkillDirCommands(cwd)` — загрузка навыков из managed/user/project/additional/legacy (мемоизировано)
4. `loadAllPluginsCacheOnly()` — загрузка плагинов из cache (без сети)
5. `performBackgroundPluginInstallations()` — фоновая установка/обновление marketplace-ов
6. Plugin компоненты → AppState (commands, agents, hooks, MCP servers)

### При вызове навыка

1. Пользователь: `/skill-name args` → или модель: `Skill(skill: "name", args: "...")`
2. `SkillTool.validateInput()` — проверка существования, типа, disableModelInvocation
3. `SkillTool.checkPermissions()` — deny/allow rules, safe properties, ask
4. `SkillTool.call()`:
   - **Inline**: `processPromptSlashCommand()` → newMessages + contextModifier
   - **Fork**: `executeForkedSkill()` → sub-agent → text result

### При обнаружении файла

1. Модель `Read`/`Edit`/`Write` файл
2. `discoverSkillDirsForPaths()` — поиск `.claude/skills/` в parent directories
3. `addSkillDirectories()` — загрузка новых навыков
4. `activateConditionalSkillsForPaths()` — активация conditional skills по glob patterns
5. `skillsLoaded.emit()` — уведомление listeners (очистка кешей)

### При поиске тулов

1. Модель видит deferred tool names в `<system-reminder>`
2. Вызывает `ToolSearch(query: "select:tool_name")` или `ToolSearch(query: "keyword search")`
3. `ToolSearchTool.call()` — поиск по имени/ключевым словам
4. Возврат `tool_reference` блоков → API раскрывает полные JSONSchema
5. Найденные тулы становятся вызываемыми

---

## Верификация

### Проверенные утверждения

| Утверждение | Файл | Строка |
|-------------|------|--------|
| Skills directory format: `skill-name/SKILL.md` only | `loadSkillsDir.ts` | 425-427 |
| Single .md files NOT supported in /skills/ | `loadSkillsDir.ts` | 426 comment |
| Dedup via `realpath()` | `loadSkillsDir.ts` | 118-124, 726-763 |
| Conditional skills via `ignore` library | `loadSkillsDir.ts` | 1012, 1029 |
| MCP skills: no shell execution | `loadSkillsDir.ts` | 371-374 |
| `${CLAUDE_SKILL_DIR}` substitution | `loadSkillsDir.ts` | 359-363 |
| `${CLAUDE_SESSION_ID}` substitution | `loadSkillsDir.ts` | 366-369 |
| Bundled skill files: `O_EXCL \| O_NOFOLLOW` | `bundledSkills.ts` | 176-184 |
| BundledSkillDefinition.files → lazy extraction | `bundledSkills.ts` | 59-73 |
| SkillTool inline vs fork: `command.context === 'fork'` | `SkillTool.ts` | 622 |
| SAFE_SKILL_PROPERTIES: 28 properties | `SkillTool.ts` | 875-908 |
| `isDeferredTool()`: MCP always deferred, `alwaysLoad` opt-out | `prompt.ts` | 62-108 |
| ToolSearch scoring: MCP exact=12, regular exact=10 | `ToolSearchTool.ts` | 271-275 |
| `mcpSkillBuilders.ts`: leaf module, write-once registry | `mcpSkillBuilders.ts` | 1-44 |
| Registration at `loadSkillsDir.ts` module init | `loadSkillsDir.ts` | 1083-1086 |
| Plugin manifest: PluginManifestSchema with strict validation | `validatePlugin.ts` | 247 |
| `ALLOWED_OFFICIAL_MARKETPLACE_NAMES`: 8 entries | `schemas.ts` | 19-28 |
| Built-in plugins: `initBuiltinPlugins()` is empty scaffolding | `bundled/index.ts` | 20-23 |
| Plugin hooks: 28 event types | `loadPluginHooks.ts` | 31-59 |
| `BLOCKED_OFFICIAL_NAME_PATTERN` regex | `schemas.ts` | 72 |
| Non-ASCII blocking (`NON_ASCII_PATTERN`) | `schemas.ts` | 79 |
| Zip cache: `CLAUDE_CODE_PLUGIN_USE_ZIP_CACHE` env | `zipCache.ts` | 55-57 |
| Plugin data dir: `${CLAUDE_PLUGIN_DATA}` | `pluginDirectories.ts` | 98-123 |
| Seed dirs: `CLAUDE_CODE_PLUGIN_SEED_DIR` with delimiter split | `pluginDirectories.ts` | 85-90 |
| `performBackgroundPluginInstallations()`: diff → reconcile → refresh | `PluginInstallationManager.ts` | 60-184 |
| Marketplace source hint for `..` paths | `validatePlugin.ts` | 113-124 |

### Не покрытые темы

- `officialMarketplaceGcs.ts` — загрузка official marketplace из GCS
- `pluginAutoupdate.ts` — автообновление плагинов
- `dependencyResolver.ts` — разрешение зависимостей между плагинами
- `pluginFlagging.ts` — флаги и блокировка плагинов
- `reconciler.ts` — детальная логика reconciliation marketplace-ов
- `lspPluginIntegration.ts` / `lspRecommendation.ts` — LSP-интеграция плагинов
- `hintRecommendation.ts` — рекомендация плагинов
- Детальная логика `pluginLoader.ts` (900+ строк) — полный flow загрузки
