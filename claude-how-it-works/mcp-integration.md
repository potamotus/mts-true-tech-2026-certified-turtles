# Claude Code — Полная архитектура MCP-интеграции

Анализ на основе утёкшего исходного кода Claude Code (`claude-leaked/src/`).

---

## Обзор: MCP в Claude Code

MCP (Model Context Protocol) — протокол, позволяющий Claude Code подключаться к внешним серверам инструментов. Серверы предоставляют tools, resources, prompts и instructions, которые динамически расширяют возможности LLM.

| Компонент | Файл | Назначение |
|-----------|------|------------|
| **Типы и схемы** | `services/mcp/types.ts` | Zod-схемы конфигов, типы подключений |
| **Конфигурация** | `services/mcp/config.ts` | Загрузка, merge, policy-фильтрация |
| **Подключение** | `services/mcp/client.ts` | `connectToServer()`, fetch tools/resources/commands |
| **Connection Manager** | `services/mcp/MCPConnectionManager.tsx` | React-контекст, reconnect/toggle |
| **Управление** | `services/mcp/useManageMCPConnections.ts` | React hook, жизненный цикл |
| **Аутентификация** | `services/mcp/auth.ts` | OAuth flow, ClaudeAuthProvider |
| **Транспорты** | `InProcessTransport.ts`, `SdkControlTransport.ts` | In-process, SDK bridge |
| **MCPTool** | `tools/MCPTool/MCPTool.ts` | Базовый tool-шаблон |
| **McpAuthTool** | `tools/McpAuthTool/McpAuthTool.ts` | Pseudo-tool для OAuth |
| **Resources** | `tools/ListMcpResourcesTool/`, `tools/ReadMcpResourceTool/` | Листинг и чтение ресурсов |
| **Нормализация** | `services/mcp/normalization.ts`, `mcpStringUtils.ts` | Имена серверов/инструментов |
| **Channel** | `channelNotification.ts`, `channelPermissions.ts`, `channelAllowlist.ts` | Каналы (Telegram, Discord и др.) |
| **Elicitation** | `elicitationHandler.ts` | Интерактивные запросы от MCP-серверов |
| **claude.ai MCP** | `claudeai.ts` | Прокси-серверы из организации |
| **Official Registry** | `officialRegistry.ts` | Реестр одобренных MCP URL |
| **Instructions Delta** | `utils/mcpInstructionsDelta.ts` | Инкрементальные MCP-инструкции |

---

## 1. Конфигурация — как серверы определяются

### 7 scopes конфигурации

```typescript
// types.ts:10-20
ConfigScope = 'local' | 'user' | 'project' | 'dynamic' | 'enterprise' | 'claudeai' | 'managed'
```

| Scope | Источник | Приоритет |
|-------|----------|-----------|
| **enterprise** | `managed-mcp.json` (управляемый путь) | Эксклюзивный — если существует, остальные игнорируются |
| **managed** | Управляемый файл ОС | Высший |
| **local** | `~/.claude/projects/<cwd>/settings.json → mcpServers` | Приватный project-specific |
| **project** | `.mcp.json` (traverse от CWD вверх) | В git, shared |
| **user** | `~/.claude/settings.json → mcpServers` | Глобальный пользовательский |
| **dynamic** | `--mcp-config` флаг / SDK `setMcpServers()` | Runtime |
| **claudeai** | API `v1/mcp_servers` через OAuth | Организационные |

### Загрузка из .mcp.json (config.ts)

**Формат файла:**
```json
{
  "mcpServers": {
    "server-name": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem"],
      "env": { "HOME": "${HOME}" }
    }
  }
}
```

**Discovery** (`getMcpConfigsByScope('project')`, строка 908): traverse от CWD до корня файловой системы. Файлы ближе к CWD имеют приоритет (перезаписывают одноимённые серверы из родительских директорий).

### Env var expansion (envExpansion.ts)

Поддерживается синтаксис `${VAR}` и `${VAR:-default}`:

```typescript
// envExpansion.ts:16
value.replace(/\$\{([^}]+)\}/g, (match, varContent) => {
  const [varName, defaultValue] = varContent.split(':-', 2)
  // ...
})
```

Применяется к: `command`, `args`, `env` (stdio), `url`, `headers` (remote). Не применяется к: `sse-ide`, `ws-ide`, `sdk`, `claudeai-proxy`.

### Enterprise — эксклюзивный контроль

```typescript
// config.ts:1082-1096
if (doesEnterpriseMcpConfigExist()) {
  // ТОЛЬКО enterprise серверы, user/project/local — игнорируются
  return { servers: filtered, errors: [] }
}
```

Путь enterprise: `getEnterpriseMcpFilePath()` → `join(getManagedFilePath(), 'managed-mcp.json')`.

