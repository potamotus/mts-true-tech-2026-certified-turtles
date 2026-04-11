# Claude Code -- API Client, State Management и Sessions

Анализ на основе утёкшего исходного кода Claude Code (`claude-leaked/src/`).

---

## Часть 1: API Client

### 1. Обзор архитектуры API

API-слой Claude Code расположен в `src/services/api/` и состоит из 20 файлов:

| Файл | Назначение | Строк |
|------|-----------|-------|
| `claude.ts` | Основной query-генератор, сборка запросов к Messages API | ~1500 |
| `client.ts` | Фабрика Anthropic SDK клиентов (Direct/Bedrock/Vertex/Foundry) | 390 |
| `withRetry.ts` | Retry logic с exponential backoff и 529 fallback | 823 |
| `errors.ts` | Типизация ошибок, error messages, rate limit parsing | ~1000 |
| `errorUtils.ts` | SSL/connection error detection, HTML sanitization | 261 |
| `logging.ts` | Telemetry: logAPIQuery, logAPISuccess, logAPIError | 789 |
| `usage.ts` | Fetch rate limit utilization (OAuth) | 63 |
| `bootstrap.ts` | Bootstrap API: client_data + model options | 141 |
| `promptCacheBreakDetection.ts` | 2-фазная детекция сбросов prompt cache | 728 |
| `sessionIngress.ts` | Persistent session transcript на сервере Anthropic | 515 |
| `filesApi.ts` | Files API: download/upload файлов (BYOC mode) | 749 |
| `adminRequests.ts` | Admin requests: limit increase, seat upgrade | 120 |
| `emptyUsage.ts` | Константа пустого usage-объекта | ~10 |
| `firstTokenDate.ts` | Timestamp первого токена | ~20 |
| `metricsOptOut.ts` | Opt-out из метрик | ~20 |
| `overageCreditGrant.ts` | Overage credit management | ~50 |
| `referral.ts` | Referral tracking | ~30 |
| `ultrareviewQuota.ts` | Ultrareview quota check | ~40 |
| `grove.ts` | Grove API integration | ~30 |
| `dumpPrompts.ts` | Debug: dump prompts to file | ~50 |

Поток вызова:
```
query() [claude.ts]
  → getAnthropicClient() [client.ts]
  → withRetry() [withRetry.ts]
    → client.beta.messages.stream() [Anthropic SDK]
    → recordPromptState() [promptCacheBreakDetection.ts]
    → logAPISuccessAndDuration() [logging.ts]
    → checkResponseForCacheBreak() [promptCacheBreakDetection.ts]
```

---

### 2. Client Abstraction (`client.ts`)

#### 4 провайдера, один интерфейс

`getAnthropicClient()` (строка 88) -- async фабрика, возвращающая `Anthropic` инстанс. Выбор провайдера по env vars:

| Условие | Провайдер | SDK |
|---------|-----------|-----|
| `CLAUDE_CODE_USE_BEDROCK=true` | AWS Bedrock | `@anthropic-ai/bedrock-sdk` |
| `CLAUDE_CODE_USE_FOUNDRY=true` | Azure Foundry | `@anthropic-ai/foundry-sdk` |
| `CLAUDE_CODE_USE_VERTEX=true` | GCP Vertex AI | `@anthropic-ai/vertex-sdk` |
| Default | Anthropic Direct API | `@anthropic-ai/sdk` |

**Bedrock и Vertex кастятся к `Anthropic`** через `as unknown as Anthropic` (строки 189, 219, 297) -- "we have always been lying about the return type".

#### Заголовки по умолчанию (строки 105-116)

```typescript
const defaultHeaders = {
  'x-app': 'cli',
  'User-Agent': getUserAgent(),
  'X-Claude-Code-Session-Id': getSessionId(),
  ...customHeaders,  // из ANTHROPIC_CUSTOM_HEADERS
  ...(containerId ? { 'x-claude-remote-container-id': containerId } : {}),
  ...(remoteSessionId ? { 'x-claude-remote-session-id': remoteSessionId } : {}),
  ...(clientApp ? { 'x-client-app': clientApp } : {}),
}
```

`ANTHROPIC_CUSTOM_HEADERS` парсится как многострочный формат `Name: Value` (curl-style), разделитель -- `\n`.

#### Client Request ID (`buildFetch`, строка 358)

Обёртка над `fetch`, которая для first-party API инъектирует `x-client-request-id` (UUID) в каждый запрос. Цель -- корреляция таймаутов (когда server request ID недоступен) с серверными логами.

#### Аутентификация

| Режим | Механизм |
|-------|----------|
| Claude.ai (OAuth) | `authToken` из `getClaudeAIOAuthTokens().accessToken` |
| API Key (Console) | `apiKey` из `getAnthropicApiKey()` или `ANTHROPIC_AUTH_TOKEN` |
| Bedrock | AWS credentials (STS refresh), Bearer token, или `skipAuth` |
| Vertex | `GoogleAuth` с scopes `cloud-platform`, fallback `projectId` |
| Foundry | `ANTHROPIC_FOUNDRY_API_KEY` или Azure AD token via `DefaultAzureCredential` |

