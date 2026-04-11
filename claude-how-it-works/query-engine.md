# Claude Code — Query Engine & Tool Orchestration

Анализ на основе утёкшего исходного кода Claude Code (`claude-leaked/src/`).

---

## Обзор: ключевые компоненты подсистемы

| # | Компонент | Файл | Ответственность |
|---|-----------|------|-----------------|
| 1 | **query()** | `query.ts` | Основной цикл запроса — стриминг API, tool dispatch, recovery, compaction |
| 2 | **QueryEngine** | `QueryEngine.ts` | Lifecycle класс для SDK/headless — владеет сессией, сообщениями, usage |
| 3 | **Tool** | `Tool.ts` | Абстракция инструмента — типы, интерфейсы, `buildTool()` |
| 4 | **toolOrchestration** | `services/tools/toolOrchestration.ts` | Партиционирование и выполнение tool-батчей (serial/concurrent) |
| 5 | **StreamingToolExecutor** | `services/tools/StreamingToolExecutor.ts` | Стриминговое выполнение тулов параллельно с API-ответом |
| 6 | **toolExecution** | `services/tools/toolExecution.ts` | Валидация, permissions, hooks, вызов `tool.call()` |
| 7 | **toolHooks** | `services/tools/toolHooks.ts` | Pre/Post tool hooks, permission resolution |
| 8 | **stopHooks** | `query/stopHooks.ts` | Хуки после завершения хода — extractMemories, autoDream, Stop hooks |
| 9 | **tokenBudget** | `query/tokenBudget.ts` | Auto-continue при +500K token budget |
| 10 | **config/deps** | `query/config.ts`, `query/deps.ts` | Конфигурация и injectable-зависимости цикла |

---

## 1. Главный цикл запроса — `query()`

### Архитектура: AsyncGenerator

`query()` — это **async generator** (`query.ts:219`). Не рекурсия, а `while(true)` цикл с мутируемым `State`.

```typescript
export async function* query(params: QueryParams): AsyncGenerator<
  StreamEvent | RequestStartEvent | Message | TombstoneMessage | ToolUseSummaryMessage,
  Terminal
>
```

Вызывающий код (`QueryEngine`, REPL) итерирует `for await (const message of query(...))` и получает поток сообщений.

### QueryParams (`query.ts:181-199`)

```typescript
export type QueryParams = {
  messages: Message[]
  systemPrompt: SystemPrompt
  userContext: { [k: string]: string }
  systemContext: { [k: string]: string }
  canUseTool: CanUseToolFn
  toolUseContext: ToolUseContext
  fallbackModel?: string
  querySource: QuerySource
  maxOutputTokensOverride?: number
  maxTurns?: number
  skipCacheWrite?: boolean
  taskBudget?: { total: number }
  deps?: QueryDeps
}
```

### State — мутируемое состояние цикла (`query.ts:204-217`)

```typescript
type State = {
  messages: Message[]
  toolUseContext: ToolUseContext
  autoCompactTracking: AutoCompactTrackingState | undefined
  maxOutputTokensRecoveryCount: number
  hasAttemptedReactiveCompact: boolean
  maxOutputTokensOverride: number | undefined
  pendingToolUseSummary: Promise<ToolUseSummaryMessage | null> | undefined
  stopHookActive: boolean | undefined
  turnCount: number
  transition: Continue | undefined  // Причина предыдущего continue
}
```

Каждое `continue` в цикле создаёт новый `State` с `transition.reason`:
- `'next_turn'` — стандартный переход после tool results
- `'reactive_compact_retry'` — retry после reactive compact
- `'collapse_drain_retry'` — retry после context collapse drain
- `'max_output_tokens_recovery'` — retry при max_output_tokens
- `'max_output_tokens_escalate'` — однократная эскалация до 64K
- `'stop_hook_blocking'` — retry после blocking stop hook
- `'token_budget_continuation'` — auto-continue по token budget

### Полный flow одной итерации

