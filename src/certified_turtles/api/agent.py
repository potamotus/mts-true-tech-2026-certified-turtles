from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from certified_turtles.mws_gpt.client import MWSGPTError, http_status_for_mws_error
from certified_turtles.services.llm import LLMService

router = APIRouter(tags=["agent"])


class AgentChatRequest(BaseModel):
    model: str = Field(..., description="Идентификатор модели из allowlist ключа (например mws-gpt-alpha).")
    messages: list[dict] = Field(..., description="История в формате OpenAI chat messages.")
    max_tool_rounds: int = Field(default=10, ge=1, le=40)
    temperature: float | None = None
    max_tokens: int | None = None

    @field_validator("messages")
    @classmethod
    def messages_not_empty(cls, v: list) -> list:
        if not v:
            raise ValueError("messages не должны быть пустыми")
        return v


@router.post("/agent/chat")
async def agent_chat(body: AgentChatRequest) -> dict:
    try:
        service = LLMService.from_env()
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    extra: dict = {}
    if body.temperature is not None:
        extra["temperature"] = body.temperature
    if body.max_tokens is not None:
        extra["max_tokens"] = body.max_tokens

    try:
        return await asyncio.to_thread(
            service.run_agent,
            body.model,
            body.messages,
            max_tool_rounds=body.max_tool_rounds,
            **extra,
        )
    except MWSGPTError as e:
        raise HTTPException(
            status_code=http_status_for_mws_error(e),
            detail={"message": str(e), "status": e.status, "body": e.body},
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
