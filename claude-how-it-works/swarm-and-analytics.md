# Claude Code — Swarm / Multi-Agent & Analytics / Telemetry

Анализ на основе утёкшего исходного кода Claude Code (claude-leaked/src/).

---

# Часть 1: Swarm & Multi-Agent система

## Обзор архитектуры

Claude Code имеет **три уровня** мультиагентности:

| # | Уровень | Механизм | Изоляция | Кто управляет |
|---|---------|----------|----------|---------------|
| 1 | **AgentTool** (субагенты) | `runForkedAgent()` — fork в том же процессе | Prompt cache sharing, `createSubagentContext()` | Основной агент |
| 2 | **Coordinator Mode** | Специальный system prompt, workers через AgentTool | Те же субагенты, но async с `<task-notification>` | Coordinator LLM |
| 3 | **Swarm (Teams)** | Отдельные процессы (tmux/iTerm2/in-process) | Полная: свой Node.js контекст или AsyncLocalStorage | Team Lead |

---

## 1. Coordinator Mode

Файл: `coordinator/coordinatorMode.ts`

### Активация

```typescript
// coordinatorMode.ts:36-41
export function isCoordinatorMode(): boolean {
  if (feature('COORDINATOR_MODE')) {
    return isEnvTruthy(process.env.CLAUDE_CODE_COORDINATOR_MODE)
  }
  return false
}
```

Двойной гейт: compile-time feature flag `COORDINATOR_MODE` + runtime env var `CLAUDE_CODE_COORDINATOR_MODE`.

### Роль координатора

Координатор получает **специальный system prompt** (строки 111-368) который полностью переопределяет поведение. Ключевые отличия от обычного агента:

| Свойство | Обычный агент | Coordinator |
|----------|--------------|-------------|
| Тулы | Все стандартные | Только `Agent`, `SendMessage`, `TaskStop`, `subscribe_pr_activity` |
| Работа с файлами | Напрямую | Только через workers |
| Модель взаимодействия | Синхронная | Async: `<task-notification>` XML |
| System prompt | Стандартный | Специализированный (370 строк) |

### Worker Tools

Координатор сообщает модели, какие тулы доступны workers (строки 88-108):

- **Simple mode** (`CLAUDE_CODE_SIMPLE`): `Bash`, `Read`, `Edit`
- **Standard mode**: все из `ASYNC_AGENT_ALLOWED_TOOLS`, минус внутренние (`TeamCreate`, `TeamDelete`, `SendMessage`, `SyntheticOutput`)

Если подключены MCP-серверы — их имена добавляются в контекст.

### Scratchpad

Если включён gate `tengu_scratch`, координатор получает scratchpad-директорию:
```
Workers can read and write here without permission prompts.
Use this for durable cross-worker knowledge.
```

### Session Resume

При возобновлении сессии `matchSessionMode()` (строки 49-78) синхронизирует режим: если сессия была coordinator, а текущий процесс нет — переключает env var на лету.

### Workflow фазы (из system prompt)

| Фаза | Кто | Цель |
|------|-----|------|
| Research | Workers (параллельно) | Исследование кодобазы |
| Synthesis | **Coordinator** | Осмысление результатов, составление спецификации |
| Implementation | Workers | Точечные изменения по спецификации |
| Verification | Workers | Тестирование, typecheck |

**Ключевое правило**: координатор **обязан синтезировать** результаты research сам. Промпт явно запрещает "based on your findings" — это делегирование понимания.

---

## 2. AgentTool — Sub-Agent Spawning

Файлы: `tools/AgentTool/`

### Типы агентов (loadAgentsDir.ts)

```typescript
// loadAgentsDir.ts:162-165
export type AgentDefinition =
  | BuiltInAgentDefinition    // source: 'built-in'
  | CustomAgentDefinition     // source: SettingSource
  | PluginAgentDefinition     // source: 'plugin'
```

**Приоритет разрешения** (при одинаковом `agentType`, последний побеждает):
1. Built-in agents
2. Plugin agents
3. User settings agents
4. Project settings agents
5. Flag settings agents (GrowthBook)
6. Policy/Managed settings agents

### BaseAgentDefinition — полная структура (строки 106-133)

