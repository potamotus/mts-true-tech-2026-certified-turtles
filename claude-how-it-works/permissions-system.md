# Claude Code — Система разрешений (Permissions System)

Анализ на основе утёкшего исходного кода Claude Code (`claude-leaked/src/`).

---

## Обзор: как принимается решение allow/deny/ask

Каждый вызов инструмента проходит через единую точку входа — `hasPermissionsToUseTool()` в `permissions.ts:473`. Функция возвращает одно из трёх решений:

| Решение | Значение | Что происходит |
|---------|----------|----------------|
| `allow` | Инструмент может выполняться | Выполнение без вопросов |
| `deny` | Инструмент заблокирован | Модели сообщается об отказе |
| `ask` | Нужно подтверждение | Пользователю показывается диалог (или авто-отказ в headless) |

Четвёртое внутреннее значение `passthrough` (строка 1301) конвертируется в `ask` перед возвратом — оно означает "tool не высказал мнения".

### Полная цепочка решения (hasPermissionsToUseToolInner)

Файл: `permissions.ts:1158-1319`

```
Шаг 1a. Deny rule на весь инструмент → deny
Шаг 1b. Ask rule на весь инструмент → ask (кроме sandbox auto-allow для Bash)
Шаг 1c. tool.checkPermissions() → tool-specific проверка
Шаг 1d. Tool deny → deny
Шаг 1e. tool.requiresUserInteraction() → ask (даже в bypass)
Шаг 1f. Content-specific ask rule (e.g. Bash(npm publish:*)) → ask
Шаг 1g. Safety check (DANGEROUS_DIRECTORIES) → ask (bypass-immune)
Шаг 2a. bypassPermissions mode → allow
Шаг 2b. Tool-wide allow rule → allow
Шаг 3.  passthrough → ask (с suggestions)
```

### Post-processing (hasPermissionsToUseTool)

После `hasPermissionsToUseToolInner` применяются режимные трансформации (`permissions.ts:473-956`):

1. **allow** — сбросить consecutive denials в auto mode
2. **ask + dontAsk mode** — конвертировать в deny
3. **ask + auto mode** — запустить AI-классификатор (YOLO classifier)
4. **ask + shouldAvoidPermissionPrompts** — авто-deny (headless agents)

---

## 1. Permission Modes — режимы разрешений

Файл: `types/permissions.ts:16-38`, `PermissionMode.ts`

### Внешние режимы (доступны всем пользователям)

| Режим | Описание | Символ |
|-------|----------|--------|
| `default` | Спрашивает для записи и shell-команд | — |
| `acceptEdits` | Авто-разрешает редактирование файлов в рабочей директории | `⏵⏵` |
| `plan` | Режим планирования (не выполняет действия) | `⏸` |
| `bypassPermissions` | Пропускает все проверки (кроме safety checks и deny rules) | `⏵⏵` |
| `dontAsk` | Конвертирует все `ask` в `deny` — никогда не спрашивает | `⏵⏵` |

### Внутренние режимы (ant-only)

| Режим | Описание |
|-------|----------|
| `auto` | AI-классификатор решает вместо пользователя |
| `bubble` | Внутренний координационный режим |

### Цикл переключения (Shift+Tab)

Файл: `getNextPermissionMode.ts:34-79`

```
default → acceptEdits → plan → bypassPermissions* → auto* → default
```

`*` — только если доступны (bypassPermissions через `--dangerously-skip-permissions`, auto через feature gate).

Для ant-пользователей: `default → bypassPermissions → auto → default` (пропускают acceptEdits и plan).

---

## 2. Permission Rules — правила разрешений

### Структура правила

Файл: `PermissionRule.ts`, `types/permissions.ts:54-75`

```typescript
type PermissionRule = {
  source: PermissionRuleSource   // откуда пришло правило
  ruleBehavior: PermissionBehavior  // 'allow' | 'deny' | 'ask'
  ruleValue: {
    toolName: string             // имя инструмента, e.g. 'Bash'
    ruleContent?: string         // опциональный контент, e.g. 'npm install:*'
  }
}
```

### Источники правил (PermissionRuleSource)

Файл: `permissions.ts:109-114`

| Источник | Файл | Приоритет |
|----------|------|-----------|
| `policySettings` | `/etc/claude-code/settings.json` (managed) | Высший |
| `flagSettings` | `--settings` CLI аргумент | |
| `projectSettings` | `.claude/settings.json` (в git) | |
| `localSettings` | `.claude/settings.local.json` (gitignored) | |
| `userSettings` | `~/.claude/settings.json` | |
| `cliArg` | CLI аргументы `--allowedTools`, `--disallowedTools` | |
| `command` | Frontmatter slash-команд | |
| `session` | In-memory, текущая сессия | |

