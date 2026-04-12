from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
import uuid
from typing import Any

_log = logging.getLogger(__name__)


def _emit_memory_event(*, action: str, scope_id: str, filename: str, memory_type: str, name: str) -> None:
    try:
        from .events import MemoryEvent, get_event_bus
        get_event_bus().publish(MemoryEvent(
            action=action,
            filename=filename,
            memory_type=memory_type,
            name=name,
            scope_id=scope_id,
        ))
    except Exception:
        pass


MAX_MEMORY_FILES = 200
MAX_MEMORY_INDEX_LINES = 200
MAX_MEMORY_INDEX_BYTES = 25_000
MAX_MEMORY_FILE_BYTES = 4 * 1024
MAX_MEMORY_SESSION_BYTES = 60 * 1024
MAX_RELEVANT_MEMORIES = 5
VALID_MEMORY_TYPES = {"user", "feedback", "project", "reference"}
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
SAFE_SEGMENT_RE = re.compile(r"[^a-zA-Z0-9._-]+")
FRONTMATTER_SCAN_LINES = 30
_LEGACY_KEY_MAX = 80


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


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
    return cleaned[:_LEGACY_KEY_MAX] or "default"


def stable_bucket_name(value: str, *, prefix: str) -> str:
    raw = value or f"default-{prefix}"
    slug = slugify(raw)
    digest = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{prefix}-{slug}-{digest}"


def _legacy_bucket_name(value: str, *, fallback: str) -> str:
    return slugify(value or fallback)


def scope_slug(scope_id: str) -> str:
    return stable_bucket_name(scope_id or "default-scope", prefix="scope")


def session_slug(session_id: str) -> str:
    return stable_bucket_name(session_id or "default-session", prefix="session")


def projects_root() -> Path:
    path = claude_like_root() / "projects"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sessions_root() -> Path:
    path = claude_like_root() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding=encoding) as tmp:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _ensure_bucket(root: Path, current_name: str, legacy_name: str) -> Path:
    current = root / current_name
    legacy = root / legacy_name
    if current.exists():
        current.mkdir(parents=True, exist_ok=True)
        return current
    if legacy.exists() and not current.exists():
        legacy.parent.mkdir(parents=True, exist_ok=True)
        try:
            legacy.replace(current)
        except OSError:
            # Best-effort compatibility for dirty local state: keep using the legacy bucket.
            legacy.mkdir(parents=True, exist_ok=True)
            return legacy
    current.mkdir(parents=True, exist_ok=True)
    return current


def memory_dir(scope_id: str) -> Path:
    bucket = _ensure_bucket(
        projects_root(),
        scope_slug(scope_id),
        _legacy_bucket_name(scope_id, fallback="default-scope"),
    )
    path = bucket / "memory"
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_dir(session_id: str) -> Path:
    path = _ensure_bucket(
        sessions_root(),
        session_slug(session_id),
        _legacy_bucket_name(session_id, fallback="default-session"),
    )
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


def scope_lock_path(scope_id: str) -> Path:
    return memory_dir(scope_id) / ".auto-dream.lock"


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
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _frontmatter_scalar(value: str) -> str:
    # YAML quoted scalars via JSON string encoding are sufficient for this frontmatter subset.
    return json.dumps(str(value), ensure_ascii=False)


def _validate_memory_filename(raw: str | None, *, fallback_name: str) -> Path:
    if not raw or not raw.strip():
        return Path(f"{slugify(fallback_name)}.md")
    candidate = Path(raw.strip())
    if candidate.is_absolute():
        raise ValueError("memory filename must be relative to the memory directory")
    parts: list[str] = []
    for part in candidate.parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("memory filename cannot traverse outside the memory directory")
        sanitized = SAFE_SEGMENT_RE.sub("-", part).strip("-")
        if not sanitized:
            raise ValueError("memory filename contains an empty path segment after sanitization")
        if len(sanitized.encode("utf-8")) > 255:
            raise ValueError("memory filename segment exceeds 255 bytes")
        parts.append(sanitized)
    if not parts:
        parts = [f"{slugify(fallback_name)}.md"]
    if not parts[-1].endswith(".md"):
        parts[-1] = f"{parts[-1]}.md"
    return Path(*parts)


def resolve_memory_path(scope_id: str, filename: str | None, *, fallback_name: str) -> Path:
    root = memory_dir(scope_id)
    rel = _validate_memory_filename(filename, fallback_name=fallback_name)
    path = (root / rel).resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    try:
        path.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("memory path escaped the memory directory") from exc
    return path


