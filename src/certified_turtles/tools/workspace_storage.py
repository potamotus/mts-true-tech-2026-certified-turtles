from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

_MAX_BYTES = int(os.environ.get("UPLOAD_MAX_BYTES", str(12 * 1024 * 1024)))

ALLOWED_UPLOAD_EXTENSIONS = frozenset(
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
        ".xlsx",
        # Аудио: загрузка и ASR (POST /v1/audio/transcriptions, тул transcribe_workspace_audio)
        ".mp3",
        ".wav",
        ".m4a",
        ".ogg",
        ".webm",
        ".flac",
    }
)

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def uploads_dir() -> Path:
    """Каталог пользовательских загрузок (том в compose: UPLOADS_DIR)."""
    root = os.environ.get("UPLOADS_DIR", "/tmp/certified_turtles_uploads")
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_upload_basename(name: str) -> str:
    base = Path(name).name
    if not base or base in (".", ".."):
        return "upload.bin"
    cleaned = _SAFE.sub("-", base).strip("-")
    return cleaned or "upload.bin"


def extension_allowed(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_UPLOAD_EXTENSIONS


def new_stored_filename(original_name: str) -> str:
    safe = safe_upload_basename(original_name)
    uid = uuid.uuid4().hex[:16]
    return f"{uid}_{safe}"


def save_workspace_file(original_name: str, data: bytes) -> str:
    """
    Сохраняет файл в рабочую область (как POST /api/v1/uploads).
    Возвращает file_id — имя файла в каталоге загрузок.
    """
    if len(data) > _MAX_BYTES:
        raise ValueError("file_too_large")
    name = original_name or "upload.bin"
    if not extension_allowed(name):
        raise ValueError("extension_not_allowed")
    stored_name = new_stored_filename(name)
    dest = uploads_dir() / stored_name
    dest.write_bytes(data)
    return stored_name
