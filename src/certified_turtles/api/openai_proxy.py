from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from certified_turtles.mws_gpt.client import MWSGPTError, http_status_for_mws_error
from certified_turtles.services.llm import LLMService, clamp_agent_tool_rounds

router = APIRouter(tags=["openai-proxy"])

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


def _final_assistant_content(completion: dict[str, Any]) -> str:
    choices = completion.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")
    return content if isinstance(content, str) else ""


def _sse_stream(model: str, completion: dict[str, Any]):
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    content = _final_assistant_content(completion)
    chunk = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }
    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
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
    plain = force_plain or _wants_plain_chat(body)
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
