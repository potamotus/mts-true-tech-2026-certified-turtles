from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from certified_turtles.tools.presentation import _storage_dir  # noqa: PLC2701 - shared storage root

router = APIRouter(tags=["files"])


@router.get("/files/{filename}")
def download_file(filename: str) -> FileResponse:
    """Отдаёт файл, сгенерированный тулом (например .pptx из `generate_presentation`)."""
    # Защита от path traversal: ровно basename, никаких '..', '/' и абсолютных путей.
    if "/" in filename or "\\" in filename or filename.startswith(".."):
        raise HTTPException(status_code=400, detail="Некорректное имя файла")
    path = _storage_dir() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(
        path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