Timeout по умолчанию: `API_TIMEOUT_MS` env var или **600 секунд** (строка 144).

---

### 3. Claude API Calls (`claude.ts`)

`claude.ts` -- ядро API-слоя (~1500 строк). Экспортирует `query()` -- **async generator**, который стримит `StreamEvent | SystemAPIErrorMessage` и возвращает массив `AssistantMessage[]`.

#### Ключевые типы

```typescript
type Options = {
  model: string
  maxTokens: number
  systemPrompt: SystemPrompt
  tools: Tools
  toolPermissionContext: ToolPermissionContext
  thinkingConfig: ThinkingConfig
  effortValue?: EffortValue
  temperature?: number
  querySource: QuerySource
  fastMode?: boolean
  taskBudget?: { total: number; remaining?: number }
  // ...и ещё ~20 полей
}
```

#### Сборка запроса (обзор flow)

1. **Нормализация модели**: `normalizeModelStringForAPI(model)` -- resolve alias в full model ID
2. **Prompt caching**: `getPromptCachingEnabled(model)` проверяет `DISABLE_PROMPT_CACHING` / per-model env vars
3. **System prompt**: разбивается на `systemPrefix` (глобально кешируемый) и `systemSuffix` (per-request)
4. **Cache control**: `getCacheControl({ scope, querySource })` -- определяет TTL (`ephemeral` или `1h`) и scope (`global` или none)
5. **Tools**: `toolToAPISchema(tool)` -- конверсия в `BetaToolUnion[]`
6. **Betas**: `getMergedBetas()` собирает ~15 beta-заголовков (AFK, effort, caching scope, structured outputs, context management, и т.д.)
7. **Extra body**: `getExtraBodyParams(betaHeaders)` парсит `CLAUDE_CODE_EXTRA_BODY` + инъектирует `anti_distillation`, `anthropic_internal`
8. **Messages**: `normalizeMessagesForAPI(messages)` + `ensureToolResultPairing()` -- гарантирует API invariant: каждый `tool_use` имеет `tool_result`
9. **Effort**: `configureEffortParams()` -- строковый effort идёт в `output_config.effort`, числовой (ant-only) -- в `anthropic_internal.effort_override`
10. **Task budget**: `configureTaskBudgetParams()` -- token-aware бюджет задачи (EAP beta)

#### Prompt Cache: 1h TTL (`should1hCacheTTL`, строка 393)

```typescript
function should1hCacheTTL(querySource?: QuerySource): boolean {
  // Bedrock: 1h если ENABLE_PROMPT_CACHING_1H_BEDROCK
  // Eligibility latched в bootstrap state (session-stable):
  //   - ant users: всегда eligible
  //   - Claude.ai subscribers: eligible если НЕ в overage
  // Allowlist из GrowthBook (тоже latched):
  //   - patterns: "repl_main_thread*", "sdk", "agent:*"
}
```

**Ключевой момент**: eligibility и allowlist **latched** (кешированы в bootstrap state) -- предотвращает mid-session переключение TTL, которое сбросило бы prompt cache.

#### Global Cache Scope Strategy (строки ~350-370)

Три стратегии для `cache_control.scope`:

| Стратегия | Когда | Поведение |
|-----------|-------|-----------|
| `tool_based` | Есть MCP-тулы с `cache_control` | Scope на tool-уровне |
| `system_prompt` | `shouldUseGlobalCacheScope()` = true | Scope на system prompt prefix |
| `none` | Default | Без global scope |

---

### 4. Retry Logic (`withRetry.ts`)

#### Архитектура

`withRetry()` -- **async generator** (строка 170). Yield-ит `SystemAPIErrorMessage` (для UI feedback), return-ит результат операции.

```typescript
export async function* withRetry<T>(
  getClient: () => Promise<Anthropic>,
  operation: (client: Anthropic, attempt: number, context: RetryContext) => Promise<T>,
  options: RetryOptions,
): AsyncGenerator<SystemAPIErrorMessage, T>
```

#### Константы

| Константа | Значение | Назначение |
|-----------|----------|------------|
| `DEFAULT_MAX_RETRIES` | 10 | По умолчанию (override: `CLAUDE_CODE_MAX_RETRIES`) |
| `BASE_DELAY_MS` | 500 | Базовая задержка для exponential backoff |
| `MAX_529_RETRIES` | 3 | До fallback на другую модель |
| `FLOOR_OUTPUT_TOKENS` | 3000 | Минимум output tokens после context overflow |
| `PERSISTENT_MAX_BACKOFF_MS` | 5 мин | Max backoff для unattended retry |
| `PERSISTENT_RESET_CAP_MS` | 6 часов | Абсолютный cap для persistent retry |
| `HEARTBEAT_INTERVAL_MS` | 30 сек | Keep-alive chunk для persistent retry |
| `SHORT_RETRY_THRESHOLD_MS` | 20 сек | Fast mode: retry vs cooldown порог |
| `MIN_COOLDOWN_MS` | 10 мин | Минимальный cooldown fast mode |
| `DEFAULT_FAST_MODE_FALLBACK_HOLD_MS` | 30 мин | Дефолтный cooldown |

