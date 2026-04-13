"""Тулы для работы с MWS Tables (APITable) — CRUD записей, схема таблиц."""

from __future__ import annotations

import json
import logging
from typing import Any

from certified_turtles.mws_tables.client import (
    MWSTablesError,
    get_client,
    is_configured,
    parse_datasheet_id,
)
from certified_turtles.tools.registry import ToolSpec, register_tool

logger = logging.getLogger(__name__)

_MAX_RECORDS_RESPONSE = 50  # лимит записей в одном ответе агенту


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


def _not_configured() -> str:
    return _err(
        "MWS Tables не настроен: задайте MWS_TABLES_API_TOKEN в переменных окружения. "
        "Токен генерируется в настройках профиля на tabs.mts.ru."
    )


def _safe_int(val: Any, default: int) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


# ── mws_tables_describe ─────────────────────────────────────

def _handle_describe(arguments: dict[str, Any]) -> str:
    if not is_configured():
        return _not_configured()
    raw_id = arguments.get("datasheet_id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return _err("Нужен datasheet_id (ID таблицы или URL из tabs.mts.ru)")
    ds_id = parse_datasheet_id(raw_id)
    client = get_client()
    try:
        fields_resp = client.get_fields(ds_id)
        views_resp = client.get_views(ds_id)
    except MWSTablesError as e:
        logger.warning("mws_tables_describe error: %s (status=%s)", e, e.status)
        return _err(f"MWS Tables API error: {e} (status={e.status})")
    fields = fields_resp.get("data", {}).get("fields", [])
    views = views_resp.get("data", {}).get("views", [])
    return json.dumps(
        {"datasheet_id": ds_id, "fields": fields, "views": views},
        ensure_ascii=False,
    )


register_tool(
    ToolSpec(
        name="mws_tables_describe",
        description=(
            "MWS Tables: получить схему таблицы — список полей (колонок) и представлений (views). "
            "Вызывай ПЕРВЫМ перед чтением/записью, чтобы узнать точные имена полей. "
            "Возвращает {datasheet_id, fields, views}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "datasheet_id": {
                    "type": "string",
                    "description": "ID таблицы (dstXXX) или полный URL из tabs.mts.ru",
                },
            },
            "required": ["datasheet_id"],
        },
        handler=_handle_describe,
    )
)


# ── mws_tables_get_records ──────────────────────────────────

def _handle_get_records(arguments: dict[str, Any]) -> str:
    if not is_configured():
        return _not_configured()
    raw_id = arguments.get("datasheet_id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return _err("Нужен datasheet_id")
    ds_id = parse_datasheet_id(raw_id)
    client = get_client()
    page_size = min(_safe_int(arguments.get("page_size"), 100), _MAX_RECORDS_RESPONSE)
    page_num = _safe_int(arguments.get("page_num"), 1)
    try:
        resp = client.get_records(
            ds_id,
            view_id=arguments.get("view_id"),
            filter_formula=arguments.get("filter"),
            sort=arguments.get("sort"),
            fields=arguments.get("fields"),
            page_size=page_size,
            page_num=page_num,
        )
    except MWSTablesError as e:
        logger.warning("mws_tables_get_records error: %s (status=%s)", e, e.status)
        return _err(f"MWS Tables API error: {e} (status={e.status})")
    data = resp.get("data", {})
    records = data.get("records", [])
    return json.dumps(
        {
            "datasheet_id": ds_id,
            "total": data.get("total"),
            "page_num": data.get("pageNum"),
            "page_size": data.get("pageSize"),
            "records_count": len(records),
            "records": records[:_MAX_RECORDS_RESPONSE],
        },
        ensure_ascii=False,
    )


register_tool(
    ToolSpec(
        name="mws_tables_get_records",
        description=(
            "MWS Tables: прочитать записи из таблицы. Поддерживает фильтрацию, сортировку, выбор полей и пагинацию. "
            "Возвращает {datasheet_id, total, page_num, page_size, records_count, records}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "datasheet_id": {
                    "type": "string",
                    "description": "ID таблицы (dstXXX) или URL",
                },
                "view_id": {
                    "type": "string",
                    "description": "ID представления (viwXXX) для фильтрации. Необязательно.",
                },
                "filter": {
                    "type": "string",
                    "description": 'Формула фильтрации в синтаксисе APITable, например: OR(find("текст", {Поле}) > 0)',
                },
                "sort": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "order": {"type": "string", "enum": ["asc", "desc"]},
                        },
                    },
                    "description": "Сортировка: [{field, order}]",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Список имён полей для возврата. Если не указано — все поля.",
                },
                "page_size": {"type": "integer", "description": "Записей на странице (1–50, по умолчанию 50)."},
                "page_num": {"type": "integer", "description": "Номер страницы (с 1, по умолчанию 1)."},
            },
            "required": ["datasheet_id"],
        },
        handler=_handle_get_records,
    )
)


