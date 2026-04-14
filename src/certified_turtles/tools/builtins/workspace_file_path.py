from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from certified_turtles.tools.registry import ToolSpec, register_tool
from certified_turtles.tools.workspace_storage import uploads_dir


def _looks_like_placeholder_file_id(file_id: str) -> bool:
    """Модель часто подставляет выдуманный текст вместо реального file_id из [CT: … file_id=\"…\"]."""
    if any(c in file_id for c in "[]"):
        return True
    if "..." in file_id or "\u2026" in file_id:
        return True
    low = file_id.lower()
    if "rag" in low and "источник" in low:
        return True
    if low.startswith("file_id") or "из_ответа" in low or "from_response" in low:
        return True
    return False


def resolve_workspace_upload_file(file_id: str) -> tuple[str, Path] | None:
    """(каноническое имя файла, путь) если загрузка существует; иначе None."""
    raw = file_id.strip()
    if not raw or _looks_like_placeholder_file_id(raw):
        return None
    name = Path(raw).name
    if name != raw or ".." in raw or "/" in raw or "\\" in raw:
        return None
    path = uploads_dir() / name
    # Extra guard: resolved path must stay within uploads_dir
    try:
        resolved = path.resolve(strict=False)
        if not str(resolved).startswith(str(uploads_dir().resolve())):
            return None
    except (OSError, ValueError):
        return None
    if not path.is_file():
        return None
    return name, path


def _handle_workspace_file_path(arguments: dict[str, Any]) -> str:
    file_id = arguments.get("file_id")
    if not isinstance(file_id, str) or not file_id.strip():
        return json.dumps({"error": "Нужен непустой file_id (как в ответе POST /api/v1/uploads)."}, ensure_ascii=False)
    fid = file_id.strip()
    if _looks_like_placeholder_file_id(fid):
        return json.dumps(
            {
                "error": "file_id_placeholder",
                "detail": (
                    "Подставлен шаблон вместо реального идентификатора. Скопируйте значение из строки "
                    'file_id="…" рядом с [CT: RAG-источник …] или из ответа загрузки; не подставляйте текст цитаты.'
                ),
            },
            ensure_ascii=False,
        )
    resolved = resolve_workspace_upload_file(fid)
    if resolved is None:
        name = Path(fid).name
        if name != fid or ".." in fid or "/" in fid or "\\" in fid:
            return json.dumps({"error": "Некорректный file_id."}, ensure_ascii=False)
        return json.dumps({"error": "Файл не найден. Сначала загрузите через POST /api/v1/uploads."}, ensure_ascii=False)
    name, path = resolved
    try:
        size_b = path.stat().st_size
    except OSError:
        size_b = None
    return json.dumps(
        {
            "file_id": name,
            "absolute_path": str(path.resolve()),
            "suffix": path.suffix.lower(),
            "size_bytes": size_b,
            "hint": (
                "В execute_python передай тот же file_id во второй аргумент `file_id` тула или вставь absolute_path в pd.read_csv(path). "
                "Не вызывай workspace_file_path() внутри Python — это отдельный тул."
            ),
        },
        ensure_ascii=False,
    )


register_tool(
    ToolSpec(
        name="workspace_file_path",
        description=(
            "Путь к загруженному файлу на сервере для pandas в `execute_python`. "
            "После POST /api/v1/uploads вызови с `file_id` из ответа; вернётся `absolute_path` для pd.read_csv / pd.read_excel. "
            "Для .csv/.xlsx и больших таблиц так надёжнее, чем read_workspace_file (там лимит по тексту)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "Идентификатор файла после загрузки (поле file_id).",
                },
            },
            "required": ["file_id"],
        },
        handler=_handle_workspace_file_path,
    )
)