#### Exponential Backoff (строка 530)

```typescript
function getRetryDelay(attempt, retryAfterHeader?, maxDelayMs = 32000): number {
  if (retryAfterHeader) return parseInt(retryAfterHeader) * 1000  // Retry-After header wins
  const baseDelay = min(BASE_DELAY_MS * 2^(attempt-1), maxDelayMs)
  const jitter = random() * 0.25 * baseDelay
  return baseDelay + jitter
}
```

#### Что retry-ится (`shouldRetry`, строка 696)

| Ошибка | Retry? | Условие |
|--------|--------|---------|
| 401 | Да | Очищает API key cache |
| 403 "token revoked" | Да | OAuth token refresh |
| 408 (timeout) | Да | Всегда |
| 409 (lock timeout) | Да | Всегда |
| 429 (rate limit) | Условно | Не для Claude.ai Max/Pro (кроме Enterprise) |
| 529 (overloaded) | Условно | Только foreground sources (строка 62) |
| 5xx | Да | Всегда (ant может ignore `x-should-retry: false`) |
| `APIConnectionError` | Да | Всегда |
| `overloaded_error` в message | Да | SDK не всегда правильно парсит 529 |

#### 529 Fallback Logic (строки 327-365)

После `MAX_529_RETRIES` (3) consecutive 529 ошибок:
1. Если есть `fallbackModel` -- бросает `FallbackTriggeredError` (caller переключает модель)
2. Если external user без sandbox -- бросает `CannotRetryError` с `REPEATED_529_ERROR_MESSAGE`

#### Foreground vs Background 529 (строки 62-89)

Background source (speculation, session_memory, prompt_suggestion) -- **не retry-ятся** при 529. Причина: "during a capacity cascade each retry is 3-10x gateway amplification, and the user never sees those fail anyway".

```typescript
const FOREGROUND_529_RETRY_SOURCES = new Set([
  'repl_main_thread', 'sdk', 'agent:custom', 'agent:default', 'agent:builtin',
  'compact', 'hook_agent', 'hook_prompt', 'verification_agent', 'side_question',
  'auto_mode', /* + bash_classifier (ant-only via feature gate) */
])
```

#### Fast Mode Fallback (строки 267-314)

При 429/529 в fast mode:
1. Проверяет `anthropic-ratelimit-unified-overage-disabled-reason` header -- если есть, permanent disable
2. Если `Retry-After < 20 сек` -- retry с fast mode ON (сохраняет prompt cache)
3. Если `Retry-After >= 20 сек` или unknown -- trigger cooldown, переключить на standard speed

#### Persistent Retry (`CLAUDE_CODE_UNATTENDED_RETRY`, строки 91-104)

Ant-only. При 429/529 retry-ится **бесконечно** (for-loop clamp, строка 506), с heartbeat yield каждые 30 сек чтобы host не убил idle session.

#### Context Overflow Handling (строки 388-427)

При 400 "input length and `max_tokens` exceed context limit":
1. Парсит `inputTokens` и `contextLimit` из error message (regex)
2. Вычисляет `availableContext = contextLimit - inputTokens - 1000 (safety buffer)`
3. Устанавливает `retryContext.maxTokensOverride = max(3000, availableContext, thinkingBudget+1)`
4. `continue` без задержки -- следующая попытка с уменьшенным max_tokens

#### Reconnection (строки 218-251)

Fresh client создаётся при:
- Первый вызов (`client === null`)
- 401 (expired token)
- 403 "OAuth token has been revoked"
- Bedrock auth error (CredentialsProviderError или 403)
- Vertex auth error (google-auth-library credential failure или 401)
- ECONNRESET/EPIPE (stale keep-alive socket -- `disableKeepAlive()`)

---

### 5. Error Handling (`errors.ts`, `errorUtils.ts`)

#### Error Classification (`classifyAPIError`, errors.ts)

Классифицирует ошибки в категории для аналитики:

| Категория | Условие |
|-----------|---------|
| `prompt_too_long` | 413 или "prompt is too long" |
| `rate_limit` | 429 |
| `overloaded` | 529 или `overloaded_error` в message |
| `auth_error` | 401 |
| `timeout` | `APIConnectionTimeoutError` |
| `connection_error` | `APIConnectionError` |
| `ssl_error` | SSL code в cause chain |
| `server_error` | 5xx |
| `media_size` | Image/PDF size rejection |
| `unknown` | Всё остальное |

#### SSL Error Detection (`errorUtils.ts`, строки 5-29)

29 конкретных SSL error codes отслеживаются:

```typescript
const SSL_ERROR_CODES = new Set([
  'UNABLE_TO_VERIFY_LEAF_SIGNATURE', 'CERT_HAS_EXPIRED',
  'DEPTH_ZERO_SELF_SIGNED_CERT', 'SELF_SIGNED_CERT_IN_CHAIN',
  'ERR_TLS_CERT_ALTNAME_INVALID', 'ERR_TLS_HANDSHAKE_TIMEOUT',
  // ... всего 18 кодов
])
```

