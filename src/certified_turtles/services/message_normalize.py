from __future__ import annotations

import base64
import json
from typing import Any


def _part_to_blocks(part: Any) -> list[dict[str, Any]]:
    """Преобразует один элемент `content` (OpenAI/Open WebUI) в блоки для API модели."""
    if not isinstance(part, dict):
        return [{"type": "text", "text": str(part)[:16000]}]

    ptype = part.get("type")
    if ptype in ("text", "input_text"):
        key = "text" if "text" in part else "input_text"
        raw = part.get("text") if key == "text" else part.get("input_text")
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        return [{"type": "text", "text": text[:16000]}]

    if ptype == "image_url":
        iu = part.get("image_url")
        if isinstance(iu, dict) and isinstance(iu.get("url"), str):
            return [{"type": "image_url", "image_url": {"url": iu["url"], **{k: v for k, v in iu.items() if k != "url"}}}]
        return [{"type": "text", "text": json.dumps(part, ensure_ascii=False)[:8000]}]

    # Open WebUI / клиенты: вложение как file с base64 или ссылкой
    if ptype in ("file", "input_file"):
        name = part.get("filename") or part.get("name") or "attachment"
        mime = part.get("mime_type") or part.get("mimeType") or ""
        data = part.get("file") or part.get("data") or part.get("content")
        if isinstance(data, dict) and isinstance(data.get("url"), str):
            return [{"type": "text", "text": f"[файл {name} ({mime})]: {data['url'][:4000]}"}]
        if isinstance(data, str) and data.startswith("data:"):
            head, _, b64 = data.partition(",")
            try:
                raw = base64.b64decode(b64, validate=False)
            except Exception:  # noqa: BLE001
                raw = b""
            snippet = raw[:12000].decode("utf-8", errors="replace")
            return [
                {
                    "type": "text",
                    "text": (
                        f"[вложение «{name}», {mime or head}, текстовое начало до 12k символов]\n{snippet}"
                    ),
                }
            ]
        if isinstance(data, str) and len(data) < 2000:
            return [{"type": "text", "text": f"[вложение «{name}»]: {data}"}]
        return [{"type": "text", "text": f"[вложение «{name}» ({mime}) — бинарные данные, см. загрузку на API]"}]

    return [{"type": "text", "text": json.dumps(part, ensure_ascii=False)[:8000]}]


def normalize_message_content(content: Any) -> str | list[dict[str, Any]]:
    """Строка остаётся строкой; список частей — в OpenAI-мультимодальный вид (text/image_url)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for part in content:
            blocks.extend(_part_to_blocks(part))
        merged: list[dict[str, Any]] = []
        for b in blocks:
            if b.get("type") == "text" and merged and merged[-1].get("type") == "text":
                merged[-1]["text"] = (merged[-1].get("text") or "") + "\n" + (b.get("text") or "")
            else:
                merged.append(b)
        if len(merged) == 1 and merged[0].get("type") == "text":
            return merged[0].get("text") or ""
        return merged
    return str(content)


def normalize_chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Копия сообщений с нормализованным полем `content` (совместимость с Open WebUI)."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        mm = dict(m)
        if "content" in mm:
            mm["content"] = normalize_message_content(mm.get("content"))
        out.append(mm)
    return out