### Policy-фильтрация (allowlist/denylist)

**Denylist** (`deniedMcpServers`) — абсолютный приоритет, проверяется первым:
- По имени: `{ serverName: "..." }`
- По команде: `{ serverCommand: ["npx", "..."] }`
- По URL: `{ serverUrl: "https://example.com/*" }` — поддерживает wildcard `*`

**Allowlist** (`allowedMcpServers`) — если задан:
- Пустой `[]` — блокирует всё
- `undefined` — нет ограничений (пропускает всех)
- stdio серверы проверяются по `serverCommand`, remote — по `serverUrl`

```typescript
// config.ts:536-551
function filterMcpServersByPolicy<T>(configs: Record<string, T>): {
  allowed: Record<string, T>; blocked: string[]
}
```

SDK-серверы (`type: 'sdk'`) **всегда** проходят policy — они не управляют реальными процессами.

### Dedup

Три уровня дедупликации:

1. **Plugin vs manual** (`dedupPluginMcpServers`): manual побеждает; между плагинами — first-loaded
2. **claude.ai vs manual** (`dedupClaudeAiMcpServers`): manual побеждает (только enabled manual)
3. **Signature-based**: `stdio:["cmd","arg"]` или `url:https://...` (после unwrap CCR proxy)

```typescript
// config.ts:202-212
function getMcpServerSignature(config: McpServerConfig): string | null {
  const cmd = getServerCommandArray(config)
  if (cmd) return `stdio:${jsonStringify(cmd)}`
  const url = getServerUrl(config)
  if (url) return `url:${unwrapCcrProxyUrl(url)}`
  return null
}
```

---

## 2. Типы транспортов

### 8 типов транспортов

```typescript
// types.ts:23-25
Transport = 'stdio' | 'sse' | 'sse-ide' | 'http' | 'ws' | 'sdk'
// + 'ws-ide' (McpWebSocketIDEServerConfigSchema) и 'claudeai-proxy'
```

| Транспорт | Протокол | Назначение |
|-----------|----------|------------|
| **stdio** | JSON-RPC через stdin/stdout | Локальные серверы (default) |
| **sse** | Server-Sent Events | Удалённые серверы (legacy) |
| **http** | Streamable HTTP (MCP spec 2025-03-26) | Удалённые серверы (новый стандарт) |
| **ws** | WebSocket | Удалённые серверы |
| **sse-ide** | SSE | IDE-расширения (VS Code и др.) |
| **ws-ide** | WebSocket | IDE-расширения (с auth token) |
| **sdk** | Control messages через stdout | SDK in-process серверы |
| **claudeai-proxy** | Streamable HTTP через Anthropic API | Claude.ai организационные |

### stdio (client.ts:944-958)

```typescript
transport = new StdioClientTransport({
  command: finalCommand,
  args: finalArgs,
  env: { ...subprocessEnv(), ...serverRef.env },
  stderr: 'pipe',
})
```

`CLAUDE_CODE_SHELL_PREFIX` env var — позволяет обернуть команду (например, для Docker).

Stderr капчурится в буфер (до 64MB) для отладки. При cleanup — каскадный signal: SIGINT → 100ms → SIGTERM → 400ms → SIGKILL.

### InProcessTransport (InProcessTransport.ts)

Linked pair для серверов, работающих в том же процессе (Chrome MCP, Computer Use):

```typescript
// InProcessTransport.ts:57-63
export function createLinkedTransportPair(): [Transport, Transport] {
  const a = new InProcessTransport()
  const b = new InProcessTransport()
  a._setPeer(b)
  b._setPeer(a)
  return [a, b]
}
```

`send()` доставляет через `queueMicrotask()` — асинхронно, чтобы избежать stack depth.

**Использование:**
```typescript
// client.ts:920-924 (Chrome MCP)
const [clientTransport, serverTransport] = createLinkedTransportPair()
await inProcessServer.connect(serverTransport)
transport = clientTransport
```

### SdkControlTransport (SdkControlTransport.ts)

Bridge между CLI-процессом (MCP Client) и SDK-процессом (MCP Server):

```
CLI → SdkControlClientTransport → control request (stdout) → SDK
SDK → SdkControlServerTransport → control response → CLI
```

```typescript
// SdkControlTransport.ts:60-95
class SdkControlClientTransport implements Transport {
  async send(message: JSONRPCMessage): Promise<void> {
    const response = await this.sendMcpMessage(this.serverName, message)
    this.onmessage?.(response)
  }
}
```

### HTTP/SSE с OAuth (client.ts:619-865)

