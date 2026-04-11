from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from certified_turtles.agents.json_agent_protocol import (
    extract_user_visible_assistant_text,
    message_text_content,
    patch_completion_assistant_markdown,
)
from certified_turtles.agent_debug_log import agent_logger, debug_clip, summarize_messages
from certified_turtles.memory_runtime import RequestContext, runtime_from_env
from certified_turtles.mws_gpt.client import MWSGPTError, http_status_for_mws_error
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
        return await asyncio.to_thread(svc.list_models)
    except MWSGPTError as e:
        raise HTTPException(
            status_code=http_status_for_mws_error(e),
            detail={"message": str(e), "status": e.status, "body": e.body},
        ) from e


@router.get("/v1/plain/models")
async def list_models_plain_prefix() -> Any:
    """Тот же /v1/models, если в Open WebUI заведено отдельное подключение с base …/v1/plain."""
    return await list_models()


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


def _sse_stream(model: str, completion: dict[str, Any]):
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    content = _final_assistant_content(completion)
    role_chunk = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant"},
                "finish_reason": None,
            }
        ],
    }
    yield f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\n"
    step = 320
    for start in range(0, len(content), step):
        piece = content[start : start + step]
        chunk = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": piece},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    final_chunk = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }
        ],
    }
    yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


async def _upstream_sse_stream(svc, model, messages, runtime, session_id, scope_id, prepared_messages, extra):
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
    max_tool_rounds = clamp_agent_tool_rounds(body.get("max_tool_rounds", 10))
    extra = {k: v for k, v in body.items() if k not in _PASS_THROUGH_IGNORE}

    svc = _service()
    runtime = runtime_from_env()
    contract_mode = _request_contract_mode(body)
    ow_meta_plain = _openwebui_meta_task_forces_plain(messages)
    ow_tool_router_plain = _openwebui_tool_router_forces_plain(messages)
    if contract_mode == "agent":
        plain = bool(force_plain)
    else:
        plain = (
            force_plain
            or contract_mode in {"plain", "chat", "router", "meta_task"}
            or _wants_plain_chat(body)
            or ow_meta_plain
            or ow_tool_router_plain
        )
    session_id, scope_id = _request_ids(body)
    prepared_messages = runtime.prepare_messages(
        svc.client,
        model=model,
        messages=messages,
        session_id=session_id,
        scope_id=scope_id,
    )
    req_ctx = RequestContext(session_id=session_id, scope_id=scope_id, file_state_namespace=session_id)
    if contract_mode in {"router", "meta_task"}:
        _proxy_log.debug("explicit request contract mode=%s -> plain chat", contract_mode)
    if ow_meta_plain and contract_mode is None and not force_plain and not _wants_plain_chat(body):
        _proxy_log.debug("openwebui auxiliary ### Task -> plain chat (no agent JSON protocol)")
    if ow_tool_router_plain and contract_mode is None and not force_plain and not _wants_plain_chat(body):
        _proxy_log.debug("openwebui Available Tools router -> plain chat (no agent JSON protocol)")
    _proxy_log.debug(
        "chat_completions request model=%s plain=%s stream=%s max_tool_rounds=%s extra_keys=%s\nmessages_in=\n%s",
        model,
        plain,
        stream,
        max_tool_rounds,
        sorted(extra.keys()),
        summarize_messages(prepared_messages, preview=400) if isinstance(prepared_messages, list) else str(type(prepared_messages)),
    )
    # Plain + stream: true upstream SSE (pipe chunks directly from MWS).
    if plain and stream:
        return StreamingResponse(
            _upstream_sse_stream(svc, model, prepared_messages, runtime, session_id, scope_id, prepared_messages, extra),
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
                max_tool_rounds=max_tool_rounds,
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
    return await _chat_completions_from_body(body, force_plain=True)
