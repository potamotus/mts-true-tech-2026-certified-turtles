from __future__ import annotations

import time

from certified_turtles.mws_gpt.client import MWSGPTClient

from .selector import select_relevant_memories
from .storage import (
    MAX_MEMORY_SESSION_BYTES,
    MAX_RELEVANT_MEMORIES,
    memory_dir,
    memory_index_path,
    read_body,
    read_frontmatter,
    read_session_memory,
    scan_memory_headers,
)


def _memory_age_warning(updated: str) -> str:
    if not updated:
        return ""
    try:
        stamp = time.strptime(updated, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return ""
    age_days = int((time.time() - time.mktime(stamp)) / 86400)
    if age_days <= 1:
        return ""
    return (
        f"\nThis memory is {age_days} days old. Memories are point-in-time observations. "
        "Verify code, files and URLs before treating them as current facts."
    )


def build_memory_prompt(
    client: MWSGPTClient | None,
    *,
    model: str,
    scope_id: str,
    session_id: str,
    user_query: str,
) -> str:
    mem_root = memory_dir(scope_id)
    parts: list[str] = [
        "# session_guidance",
        "You have a Claude-like persistent memory system and session memory system.",
        "",
        "# memory",
        f"Memory directory: {mem_root}",
        "Memory types: user, feedback, project, reference.",
        "Save only durable facts. Do not save code patterns, file trees, ephemeral task state or secrets.",
        "If the user asks to remember something durable, update memory files and MEMORY.md.",
        "",
        "# before_recommending_from_memory",
        "If a memory mentions a file, path, URL or implementation detail, verify it before asserting it as current fact.",
    ]

    index_path = memory_index_path(scope_id)
    if index_path.is_file():
        parts.extend(["", "## MEMORY.md", index_path.read_text(encoding='utf-8', errors='replace').strip()])

    headers = scan_memory_headers(scope_id)
    selected: list[str] = []
    if client is not None:
        selected = select_relevant_memories(
            client,
            model=model,
            query=user_query,
            headers=headers,
            limit=MAX_RELEVANT_MEMORIES,
        )
    if selected:
        parts.append("")
        parts.append("## relevant_memories")
        total = 0
        for filename in selected:
            path = mem_root / filename
            if not path.is_file():
                continue
            body = read_body(path).strip()
            fm = read_frontmatter(path)
            encoded = len(body.encode("utf-8"))
            if total + encoded > MAX_MEMORY_SESSION_BYTES:
                break
            total += encoded
            title = fm.get("name", filename)
            warning = _memory_age_warning(fm.get("updated", ""))
            parts.append(f"### {title} ({fm.get('type', 'project')})")
            parts.append(body[:4096])
            if warning:
                parts.append(warning)

    session_memory = read_session_memory(session_id).strip()
    if session_memory:
        parts.extend(["", "# session_memory", session_memory[:12_000]])
    return "\n".join(parts).strip()
