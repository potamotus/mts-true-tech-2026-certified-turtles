from __future__ import annotations

import asyncio

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from certified_turtles.chat_modes import prepare_chat_request
from certified_turtles.mws_gpt.client import MWSGPTError, http_status_for_mws_error
from certified_turtles.api.agent_config import get_max_agent_tokens
from certified_turtles.services.llm import LLMService

router = APIRouter(tags=["agent"])


class AgentChatRequest(BaseModel):
    model: str = Field(..., description="Идентификатор модели из allowlist ключа (например mws-gpt-alpha).")
    messages: list[dict] = Field(..., description="История в формате OpenAI chat messages.")
    max_agent_tokens: int | None = Field(default=None, description="Token budget for agent loop. Uses server default if not set.")
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    ct_mode: str | None = None

    @field_validator("messages")
    @classmethod
    def messages_not_empty(cls, v: list) -> list:
        if not v:
            raise ValueError("messages не должны быть пустыми")
        return v


@router.post("/agent/chat")
async def agent_chat(body: AgentChatRequest):
    try:
        service = LLMService.from_env()
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    extra: dict = {}
    if body.temperature is not None:
        extra["temperature"] = body.temperature
    if body.max_tokens is not None:
        extra["max_tokens"] = body.max_tokens

    prepared = prepare_chat_request(
        {"ct_mode": body.ct_mode} if body.ct_mode else {},
        body.messages,
        for_agent=True,
    )
    messages = prepared.messages
    max_tool_rounds = body.max_tool_rounds
    if prepared.max_tool_rounds_override is not None:
        max_tool_rounds = max(max_tool_rounds, prepared.max_tool_rounds_override)
    if prepared.forced_agent_id:
        extra["forced_agent_id"] = prepared.forced_agent_id

    if body.stream:
        def _event_stream():
            try:
                for event in service.stream_agent(
                    body.model,
                    messages,
                    max_tool_rounds=max_tool_rounds,
                    **extra,
                ):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            except MWSGPTError as e:
                payload = {"type": "error", "detail": {"message": str(e), "status": e.status, "body": e.body}}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            except ValueError as e:
                payload = {"type": "error", "detail": str(e)}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(_event_stream(), media_type="text/event-stream")

    try:
        return await asyncio.to_thread(
            service.run_agent,
            body.model,
            messages,
            max_tool_rounds=max_tool_rounds,
            **extra,
        )
    except MWSGPTError as e:
        raise HTTPException(
            status_code=http_status_for_mws_error(e),
            detail={"message": str(e), "status": e.status, "body": e.body},
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