| Поле | Тип | Назначение |
|------|-----|------------|
| `agentType` | `string` | Уникальное имя агента |
| `whenToUse` | `string` | Описание для LLM когда использовать |
| `tools` | `string[]` | Allowlist тулов (undefined = все) |
| `disallowedTools` | `string[]` | Denylist тулов |
| `model` | `string` | Модель (`'inherit'` = родительская) |
| `effort` | `EffortValue` | `'low'`/`'medium'`/`'high'`/`'max'` или integer |
| `permissionMode` | `PermissionMode` | Режим permissions |
| `maxTurns` | `number` | Лимит API roundtrips |
| `memory` | `AgentMemoryScope` | `'user'`/`'project'`/`'local'` |
| `isolation` | `'worktree'`/`'remote'` | Изоляция в git worktree или CCR |
| `background` | `boolean` | Всегда в фоне |
| `omitClaudeMd` | `boolean` | Пропустить CLAUDE.md (экономия токенов) |
| `mcpServers` | `AgentMcpServerSpec[]` | MCP-серверы для агента |
| `hooks` | `HooksSettings` | Session-scoped хуки |
| `skills` | `string[]` | Preload skills |
| `initialPrompt` | `string` | Prepend к первому user turn |

### Fork Subagent (forkSubagent.ts)

Новый механизм — **fork** вместо специализированного субагента:

```typescript
// forkSubagent.ts:32-39
export function isForkSubagentEnabled(): boolean {
  if (feature('FORK_SUBAGENT')) {
    if (isCoordinatorMode()) return false    // взаимоисключающе с coordinator
    if (getIsNonInteractiveSession()) return false
    return true
  }
  return false
}
```

Fork наследует **полный контекст** родителя (system prompt, историю сообщений). Cache sharing максимальный — отличается только directive в конце.

**FORK_AGENT** (строки 60-71):
- `tools: ['*']` — все тулы родителя
- `model: 'inherit'` — модель родителя (для cache sharing)
- `permissionMode: 'bubble'` — permissions всплывают к родителю
- `maxTurns: 200`

**Защита от рекурсии**: `isInForkChild()` проверяет наличие `<fork-boilerplate>` тега в истории сообщений.

**buildForkedMessages()** (строки 107-169): создаёт byte-identical prefix для cache sharing. Все tool_result'ы заменяются на одинаковый placeholder `'Fork started — processing in background'`, уникален только directive в конце.

### Prompt (prompt.ts)

Промпт AgentTool занимает ~288 строк. Ключевые секции:

1. **Agent listing** — может быть inline (в tool description) или через attachment message (`tengu_agent_list_attach`). Attachment вариант экономит ~10.2% fleet cache_creation токенов.

2. **When to fork** (только при fork enabled) — инструкции когда форкать vs. спавнить субагента.

3. **Writing the prompt** — как писать промпты для субагентов.

4. **When NOT to use** — не использовать для простых file reads, grep, поиска определений.

---

## 3. Built-in Agents

Файл: `tools/AgentTool/builtInAgents.ts`

### Список встроенных агентов

| Агент | Файл | Условие | Назначение |
|-------|------|---------|------------|
| `general-purpose` | `generalPurposeAgent.ts` | Всегда | Дефолтный агент |
| `statusline-setup` | `statuslineSetup.ts` | Всегда | Настройка statusline |
| `Explore` | `exploreAgent.ts` | `tengu_amber_stoat` gate | Исследование кодобазы (read-only) |
| `Plan` | `planAgent.ts` | `tengu_amber_stoat` gate | Планирование (read-only) |
| `claude-code-guide` | `claudeCodeGuideAgent.ts` | Не SDK entrypoint | Гайд по Claude Code |
| `verification` | `verificationAgent.ts` | `tengu_hive_evidence` gate | Верификация кода |

**ONE_SHOT_BUILTIN_AGENT_TYPES** = `{'Explore', 'Plan'}` — для них пропускается agentId/SendMessage/usage trailer (~135 chars x 34M Explore runs/week).

**Coordinator mode**: заменяет все built-in agents на `getCoordinatorAgents()` из `coordinator/workerAgent.js`.

**Отключение**: `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=true` + noninteractive mode → пустой список.

### Explore/Plan — omitClaudeMd

Explore и Plan агенты имеют `omitClaudeMd: true`. Read-only агентам не нужны commit/PR/lint guidelines — основной агент имеет полный CLAUDE.md и интерпретирует их вывод. Экономия: ~5-15 Gtok/week при 34M+ Explore spawns.

---

## 4. Agent Backends (Swarm)

Директория: `utils/swarm/backends/`

### Типы бэкендов

| Бэкенд | Файл | Механизм | Когда используется |
|--------|------|----------|-------------------|
| **TmuxBackend** | `TmuxBackend.ts` | tmux split-pane | Внутри tmux или fallback |
| **ITermBackend** | `ITermBackend.ts` | iTerm2 native API | В iTerm2 без tmux |
| **InProcessBackend** | `InProcessBackend.ts` | AsyncLocalStorage | В том же Node.js процессе |

