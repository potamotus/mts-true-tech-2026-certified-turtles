from __future__ import annotations

import json
import os
import re
from typing import Any

from certified_turtles.tools.registry import ToolSpec, register_tool

_SCOPES = ("https://www.googleapis.com/auth/documents",)


def _parse_document_id(raw: str) -> str:
    s = raw.strip()
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", s)
    if m:
        return m.group(1)
    return s


def _build_docs_service():
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as e:  # pragma: no cover - optional extra
        return None, f"Пакеты Google не установлены: {e}. Установите optional-группу `google` (см. pyproject.toml)."

    path = os.environ.get("GOOGLE_DOCS_CREDENTIALS_JSON") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not path or not os.path.isfile(path):
        return None, (
            "Нет JSON ключа service account: задайте GOOGLE_DOCS_CREDENTIALS_JSON (путь к файлу) "
            "или GOOGLE_APPLICATION_CREDENTIALS. Документ нужно расшарить на email сервис-аккаунта."
        )
    try:
        creds = service_account.Credentials.from_service_account_file(path, scopes=_SCOPES)
        svc = build("docs", "v1", credentials=creds, cache_discovery=False)
        return svc, None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def _extract_plain_text(body: dict[str, Any]) -> str:
    parts: list[str] = []

    def walk(content: list[dict[str, Any]] | None) -> None:
        for el in content or []:
            para = el.get("paragraph")
            if isinstance(para, dict):
                for elem in para.get("elements") or []:
                    if not isinstance(elem, dict):
                        continue
                    tr = elem.get("textRun")
                    if isinstance(tr, dict) and isinstance(tr.get("content"), str):
                        parts.append(tr["content"])
            tbl = el.get("table")
            if isinstance(tbl, dict):
                for row in tbl.get("tableRows") or []:
                    if not isinstance(row, dict):
                        continue
                    for cell in row.get("tableCells") or []:
                        if isinstance(cell, dict):
                            walk(cell.get("content"))

    walk(body.get("content") if isinstance(body.get("content"), list) else None)
    return "".join(parts)


def _append_text(service: Any, document_id: str, text: str) -> dict[str, Any]:
    doc = service.documents().get(documentId=document_id).execute()
    body = doc.get("body") or {}
    content = body.get("content") or []
    if not content:
        insert_index = 1
    else:
        last = content[-1]
        end_index = int(last.get("endIndex") or 2)
        insert_index = max(1, end_index - 1)
    service.documents().batchUpdate(
        documentId=document_id,
        body={"requests": [{"insertText": {"location": {"index": insert_index}, "text": text}}]},
    ).execute()
    return {"inserted_at_index": insert_index, "chars": len(text)}


def _handle_google_docs_read(arguments: dict[str, Any]) -> str:
    raw_id = arguments.get("document_id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return json.dumps({"error": "Нужен document_id (ID или URL Google Doc)."}, ensure_ascii=False)
    document_id = _parse_document_id(raw_id)

    service, err = _build_docs_service()
    if err:
        return json.dumps({"error": "google_docs_unavailable", "detail": err}, ensure_ascii=False)
    try:
        doc = service.documents().get(documentId=document_id).execute()
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "docs_get_failed", "detail": str(e)}, ensure_ascii=False)
    title = doc.get("title") or ""
    body = doc.get("body") or {}
    text = _extract_plain_text(body if isinstance(body, dict) else {})
    max_chars = int(os.environ.get("GOOGLE_DOCS_READ_MAX_CHARS", "120000"))
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return json.dumps(
        {"title": title, "document_id": document_id, "truncated": truncated, "text": text},
        ensure_ascii=False,
    )


def _handle_google_docs_append(arguments: dict[str, Any]) -> str:
    raw_id = arguments.get("document_id")
    text = arguments.get("text")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return json.dumps({"error": "Нужен document_id."}, ensure_ascii=False)
    if not isinstance(text, str):
        return json.dumps({"error": "Параметр text должен быть строкой."}, ensure_ascii=False)
    document_id = _parse_document_id(raw_id)
    if len(text) > int(os.environ.get("GOOGLE_DOCS_APPEND_MAX_CHARS", "50000")):
        return json.dumps({"error": "text_too_long"}, ensure_ascii=False)

    service, err = _build_docs_service()
    if err:
        return json.dumps({"error": "google_docs_unavailable", "detail": err}, ensure_ascii=False)
    try:
        meta = _append_text(service, document_id, text)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "docs_append_failed", "detail": str(e)}, ensure_ascii=False)
    return json.dumps({"ok": True, "document_id": document_id, **meta}, ensure_ascii=False)


register_tool(
    ToolSpec(
        name="google_docs_read",
        description=(
            "Прочитать текст Google Doc по document_id или URL. "
            "Нужен service account с доступом к документу (расшарить на email из JSON ключа). "
            "Переменная окружения: GOOGLE_DOCS_CREDENTIALS_JSON."
        ),
        parameters={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "ID документа или полная ссылка вида https://docs.google.com/document/d/…/edit",
                },
            },
            "required": ["document_id"],
        },
        handler=_handle_google_docs_read,
    )
)

register_tool(
    ToolSpec(
        name="google_docs_append",
        description=(
            "Добавить текст в конец Google Doc (plain text). "
            "Тот же доступ, что и для google_docs_read. Не подходит для сложного форматирования — только сырой текст."
        ),
        parameters={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "ID или URL документа.",
                },
                "text": {"type": "string", "description": "Текст для вставки в конец документа."},
            },
            "required": ["document_id", "text"],
        },
        handler=_handle_google_docs_append,
    )
)
