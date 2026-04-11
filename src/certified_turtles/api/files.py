from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from certified_turtles.tools.presentation import _storage_dir  # noqa: PLC2701 - shared storage root

router = APIRouter(tags=["files"])

_RUN_ID_RE = re.compile(r"^[a-f0-9]{12}$")


def _guess_media_type(filename: str) -> str:
    mt, _ = mimetypes.guess_type(filename)
    if mt:
        return mt
    suf = Path(filename).suffix.lower()
    if suf == ".pptx":
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    return "application/octet-stream"


@router.get("/files/python_runs/{run_id}/{filename}")
def download_python_run_artifact(run_id: str, filename: str) -> FileResponse:
    """Артефакты `execute_python` (графики и т.п.) в подкаталоге python_runs/{run_id}/."""
    if not _RUN_ID_RE.fullmatch(run_id):
        raise HTTPException(status_code=400, detail="Некорректный run_id")
    if "/" in filename or "\\" in filename or filename.startswith("..") or filename == "user_code.py":
        raise HTTPException(status_code=400, detail="Некорректное имя файла")
    path = _storage_dir() / "python_runs" / run_id / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(path, filename=filename, media_type=_guess_media_type(filename))


@router.get("/files/{filename}")
def download_file(filename: str) -> FileResponse:
    """Отдаёт файл из GENERATED_FILES_DIR (pptx, png и др.)."""
    if "/" in filename or "\\" in filename or filename.startswith(".."):
        raise HTTPException(status_code=400, detail="Некорректное имя файла")
    path = _storage_dir() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(path, filename=filename, media_type=_guess_media_type(filename))