### Детекция (detection.ts)

```
1. Проверка: внутри tmux? → TmuxBackend (shared session)
2. Проверка: iTerm2 + it2 CLI? → ITermBackend
3. Fallback: TmuxBackend с внешней сессией (claude-swarm-${PID})
```

### In-Process Backend (spawnInProcess.ts)

Наиболее интересный вариант. Teammates работают **в том же Node.js процессе** с изоляцией через `AsyncLocalStorage`:

```typescript
// spawnInProcess.ts:104-216
export async function spawnInProcessTeammate(config, context):
  1. agentId = formatAgentId(name, teamName)  // "name@team"
  2. abortController = createAbortController()  // независимый от лидера
  3. identity = { agentId, agentName, teamName, color, planModeRequired, parentSessionId }
  4. teammateContext = createTeammateContext({...})  // для AsyncLocalStorage
  5. registerPerfettoAgent(agentId, ...)  // трассировка
  6. registerTask(taskState, setAppState)  // регистрация в AppState
```

**Изоляция**: teammates НЕ abort-ятся при interrupt лидера (строка 121-122: "Teammates should not be aborted when the leader's query is interrupted").

### Kill (строки 227-328)

При kill in-process teammate:
1. `abortController.abort()`
2. Cleanup handler
3. Все `onIdleCallbacks` вызываются (разблокировка waiters)
4. Удаление из `teamContext.teammates`
5. `removeMemberByAgentId()` из team file
6. `evictTaskOutput()`, `emitTaskTerminatedSdk()`
7. `unregisterPerfettoAgent()`

### Tmux Constants (constants.ts)

```typescript
TEAM_LEAD_NAME = 'team-lead'
SWARM_SESSION_NAME = 'claude-swarm'
SWARM_VIEW_WINDOW_NAME = 'swarm-view'
HIDDEN_SESSION_NAME = 'claude-hidden'
getSwarmSocketName() = `claude-swarm-${process.pid}`  // изоляция сокета
```

---

## 5. Agent Communication & Synchronization

### Mailbox System

Агенты общаются через **mailbox** — файловую систему (`~/.claude/teams/{teamName}/`). Каждый teammate имеет почтовый ящик; сообщения пишутся через `writeToMailbox()`.

### Permission Sync (permissionSync.ts)

Полная система синхронизации permissions между workers и leader:

**Flow** (строки 1-18):
```
1. Worker → permission prompt
2. Worker → writePermissionRequest() → pending/{requestId}.json
3. Leader → readPendingPermissions() → показ в UI
4. User approves/denies через leader UI
5. Leader → resolvePermission() → resolved/{requestId}.json, unlink pending
6. Worker → pollForResponse() → продолжение
```

**Структура SwarmPermissionRequest** (строки 49-86):

| Поле | Назначение |
|------|------------|
| `id` | `perm-${Date.now()}-${random}` |
| `workerId` | CLAUDE_CODE_AGENT_ID |
| `workerName` | CLAUDE_CODE_AGENT_NAME |
| `workerColor` | Цвет в UI |
| `teamName` | Для роутинга |
| `toolName` | Bash, Edit и т.д. |
| `toolUseId` | Original ID из контекста |
| `input` | Сериализованный input |
| `permissionSuggestions` | "Always allow" варианты |
| `status` | pending/approved/rejected |

**File locking**: `lockfile.lock()` на уровне директории для атомарных записей.

**Mailbox-based path** (строки 643+): новый подход — permission requests через mailbox вместо файловой директории. `sendPermissionRequestViaMailbox()` и `sendPermissionResponseViaMailbox()`.

**Sandbox permissions** (строки 785+): отдельная подсистема для network access approval в sandbox runtime.

### Idle Notification (teammateInit.ts)

При завершении работы teammate:
1. `setMemberActive(teamName, agentName, false)` — mark idle в team file
2. `createIdleNotification()` → `writeToMailbox(leaderName, ...)` — уведомление лидера

### Leader Permission Bridge (leaderPermissionBridge.ts)

Мост для in-process teammates: они используют стандартный `ToolUseConfirm` диалог лидера вместо worker permission badge. Module-level singleton с `registerLeaderToolUseConfirmQueue()`.

---

## 6. Team Management (teamHelpers.ts)

### TeamFile — структура (строки 64-90)

