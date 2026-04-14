from __future__ import annotations

import asyncio
import copy
import json
import os
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
from certified_turtles.memory_runtime import RequestContext, runtime_from_env
from certified_turtles.mws_gpt.client import MWSGPTError, http_status_for_mws_error
from certified_turtles.chat_modes import prepare_chat_request, resolve_mode_path_segment
from certified_turtles.model_mode import (
    apply_virtual_model_to_body,
    merge_virtual_models_openai_payload,
    should_merge_virtual_models_into_list,
)
from certified_turtles.services.llm import LLMService, clamp_agent_tool_rounds
from certified_turtles.services.message_normalize import normalize_chat_messages

router = APIRouter(tags=["openai-proxy"])
_proxy_log = agent_logger("openai_proxy")

_PASS_THROUGH_IGNORE = {
    "model",
    "messages",
    "stream",
    "max_agent_tokens",
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


def _mws_image_chat_model_ids() -> set[str]:
    """Модели MWS, у которых генерация картинок только через /v1/images/generations (chat/completions → 404)."""
    raw = (os.environ.get("CT_MWS_IMAGE_CHAT_MODELS") or "qwen-image,qwen-image-lightning").strip()
    return {x.strip() for x in raw.split(",") if x.strip()}


def _is_mws_image_chat_model(model: str) -> bool:
    return model.strip() in _mws_image_chat_model_ids()


def _last_user_prompt_for_image(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            return message_text_content(m).strip()
    return ""


def _chat_completion_from_mws_images(model: str, img: Any) -> dict[str, Any]:
    """Собирает chat.completion с markdown-картинкой для Open WebUI."""
    if not isinstance(img, dict):
        raise ValueError("Ответ images/generations не JSON-объект")
    data = img.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError("В ответе images нет data[]")
    first = data[0] if isinstance(data[0], dict) else {}
    url = first.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("В ответе images нет url")
    alt = first.get("revised_prompt")
    if not isinstance(alt, str) or not alt.strip():
        alt = "image"
    content = f"![{alt}]({url})\n"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


async def _chat_completions_mws_image_model(
    svc: LLMService,
    model: str,
    messages: list[dict[str, Any]],
    *,
    stream: bool,
    body: dict[str, Any],
) -> Any:
    """Обход 404: qwen-image в MWS не поддерживает chat/completions, только images/generations."""
    msgs = normalize_chat_messages(copy.deepcopy(messages))
    prompt = _last_user_prompt_for_image(msgs)
    if not prompt:
        raise HTTPException(
            status_code=400,
            detail="Нужен непустой текст пользователя (промпт) для генерации изображения.",
        )
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1}
    size = body.get("size")
    if isinstance(size, str) and size.strip():
        payload["size"] = size.strip()
    else:
        payload["size"] = "1024x1024"
    for key in ("quality", "response_format", "style"):
        v = body.get(key)
        if isinstance(v, str) and v.strip():
            payload[key] = v.strip()
    try:
        img = await asyncio.to_thread(svc.images_generations, payload)
    except MWSGPTError as e:
        raise HTTPException(
            status_code=http_status_for_mws_error(e),
            detail={"message": str(e), "status": e.status, "body": e.body},
        ) from e
    try:
        completion = _chat_completion_from_mws_images(model, img)
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    completion = _completion_with_visible_markdown(completion)
    _proxy_log.debug(
        "mws image chat completion model=%s stream=%s preview=\n%s",
        model,
        stream,
        debug_clip(_final_assistant_content(completion)),
    )
    if not stream:
        return completion
    return StreamingResponse(_sse_stream(model, completion), media_type="text/event-stream")


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


async def _images_generations_from_body(body: dict[str, Any]) -> Any:
    svc = _service()
    try:
        return await asyncio.to_thread(svc.images_generations, body)
    except MWSGPTError as e:
        raise HTTPException(
            status_code=http_status_for_mws_error(e),
            detail={"message": str(e), "status": e.status, "body": e.body},
        ) from e


@router.post("/v1/images/generations")
async def images_generations(request: Request) -> Any:
    """Прокси на MWS POST /v1/images/generations (модели qwen-image и др.)."""
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Ожидается JSON: {e}") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидается JSON-объект")
    return await _images_generations_from_body(body)


@router.post("/v1/plain/images/generations")
async def images_generations_plain_prefix(request: Request) -> Any:
    """То же при base URL …/v1/plain."""
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Ожидается JSON: {e}") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидается JSON-объект")
    return await _images_generations_from_body(body)


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


