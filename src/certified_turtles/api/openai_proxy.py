from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from certified_turtles.agents.json_agent_protocol import (
    extract_user_visible_assistant_text,
    message_text_content,
    patch_completion_assistant_markdown,
)
from certified_turtles.agent_debug_log import agent_logger, debug_clip, summarize_messages
from certified_turtles.mws_gpt.client import MWSGPTError, http_status_for_mws_error
from certified_turtles.chat_modes import prepare_chat_request, resolve_mode_path_segment
from certified_turtles.model_mode import (
    apply_virtual_model_to_body,
    merge_virtual_models_openai_payload,
    should_merge_virtual_models_into_list,
)
from certified_turtles.services.llm import LLMService, clamp_agent_tool_rounds

router = APIRouter(tags=["openai-proxy"])
_proxy_log = agent_logger("openai_proxy")

_PASS_THROUGH_IGNORE = {
    "model",
    "messages",
    "stream",
    "max_tool_rounds",
    "tools",
    "tool_choice",
    "use_agent",
    "ct_use_agent",
    "agent_mode",
    "ct_mode",
}


def _service() -> LLMService:
    try:
        return LLMService.from_env()
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/v1/models")
async def list_models() -> Any:
    svc = _service()
    try:
        # list_models ходит в MWS по сети — не блокируем event loop (параллель с /v1/chat/completions).
        raw = await asyncio.to_thread(svc.list_models)
        if should_merge_virtual_models_into_list():
            return merge_virtual_models_openai_payload(raw)
        return raw
    except MWSGPTError as e:
        raise HTTPException(
            status_code=http_status_for_mws_error(e),
            detail={"message": str(e), "status": e.status, "body": e.body},
        ) from e


@router.get("/v1/plain/models")
async def list_models_plain_prefix() -> Any:
    """Тот же /v1/models, если в Open WebUI заведено отдельное подключение с base …/v1/plain."""
    return await list_models()


@router.get("/v1/m/{mode}/models")
async def list_models_for_mode(mode: str) -> Any:
    """Список моделей MWS без размножения; режим задаётся URL подключения (см. POST …/v1/m/{mode}/chat/completions)."""
    try:
        resolve_mode_path_segment(mode)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    svc = _service()
    try:
        return await asyncio.to_thread(svc.list_models)
    except MWSGPTError as e:
        raise HTTPException(
            status_code=http_status_for_mws_error(e),
            detail={"message": str(e), "status": e.status, "body": e.body},
        ) from e


@router.get("/v1/m/{mode}/plain/models")
async def list_models_for_mode_plain(mode: str) -> Any:
    """То же для base …/v1/m/{mode}/plain."""
    return await list_models_for_mode(mode)


async def _audio_transcriptions_impl(
    file: UploadFile,
    model: str | None,
    language: str | None,
    prompt: str | None,
    response_format: str | None,
) -> Any:
    svc = _service()
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Пустой файл")
    fn = file.filename or "audio.bin"
    try:
        return await asyncio.to_thread(
            svc.client.audio_transcriptions,
            raw,
            fn,
            model=model,
            language=language,
            prompt=prompt,
            response_format=response_format,
        )
    except MWSGPTError as e:
        raise HTTPException(
            status_code=http_status_for_mws_error(e),
            detail={"message": str(e), "status": e.status, "body": e.body},
        ) from e


@router.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    file: UploadFile = File(...),
    model: str | None = Form(None),
    language: str | None = Form(None),
    prompt: str | None = Form(None),
    response_format: str | None = Form(None),
) -> Any:
    """OpenAI-совместимый ASR: прокси на MWS. В Open WebUI: AUDIO_STT_ENGINE=openai и тот же API base."""
    return await _audio_transcriptions_impl(file, model, language, prompt, response_format)


@router.post("/v1/plain/audio/transcriptions")
async def audio_transcriptions_plain_prefix(
    file: UploadFile = File(...),
    model: str | None = Form(None),
    language: str | None = Form(None),
    prompt: str | None = Form(None),
    response_format: str | None = Form(None),
) -> Any:
    """Тот же ASR при base URL …/v1/plain."""
    return await _audio_transcriptions_impl(file, model, language, prompt, response_format)


