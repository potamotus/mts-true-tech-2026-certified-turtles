from __future__ import annotations

import calendar
import fnmatch
from dataclasses import dataclass
import time
from typing import Any

from certified_turtles.agents.json_agent_protocol import message_text_content, parse_agent_response
from certified_turtles.mws_gpt.client import MWSGPTClient

from .memory_types import memory_instructions
from .selector import select_relevant_memories
from .static_instructions import ConditionalRule, load_conditional_rules, load_static_instruction_prompt
from .storage import (
    MAX_MEMORY_INDEX_BYTES,
    MAX_MEMORY_INDEX_LINES,
    MAX_MEMORY_SESSION_BYTES,
    MAX_RELEVANT_MEMORIES,
    list_instruction_files,
    memory_dir,
    memory_index_path,
    read_json,
    read_body,
    read_frontmatter,
    read_session_memory,
    scan_memory_headers,
    session_meta_path,
    write_json,
)


def _truncate_entrypoint_content(raw: str) -> str:
    """Truncate MEMORY.md content to line and byte caps, appending a warning.

    Matches Claude Code's truncateEntrypointContent() from memdir.ts.
    """
    trimmed = raw.strip()
    lines = trimmed.split("\n")
    line_count = len(lines)
    byte_count = len(trimmed.encode("utf-8", errors="replace"))

    was_line_truncated = line_count > MAX_MEMORY_INDEX_LINES
    was_byte_truncated = byte_count > MAX_MEMORY_INDEX_BYTES

    if not was_line_truncated and not was_byte_truncated:
        return trimmed

    truncated = "\n".join(lines[:MAX_MEMORY_INDEX_LINES]) if was_line_truncated else trimmed

    encoded = truncated.encode("utf-8", errors="replace")
    if len(encoded) > MAX_MEMORY_INDEX_BYTES:
        cut = truncated.rfind("\n", 0, MAX_MEMORY_INDEX_BYTES)
        truncated = truncated[: cut if cut > 0 else MAX_MEMORY_INDEX_BYTES]

    if was_byte_truncated and not was_line_truncated:
        reason = f"{byte_count} bytes (limit: {MAX_MEMORY_INDEX_BYTES}) — index entries are too long"
    elif was_line_truncated and not was_byte_truncated:
        reason = f"{line_count} lines (limit: {MAX_MEMORY_INDEX_LINES})"
    else:
        reason = f"{line_count} lines and {byte_count} bytes"

    return (
        truncated
        + f"\n\n> WARNING: MEMORY.md is {reason}. Only part of it was loaded. "
        "Keep index entries to one line under ~200 chars; move detail into topic files."
    )


def _memory_age_days(mtime: float) -> int:
    return max(0, int((time.time() - mtime) / 86400))


def _memory_freshness_text(mtime: float) -> str:
    """Match Claude Code's memoryFreshnessText() from memoryAge.ts."""
    d = _memory_age_days(mtime)
    if d <= 1:
        return ""
    return (
        f"This memory is {d} days old. "
        "Memories are point-in-time observations, not live state — "
        "claims about code behavior or file:line citations may be outdated. "
        "Verify against current code before asserting as fact."
    )


