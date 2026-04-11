# Claude Code — Bridge System (Remote Agent-IDE Communication)

Анализ на основе утёкшего исходного кода Claude Code (`claude-leaked/src/bridge/`, 31 файл).

---

## Обзор: что такое Bridge

Bridge (он же Remote Control) — система двусторонней связи между локальным CLI-процессом Claude Code и удалёнными клиентами (claude.ai, VS Code, JetBrains, мобильные приложения). Позволяет:

1. **Управлять CLI из браузера** — отправлять промпты, прерывать выполнение, менять модель
2. **Видеть активность CLI в реальном времени** — стриминг assistant-ответов, статусы инструментов
3. **Управлять правами** — approve/deny конкретных tool invocations из web-интерфейса
4. **Запускать несколько сессий** — worktree/same-dir режимы для параллельной работы

### Архитектура (два пути)

| Путь | Файл | Слои | Gate |
|------|-------|------|------|
| **v1 (env-based)** | `replBridge.ts` / `bridgeMain.ts` | Environments API → Poll/Ack/Heartbeat → Session-Ingress WS/SSE | `tengu_ccr_bridge` |
| **v2 (env-less)** | `remoteBridgeCore.ts` | POST /code/sessions → POST /bridge → SSE+CCRClient | `tengu_bridge_repl_v2` |

v1 использует Environments API: register → poll → ack → heartbeat → deregister.
v2 убирает этот слой: создаёт сессию напрямую, получает JWT через `/bridge`, подключается через SSE.

---

## 1. Bridge Lifecycle — жизненный цикл

### 1.1. Точка входа: `initReplBridge.ts`

REPL-специфичная обёртка (~570 строк). Проверяет все предусловия перед делегацией к ядру:

**Порядок проверок:**
1. `isBridgeEnabledBlocking()` — GrowthBook gate `tengu_ccr_bridge` + подписка claude.ai
2. `getBridgeAccessToken()` — OAuth-токен есть?
3. `isPolicyAllowed('allow_remote_control')` — организационная политика
4. Cross-process backoff — если 3+ процесса уже видели мёртвый токен (по `expiresAt`), skip
5. `checkAndRefreshOAuthTokenIfNeeded()` — проактивный рефреш, если протух
6. Пост-рефреш проверка — если протух и рефреш не помог, skip
7. `getOrganizationUUID()` — нужен для обоих путей

**Развилка v1/v2** (`initReplBridge.ts:410`):
```typescript
if (isEnvLessBridgeEnabled() && !perpetual) {
  // v2: initEnvLessBridgeCore()
} else {
  // v1: initBridgeCore()
}
```

`perpetual` (assistant-mode session continuity) пока не реализован в v2 — fallback на v1.

### 1.2. bridgeEnabled.ts — проверки доступности

| Функция | Назначение | Gate |
|---------|------------|------|
| `isBridgeEnabled()` | Cached, для UI | `tengu_ccr_bridge` + subscriber |
| `isBridgeEnabledBlocking()` | Blocking, для entitlement gates | То же, но ждёт GrowthBook init |
| `getBridgeDisabledReason()` | Диагностическое сообщение | Проверяет subscriber, scope, orgUUID, gate |
| `isEnvLessBridgeEnabled()` | v2 path? | `tengu_bridge_repl_v2` |
| `isCseShimEnabled()` | Ретэг `cse_*` → `session_*`? | `tengu_bridge_repl_v2_cse_shim_enabled` |
| `getCcrAutoConnectDefault()` | Авто-коннект при старте | `tengu_cobalt_harbor` |
| `isCcrMirrorEnabled()` | Mirror mode (outbound-only) | `tengu_ccr_mirror` |

**Subscriber check** (`bridgeEnabled.ts:94`):
```typescript
function isClaudeAISubscriber(): boolean {
  try { return authModule.isClaudeAISubscriber() }
  catch { return false } // Config not yet initialized
}
```

### 1.3. BridgeConfig (types.ts)

```typescript
type BridgeConfig = {
  dir: string               // Рабочая директория
  machineName: string       // hostname()
  branch: string            // git branch
  gitRepoUrl: string | null
  maxSessions: number       // Макс. параллельных сессий
  spawnMode: SpawnMode      // 'single-session' | 'worktree' | 'same-dir'
  bridgeId: string          // Клиентский UUID
  workerType: string        // 'claude_code' | 'claude_code_assistant'
  apiBaseUrl: string
  sessionIngressUrl: string
  sessionTimeoutMs?: number // По умолчанию 24 часа
}
```