```
1. Деструктуризация State
2. Skill discovery prefetch (фоновый)
3. yield { type: 'stream_request_start' }
4. Query chain tracking: chainId + depth++
5. Tool result budget (applyToolResultBudget)
6. Snip compact (если HISTORY_SNIP)
7. Microcompact (time-based / cached MC)
8. Context collapse (если CONTEXT_COLLAPSE)
9. Auto-compact (если threshold достигнут)
10. Blocking limit check (если auto-compact OFF)
11. ── API CALL ── (стриминг через deps.callModel)
    - Стриминг assistant сообщений
    - Backfill tool_use inputs
    - Withholding recoverable errors (413, max_output_tokens)
    - StreamingToolExecutor: addTool() для каждого tool_use блока
    - getCompletedResults() между стриминг-событиями
12. Post-sampling hooks (fire-and-forget)
13. Abort check #1 (aborted during streaming)
14. Yield pending tool use summary (от предыдущего хода)
15. Если needsFollowUp = false (нет tool_use блоков):
    - Recovery: 413, max_output_tokens, media errors
    - Stop hooks (handleStopHooks)
    - Token budget check
    - return { reason: 'completed' }
16. Если needsFollowUp = true (есть tool_use блоки):
    - Выполнение тулов (streamingToolExecutor.getRemainingResults() или runTools())
    - Yield tool results
    - Tool use summary generation (фоновая)
    - Abort check #2 (aborted during tools)
    - Hook prevention check
    - Attachment messages (queued commands, memory prefetch, skill discovery)
    - Refresh tools (MCP hot-reload)
    - maxTurns check
    - state = next → continue
```

### API Call — стриминг (`query.ts:659-863`)

Вызов модели:
```typescript
for await (const message of deps.callModel({
  messages: prependUserContext(messagesForQuery, userContext),
  systemPrompt: fullSystemPrompt,
  thinkingConfig, tools, signal,
  options: { model, toolChoice, querySource, taskBudget, ... }
})) {
```

`deps.callModel` — это `queryModelWithStreaming` из `services/api/claude.js`. Каждое сообщение из стрима — либо `StreamEvent`, либо `AssistantMessage`.

**Streaming fallback** (`query.ts:712-741`): если срабатывает fallback на другую модель:
1. Yield tombstones для orphan-сообщений
2. Очистка assistantMessages, toolResults, toolUseBlocks
3. Пересоздание StreamingToolExecutor
4. Новое сообщение продолжает streaming loop

**Model fallback** (`query.ts:893-953`): при `FallbackTriggeredError`:
1. `currentModel = fallbackModel`
2. `attemptWithFallback = true`
3. Strip thinking signature blocks (ant-only)
4. System message "Switched to {model}"
5. `continue` во внутреннем `while(attemptWithFallback)` цикле

### Withholding механизм (`query.ts:799-825`)

Recoverable ошибки **не yield-ятся** сразу:
```typescript
let withheld = false
if (contextCollapse?.isWithheldPromptTooLong(message, ...)) withheld = true
if (reactiveCompact?.isWithheldPromptTooLong(message)) withheld = true
if (reactiveCompact?.isWithheldMediaSizeError(message)) withheld = true
if (isWithheldMaxOutputTokens(message)) withheld = true
if (!withheld) yield yieldMessage
```

Withheld-сообщения обрабатываются ПОСЛЕ стриминга:
- 413 → context collapse drain → reactive compact → surface error
- max_output_tokens → escalate to 64K → recovery с meta-сообщением → surface

### max_output_tokens recovery (`query.ts:1188-1256`)

**Эскалация** (однократно): если `maxOutputTokensOverride === undefined` и `CLAUDE_CODE_MAX_OUTPUT_TOKENS` не задан → retry с `ESCALATED_MAX_TOKENS` (64K).

**Multi-turn recovery**: до `MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3` попыток (`query.ts:164`):
```
"Output token limit hit. Resume directly — no apology, no recap of what you were doing.
Pick up mid-thought if that is where the cut happened. Break remaining work into smaller pieces."
```

---

## 2. QueryEngine — lifecycle класс

### Назначение (`QueryEngine.ts:176-183`)

```
One QueryEngine per conversation. Each submitMessage() call starts a new
turn within the same conversation. State (messages, file cache, usage, etc.)
persists across turns.
```

`QueryEngine` — обёртка над `query()` для SDK/headless. Одна инстанция на разговор.

### Ключевые поля

| Поле | Тип | Назначение |
|------|-----|------------|
| `mutableMessages` | `Message[]` | Полная история разговора |
| `abortController` | `AbortController` | Отмена текущего хода |
| `permissionDenials` | `SDKPermissionDenial[]` | Трекинг отказов для SDK reporting |
| `totalUsage` | `NonNullableUsage` | Кумулятивный usage |
| `readFileState` | `FileStateCache` | Кеш прочитанных файлов |
| `discoveredSkillNames` | `Set<string>` | Turn-scoped трекинг skill discovery |
| `loadedNestedMemoryPaths` | `Set<string>` | Дедупликация nested memory |

### submitMessage() flow (`QueryEngine.ts:209-1100+`)