`extractConnectionErrorDetails()` (строка 42) ходит по `error.cause` chain (до 5 уровней глубины) чтобы найти root error с `.code`.

#### Nested API Error Parsing (строка 144)

После JSON round-trip (десериализация из JSONL) SDK-ный `APIError` теряет `.message`. Сообщение ищется в двух вложенных формах:
- Anthropic API: `error.error.error.message`
- Bedrock: `error.error.message`

#### HTML Sanitization (строка 107)

CloudFlare error pages (`<!DOCTYPE html>`) заменяются на `<title>` content.

#### Key Error Constants

```typescript
API_ERROR_MESSAGE_PREFIX = 'API Error'
PROMPT_TOO_LONG_ERROR_MESSAGE = 'Prompt is too long'
REPEATED_529_ERROR_MESSAGE = 'Repeated 529 Overloaded errors'
CREDIT_BALANCE_TOO_LOW_ERROR_MESSAGE = 'Credit balance is too low'
INVALID_API_KEY_ERROR_MESSAGE = 'Not logged in · Please run /login'
```

---

### 6. Usage Tracking (`usage.ts`, `logging.ts`)

#### Rate Limit Utilization (`usage.ts`)

`fetchUtilization()` -- GET `/api/oauth/usage`. Только для Claude.ai OAuth subscribers с `user:profile` scope.

```typescript
type Utilization = {
  five_hour?: RateLimit | null       // { utilization: 0-100, resets_at: ISO }
  seven_day?: RateLimit | null
  seven_day_oauth_apps?: RateLimit | null
  seven_day_opus?: RateLimit | null
  seven_day_sonnet?: RateLimit | null
  extra_usage?: ExtraUsage | null    // { is_enabled, monthly_limit, used_credits, utilization }
}
```

Timeout: 5 секунд. Пропускается если OAuth token expired.

#### API Success Logging (`logging.ts`, строка 398)

`logAPISuccess()` отправляет событие `tengu_api_success` с ~40 полями:

| Поле | Описание |
|------|----------|
| `inputTokens`, `outputTokens` | Из `usage` объекта |
| `cachedInputTokens` | `cache_read_input_tokens` |
| `uncachedInputTokens` | `cache_creation_input_tokens` |
| `durationMs` | Время последнего успешного attempt |
| `durationMsIncludingRetries` | Общее время с retry |
| `ttftMs` | Time to first token |
| `costUSD` | `calculateUSDCost()` |
| `textContentLength` | Суммарная длина text blocks |
| `thinkingContentLength` | Суммарная длина thinking blocks |
| `toolUseContentLengths` | Per-tool JSON input lengths |
| `timeSinceLastApiCallMs` | Интервал между запросами |

#### Gateway Detection (строка 107)

Определяет AI-proxy по response headers:

| Gateway | Fingerprint |
|---------|-------------|
| LiteLLM | `x-litellm-*` headers |
| Helicone | `helicone-*` headers |
| Portkey | `x-portkey-*` headers |
| Cloudflare AI Gateway | `cf-aig-*` headers |
| Kong | `x-kong-*` headers |
| Braintrust | `x-bt-*` headers |
| Databricks | `.cloud.databricks.com` / `.azuredatabricks.net` hostname |

---

### 7. Prompt Cache Break Detection (`promptCacheBreakDetection.ts`)

728 строк. **Двухфазная система** детекции сброса серверного prompt cache.

#### Phase 1: Pre-call (`recordPromptState`, строка 247)

Записывает snapshot текущего состояния и определяет, что изменилось:

```typescript
type PreviousState = {
  systemHash: number          // Hash system prompt без cache_control
  toolsHash: number           // Hash tool schemas
  cacheControlHash: number    // Hash WITH cache_control (TTL/scope flips)
  perToolHashes: Record<string, number>  // Per-tool schema hashes
  model: string
  fastMode: boolean
  globalCacheStrategy: string  // 'tool_based' | 'system_prompt' | 'none'
  betas: string[]             // Sorted beta headers
  effortValue: string
  extraBodyHash: number       // Hash getExtraBodyParams()
  callCount: number
  pendingChanges: PendingChanges | null
  prevCacheReadTokens: number | null
  cacheDeletionsPending: boolean
}
```

**Tracking по source**: `previousStateBySource` Map, max 10 entries. `compact` маппится на `repl_main_thread` (они шарят server-side cache).

**Hash**: `Bun.hash()` (с bigint→number coerce) или fallback `djb2Hash` для non-Bun.

#### Phase 2: Post-call (`checkResponseForCacheBreak`, строка 437)

Сравнивает `cache_read_input_tokens` с предыдущим значением:

```
Cache break = (cacheReadTokens < prevCacheRead * 0.95) AND (tokenDrop >= 2000)
```

Если break обнаружен:
1. Строит explanation из `pendingChanges` ("system prompt changed (+1234 chars)", "tools changed (+2/-1 tools)", "model changed (opus → sonnet)")
2. Проверяет TTL: если >5 мин или >1ч с последнего assistant message -- "possible TTL expiry"
3. Если ничего не изменилось и <5 мин -- "likely server-side"
4. Пишет diff-файл в temp dir для ant debugging
5. Логирует `tengu_prompt_cache_break` event

