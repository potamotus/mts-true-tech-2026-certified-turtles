from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from certified_turtles.mws_gpt.client import MWSGPTError
from certified_turtles.services.llm import LLMService

router = APIRouter(tags=["openai-proxy"])

_PASS_THROUGH_IGNORE = {"model", "messages", "stream", "max_tool_rounds", "tools", "tool_choice"}


def _service() -> LLMService:
    try:
        return LLMService.from_env()
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/v1/models")
def list_models() -> Any:
    svc = _service()
    try:
        return svc.list_models()
    except MWSGPTError as e:
        raise HTTPException(
            status_code=502,
            detail={"message": str(e), "status": e.status, "body": e.body},
        ) from e


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


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Ожидается JSON: {e}") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидается JSON-объект")

    model = body.get("model")
    messages = body.get("messages")
    if not isinstance(model, str) or not model:
        raise HTTPException(status_code=400, detail="Поле `model` обязательно")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="Поле `messages` обязательно и не должно быть пустым")

    stream = bool(body.get("stream"))
    max_tool_rounds = int(body.get("max_tool_rounds", 10))
    extra = {k: v for k, v in body.items() if k not in _PASS_THROUGH_IGNORE}

    svc = _service()
    try:
        out = svc.run_agent(model, messages, max_tool_rounds=max_tool_rounds, **extra)
    except MWSGPTError as e:
        raise HTTPException(
            status_code=502,
            detail={"message": str(e), "status": e.status, "body": e.body},
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    completion = out.get("completion") or {}
    if not stream:
        return completion
    return StreamingResponse(_sse_stream(model, completion), media_type="text/event-stream")