Общий паттерн для SSE и HTTP:
1. `ClaudeAuthProvider` — OAuth provider
2. `wrapFetchWithStepUpDetection` — обнаружение 403 (step-up auth)
3. `wrapFetchWithTimeout` — 60s timeout per POST, GET без timeout (SSE stream)
4. `getMcpServerHeaders` — статические + динамические заголовки

**Важно:** `eventSourceInit.fetch` для SSE НЕ оборачивается timeout — это long-lived поток.

### claudeai-proxy (client.ts:868-904)

```typescript
const proxyUrl = `${oauthConfig.MCP_PROXY_URL}${oauthConfig.MCP_PROXY_PATH.replace('{server_id}', serverRef.id)}`
const fetchWithAuth = createClaudeAiProxyFetch(globalThis.fetch)
```

`createClaudeAiProxyFetch` автоматически добавляет `Bearer` token и retry при 401.

---

## 3. Connection Manager — жизненный цикл

### MCPConnectionManager (MCPConnectionManager.tsx)

React-компонент, предоставляющий контекст с двумя функциями:

```typescript
// MCPConnectionManager.tsx:8-15
interface MCPConnectionContextValue {
  reconnectMcpServer: (serverName: string) => Promise<{
    client: MCPServerConnection
    tools: Tool[]
    commands: Command[]
    resources?: ServerResource[]
  }>
  toggleMcpServer: (serverName: string) => Promise<void>
}
```

Делегирует `useManageMCPConnections` hook.

### Batched connection (client.ts:552-565)

```typescript
// Локальные серверы — batch по 3 (default)
function getMcpServerConnectionBatchSize(): number {
  return parseInt(process.env.MCP_SERVER_CONNECTION_BATCH_SIZE || '', 10) || 3
}

// Удалённые серверы — batch по 20
function getRemoteMcpServerConnectionBatchSize(): number {
  return parseInt(process.env.MCP_REMOTE_SERVER_CONNECTION_BATCH_SIZE || '', 10) || 20
}
```

### Memoization и reconnect

`connectToServer` мемоизирован через `memoize()` с ключом `getServerCacheKey(name, config)`.

При `onclose`:
```typescript
// client.ts:1383-1397
fetchToolsForClient.cache.delete(name)
fetchResourcesForClient.cache.delete(name)
fetchCommandsForClient.cache.delete(name)
connectToServer.cache.delete(key)
```

Следующий `ensureConnectedClient()` вызов создаст новое подключение.

### Connection timeout

```typescript
// client.ts:456-458
function getConnectionTimeoutMs(): number {
  return parseInt(process.env.MCP_TIMEOUT || '', 10) || 30000  // 30s default
}
```

### Error-driven reconnection (client.ts:1226-1371)

Три триггера переподключения:

1. **Session expired**: HTTP 404 + JSON-RPC `-32001` → `closeTransportAndRejectPending()`
2. **SSE reconnect exhausted**: `"Maximum reconnection attempts"` → close
3. **Terminal errors**: ECONNRESET/ETIMEDOUT/EPIPE/EHOSTUNREACH/ECONNREFUSED — после 3 подряд → close

```typescript
const MAX_ERRORS_BEFORE_RECONNECT = 3
```

### 5 состояний подключения

```typescript
// types.ts:221-227
type MCPServerConnection =
  | ConnectedMCPServer    // Работает
  | FailedMCPServer       // Ошибка подключения
  | NeedsAuthMCPServer    // Требует OAuth
  | PendingMCPServer      // В процессе подключения
  | DisabledMCPServer     // Отключён пользователем
```

---

## 4. Аутентификация — OAuth flow

### ClaudeAuthProvider (auth.ts)

Реализует `OAuthClientProvider` из MCP SDK. Хранит credentials в OS keychain через `getSecureStorage()`.

**Ключ сервера:**
```typescript
// auth.ts:325-341
function getServerKey(serverName, serverConfig): string {
  const hash = createHash('sha256')
    .update(jsonStringify({ type, url, headers }))
    .digest('hex').substring(0, 16)
  return `${serverName}|${hash}`
}
```

### OAuth discovery (auth.ts:256-311)

Порядок:
1. `configuredMetadataUrl` (из `oauth.authServerMetadataUrl` в конфиге) — если задан
2. RFC 9728 → RFC 8414: `discoverOAuthServerInfo()` из MCP SDK
3. Fallback: path-aware `discoverAuthorizationServerMetadata()` для legacy серверов

### OAuth flow (performMCPOAuthFlow)

1. Discover metadata → get client info (DCR если нет)
2. Запуск HTTP-сервера на localhost для callback
3. Открытие браузера с authorization URL
4. Callback → token exchange
5. Сохранение tokens в keychain

**Timeout:** `AUTH_REQUEST_TIMEOUT_MS = 30000` (30s) на каждый OAuth запрос.

