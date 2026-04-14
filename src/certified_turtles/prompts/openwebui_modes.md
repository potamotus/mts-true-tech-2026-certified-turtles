# Режимы (Deep Research и др.) в Open WebUI

## Рекомендуется: отдельное подключение (как «кнопка» в сайдбаре)

Список моделей **не раздувается**: в корне остаётся обычный `GET /v1/models` (только модели MWS).

1. В Open WebUI: **Настройки → Подключения → Добавить** (или второй OpenAI endpoint).
2. **API Base URL:** `http://<хост>:8000/v1/m/deep_research`  
   (для Docker из контейнера UI: `http://api:8000/v1/m/deep_research`).
3. **API Key:** тот же `MWS_API_KEY`.
4. Сохрани и выбери это подключение в сайдбаре, когда нужен Deep Research; для обычного чата — первое подключение с base `http://…:8000/v1`.

Запросы пойдут на `…/v1/m/deep_research/chat/completions` — режим задаётся **путём**, поле `model` остаётся обычным id MWS.

Другие режимы: замени сегмент на `research`, `coder`, `data_analyst`, `writer` или короткий алиас `deep` (см. `resolve_mode_path_segment`).

**Plain-чат с режимом:** base `http://…:8000/v1/m/deep_research/plain`.

## Опционально: много моделей в одном списке

Переменная окружения **`CT_LIST_MODE_VARIANTS=1`** на сервисе `api`: в `GET /v1/models` к каждой модели MWS добавляются варианты `deep_research::<id>`, … (список может стать очень длинным).

## Вручную в JSON

Поле **`ct_mode`** в теле `chat/completions` или префикс **`[CT_MODE:…]`** в тексте сообщения — для скриптов и отладки.
