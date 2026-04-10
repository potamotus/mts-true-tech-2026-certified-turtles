# MWS GPT API — справочник для агентов

**Сущность документа:** OpenAI-совместимый HTTP API для моделей MWS.  
**Базовый URL:** `https://api.gpt.mws.ru`  
**Аутентификация:** заголовок `Authorization: Bearer YOUR_API_KEY`  
**Экспорт исходника:** 02/10/2025; автор исходного документа: Губанов Антон Дмитриевич.

Используйте этот файл как **единый источник правды** по эндпоинтам и параметрам. Разделы ниже — в порядке: быстрый индекс → эндпоинты → модели → эмбеддинги → промпты.

---

## 1. Индекс эндпоинтов

| Метод | Путь | Назначение |
|-------|------|------------|
| GET | `/v1/models` | Список доступных моделей |
| POST | `/v1/chat/completions` | Диалог с контекстом (роли `system` / `user` / `assistant`) |
| POST | `/v1/completions` | Однопроходовая генерация по `prompt` |
| POST | `/v1/embeddings` | Векторное представление текста |

Полные URL: `https://api.gpt.mws.ru` + путь из таблицы.

### Доступ к моделям (команда / ключ API)

Список моделей в `GET /v1/models` — **глобальный каталог**. Фактический вызов разрешён только для моделей из **allowlist команды**, привязанной к API-ключу. Иначе ответ **`401`** с типом вроде `team_model_access_denied` и перечнем разрешённых `id`. Подставляйте в `model` значение из этого списка или уточняйте у администратора ключа.

**Пример для хакатон-ключа:** в запросах чата/комплишенов удобно использовать `mws-gpt-alpha` (если она есть в выдаче ошибки или в `GET /v1/models` для вашего ключа).

---

## 2. Листинг моделей

### Эндпоинт

- **Метод:** `GET`
- **URL:** `https://api.gpt.mws.ru/v1/models`

### Пример запроса

```bash
curl -X GET "https://api.gpt.mws.ru/v1/models" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

### Пример ответа

```json
{
  "data": [
    {
      "id": "mws-gpt-alpha",
      "object": "model",
      "created": 1677610602,
      "owned_by": "openai"
    },
    {
      "id": "kodify-2.0",
      "object": "model",
      "created": 1677610602,
      "owned_by": "openai"
    },
    {
      "id": "cotype-preview-32k",
      "object": "model",
      "created": 1677610602,
      "owned_by": "openai"
    }
  ]
}
```

---

## 3. Chat Completions (`/v1/chat/completions`)

### Назначение

- Диалоговое взаимодействие.
- Учёт контекста через массив `messages`.
- Подходит для задач с несколькими ролями и историей.

### Эндпоинт

- **Метод:** `POST`
- **URL:** `https://api.gpt.mws.ru/v1/chat/completions`
- **Рекомендуется:** `Content-Type: application/json`

### Пример запроса (curl)

В исходном документе в роли пользователя была опечатка `"use"`; корректное значение — **`user`**.

```bash
curl -X POST "https://api.gpt.mws.ru/v1/chat/completions" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mws-gpt-alpha",
    "messages": [
      {"role": "system", "content": "Ты помощник"},
      {"role": "user", "content": "Привет, как дела?"}
    ],
    "temperature": 0.6
  }'
```

### Пример ответа (сокращённая структура)

Ответ в стиле OpenAI: `id`, `created`, `model`, `object`, `choices[]` с полем `message` (`role`, `content`), блок `usage` с подсчётом токенов. В документе встречались поля вроде `finish_reason`, `tool_calls`, `function_call`, `service_tier`, `prompt_logprobs`.

### Параметры запроса (chat)

| Параметр | Тип / смысл |
|----------|-------------|
| `model` | Идентификатор модели обработки текста |
| `messages` | Массив объектов: `role` ∈ `system`, `user`, `assistant` и строка `content` |
| `temperature` | Случайность: `0.0` — более детерминированно, `1.0` — более случайно |
| `max_tokens` | Максимум токенов в ответе |
| `n` | Целое число: сколько вариантов ответа сгенерировать |
| `presence_penalty` | От `-2.0` до `2.0`: штраф за повтор токенов, уже присутствующих в истории |
| `frequency_penalty` | От `-2.0` до `2.0`: штраф за частые токены |

---

## 4. Completions (`/v1/completions`)

### Назначение

- Генерация / дополнение текста по одному полю `prompt`.
- Завершение предложений и аналогичные задачи без явной ролевой структуры.

### Эндпоинт