### Token refresh

```typescript
// auth.ts — revokeToken()
// RFC 7009: client_id в body (не в Authorization header)
// Fallback: Bearer auth для non-compliant серверов
```

**Нестандартные коды Slack:** `invalid_refresh_token`, `expired_refresh_token`, `token_expired` → нормализуются в `invalid_grant`.

### XAA (Cross-App Access, SEP-990)

```typescript
// types.ts:37-41
// Per-server flag: oauth.xaa = true
// IdP connection details — из settings.xaaIdp (shared)
```

XAA серверы могут тихо re-auth через cached `id_token` без access/refresh token.

### Needs-auth кэш (client.ts:257-316)

```typescript
const MCP_AUTH_CACHE_TTL_MS = 15 * 60 * 1000  // 15 мин
```

Путь: `~/.claude/mcp-needs-auth-cache.json`. Серверы в этом кэше не пытаются подключиться повторно — только через `/mcp`.

### McpAuthTool (McpAuthTool.ts)

Pseudo-tool, заменяющий реальные инструменты сервера, который в состоянии `needs-auth`:

```typescript
// McpAuthTool.ts:49-52
function createMcpAuthTool(serverName, config): Tool {
  // name: mcp__<server>__authenticate
  // При вызове → performMCPOAuthFlow(skipBrowserOpen: true)
  // Возвращает authorization URL модели
}
```

После успешной OAuth → `reconnectMcpServerImpl` → реальные tools заменяют pseudo-tool через prefix `mcp__<server>__*`.

---

## 5. Tool Discovery — как MCP tools становятся доступными

### Нормализация имён

```typescript
// normalization.ts:17-23
function normalizeNameForMCP(name: string): string {
  let normalized = name.replace(/[^a-zA-Z0-9_-]/g, '_')
  // claude.ai серверы: collapse underscores, strip leading/trailing
  if (name.startsWith('claude.ai ')) {
    normalized = normalized.replace(/_+/g, '_').replace(/^_|_$/g, '')
  }
  return normalized
}
```

### Формат имени инструмента

```typescript
// mcpStringUtils.ts:50-52
function buildMcpToolName(serverName, toolName): string {
  return `mcp__${normalizeNameForMCP(serverName)}__${normalizeNameForMCP(toolName)}`
}
// Пример: "mcp__github__search_code"
```

**Известное ограничение:** `__` в имени сервера ломает парсинг. `mcp__my__server__tool` → server=`my`, tool=`server__tool`.

### fetchToolsForClient (client.ts:1743-1998)

LRU-кэш по имени сервера (max 20 entries). Каждый MCP tool оборачивается в стандартный `Tool`:

```typescript
return {
  ...MCPTool,  // базовый шаблон
  name: skipPrefix ? tool.name : fullyQualifiedName,
  mcpInfo: { serverName: client.name, toolName: tool.name },
  isMcp: true,
  searchHint: tool._meta?.['anthropic/searchHint'],
  alwaysLoad: tool._meta?.['anthropic/alwaysLoad'] === true,
  // Annotations → isConcurrencySafe, isReadOnly, isDestructive, isOpenWorld
  isConcurrencySafe() { return tool.annotations?.readOnlyHint ?? false },
  isReadOnly() { return tool.annotations?.readOnlyHint ?? false },
  isDestructive() { return tool.annotations?.destructiveHint ?? false },
  isOpenWorld() { return tool.annotations?.openWorldHint ?? false },
  // Permission → passthrough (MCPTool requires permission)
  async checkPermissions() { return { behavior: 'passthrough' } },
  // Call → через callMCPToolWithUrlElicitationRetry
  async call(args, context) { ... },
  // Display name: "github - search_code (MCP)" или annotations.title
  userFacingName() {
    const displayName = tool.annotations?.title || tool.name
    return `${client.name} - ${displayName} (MCP)`
  },
}
```

### Tool description truncation

```typescript
// client.ts:218
const MAX_MCP_DESCRIPTION_LENGTH = 2048
```

OpenAPI-generated серверы могут отдавать 15-60KB описаний. Обрезается до 2048 символов с `… [truncated]`.

### SDK MCP: skip prefix mode

```typescript
// client.ts:1761-1763
const skipPrefix = client.config.type === 'sdk' &&
  isEnvTruthy(process.env.CLAUDE_AGENT_SDK_MCP_NO_PREFIX)
```

В этом режиме MCP tools могут переопределять builtin tools по имени.

### IDE tools фильтрация