`DEFAULT_SESSION_TIMEOUT_MS = 24 * 60 * 60 * 1000` (`types.ts:1`)

### 1.4. bridgeConfig.ts — auth/URL resolution

Два слоя: Override (ant-only env vars) → Production (OAuth store/config):

| Функция | Приоритет 1 (ant) | Приоритет 2 |
|---------|-------------------|-------------|
| `getBridgeAccessToken()` | `CLAUDE_BRIDGE_OAUTH_TOKEN` | `getClaudeAIOAuthTokens().accessToken` |
| `getBridgeBaseUrl()` | `CLAUDE_BRIDGE_BASE_URL` | `getOauthConfig().BASE_API_URL` |

---

## 2. v1: Environment-Based Bridge (bridgeMain.ts + replBridge.ts)

### 2.1. bridgeMain.ts — standalone `claude remote-control`

~1100 строк. Оркестрирует полный lifecycle для standalone-режима (отдельный процесс).

**Основной цикл `runBridgeLoop()`** (`bridgeMain.ts:141`):

```
1. Инициализация Maps:
   - activeSessions: Map<sessionId, SessionHandle>
   - sessionStartTimes, sessionWorkIds, sessionCompatIds
   - sessionIngressTokens, sessionTimers
   - completedWorkIds: Set<workId>
   - sessionWorktrees: Map<sessionId, worktreeInfo>
   - timedOutSessions: Set<sessionId>
   - titledSessions: Set<compatSessionId>

2. capacityWake = createCapacityWake(loopSignal)

3. printBanner → startStatusUpdates → poll loop
```

**Poll loop** (`bridgeMain.ts:600`):
```
while (!loopSignal.aborted):
  pollConfig = getPollIntervalConfig()
  work = api.pollForWork(environmentId, environmentSecret, signal, reclaim_older_than_ms)

  if (!work):
    if atCapacity:
      heartbeat loop ИЛИ slow poll
    else:
      sleep(poll_interval)
    continue

  switch (work.data.type):
    case 'healthcheck': ack, log
    case 'session':
      existingHandle? → updateAccessToken + ack
      atCapacity? → break
      ack → decodeWorkSecret → registerWorker (v2) → createWorktree? → spawn
```

**Backoff-конфигурация** (`bridgeMain.ts:72`):
```typescript
const DEFAULT_BACKOFF = {
  connInitialMs: 2_000,
  connCapMs: 120_000,      // 2 минуты
  connGiveUpMs: 600_000,   // 10 минут
  generalInitialMs: 500,
  generalCapMs: 30_000,
  generalGiveUpMs: 600_000,
}
```

**onSessionDone** — cleanup callback (`bridgeMain.ts:442`):
- Удаляет из всех Maps/Sets
- `capacityWake.wake()` — будит poll loop для новой работы
- Timeout-killed сессии: `'interrupted'` → `'failed'`
- `stopWorkWithRetry()` — уведомляет сервер
- Multi-session: `archiveSession()`, потом idle
- Single-session: `controller.abort()` — весь bridge закрывается

### 2.2. replBridge.ts — REPL-embedded bridge (~2400 строк initBridgeCore)

Встроен в терминальный REPL-процесс (не standalone). Управляет WebSocket/SSE-транспортом из того же процесса.

**initBridgeCore()** (`replBridge.ts:260`):

```
1. readBridgePointer (perpetual mode) → tryReconnectInPlace
2. createBridgeApiClient → registerBridgeEnvironment
3. createSession (POST /v1/sessions)
4. writeBridgePointer (crash-recovery)
5. Poll loop → onWorkReceived → createTransport → wireCallbacks → connect
6. Teardown: result → archive → deregister → clearPointer
```

**Ключевой объект — BridgeCoreHandle** (superset ReplBridgeHandle):
```typescript
type ReplBridgeHandle = {
  bridgeSessionId: string
  environmentId: string
  sessionIngressUrl: string
  writeMessages(messages: Message[]): void       // Internal → SDK format
  writeSdkMessages(messages: SDKMessage[]): void // Direct SDK
  sendControlRequest(request: SDKControlRequest): void
  sendControlResponse(response: SDKControlResponse): void
  sendControlCancelRequest(requestId: string): void
  sendResult(): void
  teardown(): Promise<void>
}
```

---

## 3. Bridge API (bridgeApi.ts)

HTTP-клиент для Environments API. 540 строк.

### 3.1. Endpoints