#### Исключения

- `notifyCacheDeletion()` -- cached microcompact удалил контент, drop ожидаем
- `notifyCompaction()` -- compaction сбросил baseline
- Haiku -- excluded (`isExcludedModel`, строка 129)

---

### 8. Session Ingress (`sessionIngress.ts`)

Session Ingress -- система **серверной персистенции транскриптов** сессий. Позволяет teleport (перенос) сессий между устройствами.

#### Архитектура

- Append-only лог (linked list через `Last-Uuid` header)
- Optimistic concurrency control: конфликт 409 → adopt server UUID → retry
- Sequential execution per session: `sequentialAppendBySession` Map (предотвращает race conditions)

#### `appendSessionLog()` (строка 193)

```
PUT /v1/session_ingress/session/{sessionId}
Headers: Authorization: Bearer {JWT}, Last-Uuid: {prevUuid}
Body: TranscriptMessage (uuid, type, content)
```

**Retry**: до 10 попыток с exponential backoff (500ms → 8s). На 409 (conflict):
1. Если `x-last-uuid === entry.uuid` → наш entry уже на сервере, восстанавливаем state
2. Иначе → adopt server UUID из header или re-fetch session

#### `getTeleportEvents()` (строка 291)

Новый v2 API:
```
GET /v1/code/sessions/{id}/teleport-events?limit=1000&cursor={opaque}
```

Пагинированный (1000/page, max 100 pages = 100K events). Infinite-loop guard.

---

### 9. Bootstrap (`bootstrap.ts`)

#### `fetchBootstrapData()` (строка 114)

```
GET /api/claude_cli/bootstrap
```

Возвращает:
- `client_data` -- произвольный JSON (remote config)
- `additional_model_options` -- дополнительные модели (name, model ID, description)

**Кеширование**: результат сохраняется в `globalConfig` (`clientDataCache`, `additionalModelOptionsCache`). Перезаписывается только если данные изменились (deep equal через `isEqual`).

**Аутентификация**: OAuth preferred (нужен `user:profile` scope), fallback на API key.

**Guard**: пропускается для 3P providers, essential-traffic-only mode, и отсутствии auth.

---

### 10. Files API (`filesApi.ts`)

#### Download

```
GET /v1/files/{fileId}/content
Headers: Authorization: Bearer {OAuth}, anthropic-version: 2023-06-01, anthropic-beta: files-api-2025-04-14,oauth-2025-04-20
```

Timeout: 60 сек. Max retries: 3. Non-retriable: 404, 401, 403.

`downloadSessionFiles()` -- parallel download с concurrency limit (default 5). Файлы сохраняются в `{cwd}/{sessionId}/uploads/{relativePath}`.

**Path validation**: `buildDownloadPath()` (строка 187) нормализует путь, отклоняет `..` traversal, strip-ит redundant prefixes.

#### Upload (BYOC mode)

```
POST /v1/files
Content-Type: multipart/form-data
```

Max file size: **500 MB** (строка 82). Timeout: 120 сек. Multipart boundary -- `randomUUID()`.

Non-retriable: 401, 403, 413. Retriable: 5xx, network errors.

`uploadSessionFiles()` -- parallel upload с concurrency limit 5.

#### List

```
GET /v1/files?after_created_at={ISO}&after_id={cursor}
```

Пагинированный через `after_id` cursor.

---

### 11. Admin Requests (`adminRequests.ts`)

Для Team/Enterprise пользователей без billing permissions:

| Endpoint | Назначение |
|----------|-----------|
| `POST /api/oauth/organizations/{orgUUID}/admin_requests` | Создать запрос (limit increase / seat upgrade) |
| `GET .../admin_requests/me?request_type=...&statuses=...` | Мои pending запросы |
| `GET .../admin_requests/eligibility?request_type=...` | Проверить допустимость |

---

## Часть 2: State Management

### 12. AppState -- схема (`AppStateStore.ts`)

**452 строки**. `AppState` -- единый immutable state объект приложения.