```typescript
// client.ts:568-573
const ALLOWED_IDE_TOOLS = ['mcp__ide__executeCode', 'mcp__ide__getDiagnostics']
function isIncludedMcpTool(tool: Tool): boolean {
  return !tool.name.startsWith('mcp__ide__') || ALLOWED_IDE_TOOLS.includes(tool.name)
}
```

---

## 6. Tool Execution — MCPTool dispatch

### MCPTool (MCPTool.ts)

Базовый шаблон. Все поля `name`, `description`, `prompt`, `call` **перезаписываются** в `fetchToolsForClient`.

```typescript
// MCPTool.ts:27-77
export const MCPTool = buildTool({
  isMcp: true,
  name: 'mcp',
  maxResultSizeChars: 100_000,
  // checkPermissions → 'passthrough' (всегда спрашивает)
})
```

### Вызов инструмента (client.ts:1833-1971)

```typescript
async call(args, context, _canUseTool, parentMessage, onProgress) {
  const MAX_SESSION_RETRIES = 1
  for (let attempt = 0; ; attempt++) {
    try {
      const connectedClient = await ensureConnectedClient(client)
      const mcpResult = await callMCPToolWithUrlElicitationRetry({
        client: connectedClient,
        tool: tool.name,
        args,
        meta: { 'claudecode/toolUseId': toolUseId },
        signal: context.abortController.signal,
        setAppState: context.setAppState,
        onProgress,
        handleElicitation: context.handleElicitation,
      })
      return { data: mcpResult.content, mcpMeta: { _meta, structuredContent } }
    } catch (error) {
      if (error instanceof McpSessionExpiredError && attempt < MAX_SESSION_RETRIES) {
        continue  // Retry с новым подключением
      }
      throw error
    }
  }
}
```

### Timeout

```typescript
// client.ts:211
const DEFAULT_MCP_TOOL_TIMEOUT_MS = 100_000_000  // ~27.8 часов
// Переопределяется через MCP_TOOL_TIMEOUT env var
```

### Progress tracking

Три этапа: `started` → `completed` / `failed`. Передаётся через `onProgress` callback.

### McpAuthError

При 401 во время tool call — `McpAuthError` устанавливает сервер в `needs-auth` state.

---

## 7. Resources — листинг и чтение

### ListMcpResourcesTool (ListMcpResourcesTool.ts)

```typescript
// Input: { server?: string } — опциональный фильтр
// Output: [{ uri, name, mimeType?, description?, server }]
```

Кэш `fetchResourcesForClient` (LRU по имени сервера). Инвалидируется при `onclose` и `resources/list_changed` notification.

```typescript
// ListMcpResourcesTool.ts:84-96
const results = await Promise.all(
  clientsToProcess.map(async client => {
    const fresh = await ensureConnectedClient(client)
    return await fetchResourcesForClient(fresh)
  })
)
```

**Deferred:** `shouldDefer: true` — не загружается до первого использования.

### ReadMcpResourceTool (ReadMcpResourceTool.ts)

```typescript
// Input: { server: string, uri: string }
```

Для blob-содержимого: decode base64 → persist на диск → возвращает путь вместо raw data:

```typescript
// ReadMcpResourceTool.ts:114-138
const persisted = await persistBinaryContent(
  Buffer.from(c.blob, 'base64'), c.mimeType, persistId
)
return { blobSavedTo: persisted.filepath, text: getBinaryBlobSavedMessage(...) }
```

---

## 8. System Prompt Integration — MCP instructions

### Два режима доставки

| Режим | Условие | Механизм |
|-------|---------|----------|
| **Legacy** | `isMcpInstructionsDeltaEnabled() === false` | `DANGEROUS_uncachedSystemPromptSection('mcp_instructions')` — пересчитывается **каждый ход** |
| **Delta** | `isMcpInstructionsDeltaEnabled() === true` | `mcp_instructions_delta` attachment — persist, инкрементальный |

**Gate:** `tengu_basalt_3kr` (GrowthBook) или `USER_TYPE === 'ant'` или env `CLAUDE_CODE_MCP_INSTR_DELTA`.

### Legacy: getMcpInstructions (prompts.ts:579-604)

