"""ASR при вложениях и тулах: только MWS-клиент (без пакета services — иначе цикл импортов)."""

from __future__ import annotations

import os
from typing import Any

from certified_turtles.mws_gpt.client import MWSGPTClient


def default_asr_model() -> str:
    return (os.environ.get("CT_ASR_MODEL") or "whisper-1").strip() or "whisper-1"


def transcription_text(result: Any) -> str:
    if isinstance(result, dict) and isinstance(result.get("text"), str):
        return result["text"]
    if isinstance(result, str):
        return result
    return str(result)


def transcribe_bytes(
    client: MWSGPTClient,
    file_bytes: bytes,
    filename: str,
    *,
    language: str | None = None,
) -> str:
    out = client.audio_transcriptions(
        file_bytes,
        filename,
        model=default_asr_model(),
        language=language,
    )
    return transcription_text(out)


def maybe_auto_transcribe_upload(
    raw: bytes,
    display_name: str,
    *,
    language: str | None = None,
) -> str | None:
    """Если CT_CHAT_AUTO_ASR=1 и есть ключ MWS — расшифровка аудио при вложении в чат."""
    flag = (os.environ.get("CT_CHAT_AUTO_ASR") or "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return None
    try:
        client = MWSGPTClient()
    except ValueError:
        return None
    try:
        return transcribe_bytes(client, raw, display_name, language=language)
    except Exception:  # noqa: BLE001
        return None