def _completion_with_visible_markdown(completion: dict[str, Any]) -> dict[str, Any]:
    """Убирает обёртку JSON-протокола и служебные поля message — Open WebUI показывает markdown."""
    choices = completion.get("choices")
    if not isinstance(choices, list) or not choices:
        return completion
    ch0 = choices[0]
    if not isinstance(ch0, dict):
        return completion
    msg = ch0.get("message")
    if not isinstance(msg, dict):
        return completion
    raw = message_text_content(msg)
    visible = extract_user_visible_assistant_text(raw)
    return patch_completion_assistant_markdown(completion, visible)


def _final_assistant_content(completion: dict[str, Any]) -> str:
    choices = completion.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    if not isinstance(msg, dict):
        return ""
    return extract_user_visible_assistant_text(message_text_content(msg))


# Длинный ответ одним SSE-событием ломает JSON.parse в Open WebUI (ошибка вида column ~3k в chunk JS).
_SSE_CONTENT_CHUNK_CHARS = 400


def _sse_stream(model: str, completion: dict[str, Any]):
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    content = _final_assistant_content(completion)

    def _line(delta: dict[str, Any], finish_reason: str | None) -> str:
        payload = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # Как у OpenAI: сначала роль, затем куски content, в конце finish_reason (короткие JSON-строки).
    yield _line({"role": "assistant"}, None)
    for i in range(0, len(content), _SSE_CONTENT_CHUNK_CHARS):
        piece = content[i : i + _SSE_CONTENT_CHUNK_CHARS]
        yield _line({"content": piece}, None)
    yield _line({}, "stop")
    yield "data: [DONE]\n\n"


def _wants_plain_chat(body: dict[str, Any]) -> bool:
    """Режим «просто чат»: без агента и тулов (см. use_agent в теле или отдельный URL /v1/plain/...)."""
    v = body.get("use_agent", body.get("ct_use_agent", True))
    mode = body.get("agent_mode")
    if isinstance(mode, str) and mode.strip().lower() in ("plain", "chat", "off", "false", "0"):
        return True
    if isinstance(v, str):
        return v.strip().lower() in ("0", "false", "off", "no", "plain", "chat")
    return v is False


def _openwebui_meta_task_forces_plain(messages: Any) -> bool:
    """Open WebUI шлёт отдельные POST с одним user и префиксом «### Task:» (заголовок чата, follow-up, web search …).

    Их нельзя гонять через JSON-протокол агента — модель отвечает обычным текстом → ложные «parse failed» в логах.
    Основной RAG-ответ («…provided context…» + при необходимости <source>) оставляем на агенте с тулами.
    """
    if not isinstance(messages, list) or len(messages) != 1:
        return False
    m = messages[0]
    if not isinstance(m, dict) or m.get("role") != "user":
        return False
    text = message_text_content(m)
    if not text.lstrip().startswith("### Task:"):
        return False
    low = text.lower()
    if "<source" in low:
        return False
    if "respond to the user query using the provided context" in low:
        return False
    return True


def _openwebui_tool_router_forces_plain(messages: Any) -> bool:
    """Open WebUI иногда шлёт отдельный запрос-роутер вида `Available Tools: []` + `Query: ...`.

    Это не пользовательский чат и не место для нашего agent-loop: там нет file_id/вложений/RAG,
    а конфликт системных промптов заставляет модель бессмысленно крутить workspace_file_path с пустым id.
    """
    if not isinstance(messages, list) or len(messages) != 2:
        return False
    sys_msg, user_msg = messages
    if not isinstance(sys_msg, dict) or not isinstance(user_msg, dict):
        return False
    if sys_msg.get("role") != "system" or user_msg.get("role") != "user":
        return False
    sys_text = message_text_content(sys_msg)
    user_text = message_text_content(user_msg)
    sys_low = sys_text.lower()
    if "available tools:" not in sys_low:
        return False
    if "choose and return the correct tool" not in sys_low:
        return False
    if not user_text.lstrip().lower().startswith("query:"):
        return False
    return True