```typescript
function getMcpInstructions(mcpClients: MCPServerConnection[]): string | null {
  const clientsWithInstructions = connectedClients.filter(c => c.instructions)
  if (clientsWithInstructions.length === 0) return null

  const instructionBlocks = clientsWithInstructions
    .map(client => `## ${client.name}\n${client.instructions}`)
    .join('\n\n')

  return `# MCP Server Instructions

The following MCP servers have provided instructions for how to use their tools and resources:

${instructionBlocks}`
}
```

**Проблема legacy:** `DANGEROUS_uncachedSystemPromptSection` — означает пересчёт каждый ход. Если MCP-сервер подключается между ходами, prompt cache бьётся.

### Delta: getMcpInstructionsDelta (mcpInstructionsDelta.ts:55-130)

Сканирует историю `mcp_instructions_delta` attachments, строит set `announced`. Сравнивает с текущими connected servers с instructions. Возвращает diff:

```typescript
type McpInstructionsDelta = {
  addedNames: string[]      // Новые серверы
  addedBlocks: string[]     // Rendered "## {name}\n{instructions}"
  removedNames: string[]    // Отключённые серверы
}
```

**Client-side instructions:** Помимо server-authored `InitializeResult.instructions`, есть client-side блоки (например, Chrome MCP ToolSearch hint).

### Truncation server instructions

```typescript
// client.ts:1161-1171
if (rawInstructions && rawInstructions.length > MAX_MCP_DESCRIPTION_LENGTH) {
  instructions = rawInstructions.slice(0, MAX_MCP_DESCRIPTION_LENGTH) + '… [truncated]'
}
// MAX_MCP_DESCRIPTION_LENGTH = 2048
```

---

## 9. Elicitation — интерактивные запросы от MCP-серверов

### Два режима elicitation

| Режим | Назначение |
|-------|------------|
| **form** | Структурированная форма с JSON Schema |
| **url** | Открытие URL в браузере (OAuth callback и т.п.) |

### registerElicitationHandler (elicitationHandler.ts:68-212)

Регистрируется на каждом подключённом клиенте:

```typescript
client.setRequestHandler(ElicitRequestSchema, async (request, extra) => {
  // 1. Пробуем elicitation hooks (программный ответ)
  const hookResponse = await runElicitationHooks(serverName, request.params, signal)
  if (hookResponse) return hookResponse

  // 2. Добавляем в UI-очередь через setAppState
  setAppState(prev => ({
    ...prev,
    elicitation: {
      queue: [...prev.elicitation.queue, {
        serverName, requestId, params, signal,
        waitingState, respond: (result) => resolve(result),
      }],
    },
  }))

  // 3. Ждём ответа пользователя
  const rawResult = await response
  // 4. Пост-обработка через result hooks
  return await runElicitationResultHooks(serverName, rawResult, signal, mode)
})
```

### Elicitation completion (URL mode)

Сервер присылает `ElicitationCompleteNotification` с `elicitationId`. Handler ставит `completed: true` на элемент очереди — UI реагирует.

### Hooks

```typescript
// elicitationHandler.ts:214-257
async function runElicitationHooks(serverName, params, signal) {
  const { elicitationResponse, blockingError } = await executeElicitationHooks({
    serverName, message, requestedSchema, signal, mode, url, elicitationId,
  })
  if (blockingError) return { action: 'decline' }
  if (elicitationResponse) return { action, content }
  return undefined  // Пропускаем в UI
}
```

### ElicitResult actions

```typescript
type ElicitResult = {
  action: 'accept' | 'decline' | 'cancel'
  content?: Record<string, unknown>
}
```

---

## 10. Channel Permissions — каналы и разрешения

### Архитектура каналов (channelNotification.ts)

MCP-сервер = канал (Discord, Telegram, SMS), если:
1. Declares `capabilities.experimental['claude/channel']`
2. Sends `notifications/claude/channel` для входящих сообщений

### Gate (gateChannelServer, channelNotification.ts:191-316)

**Порядок проверок:**
1. `capabilities.experimental['claude/channel']` — есть?
2. `isChannelsEnabled()` → `tengu_harbor` GrowthBook gate
3. OAuth auth (API key пользователи заблокированы)
4. Team/Enterprise → `channelsEnabled: true` в managed settings
5. `--channels` в сессии → `findChannelEntry()`
6. Plugin-kind → marketplace verification + allowlist

### Channel allowlist (channelAllowlist.ts)

GrowthBook `tengu_harbor_ledger`:
```typescript
type ChannelAllowlistEntry = { marketplace: string; plugin: string }
```

Org может переопределить через `allowedChannelPlugins` в managed settings.

### Channel permission relay (channelPermissions.ts)

Разрешение инструментов через канал (SMS, Telegram):

```
CC → notifications/claude/channel/permission_request → Channel Server
User → "yes tbxkq" → Channel Server парсит →
  notifications/claude/channel/permission → CC