def _iter_text_chunks(text: str, *, size: int = _SSE_CONTENT_CHUNK_CHARS):
    for i in range(0, len(text), size):
        yield text[i : i + size]


def _sse_line(
    model: str,
    cid: str,
    created: int,
    delta: dict[str, Any],
    finish_reason: str | None,
    *,
    extra: dict[str, Any] | None = None,
) -> str:
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
    if extra:
        payload.update(extra)
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _agent_sse_stream(
    svc: LLMService,
    model: str,
    messages: list[dict[str, Any]],
    *,
    max_tool_rounds: int,
    extra: dict[str, Any],
):
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    cumulative_output: list[dict[str, Any]] = []
    yield _sse_line(model, cid, created, {"role": "assistant"}, None)
    try:
        for event in svc.stream_agent(model, messages, max_tool_rounds=max_tool_rounds, **extra):
            etype = event.get("type")
            if etype == "done":
                result = event.get("result") if isinstance(event.get("result"), dict) else {}
                final_output = result.get("output") if isinstance(result.get("output"), list) else cumulative_output
                yield _sse_line(
                    model,
                    cid,
                    created,
                    {},
                    "stop",
                    extra={"done": True, "output": final_output},
                )
                yield "data: [DONE]\n\n"
                return
            if etype == "reasoning_stream":
                text = str(event.get("text") or "")
                if not text.strip():
                    continue
                yield _sse_line(
                    model,
                    cid,
                    created,
                    {"reasoning": text, "ct_phase": "reasoning"},
                    None,
                    extra={"output": cumulative_output, "ct_phase": "reasoning_stream"},
                )
                continue
            if etype == "content_stream":
                text = str(event.get("text") or "")
                if not text.strip():
                    continue
                yield _sse_line(
                    model,
                    cid,
                    created,
                    {"content": text, "ct_phase": "final"},
                    None,
                    extra={"output": cumulative_output, "ct_phase": "content_stream"},
                )
                continue
            if etype not in ("reasoning", "status", "final"):
                continue
            text = str(event.get("text") or "")
            if not text.strip():
                continue
            cumulative_output.append(copy.deepcopy(event))
            rendered = text if etype != "status" else text.strip()
            for piece in _iter_text_chunks(rendered):
                delta: dict[str, Any] = {"ct_phase": etype}
                if etype == "reasoning":
                    delta["reasoning"] = piece
                elif etype == "status":
                    delta["tool_status"] = piece
                elif etype == "final":
                    delta["content"] = piece
                    delta["final"] = piece
                yield _sse_line(
                    model,
                    cid,
                    created,
                    delta,
                    None,
                    extra={"output": cumulative_output, "ct_phase": etype},
                )
    except (MWSGPTError, ValueError) as e:
        err = f"\n\n[Ошибка агента: {e}]"
        for piece in _iter_text_chunks(err):
            yield _sse_line(
                model,
                cid,
                created,
                {"content": piece, "ct_phase": "error", "error": str(e)},
                None,
                extra={"output": cumulative_output, "ct_phase": "error"},
            )
        yield _sse_line(model, cid, created, {}, "stop")
        yield "data: [DONE]\n\n"
        return


async def _upstream_sse_stream(svc, model, messages, runtime, session_id, scope_id, prepared_messages, extra, *, skip_after_response: bool = False):
    """True upstream SSE for plain mode: pipe chunks, collect content for after_response."""
    import queue as _queue

    q: _queue.Queue[bytes | Exception | None] = _queue.Queue()
    accumulated: list[str] = []

    def _producer():
        try:
            call_kwargs = {k: v for k, v in extra.items() if k not in ("tools", "tool_choice", "request_context")}
            for raw_line in svc.chat_plain_stream(model, messages, **call_kwargs):
                q.put(raw_line)
        except Exception as exc:
            q.put(exc)
        finally:
            q.put(None)

    thread = threading.Thread(target=_producer, daemon=True)
    thread.start()

    while True:
        item = await asyncio.to_thread(q.get)
        if item is None:
            break
        if isinstance(item, Exception):
            raise item
        text = item.decode("utf-8", errors="replace").strip()
        if text.startswith("data: ") and text[6:] != "[DONE]":
            try:
                chunk = json.loads(text[6:])
                delta = (chunk.get("choices") or [{}])[0].get("delta", {})
                c = delta.get("content")
                if c:
                    accumulated.append(c)
            except json.JSONDecodeError:
                pass
        yield item if item.endswith(b"\n") else item + b"\n"

    full_content = "".join(accumulated)
    final_messages = [*prepared_messages, {"role": "assistant", "content": full_content}]
    if not skip_after_response:
        try:
            runtime.after_response(
                svc.client, model=model, prepared_messages=prepared_messages,
                final_messages=final_messages, session_id=session_id, scope_id=scope_id,
            )
        except Exception:
            pass


