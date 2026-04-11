from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from certified_turtles.tools.workspace_storage import uploads_dir

router = APIRouter(tags=["uploads"])

_MAX_BYTES = int(os.environ.get("UPLOAD_MAX_BYTES", str(12 * 1024 * 1024)))

_ALLOWED_EXT = frozenset(
    {
        ".txt",
        ".csv",
        ".tsv",
        ".json",
        ".md",
        ".xml",
        ".html",
        ".htm",
        ".log",
        ".yaml",
        ".yml",
        ".py",
    }
)

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_original_name(name: str) -> str:
    base = Path(name).name
    if not base or base in (".", ".."):
        return "upload.bin"
    cleaned = _SAFE.sub("-", base).strip("-")
    return cleaned or "upload.bin"


def _ext_ok(filename: str) -> bool:
    suf = Path(filename).suffix.lower()
    return suf in _ALLOWED_EXT


@router.post("/uploads")
async def upload_workspace_file(file: UploadFile = File(...)) -> dict[str, str]:
    """Загрузка файла в рабочую область агента; дальше — тул `read_workspace_file`."""
    raw_name = file.filename or "upload"
    if not _ext_ok(raw_name):
        raise HTTPException(
            status_code=400,
            detail=f"Расширение не разрешено. Допустимо: {', '.join(sorted(_ALLOWED_EXT))}",
        )
    safe = _safe_original_name(raw_name)
    uid = uuid.uuid4().hex[:16]
    stored_name = f"{uid}_{safe}"
    dest = uploads_dir() / stored_name

    size = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_BYTES:
                    raise HTTPException(status_code=413, detail="Файл слишком большой")
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except Exception as e:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=str(e)) from e

    return {
        "file_id": stored_name,
        "stored_as": stored_name,
        "original_name": raw_name,
        "hint": "В чате укажи file_id и вызови read_workspace_file, либо скажи пользователю загрузить файл через этот эндпоинт.",
    }