```

**ID формат:** 5 символов из алфавита без 'l' (25^5 = 9.8M space). FNV-1a hash от toolUseID. Blocklist на нецензурные слова.

```typescript
// channelPermissions.ts:75
export const PERMISSION_REPLY_RE = /^\s*(y|yes|n|no)\s+([a-km-z]{5})\s*$/i
```

### filterPermissionRelayClients (channelPermissions.ts:177-194)

Три условия (все обязательны):
1. `type === 'connected'`
2. В allowlist сессии
3. Declares ОБОИХ capabilities: `claude/channel` И `claude/channel/permission`

---

## 11. claude.ai MCP серверы (claudeai.ts)

### Fetch (claudeai.ts:39-134)

```typescript
const response = await axios.get<ClaudeAIMcpServersResponse>(
  `${baseUrl}/v1/mcp_servers?limit=1000`,
  { headers: { Authorization: `Bearer ${accessToken}` } }
)
```

**Условия:**
- `ENABLE_CLAUDEAI_MCP_SERVERS` env не `false`
- OAuth token с scope `user:mcp_servers`
- Timeout: 5000ms

**Naming:** `"claude.ai ${display_name}"` → нормализуется. При коллизиях: `(2)`, `(3)` и т.д.

**Мемоизация:** один fetch за сессию.

### claudeai-proxy transport

Подключение через Anthropic MCP proxy:
```typescript
// client.ts:880
const proxyUrl = `${oauthConfig.MCP_PROXY_URL}${oauthConfig.MCP_PROXY_PATH.replace('{server_id}', serverRef.id)}`
```

OAuth retry: `createClaudeAiProxyFetch` → bearer token + retry при 401 через `handleOAuth401Error`.

### markClaudeAiMcpConnected (claudeai.ts:154-160)

Трекинг "когда-либо подключённых" серверов в global config. Используется для уведомлений: сервер, который работал вчера и сегодня failed — state change; сервер в needs-auth с первого дня — ignored.

---

## 12. Official Registry (officialRegistry.ts)

### Prefetch

```typescript
// officialRegistry.ts:33-60
async function prefetchOfficialMcpUrls(): Promise<void> {
  const response = await axios.get<RegistryResponse>(
    'https://api.anthropic.com/mcp-registry/v0/servers?version=latest&visibility=commercial',
    { timeout: 5000 },
  )
  // Собираем все URL из entry.server.remotes[].url
  // Нормализация: strip query, strip trailing slash
  officialUrls = new Set(urls)
}
```

**Отключается:** `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` env var.

### Проверка

```typescript
function isOfficialMcpUrl(normalizedUrl: string): boolean {
  return officialUrls?.has(normalizedUrl) ?? false  // Fail-closed: undefined → false
}
```

---

## 13. Headers Helper (headersHelper.ts)

Динамические заголовки через внешний скрипт:

```typescript
// headersHelper.ts:61-77
const execResult = await execFileNoThrowWithCwd(config.headersHelper, [], {
  shell: true,
  timeout: 10000,
  env: {
    ...process.env,
    CLAUDE_CODE_MCP_SERVER_NAME: serverName,
    CLAUDE_CODE_MCP_SERVER_URL: config.url,
  },
})
```

**Security:** Project/local scope → проверяется workspace trust (`checkHasTrustDialogAccepted()`). Non-interactive mode — пропускает проверку.

**Merge:** Dynamic headers переопределяют static: `{ ...staticHeaders, ...dynamicHeaders }`.

---

## 14. Ключевые типы и константы

### Типы подключений

```typescript
// types.ts:180-227
type ConnectedMCPServer = {
  client: Client             // MCP SDK Client
  name: string
  type: 'connected'
  capabilities: ServerCapabilities
  serverInfo?: { name, version }
  instructions?: string      // InitializeResult.instructions (max 2048 chars)
  config: ScopedMcpServerConfig
  cleanup: () => Promise<void>
}