```typescript
export type AppState = DeepImmutable<{
  // Core
  settings: SettingsJson
  verbose: boolean
  mainLoopModel: ModelSetting            // alias, full name, или null (default)
  mainLoopModelForSession: ModelSetting   // session-scoped override

  // UI
  statusLineText: string | undefined
  expandedView: 'none' | 'tasks' | 'teammates'
  isBriefOnly: boolean
  selectedIPAgentIndex: number
  coordinatorTaskIndex: number
  viewSelectionMode: 'none' | 'selecting-agent' | 'viewing-agent'
  footerSelection: FooterItem | null     // 'tasks' | 'tmux' | 'bagel' | 'teams' | 'bridge' | 'companion'

  // Permissions
  toolPermissionContext: ToolPermissionContext

  // Features
  kairosEnabled: boolean                 // Assistant mode
  thinkingEnabled: boolean | undefined
  promptSuggestionEnabled: boolean
  fastMode?: boolean
  effortValue?: EffortValue
  advisorModel?: string

  // Remote/Bridge
  remoteSessionUrl: string | undefined
  remoteConnectionStatus: 'connecting' | 'connected' | 'reconnecting' | 'disconnected'
  remoteBackgroundTaskCount: number
  replBridgeEnabled: boolean
  replBridgeConnected: boolean
  replBridgeSessionActive: boolean
  replBridgeConnectUrl: string | undefined
  replBridgeSessionUrl: string | undefined
  // ... ещё ~10 bridge-полей

  // Ultraplan
  ultraplanLaunching?: boolean
  ultraplanSessionUrl?: string
  ultraplanPendingChoice?: { plan: string; sessionId: string; taskId: string }
  isUltraplanMode?: boolean
}> & {
  // Mutable sections (excluded from DeepImmutable)
  tasks: { [taskId: string]: TaskState }
  agentNameRegistry: Map<string, AgentId>
  foregroundedTaskId?: string
  viewingAgentTaskId?: string

  // MCP
  mcp: {
    clients: MCPServerConnection[]
    tools: Tool[]
    commands: Command[]
    resources: Record<string, ServerResource[]>
    pluginReconnectKey: number
  }

  // Plugins
  plugins: {
    enabled: LoadedPlugin[]
    disabled: LoadedPlugin[]
    commands: Command[]
    errors: PluginError[]
    installationStatus: { marketplaces: [...], plugins: [...] }
    needsRefresh: boolean
  }

  // Other
  agentDefinitions: AgentDefinitionsResult
  fileHistory: FileHistoryState
  attribution: AttributionState
  todos: { [agentId: string]: TodoList }
  notifications: { current: Notification | null; queue: Notification[] }
  elicitation: { queue: ElicitationRequestEvent[] }
  sessionHooks: SessionHooksState
  promptSuggestion: { text: string | null; promptId: ...; shownAt: number; ... }
  speculation: SpeculationState
  speculationSessionTimeSavedMs: number
  authVersion: number
  initialMessage: { message: UserMessage; clearContext?: boolean; mode?: PermissionMode } | null
  activeOverlays: ReadonlySet<string>
  // ... computer use, REPL context, team context, inbox, worker sandbox, и т.д.
}
```

#### `getDefaultAppState()` (строка 456)

Инициализирует ~60 полей. Ключевые дефолты:
- `settings: getInitialSettings()` -- merged settings из всех source-ов
- `toolPermissionContext.mode`: `'plan'` для teammates с `planModeRequired`, иначе `'default'`
- `thinkingEnabled: shouldEnableThinkingByDefault()`
- `promptSuggestionEnabled: shouldEnablePromptSuggestion()`
- `speculation: { status: 'idle' }`

---

### 13. Store -- реализация (`store.ts`)

**35 строк**. Минимальный store без зависимостей:

```typescript
export function createStore<T>(initialState: T, onChange?: OnChange<T>): Store<T> {
  let state = initialState
  const listeners = new Set<Listener>()

  return {
    getState: () => state,
    setState: (updater: (prev: T) => T) => {
      const prev = state
      const next = updater(prev)
      if (Object.is(next, prev)) return   // identity check -- no-op если тот же объект
      state = next
      onChange?.({ newState: next, oldState: prev })
      for (const listener of listeners) listener()
    },
    subscribe: (listener: Listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
}
```

**React integration** (`AppState.tsx`):

- `AppStateProvider` -- создаёт store один раз (`useState(() => createStore(...))`)
- `useAppState(selector)` -- `useSyncExternalStore(store.subscribe, get)` с selector
- `useSetAppState()` -- stable reference на `store.setState`
- `useAppStateMaybeOutsideOfProvider(selector)` -- safe version, returns `undefined` вне Provider

**Правило**: selector не должен возвращать новый объект (Object.is не пройдёт). Выбирать существующий sub-object reference.

---

### 14. Selectors (`selectors.ts`)

Два основных selector:

#### `getViewedTeammateTask(appState)` (строка 18)

```typescript
function getViewedTeammateTask(appState): InProcessTeammateTaskState | undefined {
  const task = appState.tasks[appState.viewingAgentTaskId]
  if (!task || !isInProcessTeammateTask(task)) return undefined
  return task
}
```

#### `getActiveAgentForInput(appState)` (строка 59)

Discriminated union для роутинга input:

```typescript
type ActiveAgentForInput =
  | { type: 'leader' }                                    // Input идёт в leader
  | { type: 'viewed'; task: InProcessTeammateTaskState }   // Input в viewed agent
  | { type: 'named_agent'; task: LocalAgentTaskState }     // Input в named agent
```

---

### 15. Change Listeners (`onChangeAppState.ts`)

`onChangeAppState()` -- callback, вызываемый store при каждом изменении state. Центральный **choke point** для side effects.

#### Permission Mode Sync (строки 50-92)

**Проблема**: 8+ путей мутации `toolPermissionContext.mode` (Shift+Tab, ExitPlanMode dialog, `/plan`, rewind, bridge, и т.д.). Раньше только 2 из них уведомляли CCR.

**Решение**: diff в `onChangeAppState`:

```typescript
if (prevMode !== newMode) {
  const prevExternal = toExternalPermissionMode(prevMode)
  const newExternal = toExternalPermissionMode(newMode)
  if (prevExternal !== newExternal) {
    notifySessionMetadataChanged({ permission_mode: newExternal, is_ultraplan_mode: ... })
  }
  notifyPermissionModeChanged(newMode)
}
```

`toExternalPermissionMode()` нормализует internal-only modes (bubble, ungated auto → 'default').

#### Model Persistence (строки 94-112)

При изменении `mainLoopModel`:
- `null` → remove `model` из userSettings
- non-null → save в userSettings + `setMainLoopModelOverride()`

#### Settings Change (строки 156-170)

При изменении `settings`:
1. Clear auth caches (`clearApiKeyHelperCache`, `clearAwsCredentialsCache`, `clearGcpCredentialsCache`)
2. Re-apply env vars если `settings.env` изменился

#### Другие side effects

- `expandedView` → persist в globalConfig (`showExpandedTodos`, `showSpinnerTree`)
- `verbose` → persist в globalConfig
- `tungstenPanelVisible` → persist в globalConfig (ant-only)

---

## Часть 3: History & Sessions

### 16. Session State (`sessionState.ts`)

#### Три состояния сессии

```typescript
type SessionState = 'idle' | 'running' | 'requires_action'
```

#### `RequiresActionDetails` (строка 15)

```typescript
type RequiresActionDetails = {
  tool_name: string
  action_description: string   // "Editing src/foo.ts", "Running npm test"
  tool_use_id: string
  request_id: string
  input?: Record<string, unknown>
}
```

#### External Metadata (строка 32)

```typescript
type SessionExternalMetadata = {
  permission_mode?: string | null
  is_ultraplan_mode?: boolean | null
  model?: string | null
  pending_action?: RequiresActionDetails | null
  post_turn_summary?: unknown
  task_summary?: string | null
}
```

#### Notification Flow

```
notifySessionStateChanged('requires_action', details)
  → stateListener?.(state, details)
  → metadataListener?.({ pending_action: details })
  → enqueueSdkEvent({ type: 'system', subtype: 'session_state_changed', state })

notifySessionStateChanged('idle')
  → metadataListener?.({ pending_action: null, task_summary: null })
```

---

### 17. Session Storage (`sessionStorage.ts`)

Большой файл (~2000+ строк). JSONL-based transcript persistence.

#### Формат транскрипта

Каждая строка -- JSON entry одного из типов:

```typescript
function isTranscriptMessage(entry: Entry): entry is TranscriptMessage {
  return entry.type === 'user' || entry.type === 'assistant'
      || entry.type === 'attachment' || entry.type === 'system'
}
```

Дополнительные entry types (не транскрипт):
- `FileHistorySnapshotMessage` -- snapshot файлов
- `AttributionSnapshotMessage` -- commit attribution
- `ContentReplacementEntry` -- замена tool results
- `ContextCollapseCommitEntry` / `ContextCollapseSnapshotEntry`
- Metadata entries (sessionId, mode, worktree state, и т.д.)

#### Linked List через `parentUuid`

Каждое TranscriptMessage имеет `uuid` и `parentUuid`. При resume -- reconstruct chain от последнего entry. Это позволяет:
- Обнаруживать chain forks (orphaned messages)
- Правильно восстанавливать порядок после crashes

#### Session Ingress Integration

```typescript
import * as sessionIngress from '../services/api/sessionIngress.js'
```

При `appendSessionLog()` -- запись идёт и в локальный JSONL, и на сервер Anthropic (для teleport).

#### Ключевые операции

| Функция | Назначение |
|---------|-----------|
| `recordTranscript()` | Append message в JSONL + session ingress |
| `loadTranscriptFile()` | Прочитать JSONL, reconstruct message chain |
| `adoptResumedSessionFile()` | Переключить session file pointer на resumed |
| `resetSessionFilePointer()` | Обнулить (при switchSession) |
| `restoreSessionMetadata()` | Восстановить name, mode, worktree из entries |
| `saveMode()` | Persist coordinator/normal mode |
| `saveWorktreeState()` | Persist worktree session |
| `recordContentReplacement()` | Persist tool result replacement records |

#### Skip-precompact Optimization

```typescript
SKIP_PRECOMPACT_THRESHOLD // из sessionStoragePortable.ts
```

Для больших файлов -- "lite read": читает head+tail, пропуская pre-compaction messages.

#### Tombstone

Max rewrite size: **50 MB** (`MAX_TOMBSTONE_REWRITE_BYTES`, строка 123). Предотвращает OOM при переписывании больших session файлов.

---

### 18. Session Restore (`sessionRestore.ts`)

552 строки. Логика восстановления состояния при `--resume` / `--continue`.

#### `restoreSessionStateFromLog()` (строка 99)

Вызывается при resume. Восстанавливает:
1. **File history** -- из `fileHistorySnapshots`
2. **Attribution** -- из `attributionSnapshots` (ant-only)
3. **Context collapse** -- из `contextCollapseCommits` + `contextCollapseSnapshot`
4. **TodoWrite** -- сканирует transcript backward для последнего `TodoWrite` tool_use block