# ── mws_tables_create_records ───────────────────────────────

def _handle_create_records(arguments: dict[str, Any]) -> str:
    if not is_configured():
        return _not_configured()
    raw_id = arguments.get("datasheet_id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return _err("Нужен datasheet_id")
    records = arguments.get("records")
    if not isinstance(records, list) or not records:
        return _err("records должен быть непустым массивом объектов с полями")
    ds_id = parse_datasheet_id(raw_id)
    client = get_client()
    try:
        resp = client.create_records(ds_id, records[:10])
    except MWSTablesError as e:
        logger.warning("mws_tables_create_records error: %s (status=%s)", e, e.status)
        return _err(f"MWS Tables API error: {e} (status={e.status})")
    created = resp.get("data", {}).get("records", [])
    return json.dumps(
        {"ok": True, "datasheet_id": ds_id, "created_count": len(created), "records": created},
        ensure_ascii=False,
    )


register_tool(
    ToolSpec(
        name="mws_tables_create_records",
        description=(
            "MWS Tables: создать записи (строки) в таблице. Максимум 10 записей за раз. "
            "Каждая запись — объект с именами полей и значениями. "
            "Возвращает {ok, datasheet_id, created_count, records}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "datasheet_id": {
                    "type": "string",
                    "description": "ID таблицы (dstXXX) или URL",
                },
                "records": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "description": "Объект {имя_поля: значение}",
                    },
                    "description": 'Массив записей (1–10), например: [{"Имя": "Вася", "Возраст": 25}]',
                },
            },
            "required": ["datasheet_id", "records"],
        },
        handler=_handle_create_records,
    )
)


# ── mws_tables_update_records ───────────────────────────────

def _handle_update_records(arguments: dict[str, Any]) -> str:
    if not is_configured():
        return _not_configured()
    raw_id = arguments.get("datasheet_id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return _err("Нужен datasheet_id")
    records = arguments.get("records")
    if not isinstance(records, list) or not records:
        return _err("records должен быть непустым массивом [{recordId, fields}, ...]")
    for r in records:
        if not isinstance(r, dict) or "recordId" not in r or "fields" not in r:
            return _err("Каждая запись должна содержать recordId и fields")
    ds_id = parse_datasheet_id(raw_id)
    client = get_client()
    try:
        resp = client.update_records(ds_id, records[:10])
    except MWSTablesError as e:
        logger.warning("mws_tables_update_records error: %s (status=%s)", e, e.status)
        return _err(f"MWS Tables API error: {e} (status={e.status})")
    updated = resp.get("data", {}).get("records", [])
    return json.dumps(
        {"ok": True, "datasheet_id": ds_id, "updated_count": len(updated), "records": updated},
        ensure_ascii=False,
    )


register_tool(
    ToolSpec(
        name="mws_tables_update_records",
        description=(
            "MWS Tables: обновить существующие записи по recordId. Максимум 10 записей за раз. "
            "Возвращает {ok, datasheet_id, updated_count, records}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "datasheet_id": {
                    "type": "string",
                    "description": "ID таблицы (dstXXX) или URL",
                },
                "records": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "recordId": {"type": "string", "description": "ID записи (recXXX)"},
                            "fields": {"type": "object", "description": "Поля для обновления"},
                        },
                        "required": ["recordId", "fields"],
                    },
                    "description": 'Массив (1–10): [{"recordId": "recXXX", "fields": {"Имя": "Новое"}}]',
                },
            },
            "required": ["datasheet_id", "records"],
        },
        handler=_handle_update_records,
    )
)


# ── mws_tables_delete_records ───────────────────────────────

def _handle_delete_records(arguments: dict[str, Any]) -> str:
    if not is_configured():
        return _not_configured()
    raw_id = arguments.get("datasheet_id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return _err("Нужен datasheet_id")
    record_ids = arguments.get("record_ids")
    if not isinstance(record_ids, list) or not record_ids:
        return _err("record_ids должен быть непустым массивом строк (recXXX)")
    ds_id = parse_datasheet_id(raw_id)
    client = get_client()
    try:
        resp = client.delete_records(ds_id, record_ids[:10])
    except MWSTablesError as e:
        logger.warning("mws_tables_delete_records error: %s (status=%s)", e, e.status)
        return _err(f"MWS Tables API error: {e} (status={e.status})")
    return json.dumps({"ok": True, "datasheet_id": ds_id, "deleted_ids": record_ids[:10]}, ensure_ascii=False)


register_tool(
    ToolSpec(
        name="mws_tables_delete_records",
        description=(
            "MWS Tables: удалить записи по recordId. Максимум 10 за раз. "
            "Сначала получи recordId через mws_tables_get_records. "
            "Возвращает {ok, datasheet_id, deleted_ids}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "datasheet_id": {
                    "type": "string",
                    "description": "ID таблицы (dstXXX) или URL",
                },
                "record_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Массив ID записей для удаления (1–10, recXXX)",
                },
            },
            "required": ["datasheet_id", "record_ids"],
        },
        handler=_handle_delete_records,
    )
)