type FailedMCPServer = { name, type: 'failed', config, error? }
type NeedsAuthMCPServer = { name, type: 'needs-auth', config }
type PendingMCPServer = { name, type: 'pending', config, reconnectAttempt?, maxReconnectAttempts? }
type DisabledMCPServer = { name, type: 'disabled', config }
```

### Ключевые константы

| Константа | Значение | Файл |
|-----------|----------|------|
| `DEFAULT_MCP_TOOL_TIMEOUT_MS` | 100,000,000 (~27.8ч) | client.ts:211 |
| `MAX_MCP_DESCRIPTION_LENGTH` | 2048 | client.ts:218 |
| `MCP_REQUEST_TIMEOUT_MS` | 60,000 (60s) | client.ts:463 |
| `MCP_AUTH_CACHE_TTL_MS` | 900,000 (15мин) | client.ts:257 |
| `AUTH_REQUEST_TIMEOUT_MS` | 30,000 (30s) | auth.ts:65 |
| `MAX_ERRORS_BEFORE_RECONNECT` | 3 | client.ts:1228 |
| `MAX_SESSION_RETRIES` | 1 | client.ts:1859 |
| Connection timeout | 30,000 (30s) | client.ts:457 |
| MCP batch (local) | 3 | client.ts:553 |
| MCP batch (remote) | 20 | client.ts:558 |
| MCP fetch cache size | 20 | client.ts:1726 |
| `FETCH_TIMEOUT_MS` (claudeai) | 5000 | claudeai.ts:30 |
| `maxResultSizeChars` (MCPTool) | 100,000 | MCPTool.ts:35 |

### Feature gates

| Gate | Назначение |
|------|------------|
| `tengu_harbor` | Channels on/off |
| `tengu_harbor_permissions` | Channel permission relay |
| `tengu_harbor_ledger` | Channel allowlist |
| `tengu_basalt_3kr` | MCP instructions delta mode |
| `MCP_SKILLS` | MCP skills fetching |
| `CHICAGO_MCP` | Computer Use MCP server |

---

## 15. Верификация: аудит точности документа

Каждое утверждение проверено против исходного кода. Проверено ~70 утверждений.

### Подтверждённые утверждения (ключевые)

| Утверждение | Подтверждено | Строка |
|-------------|--------------|--------|
| 7 ConfigScope значений | Да | types.ts:11-19 |
| 8 типов транспортов (с ws-ide и claudeai-proxy) | Да | types.ts:23-134 |
| `connectToServer` мемоизирован через `memoize(fn, getServerCacheKey)` | Да | client.ts:595,1640 |
| Connection timeout default 30s | Да | client.ts:457 |
| MCP tool timeout default ~27.8h | Да | client.ts:211 |
| `MAX_MCP_DESCRIPTION_LENGTH = 2048` | Да | client.ts:218 |
| `InProcessTransport` deliver через `queueMicrotask` | Да | InProcessTransport.ts:32 |
| `SdkControlClientTransport` — CLI-side bridge | Да | SdkControlTransport.ts:60-95 |
| Enterprise MCP — exclusive control | Да | config.ts:1082-1096 |
| Denylist → absolute priority, checked first | Да | config.ts:422 |
| Project .mcp.json: traverse от CWD вверх, ближе = приоритет | Да | config.ts:910-955 |
| `expandEnvVarsInString`: `${VAR}` и `${VAR:-default}` | Да | envExpansion.ts:16 |
| Dedup: manual > plugin > claude.ai | Да | config.ts:223-310 |
| `normalizeNameForMCP`: `[^a-zA-Z0-9_-]` → `_`; claude.ai collapse | Да | normalization.ts:17-23 |
| `buildMcpToolName`: `mcp__${norm(server)}__${norm(tool)}` | Да | mcpStringUtils.ts:50-52 |
| `fetchToolsForClient` — LRU cache, max 20 | Да | client.ts:1726,1996-1997 |
| `tool.annotations?.readOnlyHint` → `isConcurrencySafe`/`isReadOnly` | Да | client.ts:1795-1799 |
| McpAuthTool: name = `mcp__<server>__authenticate` | Да | McpAuthTool.ts:63 |
| `ListMcpResourcesTool.shouldDefer = true` | Да | ListMcpResourcesTool.ts:50 |
| `ReadMcpResourceTool`: blob → persist to disk | Да | ReadMcpResourceTool.ts:106-138 |
| `getMcpInstructions`: `# MCP Server Instructions` header, `## {name}` blocks | Да | prompts.ts:599-603 |
| `mcp_instructions` is `DANGEROUS_uncachedSystemPromptSection` | Да | prompts.ts:513-519 |
| `getMcpInstructionsDelta`: diff на server NAME, не content | Да | mcpInstructionsDelta.ts:53 |
| Elicitation: form / url modes | Да | elicitationHandler.ts:49-51 |
| `ElicitationCompleteNotification` → `completed: true` flag | Да | elicitationHandler.ts:175-207 |
| Channel: `notifications/claude/channel` notification method | Да | channelNotification.ts:38-46 |
| Permission reply: 5 chars, no 'l', 25^5 space | Да | channelPermissions.ts:77-78 |
| Official registry: `api.anthropic.com/mcp-registry/v0/servers` | Да | officialRegistry.ts:39 |
| claudeai: `v1/mcp_servers?limit=1000`, scope `user:mcp_servers` | Да | claudeai.ts:66,78-79 |
| `headersHelper`: timeout 10s, env `CLAUDE_CODE_MCP_SERVER_NAME/URL` | Да | headersHelper.ts:61-71 |
| `MCP_AUTH_CACHE_TTL_MS = 15 * 60 * 1000` (15 мин) | Да | client.ts:257 |
| `MAX_ERRORS_BEFORE_RECONNECT = 3` | Да | client.ts:1228 |

### Вердикт

~70 утверждений проверено, 0 неточностей найдено. Документ отражает актуальное состояние кодовой базы.
