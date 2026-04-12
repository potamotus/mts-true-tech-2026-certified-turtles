from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from certified_turtles.tools.builtins.workspace_file_path import (
    _looks_like_placeholder_file_id,
    resolve_workspace_upload_file,
)
from certified_turtles.tools.registry import ToolSpec, register_tool

_MAX_READ = 512_000
_TEXT_LIKE_SUFFIXES = frozenset(
    {".txt", ".csv", ".tsv", ".json", ".md", ".xml", ".html", ".htm", ".log", ".yaml", ".yml", ".py"}
)


def _handle_read_workspace_file(arguments: dict[str, Any]) -> str:
    file_id = arguments.get("file_id")
    if not isinstance(file_id, str) or not file_id.strip():
        return json.dumps({"error": "Нужен непустой file_id (как в ответе POST /api/v1/uploads)."}, ensure_ascii=False)
    fid = file_id.strip()
    if _looks_like_placeholder_file_id(fid):
        return json.dumps(
            {
                "error": "file_id_placeholder",
                "detail": "Подставь реальный file_id из [CT:…] или ответа загрузки, не шаблон.",
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
        data = path.read_bytes()
    except OSError as e:
        return json.dumps({"error": "read_failed", "detail": str(e)}, ensure_ascii=False)
    suf = path.suffix.lower()
    if suf not in _TEXT_LIKE_SUFFIXES and b"\x00" in data[:8192]:
        return json.dumps(
            {
                "error": "likely_binary",
                "file_id": name,
                "detail": "Файл похож на бинарный; для таблиц используй workspace_file_path + execute_python.",
            },
            ensure_ascii=False,
        )
    if len(data) > _MAX_READ:
        data = data[:_MAX_READ]
        truncated = True
    else:
        truncated = False
    text = data.decode("utf-8", errors="replace")
    return json.dumps(
        {"file_id": name, "chars": len(text), "truncated": truncated, "content": text},
        ensure_ascii=False,
    )


register_tool(
    ToolSpec(
        name="read_workspace_file",
        description=(
            "Прочитать текст из файла, загруженного в рабочую область (POST /api/v1/uploads). "
            "Передай file_id из ответа загрузки. Лимит ~512k символов. "
            "Для .xlsx и больших .csv сначала `workspace_file_path`, затем pandas в `execute_python`."
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
        handler=_handle_read_workspace_file,
    )
)
