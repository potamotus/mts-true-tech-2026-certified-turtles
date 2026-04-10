# MWS Tables FUSION API — Полное руководство

## Что такое MWS Tables?

MWS Tables — платформа для работы с данными, аналог **Airtable/Notion**. Позволяет создавать структурированные базы данных с гибкой схемой, разными типами представлений и совместной работой.

**Базовый URL:** `https://tables.mws.ru/fusion/v1/`

**Авторизация:** `Authorization: Bearer usk***`

---

## Иерархия сущностей

```
Space (Пространство)
  └── Node (Элемент)
        ├── Folder (Папка)
        │     └── ... вложенные ноды
        └── Datasheet (Таблица)
              ├── Fields (Поля/Колонки)
              ├── Records (Записи/Строки)
              ├── Views (Представления)
              └── Attachments (Вложения)
```

---

## 1. Space (Пространство)

Корневой контейнер — "рабочее пространство". Объединяет все таблицы, папки и пользователей команды.

### Получить все пространства

```http
GET /fusion/v1/spaces
Authorization: Bearer usk***
```

**Ответ:**
```json
{
  "success": true,
  "code": 200,
  "data": {
    "spaces": [
      { "id": "spcXXX", "name": "Мой проект", "isAdmin": true }
    ]
  }
}
```

---

## 2. Node (Элемент пространства)

Любой объект внутри пространства — папка или таблица. Образует древовидную структуру.

### Типы нод
- `Folder` — папка для организации
- `Datasheet` — таблица с данными

### Получить элементы пространства

```http
GET /fusion/v1/spaces/{spaceId}/nodes
GET /fusion/v1/spaces/{spaceId}/nodes?type=2    # поиск по типу
```

### Получить детали элемента

```http
GET /fusion/v1/nodes/{nodeId}
```

### Удалить элемент

```http
DELETE /fusion/v1/spaces/{spaceId}/node/{nodeId}
```

**Пример структуры:**
```json
{
  "id": "fodXXX",
  "name": "Проекты 2024",
  "type": "Folder",
  "isFav": false,
  "permission": 0,
  "children": [
    { "id": "dstYYY", "name": "Задачи", "type": "Datasheet" }
  ]
}
```

---

## 3. Datasheet (Таблица)

Основная единица хранения данных. Аналог таблицы в Excel, но с типизированными полями.

### Создать таблицу

```http
POST /fusion/v1/spaces/{spaceId}/datasheets
Content-Type: application/json
```

```json
{
  "name": "Мои задачи",
  "description": "Трекер задач проекта",
  "folderId": "fodXXX",
  "fields": [
    { "type": "SingleText", "name": "Название" },
    {
      "type": "SingleSelect",
      "name": "Статус",
      "property": {
        "options": [
          { "name": "Новая", "color": "blue" },
          { "name": "В работе", "color": "yellow" },
          { "name": "Готово", "color": "green" }
        ]
      }
    },
    {
      "type": "DateTime",
      "name": "Дедлайн",
      "property": {
        "dateFormat": "YYYY-MM-DD",
        "includeTime": true
      }
    }
  ]
}
```

**Ответ:**
```json
{
  "success": true,
  "data": {
    "id": "dstXXX",
    "createdAt": 1712764800000,
    "fields": [
      { "id": "fldAAA", "name": "Название" },
      { "id": "fldBBB", "name": "Статус" },
      { "id": "fldCCC", "name": "Дедлайн" }
    ]
  }
}
```

### Удалить таблицу

```http
DELETE /fusion/v1/spaces/{spaceId}/datasheet/{dstId}
```

---

## 4. Fields (Поля/Колонки)

Схема таблицы. Каждое поле имеет тип и свойства.

### Все типы полей

| Тип | Описание | Ключевые свойства |
|-----|----------|-------------------|
| `SingleText` | Однострочный текст | `defaultValue`, `mask` |
| `Text` | Многострочный текст | `mask` |
| `SingleSelect` | Выбор одного | `options: [{name, color}]` |
| `MultiSelect` | Множественный выбор | `options` |
| `Number` | Число | `precision`, `symbol` |
| `Currency` | Валюта | `precision`, `symbol`, `symbolAlign` |
| `Percent` | Проценты | `precision` |
| `DateTime` | Дата/время | `dateFormat`, `timeFormat`, `includeTime` |
| `Attachment` | Вложения | — |
| `Member` | Участник | `isMulti`, `shouldSendMsg` |
| `Checkbox` | Чекбокс | `icon` |
| `Rating` | Рейтинг | `icon`, `max` (1-10) |
| `URL` | Ссылка | — |
| `Phone` | Телефон | — |
| `Email` | Email | — |
| `OneWayLink` | Односторонняя связь | `foreignDatasheetId`, `limitSingleRecord` |
| `TwoWayLink` | Двусторонняя связь | `foreignDatasheetId` |
| `MagicLookUp` | Lookup из связи | `relatedLinkFieldId`, `targetFieldId` |
| `Formula` | Формула | `expression` |
| `AutoNumber` | Автоинкремент | — |
| `CreatedTime` | Время создания | `dateFormat`, `timeFormat` |
| `LastModifiedTime` | Время изменения | `collectType`, `fieldIdCollection` |
| `CreatedBy` | Кто создал | — |
| `LastModifiedBy` | Кто изменил | — |
| `Button` | Кнопка | `text`, `style`, `action` |