| Метод | Endpoint | Auth | Назначение |
|-------|----------|------|-----------|
| POST | `/v1/environments/bridge` | OAuth | Регистрация environment |
| GET | `/v1/environments/{id}/work/poll` | EnvironmentSecret | Запрос работы |
| POST | `.../work/{id}/ack` | SessionIngress JWT | Подтверждение работы |
| POST | `.../work/{id}/stop` | OAuth | Остановка работы |
| POST | `.../work/{id}/heartbeat` | SessionIngress JWT | Продление lease |
| DELETE | `/v1/environments/bridge/{id}` | OAuth | Дерегистрация |
| POST | `/v1/sessions/{id}/archive` | OAuth | Архивирование сессии |
| POST | `.../bridge/reconnect` | OAuth | Переподключение сессии |
| POST | `/v1/sessions/{id}/events` | SessionIngress JWT | Permission response |

### 3.2. OAuth retry (`bridgeApi.ts:106`)

```typescript
async function withOAuthRetry(fn, context):
  response = fn(resolveAuth())
  if response.status !== 401: return
  if !deps.onAuth401: return response  // no handler
  refreshed = await deps.onAuth401(accessToken)
  if refreshed: retry once with new token
  return original 401 response
```

### 3.3. Error handling

| Status | Обработка |
|--------|-----------|
| 200/204 | OK |
| 401 | `BridgeFatalError` + login instruction |
| 403 | Expired? → "session expired". Else → "check permissions" |
| 404 | "Not found / not available" |
| 410 | "Session expired" (errorType `environment_expired`) |
| 429 | Transient "rate limited" |

### 3.4. ID validation (`bridgeApi.ts:41`)

```typescript
const SAFE_ID_PATTERN = /^[a-zA-Z0-9_-]+$/
// Prevents path traversal (../../admin) in URL interpolation
```

### 3.5. Headers

```typescript
{
  Authorization: `Bearer ${accessToken}`,
  'Content-Type': 'application/json',
  'anthropic-version': '2023-06-01',
  'anthropic-beta': 'environments-2025-11-01',
  'x-environment-runner-version': MACRO.VERSION,
  'X-Trusted-Device-Token': deviceToken  // optional
}
```

---

## 4. Session Management

### 4.1. createSession.ts — POST /v1/sessions

Создание сессии с контекстом Git-репозитория:

```typescript
const requestBody = {
  title,
  events,   // SDKMessage[] wrapped in {type:'event', data:msg}
  session_context: {
    sources: [{ type: 'git_repository', url, revision }],
    outcomes: [{ type: 'git_repository', git_info: { type: 'github', repo, branches } }],
    model: getMainLoopModel(),
  },
  environment_id: environmentId,
  source: 'remote-control',
  permission_mode,  // optional
}
```

Headers: `'anthropic-beta': 'ccr-byoc-2025-07-29'`, `'x-organization-uuid': orgUUID`

### 4.2. codeSessionApi.ts — v2 Code Session API

Для env-less path (без Environments API):

**POST /v1/code/sessions** (`codeSessionApi.ts:26`):
```typescript
axios.post(url, { title, bridge: {}, tags }, { headers: oauthHeaders(accessToken) })
// bridge: {} — positive signal for the oneof runner
// session.id начинается с "cse_"
```

**POST /v1/code/sessions/{id}/bridge** (`codeSessionApi.ts:93`):
```typescript
// Возвращает RemoteCredentials:
type RemoteCredentials = {
  worker_jwt: string      // Opaque JWT — не декодировать
  api_base_url: string
  expires_in: number      // Секунды до истечения
  worker_epoch: number    // Каждый вызов /bridge бампит epoch
}
```

### 4.3. sessionRunner.ts — Spawn дочерних процессов

**createSessionSpawner()** — фабрика для порождения CLI-процессов:

```typescript
const args = [
  ...scriptArgs,
  '--print',
  '--sdk-url', sdkUrl,
  '--session-id', sessionId,
  '--input-format', 'stream-json',
  '--output-format', 'stream-json',
  '--replay-user-messages',
]
const env = {
  CLAUDE_CODE_ENVIRONMENT_KIND: 'bridge',
  CLAUDE_CODE_SESSION_ACCESS_TOKEN: opts.accessToken,
  CLAUDE_CODE_POST_FOR_SESSION_INGRESS_V2: '1',  // v1
  CLAUDE_CODE_USE_CCR_V2: '1',                    // v2
  CLAUDE_CODE_WORKER_EPOCH: String(workerEpoch),  // v2
}
```

**Парсинг stdout** — NDJSON. `extractActivities()` (`sessionRunner.ts:107`) маппит:
- `assistant.content[].type === 'tool_use'` → `tool_start` activity
- `assistant.content[].type === 'text'` → `text` activity
- `result.subtype === 'success'` → `result` activity

