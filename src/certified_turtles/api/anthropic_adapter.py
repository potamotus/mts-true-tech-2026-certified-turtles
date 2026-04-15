"""
Anthropic Messages API → OpenAI Chat Completions adapter.

Lets Anthropic-native clients (Claude Code, Anthropic SDK) talk to
OpenAI-compatible backends (MWS GPT) through format translation.

Mount under prefix "/anthropic" so that:
  ANTHROPIC_BASE_URL=http://localhost:8000/anthropic
  → POST /anthropic/v1/messages  →  translated  →  POST MWS /v1/chat/completions
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

import requests as req

router = APIRouter()


# ── Anthropic → OpenAI request translation ──

def _anthropic_msgs_to_openai(
    system: str | list | None,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert Anthropic message array to OpenAI message array."""
    out: list[dict[str, Any]] = []

    # Identity override — prepended to any system prompt
    _IDENTITY = (
        "You are GPTHub Code — an AI coding assistant by the Certified Turtles team. "
        "Never say you are Claude, Anthropic, or any other AI. You are GPTHub Code."
    )

    # System prompt
    if system:
        if isinstance(system, list):
            text = " ".join(
                b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            text = system
        if text:
            out.append({"role": "system", "content": _IDENTITY + "\n\n" + text})
    else:
        out.append({"role": "system", "content": _IDENTITY})

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")

        if role == "user":
            # Content can be string or list of content blocks
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
            elif isinstance(content, list):
                # Check for tool_result blocks
                tool_results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
                text_parts = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
                image_parts = [b for b in content if isinstance(b, dict) and b.get("type") == "image"]

                # Tool results → OpenAI tool messages
                for tr in tool_results:
                    tc_content = tr.get("content", "")
                    if isinstance(tc_content, list):
                        tc_content = " ".join(
                            b.get("text", "") for b in tc_content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    out.append({
                        "role": "tool",
                        "tool_call_id": tr.get("tool_use_id", ""),
                        "content": str(tc_content),
                    })

                # Text parts → user message
                if text_parts:
                    text = " ".join(b.get("text", "") for b in text_parts)
                    out.append({"role": "user", "content": text})

                # Image parts → OpenAI vision format
                if image_parts and not text_parts and not tool_results:
                    oai_content: list[dict] = []
                    for b in content:
                        if b.get("type") == "text":
                            oai_content.append({"type": "text", "text": b.get("text", "")})
                        elif b.get("type") == "image":
                            src = b.get("source", {})
                            oai_content.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{src.get('media_type','image/png')};base64,{src.get('data','')}"},
                            })
                    out.append({"role": "user", "content": oai_content})
            else:
                out.append({"role": "user", "content": str(content) if content else ""})

        elif role == "assistant":
            if isinstance(content, str):
                out.append({"role": "assistant", "content": content})
            elif isinstance(content, list):
                text = ""
                tool_calls = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text += block.get("text", "")
                        elif block.get("type") == "tool_use":
                            tool_calls.append({
                                "id": block.get("id", str(uuid.uuid4())),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": json.dumps(block.get("input", {})),
                                },
                            })
                m: dict[str, Any] = {"role": "assistant"}
                if text:
                    m["content"] = text
                if tool_calls:
                    m["tool_calls"] = tool_calls
                out.append(m)
            else:
                out.append({"role": "assistant", "content": str(content) if content else ""})

    return out