- **Метод:** `POST`
- **URL:** `https://api.gpt.mws.ru/v1/completions`

### Пример запроса (curl)

```bash
curl -X POST "https://api.gpt.mws.ru/v1/completions" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mws-gpt-alpha",
    "prompt": "Что такое искусственный интеллект?",
    "temperature": 0.6,
    "max_tokens": 150,
    "top_p": 1,
    "frequency_penalty": 0,
    "presence_penalty": 0,
    "stop": ["\n"]
  }'
```

### Пример ответа (поля)

- В ответе: `id`, `object` (например `text_completion`), `created`, `model`, `choices[]` с полем `text`, `finish_reason`, при необходимости `logprobs`; блок `usage` с `completion_tokens`, `prompt_tokens`, `total_tokens`.

### Параметры запроса (completion)

| Параметр | Тип / смысл |
|----------|-------------|
| `model` | Модель |
| `prompt` | Текст запроса |
| `max_tokens` | Максимум токенов в ответе |
| `temperature` | Как в chat |
| `top_p` | Альтернативный контроль случайности (nucleus sampling) |
| `presence_penalty` | Как в chat |
| `frequency_penalty` | Как в chat |
| `stop` | Условие остановки генерации (например список стоп-последовательностей) |

---

## 5. Embeddings (`/v1/embeddings`)

### Назначение

- Классификация и семантическое представление текста.
- Поиск и сравнение текстов по векторам.

### Эндпоинт

- **Метод:** `POST`
- **URL:** `https://api.gpt.mws.ru/v1/embeddings`

### Пример запроса (curl)

```bash
curl -X POST "https://api.gpt.mws.ru/v1/embeddings" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "bge-m3", "input": "как у тебя дела?"}'
```

### Параметры запроса (embeddings)

| Параметр | Смысл |
|----------|--------|
| `model` | Модель эмбеддингов |
| `input` | Текст для векторизации |

---

## 6. Доступные модели (из документации)

Список имён моделей, явно указанных в материале:

- `mws-gpt-alpha`
- `kodify-2.0`
- `cotype-preview-32k`
- `bge-m3` — для эмбеддингов

В старых материалах встречалось имя **`mts-anya`** — у многих команд к нему **нет доступа**; ориентируйтесь на allowlist в ошибке `team_model_access_denied` или на `GET /v1/models` для вашего ключа. Имена чувствительны к регистру и написанию (`mws-gpt-alpha` ≠ другое имя).

### Оценка токенов (из документа)

- Примерно **2–3 русских слова ≈ 1 токен**.
- Примерно **3–4 английских слова ≈ 1 токен**.

---

## 7. Промпты — кратко для агентов

### Определения

- **Промпт** — вход модели, задающий поведение и формат результата (текст и при необходимости другие модальности в других продуктах).
- **Шаблон промпта** — промпт с плейсхолдерами; подстановка значений даёт конкретный запрос.

### Роли в Chat API

Поддерживаемые роли в `messages`: **`system`**, **`user`**, **`assistant`**.  
Структура с ролями не обязательна; для простых случаев достаточно одного сообщения с ролью `user`.

### Минимальный пример сообщений (JSON)

```json
{
  "model": "gpt-4",
  "messages": [
    {"role": "system", "content": "Ты эксперт по искусственному интеллекту."},
    {"role": "user", "content": "Как работает машинное обучение?"}
  ]
}
```

### Полный curl с промптом (chat)

```bash
curl -X POST "https://api.gpt.mws.ru/v1/chat/completions" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4",
    "messages": [
      {"role": "system", "content": "Ты эксперт по AI."},
      {"role": "user", "content": "Как работает машинное обучение?"}
    ],
    "temperature": 0.7
  }'
```

### Педагогический пример из оригинала (смысл)

- Короткий вопрос «Что такое замок?» может трактоваться как архитектурный объект.
- Уточнение «речь идёт о дверных замках» сужает домен и улучшает релевантность ответа.

**Вывод для агента:** явно задавайте инструкцию, контекст и формат ответа; при неоднозначности уточняйте смысл термина.

---

## 8. Чеклист перед вызовом API

1. Подставить реальный ключ вместо `YOUR_API_KEY`.
2. Для JSON-тел указать `Content-Type: application/json`.
3. Выбрать эндпоинт: чат → `/v1/chat/completions`, одно поле `prompt` → `/v1/completions`, вектор → `/v1/embeddings`.
4. В `messages` использовать `"role": "user"`, не `"use"`.
5. При сомнениях в имени модели — `GET /v1/models`.
