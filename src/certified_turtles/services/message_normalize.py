from __future__ import annotations

import base64
import csv
import io
import json
import re
from html import unescape
from pathlib import Path
from typing import Any

from certified_turtles.agent_debug_log import agent_logger, debug_clip
from certified_turtles.asr_upload import maybe_auto_transcribe_upload
from certified_turtles.tools.workspace_storage import extension_allowed, save_workspace_file

_norm_log = agent_logger("message_normalize")

_AUDIO_EXTS = frozenset({".mp3", ".wav", ".m4a", ".ogg", ".webm", ".flac"})


def _is_audio_kind(mime: str, name: str) -> bool:
    ml = (mime or "").lower()
    if ml.startswith("audio/"):
        return True
    return Path(name).suffix.lower() in _AUDIO_EXTS

# Open WebUI RAG: <source id="1" name="file.csv">…текст документа…</source>
_SOURCE_BLOCK_RE = re.compile(r"<source\s+([^>]+)>([\s\S]*?)</source>", re.IGNORECASE)
_CONTEXT_BLOCK_RE = re.compile(r"<context>([\s\S]*?)</context>", re.IGNORECASE)


def _source_attr(attrs: str, key: str) -> str:
    m = re.search(rf'\b{re.escape(key)}\s*=\s*"([^"]*)"', attrs, re.IGNORECASE)
    return unescape(m.group(1)).strip() if m else ""


def _stored_name_for_rag_body(cit_id: str, src_name: str, body: str) -> str:
    raw_name = (src_name or "").strip()
    base = Path(raw_name).name if raw_name else ""
    if base and extension_allowed(base):
        return base
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if len(lines) >= 2 and "," in lines[0]:
        stem = Path(base).stem if base else f"source_{cit_id}"
        return f"{stem or f'source_{cit_id}'}.csv"
    return f"source_{cit_id}.txt"


def _rag_source_bytes(filename: str, body: str) -> bytes:
    """Пытается восстановить CSV/TSV из key:value чанка RAG; иначе пишет исходный текст."""
    suffix = Path(filename).suffix.lower()
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if suffix not in (".csv", ".tsv") or len(lines) < 2:
        return body.encode("utf-8")
    pairs: list[tuple[str, str]] = []
    for ln in lines:
        if ":" not in ln:
            return body.encode("utf-8")
        k, v = ln.split(":", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            return body.encode("utf-8")
        pairs.append((k, v))
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=[k for k, _ in pairs], delimiter="," if suffix == ".csv" else "\t")
    writer.writeheader()
    writer.writerow({k: v for k, v in pairs})
    return buf.getvalue().encode("utf-8")


