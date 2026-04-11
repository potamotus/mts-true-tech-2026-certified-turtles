from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from pptx import Presentation
from pptx.util import Inches, Pt


@dataclass(frozen=True)
class Slide:
    title: str
    bullets: tuple[str, ...]
    notes: str = ""


def _storage_dir() -> Path:
    root = os.environ.get("GENERATED_FILES_DIR", "/tmp/certified_turtles_files")
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _slugify(text: str, *, fallback: str = "presentation") -> str:
    base = _SAFE_FILENAME.sub("-", text.strip()).strip("-")
    return base or fallback


def build_presentation(title: str, subtitle: str, slides: list[Slide]) -> Path:
    """Строит .pptx из заголовка, подзаголовка и списка слайдов. Возвращает путь к файлу."""
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    title_layout = prs.slide_layouts[0]
    title_slide = prs.slides.add_slide(title_layout)
    title_slide.shapes.title.text = title
    if len(title_slide.placeholders) > 1:
        title_slide.placeholders[1].text = subtitle or ""

    bullet_layout = prs.slide_layouts[1]
    for slide in slides:
        s = prs.slides.add_slide(bullet_layout)
        s.shapes.title.text = slide.title or ""
        body = s.placeholders[1] if len(s.placeholders) > 1 else None
        if body is not None:
            tf = body.text_frame
            tf.clear()
            for idx, bullet in enumerate(slide.bullets):
                para = tf.paragraphs[0] if idx == 0 else tf.add_paragraph()
                para.text = bullet
                para.font.size = Pt(20)
                para.level = 0
        if slide.notes:
            s.notes_slide.notes_text_frame.text = slide.notes

    filename = f"{_slugify(title)[:60]}-{uuid.uuid4().hex[:8]}.pptx"
    out = _storage_dir() / filename
    prs.save(out)
    return out
