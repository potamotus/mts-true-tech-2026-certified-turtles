from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from certified_turtles.asr_upload import default_asr_model, transcribe_bytes
from certified_turtles.mws_gpt.client import MWSGPTClient, MWSGPTError
from certified_turtles.tools.registry import ToolSpec, register_tool
from certified_turtles.tools.workspace_storage import uploads_dir

_AUDIO_SUFFIXES = frozenset({".mp3", ".wav", ".m4a", ".ogg", ".webm", ".flac"})


def _max_asr_bytes() -> int:
    try:
        return int(os.environ.get("CT_ASR_MAX_BYTES", os.environ.get("UPLOAD_MAX_BYTES", str(12 * 1024 * 1024))))
    except (TypeError, ValueError):
        return 12 * 1024 * 1024


def _handle_transcribe_workspace_audio(arguments: dict[str, Any]) -> str:
    file_id = arguments.get("file_id")
    if not isinstance(file_id, str) or not file_id.strip():
        return json.dumps({"error": "Нужен непустой file_id (аудио из чата или POST /api/v1/uploads)."}, ensure_ascii=False)
    name = Path(file_id.strip()).name
    if name != file_id.strip() or ".." in file_id or "/" in file_id or "\\" in file_id:
        return json.dumps({"error": "Некорректный file_id."}, ensure_ascii=False)
    if Path(name).suffix.lower() not in _AUDIO_SUFFIXES:
        return json.dumps(
            {"error": "Ожидается аудиофайл (.mp3, .wav, .m4a, …)."},
            ensure_ascii=False,
        )
    path = uploads_dir() / name
    if not path.is_file():
        return json.dumps({"error": "Файл не найден в рабочей области."}, ensure_ascii=False)
    try:
        raw = path.read_bytes()
    except OSError as e:
        return json.dumps({"error": "read_failed", "detail": str(e)}, ensure_ascii=False)
    lim = max(1_000_000, min(_max_asr_bytes(), 50 * 1024 * 1024))
    if len(raw) > lim:
        return json.dumps(
            {"error": "audio_too_large", "detail": f"Лимит {lim} байт; сожми или разрежь файл."},
            ensure_ascii=False,
        )
    lang = arguments.get("language")
    language = lang.strip() if isinstance(lang, str) and lang.strip() else None
    try:
        client = MWSGPTClient()
        text = transcribe_bytes(client, raw, path.name, language=language)
    except MWSGPTError as e:
        return json.dumps(
            {"error": "transcription_failed", "detail": str(e), "http_status": e.status},
            ensure_ascii=False,
        )
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "transcription_failed", "detail": str(e)}, ensure_ascii=False)
    return json.dumps(
        {"file_id": name, "model": default_asr_model(), "text": text},
        ensure_ascii=False,
    )


register_tool(
    ToolSpec(
        name="transcribe_workspace_audio",
        description=(
            "Расшифровать аудио из рабочей области (вложение в чат или POST /api/v1/uploads): "
            "POST /v1/audio/transcriptions на стороне MWS. Передай file_id. "
            "Опционально language (ISO-639-1), например ru."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "Идентификатор аудиофайла в uploads."},
                "language": {
                    "type": "string",
                    "description": "Необязательно: код языка для Whisper (например ru, en).",
                },
            },
            "required": ["file_id"],
        },
        handler=_handle_transcribe_workspace_audio,
    )
)
