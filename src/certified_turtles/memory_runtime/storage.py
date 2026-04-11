from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import time
import uuid
from typing import Any


MAX_MEMORY_FILES = 200
MAX_MEMORY_INDEX_LINES = 200
MAX_MEMORY_INDEX_BYTES = 25 * 1024
MAX_MEMORY_FILE_BYTES = 4 * 1024
MAX_MEMORY_SESSION_BYTES = 60 * 1024
MAX_RELEVANT_MEMORIES = 5
VALID_MEMORY_TYPES = {"user", "feedback", "project", "reference"}
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
SAFE_SEGMENT_RE = re.compile(r"[^a-zA-Z0-9._-]+")
FRONTMATTER_SCAN_LINES = 30


@dataclass(frozen=True)
class MemoryHeader:
    filename: str
    name: str
    description: str
    type: str
    mtime: float


def claude_like_root() -> Path:
    root = os.environ.get("CT_CLAUDE_HOME", "/tmp/certified_turtles_claude_like")
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def slugify(value: str) -> str:
    cleaned = SAFE_SEGMENT_RE.sub("-", value.strip()).strip("-").lower()
    return cleaned[:80] or "default"


def scope_slug(scope_id: str) -> str:
    return slugify(scope_id or "default-scope")


def session_slug(session_id: str) -> str:
    return slugify(session_id or "default-session")


def projects_root() -> Path:
    path = claude_like_root() / "projects"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sessions_root() -> Path:
    path = claude_like_root() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def memory_dir(scope_id: str) -> Path:
    path = projects_root() / scope_slug(scope_id) / "memory"
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_dir(session_id: str) -> Path:
    path = sessions_root() / session_slug(session_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_memory_path(session_id: str) -> Path:
    return session_dir(session_id) / "session.md"


def session_transcript_path(session_id: str) -> Path:
    return session_dir(session_id) / "session.jsonl"


def session_meta_path(session_id: str) -> Path:
    return session_dir(session_id) / "meta.json"


def scope_meta_path(scope_id: str) -> Path:
    return memory_dir(scope_id) / ".meta.json"


def ensure_session_meta(session_id: str, *, scope_id: str) -> None:
    path = session_meta_path(session_id)
    data = read_json(path) or {}
    data["scope_id"] = scope_id
    if "created_at" not in data:
        data["created_at"] = time.time()
    data["updated_at"] = time.time()
    write_json(path, data)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_frontmatter(text: str) -> dict[str, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    out: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip('"')
    return out


def read_frontmatter(path: Path) -> dict[str, str]:
    try:
        return parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}


def read_body(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = FRONTMATTER_RE.match(text)
    if not match:
        return text
    return text[match.end() :].strip()


def memory_index_path(scope_id: str) -> Path:
    return memory_dir(scope_id) / "MEMORY.md"


def list_memory_files(scope_id: str) -> list[Path]:
    root = memory_dir(scope_id)
    files = [p for p in root.rglob("*.md") if p.name != "MEMORY.md"]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:MAX_MEMORY_FILES]


def scan_memory_headers(scope_id: str) -> list[MemoryHeader]:
    headers: list[MemoryHeader] = []
    for path in list_memory_files(scope_id):
        try:
            preview = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[:FRONTMATTER_SCAN_LINES])
        except OSError:
            continue
        fm = parse_frontmatter(preview)
        try:
            stat = path.stat()
        except OSError:
            continue
        headers.append(
            MemoryHeader(
                filename=str(path.relative_to(memory_dir(scope_id))),
                name=fm.get("name", path.stem),
                description=fm.get("description", ""),
                type=fm.get("type", "project"),
                mtime=stat.st_mtime,
            )
        )
    return headers


def format_memory_manifest(headers: list[MemoryHeader]) -> str:
    lines: list[str] = []
    for item in headers:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(item.mtime))
        lines.append(f"- [{item.type}] {item.filename} ({stamp}): {item.description}")
    return "\n".join(lines)


def write_memory_file(
    scope_id: str,
    *,
    name: str,
    description: str,
    type_: str,
    body: str,
    filename: str | None = None,
    source: str = "manual",
) -> Path:
    kind = type_ if type_ in VALID_MEMORY_TYPES else "project"
    root = memory_dir(scope_id)
    slug = slugify(filename or name)
    nested = Path(filename) if filename else Path(f"{slug}.md")
    path = root / nested
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    created = now
    if path.exists():
        existing = read_frontmatter(path)
        created = existing.get("created", now)
    payload = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"type: {kind}\n"
        f"created: {created}\n"
        f"updated: {now}\n"
        f"source: {source}\n"
        "---\n\n"
        f"{body.strip()}\n"
    )
    path.write_text(payload, encoding="utf-8")
    rebuild_memory_index(scope_id)
    return path


def delete_memory_file(scope_id: str, filename: str) -> bool:
    path = memory_dir(scope_id) / Path(filename)
    if not path.is_file():
        return False
    path.unlink()
    rebuild_memory_index(scope_id)
    return True


def rebuild_memory_index(scope_id: str) -> Path:
    headers = scan_memory_headers(scope_id)
    lines = ["# Memory Index"]
    for item in headers[: max(0, MAX_MEMORY_INDEX_LINES - 1)]:
        lines.append(f"- [{item.filename}]({item.filename}) — {item.description}")
    text = "\n".join(lines).strip() + "\n"
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_MEMORY_INDEX_BYTES:
        truncated = encoded[:MAX_MEMORY_INDEX_BYTES].decode("utf-8", errors="ignore")
        text = truncated.rstrip() + "\n"
    path = memory_index_path(scope_id)
    path.write_text(text, encoding="utf-8")
    return path


def append_transcript_event(session_id: str, payload: dict[str, Any]) -> None:
    path = session_transcript_path(session_id)
    record = {"uuid": uuid.uuid4().hex, **payload}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_transcript_events(session_id: str, *, limit: int = 80) -> list[dict[str, Any]]:
    path = session_transcript_path(session_id)
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    rows = lines[-limit:]
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            data = json.loads(row)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def write_session_memory(session_id: str, content: str) -> Path:
    path = session_memory_path(session_id)
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path


def read_session_memory(session_id: str) -> str:
    path = session_memory_path(session_id)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def list_scope_sessions(scope_id: str) -> list[Path]:
    out: list[Path] = []
    for path in sessions_root().iterdir():
        if not path.is_dir():
            continue
        meta = read_json(path / "meta.json") or {}
        if meta.get("scope_id") == scope_id:
            out.append(path)
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out