### Формат правил в settings.json

```json
{
  "permissions": {
    "allow": ["Bash(npm install:*)", "Edit(src/**)", "mcp__server1"],
    "deny": ["Bash(rm -rf:*)", "Edit(//.env)"],
    "ask": ["Bash(git push:*)"]
  }
}
```

### Парсинг правил (permissionRuleParser.ts)

Файл: `permissionRuleParser.ts:93-133`

Формат: `ToolName` или `ToolName(content)`

```typescript
permissionRuleValueFromString('Bash')
// → { toolName: 'Bash' }

permissionRuleValueFromString('Bash(npm install)')
// → { toolName: 'Bash', ruleContent: 'npm install' }

permissionRuleValueFromString('Bash(python -c "print\\(1\\)")')
// → { toolName: 'Bash', ruleContent: 'python -c "print(1)"' }
```

Поддерживает:
- Экранирование скобок: `\(` и `\)`
- Экранирование бэкслешей: `\\`
- Legacy tool names: `Task` → `Agent`, `KillShell` → `TaskStop`
- `Bash()` и `Bash(*)` трактуются как tool-wide rule (без content)

### Matching правил для shell-команд

Файл: `shellRuleMatching.ts:159-184`

Три типа правил:

| Тип | Пример | Семантика |
|-----|--------|-----------|
| `exact` | `Bash(npm install)` | Точное совпадение команды |
| `prefix` | `Bash(npm:*)` | Команда начинается с `npm` (legacy `:*` синтаксис) |
| `wildcard` | `Bash(git * --force)` | Wildcard `*` = любая последовательность символов |

**Wildcard matching** (`shellRuleMatching.ts:90-154`):
- `*` матчит любую строку (включая пустую)
- `\*` — литеральная звёздочка
- `\\` — литеральный бэкслеш
- `git *` матчит и `git add` и голый `git` (trailing wildcard optional, строка 143)
- Regex с флагом `s` (dotAll) — wildcard матчит newlines

### Matching правил для файловых путей

Файл: `filesystem.ts:955-1025`

Используется библиотека `ignore` (gitignore-совместимая). Паттерны резолвятся относительно корня:

| Префикс паттерна | Корень | Пример |
|-------------------|--------|--------|
| `//` | Filesystem root (`/`) | `//etc/hosts` |
| `~/` | Home directory | `~/.ssh/**` |
| `/` | Директория settings-файла | `/src/**` |
| `./` или без префикса | CWD | `*.env` |

### Порядок проверки правил

1. **Deny rules** проверяются первыми — всегда побеждают
2. **Ask rules** проверяются вторыми
3. **Allow rules** проверяются последними

Deny-правила **bypass-immune** — работают даже в `bypassPermissions` mode (шаг 1a).

### Shadowed Rules Detection

Файл: `shadowedRuleDetection.ts:193-234`

Детектор находит "мёртвые" allow-правила:
- Allow rule `Bash(ls:*)` при наличии tool-wide deny `Bash` → completely blocked
- Allow rule `Bash(ls:*)` при наличии tool-wide ask `Bash` → always prompts

Исключение: Bash с sandbox auto-allow — tool-wide ask rule от personal settings не блокирует specific allow rules.

---

## 3. Filesystem Permissions — полное дерево решений

### Проверка чтения (checkReadPermissionForTool)

Файл: `filesystem.ts:1030-1194`

```
1. UNC path defense → ask
2. Suspicious Windows patterns → ask
3. Read deny rules → deny
4. Read ask rules → ask
5. Edit access implies read access (если write allowed → read allowed)
6. Working directory → allow
7. Internal readable paths (session-memory, plans, tool-results, scratchpad, project-temp) → allow
8. Read allow rules → allow
9. Default → ask (с suggestions)
```

### Проверка записи (checkWritePermissionForTool)

Файл: `filesystem.ts:1205-1412`

```
1.   Edit deny rules → deny
1.5. Internal editable paths (plan, scratchpad, agent memory, memdir, launch.json) → allow
1.6. .claude/** session allow rules → allow (только session scope!)
1.7. Safety checks (Windows patterns, config files, dangerous dirs) → ask
2.   Edit ask rules → ask
3.   acceptEdits mode + working directory → allow
4.   Edit allow rules → allow
5.   Default → ask
```