```
1. setCwd(cwd)
2. fetchSystemPromptParts() — system prompt + contexts
3. processUserInput() — обработка /slash commands, attachments
4. mutableMessages.push(...messagesFromUserInput)
5. recordTranscript() — persist до API call
6. Structured output enforcement registration
7. for await (const message of query({ ... })):
   - Record transcript
   - Acknowledge initial user messages
   - Track usage (message_start/message_delta/message_stop)
   - Handle compact boundaries (splice old messages for GC)
   - Budget/max-turns/structured-output limit checks
   - yield SDKMessage events
8. Final result: success / error_during_execution / error_max_turns / error_max_budget_usd
```

### SDK-специфика

- `wrappedCanUseTool` (`QueryEngine.ts:244-271`) — обёртка для трекинга permission denials
- `replayUserMessages` — yield SDKUserMessageReplay для отображения в UI
- `includePartialMessages` — стриминг stream_events для SDK
- Compact boundary обработка: `mutableMessages.splice(0, boundaryIdx)` для GC (`QueryEngine.ts:926-929`)
- Budget check: `getTotalCost() >= maxBudgetUsd` после каждого сообщения (`QueryEngine.ts:972`)

### Результирующие типы

| Subtype | Условие |
|---------|---------|
| `success` | isResultSuccessful(result, lastStopReason) |
| `error_during_execution` | Последнее сообщение не является успешным |
| `error_max_turns` | `max_turns_reached` attachment |
| `error_max_budget_usd` | Cost >= maxBudgetUsd |
| `error_max_structured_output_retries` | StructuredOutput retries >= MAX_STRUCTURED_OUTPUT_RETRIES (5) |

---

## 3. Tool System — абстракция инструмента

### Тип Tool (`Tool.ts:362-695`)

Большой generic тип с 40+ методами. Ключевые:

**Обязательные:**

| Метод | Сигнатура | Назначение |
|-------|-----------|------------|
| `call()` | `(args, context, canUseTool, parentMessage, onProgress?) => Promise<ToolResult>` | Выполнение тула |
| `description()` | `(input, options) => Promise<string>` | Описание для промпта |
| `inputSchema` | `z.ZodType` | Zod-схема валидации входных данных |
| `checkPermissions()` | `(input, context) => Promise<PermissionResult>` | Tool-specific проверка прав |
| `prompt()` | `(options) => Promise<string>` | Промпт-секция для system prompt |
| `name` | `string` | Уникальное имя |
| `maxResultSizeChars` | `number` | Лимит на размер результата (Infinity для Read) |
| `renderToolUseMessage()` | UI-рендер вызова |
| `mapToolResultToToolResultBlockParam()` | Маппинг результата в API-формат |

**Опциональные:**

| Метод | Назначение |
|-------|------------|
| `aliases` | Альтернативные имена (backward compat) |
| `validateInput()` | Дополнительная валидация после Zod |
| `isConcurrencySafe()` | Может ли выполняться параллельно |
| `isReadOnly()` | Не модифицирует файловую систему |
| `isDestructive()` | Необратимые операции |
| `interruptBehavior()` | `'cancel'` или `'block'` при interrupt |
| `shouldDefer` | Deferred loading через ToolSearch |
| `alwaysLoad` | Никогда не defer-ить |
| `backfillObservableInput()` | Добавление legacy/derived полей |
| `preparePermissionMatcher()` | Matcher для hook `if` conditions |
| `isSearchOrReadCommand()` | Определение операции для UI collapse |

### buildTool() — фабрика (`Tool.ts:783-792`)

```typescript
export function buildTool<D extends AnyToolDef>(def: D): BuiltTool<D> {
  return {
    ...TOOL_DEFAULTS,
    userFacingName: () => def.name,
    ...def,
  } as BuiltTool<D>
}
```

**Дефолты (fail-closed):**
- `isEnabled` → `true`
- `isConcurrencySafe` → `false` (безопасно по умолчанию)
- `isReadOnly` → `false` (предполагает запись)
- `isDestructive` → `false`
- `checkPermissions` → `{ behavior: 'allow' }` (делегирует общей системе)
- `toAutoClassifierInput` → `''` (skip classifier)

### ToolUseContext (`Tool.ts:158-300`)

Огромный контекстный объект, передаваемый во все tool-вызовы:

| Группа | Ключевые поля |
|--------|---------------|
| **Options** | `tools`, `commands`, `mainLoopModel`, `thinkingConfig`, `mcpClients`, `agentDefinitions` |
| **Control** | `abortController`, `messages`, `queryTracking` |
| **State** | `readFileState`, `contentReplacementState`, `toolDecisions` |
| **Callbacks** | `getAppState()`, `setAppState()`, `setToolJSX()`, `setInProgressToolUseIDs()`, `setResponseLength()` |
| **Identity** | `agentId` (только для subagents), `agentType` |
| **Limits** | `fileReadingLimits`, `globLimits`, `maxBudgetUsd` |

