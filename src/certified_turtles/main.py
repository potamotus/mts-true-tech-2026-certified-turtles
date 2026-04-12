from __future__ import annotations

import json
import time
from typing import Any

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from certified_turtles.agent_debug_log import configure_agent_debug_from_env
from certified_turtles.backend_log import get_backend_logger

configure_agent_debug_from_env()
from certified_turtles.tools.builtins.google_docs import google_docs_capability_dict

from certified_turtles.api.agent import router as agent_router
from certified_turtles.api.files import router as files_router
from certified_turtles.api.memory import router as memory_router
from certified_turtles.api.openai_proxy import router as openai_proxy_router
from certified_turtles.api.uploads import router as uploads_router

_backend = get_backend_logger()

app = FastAPI(
    title="Certified Turtles / GPTHub API",
    version="0.3.0",
    description=(
        "Единый фасад над MWS GPT: OpenAI-совместимый прокси для Open WebUI "
        "(`/v1/*` — агент с тулами; `/v1/plain/*` — обычный чат), "
        "`/api/v1/agent/chat`, загрузки `POST /api/v1/uploads`, раздача `/files/*`."
    ),
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _should_log_backend_request(method: str, path: str) -> bool:
    if path in ("/v1/chat/completions", "/v1/plain/chat/completions"):
        return method == "POST"
    if path in ("/v1/models", "/v1/plain/models"):
        return method == "GET"
    if path == "/api/v1/agent/chat":
        return method == "POST"
    if path == "/api/v1/uploads":
        return method == "POST"
    return False


def _collapse_preview(content: Any, max_len: int = 200) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        s = " ".join(content.split())
    elif isinstance(content, list):
        texts: list[str] = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text") if isinstance(p.get("text"), str) else p.get("input_text")
                if isinstance(t, str):
                    texts.append(t)
        s = " ".join(" ".join(x.split()) for x in texts)
    else:
        s = str(content)
    s = s.strip()
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s


def _last_user_preview(msgs: list[Any]) -> str:
    for item in reversed(msgs):
        if isinstance(item, dict) and item.get("role") == "user":
            return _collapse_preview(item.get("content"))
    return ""


def _summarize_json_body(path: str, data: dict[str, Any]) -> str:
    """Только безопасные поля: без полного JSON, ключей и секретов."""
    parts: list[str] = []
    if isinstance(data.get("model"), str) and data["model"]:
        parts.append(f"model={data['model']}")
    msgs = data.get("messages")
    if isinstance(msgs, list):
        parts.append(f"messages={len(msgs)}")
        prev = _last_user_preview(msgs)
        if prev:
            parts.append(f"last_user={json.dumps(prev, ensure_ascii=False)}")
    if path in ("/v1/chat/completions", "/v1/plain/chat/completions"):
        if "stream" in data:
            parts.append(f"stream={bool(data.get('stream'))}")
        ua = data.get("use_agent", data.get("ct_use_agent"))
        if ua is not None:
            parts.append(f"use_agent={ua}")
    if path == "/api/v1/agent/chat":
        mtr = data.get("max_tool_rounds")
        if mtr is not None:
            parts.append(f"max_tool_rounds={mtr}")
    return ", ".join(parts) if parts else ""


async def _request_payload_note(request: Request, path: str) -> str:
    if request.method == "GET":
        return ""
    if path == "/api/v1/uploads":
        cl = request.headers.get("content-length", "?")
        return f"multipart cl={cl}"
    ct = request.headers.get("content-type", "")
    if "application/json" not in ct:
        return f"content-type={ct.split(';')[0].strip() or 'none'}"
    try:
        raw = await request.body()
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "body=<invalid json>"
    if not isinstance(data, dict):
        return "body=<not object>"
    return _summarize_json_body(path, data)


@app.middleware("http")
async def _log_backend_requests(request: Request, call_next):
    """Только бизнес-запросы: кратко что ушло в запрос, статус и время."""
    path = request.url.path
    track = _should_log_backend_request(request.method, path)
    if not track:
        return await call_next(request)
    note = await _request_payload_note(request, path)
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - start
    mid = f" | {note}" if note else ""
    msg = f"{request.method} {path}{mid} -> {response.status_code} ({elapsed:.2f}s)"
    if response.status_code >= 400:
        _backend.warning(msg)
    else:
        _backend.info(msg)
    return response


app.include_router(openai_proxy_router)
app.include_router(files_router)
app.include_router(agent_router, prefix="/api/v1")
app.include_router(uploads_router, prefix="/api/v1")
app.include_router(memory_router, prefix="/api/v1")


_STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/memory")
async def memory_page():
    return FileResponse(_STATIC_DIR / "memory.html", media_type="text/html")


@app.get("/static/{filename}")
async def serve_static(filename: str):
    path = _STATIC_DIR / filename
    if not path.is_file():
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
    suffix_map = {".js": "application/javascript", ".css": "text/css", ".html": "text/html"}
    media = suffix_map.get(path.suffix, "application/octet-stream")
    return FileResponse(path, media_type=media)


@app.get("/health")
def health() -> dict[str, Any]:
    """Статус API и возможности (в т.ч. Google Docs), чтобы админ/пользователь видели, что включено."""
    return {
        "status": "ok",
        "capabilities": {
            "google_docs": google_docs_capability_dict(),
        },
    }