### Ключевая деталь: symlink resolution

Файл: `filesystem.ts:1048`, `fsOperations.ts:getPathsForPermissionCheck`

Для каждого пути проверяются **все представления**: оригинальный путь + путь после resolve symlinks. Если хотя бы одно представление не проходит проверку — отказ.

---

## 4. DANGEROUS_DIRECTORIES и DANGEROUS_FILES

### DANGEROUS_DIRECTORIES

Файл: `filesystem.ts:74-79`

```typescript
export const DANGEROUS_DIRECTORIES = [
  '.git',
  '.vscode',
  '.idea',
  '.claude',
] as const
```

**Enforcement** (`filesystem.ts:435-488` — `isDangerousFilePathToAutoEdit`):
- Итерирует все сегменты пути
- Case-insensitive сравнение (`normalizeCaseForComparison`, строка 90)
- Если найден сегмент из списка → блокировка

**Спецкейс `.claude/worktrees/`** (строки 460-467):
```typescript
if (dir === '.claude') {
  const nextSegment = pathSegments[i + 1]
  if (nextSegment && normalizeCaseForComparison(nextSegment) === 'worktrees') {
    break // Skip this .claude, continue checking other segments
  }
}
```
Путь `.claude/worktrees/...` **пропускается** — это структурная директория для git worktrees.

### DANGEROUS_FILES

Файл: `filesystem.ts:57-68`

```typescript
export const DANGEROUS_FILES = [
  '.gitconfig',
  '.gitmodules',
  '.bashrc',
  '.bash_profile',
  '.zshrc',
  '.zprofile',
  '.profile',
  '.ripgreprc',
  '.mcp.json',
  '.claude.json',
] as const
```

Это файлы конфигурации, через которые возможны code execution или data exfiltration.

### Suspicious Windows Path Patterns

Файл: `filesystem.ts:537-602`

Дополнительная защита от обхода через Windows-специфичные паттерны:

| Паттерн | Пример | Опасность |
|---------|--------|-----------|
| NTFS Alternate Data Streams | `file.txt::$DATA` | Скрытые данные |
| 8.3 short names | `CLAUDE~1` | Обход string matching |
| Long path prefixes | `\\?\C:\...` | Обход лимитов пути |
| Trailing dots/spaces | `.git.` | Windows strips при resolution |
| DOS device names | `.git.CON` | Спецустройства |
| Triple dots | `.../file.txt` | Path confusion |
| UNC paths | `\\server\share` | Network credential leak |

Проверка выполняется на **всех платформах** (NTFS может быть смонтирован через ntfs-3g).

### Claude Config Files

Файл: `filesystem.ts:225-242`

Отдельная проверка для файлов конфигурации Claude:
- `.claude/settings.json` и `.claude/settings.local.json` — в любом проекте
- `.claude/commands/`, `.claude/agents/`, `.claude/skills/` — в текущем CWD

---

## 5. Carve-outs — исключения из DANGEROUS_DIRECTORIES

Файл: `filesystem.ts:1479-1604` — `checkEditableInternalPath`

Пути, которые разрешены для записи **до** проверки DANGEROUS_DIRECTORIES:

| Путь | Условие | Строки |
|------|---------|--------|
| Session plan files | `{plansDir}/{planSlug}*.md` | 1488-1497 |
| Scratchpad | `/tmp/claude-{uid}/{cwd}/{session}/scratchpad/` | 1500-1509 |
| Template job directory | `~/.claude/jobs/...` (feature TEMPLATES) | 1520-1551 |
| Agent memory | `isAgentMemoryPath()` | 1554-1563 |
| Auto memory (memdir) | `isAutoMemPath()` **без** override | 1572-1581 |
| `.claude/launch.json` | Точный match, project-level | 1590-1602 |

### Memdir carve-out — ключевая деталь

```typescript
// filesystem.ts:1572
if (!hasAutoMemPathOverride() && isAutoMemPath(normalizedPath)) {
  return { behavior: 'allow', ... }
}
```

Carve-out работает **только** для дефолтного пути (`~/.claude/projects/{cwd}/memory/`). Если задан `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` — carve-out отключён, запись идёт через стандартный permission flow.

### Readable internal paths

Файл: `filesystem.ts:1611-1695`

| Путь | Описание |
|------|----------|
| Session memory dir | `{projectDir}/{sessionId}/session-memory/` |
| Project dir | `~/.claude/projects/{sanitized-cwd}/` |
| Plan files | Текущая сессия |
| Tool results dir | Persisted large outputs |
| Scratchpad | Текущая сессия |
| Project temp dir | `/tmp/claude-{uid}/{sanitized-cwd}/` (все сессии) |
| Bundled skills root | `/tmp/claude-{uid}/bundled-skills/{VERSION}/{nonce}/` |