def _memory_age_warning(updated: str) -> str:
    if not updated:
        return ""
    try:
        stamp = time.strptime(updated, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return ""
    mtime = calendar.timegm(stamp)
    text = _memory_freshness_text(mtime)
    if not text:
        return ""
    return f"\n{text}"


def _estimate_tokens(text: str) -> int:
    return max(1, len(text.encode("utf-8", errors="replace")) // 4)


def _recent_tools(messages: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in messages[-8:]:
        if item.get("role") != "assistant":
            continue
        parsed = parse_agent_response(message_text_content(item))
        if parsed is None:
            continue
        for call in parsed.get("calls", []):
            name = call.get("name")
            if not isinstance(name, str) or not name or name in seen:
                continue
            seen.add(name)
            out.append(name)
    return out


def _extract_file_paths_from_messages(messages: list[dict[str, Any]]) -> set[str]:
    paths: set[str] = set()
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        parsed = parse_agent_response(message_text_content(msg))
        if parsed is None:
            continue
        for call in parsed.get("calls", []):
            args = call.get("arguments", {})
            if isinstance(args, dict):
                for key in ("file_path", "path", "filename", "pattern"):
                    val = args.get(key)
                    if isinstance(val, str) and val.strip():
                        paths.add(val.strip())
    return paths


def _match_conditional_rules(
    rules: list[ConditionalRule],
    active_paths: set[str],
) -> list[ConditionalRule]:
    matched: list[ConditionalRule] = []
    for rule in rules:
        for glob_pattern in rule.globs:
            if any(fnmatch.fnmatch(p, glob_pattern) for p in active_paths):
                matched.append(rule)
                break
    return matched


@dataclass(frozen=True)
class MemoryPromptBundle:
    prompt: str
    selected_memories: tuple[str, ...]


def build_memory_prompt(
    client: MWSGPTClient | None,
    *,
    model: str,
    messages: list[dict[str, Any]],
    scope_id: str,
    session_id: str,
    user_query: str,
) -> MemoryPromptBundle:
    mem_root = memory_dir(scope_id)
    static_prompt = load_static_instruction_prompt()
    parts: list[str] = []
    if static_prompt:
        parts.extend([static_prompt, ""])
    conditional_rules = load_conditional_rules()
    if conditional_rules:
        active_paths = _extract_file_paths_from_messages(messages)
        for rule in _match_conditional_rules(conditional_rules, active_paths):
            parts.append(f"## Conditional Rule: {rule.path.name}\n{rule.content}")
    parts.append(memory_instructions(str(mem_root), include_index_rules=True, for_main_agent=True))

    index_path = memory_index_path(scope_id)
    if index_path.is_file():
        raw_index = index_path.read_text(encoding='utf-8', errors='replace').strip()
        index_content = _truncate_entrypoint_content(raw_index)
        parts.extend(["", "## MEMORY.md", "", index_content])
    else:
        parts.extend(["", "## MEMORY.md", "", "Your MEMORY.md is currently empty. When you save new memories, they will appear here."])

    # Instructions — always injected (not by relevance)
    instr_files = list_instruction_files(scope_id)
    if instr_files:
        parts.append("")
        parts.append("## instructions")
        parts.append("")
        parts.append("The following behavioral rules were set by the user. Follow them in every response.")
        for ipath in instr_files:
            body = read_body(ipath).strip()
            if not body:
                continue
            fm = read_frontmatter(ipath)
            title = fm.get("name", ipath.stem)
            source = fm.get("source", "user")
            parts.append(f"### {title} (source: {source})")
            parts.append(body)

    headers = scan_memory_headers(scope_id)
    selected: list[str] = []
    meta = read_json(session_meta_path(session_id)) or {}
    already_surfaced = {str(x) for x in meta.get("surfaced_memories", []) if isinstance(x, str)}
    if client is not None:
        selected = select_relevant_memories(
            client,
            model=model,
            query=user_query,
            headers=headers,
            limit=MAX_RELEVANT_MEMORIES,
            recent_tools=_recent_tools(messages),
            already_surfaced=already_surfaced,
        )
    if selected:
        parts.append("")
        parts.append("## relevant_memories")
        parts.append("")
        parts.append("These are facts you know about the user. Use them to personalize your responses. When the user asks what you remember — share these facts.")
        total = 0
        for filename in selected:
            path = mem_root / filename
            if not path.is_file():
                continue
            body = read_body(path).strip()
            fm = read_frontmatter(path)
            max_visible_bytes = min(MAX_MEMORY_SESSION_BYTES, 4096)
            visible_body = body.encode("utf-8", errors="replace")[:max_visible_bytes].decode("utf-8", errors="ignore").strip()
            encoded = len(visible_body.encode("utf-8", errors="replace"))
            if total + encoded > MAX_MEMORY_SESSION_BYTES:
                continue
            total += encoded
            title = fm.get("name", filename)
            warning = _memory_age_warning(fm.get("updated", ""))
            parts.append(f"### {title} ({fm.get('type', 'project')})")
            parts.append(visible_body)
            if warning:
                parts.append(warning)

    session_memory = read_session_memory(session_id).strip()
    if session_memory:
        session_tokens = _estimate_tokens(session_memory)
        if session_tokens > 3000:
            visible_bytes = min(len(session_memory.encode("utf-8", errors="replace")), 12_000)
            session_memory = session_memory.encode("utf-8", errors="replace")[:visible_bytes].decode("utf-8", errors="ignore").strip()
        parts.extend(["", "# session_memory", session_memory])

    if selected:
        merged = list(dict.fromkeys([*selected, *already_surfaced]))
        meta["surfaced_memories"] = merged[:50]
        write_json(session_meta_path(session_id), meta)
    return MemoryPromptBundle(prompt="\n".join(parts).strip(), selected_memories=tuple(selected))