### ToolResult (`Tool.ts:321-336`)

```typescript
export type ToolResult<T> = {
  data: T
  newMessages?: (UserMessage | AssistantMessage | AttachmentMessage | SystemMessage)[]
  contextModifier?: (context: ToolUseContext) => ToolUseContext
  mcpMeta?: { _meta?: Record<string, unknown>; structuredContent?: Record<string, unknown> }
}
```

`contextModifier` — позволяет тулу модифицировать контекст для следующих тулов. Только для non-concurrent тулов.

### findToolByName (`Tool.ts:358-360`)

Поиск по `name` ИЛИ `aliases`:
```typescript
export function findToolByName(tools: Tools, name: string): Tool | undefined {
  return tools.find(t => toolMatchesName(t, name))
}
```

---

## 4. Tool Execution — полный pipeline

### runToolUse() (`toolExecution.ts:337-490`)

Точка входа для выполнения одного tool_use блока:

```
1. findToolByName() — поиск в tools, fallback на alias в getAllBaseTools()
2. Abort check — если уже aborted, yield CANCEL_MESSAGE
3. streamedCheckPermissionsAndCallTool() — основной pipeline
```

### checkPermissionsAndCallTool() (`toolExecution.ts:599-...`)

Полный pipeline одного тула:

```
1. Zod validation (inputSchema.safeParse)
   - Ошибка? → buildSchemaNotSentHint() для deferred tools
2. validateInput() — tool-specific validation
3. Speculative Bash classifier (параллельно с hooks)
4. Backfill observable input (clone для hooks, оригинал для call())
5. PreToolUse hooks (runPreToolUseHooks)
   - hookPermissionResult, updatedInput, preventContinuation
6. Hook timing summary (ant-only, > 500ms)
7. OTel: startToolSpan, startToolBlockedOnUserSpan
8. Permission resolution (resolveHookPermissionDecision)
   - Hook allow → checkRuleBasedPermissions (deny/ask правила НЕ обходятся!)
   - Hook deny → сразу deny
   - Hook ask / нет → canUseTool() (диалог с пользователем)
9. Denied? → error tool_result, PostToolUseFailure hooks
10. Allowed:
    - endToolBlockedOnUserSpan, startToolExecutionSpan
    - startSessionActivity()
    - tool.call(processedInput, toolUseContext, canUseTool, ...)
    - mapToolResult → processToolResultBlock
    - stopSessionActivity()
    - endToolExecutionSpan
11. PostToolUse hooks (runPostToolUseHooks)
    - blockingError → hook_blocking_error attachment
    - preventContinuation → hook_stopped_continuation attachment
    - additionalContexts → hook_additional_context attachment
    - updatedMCPToolOutput → replace output for MCP tools
12. Return MessageUpdateLazy[]
```

### Permission Resolution — resolveHookPermissionDecision() (`toolHooks.ts:332-433`)

Критическая логика: **hook `allow` НЕ обходит deny/ask правила**:

```typescript
if (hookPermissionResult?.behavior === 'allow') {
  // 1. Если tool requiresUserInteraction И hook не дал updatedInput → canUseTool
  // 2. Если requireCanUseTool → canUseTool
  // 3. checkRuleBasedPermissions():
  //    - null → hook разрешён
  //    - deny → deny (правило побеждает hook)
  //    - ask → canUseTool (диалог несмотря на hook)
}
```

Это защита от случая, когда hook одобряет опасный инструмент, но пользователь настроил deny-правило в settings.json.

---

## 5. Tool Orchestration — параллельное выполнение

### Два режима выполнения

1. **Legacy** (`toolOrchestration.ts:runTools`) — post-streaming batch execution
2. **Streaming** (`StreamingToolExecutor`) — execution during API streaming

Выбор:
```typescript
const useStreamingToolExecution = config.gates.streamingToolExecution  // query.ts:561
```

### Legacy: partitionToolCalls() (`toolOrchestration.ts:91-116`)

Разбивает tool_use блоки на батчи:

```
[Read, Read, Edit, Grep, Grep] →
  Batch 1: { isConcurrencySafe: true,  blocks: [Read, Read] }
  Batch 2: { isConcurrencySafe: false, blocks: [Edit] }
  Batch 3: { isConcurrencySafe: true,  blocks: [Grep, Grep] }
```

**Правило**: `isConcurrencySafe` определяется через `tool.isConcurrencySafe(parsedInput)`. По умолчанию `false` (buildTool default).

- **Concurrent batch**: `runToolsConcurrently()` через `all()` utility с лимитом `MAX_TOOL_USE_CONCURRENCY` (env `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY`, default 10, строка 10)
- **Serial batch**: `runToolsSerially()` — по одному, с context modifier chain