**Tool verbs** (`sessionRunner.ts:70`):
```typescript
const TOOL_VERBS = {
  Read: 'Reading', Write: 'Writing', Edit: 'Editing',
  Bash: 'Running', Glob: 'Searching', Grep: 'Searching',
  WebFetch: 'Fetching', WebSearch: 'Searching', Task: 'Running task',
}
```

**Token refresh через stdin** (`sessionRunner.ts:527`):
```typescript
handle.writeStdin(JSON.stringify({
  type: 'update_environment_variables',
  variables: { CLAUDE_CODE_SESSION_ACCESS_TOKEN: token },
}) + '\n')
```

### 4.4. sessionIdCompat.ts — Ретэгинг ID

v2 CCR отдаёт `cse_*` (infrastructure layer), но compat API (/v1/sessions/) требует `session_*`:

```typescript
function toCompatSessionId(id: string): string {
  if (!id.startsWith('cse_')) return id
  return 'session_' + id.slice('cse_'.length)
}
function toInfraSessionId(id: string): string {
  if (!id.startsWith('session_')) return id
  return 'cse_' + id.slice('session_'.length)
}
```

Kill-switch: `tengu_bridge_repl_v2_cse_shim_enabled` (default `true`).

---

## 5. Messaging — обмен сообщениями

### 5.1. bridgeMessaging.ts — маршрутизация

**handleIngressMessage()** (`bridgeMessaging.ts:132`) — парсинг входящих WebSocket-сообщений:

```
1. normalizeControlMessageKeys(JSON.parse(data))
2. isSDKControlResponse? → onPermissionResponse
3. isSDKControlRequest? → onControlRequest (initialize, set_model, interrupt...)
4. isSDKMessage? → echo dedup (recentPostedUUIDs) → re-delivery dedup (recentInboundUUIDs)
5. type === 'user' → onInboundMessage (fire-and-forget)
```

**BoundedUUIDSet** (`bridgeMessaging.ts:429`) — FIFO ring buffer для echo-dedup:
- Capacity: 2000 (configurable в v2 через `uuid_dedup_buffer_size`)
- O(1) add/has, O(capacity) memory
- Evicts oldest при переполнении

**isEligibleBridgeMessage()** (`bridgeMessaging.ts:77`) — фильтр для пересылки:
- `user` или `assistant` (не virtual)
- `system` с `subtype === 'local_command'`
- Остальное (tool_result, progress) — internal REPL chatter

### 5.2. Server control requests (`bridgeMessaging.ts:243`)

`handleServerControlRequest()` обрабатывает inbound control_request от сервера:

| Subtype | Действие |
|---------|----------|
| `initialize` | Respond: commands=[], models=[], account={}, pid |
| `set_model` | `onSetModel(model)` → success |
| `set_max_thinking_tokens` | `onSetMaxThinkingTokens(tokens)` → success |
| `set_permission_mode` | `onSetPermissionMode(mode)` → success/error |
| `interrupt` | `onInterrupt()` → success |
| unknown | error response |

**Outbound-only mode**: все мутирующие запросы (кроме `initialize`) отвергаются с ошибкой `"This session is outbound-only"`.

### 5.3. inboundMessages.ts — нормализация входящих

`extractInboundMessageFields()` — извлекает content + UUID из SDKMessage.

`normalizeImageBlocks()` — фикс для iOS/web-клиентов, которые шлют `mediaType` (camelCase) вместо `media_type` (snake_case). Без нормализации все последующие API-вызовы ломаются: `"media_type: Field required"`.

### 5.4. inboundAttachments.ts — файловые вложения

Пользователь загружает файл через claude.ai → `file_uuid` приходит с сообщением → bridge скачивает и сохраняет локально:

```
1. extractInboundAttachments(msg) → [{file_uuid, file_name}]
2. GET /api/oauth/files/{uuid}/content (OAuth, timeout 30s)
3. Записать в ~/.claude/uploads/{sessionId}/{prefix}-{safeName}
4. Вернуть @"path" refs для prepend к content
```

`prependPathRefs()` (`inboundAttachments.ts:142`) — вставляет `@"path"` в **последний** text-блок (не первый!), потому что `processUserInputBase` читает `processedBlocks[last]`.

---

## 6. REPL Bridge — in-process транспорт

### 6.1. replBridgeTransport.ts — абстракция транспорта

