from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from certified_turtles.memory_runtime.file_state import get_file_state, note_file_read, note_file_write
from certified_turtles.memory_runtime.request_context import current_request_context
from certified_turtles.memory_runtime.storage import claude_like_root
from certified_turtles.tools.registry import ToolSpec, register_tool
from certified_turtles.tools.workspace_storage import uploads_dir


def _generated_dir() -> Path:
    path = Path(os.environ.get("GENERATED_FILES_DIR", "/tmp/certified_turtles_generated"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _allowed_roots() -> tuple[Path, ...]:
    return (claude_like_root(), uploads_dir(), _generated_dir())


def _resolve_allowed_path(raw: Any) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Нужен непустой абсолютный file_path.")
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        raise ValueError("file_path должен быть абсолютным путём.")
    if str(path).startswith("/dev/"):
        raise ValueError("Спец-устройства не разрешены.")
    if str(path).startswith("//") or str(path).startswith("\\\\"):
        raise ValueError("UNC-пути не разрешены.")
    resolved = path.resolve(strict=False)
    for root in _allowed_roots():
        root_resolved = root.resolve(strict=False)
        try:
            resolved.relative_to(root_resolved)
            return resolved
        except ValueError:
            continue
    raise ValueError("Путь вне разрешённых директорий.")


def _current_session_id() -> str:
    ctx = current_request_context()
    return ctx.session_id if ctx is not None else "global"


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    encoding = _detect_encoding(data)
    return data.decode(encoding, errors="replace")


def _detect_encoding(data: bytes) -> str:
    if data.startswith(b"\xff\xfe"):
        return "utf-16le"
    if data.startswith(b"\xfe\xff"):
        return "utf-16be"
    return "utf-8"


def _detect_line_ending(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    return "\n"


def _is_binary_path(path: Path) -> bool:
    return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ipynb", ".zip", ".pptx", ".xlsx"}


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _handle_file_read(arguments: dict[str, Any]) -> str:
    path = _resolve_allowed_path(arguments.get("file_path"))
    if not path.is_file():
        return json.dumps({"error": "file_not_found", "file_path": str(path)}, ensure_ascii=False)
    if _is_binary_path(path):
        return json.dumps({"error": "binary_file_not_supported", "file_path": str(path)}, ensure_ascii=False)
    raw = path.read_bytes()
    encoding = _detect_encoding(raw)
    text = raw.decode(encoding, errors="replace")
    line_ending = _detect_line_ending(text)
    offset = int(arguments.get("offset") or 0)
    limit = arguments.get("limit")
    lines = text.splitlines()
    if limit is None:
        end = len(lines)
        partial = False
    else:
        end = min(len(lines), max(offset, 0) + max(int(limit), 0))
        partial = True
    body = "\n".join(f"{i + 1}\t{line}" for i, line in enumerate(lines[offset:end], start=offset))
    stat = path.stat()
    prev = get_file_state(_current_session_id(), path)
    if prev is not None and prev.mtime_ns == stat.st_mtime_ns and not partial and prev.content == text:
        return json.dumps({"file_path": str(path), "content": "[FILE_UNCHANGED_STUB]", "unchanged": True}, ensure_ascii=False)
    note_file_read(
        _current_session_id(),
        path,
        content=text,
        mtime_ns=stat.st_mtime_ns,
        encoding=encoding,
        line_ending=line_ending,
        is_partial_view=partial,
        offset=offset,
        limit=int(limit) if limit is not None else None,
    )
    return json.dumps(
        {
            "file_path": str(path),
            "offset": offset,
            "limit": limit,
            "truncated": end < len(lines),
            "encoding": encoding,
            "line_ending": "CRLF" if line_ending == "\r\n" else "LF",
            "content": body,
        },
        ensure_ascii=False,
    )


def _handle_file_write(arguments: dict[str, Any]) -> str:
    path = _resolve_allowed_path(arguments.get("file_path"))
    content = arguments.get("content")
    if not isinstance(content, str):
        return json.dumps({"error": "invalid_content"}, ensure_ascii=False)
    if _is_binary_path(path):
        return json.dumps({"error": "binary_file_not_supported", "file_path": str(path)}, ensure_ascii=False)
    session_id = _current_session_id()
    prev = get_file_state(session_id, path)
    encoding = prev.encoding if prev is not None else "utf-8"
    if path.exists():
        if prev is None:
            return json.dumps({"error": "read_before_write_required", "file_path": str(path)}, ensure_ascii=False)
        if prev.is_partial_view:
            return json.dumps({"error": "partial_read_not_enough", "file_path": str(path)}, ensure_ascii=False)
        stat = path.stat()
        if stat.st_mtime_ns > prev.mtime_ns:
            return json.dumps({"error": "stale_read", "file_path": str(path)}, ensure_ascii=False)
    line_ending = "\n"
    if prev is not None:
        line_ending = prev.line_ending
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").replace("\n", line_ending)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, normalized.encode(encoding, errors="replace"))
    stat = path.stat()
    note_file_write(session_id, path, content=normalized, mtime_ns=stat.st_mtime_ns, encoding=encoding, line_ending=line_ending)
    return json.dumps({"ok": True, "file_path": str(path), "bytes": len(normalized.encode(encoding, errors='replace'))}, ensure_ascii=False)


def _handle_file_edit(arguments: dict[str, Any]) -> str:
    path = _resolve_allowed_path(arguments.get("file_path"))
    old = arguments.get("old_string")
    new = arguments.get("new_string")
    replace_all = bool(arguments.get("replace_all"))
    if not isinstance(old, str) or not isinstance(new, str):
        return json.dumps({"error": "invalid_edit_strings"}, ensure_ascii=False)
    session_id = _current_session_id()
    prev = get_file_state(session_id, path)
    if prev is None or prev.is_partial_view:
        return json.dumps({"error": "read_before_edit_required", "file_path": str(path)}, ensure_ascii=False)
    if not path.is_file():
        return json.dumps({"error": "file_not_found", "file_path": str(path)}, ensure_ascii=False)
    stat = path.stat()
    if stat.st_mtime_ns > prev.mtime_ns:
        return json.dumps({"error": "stale_read", "file_path": str(path)}, ensure_ascii=False)
    text = _read_text(path)
    count = text.count(old)
    if count == 0:
        return json.dumps({"error": "old_string_not_found", "file_path": str(path)}, ensure_ascii=False)
    if count > 1 and not replace_all:
        return json.dumps({"error": "old_string_not_unique", "matches": count}, ensure_ascii=False)
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    _write_atomic(path, updated.encode(prev.encoding, errors="replace"))
    stat = path.stat()
    note_file_write(session_id, path, content=updated, mtime_ns=stat.st_mtime_ns, encoding=prev.encoding, line_ending=prev.line_ending)
    return json.dumps(
        {"ok": True, "file_path": str(path), "replacements": count if replace_all else 1},
        ensure_ascii=False,
    )


def _handle_glob_search(arguments: dict[str, Any]) -> str:
    pattern = arguments.get("pattern")
    root = _resolve_allowed_path(arguments.get("path") or str(claude_like_root()))
    if not isinstance(pattern, str) or not pattern.strip():
        return json.dumps({"error": "invalid_pattern"}, ensure_ascii=False)
    files = [str(p) for p in root.glob(pattern) if p.is_file()]
    files.sort()
    return json.dumps({"filenames": files[:100], "num_files": len(files), "truncated": len(files) > 100}, ensure_ascii=False)


def _handle_grep_search(arguments: dict[str, Any]) -> str:
    pattern = arguments.get("pattern")
    root = _resolve_allowed_path(arguments.get("path") or str(claude_like_root()))
    if not isinstance(pattern, str) or not pattern.strip():
        return json.dumps({"error": "invalid_pattern"}, ensure_ascii=False)
    expr = re.compile(pattern, re.IGNORECASE)
    lines: list[str] = []
    matches = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = _read_text(path)
        except OSError:
            continue
        for idx, line in enumerate(text.splitlines(), start=1):
            if expr.search(line):
                matches += 1
                if len(lines) < 250:
                    lines.append(f"{path}:{idx}:{line[:500]}")
    return json.dumps(
        {"content": "\n".join(lines), "num_matches": matches, "truncated": matches > len(lines)},
        ensure_ascii=False,
    )


register_tool(
    ToolSpec(
        name="file_read",
        description="Прочитать текстовый файл по абсолютному пути из разрешённых директорий. Для существующих файлов перед file_write/file_edit сначала вызови file_read.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["file_path"],
        },
        handler=_handle_file_read,
    )
)

register_tool(
    ToolSpec(
        name="file_write",
        description="Перезаписать файл по абсолютному пути. Для существующего файла нужен полный предшествующий file_read в этой сессии.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
        },
        handler=_handle_file_write,
    )
)

register_tool(
    ToolSpec(
        name="file_edit",
        description="Точечно заменить строку в файле по абсолютному пути. Требует предшествующий file_read.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
        handler=_handle_file_edit,
    )
)

register_tool(
    ToolSpec(
        name="glob_search",
        description="Поиск файлов по glob-паттерну в разрешённой директории.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern"],
        },
        handler=_handle_glob_search,
    )
)

register_tool(
    ToolSpec(
        name="grep_search",
        description="Поиск по содержимому файлов в разрешённой директории.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern"],
        },
        handler=_handle_grep_search,
    )
)