### Получить поля таблицы

```http
GET /fusion/v1/datasheets/{dstId}/fields
GET /fusion/v1/datasheets/{dstId}/fields?viewId=viwXXX   # в порядке view
```

### Создать поле

```http
POST /fusion/v1/spaces/{spaceId}/datasheets/{dstId}/fields
```

```json
{
  "type": "SingleSelect",
  "name": "Приоритет",
  "property": {
    "options": [
      { "name": "Низкий", "color": "gray" },
      { "name": "Средний", "color": "yellow" },
      { "name": "Высокий", "color": "red" }
    ],
    "defaultValue": "Средний"
  }
}
```

### Удалить поле

```http
DELETE /fusion/v1/spaces/{spaceId}/datasheets/{dstId}/fields/{fieldId}
```

### Изменить порядок поля

```http
PATCH /fusion/v1/datasheets/{dstId}/views/{viewId}/fields/{fieldId}
```

```json
{
  "index": 3
}
```

---

## 5. Records (Записи/Строки)

Сами данные в таблице.

### Получить записи

```http
GET /fusion/v1/datasheets/{dstId}/records
```

**Query параметры:**

| Параметр | Тип | Описание |
|----------|-----|----------|
| `viewId` | string | ID представления (скрытые записи не вернутся) |
| `pageSize` | int | Размер страницы (default: 100) |
| `pageNum` | int | Номер страницы (default: 1) |
| `maxRecords` | int | Максимум записей всего |
| `recordIds` | string[] | Конкретные ID записей |
| `fields` | string[] | Только определённые поля |
| `sort` | object[] | `[{field: "Name", order: "asc"}]` |
| `filterByFormula` | string | Формула: `{Status}="Done"` |
| `cellFormat` | string | `json` или `string` |
| `fieldKey` | string | `name` или `id` |

**Пример:**
```http
GET /fusion/v1/datasheets/dstXXX/records?pageSize=50&filterByFormula={Status}="В работе"
```

**Ответ:**
```json
{
  "success": true,
  "data": {
    "total": 150,
    "pageNum": 1,
    "pageSize": 50,
    "records": [
      {
        "recordId": "recXXX",
        "fields": {
          "Название": "Сделать API",
          "Статус": "В работе",
          "Дедлайн": 1712764800000
        },
        "createdAt": 1712000000000,
        "updatedAt": 1712500000000
      }
    ]
  }
}
```

### Создать записи

```http
POST /fusion/v1/datasheets/{dstId}/records
```

```json
{
  "fieldKey": "name",
  "records": [
    {
      "fields": {
        "Название": "Новая задача",
        "Статус": "Новая",
        "Приоритет": "Высокий"
      }
    },
    {
      "fields": {
        "Название": "Ещё задача",
        "Статус": "Новая"
      }
    }
  ]
}
```

### Обновить записи

```http
PATCH /fusion/v1/datasheets/{dstId}/records
```

```json
{
  "fieldKey": "name",
  "records": [
    {
      "recordId": "recXXX",
      "fields": {
        "Статус": "Готово"
      }
    }
  ]
}
```

### Удалить записи

```http
DELETE /fusion/v1/datasheets/{dstId}/records?recordIds=recXXX,recYYY
```

---

## 6. Views (Представления)

Разные способы отображения одних данных. Не создают копии — только фильтруют/сортируют/группируют.

### Типы представлений

| Тип | Описание | Обязательные настройки |
|-----|----------|------------------------|
| `Grid` | Таблица | — |
| `Kanban` | Доска | `groupFieldId` (Select/Member) |
| `Gantt` | Диаграмма Ганта | `startFieldId`, `endFieldId?`, `linkFieldId?` |
| `Calendar` | Календарь | `startFieldId`, `endFieldId?` |
| `Gallery` | Галерея | — |
| `Architecture` | Орг-структура | `linkFieldId` |

### Получить представления

```http
GET /fusion/v1/datasheets/{dstId}/views
```

### Создать представление

```http
POST /fusion/v1/spaces/{spaceId}/datasheets/{dstId}/views
```