### Streaming: StreamingToolExecutor (`StreamingToolExecutor.ts:40-530`)

Класс, который начинает выполнение тулов **ДО завершения стриминга API**.

#### Состояния TrackedTool

```
queued → executing → completed → yielded
```

#### Concurrency control (`canExecuteTool`, строка 129-135)

```typescript
private canExecuteTool(isConcurrencySafe: boolean): boolean {
  const executingTools = this.tools.filter(t => t.status === 'executing')
  return (
    executingTools.length === 0 ||
    (isConcurrencySafe && executingTools.every(t => t.isConcurrencySafe))
  )
}
```

Правило: non-concurrent тул запускается только когда ничего не выполняется. Concurrent тулы могут работать параллельно с другими concurrent тулами.

#### Sibling Abort (`StreamingToolExecutor.ts:46-62`)

```typescript
private siblingAbortController: AbortController  // Child of toolUseContext.abortController
```

Только Bash-ошибки каскадируют (`StreamingToolExecutor.ts:359`):
```typescript
if (tool.block.name === BASH_TOOL_NAME) {
  this.hasErrored = true
  this.siblingAbortController.abort('sibling_error')
}
```

Read/WebFetch/etc. ошибки НЕ отменяют siblings — они независимы.

#### Interaction flow

```
API streaming:
  → addTool(block, assistantMessage)    // Добавить в очередь
  → processQueue()                       // Запустить если можно
  → getCompletedResults()                // Yield готовые результаты (non-blocking)

Post-streaming:
  → getRemainingResults()                // Ждать все оставшиеся (async)
  → yield results in order
```

#### Discard (`StreamingToolExecutor.ts:69-71`)

При streaming fallback: `discard()` → все queued/executing тулы получают synthetic error `'streaming_fallback'`.

---

## 6. Stop Hooks — что происходит после каждого хода

### handleStopHooks() (`stopHooks.ts:65-473`)

Вызывается из `query.ts:1267` когда `needsFollowUp = false` (модель завершила ход без tool_use).

### Порядок выполнения

```
1. saveCacheSafeParams() — сохранить для forked agents (только main thread)
2. Template job classification (если TEMPLATES + CLAUDE_JOB_DIR)
3. Если НЕ bare mode:
   a. executePromptSuggestion() — fire-and-forget
   b. executeExtractMemories() — fire-and-forget (только main thread + EXTRACT_MEMORIES)
   c. executeAutoDream() — fire-and-forget (только main thread)
4. Computer Use cleanup (если CHICAGO_MCP + main thread)
5. executeStopHooks() — пользовательские stop hooks
   - Blocking errors → вставить как meta user messages → continue
   - preventContinuation → return true
6. Teammate hooks (если isTeammate()):
   a. executeTaskCompletedHooks() — для in_progress задач
   b. executeTeammateIdleHooks()
7. Stop hook summary message (если hookCount > 0)
```

### Blocking errors

Stop hooks могут вернуть `blockingError` — это вставляется как `isMeta: true` user message и цикл **продолжается** (`query.ts:1282-1306`):

```typescript
if (stopHookResult.blockingErrors.length > 0) {
  state = {
    messages: [...messagesForQuery, ...assistantMessages, ...stopHookResult.blockingErrors],
    stopHookActive: true,
    transition: { reason: 'stop_hook_blocking' },
    ...
  }
  continue
}
```

### extractMemories trigger (`stopHooks.ts:142-153`)

```typescript
if (feature('EXTRACT_MEMORIES') && !toolUseContext.agentId && isExtractModeActive()) {
  void extractMemoriesModule!.executeExtractMemories(stopHookContext, ...)
}
```

- Fire-and-forget (`void`)
- Только main thread (`!toolUseContext.agentId`)
- Только если extract mode active (`isExtractModeActive()`)

### autoDream trigger (`stopHooks.ts:154-156`)

```typescript
if (!toolUseContext.agentId) {
  void executeAutoDream(stopHookContext, toolUseContext.appendSystemMessage)
}
```

---

## 7. Token Budget — auto-continue система

### Механика (`tokenBudget.ts:1-93`)

Token budget позволяет модели продолжать работу сверх одного хода, если суммарный output не достиг порога.

#### BudgetTracker

```typescript
export type BudgetTracker = {
  continuationCount: number
  lastDeltaTokens: number
  lastGlobalTurnTokens: number
  startedAt: number
}
```

#### checkTokenBudget() (`tokenBudget.ts:45-93`)

