from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from certified_turtles.memory_runtime.events import get_event_bus
from certified_turtles.memory_runtime.storage import (
    delete_memory_file,
    list_memory_files,
    memory_dir,
    parse_frontmatter,
    read_body,
    read_frontmatter,
    scan_memory_headers,
    write_memory_file,
)

log = logging.getLogger(__name__)
router = APIRouter(tags=["memory"])

DEFAULT_SCOPE = "default-scope"

GENERATING_PLACEHOLDER = "✨ …"


def _generate_name_sync(body: str, mem_type: str, model: str) -> str:
    """Call LLM to generate a categorical name for a memory."""
    from certified_turtles.mws_gpt.client import MWSGPTClient
    try:
        client = MWSGPTClient()
        resp = client.chat_completions(
            model,
            [
                {"role": "system", "content": (
                    "Generate a short categorical title (3-7 words) for a memory note. "
                    "The title should describe the CATEGORY, not the specific content. "
                    "Examples: 'Вкусовые предпочтения', 'Опыт работы и навыки', 'Дедлайны проекта'. "
                    "Reply with ONLY the title, nothing else. Use the same language as the input."
                )},
                {"role": "user", "content": f"Type: {mem_type}\nContent: {body[:500]}"},
            ],
        )
        name = resp["choices"][0]["message"]["content"].strip().strip('"\'')
        return name[:200] if name else "Untitled"
    except Exception as e:
        log.warning("Name generation failed: %s", e)
        return "Untitled"


async def _generate_name_and_update(scope_id: str, filename: str, body: str, description: str, mem_type: str):
    """Background task: generate name via LLM, then update the file."""
    from certified_turtles.memory_runtime.manager import get_last_model
    model = get_last_model(scope_id)
    name = await asyncio.get_event_loop().run_in_executor(None, _generate_name_sync, body, mem_type, model)
    try:
        write_memory_file(
            scope_id,
            name=name,
            description=description,
            type_=mem_type,
            body=body,
            filename=filename,
            source="ui",
        )
    except Exception as e:
        log.warning("Failed to update memory name: %s", e)


@router.get("/memory")
async def list_memories(scope_id: str = Query(DEFAULT_SCOPE)) -> dict[str, Any]:
    headers = scan_memory_headers(scope_id)
    root = memory_dir(scope_id)
    items = []
    for h in headers:
        body = read_body(root / h.filename)
        items.append({
            "filename": h.filename,
            "name": h.name,
            "description": body or h.description,
            "type": h.type,
            "mtime": h.mtime,
            "body": body,
        })
    return {"scope_id": scope_id, "memories": items}


@router.get("/memory/{filename:path}")
async def get_memory(filename: str, scope_id: str = Query(DEFAULT_SCOPE)) -> dict[str, Any]:
    root = memory_dir(scope_id)
    path = root / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Memory not found")
    fm = read_frontmatter(path)
    body = read_body(path)
    return {
        "filename": filename,
        "name": fm.get("name", path.stem),
        "description": fm.get("description", ""),
        "type": fm.get("type", "project"),
        "body": body,
    }


class MemoryWriteRequest(BaseModel):
    name: str = Field(..., max_length=200)
    description: str = Field(..., max_length=500)
    type: str = Field(..., pattern=r"^(user|project|reference)$")
    body: str = Field(..., max_length=4096)


@router.put("/memory/{filename:path}")
async def put_memory(
    filename: str,
    req: MemoryWriteRequest,
    scope_id: str = Query(DEFAULT_SCOPE),
) -> dict[str, Any]:
    # Save immediately with placeholder name
    try:
        path = write_memory_file(
            scope_id,
            name=GENERATING_PLACEHOLDER,
            description=req.description,
            type_=req.type,
            body=req.body,
            filename=filename,
            source="ui",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # Generate proper name in background
    asyncio.create_task(_generate_name_and_update(scope_id, filename, req.body, req.description, req.type))
    return {"ok": True, "filename": filename, "path": str(path)}


@router.delete("/memory/{filename:path}")
async def remove_memory(filename: str, scope_id: str = Query(DEFAULT_SCOPE)) -> dict[str, Any]:
    deleted = delete_memory_file(scope_id, filename)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"ok": True, "filename": filename}


@router.get("/memory-events")
async def memory_events_sse(scope_id: str = Query(DEFAULT_SCOPE)):
    bus = get_event_bus()
    q = bus.subscribe()

    async def stream():
        yield "data: {\"type\":\"connected\"}\n\n"
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                    if scope_id and event.scope_id != scope_id:
                        continue
                    yield event.to_sse()
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/memory-recent")
async def memory_recent(scope_id: str = Query(DEFAULT_SCOPE), limit: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
    bus = get_event_bus()
    events = bus.recent(scope_id=scope_id, limit=limit)
    return {"events": [e.to_dict() for e in events]}


@router.post("/memory-dream")
async def trigger_dream(scope_id: str = Query(DEFAULT_SCOPE)) -> dict[str, Any]:
    """Manually trigger memory consolidation (dream) for a scope."""
    import os
    from certified_turtles.memory_runtime.manager import runtime_from_env
    from certified_turtles.memory_runtime.storage import memory_dir as _mem_dir, memory_index_path
    from certified_turtles.mws_gpt.client import DEFAULT_BASE_URL, MWSGPTClient

    from certified_turtles.memory_runtime.forking import CacheSafeSnapshot
    from certified_turtles.memory_runtime.manager import get_last_model
    import time as _time

    rt = runtime_from_env()
    client = MWSGPTClient(base_url=os.environ.get("MWS_API_BASE", DEFAULT_BASE_URL))
    scope = scope_id
    mem_root = _mem_dir(scope)
    idx_path = memory_index_path(scope)

    # Ensure snapshot exists for the synthetic session so run_named_subagent works.
    dream_session = "manual-dream"
    model = get_last_model(scope)
    rt.forks.save_snapshot(CacheSafeSnapshot(
        model=model,
        scope_id=scope,
        session_id=dream_session,
        file_state_namespace=dream_session,
        messages=[],
        saved_at=_time.time(),
    ))

    rt._launch_post_hook(
        client,
        session_id="manual-dream",
        scope_id=scope,
        agent_id="auto_dream",
        prompt=(
            f"# Dream: Memory Consolidation\n\n"
            f"You are performing a reflective pass over the memory directory `{mem_root}`.\n"
            f"Rebuild `{idx_path}` after consolidating.\n\n"
            "Phase 1 — Orient: list the memory directory, read MEMORY.md, inspect topic files before creating duplicates.\n"
            "Phase 2 — Gather recent signal: use transcript evidence narrowly; do not read the entire world.\n"
            "Phase 3 — Consolidate: merge TRUE duplicates (same domain, same file topic), convert relative dates to absolute dates, fix contradicted facts at the source. "
            "NEVER merge files about different life domains (e.g. music and food are separate even if both are 'preferences').\n"
            "Phase 4 — Prune and index: keep MEMORY.md concise, one-line hooks only, remove stale pointers and contradictions.\n\n"
            "IMPORTANT: Do NOT touch the `instructions/` directory. Instructions are managed separately.\n\n"
            "Return a brief summary of what changed. If nothing changed, say so."
        ),
    )
    return {"ok": True, "message": "Dream consolidation started"}
