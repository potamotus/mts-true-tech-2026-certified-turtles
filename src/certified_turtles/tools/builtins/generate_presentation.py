from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.parse import urlparse

from certified_turtles.tools.presentation import Slide, SlideKind, build_presentation
from certified_turtles.tools.registry import ToolSpec, register_tool

logger = logging.getLogger(__name__)

_MAX_SLIDES = 25
_MAX_BULLETS = 10
_MAX_TITLE_LEN = 200
_MAX_SUBTITLE_LEN = 300
_SLIDE_KINDS: frozenset[str] = frozenset({"content", "section", "thanks", "image"})


def _public_url(filename: str) -> str:
    """Ссылка, по которой файл отдаётся FastAPI-приложением."""
    base = os.environ.get("PUBLIC_API_BASE_URL", "http://localhost:8000")
    return f"{base.rstrip('/')}/files/{filename}"


def _parse_slide_kind(raw: dict[str, Any]) -> SlideKind:
    for key in ("kind", "type", "slide_type", "layout"):
        v = raw.get(key)
        if isinstance(v, str) and v.strip().lower() in _SLIDE_KINDS:
            return v.strip().lower()  # type: ignore[return-value]
    return "content"


def _short_url_hint(url: str) -> str:
    try:
        p = urlparse(url)
        host = (p.netloc or "").split("@")[-1]
        if host:
            return host
    except ValueError:
        pass
    return "источник"


def _default_image_caption(title: str, image_url: str | None) -> str:
    """Подпись к слайду image, если модель не передала bullets/caption."""
    t = title.strip()
    if t:
        return t
    return f"Иллюстрация ({_short_url_hint(image_url or '')})"


def _normalize_slide(idx: int, raw: Any) -> Slide | dict[str, str]:
    if not isinstance(raw, dict):
        return {"error": f"slides[{idx}] должен быть объектом с title/bullets"}
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return {"error": f"slides[{idx}].title обязателен"}
    kind = _parse_slide_kind(raw)
    bullets_raw = raw.get("bullets") or raw.get("points") or raw.get("content") or []
    if isinstance(bullets_raw, str):
        bullets_list = [line.strip() for line in bullets_raw.splitlines() if line.strip()]
    elif isinstance(bullets_raw, list):
        bullets_list = [str(b).strip() for b in bullets_raw if str(b).strip()]
    else:
        bullets_list = []
    bullets_list = bullets_list[:_MAX_BULLETS]

    caption_raw = raw.get("caption") or raw.get("subtitle")
    if isinstance(caption_raw, str) and caption_raw.strip() and not bullets_list:
        bullets_list = [caption_raw.strip()]

    image_url = raw.get("image_url") or raw.get("image") or raw.get("picture_url")
    if isinstance(image_url, str):
        image_url = image_url.strip() or None
    else:
        image_url = None
    if kind == "image" and not image_url:
        return {"error": f"slides[{idx}]: для image укажи image_url (HTTPS-ссылка на картинку)."}

    if kind == "content" and not bullets_list:
        return {"error": f"slides[{idx}]: для content нужен непустой bullets"}
    if kind == "image" and not bullets_list:
        bullets_list = [_default_image_caption(title.strip(), image_url)]

    notes = raw.get("notes") or ""
    if not isinstance(notes, str):
        notes = ""
    return Slide(
        title=title.strip(),
        bullets=tuple(bullets_list),
        notes=notes.strip(),
        kind=kind,
        image_url=image_url,
    )


def _handle_generate_presentation(arguments: dict[str, Any]) -> str:
    title = arguments.get("title")
    if not isinstance(title, str) or not title.strip():
        return json.dumps({"error": "Нужен непустой title."}, ensure_ascii=False)
    if len(title.strip()) > _MAX_TITLE_LEN:
        return json.dumps({"error": f"title слишком длинный (макс {_MAX_TITLE_LEN} символов)."}, ensure_ascii=False)
    subtitle_raw = arguments.get("subtitle") or ""
    subtitle = subtitle_raw if isinstance(subtitle_raw, str) else ""
    if len(subtitle) > _MAX_SUBTITLE_LEN:
        return json.dumps({"error": f"subtitle слишком длинный (макс {_MAX_SUBTITLE_LEN})."}, ensure_ascii=False)

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
            "Генерирует .pptx-презентацию (шаблон Office с типами слайдов) и возвращает ссылку для скачивания. "
            "Используй для запросов «презентация», «слайды», «pptx». "
            "Задай title, subtitle (опционально), slides. "
            "kind: content (нужен bullets), section/thanks (bullets можно []), "
            "image — обязателен image_url (прямая ссылка на png/jpg/webp/gif); bullets или caption опциональны "
            "(если пусто — подпись возьмётся из title). "
            "URL картинок — из web_search или прямые ссылки с сайтов."
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
                            "kind": {
                                "type": "string",
                                "enum": ["content", "section", "thanks", "image"],
                                "description": "Тип слайда: content | section | thanks | image.",
                            },
                            "bullets": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": f"Пункты (макс {_MAX_BULLETS}). content: обязательны. "
                                f"image: опционально (иначе подпись = title). thanks/section: можно [].",
                            },
                            "caption": {
                                "type": "string",
                                "description": "Для kind=image — короткая подпись под фото (альтернатива одному элементу bullets).",
                            },
                            "image_url": {
                                "type": "string",
                                "description": "Для kind=image — прямая http(s)-ссылка на изображение (png/jpg/webp/gif).",
                            },
                            "notes": {
                                "type": "string",
                                "description": "Заметки спикера (опционально).",
                            },
                        },
                        "required": ["title"],
                    },
                },
            },
            "required": ["title", "slides"],
        },
        handler=_handle_generate_presentation,
    )
)