# ── mws_tables_list_nodes ───────────────────────────────────

def _handle_list_nodes(arguments: dict[str, Any]) -> str:
    if not is_configured():
        return _not_configured()
    client = get_client()
    space_id = arguments.get("space_id")
    try:
        if space_id:
            resp = client.list_nodes(space_id)
        else:
            spaces_resp = client.list_spaces()
            spaces = spaces_resp.get("data", {}).get("spaces", [])
            if not spaces:
                return _err("Нет доступных пространств (spaces) в MWS Tables")
            resp = client.list_nodes(spaces[0]["id"])
    except MWSTablesError as e:
        logger.warning("mws_tables_list_nodes error: %s (status=%s)", e, e.status)
        return _err(f"MWS Tables API error: {e} (status={e.status})")
    nodes = resp.get("data", {}).get("nodes", [])
    return json.dumps({"nodes": nodes}, ensure_ascii=False)


register_tool(
    ToolSpec(
        name="mws_tables_list_nodes",
        description=(
            "MWS Tables: список доступных таблиц (datasheets) и папок в пространстве. "
            "Используй для обнаружения таблиц, если пользователь не указал конкретный ID. "
            "Возвращает {nodes} — массив с id, name, type каждого узла."
        ),
        parameters={
            "type": "object",
            "properties": {
                "space_id": {
                    "type": "string",
                    "description": "ID пространства (spcXXX). Если не указано — используется первое доступное.",
                },
            },
        },
        handler=_handle_list_nodes,
    )
)


# ── mws_tables_create_datasheet ──────────────────────────────

def _handle_create_datasheet(arguments: dict[str, Any]) -> str:
    if not is_configured():
        return _not_configured()
    name = arguments.get("name", "").strip()
    if not name:
        return _err("Нужно указать name — название таблицы")
    client = get_client()
    space_id = arguments.get("space_id")
    try:
        if not space_id:
            spaces = client.list_spaces().get("data", {}).get("spaces", [])
            if not spaces:
                return _err("Нет доступных пространств (spaces) в MWS Tables")
            space_id = spaces[0]["id"]
        fields = arguments.get("fields")
        resp = client.create_datasheet(
            space_id,
            name,
            folder_id=arguments.get("folder_id"),
            description=arguments.get("description"),
            fields=fields,
        )
    except MWSTablesError as e:
        logger.warning("mws_tables_create_datasheet error: %s (status=%s)", e, e.status)
        return _err(f"MWS Tables API error: {e} (status={e.status})")
    data = resp.get("data", {})
    return json.dumps({"ok": True, "datasheet_id": data.get("id"), "data": data}, ensure_ascii=False)


register_tool(
    ToolSpec(
        name="mws_tables_create_datasheet",
        description=(
            "MWS Tables: создать новую таблицу (datasheet) в пространстве. "
            "Можно указать начальные колонки (fields). Типы полей: SingleText, Text, Number, "
            "SingleSelect, MultiSelect, DateTime, Checkbox, Rating, URL, Email, Phone, Currency, Percent, Attachment. "
            "Возвращает {ok, datasheet_id, data}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Название новой таблицы",
                },
                "space_id": {
                    "type": "string",
                    "description": "ID пространства (spcXXX). Если не указано — первое доступное.",
                },
                "folder_id": {
                    "type": "string",
                    "description": "ID папки (fodXXX) для размещения. Необязательно.",
                },
                "description": {
                    "type": "string",
                    "description": "Описание таблицы. Необязательно.",
                },
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "description": "Тип поля: SingleText, Text, Number, SingleSelect, MultiSelect, DateTime, Checkbox и др.",
                            },
                            "name": {
                                "type": "string",
                                "description": "Название колонки",
                            },
                        },
                        "required": ["type", "name"],
                    },
                    "description": 'Начальные колонки, например: [{"type": "SingleText", "name": "Имя"}, {"type": "Number", "name": "Возраст"}]',
                },
            },
            "required": ["name"],
        },
        handler=_handle_create_datasheet,
    )
)


# ── mws_tables_create_field ──────────────────────────────────