```typescript
type ReplBridgeTransport = {
  write(message: StdoutMessage): Promise<void>
  writeBatch(messages: StdoutMessage[]): Promise<void>
  close(): void
  isConnectedStatus(): boolean
  getLastSequenceNum(): number    // SSE high-water mark
  droppedBatchCount: number       // v1: maxConsecutiveFailures drops
  reportState(state: SessionState): void   // v2: PUT /worker state
  reportDelivery(eventId: string, status: 'processing' | 'processed'): void
  flush(): Promise<void>          // v2: drain write queue
}
```

**v1 adapter** (`createV1ReplTransport`, строка 78):
- Обёртка над `HybridTransport` (WS reads + POST writes)
- `getLastSequenceNum()` → всегда 0 (WS не использует SSE sequence numbers)
- `reportState/reportDelivery/flush` → no-op

**v2 adapter** (`createV2ReplTransport`, строка 119):
- `SSETransport` (reads) + `CCRClient` (writes через `SerialBatchEventUploader`)
- `registerWorker()` → получает epoch
- `getAuthToken` closure → не пишет JWT в `process.env` (безопасность для MCP-серверов)
- `onEpochMismatch` (409) → close transport → `onCloseCb(4090)` → poll-loop recovery
- Init failure → `onCloseCb(4091)`
- Immediate ACK: `setOnEvent` → `reportDelivery('received')` + `reportDelivery('processed')` одновременно (фикс phantom prompts)

### 6.2. FlushGate (flushGate.ts)

State machine для порядка сообщений при initial flush:

```
start() → enqueue() returns true, items queued
end()   → returns queued items for draining
drop()  → discard all (permanent close)
deactivate() → clear flag without dropping (transport replacement)
```

Защищает от interleaving: пока historical messages летят POST-запросом, live messages копятся в очереди.

### 6.3. replBridgeHandle.ts — глобальный указатель

```typescript
let handle: ReplBridgeHandle | null = null
export function setReplBridgeHandle(h: ReplBridgeHandle | null): void
export function getReplBridgeHandle(): ReplBridgeHandle | null
export function getSelfBridgeCompatId(): string | undefined
```

Один bridge на процесс. Tools и slash-команды получают доступ через `getReplBridgeHandle()`.

---

## 7. v2: Env-Less Bridge (remoteBridgeCore.ts)

~1008 строк. Прямое подключение без Environments API.

### 7.1. Lifecycle

```
1. POST /v1/code/sessions         (OAuth)    → session.id (cse_*)
2. POST /v1/code/sessions/{id}/bridge (OAuth) → {worker_jwt, expires_in, api_base_url, worker_epoch}
3. createV2ReplTransport(worker_jwt, worker_epoch) → SSE + CCRClient
4. wireTransportCallbacks → connect
5. Token refresh scheduler → proactive /bridge re-call
6. 401 on SSE → recoverFromAuthFailure → rebuildTransport
```

### 7.2. Transport rebuild (`remoteBridgeCore.ts:477`)

Каждый вызов `/bridge` бампит epoch. При refresh:
1. `flushGate.start()` — queue live writes
2. `transport.getLastSequenceNum()` — сохранить SSE позицию
3. `transport.close()`
4. `createV2ReplTransport` с новыми credentials и `initialSequenceNum`
5. `wireTransportCallbacks()` → `transport.connect()`
6. `drainFlushGate()` — отправить накопленные сообщения

### 7.3. 401 recovery (`remoteBridgeCore.ts:530`)

```
if (authRecoveryInFlight) return   // Prevent double /bridge fetch
authRecoveryInFlight = true
try:
  onAuth401(stale) → getAccessToken()
  fetchRemoteCredentials(sessionId, baseUrl, oauthToken)
  initialFlushDone = false  // Reset for re-flush
  rebuildTransport(fresh, 'auth_401_recovery')
finally:
  authRecoveryInFlight = false
```

### 7.4. Retry helper (`remoteBridgeCore.ts:892`)

```typescript
async function withRetry(fn, label, cfg):
  for attempt 1..max:
    result = await fn()
    if result !== null: return result
    delay = min(base * 2^(attempt-1) + jitter, maxDelay)
    sleep(delay)
  return null
```

---

## 8. Security — безопасность

### 8.1. JWT Utils (jwtUtils.ts)

**decodeJwtPayload()** — декодирует без верификации подписи. Стрипает `sk-ant-si-` префикс:
```typescript
const jwt = token.startsWith('sk-ant-si-') ? token.slice(10) : token
const payload = JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8'))
```