**Grid:**
```json
{
  "name": "Все задачи",
  "properties": {
    "type": "Grid"
  }
}
```

**Kanban:**
```json
{
  "name": "Kanban задач",
  "properties": {
    "type": "Kanban",
    "settings": {
      "groupFieldId": "fldSTATUS"
    }
  }
}
```

**Gantt:**
```json
{
  "name": "Timeline",
  "properties": {
    "type": "Gantt",
    "settings": {
      "startFieldId": "fldSTART",
      "endFieldId": "fldEND"
    }
  }
}
```

**Calendar:**
```json
{
  "name": "Календарь",
  "properties": {
    "type": "Calendar",
    "settings": {
      "startFieldId": "fldDATE"
    }
  }
}
```

### Удалить представление

```http
DELETE /fusion/v1/spaces/{spaceId}/datasheets/{dstId}/views/{viewId}
```

### Переименовать представление

```http
PUT /fusion/v1/spaces/{spaceId}/datasheets/{dstId}/views/{viewId}
```

```json
{
  "name": "Новое название"
}
```

### Настроить сортировку

```http
POST /fusion/v1/spaces/{spaceId}/datasheets/{dstId}/views/{viewId}/sort
```

```json
{
  "data": {
    "keepSort": true,
    "rules": [
      { "fieldId": "fldPRIORITY", "desc": true },
      { "fieldId": "fldDEADLINE", "desc": false }
    ]
  },
  "applySort": true
}
```

### Настроить группировку

```http
POST /fusion/v1/spaces/{spaceId}/datasheets/{dstId}/views/{viewId}/group
```

```json
{
  "data": [
    { "fieldId": "fldSTATUS", "desc": false }
  ]
}
```

### Скрыть/показать поля

```http
POST /fusion/v1/spaces/{spaceId}/datasheets/{dstId}/views/{viewId}/hidden
```

```json
{
  "data": [
    { "fieldId": "fldXXX", "hidden": true },
    { "fieldId": "fldYYY", "hidden": false }
  ]
}
```

### Переместить представление

```http
POST /fusion/v1/spaces/{spaceId}/datasheets/{dstId}/views/{viewId}/move
```

```json
{
  "data": {
    "newIndex": 2
  }
}
```

---

## 7. Attachments (Вложения)

Файлы, прикреплённые к записям.

### Загрузить файл

```http
POST /fusion/v1/datasheets/{dstId}/attachments
Content-Type: multipart/form-data
```

**В конкретную ячейку:**
```http
POST /fusion/v1/datasheets/{dstId}/attachments?recordId=recXXX&fieldId=fldYYY
```

**Ответ:**
```json
{
  "success": true,
  "data": {
    "token": "space/2024/04/10/abc123.pdf",
    "name": "document.pdf",
    "size": 102400,
    "mimeType": "application/pdf",
    "url": "/attachments/space/2024/04/10/abc123.pdf"
  }
}
```

### Скачать файл

```http
GET /fusion/v1/datasheets/{dstId}/attachments?token=space/2024/04/10/abc123.pdf
```

---

## 8. Timemachine

Восстановление удалённых записей.

```http
GET /fusion/v1/timemachine/{dstId}
```

**Ответ:**
```json
{
  "success": true,
  "data": ["recDELETED1", "recDELETED2"]
}
```

---

## Коды ошибок

| Код | Описание |
|-----|----------|
| 200 | Успех |
| 201 | Создано |
| 400 | Неверные параметры |
| 401 | Не авторизован |
| 403 | Нет прав доступа |
| 404 | Не найдено |
| 500 | Внутренняя ошибка сервера |

---

## Пример: Wiki-редактор для хакатона

### Структура таблицы страниц

```json
{
  "name": "Wiki Pages",
  "fields": [
    { "type": "SingleText", "name": "Title" },
    { "type": "Text", "name": "Content" },
    {
      "type": "TwoWayLink",
      "name": "Backlinks",
      "property": { "foreignDatasheetId": "self" }
    },
    { "type": "DateTime", "name": "UpdatedAt", "property": { "autoFill": true } },
    { "type": "Member", "name": "Author" },
    { "type": "OneWayLink", "name": "EmbeddedTables" }
  ]
}
```

### In-line автосохранение

```javascript
// Debounced save
const saveContent = debounce(async (recordId, content) => {
  await fetch(`/fusion/v1/datasheets/${dstId}/records`, {
    method: 'PATCH',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      fieldKey: 'name',
      records: [{ recordId, fields: { Content: content } }]
    })
  });
}, 1000);
```

### Backlinks через TwoWayLink

При создании связи `PageA → PageB`, автоматически создаётся обратная связь `PageB → PageA`.

### Вставка таблицы в страницу

Используй `OneWayLink` поле для привязки существующих таблиц к странице wiki.