```typescript
type TeamFile = {
  name: string
  description?: string
  createdAt: number
  leadAgentId: string
  leadSessionId?: string       // UUID лидера для discovery
  hiddenPaneIds?: string[]     // Скрытые pane в UI
  teamAllowedPaths?: TeamAllowedPath[]  // Пути без permission prompt
  members: Array<{
    agentId: string            // "name@team"
    name: string
    agentType?: string
    model?: string
    prompt?: string
    color?: string
    planModeRequired?: boolean
    joinedAt: number
    tmuxPaneId: string
    cwd: string
    worktreePath?: string
    sessionId?: string
    subscriptions: string[]
    backendType?: BackendType  // 'tmux' | 'iterm2' | 'in_process'
    isActive?: boolean
    mode?: PermissionMode
  }>
}
```

Файл хранится в `~/.claude/teams/{sanitized-team-name}/config.json`.

### Spawn Utils (spawnUtils.ts)

**buildInheritedCliFlags()** — пропагация настроек к tmux teammates:
- `--dangerously-skip-permissions` (НЕ если planModeRequired)
- `--model`, `--settings`, `--plugin-dir`
- `--teammate-mode`
- `--chrome` / `--no-chrome`

**buildInheritedEnvVars()** — env vars для tmux spawn:
- `CLAUDECODE=1`, `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`
- API provider: `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`
- Proxy: `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, SSL certs
- `CLAUDE_CODE_REMOTE`, `CLAUDE_CODE_REMOTE_MEMORY_DIR`

### Cleanup (строки 576-683)

При завершении сессии `cleanupSessionTeams()`:
1. Kill orphaned tmux/iTerm2 panes
2. Destroy git worktrees (`git worktree remove --force`)
3. Удалить team directory (`~/.claude/teams/{name}/`)
4. Удалить tasks directory (`~/.claude/tasks/{name}/`)

### Teammate Model (teammateModel.ts)

Дефолтная модель для teammates: **Claude Opus 4.6** (provider-aware: Bedrock/Vertex/Foundry получают свой model ID).

### Teammate Prompt Addendum (teammatePromptAddendum.ts)

Добавляется к system prompt каждого teammate:
```
You are running as an agent in a team.
Use the SendMessage tool with `to: "<name>"` to send messages.
Just writing a response in text is not visible to others on your team.
```

---

## 7. Agent Memory

Файл: `tools/AgentTool/agentMemory.ts`

### 3 scope памяти

| Scope | Путь | VCS | Назначение |
|-------|------|-----|------------|
| `user` | `<memoryBase>/agent-memory/<agentType>/` | Нет | Общие знания пользователя |
| `project` | `<cwd>/.claude/agent-memory/<agentType>/` | Да | Знания о проекте |
| `local` | `<cwd>/.claude/agent-memory-local/<agentType>/` | Нет | Локальные знания |

При `CLAUDE_CODE_REMOTE_MEMORY_DIR` — local scope перенаправляется на mount с project namespacing.

### Memory Injection

`loadAgentMemoryPrompt()` (строки 138-177): вызывает `buildMemoryPrompt()` из memdir. К стандартному промпту добавляется scope-specific note:
- `user` → "keep learnings general since they apply across all projects"
- `project` → "tailor your memories to this project"
- `local` → "tailor your memories to this project and machine"

### Memory Auto-Injection в agents

При определении агента с `memory` полем, автоматически инжектируются `Write`, `Edit`, `Read` в tools list (loadAgentsDir.ts, строки 456-466).

### Snapshot Sync (agentMemorySnapshot.ts)

Snapshots хранятся в `<cwd>/.claude/agent-memory-snapshots/<agentType>/snapshot.json`.

**Три действия**:
- `'none'` — snapshot отсутствует или уже синхронизирован
- `'initialize'` — нет локальной памяти → копировать из snapshot
- `'prompt-update'` — snapshot новее → предложить обновление

`checkAgentMemorySnapshot()` → `initializeFromSnapshot()` / `replaceFromSnapshot()` / `markSnapshotSynced()`.

---

# Часть 2: Analytics & Telemetry

## 8. Analytics Service — Архитектура

### Точка входа (services/analytics/index.ts)

**Принцип**: модуль index.ts **не имеет зависимостей** для избежания import cycles. Events ставятся в очередь до вызова `attachAnalyticsSink()`.

```typescript
// index.ts:80-84
const eventQueue: QueuedEvent[] = []
let sink: AnalyticsSink | null = null
```

**Sink Interface**:
```typescript
type AnalyticsSink = {
  logEvent: (eventName: string, metadata: LogEventMetadata) => void
  logEventAsync: (eventName: string, metadata: LogEventMetadata) => Promise<void>
}
```

**Типобезопасность**: metadata принимает только `boolean | number | undefined` — **строки запрещены** через тип `AnalyticsMetadata_I_VERIFIED_THIS_IS_NOT_CODE_OR_FILEPATHS = never`. Это предотвращает случайное логирование кода или файловых путей.

**PII-tagged поля**: ключи с префиксом `_PROTO_*` маршрутизируются в привилегированные BQ-колонки. `stripProtoFields()` удаляет их перед отправкой в general-access бэкенды (Datadog).

### Routing (sink.ts)

```typescript
// sink.ts:48-72
function logEventImpl(eventName, metadata):
  1. shouldSampleEvent(eventName) → sample_rate или 0 (drop)
  2. if shouldTrackDatadog() → trackDatadogEvent(name, stripProtoFields(metadata))
  3. logEventTo1P(name, metadata)  // 1P получает полный payload включая _PROTO_*