**Token Refresh Scheduler** (`jwtUtils.ts:72`):
- `TOKEN_REFRESH_BUFFER_MS = 5 * 60 * 1000` — рефреш за 5 минут до истечения
- `FALLBACK_REFRESH_INTERVAL_MS = 30 * 60 * 1000` — если не удалось прочитать exp
- `MAX_REFRESH_FAILURES = 3` — лимит consecutive failures
- Generation counter: `nextGeneration(sessionId)` предотвращает in-flight race conditions

### 8.2. Work Secret (workSecret.ts)

`decodeWorkSecret()` — base64url → JSON → валидация:
```typescript
type WorkSecret = {
  version: 1,
  session_ingress_token: string,  // JWT для ack/heartbeat
  api_base_url: string,
  sources: Array<{ type: string, git_info?: {...} }>,
  auth: Array<{ type: string, token: string }>,
  claude_code_args?: Record<string, string>,
  mcp_config?: unknown,
  environment_variables?: Record<string, string>,
  use_code_sessions?: boolean,  // Server-driven CCR v2 selector
}
```

### 8.3. Trusted Device (trustedDevice.ts)

Для ELEVATED security tier на CCR v2. Двухфазный rollout (CLI-side → server-side).

**Enrollment** (`trustedDevice.ts:98`):
```
POST /api/auth/trusted_devices
Body: { display_name: "Claude Code on {hostname} · {platform}" }
Response: { device_token, device_id }
→ Persist to keychain via getSecureStorage().update()
```

Gate: `tengu_sessions_elevated_auth_enforcement`. Enrollment ограничен по времени: `account_session.created_at < 10min` → только во время `/login`.

Token читается мемоизированно (`memoize`): `getSecureStorage().read()` спавнит macOS `security` subprocess (~40ms). Кеш чистится после enrollment и при logout.

### 8.4. Bridge Permission Callbacks (bridgePermissionCallbacks.ts)

```typescript
type BridgePermissionResponse = {
  behavior: 'allow' | 'deny'
  updatedInput?: Record<string, unknown>
  updatedPermissions?: PermissionUpdate[]
  message?: string
}
```

`isBridgePermissionResponse()` — type predicate вместо unsafe `as` cast. Проверяет `'behavior' in value`.

---

## 9. Configuration — конфигурация

### 9.1. Poll Config (pollConfig.ts + pollConfigDefaults.ts)

GrowthBook flag: `tengu_bridge_poll_interval_config`. Refresh каждые 5 минут.

| Параметр | Default | Назначение |
|----------|---------|------------|
| `poll_interval_ms_not_at_capacity` | 2,000 | Поиск работы |
| `poll_interval_ms_at_capacity` | 600,000 (10 мин) | Liveness signal |
| `non_exclusive_heartbeat_interval_ms` | 0 (disabled) | Heartbeat при capacity |
| `multisession_poll_interval_ms_not_at_capacity` | 2,000 | Multi-session |
| `multisession_poll_interval_ms_partial_capacity` | 2,000 | Частичная загрузка |
| `multisession_poll_interval_ms_at_capacity` | 600,000 | Полная загрузка |
| `reclaim_older_than_ms` | 5,000 | Подбор stale work items |
| `session_keepalive_interval_v2_ms` | 120,000 | Keep-alive фреймы |

**Валидация** — Zod schema с защитами:
- `.min(100)` на seek-work интервалах (fat-finger floor)
- `zeroOrAtLeast100` на at-capacity интервалах (0 = disabled, 1-99 rejected)
- Object-level refine: хотя бы один liveness mechanism должен быть включён

### 9.2. Env-Less Bridge Config (envLessBridgeConfig.ts)

GrowthBook flag: `tengu_bridge_repl_v2_config`.

| Параметр | Default | Назначение |
|----------|---------|------------|
| `init_retry_max_attempts` | 3 | Макс. ретраев init |
| `init_retry_base_delay_ms` | 500 | Базовая задержка |
| `heartbeat_interval_ms` | 20,000 | CCRClient heartbeat (server TTL 60s) |
| `heartbeat_jitter_fraction` | 0.1 | Jitter для spread fleet load |
| `token_refresh_buffer_ms` | 300,000 (5 мин) | Buffer до expiry |
| `teardown_archive_timeout_ms` | 1,500 | Archive timeout при shutdown |
| `connect_timeout_ms` | 15,000 | Transport connect deadline |
| `min_version` | '0.0.0' | Минимальная версия CLI |

### 9.3. Capacity Wake (capacityWake.ts)

Примитив для пробуждения poll loop при освобождении capacity:

```typescript
type CapacityWake = {
  signal(): CapacitySignal  // merged: outer abort + capacity wake
  wake(): void              // abort current sleep, arm fresh controller
}
```

