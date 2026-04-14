# GPTHub / Certified Turtles — спецификация для доработок (Cloud Code)

Контекст: Open WebUI (fork) → FastAPI `certified_turtles` → MWS GPT. Режимы чата: поле JSON `ct_mode` в `POST …/chat/completions`.

## Уже сделано в репозитории (проверить при мерже)

- Кнопки режимов в `MessageInput.svelte`: `deep_research`, `research`, `presentation`, `coder`, `data_analyst`, `writer` → `ct_mode`.
- Режим `presentation` + `prompts/modes/presentation.md`.
- Сужение `execute_python` для текстовых исследований: `intent_execute_python_system.md`, `protocol_spec.md`, `modes/writer.md`.

## Потенциальные проблемы и техдолг

### Продукт / UX

1. **Мобильная строка режимов** — много кнопок; на узком экране возможен перенос/обрезка. Идеи: выпадающее меню «Режим», сохранение последнего режима в localStorage, i18n ключи вместо хардкода tooltip.
2. **Конфликт с Open WebUI Code Interpreter** — встроенный «Code Interpreter» и режим «Код» (`ct_mode: coder`) разные; пользователь может путать. Нужен onboarding/tooltip или переименование.
3. **Режим «Текст» vs обычный чат** — `writer` отключает веб; для «исследования без Deep Research» пользователь может хотеть `research`. Документировать в UI.
4. **Презентации** — `generate_presentation` требует валидных `image_url`; модель может подставить непрямые URL → слайд с картинкой падает. Валидация URL или fallback без image-слайдов.
5. **Долгие запросы** — агент без потокового прогресса в UI; только `docker compose logs -f api`. Варианты: SSE-события статуса (нестандарт для OpenAI), или отдельная панель «статус» через WebSocket.

### Бэкенд / агент

6. **JSON-протокол** — длинный `assistant_markdown` режет JSON → repair-циклы. Уже есть `CT_AGENT_JSON_MAX_COMPLETION_TOKENS`; при очень длинных отчётах Deep Research возможны обрывы.
7. **DuckDuckGo** — лимиты, CAPTCHA, нестабильная выдача; нет «настоящего» краулинга. Альтернативы: отдельный search API, кеширование.
8. **`fetch_url`** — таймауты, JS-сайты без текста, блокировки ботов. Нет рендеринга браузером.
9. **`execute_python`** — песочница ограничена; пользовательский код с pandas «для вида» раньше проходил в markdown — смягчено промптами; регрессии проверять на задачах «посчитай CSV».
10. **Human-in-the-loop** — отсутствует; долгая «точка» = долгий LLM. Явное сообщение в UI «идёт многошаговый агент» — только на стороне fork.
11. **Под-агенты** — вложенность `max_delegate_depth`; ошибки вложенного протокола сложно показывать пользователю.

### Безопасность

12. **Прокси ключ MWS** — ключ в env Open WebUI; SSRF через `fetch_url` к внутренним IP — проверить политику URL в `fetch_url`.
13. **Загрузки файлов** — квоты, типы MIME, path traversal в `read_workspace_file`.

### Сборка / DevOps

14. **Docker: vite build OOM** — см. `VITE_LOW_MEM_BUILD`, лимит RAM Docker.
15. **`uv run` при старте API** — задержка до первого запроса; health-check с retry.

### Тесты

16. E2E: сценарий «исследование + режим Текст» без `execute_python` в calls.
17. Контракт: каждый новый `ct_mode` имеет `prompts/modes/<id>.md` и запись в `_MODE_MAX_ROUNDS` / алиасы.

---

## Формат задач для агента

- Одна задача = один пункт выше + критерий готовности.
- После правок: `uv run pytest`, при изменении fork — `npm run check` / сборка образа `open-webui`.