```
Входы: tracker, agentId, budget, globalTurnTokens

1. Если agentId или budget <= 0 → stop (subagents не используют budget)
2. pct = turnTokens / budget * 100
3. isDiminishing = continuationCount >= 3 AND delta < 500 AND lastDelta < 500
4. Если НЕ diminishing И turnTokens < budget * 0.9:
   → continue с nudgeMessage
5. Если diminishing ИЛИ continuationCount > 0:
   → stop с completionEvent (для аналитики)
6. Иначе → stop без события
```

#### Ключевые константы

| Константа | Значение | Назначение |
|-----------|----------|------------|
| `COMPLETION_THRESHOLD` | 0.9 (90%) | Порог завершения — прекратить при 90%+ |
| `DIMINISHING_THRESHOLD` | 500 токенов | Если delta < 500 дважды подряд — early stop |

#### Интеграция в query loop (`query.ts:1308-1355`)

```typescript
if (feature('TOKEN_BUDGET')) {
  const decision = checkTokenBudget(budgetTracker!, agentId, budget, turnTokens)
  if (decision.action === 'continue') {
    incrementBudgetContinuationCount()
    state = {
      messages: [...messagesForQuery, ...assistantMessages,
        createUserMessage({ content: decision.nudgeMessage, isMeta: true })],
      transition: { reason: 'token_budget_continuation' },
      ...
    }
    continue
  }
}
```

---

## 8. Стриминг — как ответы передаются

### Поток данных

```
queryModelWithStreaming (claude.ts)
  ↓ AsyncGenerator<StreamEvent | AssistantMessage>
query() (query.ts)
  ↓ yield StreamEvent | Message | ToolUseSummaryMessage
QueryEngine.submitMessage() (QueryEngine.ts)
  ↓ yield SDKMessage
SDK consumer / REPL
```

### Типы yield-ов из query()

| Тип | Когда |
|-----|-------|
| `stream_request_start` | Начало каждой итерации цикла |
| `StreamEvent` | Из callModel (content_block_start/delta/stop, message_start/delta/stop) |
| `AssistantMessage` | Завершённое assistant-сообщение из callModel |
| `UserMessage` | Tool results, interruption messages |
| `AttachmentMessage` | Hooks, memory, queued commands, file changes |
| `TombstoneMessage` | Удаление orphan-сообщений при fallback |
| `ToolUseSummaryMessage` | Сводка tool вызовов (Haiku, async) |
| `SystemMessage` | Ошибки, warnings, compact boundaries |

### Backfill Observable Input (`query.ts:742-787`)

Перед yield assistant-сообщения, tool_use inputs **клонируются** для SDK/transcript:
```typescript
const inputCopy = { ...originalInput }
tool.backfillObservableInput(inputCopy)
// Только если backfill ДОБАВИЛ поля (не перезаписал)
if (addedFields) {
  clonedContent[i] = { ...block, input: inputCopy }
}
```

Оригинал **не мутируется** — он идёт в assistantMessages для prompt cache.

### Tool Progress через Stream

`streamedCheckPermissionsAndCallTool()` (`toolExecution.ts:492-570`) оборачивает async `checkPermissionsAndCallTool` в `Stream`:

```typescript
const stream = new Stream<MessageUpdateLazy>()
checkPermissionsAndCallTool(/* ..., */ progress => {
  stream.enqueue({ message: createProgressMessage(progress) })
})
  .then(results => { for (const r of results) stream.enqueue(r) })
  .finally(() => stream.done())
return stream
```

---

## 9. Обработка ошибок — recovery, cancellation, abort

### Error categories

| Ошибка | Обработка | Файл:строка |
|--------|-----------|-------------|
| **prompt_too_long (413)** | Withhold → collapse drain → reactive compact → surface | `query.ts:1070-1183` |
| **max_output_tokens** | Withhold → escalate 64K → multi-turn recovery (3x) → surface | `query.ts:1188-1256` |
| **Media size error** | Withhold → reactive compact strip-retry → surface | `query.ts:1082-1084` |
| **FallbackTriggeredError** | Switch model → retry | `query.ts:893-953` |
| **ImageSizeError/ResizeError** | Friendly error message | `query.ts:970-978` |
| **Unknown error** | Log + yield synthetic error messages | `query.ts:955-997` |
| **Tool validation error** | InputValidationError tool_result | `toolExecution.ts:615-680` |
| **Tool unknown** | "No such tool available" tool_result | `toolExecution.ts:369-411` |
| **Tool permission denied** | Error tool_result + PostToolUseFailure hooks | `toolExecution.ts:995-1050` |
| **MCP auth error** | McpAuthError → elicitation flow | `toolExecution.ts` |

### Abort/Cancellation

**AbortController hierarchy:**

