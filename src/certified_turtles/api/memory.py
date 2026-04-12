from __future__ import annotations

import asyncio
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

router = APIRouter(tags=["memory"])

DEFAULT_SCOPE = "default-scope"


@router.get("/memory")
async def list_memories(scope_id: str = Query(DEFAULT_SCOPE)) -> dict[str, Any]:
    headers = scan_memory_headers(scope_id)
    items = []
    for h in headers:
        items.append({
            "filename": h.filename,
            "name": h.name,
            "description": h.description,
            "type": h.type,
            "mtime": h.mtime,
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
    type: str = Field(..., pattern=r"^(user|feedback|project|reference)$")
    body: str = Field(..., max_length=4096)


@router.put("/memory/{filename:path}")
async def put_memory(
    filename: str,
    req: MemoryWriteRequest,
    scope_id: str = Query(DEFAULT_SCOPE),
) -> dict[str, Any]:
    try:
        path = write_memory_file(
            scope_id,
            name=req.name,
            description=req.description,
            type_=req.type,
            body=req.body,
            filename=filename,
            source="ui",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
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