async def _chat_completions_from_body(body: dict[str, Any], *, force_plain: bool) -> Any:
    model = body.get("model")
    messages = body.get("messages")
    if not isinstance(model, str) or not model:
        raise HTTPException(status_code=400, detail="Поле `model` обязательно")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="Поле `messages` обязательно и не должно быть пустым")

    stream = bool(body.get("stream"))
    max_tool_rounds = clamp_agent_tool_rounds(body.get("max_tool_rounds", 10))
    extra = {k: v for k, v in body.items() if k not in _PASS_THROUGH_IGNORE}

    svc = _service()
    ow_meta_plain = _openwebui_meta_task_forces_plain(messages)
    ow_tool_router_plain = _openwebui_tool_router_forces_plain(messages)
    plain = force_plain or _wants_plain_chat(body) or ow_meta_plain or ow_tool_router_plain
    if plain:
        prepared = prepare_chat_request(body, messages, for_agent=False)
        messages = prepared.messages
    else:
        prepared = prepare_chat_request(body, messages, for_agent=True)
        messages = prepared.messages
        if prepared.max_tool_rounds_override is not None:
            max_tool_rounds = max(max_tool_rounds, prepared.max_tool_rounds_override)
        if prepared.mode_applied:
            _proxy_log.debug("ct_mode applied=%s max_tool_rounds=%s", prepared.mode_applied, max_tool_rounds)
    if ow_meta_plain and not force_plain and not _wants_plain_chat(body):
        _proxy_log.debug("openwebui auxiliary ### Task -> plain chat (no agent JSON protocol)")
    if ow_tool_router_plain and not force_plain and not _wants_plain_chat(body):
        _proxy_log.debug("openwebui Available Tools router -> plain chat (no agent JSON protocol)")
    _proxy_log.debug(
        "chat_completions request model=%s plain=%s stream=%s max_tool_rounds=%s extra_keys=%s\nmessages_in=\n%s",
        model,
        plain,
        stream,
        max_tool_rounds,
        sorted(extra.keys()),
        summarize_messages(messages, preview=400) if isinstance(messages, list) else str(type(messages)),
    )
    try:
        if plain:
            completion = await asyncio.to_thread(svc.chat_plain, model, messages, **extra)
        else:
            out = await asyncio.to_thread(
                svc.run_agent,
                model,
                messages,
                max_tool_rounds=max_tool_rounds,
                **extra,
            )
            completion = out.get("completion") or {}
    except MWSGPTError as e:
        raise HTTPException(
            status_code=http_status_for_mws_error(e),
            detail={"message": str(e), "status": e.status, "body": e.body},
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    _proxy_log.debug(
        "chat_completions response (visible for UI) preview=\n%s",
        debug_clip(_final_assistant_content(completion)),
    )
    completion = _completion_with_visible_markdown(completion)
    if not stream:
        return completion
    return StreamingResponse(_sse_stream(model, completion), media_type="text/event-stream")


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Ожидается JSON: {e}") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидается JSON-объект")
    apply_virtual_model_to_body(body)
    return await _chat_completions_from_body(body, force_plain=False)


@router.post("/v1/plain/chat/completions")
async def chat_completions_plain(request: Request) -> Any:
    """Тот же OpenAI-контракт, но всегда без агентского цикла — для второй «подключки» в Open WebUI (base …/v1/plain)."""
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Ожидается JSON: {e}") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидается JSON-объект")
    apply_virtual_model_to_body(body)
    return await _chat_completions_from_body(body, force_plain=True)


@router.post("/v1/m/{mode}/chat/completions")
async def chat_completions_with_mode(mode: str, request: Request) -> Any:
    """Режим из пути URL (отдельное подключение Open WebUI: base …/v1/m/deep_research) — без длинного списка моделей."""
    try:
        mode_id = resolve_mode_path_segment(mode)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Ожидается JSON: {e}") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидается JSON-объект")
    body["ct_mode"] = mode_id
    apply_virtual_model_to_body(body)
    return await _chat_completions_from_body(body, force_plain=False)


@router.post("/v1/m/{mode}/plain/chat/completions")
async def chat_completions_with_mode_plain(mode: str, request: Request) -> Any:
    """Режим из пути + plain-чат (base …/v1/m/{mode}/plain)."""
    try:
        mode_id = resolve_mode_path_segment(mode)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Ожидается JSON: {e}") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидается JSON-объект")
    body["ct_mode"] = mode_id
    apply_virtual_model_to_body(body)
    return await _chat_completions_from_body(body, force_plain=True)