### .claude/skills/ scope

Файл: `filesystem.ts:101-157` — `getClaudeSkillScope`

При редактировании файла в `.claude/skills/{name}/` генерируется узкий session-allow паттерн `/.claude/skills/{name}/**`, чтобы итерация над одним skill не требовала доступа ко всему `.claude/`.

Защита:
- Отклоняет `..` в имени skill (строка 142)
- Отклоняет glob-метасимволы `*?[]` (строка 150)
- Требует файл **внутри** skill-директории, не прямо под `skills/` (строка 136)

---

## 6. Bash Command Classification — анализ shell-команд

### Bash Classifier (bashClassifier.ts)

В утёкшем коде — **stub**. Полная реализация ant-only:

```typescript
// bashClassifier.ts:24
export function isClassifierPermissionsEnabled(): boolean {
  return false
}

// bashClassifier.ts:40
export async function classifyBashCommand(...): Promise<ClassifierResult> {
  return { matches: false, confidence: 'high', reason: 'This feature is disabled' }
}
```

Типы показывают архитектуру:
- `ClassifierResult`: `{ matches: boolean, matchedDescription?: string, confidence: 'high'|'medium'|'low', reason: string }`
- `ClassifierBehavior`: `'deny' | 'ask' | 'allow'`
- Поддерживает `prompt:` правила для семантического matching через LLM

### Dangerous Bash Patterns

Файл: `dangerousPatterns.ts`

Паттерны для опасных shell-команд (используются для очистки allow-правил при входе в auto mode):

**Cross-platform** (строки 18-42):
```
python, python3, python2, node, deno, tsx, ruby, perl, php, lua,
npx, bunx, npm run, yarn run, pnpm run, bun run,
bash, sh, ssh
```

**Bash-only** (строки 44-80):
```
zsh, fish, eval, exec, env, xargs, sudo
```

**Ant-only** (строки 58-79):
```
fa run, coo, gh, gh api, curl, wget, git,
kubectl, aws, gcloud, gsutil
```

Эти паттерны кормят `isDangerousBashPermission` в `permissionSetup.ts` — при входе в auto mode allow-правила с этими префиксами удаляются.

### Subcommand Analysis

Shell-команды разбиваются на подкоманды. Каждая проверяется отдельно:
- Оператор `&&`, `;`, `||` разделяют команды
- Output redirections (`>`, `>>`) анализируются отдельно — файл-цель проверяется как write

Результат: `decisionReason.type === 'subcommandResults'` содержит Map `[command, result]` для каждой подкоманды.

---

## 7. YOLO Mode (Auto Mode) — AI-классификатор

Файл: `yoloClassifier.ts` (1000+ строк), `permissions.ts:520-927`

### Что это

Auto mode заменяет диалог подтверждения на AI-классификатор. Вместо вопроса пользователю, **Sonnet** анализирует действие и транскрипт разговора, решая block/allow.

### Fast paths (до классификатора)

Файл: `permissions.ts:600-686`

Перед запуском дорогого API-вызова проверяются три fast path:

1. **acceptEdits fast path** (строки 600-656): если `tool.checkPermissions()` с mode=acceptEdits вернул бы `allow` — авто-разрешить (пропускает Agent и REPL)
2. **Safe tool allowlist** (строки 660-686): инструменты из `SAFE_YOLO_ALLOWLISTED_TOOLS` пропускаются без классификатора
3. **Safety check immunity** (строки 532-548): non-classifierApprovable safety checks (Windows patterns) блокируют даже в auto mode

### Safe Tool Allowlist

Файл: `classifierDecision.ts:56-94`

```
Read, Grep, Glob, LSP, ToolSearch, ListMcpResources, ReadMcpResourceTool,
TodoWrite, TaskCreate, TaskGet, TaskUpdate, TaskList, TaskStop, TaskOutput,
AskUserQuestion, EnterPlanMode, ExitPlanMode,
TeamCreate, TeamDelete, SendMessage,
WorkflowTool*, Sleep, TerminalCapture*,
OverflowTest*, VerifyPlanExecution*, classify_result
```

`*` — условно включены через feature gates.

### Двухстадийный XML-классификатор

Файл: `yoloClassifier.ts:711-800+`