def _anthropic_tools_to_openai(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Convert Anthropic tool definitions to OpenAI format."""
    if not tools:
        return None
    out = []
    for t in tools:
        out.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        })
    return out


# ── OpenAI → Anthropic response translation ──

def _openai_response_to_anthropic(oai: dict[str, Any], model: str) -> dict[str, Any]:
    """Convert OpenAI chat completion response to Anthropic messages response."""
    choice = oai.get("choices", [{}])[0]
    msg = choice.get("message", {})

    content_blocks: list[dict[str, Any]] = []

    has_tool_calls = bool(msg.get("tool_calls"))

    # Text — skip if it looks like duplicated tool arguments
    text = msg.get("content")
    if text:
        # MWS sometimes echoes tool args as text; drop if we also have tool_calls
        is_json_echo = has_tool_calls and text.strip().startswith("{")
        if not is_json_echo:
            content_blocks.append({"type": "text", "text": text})

    # Tool calls
    for tc in msg.get("tool_calls", []) or []:
        fn = tc.get("function", {})
        try:
            inp = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            inp = {}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", str(uuid.uuid4())),
            "name": fn.get("name", ""),
            "input": inp,
        })

    stop_reason_map = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
    }
    finish = choice.get("finish_reason", "stop")

    usage = oai.get("usage", {})

    return {
        "id": oai.get("id", f"msg_{uuid.uuid4().hex[:24]}"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks if content_blocks else [{"type": "text", "text": ""}],
        "stop_reason": stop_reason_map.get(finish, "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ── Streaming: OpenAI SSE → Anthropic SSE ──

def _make_anthropic_stream(oai_response, model: str, msg_id: str):
    """Generator that converts OpenAI SSE stream to Anthropic SSE events."""

    # message_start
    yield _sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })

    block_idx = 0
    text_block_started = False
    tool_blocks: dict[int, dict] = {}  # oai tool index → our block info

    for line in oai_response.iter_lines(decode_unicode=True):
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue

        delta = chunk.get("choices", [{}])[0].get("delta", {})
        finish = chunk.get("choices", [{}])[0].get("finish_reason")

        # Text content
        token = delta.get("content")
        if token:
            if not text_block_started:
                yield _sse("content_block_start", {
                    "type": "content_block_start",
                    "index": block_idx,
                    "content_block": {"type": "text", "text": ""},
                })
                text_block_started = True

            yield _sse("content_block_delta", {
                "type": "content_block_delta",
                "index": block_idx,
                "delta": {"type": "text_delta", "text": token},
            })

        # Tool calls
        for tc in delta.get("tool_calls", []):
            oai_idx = tc.get("index", 0)

            if oai_idx not in tool_blocks:
                # Close text block if open
                if text_block_started:
                    yield _sse("content_block_stop", {
                        "type": "content_block_stop", "index": block_idx,
                    })
                    block_idx += 1
                    text_block_started = False

                tool_id = tc.get("id", str(uuid.uuid4()))
                fn_name = tc.get("function", {}).get("name", "")
                tool_blocks[oai_idx] = {
                    "block_idx": block_idx,
                    "id": tool_id,
                    "name": fn_name,
                    "arguments": "",
                }
                yield _sse("content_block_start", {
                    "type": "content_block_start",
                    "index": block_idx,
                    "content_block": {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": fn_name,
                        "input": {},
                    },
                })
                block_idx += 1

            # Accumulate arguments
            fn_args = tc.get("function", {}).get("arguments", "")
            if fn_args:
                tool_blocks[oai_idx]["arguments"] += fn_args
                yield _sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": tool_blocks[oai_idx]["block_idx"],
                    "delta": {"type": "input_json_delta", "partial_json": fn_args},
                })

        # Finish
        if finish:
            # Close any open blocks
            if text_block_started:
                yield _sse("content_block_stop", {
                    "type": "content_block_stop", "index": block_idx,
                })
            for tb in tool_blocks.values():
                yield _sse("content_block_stop", {
                    "type": "content_block_stop", "index": tb["block_idx"],
                })

            stop_map = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}
            yield _sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": stop_map.get(finish, "end_turn"), "stop_sequence": None},
                "usage": {"output_tokens": 0},
            })

    yield _sse("message_stop", {"type": "message_stop"})


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ── Model mapping ──

_MODEL_MAP = {
    # Map common Anthropic model names to MWS equivalents
    "claude-opus-4-6": "gpt-oss-120b",
    "claude-sonnet-4-6": "gpt-oss-120b",
    "claude-haiku-4-5": "gpt-oss-20b",
    "claude-3-5-sonnet-20241022": "gpt-oss-120b",
    "claude-3-5-haiku-20241022": "gpt-oss-20b",
}


def _map_model(model: str) -> str:
    """Map Anthropic model name to MWS model."""
    for prefix, mws in _MODEL_MAP.items():
        if model.startswith(prefix):
            return mws
    return os.environ.get("CT_ANTHROPIC_DEFAULT_MODEL", "gpt-oss-120b")


# ── Endpoint ──

@router.post("/v1/messages")
async def anthropic_messages(request: Request):
    """Anthropic Messages API compatible endpoint."""
    body = await request.json()

    model_requested = body.get("model", "")
    model = _map_model(model_requested)
    stream = body.get("stream", False)

    # Convert request
    openai_messages = _anthropic_msgs_to_openai(
        system=body.get("system"),
        messages=body.get("messages", []),
    )
    openai_tools = _anthropic_tools_to_openai(body.get("tools"))

    openai_body: dict[str, Any] = {
        "model": model,
        "messages": openai_messages,
        "stream": stream,
        "temperature": body.get("temperature", 0.2),
    }
    if body.get("max_tokens"):
        openai_body["max_tokens"] = body["max_tokens"]
    if openai_tools:
        openai_body["tools"] = openai_tools

    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    api_base = os.environ.get("MWS_API_BASE", "https://api.gpt.mws.ru")
    api_key = os.environ.get("MWS_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    if not stream:
        r = req.post(f"{api_base}/v1/chat/completions", json=openai_body, headers=headers, timeout=300)
        r.raise_for_status()
        return _openai_response_to_anthropic(r.json(), model)

    r = req.post(
        f"{api_base}/v1/chat/completions",
        json=openai_body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        stream=True,
        timeout=300,
    )
    r.raise_for_status()

    return StreamingResponse(
        _make_anthropic_stream(r, model, msg_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Mapped-Model": model,
        },
    )
