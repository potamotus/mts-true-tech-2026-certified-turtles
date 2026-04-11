from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
import threading

MAX_CACHE_ENTRIES = 100
MAX_CACHE_BYTES = 25 * 1024 * 1024


@dataclass
class FileState:
    content: str
    mtime_ns: int
    encoding: str = "utf-8"
    line_ending: str = "\n"
    is_partial_view: bool = False
    offset: int | None = None
    limit: int | None = None

    @property
    def size_bytes(self) -> int:
        return len(self.content.encode(self.encoding, errors="replace"))


_LOCK = threading.Lock()
_SESSION_CACHE: dict[str, OrderedDict[str, FileState]] = {}
_SESSION_SIZES: dict[str, int] = {}


def _trim_cache(session_id: str) -> None:
    cache = _SESSION_CACHE.get(session_id)
    if cache is None:
        return
    size = _SESSION_SIZES.get(session_id, 0)
    while cache and (len(cache) > MAX_CACHE_ENTRIES or size > MAX_CACHE_BYTES):
        _, state = cache.popitem(last=False)
        size -= state.size_bytes
    _SESSION_SIZES[session_id] = max(size, 0)


def note_file_read(
    session_id: str,
    path: Path,
    *,
    content: str,
    mtime_ns: int,
    encoding: str,
    line_ending: str,
    is_partial_view: bool,
    offset: int | None = None,
    limit: int | None = None,
) -> None:
    with _LOCK:
        cache = _SESSION_CACHE.setdefault(session_id, OrderedDict())
        key = str(path)
        prev = cache.pop(key, None)
        if prev is not None:
            _SESSION_SIZES[session_id] = max(0, _SESSION_SIZES.get(session_id, 0) - prev.size_bytes)
        cache[key] = FileState(
            content=content,
            mtime_ns=mtime_ns,
            encoding=encoding,
            line_ending=line_ending,
            is_partial_view=is_partial_view,
            offset=offset,
            limit=limit,
        )
        _SESSION_SIZES[session_id] = _SESSION_SIZES.get(session_id, 0) + cache[key].size_bytes
        _trim_cache(session_id)


def get_file_state(session_id: str, path: Path) -> FileState | None:
    with _LOCK:
        cache = _SESSION_CACHE.get(session_id)
        if cache is None:
            return None
        key = str(path)
        state = cache.pop(key, None)
        if state is None:
            return None
        cache[key] = state
        return state


def note_file_write(
    session_id: str,
    path: Path,
    *,
    content: str,
    mtime_ns: int,
    encoding: str,
    line_ending: str,
) -> None:
    note_file_read(
        session_id,
        path,
        content=content,
        mtime_ns=mtime_ns,
        encoding=encoding,
        line_ending=line_ending,
        is_partial_view=False,
    )