def _handle_create_field(arguments: dict[str, Any]) -> str:
    if not is_configured():
        return _not_configured()
    raw_id = arguments.get("datasheet_id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return _err("Нужен datasheet_id")
    name = arguments.get("name", "").strip()
    field_type = arguments.get("type", "").strip()
    if not name or not field_type:
        return _err("Нужны name и type")
    ds_id = parse_datasheet_id(raw_id)
    client = get_client()
    try:
        resp = client.create_field(ds_id, name, field_type, property=arguments.get("property"))
    except MWSTablesError as e:
        logger.warning("mws_tables_create_field error: %s (status=%s)", e, e.status)
        return _err(f"MWS Tables API error: {e} (status={e.status})")
    data = resp.get("data", {})
    return json.dumps({"ok": True, "field": data}, ensure_ascii=False)


register_tool(
    ToolSpec(
        name="mws_tables_create_field",
        description=(
            "MWS Tables: добавить колонку (поле) в существующую таблицу. "
            "Типы: SingleText, Text, Number, SingleSelect, MultiSelect, DateTime, Checkbox, "
            "Rating, URL, Email, Phone, Currency, Percent, Attachment. "
            "Возвращает {ok, field} с id и параметрами созданного поля."
        ),
        parameters={
            "type": "object",
            "properties": {
                "datasheet_id": {
                    "type": "string",
                    "description": "ID таблицы (dstXXX) или URL",
                },
                "name": {
                    "type": "string",
                    "description": "Название колонки",
                },
                "type": {
                    "type": "string",
                    "description": "Тип поля: SingleText, Number, SingleSelect и т.д.",
                },
                "property": {
                    "type": "object",
                    "description": 'Доп. настройки поля (например, для SingleSelect: {"options": [{"name": "Opt1"}, {"name": "Opt2"}]})',
                },
            },
            "required": ["datasheet_id", "name", "type"],
        },
        handler=_handle_create_field,
    )
)


# ── mws_tables_delete_field ──────────────────────────────────

def _handle_delete_field(arguments: dict[str, Any]) -> str:
    if not is_configured():
        return _not_configured()
    raw_id = arguments.get("datasheet_id")
    field_id = arguments.get("field_id", "").strip()
    if not isinstance(raw_id, str) or not raw_id.strip():
        return _err("Нужен datasheet_id")
    if not field_id:
        return _err("Нужен field_id (fldXXX) — получи через mws_tables_describe")
    ds_id = parse_datasheet_id(raw_id)
    client = get_client()
    try:
        client.delete_field(ds_id, field_id)
    except MWSTablesError as e:
        logger.warning("mws_tables_delete_field error: %s (status=%s)", e, e.status)
        return _err(f"MWS Tables API error: {e} (status={e.status})")
    return json.dumps({"ok": True, "deleted_field_id": field_id}, ensure_ascii=False)


register_tool(
    ToolSpec(
        name="mws_tables_delete_field",
        description=(
            "MWS Tables: удалить колонку (поле) из таблицы по field_id. "
            "Сначала получи field_id через mws_tables_describe. "
            "Возвращает {ok, deleted_field_id}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "datasheet_id": {
                    "type": "string",
                    "description": "ID таблицы (dstXXX) или URL",
                },
                "field_id": {
                    "type": "string",
                    "description": "ID поля (fldXXX) — получи через mws_tables_describe.",
                },
            },
            "required": ["datasheet_id", "field_id"],
        },
        handler=_handle_delete_field,
    )
)


# ── mws_tables_upload_attachment ─────────────────────────────

def _handle_upload_attachment(arguments: dict[str, Any]) -> str:
    if not is_configured():
        return _not_configured()
    raw_id = arguments.get("datasheet_id")
    file_path = arguments.get("file_path", "").strip()
    if not isinstance(raw_id, str) or not raw_id.strip():
        return _err("Нужен datasheet_id")
    if not file_path:
        return _err("Нужен file_path — путь к файлу для загрузки")
    ds_id = parse_datasheet_id(raw_id)
    client = get_client()
    try:
        resp = client.upload_attachment(ds_id, file_path)
    except FileNotFoundError:
        return _err(f"Файл не найден: {file_path}")
    except MWSTablesError as e:
        logger.warning("mws_tables_upload_attachment error: %s (status=%s)", e, e.status)
        return _err(f"MWS Tables API error: {e} (status={e.status})")
    data = resp.get("data", {})
    return json.dumps({"ok": True, "attachment": data}, ensure_ascii=False)


register_tool(
    ToolSpec(
        name="mws_tables_upload_attachment",
        description=(
            "MWS Tables: загрузить файл как вложение в таблицу. "
            "Возвращает {ok, attachment} с токеном вложения для использования в полях типа Attachment "
            "при создании/обновлении записей."
        ),
        parameters={
            "type": "object",
            "properties": {
                "datasheet_id": {
                    "type": "string",
                    "description": "ID таблицы (dstXXX) или URL",
                },
                "file_path": {
                    "type": "string",
                    "description": "Абсолютный путь к файлу на сервере для загрузки.",
                },
            },
            "required": ["datasheet_id", "file_path"],
        },
        handler=_handle_upload_attachment,
    )
)