```
toolUseContext.abortController (query-level)
  └── siblingAbortController (StreamingToolExecutor-level)
       └── toolAbortController (per-tool, StreamingToolExecutor.ts:301)
```

**Abort check points:**
1. `query.ts:1015` — после стриминга (aborted during streaming)
2. `query.ts:1485` — после tool execution (aborted during tools)
3. `toolExecution.ts:415` — перед запуском тула
4. `StreamingToolExecutor.ts:278` — перед каждым тулом (getAbortReason)
5. `StreamingToolExecutor.ts:335` — во время execution loop

**Interrupt behavior** (`Tool.ts:413-416`):
```typescript
interruptBehavior?(): 'cancel' | 'block'
```
- `'cancel'` — остановить тул при user interrupt
- `'block'` — продолжить выполнение, новое сообщение ждёт (default)

**Synthetic error messages** при abort (`StreamingToolExecutor.ts:153-205`):
- `'sibling_error'` → "Cancelled: parallel tool call {desc} errored"
- `'user_interrupted'` → REJECT_MESSAGE
- `'streaming_fallback'` → "Streaming fallback - tool execution discarded"

### yieldMissingToolResultBlocks (`query.ts:123-149`)

Защита от dangling tool_use без matching tool_result:
```typescript
function* yieldMissingToolResultBlocks(assistantMessages, errorMessage) {
  for (const assistantMessage of assistantMessages) {
    for (const toolUse of toolUseBlocks) {
      yield createUserMessage({
        content: [{ type: 'tool_result', content: errorMessage, is_error: true, tool_use_id: toolUse.id }],
      })
    }
  }
}
```

---

## 10. Config & Dependencies

### QueryConfig (`config.ts:15-27`)

Immutable snapshot при входе в query():

```typescript
export type QueryConfig = {
  sessionId: SessionId
  gates: {
    streamingToolExecution: boolean  // Statsig 'tengu_streaming_tool_execution2'
    emitToolUseSummaries: boolean    // CLAUDE_CODE_EMIT_TOOL_USE_SUMMARIES
    isAnt: boolean                   // USER_TYPE === 'ant'
    fastModeEnabled: boolean         // !CLAUDE_CODE_DISABLE_FAST_MODE
  }
}
```

Намеренно **не включает** `feature()` gates — те остаются inline для tree-shaking.

### QueryDeps (`deps.ts:21-31`)

Injectable I/O зависимости для тестирования:

```typescript
export type QueryDeps = {
  callModel: typeof queryModelWithStreaming
  microcompact: typeof microcompactMessages
  autocompact: typeof autoCompactIfNeeded
  uuid: () => string
}
```

Production: `productionDeps()` — возвращает реальные implementations.

---

## Ключевые константы

| Константа | Значение | Файл | Назначение |
|-----------|----------|------|------------|
| `MAX_OUTPUT_TOKENS_RECOVERY_LIMIT` | 3 | `query.ts:164` | Max retries при max_output_tokens |
| `ESCALATED_MAX_TOKENS` | 64,000 | `utils/context.ts` | Однократная эскалация output tokens |
| `MAX_TOOL_USE_CONCURRENCY` | 10 (env override) | `toolOrchestration.ts:10` | Max параллельных tool вызовов |
| `COMPLETION_THRESHOLD` | 0.9 | `tokenBudget.ts:3` | Token budget — stop при 90% |
| `DIMINISHING_THRESHOLD` | 500 | `tokenBudget.ts:4` | Token budget — early stop delta |
| `HOOK_TIMING_DISPLAY_THRESHOLD_MS` | 500 | `toolExecution.ts:134` | Порог отображения hook timing |
| `SLOW_PHASE_LOG_THRESHOLD_MS` | 2,000 | `toolExecution.ts:137` | Warning для медленных hooks/permissions |
| `MAX_STRUCTURED_OUTPUT_RETRIES` | 5 (env) | `QueryEngine.ts:1012` | Max retries structured output |

---

## Как всё работает вместе: полный путь запроса

### Пользователь вводит сообщение

```
User input
  ↓
QueryEngine.submitMessage()
  ↓ processUserInput() — парсинг, /slash commands, attachments
  ↓ recordTranscript() — persist
  ↓
query({ messages, systemPrompt, userContext, systemContext, ... })
  ↓
queryLoop() — while(true)
```

### Итерация 1: API call