**Stage 1 (fast)**: max_tokens=64, stop_sequences=[`</block>`], суффикс "Err on the side of blocking. `<block>` immediately."
- Если `<block>no</block>` → **allow**, конец
- Если `<block>yes</block>` → эскалация на Stage 2

**Stage 2 (thinking)**: max_tokens=1024, суффикс с инструкцией "Review the classification process... Use `<thinking>` before responding with `<block>`."
- Chain-of-thought reasoning
- Финальное решение block/allow

**Три режима** (`mode` параметр):
- `both` (default): Stage 1 → Stage 2 при блокировке
- `fast`: только Stage 1 (max_tokens=256, без stop_sequences)
- `thinking`: только Stage 2

### Формат ответа

```xml
<block>yes</block><reason>one short sentence</reason>
```
или
```xml
<block>no</block>
```

`<thinking>` блоки strip-аются перед парсингом, чтобы теги внутри reasoning не мешали.

### System prompt

Файл: `yoloClassifier.ts:484-540`

Собирается из:
1. `BASE_PROMPT` — `auto_mode_system_prompt.txt`
2. `<permissions_template>` — `permissions_external.txt` или `permissions_anthropic.txt`
3. User overrides из `settings.autoMode`: `allow`, `soft_deny`, `environment`

Пользовательские значения **заменяют** дефолты в external template, и **дополняют** в anthropic template.

### Транскрипт

Файл: `yoloClassifier.ts:302-360`

Классификатор получает compact transcript:
- User text messages → `{"user":"text"}`
- Assistant tool_use blocks → `{"Bash":"ls -la"}` (через `toAutoClassifierInput`)
- Assistant text **исключён** — может содержать prompt injection

CLAUDE.md инъектируется как prefix message в `<user_claude_md>` тег (строка 460-477).

### Fail modes

| Ситуация | Поведение |
|----------|-----------|
| Classifier unavailable + `tengu_iron_gate_closed=true` | **Deny** (fail closed) |
| Classifier unavailable + gate=false | Fall back to normal prompting |
| Transcript too long | Fall back to prompting / abort в headless |
| Parse error | `shouldBlock=true` (fail closed) |

---

## 8. Denial Tracking — отслеживание отказов

Файл: `denialTracking.ts`

### Состояние

```typescript
type DenialTrackingState = {
  consecutiveDenials: number  // сбрасывается при каждом allow
  totalDenials: number        // только растёт
}
```

### Лимиты

```typescript
export const DENIAL_LIMITS = {
  maxConsecutive: 3,
  maxTotal: 20,
} as const
```

### Логика

Файл: `denialTracking.ts:40-44`

```typescript
export function shouldFallbackToPrompting(state: DenialTrackingState): boolean {
  return (
    state.consecutiveDenials >= DENIAL_LIMITS.maxConsecutive ||
    state.totalDenials >= DENIAL_LIMITS.maxTotal
  )
}
```

### Что происходит при превышении лимита

Файл: `permissions.ts:984-1058`

1. **CLI mode**: classifier deny → конвертируется в `ask` с warning: "N consecutive actions were blocked. Please review the transcript before continuing."
2. **Headless mode**: `throw new AbortError('Agent aborted: too many classifier denials in headless mode')`
3. **totalDenials ≥ 20**: счётчики обнуляются после показа warning

### Сброс consecutiveDenials

Происходит при **любом** allow — включая allow через rules, allowlist, acceptEdits fast path (строки 486-500).

### Persistence

Файл: `permissions.ts:963-978`

- Для async subagents: `Object.assign(context.localDenialTracking, newState)` (мутация in-place, setAppState is no-op)
- Для main thread: `context.setAppState(prev => ({ ...prev, denialTracking: newState }))`

---

## 9. Sandbox Integration — как sandbox ограничивает

Файл: `sandbox-adapter.ts` (986 строк)

### Роль sandbox в permissions

Sandbox — OS-level ограничение shell-команд через `bubblewrap` (Linux/WSL) или `sandbox-exec` (macOS). Работает **параллельно** с permission system.

### Ключевая связь: autoAllowBashIfSandboxed

Файл: `sandbox-adapter.ts:469-472`, `permissions.ts:1189-1193`

```typescript
const canSandboxAutoAllow =
  tool.name === BASH_TOOL_NAME &&
  SandboxManager.isSandboxingEnabled() &&
  SandboxManager.isAutoAllowBashIfSandboxedEnabled() &&
  shouldUseSandbox(input)
```