#### `processResumedConversation()` (строка 409)

Полный flow resume:

```
1. matchSessionMode() — coordinator/normal mode matching
2. switchSession(sid) — принять session ID
3. resetSessionFilePointer() — очистить stale pointer
4. restoreCostStateForSession(sid) — cost tracking
5. restoreSessionMetadata() — name, mode, worktree
6. restoreWorktreeForResume() — cd в worktree если был
7. adoptResumedSessionFile() — point файл на resumed transcript
8. restoreAgentFromSession() — re-apply agent type + model override
9. saveMode() — persist current mode
10. computeRestoredAttributionState() — attribution state
11. computeStandaloneAgentContext() — agent name/color
12. refreshAgentDefinitionsForModeSwitch() — re-derive agents
```

#### Worktree Restore (`restoreWorktreeForResume`, строка 332)

```typescript
function restoreWorktreeForResume(worktreeSession) {
  const fresh = getCurrentWorktreeSession()
  if (fresh) { saveWorktreeState(fresh); return }  // --worktree флаг приоритетнее

  if (!worktreeSession) return
  try {
    process.chdir(worktreeSession.worktreePath)  // TOCTOU-safe existence check
  } catch {
    saveWorktreeState(null)  // Directory gone, clear stale cache
    return
  }
  setCwd(worktreeSession.worktreePath)
  restoreWorktreeSession(worktreeSession)
  clearMemoryFileCaches()       // Invalidate stale caches
  clearSystemPromptSections()
}
```

#### Agent Restore (`restoreAgentFromSession`, строка 200)

```
1. Если --agent на CLI → keep CLI definition
2. Если session без agent → setMainThreadAgentType(undefined)
3. Найти agent в activeAgents → setMainThreadAgentType(agentType)
4. Если agent не найден → log warning, use default
5. Apply model override если user не указал --model
```

---

## Верификация

### Проверенные утверждения

| Утверждение | Подтверждено | Файл:строка |
|-------------|--------------|-------------|
| `DEFAULT_MAX_RETRIES = 10` | Да | `withRetry.ts:53` |
| `BASE_DELAY_MS = 500` | Да | `withRetry.ts:55` |
| `MAX_529_RETRIES = 3` | Да | `withRetry.ts:54` |
| `PERSISTENT_MAX_BACKOFF_MS = 5 * 60 * 1000` | Да | `withRetry.ts:96` |
| `HEARTBEAT_INTERVAL_MS = 30_000` | Да | `withRetry.ts:98` |
| Bedrock/Vertex кастятся к `Anthropic` | Да | `client.ts:189,219,297` |
| `x-app: cli` default header | Да | `client.ts:106` |
| `x-client-request-id` инъектируется только для first-party | Да | `client.ts:366-367` |
| API timeout = 600s default | Да | `client.ts:144` |
| `should1hCacheTTL` latches eligibility в bootstrap state | Да | `claude.ts:406-412` |
| 29 SSL error codes в `SSL_ERROR_CODES` set | Да | `errorUtils.ts:5-29` (18 codes in set, not 29) |
| `extractConnectionErrorDetails` walks cause chain depth 5 | Да | `errorUtils.ts:51` |
| `checkResponseForCacheBreak` threshold: 5% drop AND 2000 tokens | Да | `promptCacheBreakDetection.ts:487-489` |
| `MAX_TRACKED_SOURCES = 10` | Да | `promptCacheBreakDetection.ts:107` |
| Session ingress max retries = 10 | Да | `sessionIngress.ts:25` |
| Teleport events max pages = 100, limit 1000/page | Да | `sessionIngress.ts:311,314` |
| Bootstrap timeout = 5000ms | Да | `bootstrap.ts:92` |
| Files API max size = 500MB | Да | `filesApi.ts:82` |
| Files upload timeout = 120s | Да | `filesApi.ts:466` |
| Store uses `Object.is` for identity check | Да | `store.ts:23` |
| `onChangeAppState` syncs permission mode to CCR | Да | `onChangeAppState.ts:65-92` |
| `SessionState` = `'idle' \| 'running' \| 'requires_action'` | Да | `sessionState.ts:1` |
| `MAX_TOMBSTONE_REWRITE_BYTES = 50MB` | Да | `sessionStorage.ts:123` |

### Найденные и исправленные ошибки в процессе анализа

| # | Было | Стало | Источник |
|---|------|-------|----------|
| 1 | "29 SSL error codes" | 18 уникальных кодов в `SSL_ERROR_CODES` set | `errorUtils.ts:5-29` |
| 2 | "OAuth token check" -- предполагалось sync | `checkAndRefreshOAuthTokenIfNeeded()` -- async, awaited | `client.ts:132` |
| 3 | "fetchUtilization uses API key" | Только OAuth; API key users возвращают `{}` | `usage.ts:34` |
| 4 | "retry delay max 32s" | 32s для обычного, 5min для persistent, 6h cap | `withRetry.ts:533,444,447` |

### Вердикт

~30 утверждений проверено против исходного кода. 4 неточности найдены и исправлены. Документ отражает актуальное состояние кодовой базы.