```

**Два бэкенда**:
- **Datadog** — general-access, _PROTO_ поля stripped
- **1P (First Party)** — привилегированный, полный payload

---

## 9. GrowthBook — Feature Flags & A/B Testing

Файл: `services/analytics/growthbook.ts` (~1156 строк)

### Архитектура

GrowthBook заменяет Statsig (идёт миграция). Используется для feature gates, dynamic configs и A/B экспериментов.

**Инициализация**:
```typescript
new GrowthBook({
  apiHost: 'https://api.anthropic.com/',
  clientKey: getGrowthBookClientKey(),
  attributes: getUserAttributes(),  // id, sessionId, platform, org, account, etc.
  remoteEval: true,               // сервер pre-evaluates
  cacheKeyAttributes: ['id', 'organizationUUID'],
})
```

### User Attributes для targeting (строки 32-47)

| Атрибут | Назначение |
|---------|------------|
| `id` / `deviceID` | UUID устройства |
| `sessionId` | UUID сессии |
| `platform` | `'win32'` / `'darwin'` / `'linux'` |
| `organizationUUID` | Организация |
| `accountUUID` | Аккаунт |
| `subscriptionType` | `'pro'` / `'max'` / `'enterprise'` / `'team'` |
| `apiBaseUrlHost` | Hostname прокси (для enterprise) |
| `email` | Email (для ant targeting) |
| `appVersion` | Версия Claude Code |

### Три уровня доступа к значениям

| Функция | Блокировка | Когда использовать |
|---------|------------|-------------------|
| `getFeatureValue_CACHED_MAY_BE_STALE()` | Нет | Startup-critical, sync контексты |
| `getFeatureValue_DEPRECATED()` | Да (await init) | Legacy, не рекомендуется |
| `checkGate_CACHED_OR_BLOCKING()` | Условно (если cache=true → нет) | Entitlement gates |

### Приоритет резолюции значений

1. **Env var overrides** (`CLAUDE_INTERNAL_FC_OVERRIDES`, ant-only)
2. **Config overrides** (`getGlobalConfig().growthBookOverrides`, ant-only, через /config)
3. **In-memory** (`remoteEvalFeatureValues` Map)
4. **Disk cache** (`~/.claude.json → cachedGrowthBookFeatures`)
5. **Statsig cache** (legacy fallback: `cachedStatsigGates`)
6. **Default value**

### Periodic Refresh

```typescript
// growthbook.ts:1013-1016
GROWTHBOOK_REFRESH_INTERVAL_MS =
  process.env.USER_TYPE !== 'ant'
    ? 6 * 60 * 60 * 1000   // 6 часов
    : 20 * 60 * 1000        // 20 минут (для ants)