Когда sandbox включён + `autoAllowBashIfSandboxed=true` (default true):
- Tool-wide ask rule на Bash **пропускается**
- Sandboxed команды авто-разрешаются
- Non-sandboxed команды (excluded, `dangerouslyDisableSandbox`) всё ещё требуют ask

### Конвертация permission rules в sandbox config

Файл: `sandbox-adapter.ts:172-381`

Settings → `SandboxRuntimeConfig`:

| Permission rule | Sandbox config |
|-----------------|---------------|
| `Edit(src/**)` allow | `filesystem.allowWrite: ['src/**']` |
| `Edit(//.env)` deny | `filesystem.denyWrite: ['.env']` |
| `Read(secrets/)` deny | `filesystem.denyRead: ['secrets/']` |
| `WebFetch(domain:*.example.com)` allow | `network.allowedDomains: ['*.example.com']` |

Всегда добавляются:
- `allowWrite: ['.', getClaudeTempDir()]` — CWD и temp всегда writable
- `denyWrite: [settingsPaths...]` — settings.json файлы всегда read-only
- `denyWrite: ['.claude/skills']` — skills защищены от sandbox escape

### Bare git repo protection

Файл: `sandbox-adapter.ts:257-280`

Защита от атаки: злоумышленник создаёт `HEAD`, `objects/`, `refs/` в CWD, превращая его в bare git repo с `core.fsmonitor` → code execution при следующем `git` вызове вне sandbox.

Решение:
- Если файл существует: `denyWrite` (read-only bind mount)
- Если не существует: post-command scrub (`scrubBareGitRepoFiles()`, строка 404)

### Sandbox availability

```
isSandboxingEnabled() =
  isSupportedPlatform() &&
  checkDependencies().errors.length === 0 &&
  isPlatformInEnabledList() &&
  getSandboxEnabledSetting()
```

Поддерживаемые платформы: macOS, Linux, WSL2+ (не WSL1).

---

## 10. Permission Prompts — взаимодействие с пользователем

### Prompt flow

Файл: `hooks/useCanUseTool.tsx`

1. `hasPermissionsToUseTool()` возвращает `ask`
2. `tool.description(input)` генерирует описание для UI
3. В зависимости от контекста:
   - **Interactive CLI**: `handleInteractivePermission()` — показывает диалог
   - **Coordinator/Swarm**: `handleCoordinatorPermission()` / `handleSwarmWorkerPermission()`
   - **Headless**: авто-deny (или hooks)

### Permission Explainer

Файл: `permissionExplainer.ts`

При показе диалога параллельно запускается LLM-объяснение:

```typescript
const EXPLAIN_COMMAND_TOOL = {
  properties: {
    explanation: 'What this command does (1-2 sentences)',
    reasoning: 'Why YOU are running this command. Start with "I"',
    risk: 'What could go wrong, under 15 words',
    riskLevel: 'LOW | MEDIUM | HIGH',
  }
}
```

Использует main loop model, forced tool choice. Результат показывается в UI рядом с командой.

### Permission Updates (suggestions)

Файл: `filesystem.ts:1414-1473`

При `ask` решении генерируются `suggestions` — предложения для "Always allow":

| Ситуация | Suggestion |
|----------|------------|
| Write в CWD, mode=default | `setMode: 'acceptEdits'` |
| Write вне CWD | `addDirectories` + `setMode: 'acceptEdits'` |
| Read вне CWD | `addRules: Read({dir}/**)` |
| Write в `.claude/skills/{name}/` | `addRules: Edit(/.claude/skills/{name}/**)` |

### PermissionRequest Hooks

Файл: `permissions.ts:400-471`

Headless/async agents не могут показать диалог. Вместо авто-deny сначала запускаются `PermissionRequest` hooks:

```typescript
for await (const hookResult of executePermissionRequestHooks(...)) {
  if (hookResult.permissionRequestResult?.behavior === 'allow') {
    // Hook allowed — persist updates, return allow
  }
  if (hookResult.permissionRequestResult?.behavior === 'deny') {
    // Hook denied — optionally abort
  }
}
// No hook decision → auto-deny
```

### Managed Permission Rules Only

Файл: `permissionsLoader.ts:31-36`

```typescript
export function shouldAllowManagedPermissionRulesOnly(): boolean {
  return getSettingsForSource('policySettings')
    ?.allowManagedPermissionRulesOnly === true
}
```

Когда включено:
- Загружаются только правила из `policySettings`
- "Always allow" кнопки скрыты
- При sync очищаются все non-policy источники

---

## 11. Key Constants and Thresholds

### Denial Tracking