def _hydrate_openwebui_rag_sources_segment(text: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _SOURCE_BLOCK_RE.finditer(text):
        out.append(text[pos : m.start()])
        attrs, body = m.group(1), (m.group(2) or "").strip()
        cit_id = _source_attr(attrs, "id") or "0"
        src_name = _source_attr(attrs, "name")
        block = m.group(0)
        if not body:
            out.append(block)
            pos = m.end()
            continue
        tail = text[m.end() : m.end() + 120]
        if "[CT: RAG-источник" in tail:
            out.append(block)
            pos = m.end()
            continue
        fn = _stored_name_for_rag_body(cit_id, src_name, body)
        if not extension_allowed(fn):
            out.append(block)
            pos = m.end()
            continue
        raw = _rag_source_bytes(fn, body)
        try:
            fid = save_workspace_file(fn, raw)
        except ValueError:
            out.append(block)
            pos = m.end()
            continue
        _norm_log.debug(
            "RAG <source> hydrated file_id=%s stored_as=%s body_preview=%s",
            fid,
            fn,
            debug_clip(body),
        )
        note = (
            f'\n[CT: RAG-источник сохранён для тулов. file_id="{fid}". '
            "Атрибут id в <source> — номер цитаты Open WebUI, не подставляй его в workspace_file_path.]\n"
        )
        out.append(block + note)
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _hydrate_openwebui_rag_sources(text: str) -> str:
    """
    Сохраняет тело каждого <source> (RAG Open WebUI) в UPLOADS_DIR и добавляет file_id для тулов.
    Иначе модель путает citation id с file_id и получает «файл не найден».

    Важно: гидратируем только реальные блоки внутри <context>...</context>, чтобы не схватить
    пример `<source id="1">` из инструкций Open WebUI.
    """
    if not text or "<source" not in text.lower():
        return text
    if "<context>" not in text.lower():
        return _hydrate_openwebui_rag_sources_segment(text)

    out: list[str] = []
    pos = 0
    for m in _CONTEXT_BLOCK_RE.finditer(text):
        out.append(text[pos : m.start()])
        inner = m.group(1) or ""
        out.append("<context>" + _hydrate_openwebui_rag_sources_segment(inner) + "</context>")
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _unwrap_file_payload(data: Any, *, max_depth: int = 6) -> Any:
    """Достаёт строку/объект с url из вложенных dict (форматы Open WebUI / клиентов)."""
    cur: Any = data
    for _ in range(max_depth):
        if not isinstance(cur, dict):
            return cur
        if isinstance(cur.get("url"), str):
            return cur
        nxt: Any = None
        for key in ("data", "content", "file"):
            v = cur.get(key)
            if isinstance(v, (str, dict)):
                nxt = v
                break
        if nxt is None:
            return cur
        cur = nxt
    return cur


def _parse_data_url(s: str) -> tuple[bytes, str] | None:
    if not s.startswith("data:"):
        return None
    head, _, b64 = s.partition(",")
    meta = head[5:]
    mime = ""
    for part in meta.split(";"):
        p = part.strip()
        if p.lower() == "base64":
            continue
        if "/" in p:
            mime = p
            break
    try:
        raw = base64.b64decode(b64, validate=False)
    except Exception:  # noqa: BLE001
        return None
    return raw, mime


def _try_decode_b64_string(s: str) -> bytes | None:
    s2 = s.strip()
    if len(s2) < 8 or s2.startswith("data:"):
        return None
    try:
        raw = base64.b64decode(s2, validate=False)
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    return raw


def _filename_for_persist(original: str, mime: str) -> str:
    name = (original or "attachment").strip() or "attachment"
    base = Path(name).name
    if extension_allowed(base):
        return base
    ml = (mime or "").lower()
    stem = Path(base).stem or "attachment"
    if "csv" in ml:
        return f"{stem}.csv"
    if "tsv" in ml or "tab-separated-values" in ml:
        return f"{stem}.tsv"
    if "excel" in ml or "spreadsheetml" in ml:
        return f"{stem}.xlsx"
    return base


def _persist_chat_attachment(raw: bytes, display_name: str, mime: str) -> str | None:
    fn = _filename_for_persist(display_name, mime)
    if not extension_allowed(fn):
        return None
    try:
        return save_workspace_file(fn, raw)
    except ValueError:
        return None


def _file_part_to_blocks(part: dict[str, Any]) -> list[dict[str, Any]]:
    name = part.get("filename") or part.get("name") or "attachment"
    mime = part.get("mime_type") or part.get("mimeType") or ""
    data = part.get("file") or part.get("data") or part.get("content")

    if isinstance(data, dict) and isinstance(data.get("url"), str):
        return [{"type": "text", "text": f"[файл {name} ({mime})]: {data['url'][:4000]}"}]

    data = _unwrap_file_payload(data)
    if isinstance(data, dict) and isinstance(data.get("url"), str):
        return [{"type": "text", "text": f"[файл {name} ({mime})]: {data['url'][:4000]}"}]

    raw: bytes | None = None
    mime_from_payload = ""
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    elif isinstance(data, str):
        du = _parse_data_url(data)
        if du:
            raw, mime_from_payload = du
        else:
            raw = _try_decode_b64_string(data)

    effective_mime = mime_from_payload or mime
    if raw is not None:
        fid = _persist_chat_attachment(raw, str(name), effective_mime)
        if fid:
            if _is_audio_kind(effective_mime, str(name)):
                transcript = maybe_auto_transcribe_upload(raw, str(name))
                if transcript:
                    return [
                        {
                            "type": "text",
                            "text": (
                                "[аудио: автоматическая расшифровка (ASR через MWS)]\n"
                                f"{transcript}\n\n"
                                f"[file_id: {fid}; оригинальное имя: {name}; "
                                "при сбое или для другого языка — transcribe_workspace_audio]"
                            ),
                        }
                    ]
                return [
                    {
                        "type": "text",
                        "text": (
                            "[аудио сохранено в рабочую область агента]\n"
                            f"file_id: {fid}\n"
                            f"оригинальное имя: {name}\n"
                            "Вызови transcribe_workspace_audio с этим file_id (или включи CT_CHAT_AUTO_ASR=1 "
                            "для автоматической расшифровки при вложении)."
                        ),
                    },
                ]
            return [
                {
                    "type": "text",
                    "text": (
                        "[сервер сохранил вложение в рабочую область агента]\n"
                        f"file_id: {fid}\n"
                        f"оригинальное имя: {name}\n"
                        "Вызови workspace_file_path с этим file_id, затем execute_python — "
                        "не проси пользователя вручную POST /api/v1/uploads."
                    ),
                }
            ]
        snippet = raw[:12000].decode("utf-8", errors="replace")
        return [
            {
                "type": "text",
                "text": (
                    f"[вложение «{name}», {mime or effective_mime or 'бинарные данные'}; "
                    f"сохранить в область не вышло (тип/размер); фрагмент до 12k символов]\n{snippet}"
                ),
            }
        ]

    if isinstance(data, str) and len(data) < 2000:
        return [{"type": "text", "text": f"[вложение «{name}»]: {data}"}]
    return [{"type": "text", "text": f"[вложение «{name}» ({mime}) — бинарные данные, см. загрузку на API]"}]


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

    if ptype == "input_image":
        ii = part.get("input_image")
        if isinstance(ii, dict) and isinstance(ii.get("url"), str):
            return [{"type": "image_url", "image_url": {"url": ii["url"]}}]
        return [{"type": "text", "text": json.dumps(part, ensure_ascii=False)[:8000]}]

    if ptype == "input_audio":
        ia = part.get("input_audio")
        if not isinstance(ia, dict):
            return [{"type": "text", "text": json.dumps(part, ensure_ascii=False)[:8000]}]
        fmt = (ia.get("format") or "wav").strip() or "wav"
        data = ia.get("data")
        raw_audio: bytes | None = None
        if isinstance(data, str):
            du = _parse_data_url(data)
            if du:
                raw_audio, _ = du
            else:
                raw_audio = _try_decode_b64_string(data)
                if raw_audio is None and data.strip():
                    try:
                        raw_audio = base64.b64decode(data.strip(), validate=False)
                    except Exception:  # noqa: BLE001
                        raw_audio = None
        if raw_audio is None:
            return [{"type": "text", "text": f"[input_audio: нет декодируемых данных, format={fmt}]"}]
        mime = f"audio/{fmt}" if fmt in ("wav", "mp3", "webm", "ogg", "flac", "m4a") else "audio/wav"
        return _file_part_to_blocks(
            {"type": "file", "filename": f"recording.{fmt}", "mime_type": mime, "file": raw_audio},
        )

    if ptype in ("file", "input_file"):
        return _file_part_to_blocks(part)

    return [{"type": "text", "text": json.dumps(part, ensure_ascii=False)[:8000]}]


def normalize_message_content(content: Any) -> str | list[dict[str, Any]]:
    """Строка остаётся строкой; список частей — в OpenAI-мультимодальный вид (text/image_url)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return _hydrate_openwebui_rag_sources(content)
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
            return _hydrate_openwebui_rag_sources(merged[0].get("text") or "")
        for b in merged:
            if b.get("type") == "text" and isinstance(b.get("text"), str):
                b["text"] = _hydrate_openwebui_rag_sources(b["text"])
        return merged
    return _hydrate_openwebui_rag_sources(str(content))


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
