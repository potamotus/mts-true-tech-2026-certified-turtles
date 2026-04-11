from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from certified_turtles.tools.registry import ToolSpec, register_tool
from certified_turtles.tools.workspace_storage import uploads_dir

_MAX_READ = 512_000


def _handle_read_workspace_file(arguments: dict[str, Any]) -> str:
    file_id = arguments.get("file_id")
    if not isinstance(file_id, str) or not file_id.strip():
        return json.dumps({"error": "Нужен непустой file_id (как в ответе POST /api/v1/uploads)."}, ensure_ascii=False)
    name = Path(file_id.strip()).name
    if name != file_id.strip() or ".." in file_id or "/" in file_id or "\\" in file_id:
        return json.dumps({"error": "Некорректный file_id."}, ensure_ascii=False)
    path = uploads_dir() / name
    if not path.is_file():
        return json.dumps({"error": "Файл не найден. Сначала загрузите через POST /api/v1/uploads."}, ensure_ascii=False)
    try:
        data = path.read_bytes()
    except OSError as e:
        return json.dumps({"error": "read_failed", "detail": str(e)}, ensure_ascii=False)
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
            "Передай file_id из ответа загрузки. Лимит ~512k символов; для больших файлов используй execute_python."
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