def parse_frontmatter(text: str) -> dict[str, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    out: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        raw = value.strip()
        if raw.startswith('"') and raw.endswith('"'):
            try:
                out[key.strip()] = json.loads(raw)
                continue
            except json.JSONDecodeError:
                pass
        out[key.strip()] = raw.strip('"')
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
    files: list[tuple[float, Path]] = []
    for p in root.rglob("*.md"):
        if p.name == "MEMORY.md":
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        files.append((mtime, p))
    files.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in files[:MAX_MEMORY_FILES]]


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
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(item.mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(item.mtime * 1000) % 1000:03d}Z"
        tag = f"[{item.type}] " if item.type else ""
        if item.description:
            lines.append(f"- {tag}{item.filename} ({ts}): {item.description}")
        else:
            lines.append(f"- {tag}{item.filename} ({ts})")
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
    kind = type_
    if kind not in VALID_MEMORY_TYPES:
        _log.warning("invalid memory type %r, falling back to 'project'", kind)
        kind = "project"
    body_text = body.strip()
    body_bytes = len(body_text.encode("utf-8"))
    if body_bytes > MAX_MEMORY_FILE_BYTES:
        raise ValueError(
            f"memory body is too large ({body_bytes} bytes > {MAX_MEMORY_FILE_BYTES} byte limit)"
        )
    path = resolve_memory_path(scope_id, filename, fallback_name=name)
    path.parent.mkdir(parents=True, exist_ok=True)
    already_existed = path.exists()
    now = _utc_now_iso()
    created = now
    if already_existed:
        existing = read_frontmatter(path)
        created = existing.get("created", now)
    payload = (
        "---\n"
        f"name: {_frontmatter_scalar(name)}\n"
        f"description: {_frontmatter_scalar(description)}\n"
        f"type: {_frontmatter_scalar(kind)}\n"
        f"created: {_frontmatter_scalar(created)}\n"
        f"updated: {_frontmatter_scalar(now)}\n"
        f"source: {_frontmatter_scalar(source)}\n"
        "---\n\n"
        f"{body_text}\n"
    )
    _atomic_write_text(path, payload, encoding="utf-8")
    rebuild_memory_index(scope_id)
    try:
        rel = str(path.resolve().relative_to(memory_dir(scope_id).resolve()))
    except ValueError:
        rel = path.name
    _emit_memory_event(
        action="updated" if already_existed else "created",
        scope_id=scope_id,
        filename=rel,
        memory_type=kind,
        name=name,
    )
    return path


def delete_memory_file(scope_id: str, filename: str) -> bool:
    path = resolve_memory_path(scope_id, filename, fallback_name="memory")
    if not path.is_file():
        return False
    fm = read_frontmatter(path)
    path.unlink()
    rebuild_memory_index(scope_id)
    _emit_memory_event(
        action="deleted",
        scope_id=scope_id,
        filename=filename,
        memory_type=fm.get("type", "project"),
        name=fm.get("name", filename),
    )
    return True


_last_rebuild: dict[str, float] = {}


def rebuild_memory_index(scope_id: str, *, force: bool = False) -> Path:
    now = time.time()
    if not force and (now - _last_rebuild.get(scope_id, 0)) < 1.0:
        return memory_index_path(scope_id)
    _last_rebuild[scope_id] = now
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
    _atomic_write_text(path, text, encoding="utf-8")
    return path


def append_transcript_event(session_id: str, payload: dict[str, Any]) -> None:
    path = session_transcript_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"uuid": uuid.uuid4().hex, **payload}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_transcript_events(session_id: str, *, limit: int = 80) -> list[dict[str, Any]]:
    path = session_transcript_path(session_id)
    if not path.is_file():
        return []
    # Tail-read: read from end of file to avoid OOM on large transcripts.
    # We accumulate a carry-over fragment so lines spanning chunk boundaries
    # are reassembled correctly.
    chunk_size = 8192
    lines: list[str] = []
    carry = ""
    with path.open("rb") as fh:
        fh.seek(0, 2)  # seek to end
        remaining = fh.tell()
        while remaining > 0 and len(lines) < limit + 1:
            read_size = min(chunk_size, remaining)
            remaining -= read_size
            fh.seek(remaining)
            chunk = fh.read(read_size).decode("utf-8", errors="replace")
            parts = chunk.split("\n")
            # Last element of parts joins with the carry from the previous
            # (rightward) chunk to form a complete line.
            parts[-1] = parts[-1] + carry
            carry = parts[0]  # first element may be a partial line
            lines = parts[1:] + lines
        # After all chunks, carry holds the remainder from the very start of
        # the file — prepend it as the first line.
        if carry:
            lines = [carry] + lines
    # Filter empty strings (from trailing newlines) before slicing so they
    # don't consume limit slots.
    lines = [ln for ln in lines if ln]
    rows = lines[-limit:]
    out: list[dict[str, Any]] = []
    for row in rows:
        if not row:
            continue
        try:
            data = json.loads(row)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def write_session_memory(session_id: str, content: str) -> Path:
    path = session_memory_path(session_id)
    _atomic_write_text(path, content.strip() + "\n", encoding="utf-8")
    return path


def read_session_memory(session_id: str) -> str:
    path = session_memory_path(session_id)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def list_scope_sessions(scope_id: str) -> list[Path]:
    out: list[Path] = []
    root = sessions_root()
    if not root.is_dir():
        return out
    entries = sorted(root.iterdir(), key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True)
    for path in entries[:500]:  # cap scan at 500
        if not path.is_dir():
            continue
        meta = read_json(path / "meta.json") or {}
        if meta.get("scope_id") == scope_id:
            out.append(path)
    return out


def read_last_consolidated_at(scope_id: str) -> float:
    path = scope_lock_path(scope_id)
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def try_acquire_scope_lock(scope_id: str, *, stale_after_seconds: int = 3600) -> float | None:
    path = scope_lock_path(scope_id)
    now = time.time()
    holder_pid: int | None = None
    existing_mtime = 0.0
    try:
        stat = path.stat()
        existing_mtime = stat.st_mtime
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
        holder_pid = int(raw) if raw.isdigit() else None
    except OSError:
        pass
    if existing_mtime and (now - existing_mtime) < stale_after_seconds and holder_pid is not None:
        try:
            os.kill(holder_pid, 0)
            return None
        except OSError:
            pass
    _atomic_write_text(path, str(os.getpid()), encoding="utf-8")
    try:
        verify = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if verify != str(os.getpid()):
        return None
    return existing_mtime


def rollback_scope_lock(scope_id: str, previous_mtime: float) -> None:
    path = scope_lock_path(scope_id)
    try:
        if previous_mtime <= 0:
            path.unlink(missing_ok=True)
            return
        _atomic_write_text(path, "", encoding="utf-8")
        os.utime(path, (previous_mtime, previous_mtime))
    except OSError:
        pass
