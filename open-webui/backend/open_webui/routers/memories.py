"""Memories router — bridges to Certified Turtles memory runtime via internal HTTP."""

import os
import time
import uuid
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from open_webui.utils.auth import get_verified_user

log = logging.getLogger(__name__)
router = APIRouter()

CT_API = os.environ.get("CT_API_BASE", "http://api:8000")
CT_SCOPE = os.environ.get("CT_MEMORY_SCOPE", "default-scope")


def _ct_url(path: str) -> str:
    return f"{CT_API}/api/v1{path}?scope_id={CT_SCOPE}"


class MemoryModel(BaseModel):
    id: str
    user_id: str
    content: str
    updated_at: int
    created_at: int
    memory_type: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None


def _to_model(item: dict, user_id: str = "default") -> MemoryModel:
    mtime = int(item.get("mtime", time.time()))
    body = item.get("body", "")
    content = body or item.get("description", item.get("name", ""))
    return MemoryModel(
        id=item.get("filename", str(uuid.uuid4())),
        user_id=user_id,
        content=content,
        updated_at=mtime,
        created_at=mtime,
        memory_type=item.get("type", "project"),
        name=item.get("name", ""),
        description=content,
    )


@router.get("/", response_model=list[MemoryModel])
async def get_memories(request: Request, user=Depends(get_verified_user)):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(_ct_url("/memory"))
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("CT memory fetch failed: %s", e)
        return []
    return [_to_model(m, user.id) for m in data.get("memories", [])]


class AddMemoryForm(BaseModel):
    content: str


@router.post("/add", response_model=Optional[MemoryModel])
async def add_memory(request: Request, form_data: AddMemoryForm, user=Depends(get_verified_user)):
    filename = f"manual-{uuid.uuid4().hex[:8]}.md"
    payload = {"name": form_data.content[:80], "description": form_data.content[:200], "type": "user", "body": form_data.content}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.put(_ct_url(f"/memory/{filename}"), json=payload)
            r.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return MemoryModel(id=filename, user_id=user.id, content=form_data.content, updated_at=int(time.time()), created_at=int(time.time()), memory_type="user", name=form_data.content[:80], description=form_data.content[:200])


class MemoryUpdateModel(BaseModel):
    content: Optional[str] = None


@router.post("/{memory_id}/update", response_model=Optional[MemoryModel])
async def update_memory_by_id(memory_id: str, request: Request, form_data: MemoryUpdateModel, user=Depends(get_verified_user)):
    if not form_data.content:
        raise HTTPException(status_code=400, detail="content required")
    existing = {}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(_ct_url(f"/memory/{memory_id}"))
            if r.status_code == 200:
                existing = r.json()
    except Exception:
        pass
    payload = {"name": existing.get("name", form_data.content[:80]), "description": form_data.content[:200], "type": existing.get("type", "user"), "body": form_data.content}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.put(_ct_url(f"/memory/{memory_id}"), json=payload)
            r.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return MemoryModel(id=memory_id, user_id=user.id, content=form_data.content, updated_at=int(time.time()), created_at=int(time.time()), memory_type=payload["type"], name=payload["name"], description=payload["description"])


class QueryMemoryForm(BaseModel):
    content: str
    k: Optional[int] = 1


@router.post("/query")
async def query_memory(request: Request, form_data: QueryMemoryForm, user=Depends(get_verified_user)):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(_ct_url("/memory"))
            r.raise_for_status()
            data = r.json()
    except Exception:
        return []
    q = form_data.content.lower()
    hits = []
    for m in data.get("memories", []):
        text = f"{m.get('name','')} {m.get('description','')}".lower()
        if q in text:
            hits.append({"id": m["filename"], "score": 1.0, "document": m.get("description", "")})
    return hits[:form_data.k]


@router.post("/reset", response_model=bool)
async def reset_memory(request: Request, user=Depends(get_verified_user)):
    return True


@router.delete("/delete/user", response_model=bool)
async def delete_all_memories(request: Request, user=Depends(get_verified_user)):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(_ct_url("/memory"))
            r.raise_for_status()
            for m in r.json().get("memories", []):
                await c.delete(_ct_url(f"/memory/{m['filename']}"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return True


@router.delete("/{memory_id}", response_model=bool)
async def delete_memory_by_id(memory_id: str, request: Request, user=Depends(get_verified_user)):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.delete(_ct_url(f"/memory/{memory_id}"))
            r.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return True