`createCapacityWake(outerSignal)` — мержит два AbortController:
- `outerSignal` — shutdown
- `wakeController` — вызывается из `onSessionDone`

---

## 10. Bridge Pointer — crash recovery (bridgePointer.ts)

Файл `bridge-pointer.json` в `~/.claude/projects/{sanitized-cwd}/`:

```typescript
type BridgePointer = {
  sessionId: string
  environmentId: string
  source: 'standalone' | 'repl'
}
```

**TTL**: 4 часа (по `mtime` файла, не по embedded timestamp).

**Lifecycle**:
- Пишется после создания сессии
- Перезаписывается периодически (bumps mtime)
- Удаляется при clean shutdown (non-perpetual)
- Сохраняется при perpetual mode

**Worktree-aware read** (`readBridgePointerAcrossWorktrees`):
1. Fast path: `readBridgePointer(dir)` — текущая директория
2. `getWorktreePathsPortable(dir)` — все worktrees (cap 50)
3. Parallel `readBridgePointer()` для каждого worktree
4. Выбрать freshest (min ageMs)

---

## 11. UI & Status (bridgeUI.ts + bridgeStatusUtil.ts)

### 11.1. BridgeLogger — terminal UI

**Status states**: `'idle' | 'attached' | 'titled' | 'reconnecting' | 'failed'`

**Visual components**:
- QR code (qrcode library, UTF-8 small format)
- Shimmer animation (150ms tick interval, reverse-sweep grapheme-aware)
- Connecting spinner (`BRIDGE_SPINNER_FRAMES`, 150ms interval)
- Multi-session bullet list (per-session title + activity)
- Capacity indicator ("2/4 sessions")
- Tool activity line (30s expiry: `TOOL_DISPLAY_EXPIRY_MS`)
- OSC 8 hyperlinks (clickable session URLs in terminal)