| Константа | Значение | Файл:строка |
|-----------|----------|-------------|
| `maxConsecutive` | 3 | `denialTracking.ts:13` |
| `maxTotal` | 20 | `denialTracking.ts:14` |

### Classifier

| Константа | Значение | Файл:строка |
|-----------|----------|-------------|
| `CLASSIFIER_FAIL_CLOSED_REFRESH_MS` | 30 минут | `permissions.ts:107` |
| Stage 1 max_tokens | 64 | `yoloClassifier.ts:781` |
| Stage 2 max_tokens | 1024 (default) | implicit |
| Stage 1 fast-only max_tokens | 256 | `yoloClassifier.ts:781` |
| Always-on thinking padding | 2048 | `yoloClassifier.ts:690` |

### Filesystem

| Константа | Значение | Файл:строка |
|-----------|----------|-------------|
| `DANGEROUS_DIRECTORIES` | `.git`, `.vscode`, `.idea`, `.claude` | `filesystem.ts:74-79` |
| `DANGEROUS_FILES` | 10 файлов (.gitconfig, .bashrc, ...) | `filesystem.ts:57-68` |

### Sandbox

| Константа | Значение | Файл:строка |
|-----------|----------|-------------|
| `autoAllowBashIfSandboxed` default | `true` | `sandbox-adapter.ts:471` |
| `allowUnsandboxedCommands` default | `true` | `sandbox-adapter.ts:476` |

### Security Nonces

| Механизм | Назначение | Файл:строка |
|----------|------------|-------------|
| Bundled skills nonce | `randomBytes(16).toString('hex')` — per-process | `filesystem.ts:367` |
| Scratchpad permissions | `0o700` (owner-only) | `filesystem.ts:404` |
| Claude temp dir | `claude-{uid}` (per-user на Unix) | `filesystem.ts:313` |

---

## 12. Bypass Permissions — что обходит, что нет

### bypassPermissions mode (step 2a)

Файл: `permissions.ts:1268-1281`

**Разрешает всё**, КРОМЕ:
- Step 1a: tool-wide deny rules
- Step 1d: tool implementation deny
- Step 1e: `requiresUserInteraction()` tools
- Step 1f: content-specific ask rules (e.g. `Bash(npm publish:*)` с `ruleBehavior: 'ask'`)
- Step 1g: safety checks (DANGEROUS_DIRECTORIES, config files)

**Plan + bypassPermissions available**: если пользователь стартовал с `--dangerously-skip-permissions`, переход в plan mode сохраняет bypass capability (`isBypassPermissionsModeAvailable`).

### Killswitch

Файл: `bypassPermissionsKillswitch.ts`

Statsig gate может отключить bypassPermissions. Проверяется один раз перед первым запросом:

```typescript
const shouldDisable = await shouldDisableBypassPermissions()
if (shouldDisable) {
  setAppState(prev => ({
    ...prev,
    toolPermissionContext: createDisabledBypassPermissionsContext(prev.toolPermissionContext),
  }))
}
```

### Auto mode gate

Аналогично — `verifyAutoModeGateAccess()` может отключить auto mode. Проверяется при старте и при смене модели/fast mode.

---

## 13. Полная диаграмма решения для Bash-команды

```
Bash("npm install")
│
├─ 1a. Deny rule "Bash"?  → deny
├─ 1b. Ask rule "Bash"?
│      ├─ sandbox enabled + autoAllow + shouldUseSandbox? → skip, continue
│      └─ otherwise → ask
├─ 1c. BashTool.checkPermissions()
│      ├─ Deny rule "Bash(npm install)" → deny
│      ├─ Allow rule "Bash(npm:*)" → allow (via prefix match)
│      ├─ Wildcard "Bash(npm *)" → allow (via wildcard match)
│      ├─ Output redirections → check write permissions for targets
│      └─ passthrough
├─ 1d. Tool deny? → deny
├─ 1e. requiresUserInteraction? → N/A for Bash
├─ 1f. Content-specific ask rule? → ask
├─ 1g. Safety check? → N/A for Bash commands
├─ 2a. bypassPermissions mode? → allow
├─ 2b. Tool-wide allow "Bash"? → allow
├─ 3.  passthrough → ask
│
└─ Post-processing:
   ├─ dontAsk mode? → deny
   ├─ auto mode?
   │   ├─ acceptEdits fast path? → allow
   │   ├─ safe allowlist? → N/A (Bash not in list)
   │   └─ YOLO classifier → allow/deny
   └─ headless? → PermissionRequest hooks → auto-deny
```

