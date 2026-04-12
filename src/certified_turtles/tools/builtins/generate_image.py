from __future__ import annotations

import json
import urllib.parse
from typing import Any

from certified_turtles.tools.registry import ToolSpec, register_tool

# Pollinations: бесплатный endpoint без ключа. GET-запросом на image.pollinations.ai
# возвращается сам PNG, поэтому URL удобно отдать модели и вставить в markdown как `![alt](url)`.
_POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"
_DEFAULT_MODEL = "flux"
_ALLOWED_MODELS = frozenset({"flux", "turbo"})
_MIN_SIDE = 256
_MAX_SIDE = 1536
_MAX_PROMPT_CHARS = 2000


def _safe_markdown_alt(s: str) -> str:
    """Квадратные скобки ломают ![alt](url) — убираем."""
    return (s or "").replace("[", "(").replace("]", ")").replace("\n", " ").strip()[:500] or "image"


def _clamp_side(raw: Any, default: int) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(_MIN_SIDE, min(v, _MAX_SIDE))


def _handle_generate_image(arguments: dict[str, Any]) -> str:
    prompt = arguments.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return json.dumps({"error": "Нужен непустой строковый параметр prompt."}, ensure_ascii=False)
    prompt_clean = prompt.strip()
    if len(prompt_clean) > _MAX_PROMPT_CHARS:
        return json.dumps(
            {
                "error": "prompt_too_long",
                "detail": f"Максимум {_MAX_PROMPT_CHARS} символов; сократи описание.",
            },
            ensure_ascii=False,
        )
    width = _clamp_side(arguments.get("width", 1024), 1024)
    height = _clamp_side(arguments.get("height", 1024), 1024)
    model = arguments.get("model") or _DEFAULT_MODEL
    if model not in _ALLOWED_MODELS:
        model = _DEFAULT_MODEL

    encoded_prompt = urllib.parse.quote(prompt_clean, safe="")
    query = urllib.parse.urlencode(
        {"width": width, "height": height, "model": model, "nologo": "true"}
    )
    url = f"{_POLLINATIONS_BASE}/{encoded_prompt}?{query}"
    alt = _safe_markdown_alt(prompt_clean)
    return json.dumps(
        {
            "url": url,
            "width": width,
            "height": height,
            "model": model,
            "prompt": prompt_clean,
            "markdown": f"![{alt}]({url})",
            "hint": "Вставь `markdown` как есть в итоговый ответ — Open WebUI отрендерит картинку инлайн.",
        },
        ensure_ascii=False,
    )


register_tool(
    ToolSpec(
        name="generate_image",
        description=(
            "Генерация изображения по текстовому описанию. Возвращает URL готового PNG "
            "и markdown-строку вида `![prompt](url)` — вставь её в итоговый ответ, и UI отрендерит картинку. "
            "Используй, когда пользователь просит «нарисуй», «сгенерируй картинку», «сделай иллюстрацию» и т.п."
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Подробное описание картинки на английском (лучше) или русском.",
                },
                "width": {
                    "type": "integer",
                    "description": f"Ширина в пикселях, {_MIN_SIDE}–{_MAX_SIDE}, по умолчанию 1024.",
                    "default": 1024,
                },
                "height": {
                    "type": "integer",
                    "description": f"Высота в пикселях, {_MIN_SIDE}–{_MAX_SIDE}, по умолчанию 1024.",
                    "default": 1024,
                },
                "model": {
                    "type": "string",
                    "enum": sorted(_ALLOWED_MODELS),
                    "description": "Модель генерации: `flux` (качество) или `turbo` (скорость).",
                    "default": _DEFAULT_MODEL,
                },
            },
            "required": ["prompt"],
        },
        handler=_handle_generate_image,
    )
)