```

### Experiment Exposure Logging

При доступе к feature с experiment data:
1. Если `experimentDataByFeature` содержит данные → `logExposureForFeature()`
2. Если нет (pre-init) → `pendingExposures.add(feature)`
3. После init → drain pending exposures
4. Дедупликация: `loggedExposures` Set, каждый feature логируется максимум один раз за сессию

### onGrowthBookRefresh Signal

Подписчики уведомляются при обновлении feature values. Используется системами, которые bake values при конструировании (1P event logger, model selection).

---

## 10. Event Logging — Что трекается

### Datadog Allowed Events (datadog.ts, строки 19-64)

64 разрешённых события. Категории:

| Категория | Примеры |
|-----------|---------|
| **Lifecycle** | `tengu_init`, `tengu_started`, `tengu_exit` |
| **API** | `tengu_api_error`, `tengu_api_success`, `tengu_query_error` |
| **Tools** | `tengu_tool_use_success`, `tengu_tool_use_error`, `tengu_tool_use_granted_in_prompt_*` |
| **Auth** | `tengu_oauth_error`, `tengu_oauth_success`, `tengu_oauth_token_refresh_*` |
| **Chrome** | `chrome_bridge_connection_*`, `chrome_bridge_tool_call_*` |
| **Memory** | `tengu_team_mem_sync_pull`, `tengu_team_mem_sync_push` |
| **Errors** | `tengu_uncaught_exception`, `tengu_unhandled_rejection` |
| **UI** | `tengu_cancel`, `tengu_compact_failed`, `tengu_flicker` |

### Event Sampling (firstPartyEventLogger.ts, строки 32-85)

GrowthBook config `tengu_event_sampling_config`:
```typescript
type EventSamplingConfig = {
  [eventName: string]: { sample_rate: number }  // 0-1
}
```

- Нет config → 100% rate
- `sample_rate = 0` → drop all
- `sample_rate = 1` → log all (без metadata)
- `0 < rate < 1` → `Math.random() < rate` ? rate : 0

При sampling `sample_rate` добавляется в metadata для корректной экстраполяции.

### Event Metadata (metadata.ts)

**EventMetadata** — обогащение каждого события (строки 472-496):

| Поле | Источник |
|------|---------|
| `model` | `getMainLoopModel()` |
| `sessionId` | `getSessionId()` |
| `userType` | `process.env.USER_TYPE` |
| `betas` | `getModelBetas()` |
| `envContext` | Platform, arch, CI, version, deployment... |
| `processMetrics` | RSS, heap, CPU%, uptime |
| `agentId` | AsyncLocalStorage или env (swarm) |
| `agentType` | `'teammate'` / `'subagent'` / `'standalone'` |
| `teamName` | Для swarm agents |
| `subscriptionType` | OAuth tier |
| `rh` | SHA256(repo remote URL), первые 16 chars |

### Privacy: Tool Name Sanitization

```typescript
// metadata.ts:70-77
function sanitizeToolNameForAnalytics(toolName):
  if (toolName.startsWith('mcp__')) → 'mcp_tool'  // MCP names = PII
  else → toolName                                   // built-in tools = safe
