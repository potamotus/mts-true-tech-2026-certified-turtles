from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from certified_turtles.tools.registry import ToolSpec, register_tool

logger = logging.getLogger(__name__)

_SCOPES = ("https://www.googleapis.com/auth/documents",)


def _parse_document_id(raw: str) -> str:
    s = raw.strip()
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", s)
    if m:
        return m.group(1)
    return s


def google_docs_credentials_path() -> str:
    return (
        (os.environ.get("GOOGLE_DOCS_CREDENTIALS_JSON") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "")
        .strip()
    )


def google_docs_client_email() -> str | None:
    path = google_docs_credentials_path()
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    em = data.get("client_email") if isinstance(data, dict) else None
    return em if isinstance(em, str) and "@" in em else None


def google_docs_python_libs_ok() -> bool:
    try:
        import google.oauth2.service_account  # noqa: F401
        from googleapiclient.discovery import build  # noqa: F401

        return True
    except ImportError:
        return False


def google_docs_ready() -> bool:
    path = google_docs_credentials_path()
    return bool(path and os.path.isfile(path) and google_docs_python_libs_ok())


def google_docs_capability_dict() -> dict[str, Any]:
    path = google_docs_credentials_path()
    return {
        "google_docs_ready": google_docs_ready(),
        "credentials_path_configured": bool(path),
        "credentials_file_exists": bool(path and os.path.isfile(path)),
        "service_account_client_email": google_docs_client_email(),
        "google_python_packages_installed": google_docs_python_libs_ok(),
        "public_read_by_link_supported": True,
    }


def agent_system_prompt_google_docs_section() -> str:
    """Блок для системного промпта агента: как пользователю пользоваться Docs (тулы уже в каталоге)."""
    email = google_docs_client_email()
    ready = google_docs_ready()
    cred_path = google_docs_credentials_path()
    lines = [
        "=== Google Docs (объясни пользователю простыми шагами) ===",
        "Тулы: **google_docs_read** — текст документа; **google_docs_append** — дописать plain text в конец.",
        "",
        "**Чтение без ключа сервера (для пользователя):** в Google Doc → «Настройки доступа» / «Поделиться» → "
        "«Ограничений нет» или **«Все, у кого есть ссылка»** → роль **Читатель** (или выше). Пользователь вставляет "
        "ссылку на документ — вызывай google_docs_read с document_id = ссылка или ID из URL.",
        "",
        "**Запись в документ (google_docs_append):** нужен JSON service account на сервере и расшаривание документа "
        "на email сервис-аккаунта с ролью **Редактор** (это настраивает админ, не «доступ для всех»).",
    ]
    if ready and email:
        lines.append(f"На сервере задан ключ: client_email для шаринга при записи: **{email}**.")
    elif ready and not email:
        lines.append(
            "Файл ключа на сервере есть, но не прочитан client_email — проверьте формат JSON service account."
        )
    elif google_docs_python_libs_ok() and cred_path and not os.path.isfile(cred_path):
        lines.append(
            "Указан GOOGLE_DOCS_CREDENTIALS_JSON, но файла по пути нет — попроси админа проверить volume в Docker."
        )
    elif not google_docs_python_libs_ok():
        lines.append("Пакеты Google API не установлены в API (для append нужен образ с `extra google`).")
    else:
        lines.append("Ключ сервис-аккаунта на сервере не задан — **чтение по публичной ссылке всё равно работает**; append без ключа недоступен.")
    lines.append(
        "Если read вернул google_docs_public_access_failed — повтори шаги с доступом «по ссылке» для читателя."
    )
    return "\n".join(lines)


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


def _read_via_public_export(document_id: str) -> str:
    """Чтение через export?format=txt — работает для документов с доступом «все по ссылке» (читатель)."""
    from certified_turtles.tools.fetch_url import fetch_url_text

    url = f"https://docs.google.com/document/d/{document_id}/export?format=txt"
    max_chars = int(os.environ.get("GOOGLE_DOCS_READ_MAX_CHARS", "120000"))
    max_chars = max(5000, min(max_chars, 500_000))
    try:
        data = fetch_url_text(url, max_chars=max_chars, timeout=25)
    except RuntimeError as e:
        return json.dumps(
            {
                "error": "google_docs_public_access_failed",
                "detail": str(e),
                "hint": (
                    "В Google Doc: «Настройки доступа» → «Ограничений нет» или «Все, у кого есть ссылка» → "
                    "роль **Читатель** (или выше), затем снова пришлите ссылку."
                ),
            },
            ensure_ascii=False,
        )
    text = (data.get("text") or "").strip()
    title = (data.get("title") or "").strip()
    truncated = len(text) >= max_chars - 50
    return json.dumps(
        {
            "title": title or "Google Doc",
            "document_id": document_id,
            "truncated": truncated,
            "text": text,
            "via": "public_link_export",
        },
        ensure_ascii=False,
    )


def _handle_google_docs_read(arguments: dict[str, Any]) -> str:
    raw_id = arguments.get("document_id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return json.dumps({"error": "Нужен document_id (ID или URL Google Doc)."}, ensure_ascii=False)
    document_id = _parse_document_id(raw_id)

    service, _err = _build_docs_service()
    if service is not None:
        try:
            doc = service.documents().get(documentId=document_id).execute()
            title = doc.get("title") or ""
            body = doc.get("body") or {}
            text = _extract_plain_text(body if isinstance(body, dict) else {})
            max_chars = int(os.environ.get("GOOGLE_DOCS_READ_MAX_CHARS", "120000"))
            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]
            return json.dumps(
                {
                    "title": title,
                    "document_id": document_id,
                    "truncated": truncated,
                    "text": text,
                    "via": "service_account_api",
                },
                ensure_ascii=False,
            )
        except Exception as e:  # noqa: BLE001
            logger.info("google_docs API read failed, fallback public export: %s", e)

    return _read_via_public_export(document_id)


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
        return json.dumps(
            {
                "error": "google_docs_append_requires_service_account",
                "detail": err,
                "hint": (
                    "Дописывание в документ через API без ключа сервера недоступно. "
                    "Варианты: админ настраивает GOOGLE_DOCS_CREDENTIALS_JSON и пользователь шарит документ на client_email "
                    "с ролью Редактор; либо пользователь вручную вставляет текст. Чтение по публичной ссылке — через google_docs_read."
                ),
            },
            ensure_ascii=False,
        )
    try:
        meta = _append_text(service, document_id, text)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "docs_append_failed", "detail": str(e)}, ensure_ascii=False)
    return json.dumps({"ok": True, "document_id": document_id, **meta}, ensure_ascii=False)


register_tool(
    ToolSpec(
        name="google_docs_read",
        description=(
            "Google Docs: прочитать текст документа (plain text). "
            "**Пользователь:** достаточно открыть доступ «все, у кого есть ссылка» → Читатель и прислать ссылку/ID — "
            "чтение идёт без личного ключа пользователя. "
            "Если на сервере настроен service account, сначала используется API (удобно для приватных доков, расшаренных на client_email)."
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
            "Google Docs: дописать текст в конец документа (plain text). "
            "Требуется **ключ service account на сервере** и расшаривание документа на его client_email с ролью **Редактор** "
            "(публичной «всем по ссылке» для записи API не хватает — только чтение через google_docs_read)."
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