def _wants_plain_chat(body: dict[str, Any]) -> bool:
    """Режим «просто чат»: без агента и тулов (см. use_agent в теле или отдельный URL /v1/plain/...)."""
    v = body.get("use_agent", body.get("ct_use_agent", True))
    mode = body.get("agent_mode")
    if isinstance(mode, str) and mode.strip().lower() in ("plain", "chat", "off", "false", "0"):
        return True
    if isinstance(v, str):
        return v.strip().lower() in ("0", "false", "off", "no", "plain", "chat")
    return v is False


def _request_ids(body: dict[str, Any]) -> tuple[str, str]:
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    session_id = (
        body.get("ct_session_id")
        or body.get("conversation_id")
        or body.get("chat_id")
        or metadata.get("chat_id")
        or metadata.get("conversation_id")
        or "default-session"
    )
    scope_id = (
        body.get("ct_scope_id")
        or body.get("project_id")
        or metadata.get("project_id")
        or metadata.get("workspace_id")
        or "default-scope"
    )
    return str(session_id), str(scope_id)


def _request_contract_mode(body: dict[str, Any]) -> str | None:
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    value = body.get("ct_request_mode") or metadata.get("ct_request_mode") or body.get("ct_request_kind") or metadata.get("ct_request_kind")
    if not isinstance(value, str):
        return None
    mode = value.strip().lower()
    if mode in {"plain", "chat", "agent", "router", "meta_task"}:
        return mode
    return None


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
    max_agent_tokens = get_max_agent_tokens()
    extra = {k: v for k, v in body.items() if k not in _PASS_THROUGH_IGNORE}

    svc = _service()
    # MWS: qwen-image в /v1/models есть, но chat/completions для этих моделей → 404; генерация только images/generations.
    if _is_mws_image_chat_model(model):
        return await _chat_completions_mws_image_model(
            svc, model, messages, stream=stream, body=body
        )

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
        if prepared.forced_agent_id:
            extra["forced_agent_id"] = prepared.forced_agent_id
        if prepared.mode_applied:
            _proxy_log.debug(
                "ct_mode applied=%s forced_agent_id=%s max_tool_rounds=%s",
                prepared.mode_applied,
                prepared.forced_agent_id,
                max_tool_rounds,
            )
    if ow_meta_plain and not force_plain and not _wants_plain_chat(body):
        _proxy_log.debug("openwebui auxiliary ### Task -> plain chat (no agent JSON protocol)")
    if ow_tool_router_plain and contract_mode is None and not force_plain and not _wants_plain_chat(body):
        _proxy_log.debug("openwebui Available Tools router -> plain chat (no agent JSON protocol)")
    _proxy_log.debug(
        "chat_completions request model=%s plain=%s stream=%s max_agent_tokens=%s extra_keys=%s\nmessages_in=\n%s",
        model,
        plain,
        stream,
        max_agent_tokens,
        sorted(extra.keys()),
        summarize_messages(prepared_messages, preview=400) if isinstance(prepared_messages, list) else str(type(prepared_messages)),
    )
    if stream and not plain:
        return StreamingResponse(
            _agent_sse_stream(
                svc,
                model,
                messages,
                max_tool_rounds=max_tool_rounds,
                extra=extra,
            ),
            media_type="text/event-stream",
        )

    try:
        if plain:
            completion = await asyncio.to_thread(
                svc.chat_plain,
                model,
                prepared_messages,
                request_context=req_ctx,
                **extra,
            )
            final_messages = [*prepared_messages, (completion.get("choices") or [{}])[0].get("message") or {}]
        else:
            out = await asyncio.to_thread(
                svc.run_agent,
                model,
                prepared_messages,
                max_agent_tokens=max_agent_tokens,
                request_context=req_ctx,
                **extra,
            )
            completion = out.get("completion") or {}
            final_messages = out.get("messages") or prepared_messages
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
    if is_conversation:
        runtime.after_response(
            svc.client,
            model=model,
            prepared_messages=prepared_messages,
            final_messages=final_messages,
            session_id=session_id,
            scope_id=scope_id,
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