```

Исключения: `isAnalyticsToolDetailsLoggingEnabled()` разрешает полные MCP имена для:
- Cowork (entrypoint=local-agent)
- claude.ai-proxied connectors
- Серверы из официального MCP registry

---

## 11. Datadog Integration

Файл: `services/analytics/datadog.ts`

### Конфигурация

```typescript
DATADOG_LOGS_ENDPOINT = 'https://http-intake.logs.us5.datadoghq.com/api/v2/logs'
DATADOG_CLIENT_TOKEN = 'pubbbf48e6d78dae54bceaa4acf463299bf'
DEFAULT_FLUSH_INTERVAL_MS = 15000
MAX_BATCH_SIZE = 100
NETWORK_TIMEOUT_MS = 5000
```

### Batching

Events накапливаются в `logBatch[]`. Flush при:
- Batch size >= 100 → немедленный flush
- Timer 15 секунд → scheduled flush
- Shutdown → `shutdownDatadog()`

### Cardinality Reduction

1. **MCP tools**: `mcp__slack__read_channel` → `mcp` (строки 197-203)
2. **Model names**: canonical name, если неизвестная → `'other'` (строки 205-208)
3. **Version**: `2.0.53-dev.20251124.t173302.sha526cc6a` → `2.0.53-dev.20251124` (строки 211-217)
4. **User buckets**: SHA256(userId) % 30 — для alerting по количеству пользователей (строки 281-299)

### Условия отключения

- `NODE_ENV !== 'production'`
- 3P providers (Bedrock/Vertex/Foundry)
- Gate `tengu_log_datadog_events` выключен
- Sink killswitch `isSinkKilled('datadog')`

---

## 12. Cost Tracking

Файл: `cost-tracker.ts`

### Что трекается

| Метрика | Функция getter |
|---------|---------------|
| Стоимость USD | `getTotalCostUSD()` |
| Input tokens | `getTotalInputTokens()` |
| Output tokens | `getTotalOutputTokens()` |
| Cache read tokens | `getTotalCacheReadInputTokens()` |
| Cache creation tokens | `getTotalCacheCreationInputTokens()` |
| API duration | `getTotalAPIDuration()` / `getTotalAPIDurationWithoutRetries()` |
| Tool duration | `getTotalToolDuration()` |
| Lines changed | `getTotalLinesAdded()` / `getTotalLinesRemoved()` |
| Web search requests | `getTotalWebSearchRequests()` |
| Per-model usage | `getModelUsage()` / `getUsageForModel()` |

### StoredCostState

```typescript
type StoredCostState = {
  totalCostUSD: number
  totalAPIDuration: number
  totalAPIDurationWithoutRetries: number
  totalToolDuration: number
  totalLinesAdded: number
  totalLinesRemoved: number
  lastDuration: number | undefined
  modelUsage: { [modelName: string]: ModelUsage } | undefined
}
```

Состояние хранится в project config и привязано к `sessionId` — при resume восстанавливается.

### Расчёт стоимости

`calculateUSDCost()` из `utils/modelCost.ts` — cost per model per token type. Если модель неизвестна → `setHasUnknownModelCost(true)`.

---

## 13. Telemetry — Session Tracing

### Три системы трассировки

| Система | Файл | Для кого | Формат |
|---------|------|----------|--------|
| **OTel Session Tracing** | `sessionTracing.ts` | 3P (customers) | OpenTelemetry spans |
| **Perfetto Tracing** | `perfettoTracing.ts` | Ant-only | Chrome Trace Event JSON |
| **Beta Session Tracing** | `betaSessionTracing.ts` | Beta users | Enhanced OTel |

### Session Tracing (sessionTracing.ts)

Использует `AsyncLocalStorage` для хранения span context:

```typescript
const interactionContext = new AsyncLocalStorage<SpanContext | undefined>()
const toolContext = new AsyncLocalStorage<SpanContext | undefined>()
const activeSpans = new Map<string, WeakRef<SpanContext>>()
const strongSpans = new Map<string, SpanContext>()
```

**Типы spans**: `interaction`, `llm_request`, `tool`, `tool.blocked_on_user`, `tool.execution`, `hook`.

**Span TTL**: 30 минут — автоматическая очистка висящих spans.

### Perfetto Tracing (perfettoTracing.ts)

Ant-only трассировка в Chrome Trace Event format:

**Активация**: `CLAUDE_CODE_PERFETTO_TRACE=1` или `CLAUDE_CODE_PERFETTO_TRACE=<path>`

**Содержимое trace**:
- Agent hierarchy (parent-child в swarm)
- API requests с TTFT, TTLT, prompt length, cache stats, message ID, speculative flag
- Tool executions с name, duration, token usage
- User input waiting time

**Выход**: `~/.claude/traces/trace-<session-id>.json` → открыть в ui.perfetto.dev

### BigQuery Exporter (bigqueryExporter.ts)

`BigQueryMetricsExporter` — PushMetricExporter для OpenTelemetry SDK:

```typescript
endpoint = 'https://api.anthropic.com/api/claude_code/metrics'
timeout = 5000
```

Ant builds могут override через `ANT_CLAUDE_CODE_METRICS_ENDPOINT`.

### OTel Instrumentation (instrumentation.ts)

Полная OTel setup: `TracerProvider`, `MeterProvider`, `LoggerProvider` с поддержкой:
- Console exporter
- OTLP exporter (http/protobuf, http/json, grpc)
- Prometheus exporter (для metrics)
- BigQuery exporter (1P metrics)
- Proxy support (`HttpsProxyAgent`)
- mTLS config

### Events (events.ts)

OTel events через `logOTelEvent()`:
- Каждому event присваивается `event.sequence` (monotonic counter)
- `prompt.id` добавляется если есть
- Content redaction: `OTEL_LOG_USER_PROMPTS=1` для включения, иначе `<REDACTED>`

---

## 14. Sink & Killswitch

### Analytics Config (config.ts)

```typescript
function isAnalyticsDisabled(): boolean {
  return (
    process.env.NODE_ENV === 'test' ||
    isEnvTruthy(process.env.CLAUDE_CODE_USE_BEDROCK) ||
    isEnvTruthy(process.env.CLAUDE_CODE_USE_VERTEX) ||
    isEnvTruthy(process.env.CLAUDE_CODE_USE_FOUNDRY) ||
    isTelemetryDisabled()     // privacy level: no-telemetry / essential-traffic
  )
}
```

### Sink Killswitch (sinkKillswitch.ts)

GrowthBook dynamic config `tengu_frond_boric`:
```typescript
// Mangled name для защиты
type SinkName = 'datadog' | 'firstParty'

function isSinkKilled(sink: SinkName): boolean {
  const config = getDynamicConfig_CACHED_MAY_BE_STALE(
    'tengu_frond_boric', {}
  )
  return config?.[sink] === true
}
```

**Fail-open**: missing/malformed config → sink stays on.

**Важно**: НЕ вызывать из `is1PEventLoggingEnabled()` — growthbook.ts вызывает эту функцию, что создаст рекурсию.

### 1P Event Logger Pipeline

```
logEventTo1P(name, metadata)
  → logEventTo1PAsync(logger, name, metadata)
    → getEventMetadata()  // enrich
    → getCoreUserData()
    → logger.emit({ body: name, attributes: {...} })
      → BatchLogRecordProcessor
        → FirstPartyEventLoggingExporter
          → POST /api/event_logging/batch