---

## Верификация: аудит точности документа

Каждое утверждение проверено против исходного кода. Проверено ~60 ключевых утверждений.

### Подтверждённые утверждения

| Утверждение | Файл:строка |
|-------------|-------------|
| `DANGEROUS_DIRECTORIES = ['.git', '.vscode', '.idea', '.claude']` | `filesystem.ts:74-79` |
| `.claude/worktrees/` пропускается | `filesystem.ts:460-467` |
| `DANGEROUS_FILES` = 10 файлов | `filesystem.ts:57-68` |
| `normalizeCaseForComparison` = `.toLowerCase()` | `filesystem.ts:91` |
| Carve-out: `!hasAutoMemPathOverride() && isAutoMemPath()` | `filesystem.ts:1572` |
| Plan files carve-out с `normalize()` | `filesystem.ts:1488-1497` |
| Scratchpad dir: `0o700` permissions | `filesystem.ts:404` |
| `DENIAL_LIMITS.maxConsecutive = 3, maxTotal = 20` | `denialTracking.ts:12-15` |
| `recordSuccess` сбрасывает только consecutiveDenials | `denialTracking.ts:32-38` |
| `shouldFallbackToPrompting` = OR двух условий | `denialTracking.ts:40-44` |
| YOLO classifier tool = `classify_result` | `yoloClassifier.ts:260` |
| `yoloClassifierResponseSchema`: `thinking, shouldBlock, reason` | `yoloClassifier.ts:253-258` |
| Stage 1 max_tokens = 64, Stage 1 fast = 256 | `yoloClassifier.ts:781` |
| XML S1 suffix = "Err on the side of blocking" | `yoloClassifier.ts:550` |
| `SAFE_YOLO_ALLOWLISTED_TOOLS` содержит Read, Grep, Glob, etc. | `classifierDecision.ts:56-94` |
| `CLASSIFIER_FAIL_CLOSED_REFRESH_MS = 30 * 60 * 1000` | `permissions.ts:107` |
| `bashClassifier.ts` = stub (external build) | `bashClassifier.ts:24-26` |
| `CROSS_PLATFORM_CODE_EXEC` = 22 паттерна | `dangerousPatterns.ts:18-42` |
| `DANGEROUS_BASH_PATTERNS` включает ant-only блок | `dangerousPatterns.ts:58-79` |
| Shell rules: exact, prefix (`:*`), wildcard | `shellRuleMatching.ts:159-184` |
| Wildcard `git *` матчит bare `git` (trailing optional) | `shellRuleMatching.ts:142-144` |
| `permissionRuleValueFromString` обрабатывает escaped parens | `permissionRuleParser.ts:93-133` |
| Permission modes: 5 external + 2 internal (auto, bubble) | `types/permissions.ts:16-38` |
| `bypassPermissions` пропускает шаги 1a-1g, allow на 2a | `permissions.ts:1262-1281` |
| Safety checks bypass-immune (1g) | `permissions.ts:1252-1260` |
| Content-specific ask rules bypass-immune (1f) | `permissions.ts:1244-1250` |
| `autoAllowBashIfSandboxed` default = true | `sandbox-adapter.ts:471` |
| Sandbox always denyWrite settings.json | `sandbox-adapter.ts:232-236` |
| Sandbox always denyWrite `.claude/skills` | `sandbox-adapter.ts:252-255` |
| Bare git repo scrub: HEAD, objects, refs, hooks, config | `sandbox-adapter.ts:268` |
| `getClaudeTempDir()` resolves symlinks (macOS /tmp) | `filesystem.ts:331-347` |
| Bundled skills nonce: `randomBytes(16).toString('hex')` | `filesystem.ts:367` |
| `checkPathSafetyForAutoEdit` проверяет и symlink-resolved paths | `filesystem.ts:627-629` |
| Edit deny rules проверяются ДО internal editable paths | `filesystem.ts:1222-1239` |
| `isClaudeSettingsPath` case-insensitive | `filesystem.ts:207` |
| Headless denial limit → `AbortError` | `permissions.ts:1023-1027` |
| Total denial limit → counters reset | `permissions.ts:1034-1040` |
| `permissionExplainerEnabled` default = true | `permissionExplainer.ts:140` |
| Permission explainer uses main loop model | `permissionExplainer.ts:175` |
| `isSharedSettingSource` = project, policy, command | `shadowedRuleDetection.ts:61-67` |

### Вердикт

**~60 утверждений проверено, все подтверждены.** Документ точно отражает исходный код.