```
queryLoop iteration 1:
  applyToolResultBudget() → snip → microcompact → collapse → autocompact
  ↓
  callModel() streaming:
    → StreamEvent yields (content_block_start, delta, stop)
    → AssistantMessage с tool_use блоками
    → StreamingToolExecutor.addTool() для каждого блока
    → getCompletedResults() между стримами
  ↓
  needsFollowUp = true (есть tool_use)
  ↓
  streamingToolExecutor.getRemainingResults():
    → Ожидание всех тулов
    → Permission checks → tool.call() → PostToolUse hooks
    → yield tool results
  ↓
  getAttachmentMessages() — queued commands, memory, skills
  ↓
  state = { messages: [...old, ...assistant, ...toolResults], turnCount: 2 }
  continue
```

### Итерация 2: завершение

```
queryLoop iteration 2:
  callModel() streaming:
    → AssistantMessage без tool_use (только text)
  ↓
  needsFollowUp = false
  ↓
  handleStopHooks():
    → saveCacheSafeParams (для forked agents)
    → extractMemories (fire-and-forget)
    → autoDream (fire-and-forget)
    → executeStopHooks() → нет blocking errors
  ↓
  checkTokenBudget() → stop (budget не задан или 90%+)
  ↓
  return { reason: 'completed' }
```

### QueryEngine финализация

```
query() returned
  ↓
QueryEngine: isResultSuccessful() check
  ↓
yield { type: 'result', subtype: 'success', usage, cost, ... }
```

---

## Верификация: аудит точности документа

### Подтверждённые утверждения

| Утверждение | Подтверждено | Источник |
|-------------|--------------|----------|
| `query()` — async generator с `while(true)` | Да | `query.ts:219,307` |
| State содержит 10 полей | Да | `query.ts:204-217` |
| `MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3` | Да | `query.ts:164` |
| StreamingToolExecutor: concurrency = all-safe OR exclusive | Да | `StreamingToolExecutor.ts:129-135` |
| Только Bash-ошибки каскадируют siblings | Да | `StreamingToolExecutor.ts:359` |
| `partitionToolCalls()` — batch consecutive concurrency-safe tools | Да | `toolOrchestration.ts:91-116` |
| Default concurrency limit = 10 | Да | `toolOrchestration.ts:10` |
| Hook `allow` НЕ обходит deny/ask правила | Да | `toolHooks.ts:347-406` |
| Zod validation перед permission check | Да | `toolExecution.ts:615-616` |
| `buildTool()` defaults: `isConcurrencySafe→false`, `isReadOnly→false` | Да | `Tool.ts:757-769` |
| extractMemories: fire-and-forget, main thread only | Да | `stopHooks.ts:149-153` |
| autoDream: fire-and-forget, main thread only | Да | `stopHooks.ts:154-156` |
| `COMPLETION_THRESHOLD = 0.9`, `DIMINISHING_THRESHOLD = 500` | Да | `tokenBudget.ts:3-4` |
| Diminishing returns: `continuationCount >= 3` AND `delta < 500` дважды | Да | `tokenBudget.ts:59-62` |
| QueryEngine: один инстанс на разговор | Да | `QueryEngine.ts:176-183` |
| QueryDeps: 4 зависимости (callModel, microcompact, autocompact, uuid) | Да | `deps.ts:21-31` |
| Withholding: 4 типа (collapse PTL, reactive PTL, media, max_output_tokens) | Да | `query.ts:800-822` |
| Streaming fallback: tombstones + reset + fresh executor | Да | `query.ts:712-741` |
| Model fallback: switch model + stripSignatureBlocks (ant-only) | Да | `query.ts:893-953` |
| `HOOK_TIMING_DISPLAY_THRESHOLD_MS = 500` | Да | `toolExecution.ts:134` |
| backfillObservableInput: clone, add-only (no overwrites yield) | Да | `query.ts:771-774` |
| saveCacheSafeParams: only for `repl_main_thread` or `sdk` | Да | `stopHooks.ts:96-98` |
| Token budget: subagents excluded (`if (agentId) → stop`) | Да | `tokenBudget.ts:51` |
| maxTurns check after tools | Да | `query.ts:1705-1712` |

### Найденные нюансы при верификации

| Тема | Детали |
|------|--------|
| `transition` type | Тип `Continue` импортируется из `query/transitions.ts`, но файл отсутствует в leaked source — вероятно, простой union type с `reason` строками |
| `ESCALATED_MAX_TOKENS` | Импортируется из `utils/context.ts` (не из query/), значение = 64000 (определено отдельно) |
| `processQueue()` | Вызывается через `void this.processQueue()` (fire-and-forget) при addTool, и через `await this.processQueue()` в getRemainingResults |
| Tool fallback по alias | `toolExecution.ts:350-355` — поиск в `getAllBaseTools()` по alias для deprecated инструментов |
| `buildSchemaNotSentHint()` | `toolExecution.ts:578-597` — специальная подсказка для deferred tools с ToolSearch |
