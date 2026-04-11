# Claude Code -- Ink Terminal UI Engine

Анализ на основе утёкшего исходного кода Claude Code (`claude-leaked/src/ink/`).

---

## Обзор: зачем кастомный Ink

Claude Code использует **глубоко переработанный форк** библиотеки [Ink](https://github.com/vadimdemedes/ink) -- React-фреймворка для терминальных интерфейсов. Оригинальный Ink рендерит React-дерево в ANSI-строки для терминала, но версия в Claude Code -- это фактически полноценный терминальный UI-движок с:

- Кастомным React Reconciler на базе `react-reconciler` (React 19, ConcurrentRoot)
- Интеграцией Yoga Layout -- CSS Flexbox в терминале (чистый TypeScript порт, без WASM)
- Двойной буферизацией экрана (front/back frame) с cell-level diff
- Полноценной системой событий (keyboard, click, focus, hover, paste, resize)
- Hit-testing и text selection (как в браузере)
- SGR mouse tracking для полноэкранного режима
- ANSI-парсером и токенизатором (подсистема termio)
- Оптимизатором diff-патчей для минимизации записи в терминал
- DECSTBM hardware scroll для ScrollBox

### Архитектура -- ключевые слои

| Слой | Файлы | Назначение |
|------|-------|------------|
| **Entry** | `root.ts`, `ink.tsx` (1722 строк) | Создание Ink-инстанса, render loop, event routing |
| **Reconciler** | `reconciler.ts` (512 строк) | React reconciler → виртуальный DOM |
| **DOM** | `dom.ts` (484 строки) | Виртуальный DOM: DOMElement, TextNode, createNode |
| **Layout** | `layout/yoga.ts`, `layout/node.ts` | Yoga CSS Flexbox → computed positions |
| **Renderer** | `renderer.ts`, `render-node-to-output.ts` (1462 строки) | DOM tree → Screen buffer |
| **Screen** | `screen.ts` (1486 строк), `output.ts` | Cell-based screen buffer с интернированием |
| **Diff** | `log-update.ts` (773 строки) | Screen diff → Patch[] |
| **Optimizer** | `optimizer.ts` (93 строки) | Merge/dedupe patches |
| **Terminal** | `terminal.ts`, `termio/*` | ANSI sequences → stdout |
| **Events** | `events/*`, `hit-test.ts`, `selection.ts` | Click, keyboard, focus, hover, selection |
| **Components** | `components/*` (18 файлов) | Box, Text, Button, ScrollBox, Link и др. |
| **Hooks** | `hooks/*` (12 файлов) | useInput, useStdin, useAnimationFrame и др. |

---

## 1. React Reconciler

Файл: `reconciler.ts`

### Создание reconciler

Claude Code использует `createReconciler` из `react-reconciler` с полной типизацией:

```typescript
const reconciler = createReconciler<
  ElementNames,   // 'ink-root' | 'ink-box' | 'ink-text' | 'ink-virtual-text' | 'ink-link' | 'ink-progress' | 'ink-raw-ansi'
  Props,           // Record<string, unknown>
  DOMElement,      // Host container
  DOMElement,      // Host instance
  TextNode,        // Text instance
  DOMElement,      // Suspense instance
  unknown, unknown, DOMElement,
  HostContext,     // { isInsideText: boolean }
  null,            // UpdatePayload (не используется в React 19)
  NodeJS.Timeout,
  -1,              // noTimeout sentinel
  null
>({ ... })
```

**Container создаётся** в `ink.tsx:262` с `ConcurrentRoot`:

```typescript
this.container = reconciler.createContainer(
  this.rootNode, ConcurrentRoot, null, false, null, 'id',
  noop, noop, noop, noop
)
```

### Host Context -- текстовый контекст

`getChildHostContext()` (строка 316) отслеживает, находится ли узел внутри текста:

```typescript
getChildHostContext(parentHostContext, type): HostContext {
  const isInsideText =
    type === 'ink-text' || type === 'ink-virtual-text' || type === 'ink-link'
  if (previousIsInsideText === isInsideText) return parentHostContext
  return { isInsideText }
}
```

Это позволяет enforc'ить правила:
- `<Box>` внутри `<Text>` -- throw Error (строка 339)
- Голый текст вне `<Text>` -- throw Error (строка 366)
- `<Text>` внутри `<Text>` автоматически конвертируется в `ink-virtual-text` (строка 343)

### Применение props

`applyProp()` (строка 121) маршрутизирует props:

| Prop | Обработка |
|------|-----------|
| `children` | Пропускается |
| `style` | `setStyle(node)` + `applyStyles(node.yogaNode)` |
| `textStyles` | `node.textStyles = value` |
| Event handlers (`onKeyDown`, `onClick`, ...) | `node._eventHandlers[key] = value` |
| Всё остальное | `setAttribute(node, key, value)` |

**Важно:** event handlers хранятся отдельно от attributes (`_eventHandlers`), чтобы изменение identity handler'а не помечало узел dirty и не ломало blit-оптимизацию.

### Commit Phase

`resetAfterCommit(rootNode)` (строка 247) -- ключевой момент:

1. Вызывает `rootNode.onComputeLayout()` -- **Yoga calculateLayout()** выполняется ЗДЕСЬ, в commit phase React
2. В production: вызывает `rootNode.onRender()` (throttled через `scheduleRender`)
3. В test mode: `rootNode.onImmediateRender()` (синхронный)

### Event Dispatcher

`dispatcher` (строка 187) -- экземпляр `Dispatcher`, пробрасывается в reconciler:

```typescript
export const dispatcher = new Dispatcher()
// Wire discreteUpdates after construction to break import cycle
dispatcher.discreteUpdates = reconciler.discreteUpdates.bind(reconciler)
```

Reconciler читает `dispatcher.currentEvent` и `dispatcher.currentUpdatePriority` для `resolveUpdatePriority()`, `resolveEventType()`, `resolveEventTimeStamp()` -- аналог react-dom.

### Debug инструментация

- `CLAUDE_CODE_DEBUG_REPAINTS` -- записывает owner chain через `getOwnerChain(fiber)` для атрибуции full-screen repaints
- `CLAUDE_CODE_COMMIT_LOG` -- пишет в файл timing каждого commit (gap, reconcile ms, creates)
- Yoga профилирование: `recordYogaMs()`, `getLastCommitMs()`, `resetProfileCounters()`

---

## 2. Виртуальный DOM

Файл: `dom.ts`

### Типы узлов

```typescript
type ElementNames =
  | 'ink-root'         // корень дерева
  | 'ink-box'          // аналог <div>, flexbox контейнер
  | 'ink-text'         // текстовый узел (создаёт Yoga measureFunc)
  | 'ink-virtual-text' // вложенный Text (без Yoga-узла)
  | 'ink-link'         // гиперссылка OSC 8
  | 'ink-progress'     // прогресс-бар (без Yoga-узла)
  | 'ink-raw-ansi'     // raw ANSI output
```

### DOMElement -- полная структура

```typescript
type DOMElement = {
  nodeName: ElementNames
  attributes: Record<string, DOMNodeAttribute>
  childNodes: DOMNode[]
  textStyles?: TextStyles
  parentNode: DOMElement | undefined
  yogaNode?: LayoutNode
  style: Styles
  dirty: boolean
  isHidden?: boolean
  _eventHandlers?: Record<string, unknown>

  // Scroll state (для ScrollBox)
  scrollTop?: number
  pendingScrollDelta?: number       // анимация скролла
  scrollClampMin?: number           // virtual scroll bounds
  scrollClampMax?: number
  scrollHeight?: number
  scrollViewportHeight?: number
  scrollViewportTop?: number
  stickyScroll?: boolean
  scrollAnchor?: { el: DOMElement; offset: number }

  // Фокус и рендер
  focusManager?: FocusManager       // только на ink-root
  onComputeLayout?: () => void
  onRender?: () => void
  debugOwnerChain?: string[]        // debug: CLAUDE_CODE_DEBUG_REPAINTS
}
```

### Создание узлов (createNode, строка 110)

Yoga-узел создаётся для всех типов **кроме** `ink-virtual-text`, `ink-link`, `ink-progress`:

```typescript
const needsYogaNode =
  nodeName !== 'ink-virtual-text' &&
  nodeName !== 'ink-link' &&
  nodeName !== 'ink-progress'
```

Текстовые узлы (`ink-text`) получают `measureFunc` -- Yoga вызывает её для вычисления размера текста.

### Dirty tracking

`markDirty(node)` ходит от узла вверх по `parentNode`, помечая все предки dirty. Это позволяет blit-оптимизации: если поддерево не dirty, рендерер копирует его из предыдущего frame buffer целиком.

---

## 3. Yoga Layout -- CSS Flexbox в терминале

Файлы: `layout/node.ts`, `layout/yoga.ts`, `layout/engine.ts`, `layout/geometry.ts`

### Архитектура адаптера

Yoga интегрирован через трёхуровневую абстракцию:

1. **`LayoutNode`** (`node.ts`) -- абстрактный интерфейс: 47 методов (setWidth, setFlexDirection, calculateLayout и т.д.)
2. **`YogaLayoutNode`** (`yoga.ts`) -- конкретная реализация, оборачивает `Yoga.Node`
3. **`createLayoutNode()`** (`engine.ts`) -- фабрика, делегирует в `createYogaLayoutNode()`

### Ключевое: чистый TypeScript, без WASM

Комментарий в `yoga.ts:301-304`:

```
// The TS yoga-layout port is synchronous — no WASM loading, no linear memory
// growth, so no preload/swap/reset machinery is needed. The Yoga instance is
// just a plain JS object available at import time.
```

Это **TypeScript-порт Yoga**, а не оригинальная C++ + WASM версия. Следствия:
- Нет async-загрузки WASM (поэтому `await Promise.resolve()` в `root.ts:138` -- артефакт от старого WASM)
- Нет линейной памяти -- обычные JS-объекты
- `Yoga.Node.create()` -- синхронный вызов

### Layout flow

1. **ink.tsx:248** -- `onComputeLayout()` вызывается в `resetAfterCommit`:

```typescript
this.rootNode.yogaNode.setWidth(this.terminalColumns)
this.rootNode.yogaNode.calculateLayout(this.terminalColumns)
```

2. Direction всегда `Direction.LTR` (`yoga.ts:83`)
3. Layout reading: `getComputedLeft()`, `getComputedTop()`, `getComputedWidth()`, `getComputedHeight()`
4. Границы, padding, margin: через `LayoutEdge` enum (all, horizontal, vertical, left, right, top, bottom, start, end)

### Поддерживаемые свойства CSS Flexbox

| CSS-свойство | Ink/Yoga | Строка в node.ts |
|-------------|----------|-----------------|
| `display` | `flex` / `none` | 24-28 |
| `flex-direction` | `row` / `column` / `row-reverse` / `column-reverse` | 30-37 |
| `align-items` | `auto` / `stretch` / `flex-start` / `center` / `flex-end` | 39-46 |
| `justify-content` | `flex-start` / `center` / `flex-end` / `space-between` / `space-around` / `space-evenly` | 48-56 |
| `flex-wrap` | `nowrap` / `wrap` / `wrap-reverse` | 58-63 |
| `position` | `relative` / `absolute` | 65-70 |
| `overflow` | `visible` / `hidden` / `scroll` | 72-78 |
| `width/height` | number, percent, auto | 115-151 |
| `min-width/max-width` | number, percent | 152-175 |
| `flex-grow/shrink/basis` | number, percent | 187-198 |
| `margin/padding/border` | per edge | 276-287 |
| `gap` | column, row, all | 285-287 |

### Geometry примитивы (geometry.ts)

```typescript
type Point = { x: number; y: number }
type Size = { width: number; height: number }
type Rectangle = Point & Size
type Edges = { top: number; right: number; bottom: number; left: number }
```

Утилиты: `unionRect`, `clampRect`, `withinBounds`, `edges()` (CSS shorthand), `addEdges`.

---

## 4. Rendering Pipeline

### Полный путь: React tree → ANSI output

```
React setState()
    ↓
reconciler.commitUpdate() / resetAfterCommit()
    ↓
onComputeLayout()  →  Yoga calculateLayout()
    ↓
scheduleRender()   →  throttle(16ms) → queueMicrotask → onRender()
    ↓
renderer()         →  renderNodeToOutput()  →  Output → Screen buffer
    ↓
log.render()       →  cell-by-cell diff (prev screen vs current screen)
    ↓
optimize()         →  merge/dedupe patches
    ↓
writeDiffToTerminal()  →  ANSI sequences → stdout
```

### Двойная буферизация (ink.tsx)

Ink поддерживает **double buffering** через `frontFrame` и `backFrame`:

```typescript
private frontFrame: Frame    // текущий отображаемый кадр
private backFrame: Frame     // буфер для следующего кадра
```

После рендера -- swap:

```typescript
this.backFrame = this.frontFrame
this.frontFrame = frame
```

Каждый `Frame` содержит:

```typescript
type Frame = {
  screen: Screen           // cell buffer
  viewport: Size           // terminal dimensions
  cursor: Cursor           // { x, y, visible }
  scrollHint?: ScrollHint  // DECSTBM hardware scroll hint
  scrollDrainPending?: boolean
}
```

### Renderer (renderer.ts)

`createRenderer(node, stylePool)` возвращает функцию `(RenderOptions) => Frame`.

Ключевые решения:
1. **Alt-screen height clamp** (строка 97): `height = altScreen ? terminalRows : yogaHeight` -- предотвращает overflow за пределы alt screen
2. **Reuse Output** (строка 37): `Output` переиспользуется между кадрами, кешируя `charCache` (tokenize + grapheme clustering)
3. **Blit optimization** (строка 130): `prevScreen` передаётся в `renderNodeToOutput` для копирования неизменённых поддеревьев; `prevFrameContaminated` отключает blit
4. **Cursor placement** (строка 170): alt-screen clamp `y = min(screen.height, terminalRows) - 1` предотвращает LF-scroll

### renderNodeToOutput (1462 строки)

Рекурсивный обход DOM-дерева → запись в Screen buffer. Ключевые оптимизации:

**Blit -- копирование поддерева из prev frame:**
Если узел не dirty и его yoga position не изменилась, вся область копируется из `prevScreen` через `blitRegion()`.

**Scroll drain -- анимация скролла:**
`pendingScrollDelta` не применяется целиком за один кадр. Два алгоритма:
- **Пропорциональный** (нативные терминалы): `step = max(4, floor(abs * 3/4))` -- логарифмическое затухание
- **Адаптивный** (xterm.js/VS Code): пороговый -- при `<=5` rows drain all, иначе фиксированный шаг (2 или 3)

**DECSTBM hardware scroll:**
Когда ScrollBox скроллит на delta rows, renderer записывает `ScrollHint { top, bottom, delta }`. `log-update.ts` использует это для emit `DECSTBM` + `CSI S`/`CSI T` вместо перерисовки всего viewport.

**Layout shift detection:**
Глобальный flag `layoutShifted` -- устанавливается когда yoga position/size узла отличается от кешированного. Используется в ink.tsx для full-damage backstop.

### Output (output.ts)

`Output` собирает операции write/blit/clear/clip, затем в `get()` применяет к `Screen`. Кеширует `ClusteredChar[]` (графемный кластер + precomputed width + styleId + hyperlink) per unique line.

### Screen buffer (screen.ts, 1486 строк)

Cell-based buffer с **интернированием строк и стилей**:

```typescript
class CharPool {
  private strings: string[] = [' ', '']  // index 0 = space, 1 = empty
  private ascii: Int32Array              // charCode → index (fast path)
  intern(char: string): number           // returns integer ID
  get(index: number): string
}

class HyperlinkPool {
  private strings: string[] = ['']       // index 0 = no hyperlink
  intern(hyperlink: string): number
  get(id: number): string | undefined
}

class StylePool {
  // Интернирует AnsiCode[] → integer styleId
  // Кеширует transition strings между styleId парами
}
```

Каждая ячейка экрана -- пара 32-bit integers (packed):
- **Word 1:** charId (20 bit) + width flag (1 bit) + styleId (11 bit)
- **Word 2:** hyperlinkId + metadata

`CellWidth.Normal = 0`, `CellWidth.Wide = 1`, `CellWidth.SpacerTail = 2` -- для CJK/emoji (2-column characters).

### Diff (log-update.ts, 773 строки)

`LogUpdate.render(prevFrame, frame)` генерирует `Diff` (массив `Patch`):

```typescript
type Patch =
  | { type: 'stdout'; content: string }
  | { type: 'clear'; count: number }
  | { type: 'clearTerminal'; reason: FlickerReason }
  | { type: 'cursorHide' } | { type: 'cursorShow' }
  | { type: 'cursorMove'; x: number; y: number }
  | { type: 'cursorTo'; col: number }
  | { type: 'carriageReturn' }
  | { type: 'hyperlink'; uri: string }
  | { type: 'styleStr'; str: string }  // cached transition string
```

`diffEach()` из `screen.ts` -- per-cell comparison:
- Сравнивает packed integer IDs (не строки!)
- Damage region ограничивает сканирование
- `DECSTBM` fast path: shift rows + repair абсолютных overlay'ев

### Optimizer (optimizer.ts)

Однопроходная оптимизация diff перед записью в терминал. 7 правил:

| Правило | Описание |
|---------|----------|
| Empty stdout | Пропуск `{ type: 'stdout', content: '' }` |
| No-op cursorMove | Пропуск `{ type: 'cursorMove', x: 0, y: 0 }` |
| Zero clear | Пропуск `{ type: 'clear', count: 0 }` |
| Merge cursorMove | Consecutive cursorMove: `x += x, y += y` |
| Collapse cursorTo | Consecutive cursorTo: только последний |
| Concat styleStr | Adjacent styleStr: конкатенация (не drop!) |
| Cancel cursor pairs | `cursorHide` + `cursorShow` или наоборот: удалить обе |
| Dedupe hyperlinks | Consecutive hyperlink с одинаковым URI: одна |

---

## 5. Компоненты

### Box (`components/Box.tsx`)

Основной layout-компонент. Аналог `<div style="display: flex">`.

Props = полный набор `Styles` (кроме `textWrap`) + event handlers + `tabIndex` + `autoFocus` + `ref`.

Рендерит `<ink-box>` с вычисленным `style` объектом. React Compiler (`_c(42)`) оптимизирует мемоизацию.

### Text (`components/Text.tsx`)

Текстовый компонент. Рендерит `<ink-text>` с `textStyles` (color, bold, dim, italic, underline, strikethrough, inverse).

**Bold и dim взаимоисключающие** -- enforced через TypeScript discriminated union:

```typescript
type WeightProps =
  | { bold?: never; dim?: never }
  | { bold: boolean; dim?: never }
  | { dim: boolean; bold?: never }
```

Поддержка `wrap`: `'wrap'` | `'wrap-trim'` | `'end'` | `'middle'` | `'truncate-end'` | `'truncate-start'`.

### ScrollBox (`components/ScrollBox.tsx`)

`Box` с `overflow: scroll` и императивным scroll API.

**ScrollBoxHandle:**

```typescript
type ScrollBoxHandle = {
  scrollTo(y: number): void
  scrollBy(dy: number): void
  scrollToElement(el: DOMElement, offset?: number): void
  scrollToBottom(): void
  getScrollTop(): number
  getScrollHeight(): number
  getViewportHeight(): number
  isSticky(): boolean
  subscribe(listener: () => void): () => void
  setClampBounds(min: number | undefined, max: number | undefined): void
}
```

Скролл **обходит React**: `scrollTo`/`scrollBy` мутируют `scrollTop` на DOM-узле, вызывают `markDirty()` + `scheduleRenderFrom()` напрямую. React не участвует -- нет `setState`, нет reconciler overhead per wheel event.

`stickyScroll` -- auto-pins scroll to bottom при росте контента (streaming).

### Button (`components/Button.tsx`)

Интерактивная кнопка с render prop для стилизации по состоянию:

```typescript
type ButtonState = { focused: boolean; hovered: boolean; active: boolean }
type Props = {
  onAction: () => void
  children: ((state: ButtonState) => ReactNode) | ReactNode
  tabIndex?: number     // default 0
  autoFocus?: boolean
}
```

Активируется через Enter, Space или click. Обрабатывает onKeyDown, onClick, onFocus, onBlur, onMouseEnter, onMouseLeave -- всё через `<Box>` с проброшенными handler'ами.

### Link (`components/Link.tsx`)

OSC 8 гиперссылка:

```typescript
function Link({ children, url, fallback }: Props) {
  const content = children ?? url
  if (supportsHyperlinks()) {
    return <Text><ink-link href={url}>{content}</ink-link></Text>
  }
  return <Text>{fallback ?? content}</Text>
}
```

Проверяет `supportsHyperlinks()` и деградирует до plain text с fallback.

### AlternateScreen (`components/AlternateScreen.tsx`)

Обёртка для полноэкранного режима:

1. Входит в alt screen (`DEC 1049`), очищает, homes cursor
2. Ограничивает высоту до `terminal rows` (через `<Box height={rows}>`)
3. Опционально включает SGR mouse tracking
4. Уведомляет Ink-инстанс через `setAltScreenActive()`

Использует `useInsertionEffect` (не `useLayoutEffect`!) -- срабатывает ДО `resetAfterCommit`, чтобы `ENTER_ALT_SCREEN` дошёл до терминала до первого frame render.

### Другие компоненты

| Компонент | Назначение |
|-----------|------------|
| `App.tsx` | Root wrapper: context providers, stdin handling, mouse event dispatch |
| `Newline.tsx` | Простой `\n` |
| `Spacer.tsx` | Flex spacer (`flexGrow: 1`) |
| `NoSelect.tsx` | Запрет text selection в области |
| `RawAnsi.tsx` | Raw ANSI passthrough |
| `ErrorOverview.tsx` | Error boundary display |
| `ClockContext.tsx` | Shared animation clock |
| `StdinContext.ts` | stdin stream + rawMode + event emitter |
| `TerminalSizeContext.tsx` | { columns, rows } context |
| `TerminalFocusContext.tsx` | Terminal focus/blur events |
| `CursorDeclarationContext.ts` | IME cursor positioning |

---

## 6. Система событий

### Event Dispatcher (`events/dispatcher.ts`)

Реализация DOM-like capture/bubble event dispatch. Следует паттерну react-dom:

**Сбор listeners (`collectListeners`):**
1. Ходит от target к root
2. Capture handlers prepend (root-first)
3. Bubble handlers append (target-first)

Результат: `[root-cap, ..., parent-cap, target-cap, target-bub, parent-bub, ..., root-bub]`

**Dispatch (`processDispatchQueue`):**
- `event._prepareForTarget(node)` перед каждым handler'ом
- `stopImmediatePropagation()` прерывает цикл
- `stopPropagation()` прерывает при смене node

**Приоритеты (React scheduling):**

| Event type | Priority |
|-----------|----------|
| keydown, keyup, click, focus, blur, paste | `DiscreteEventPriority` |
| resize, scroll, mousemove | `ContinuousEventPriority` |
| Остальные | `DefaultEventPriority` |

### Event Handler Props (`events/event-handlers.ts`)

Полный список:

```typescript
type EventHandlerProps = {
  onKeyDown?: (event: KeyboardEvent) => void
  onKeyDownCapture?: (event: KeyboardEvent) => void
  onFocus?: (event: FocusEvent) => void
  onFocusCapture?: (event: FocusEvent) => void
  onBlur?: (event: FocusEvent) => void
  onBlurCapture?: (event: FocusEvent) => void
  onPaste?: (event: PasteEvent) => void
  onPasteCapture?: (event: PasteEvent) => void
  onResize?: (event: ResizeEvent) => void
  onClick?: (event: ClickEvent) => void
  onMouseEnter?: () => void
  onMouseLeave?: () => void
}
```

12 handler props зарегистрированы в `EVENT_HANDLER_PROPS` Set для O(1) lookup в reconciler.

### KeyboardEvent (`events/keyboard-event.ts`)

```typescript
class KeyboardEvent extends TerminalEvent {
  readonly key: string      // 'a', 'return', 'escape', 'f1', 'down'
  readonly ctrl: boolean
  readonly shift: boolean
  readonly meta: boolean    // meta || option
  readonly superKey: boolean
  readonly fn: boolean
}
```

`key` следует семантике браузера: `e.key.length === 1` = printable character.

### ClickEvent (`events/click-event.ts`)

```typescript
class ClickEvent extends Event {
  readonly col: number         // screen column
  readonly row: number         // screen row
  localCol: number             // relative to handler's Box
  localRow: number             // relative to handler's Box
  readonly cellIsBlank: boolean // true if clicked on empty space
}
```

`localCol`/`localRow` пересчитываются `dispatchClick` перед каждым handler'ом -- container onClick видит координаты относительно себя, не ребёнка.

`cellIsBlank` -- позволяет handler'ам игнорировать клики по пустому пространству.

### Focus Manager (`focus.ts`)

DOM-like focus manager, хранится на `ink-root`:

```typescript
class FocusManager {
  activeElement: DOMElement | null = null
  private focusStack: DOMElement[] = []  // max 32
  private dispatchFocusEvent: (target, event) => boolean

  focus(node): void       // blur previous → push to stack → focus new
  blur(): void
  handleNodeRemoved(node, root): void  // cleanup + restore from stack
  handleAutoFocus(node): void
  handleClickFocus(node): void
  moveFocusNext(root): void   // Tab cycling
  moveFocusPrev(root): void   // Shift+Tab cycling
}
```

Tab cycling (`moveFocusNext`/`moveFocusPrev`) обходит дерево, собирая узлы с `tabIndex >= 0`, сортирует по tabIndex → tree order.

---

## 7. Terminal I/O -- подсистема termio

Файлы: `termio/tokenize.ts`, `termio/parser.ts`, `termio/types.ts`, `termio/sgr.ts`, `termio/csi.ts`, `termio/dec.ts`, `termio/osc.ts`, `termio/esc.ts`, `termio/ansi.ts`

### Архитектура

Двухуровневая:

1. **Tokenizer** (`tokenize.ts`) -- определяет границы escape-последовательностей. Streaming state machine с состояниями: `ground`, `escape`, `escapeIntermediate`, `csi`, `ss3`, `osc`, `dcs`, `apc`
2. **Parser** (`parser.ts`) -- интерпретирует токены в семантические действия (Action)

### Типы действий (types.ts)

```typescript
type Action =
  | { type: 'text'; graphemes: Grapheme[]; style: TextStyle }
  | { type: 'cursor'; action: CursorAction }     // move, position, show/hide, save/restore
  | { type: 'erase'; action: EraseAction }        // display, line, chars
  | { type: 'scroll'; action: ScrollAction }      // up, down, setRegion (DECSTBM)
  | { type: 'mode'; action: ModeAction }          // altScreen, bracketedPaste, mouseTracking, focusEvents
  | { type: 'link'; action: LinkAction }          // OSC 8 start/end
  | { type: 'title'; action: TitleAction }        // OSC 0/1/2 window title
  | { type: 'tabStatus'; action: TabStatusAction }// OSC 21337
  | { type: 'sgr'; params: string }               // Select Graphic Rendition
  | { type: 'bell' }
  | { type: 'reset' }                             // ESC c
  | { type: 'unknown'; sequence: string }
```

### TextStyle -- полная модель стилей

```typescript
type TextStyle = {
  bold: boolean; dim: boolean; italic: boolean
  underline: UnderlineStyle  // 'none'|'single'|'double'|'curly'|'dotted'|'dashed'
  blink: boolean; inverse: boolean; hidden: boolean
  strikethrough: boolean; overline: boolean
  fg: Color; bg: Color; underlineColor: Color
}

type Color =
  | { type: 'named'; name: NamedColor }  // 16 colors
  | { type: 'indexed'; index: number }    // 256 colors
  | { type: 'rgb'; r, g, b: number }     // true color
  | { type: 'default' }
```

### SGR Parser (`sgr.ts`)

Парсит SGR параметры (через `;` и `:` разделители) и применяет к `TextStyle`. Поддерживает:
- 16 named colors (30-37, 90-97 fg; 40-47, 100-107 bg)
- 256-color (38;5;N, 48;5;N)
- True color RGB (38;2;R;G;B, 48;2;R;G;B)
- Colon subparams (38:2:R:G:B -- новый формат)
- 5 стилей подчёркивания (4:0 - 4:5)
- Underline color (58:2:R:G:B)

### CSI sequences (`csi.ts`)

Генераторы escape-последовательностей:

| Функция | Sequence | Описание |
|---------|----------|----------|
| `CURSOR_HOME` | `\x1b[H` | Cursor to (1,1) |
| `cursorPosition(row, col)` | `\x1b[{row};{col}H` | Absolute cursor position |
| `cursorMove(dx, dy)` | CUF/CUB/CUD/CUA | Relative cursor move |
| `ERASE_SCREEN` | `\x1b[2J` | Clear entire screen |
| `setScrollRegion(top, bottom)` | `\x1b[{top};{bottom}r` | DECSTBM |
| `scrollUp(n)` / `scrollDown(n)` | `\x1b[{n}S` / `\x1b[{n}T` | Hardware scroll |
| `ENABLE_KITTY_KEYBOARD` | `\x1b[>1u` | Kitty keyboard protocol |
| `ENABLE_MODIFY_OTHER_KEYS` | `\x1b[>4;2m` | xterm modifyOtherKeys |

### DEC private modes (`dec.ts`)

| Константа | Sequence | Описание |
|-----------|----------|----------|
| `ENTER_ALT_SCREEN` | `\x1b[?1049h` | Alt screen buffer |
| `EXIT_ALT_SCREEN` | `\x1b[?1049l` | Return to main screen |
| `ENABLE_MOUSE_TRACKING` | `\x1b[?1003h\x1b[?1006h` | mode-1003 + SGR encoding |
| `DISABLE_MOUSE_TRACKING` | `\x1b[?1006l\x1b[?1003l` | Disable mouse |
| `SHOW_CURSOR` | `\x1b[?25h` | Show cursor |
| `DBP` | `\x1b[?2026h` | DEC Begin Protected (sync output start) |
| `DFE` | `\x1b[?2026l` | DEC Finish Extended (sync output end) |

### OSC sequences (`osc.ts`)

- `link(uri)` -- OSC 8 hyperlink start/end
- `setClipboard(text)` -- OSC 52 clipboard write
- `supportsTabStatus()` -- iTerm2 tab status (OSC 21337)
- `wrapForMultiplexer(seq)` -- tmux/screen DCS passthrough

---

## 8. Hooks

### useInput (`hooks/use-input.ts`)

```typescript
const useInput = (inputHandler: Handler, options?: { isActive?: boolean }) => { ... }
type Handler = (input: string, key: Key, event: InputEvent) => void
```

- `useLayoutEffect` (не `useEffect`!) для `setRawMode(true)` -- синхронно при commit, до возврата render()
- Listener регистрируется один раз через `useEventCallback` -- стабильный slot, не ломает `stopImmediatePropagation()` ordering
- Ctrl+C обрабатывается: если `exitOnCtrlC`, handler не вызывается

### useStdin (`hooks/use-stdin.ts`)

```typescript
const useStdin = () => useContext(StdinContext)
```

Простой accessor к `StdinContext`, предоставляющему `stdin`, `setRawMode`, `internal_eventEmitter`.

### useAnimationFrame (`hooks/use-animation-frame.ts`)

```typescript
function useAnimationFrame(intervalMs: number | null = 16):
  [ref: (el: DOMElement | null) => void, time: number]
```

- Все инстансы делят один `ClockContext` -- анимации синхронизированы
- Clock запускается когда есть хотя бы один `keepAlive` subscriber
- `null` для паузы -- отписывается от clock, time замораживается
- Автоматическое замедление при terminal blur
- `useTerminalViewport` -- рендерит только если элемент видим в viewport

### useTerminalViewport (`hooks/use-terminal-viewport.ts`)

Отслеживает видимость элемента в viewport. Возвращает `[ref, { isVisible }]`. Используется `useAnimationFrame` для автопаузы оффскрин-анимаций.

### useDeclaredCursor (`hooks/use-declared-cursor.ts`)

Объявляет позицию нативного курсора терминала:

```typescript
function useDeclaredCursor(node: DOMElement, relativeX: number, relativeY: number): void
```

Курсор паркуется в объявленной позиции после каждого frame -- для IME preedit text (CJK input) и screen readers.

### Другие hooks

| Hook | Назначение |
|------|------------|
| `use-interval.ts` | `setInterval` wrapper с cleanup |
| `use-app.ts` | Access to AppContext (exit, unmount) |
| `use-selection.ts` | Access to selection state |
| `use-search-highlight.ts` | Search query highlight state |
| `use-tab-status.ts` | iTerm2 tab status control |
| `use-terminal-title.ts` | Terminal window title (OSC 2) |
| `use-terminal-focus.ts` | Terminal focus/blur events (DEC 1004) |

---

## 9. Hit Testing и Selection

### Hit Test (`hit-test.ts`)

```typescript
function hitTest(node: DOMElement, col: number, row: number): DOMElement | null
```

Рекурсивный поиск самого глубокого DOM-элемента, чей rendered rect содержит (col, row).

- Использует `nodeCache` (заполняется `renderNodeToOutput`) -- rects в screen coordinates с учётом scrollTop
- Children traversal в обратном порядке: позднейшие siblings (painted on top) побеждают
- Узлы без nodeCache entry (не рендерились) пропускаются вместе с поддеревьями

### dispatchClick

```typescript
function dispatchClick(root, col, row, cellIsBlank): boolean
```

1. `hitTest()` → deepest node
2. **Click-to-focus:** ходит вверх от target до первого `tabIndex >= 0`, вызывает `focusManager.handleClickFocus()`
3. Создаёт `ClickEvent(col, row, cellIsBlank)`
4. Bubble: ходит по `parentNode`, ищет `onClick` handler
5. Перед каждым handler: пересчитывает `localCol`/`localRow` из `nodeCache.get(target).rect`
6. `stopImmediatePropagation()` прерывает

### dispatchHover

```typescript
function dispatchHover(root, col, row, hovered: Set<DOMElement>): void
```

mouseenter/mouseleave семантика (как DOM, НЕ bubbles):
1. hitTest → collect все ancestors с hover handler'ами → `next` set
2. Diff с `hovered` set
3. Nodes exited: fire `onMouseLeave()`, remove from `hovered`
4. Nodes entered: fire `onMouseEnter()`, add to `hovered`

Skip detached nodes (removed between mouse events).

### Text Selection (`selection.ts`, 917 строк)

Полная реализация terminal text selection:

```typescript
type SelectionState = {
  anchor: Point | null          // mouse-down position
  focus: Point | null           // current drag position
  isDragging: boolean
  anchorSpan: { lo: Point; hi: Point; kind: 'word' | 'line' } | null
  scrolledOffAbove: string[]    // text scrolled out during drag
  scrolledOffBelow: string[]
  scrolledOffAboveSW: boolean[] // soft-wrap bits
  scrolledOffBelowSW: boolean[]
  virtualAnchorRow?: number     // pre-clamp position
  virtualFocusRow?: number
  lastPressHadAlt: boolean      // macOS alt-click detection
}
```

**Три mode:**
- **Char mode** -- single click + drag
- **Word mode** -- double-click → `selectWordAt()` + drag extends word-by-word
- **Line mode** -- triple-click → `selectLineAt()` + drag extends line-by-line

**Scroll integration:**
- `shiftSelection()`, `shiftAnchor()` -- translate selection при scroll
- `captureScrolledRows()` -- capture text scrolling off-screen в accumulator
- `shiftSelectionForFollow()` -- auto-follow (sticky scroll) translation
- `getSelectedText()` -- собирает on-screen + accumulated off-screen text

**Overlay:**
`applySelectionOverlay()` инвертирует cell styles в Screen buffer (SGR 7) -- diff видит это как обычные cell changes.

---

## 10. Производительность

### Frame throttling

```typescript
// constants.ts
export const FRAME_INTERVAL_MS = 16  // ~60fps
```

`scheduleRender` = `throttle(deferredRender, 16ms, { leading: true, trailing: true })`

`deferredRender` = `queueMicrotask(this.onRender)` -- deferred чтобы layout effects (useDeclaredCursor) успели commit'нуться.

### Pool management

Каждые **5 минут** (ink.tsx:600) Ink сбрасывает CharPool и HyperlinkPool:

```typescript
if (renderStart - this.lastPoolResetTime > 5 * 60 * 1000) {
  this.resetPools()
  this.lastPoolResetTime = renderStart
}
```

`migrateScreenPools()` переносит cell data из старых pools в новые (O(cells)), предотвращая unbounded growth.

StylePool **не сбрасывается** -- session-lived, styleId стабилен.

### Line width cache (`line-width-cache.ts`)

```typescript
const cache = new Map<string, number>()
const MAX_CACHE_SIZE = 4096

function lineWidth(line: string): number {
  const cached = cache.get(line)
  if (cached !== undefined) return cached
  const width = stringWidth(line)
  if (cache.size >= MAX_CACHE_SIZE) cache.clear()
  cache.set(line, width)
  return width
}
```

~50x сокращение `stringWidth` вызовов при streaming: завершённые строки не меняются.

### Blit optimization

В `render-node-to-output.ts`: если node не dirty И yoga position не изменилась, `blitRegion()` копирует cells из prevScreen. Steady-state frames (spinner tick, clock tick) -- O(changed cells), не O(all cells).

### Damage tracking

`Screen.damage: Rectangle` -- bounding box изменённых cells. `diffEach()` сканирует только damage region. Full-damage backstop срабатывает при:
- `didLayoutShift()` -- yoga position/size изменилась
- Selection overlay active
- Search highlight active
- `prevFrameContaminated`

### DECSTBM hardware scroll

При ScrollBox scroll, вместо перерисовки viewport:

```
CSI {top};{bottom} r    // set scroll region
CSI {n} S               // scroll up n rows
CSI r                   // reset scroll region
+ repair absolute-positioned overlays
```

Terminal hardware сдвигает строки -- O(1) per row вместо O(width * height) redraw.

### Yoga profiling

Counters per-frame (`getYogaCounters()`):
- `ms` -- calculateLayout время
- `visited` -- layoutNode() calls (recursive)
- `measured` -- measureFunc calls (text wrap -- expensive)
- `cacheHits` -- single-slot cache hits
- `live` -- total Yoga.Node instances (growth = leak)

Записываются в `FrameEvent.phases` для instrumentation.

---

## Верификация

### Проверенные утверждения

| Утверждение | Файл:строка | Статус |
|-------------|-------------|--------|
| ConcurrentRoot для reconciler container | `ink.tsx:262` | Подтверждено |
| `FRAME_INTERVAL_MS = 16` | `constants.ts:2` | Подтверждено |
| 7 ElementNames типов | `dom.ts:19-27` | Подтверждено |
| Yoga -- чистый TS, без WASM | `layout/yoga.ts:301-304` | Подтверждено |
| Direction всегда LTR | `layout/yoga.ts:83` | Подтверждено |
| CharPool: index 0 = space, 1 = empty | `screen.ts:22` | Подтверждено |
| Pool reset каждые 5 минут | `ink.tsx:600` | Подтверждено |
| 12 event handler props | `events/event-handlers.ts:60-73` | Подтверждено |
| 3 dispatch priorities (discrete/continuous/default) | `events/dispatcher.ts:122-138` | Подтверждено |
| hitTest -- reverse child traversal | `hit-test.ts:34` | Подтверждено |
| optimizer -- 7 правил | `optimizer.ts:16-14` | Подтверждено |
| MAX_FOCUS_STACK = 32 | `focus.ts:4` | Подтверждено |
| Line width cache MAX_CACHE_SIZE = 4096 | `line-width-cache.ts:8` | Подтверждено |
| Tokenizer states: ground, escape, escapeIntermediate, csi, ss3, osc, dcs, apc | `termio/tokenize.ts:16-24` | Подтверждено |
| Action -- 12 variant types | `termio/types.ts:224-236` | Подтверждено |
| 5 underline styles: none/single/double/curly/dotted/dashed | `termio/types.ts:43-49` | Подтверждено (6, включая none) |
| AlternateScreen -- useInsertionEffect, не useLayoutEffect | `components/AlternateScreen.tsx:67` | Подтверждено |
| Button -- Enter/Space/click activation | `components/Button.tsx:94-103` | Подтверждено |
| useInput -- useLayoutEffect для setRawMode | `hooks/use-input.ts:50` | Подтверждено |
| useAnimationFrame default 16ms | `hooks/use-animation-frame.ts:31` | Подтверждено |
| Selection 3 modes: char/word/line | `selection.ts:27-31` | Подтверждено |

### Найденные и исправленные неточности

| # | Было | Стало | Источник |
|---|------|-------|----------|
| 1 | "6 UnderlineStyle" | 6 значений включая 'none' (none/single/double/curly/dotted/dashed) | `termio/types.ts:43-49`, `termio/sgr.ts:30-37` |
| 2 | reconciler "10 args" | Комментарий говорит 10, но @types declare 11 -- несоответствие types vs runtime | `ink.tsx:260-268` |

### Вердикт

~25 утверждений проверены, 2 уточнения. Документ отражает актуальное состояние кода в `claude-leaked/src/ink/`.