```

**Batch config** (из GrowthBook `tengu_1p_event_batch_config`):
```typescript
type BatchConfig = {
  scheduledDelayMillis?: number    // default: 10000
  maxExportBatchSize?: number      // default: 200
  maxQueueSize?: number            // default: 8192
  skipAuth?: boolean
  maxAttempts?: number
  path?: string
  baseUrl?: string
}
```

**Reinit on config change**: `reinitialize1PEventLoggingIfConfigChanged()` подписан на `onGrowthBookRefresh`. При изменении batch config — flush старого provider, создание нового.

---

## Верификация: аудит точности

Каждое утверждение проверено против исходного кода. Проверено ~75 утверждений.

### Подтверждённые утверждения (выборка ключевых)

| Утверждение | Подтверждено | Файл:строка |
|-------------|--------------|-------------|
| `isCoordinatorMode()`: двойной гейт feature + env var | Да | `coordinatorMode.ts:36-41` |
| Coordinator system prompt ~370 строк, 6 секций | Да | `coordinatorMode.ts:116-368` |
| `INTERNAL_WORKER_TOOLS` = 4 элемента | Да | `coordinatorMode.ts:29-34` |
| Built-in agents: 6 типов, conditional на gates | Да | `builtInAgents.ts:22-72` |
| `ONE_SHOT_BUILTIN_AGENT_TYPES` = Explore, Plan | Да | `constants.ts:9-12` |
| `FORK_AGENT.maxTurns = 200`, `model: 'inherit'` | Да | `forkSubagent.ts:60-71` |
| Fork child защита: `isInForkChild()` через tag | Да | `forkSubagent.ts:78-89` |
| `AgentDefinition` = union из 3 типов | Да | `loadAgentsDir.ts:162-165` |
| BaseAgentDefinition: 18+ полей | Да | `loadAgentsDir.ts:106-133` |
| 3 AgentMemoryScope: user, project, local | Да | `agentMemory.ts:12-13, 52-65` |
| Snapshot sync: 3 actions (none, initialize, prompt-update) | Да | `agentMemorySnapshot.ts:98-144` |
| SwarmPermissionRequest: Zod schema, 15 полей | Да | `permissionSync.ts:49-86` |
| Permission dirs: `~/.claude/teams/{team}/permissions/` | Да | `permissionSync.ts:112-128` |
| `TeamFile.members`: 15 полей на member | Да | `teamHelpers.ts:64-90` |
| Cleanup: worktrees → team dir → tasks dir | Да | `teamHelpers.ts:641-683` |
| Default teammate model: Opus 4.6, provider-aware | Да | `teammateModel.ts:8-10` |
| `TEAMMATE_ENV_VARS`: 14 env vars forwarded | Да | `spawnUtils.ts:96-128` |
| `getSwarmSocketName()` = `claude-swarm-${PID}` | Да | `constants.ts:12-14` |
| InProcess spawn: independent AbortController | Да | `spawnInProcess.ts:121-122` |
| Analytics sink: queue until `attachAnalyticsSink()` | Да | `index.ts:80-84, 95-123` |
| `AnalyticsMetadata` type = `never` (safety marker) | Да | `index.ts:19` |
| `stripProtoFields()` removes `_PROTO_*` keys | Да | `index.ts:45-58` |
| Datadog: 64 allowed events, MAX_BATCH_SIZE=100 | Да | `datadog.ts:19-64, 16` |
| User buckets: SHA256 % 30 | Да | `datadog.ts:281-299` |
| GrowthBook: remoteEval=true, init timeout 5000ms | Да | `growthbook.ts:526-555` |
| Refresh interval: 6h external, 20min ant | Да | `growthbook.ts:1013-1016` |
| Sink killswitch config: `tengu_frond_boric` | Да | `sinkKillswitch.ts:4` |
| 1P batch defaults: delay=10s, batch=200, queue=8192 | Да | `firstPartyEventLogger.ts:300-302` |
| Perfetto: Chrome Trace Event format, `~/.claude/traces/` | Да | `perfettoTracing.ts:1-23` |
| Session tracing: 6 span types, TTL 30 min | Да | `sessionTracing.ts:49-56, 79` |
| BigQuery endpoint: `/api/claude_code/metrics` | Да | `bigqueryExporter.ts:47` |

### Вердикт

**~75 утверждений проверено, 0 неточностей обнаружено.** Документ отражает актуальное состояние кодовой базы.
