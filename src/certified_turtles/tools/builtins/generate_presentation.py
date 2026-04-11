from __future__ import annotations

import json
import logging
import os
from typing import Any

from certified_turtles.tools.presentation import Slide, build_presentation
from certified_turtles.tools.registry import ToolSpec, register_tool

logger = logging.getLogger(__name__)

_MAX_SLIDES = 25
_MAX_BULLETS = 10


def _public_url(filename: str) -> str:
    """Ссылка, по которой файл отдаётся FastAPI-приложением."""
    base = os.environ.get("PUBLIC_API_BASE_URL", "http://localhost:8000")
    return f"{base.rstrip('/')}/files/{filename}"


def _normalize_slide(idx: int, raw: Any) -> Slide | dict[str, str]:
    if not isinstance(raw, dict):
        return {"error": f"slides[{idx}] должен быть объектом с title/bullets"}
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return {"error": f"slides[{idx}].title обязателен"}
    bullets_raw = raw.get("bullets") or raw.get("points") or raw.get("content") or []
    if isinstance(bullets_raw, str):
        bullets_list = [line.strip() for line in bullets_raw.splitlines() if line.strip()]
    elif isinstance(bullets_raw, list):
        bullets_list = [str(b).strip() for b in bullets_raw if str(b).strip()]
    else:
        bullets_list = []
    bullets_list = bullets_list[:_MAX_BULLETS]
    notes = raw.get("notes") or ""
    if not isinstance(notes, str):
        notes = ""
    return Slide(title=title.strip(), bullets=tuple(bullets_list), notes=notes.strip())


def _handle_generate_presentation(arguments: dict[str, Any]) -> str:
    title = arguments.get("title")
    if not isinstance(title, str) or not title.strip():
        return json.dumps({"error": "Нужен непустой title."}, ensure_ascii=False)
    subtitle_raw = arguments.get("subtitle") or ""
    subtitle = subtitle_raw if isinstance(subtitle_raw, str) else ""

    slides_raw = arguments.get("slides")
    if not isinstance(slides_raw, list) or not slides_raw:
        return json.dumps(
            {"error": "slides должен быть непустым списком объектов {title, bullets}."},
            ensure_ascii=False,
        )
    slides_raw = slides_raw[:_MAX_SLIDES]

    normalized: list[Slide] = []
    for i, raw in enumerate(slides_raw):
        result = _normalize_slide(i, raw)
        if isinstance(result, dict):
            return json.dumps(result, ensure_ascii=False)
        normalized.append(result)

    try:
        path = build_presentation(title.strip(), subtitle.strip(), normalized)
    except Exception as e:  # noqa: BLE001
        logger.exception("build_presentation failed")
        return json.dumps({"error": "build_failed", "detail": str(e)}, ensure_ascii=False)

    return json.dumps(
        {
            "filename": path.name,
            "download_url": _public_url(path.name),
            "slide_count": len(normalized) + 1,
            "hint": "В ответе пользователю выдай ссылку download_url — это готовая .pptx-презентация.",
        },
        ensure_ascii=False,
    )


register_tool(
    ToolSpec(
        name="generate_presentation",
        description=(
            "Генерирует .pptx-презентацию и возвращает ссылку для скачивания. "
            "Используй, когда пользователь просит «сделай презентацию», «слайды», «pptx» и т.п. "
            "Сам сформулируй title, subtitle, и список слайдов с title + bullets."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Заголовок титульного слайда.",
                },
                "subtitle": {
                    "type": "string",
                    "description": "Подзаголовок титульного слайда (опционально).",
                },
                "slides": {
                    "type": "array",
                    "description": f"Список слайдов (макс {_MAX_SLIDES}).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Заголовок слайда."},
                            "bullets": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": f"Пункты (макс {_MAX_BULLETS}).",
                            },
                            "notes": {
                                "type": "string",
                                "description": "Заметки спикера (опционально).",
                            },
                        },
                        "required": ["title", "bullets"],
                    },
                },
            },
            "required": ["title", "slides"],
        },
        handler=_handle_generate_presentation,
    )
)