**Status line management** (`bridgeUI.ts:118`):
```typescript
function writeStatus(text: string): void {
  write(text)
  statusLineCount += countVisualLines(text)
}
function clearStatusLines(): void {
  write(`\x1b[${statusLineCount}A`) // cursor up N
  write('\x1b[J')                    // erase to end
  statusLineCount = 0
}
```

### 11.2. URLs

| Функция | Формат |
|---------|--------|
| `buildBridgeConnectUrl` | `{baseUrl}/code?bridge={environmentId}` |
| `buildBridgeSessionUrl` | `{baseUrl}/code/sessions/{sessionId}?bridge={environmentId}` |

---

## 12. Debug (bridgeDebug.ts)

Ant-only fault injection для тестирования recovery paths:

```typescript
type BridgeFault = {
  method: 'pollForWork' | 'registerBridgeEnvironment' | 'reconnectSession' | 'heartbeatWork'
  kind: 'fatal' | 'transient'
  status: number
  errorType?: string
  count: number  // Remaining injections
}
```

`wrapApiForFaultInjection(api)` — Proxy-обёртка. При совпадении метода:
- `fatal` → `BridgeFatalError` (teardown)
- `transient` → `Error` (retry/backoff)

Используется через `/bridge-kick` slash-команду.

---

## 13. IDE Integration — подключение IDE

### 13.1. VS Code / Claude App

Подключается через URL `{baseUrl}/code?bridge={environmentId}`. Web-frontend claude.ai:
- Показывает список environments с badge "2/4 sessions"
- Фильтрует по `worker_type` (`claude_code` vs `claude_code_assistant`)
- Отправляет SDKMessage через Session-Ingress / CCR v2

### 13.2. SpawnMode — режимы мультисессий

| Mode | Поведение | Use Case |
|------|-----------|----------|
| `single-session` | 1 сессия в cwd, bridge закрывается по завершении | Дефолт |
| `worktree` | Каждая сессия в изолированном git worktree | Параллельные PR |
| `same-dir` | Все сессии в одной директории | Быстрый старт |

Gate: `tengu_ccr_bridge_multi_session` (blocking check).

Worktree creation (`bridgeMain.ts:983`):
```typescript
const wt = await createAgentWorktree(`bridge-${safeFilenameId(sessionId)}`)
sessionWorktrees.set(sessionId, {
  worktreePath: wt.worktreePath,
  worktreeBranch: wt.worktreeBranch,
  gitRoot: wt.gitRoot,
  hookBased: wt.hookBased,
})
sessionDir = wt.worktreePath
```

### 13.3. Session Title Derivation

3-фазная логика (`initReplBridge.ts:258`):

1. **Инициализация**: `initialName` → `/rename` (sessionStorage) → last user message → slug fallback (`remote-control-{shortWordSlug}`)
2. **Count 1**: При первом промпте — placeholder (`deriveTitle`: strip tags, first sentence, truncate 50 chars), затем fire-and-forget `generateSessionTitle` (Haiku)
3. **Count 3**: При третьем промпте — re-generate по полной conversation

---

## Верификация: аудит точности документа

Каждое утверждение проверено против исходного кода. Проверено ~80 утверждений.

### Подтверждённые утверждения (выборка)

| Утверждение | Подтверждено | Строка |
|-------------|--------------|--------|
| `DEFAULT_SESSION_TIMEOUT_MS = 24h` | Да | `types.ts:1` |
| `SAFE_ID_PATTERN = /^[a-zA-Z0-9_-]+$/` | Да | `bridgeApi.ts:41` |
| `BETA_HEADER = 'environments-2025-11-01'` | Да | `bridgeApi.ts:38` |
| `BoundedUUIDSet` — ring buffer с capacity | Да | `bridgeMessaging.ts:429-461` |
| `DEFAULT_BACKOFF.connCapMs = 120_000` | Да | `bridgeMain.ts:74` |
| `SPAWN_SESSIONS_DEFAULT = 32` | Да | `bridgeMain.ts:83` |
| `STATUS_UPDATE_INTERVAL_MS = 1_000` | Да | `bridgeMain.ts:82` |
| `TOKEN_REFRESH_BUFFER_MS = 5 * 60 * 1000` | Да | `jwtUtils.ts:52` |
| `FALLBACK_REFRESH_INTERVAL_MS = 30 * 60 * 1000` | Да | `jwtUtils.ts:55` |
| `MAX_REFRESH_FAILURES = 3` | Да | `jwtUtils.ts:58` |
| `BRIDGE_POINTER_TTL_MS = 4 * 60 * 60 * 1000` | Да | `bridgePointer.ts:40` |
| `MAX_WORKTREE_FANOUT = 50` | Да | `bridgePointer.ts:19` |
| `DOWNLOAD_TIMEOUT_MS = 30_000` | Да | `inboundAttachments.ts:25` |
| `TOOL_DISPLAY_EXPIRY_MS = 30_000` | Да | `bridgeStatusUtil.ts:18` |
| `POLL_INTERVAL_MS_NOT_AT_CAPACITY = 2000` | Да | `pollConfigDefaults.ts:13` |
| `POLL_INTERVAL_MS_AT_CAPACITY = 600_000` | Да | `pollConfigDefaults.ts:30` |
| `reclaim_older_than_ms default = 5000` | Да | `pollConfigDefaults.ts:76` |
| `DEFAULT_ENV_LESS_BRIDGE_CONFIG.heartbeat_interval_ms = 20_000` | Да | `envLessBridgeConfig.ts:50` |
| `teardown_archive_timeout_ms default = 1500` | Да | `envLessBridgeConfig.ts:53` |
| `connect_timeout_ms default = 15_000` | Да | `envLessBridgeConfig.ts:55` |
| `uuid_dedup_buffer_size default = 2000` | Да | `envLessBridgeConfig.ts:49` |
| WorkSecret version must be 1 | Да | `workSecret.ts:13-14` |
| `buildSdkUrl` — localhost → ws://v2/, prod → wss://v1/ | Да | `workSecret.ts:41-48` |
| `sameSessionId` — compares body after last `_` | Да | `workSecret.ts:62-73` |
| `buildCCRv2SdkUrl` → `{base}/v1/code/sessions/{id}` | Да | `workSecret.ts:81-87` |
| `registerWorker` → POST /worker/register → epoch | Да | `workSecret.ts:97-127` |
| Trusted device gate: `tengu_sessions_elevated_auth_enforcement` | Да | `trustedDevice.ts:33` |
| Enrollment: POST /api/auth/trusted_devices | Да | `trustedDevice.ts:149` |
| v2 transport close codes: 4090=epoch, 4091=init, 4092=budget | Да | `replBridgeTransport.ts:220,312,365` |
| `normalizeImageBlocks` — fast path returns same array ref | Да | `inboundMessages.ts:55` |
| `prependPathRefs` targets LAST text block | Да | `inboundAttachments.ts:148` |
| Session creation source: `'remote-control'` | Да | `createSession.ts:134` |
| `anthropic-beta: 'ccr-byoc-2025-07-29'` | Да | `createSession.ts:141` |

### Вердикт

**~80 утверждений проверено. Все соответствуют исходному коду.** Документ отражает актуальное состояние Bridge System в кодовой базе Claude Code.
