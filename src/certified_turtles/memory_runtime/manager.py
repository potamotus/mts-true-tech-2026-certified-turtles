from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from certified_turtles.agent_debug_log import agent_logger
from certified_turtles.agents.json_agent_protocol import PROTOCOL_USER_PREFIX, message_text_content, parse_agent_response
from certified_turtles.mws_gpt.client import MWSGPTClient

from .forking import CacheSafeSnapshot, ForkRuntime
from .memory_types import MEMORY_FRONTMATTER_EXAMPLE, TYPES_SECTION, WHAT_NOT_TO_SAVE_SECTION
from .prompting import build_memory_prompt
from .storage import (
    append_transcript_event,
    ensure_session_meta,
    format_memory_manifest,
    list_scope_sessions,
    memory_dir,
    memory_index_path,
    read_json,
    read_last_consolidated_at,
    read_session_memory,
    scan_memory_headers,
    scope_meta_path,
    session_meta_path,
    try_acquire_scope_lock,
    write_json,
)


_log = agent_logger("memory_runtime")
_RUNTIME: "ClaudeLikeMemoryRuntime | None" = None

# ── Compaction constants ─────────────────────────────────────

_COMPACT_MIN_KEEP_TOKENS = 10_000
_COMPACT_MIN_KEEP_TEXT_MSGS = 5

# ── Microcompact constants ───────────────────────────────────

_MICROCOMPACT_TIME_GAP_SEC = 3600
_MICROCOMPACT_KEEP_RECENT = 6
_MICROCOMPACT_TOKEN_THRESHOLD = 80_000

# ── Auto-dream scan throttle ────────────────────────────────

_DREAM_SCAN_THROTTLE_SEC = 600

# ── Session memory template ─────────────────────────────────

_SESSION_MEMORY_TEMPLATE = """\
# Session Title
*A brief title describing the current task or session.*

# Current State
*What is the current status of the work?*

# Task Specification
*What was requested and what are the acceptance criteria?*

# Files and Functions
*Key files and functions being worked on.*

# Workflow
*Steps taken so far and next steps planned.*

# Errors & Corrections
*Errors encountered and how they were fixed.*

# Learnings
*Non-obvious things learned during this session.*

# Key Results
*Concrete outputs or deliverables produced.*

# Worklog
*Chronological record of major actions taken.*
"""


def _compact_threshold() -> int:
    try:
        return max(50_000, int(os.environ.get("CT_COMPACT_THRESHOLD", "150000")))
    except (TypeError, ValueError):
        return 150_000


def _extract_window_size() -> int:
    try:
        return max(4, min(20, int(os.environ.get("CT_EXTRACT_WINDOW", "8"))))
    except (TypeError, ValueError):
        return 8


class ClaudeLikeMemoryRuntime:
    def __init__(self):
        self.forks = ForkRuntime()
        self._extract_in_progress: set[str] = set()
        self._extract_trailing: dict[str, tuple[str, str, str]] = {}
        self._session_updates: dict[str, float] = {}
        self._last_dream_scan_at: dict[str, float] = {}

    # ── prepare ──────────────────────────────────────────────

    def prepare_messages(
        self,
        client: MWSGPTClient | None,
        *,
        model: str,
        messages: list[dict[str, Any]],
        session_id: str,
        scope_id: str,
    ) -> list[dict[str, Any]]:
        ensure_session_meta(session_id, scope_id=scope_id)
        query = self._last_user_text(messages)
        prompt = build_memory_prompt(
            client,
            model=model,
            messages=messages,
            scope_id=scope_id,
            session_id=session_id,
            user_query=query,
        )
        work = [dict(m) for m in messages]
        if prompt.prompt:
            work.insert(0, {"role": "system", "content": prompt.prompt})
        # Microcompact old tool results, then compact if needed.
        work = self._microcompact_tool_results(work, session_id)
        work = self._compact_if_needed(work, session_id)
        self.forks.save_snapshot(
            CacheSafeSnapshot(
                model=model,
                scope_id=scope_id,
                session_id=session_id,
                file_state_namespace=session_id,
                messages=[dict(m) for m in work],
                saved_at=time.time(),
            )
        )
        meta = read_json(session_meta_path(session_id)) or {}
        meta["recent_messages"] = [dict(m) for m in messages[-8:]]
        write_json(session_meta_path(session_id), meta)
        return work

    # ── after response ───────────────────────────────────────

    def after_response(
        self,
        client: MWSGPTClient,
        *,
        model: str,
        prepared_messages: list[dict[str, Any]],
        final_messages: list[dict[str, Any]],
        session_id: str,
        scope_id: str,
    ) -> None:
        ensure_session_meta(session_id, scope_id=scope_id)
        self._append_transcript(session_id, final_messages)
        self.forks.save_snapshot(
            CacheSafeSnapshot(
                model=model,
                scope_id=scope_id,
                session_id=session_id,
                file_state_namespace=session_id,
                messages=[dict(m) for m in prepared_messages],
                saved_at=time.time(),
            )
        )
        self._note_session_turn(session_id)
        if self._main_agent_wrote_memory(scope_id, final_messages):
            if self._should_update_session_memory(session_id, final_messages):
                self._launch_post_hook(
                    client,
                    session_id=session_id,
                    scope_id=scope_id,
                    agent_id="session_memory",
                    prompt=self._session_memory_prompt(session_id),
                )
                self._mark_session_memory_extracted(session_id, final_messages)
            self._maybe_launch_auto_dream(client, session_id=session_id, scope_id=scope_id)
            return
        self._launch_extract_hook(
            client,
            session_id=session_id,
            scope_id=scope_id,
            prompt=self._extractor_prompt(scope_id, final_messages),
        )
        if self._should_update_session_memory(session_id, final_messages):
            self._launch_post_hook(
                client,
                session_id=session_id,
                scope_id=scope_id,
                agent_id="session_memory",
                prompt=self._session_memory_prompt(session_id),
            )
            self._mark_session_memory_extracted(session_id, final_messages)
        self._maybe_launch_auto_dream(client, session_id=session_id, scope_id=scope_id)

    # ── compaction (4a) ──────────────────────────────────────

    def _compact_if_needed(self, messages: list[dict[str, Any]], session_id: str) -> list[dict[str, Any]]:
        total_tokens = self._estimate_message_tokens(messages)
        if total_tokens < _compact_threshold():
            return messages
        session_mem = read_session_memory(session_id).strip()
        if not session_mem:
            return messages
        kept_tokens = 0
        text_msgs = 0
        cut_index = len(messages)
        for i in range(len(messages) - 1, -1, -1):
            msg_tokens = max(1, len(message_text_content(messages[i]).encode("utf-8", errors="replace")) // 4)
            kept_tokens += msg_tokens
            if messages[i].get("role") in ("user", "assistant") and message_text_content(messages[i]).strip():
                text_msgs += 1
            if kept_tokens >= _COMPACT_MIN_KEEP_TOKENS and text_msgs >= _COMPACT_MIN_KEEP_TEXT_MSGS:
                cut_index = i
                break
        if cut_index <= 1:
            return messages
        if cut_index < 4:
            return messages
        compacted: list[dict[str, Any]] = []
        for msg in messages[:cut_index]:
            if msg.get("role") == "system":
                compacted.append(msg)
        compacted.append({"role": "user", "content": f"[Session context was compacted. Previous conversation summary:]\n\n{session_mem}"})
        compacted.append({"role": "assistant", "content": "Understood. I have the session context summary. Continuing from where we left off."})
        compacted.extend(messages[cut_index:])
        _log.debug("compact_if_needed: %d -> %d messages, cut at %d", len(messages), len(compacted), cut_index)
        return compacted

    # ── microcompact (4f) ────────────────────────────────────

    def _microcompact_tool_results(self, messages: list[dict[str, Any]], session_id: str) -> list[dict[str, Any]]:
        if len(messages) < 10:
            return messages
        total_tokens = self._estimate_message_tokens(messages)
        last_activity = self._session_updates.get(session_id, 0.0)
        time_gap = last_activity > 0 and (time.time() - last_activity) > _MICROCOMPACT_TIME_GAP_SEC
        if not time_gap and total_tokens < _MICROCOMPACT_TOKEN_THRESHOLD:
            return messages
        result = [dict(m) for m in messages]
        tool_result_indices: list[int] = []
        for i, msg in enumerate(result):
            if msg.get("role") == "user" and message_text_content(msg).startswith(PROTOCOL_USER_PREFIX):
                tool_result_indices.append(i)
        to_clear = tool_result_indices[:-_MICROCOMPACT_KEEP_RECENT] if len(tool_result_indices) > _MICROCOMPACT_KEEP_RECENT else []
        for idx in to_clear:
            result[idx] = dict(result[idx])
            result[idx]["content"] = PROTOCOL_USER_PREFIX + "\n[Old tool result content cleared]"
        if to_clear:
            _log.debug("microcompact: cleared %d old tool results", len(to_clear))
        return result

    # ── transcript ───────────────────────────────────────────

    def _append_transcript(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        for msg in messages[-12:]:
            role = msg.get("role")
            content = message_text_content(msg)
            append_transcript_event(session_id, {"timestamp": time.time(), "role": role, "content": content, "kind": "message"})
            if role == "assistant":
                parsed = parse_agent_response(content)
                if parsed is not None:
                    for call in parsed.get("calls", []):
                        append_transcript_event(
                            session_id,
                            {"timestamp": time.time(), "role": "assistant", "kind": "assistant_tool_call", "tool_name": call.get("name"), "arguments": call.get("arguments", {})},
                        )
            elif role == "user" and content.startswith(PROTOCOL_USER_PREFIX):
                raw = content[len(PROTOCOL_USER_PREFIX) :].strip()
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                outputs = payload.get("tool_outputs", [])
                if isinstance(outputs, list):
                    for item in outputs:
                        if not isinstance(item, dict):
                            continue
                        append_transcript_event(
                            session_id,
                            {"timestamp": time.time(), "role": "user", "kind": "tool_result", "tool_name": item.get("name"), "output": item.get("output")},
                        )

    _MAX_SESSION_UPDATES = 500

    def _note_session_turn(self, session_id: str) -> None:
        self._session_updates[session_id] = time.time()
        if len(self._session_updates) > self._MAX_SESSION_UPDATES:
            # Evict oldest half to amortize cleanup cost.
            sorted_keys = sorted(self._session_updates, key=self._session_updates.get)  # type: ignore[arg-type]
            for k in sorted_keys[: len(sorted_keys) // 2]:
                del self._session_updates[k]

    # ── session memory decision ──────────────────────────────

    def _should_update_session_memory(self, session_id: str, messages: list[dict[str, Any]]) -> bool:
        current_tokens = self._estimate_message_tokens(messages)
        meta_path = session_meta_path(session_id)
        meta = read_json(meta_path) or {}
        if not meta.get("session_memory_initialized"):
            if current_tokens < 10_000:
                return False
            meta["session_memory_initialized"] = True
        last_tokens = int(meta.get("session_memory_tokens_at_last_extract", 0) or 0)
        if current_tokens - last_tokens < 5_000:
            return False
        recent_tool_calls = self._count_recent_tool_calls(messages[-8:])
        last_assistant_has_tool_calls = self._last_assistant_has_tool_calls(messages)
        if recent_tool_calls < 3 and last_assistant_has_tool_calls:
            return False
        return True

    def _mark_session_memory_extracted(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        current_tokens = self._estimate_message_tokens(messages)
        meta_path = session_meta_path(session_id)
        meta = read_json(meta_path) or {}
        meta["session_memory_initialized"] = True
        meta["session_memory_tokens_at_last_extract"] = current_tokens
        meta["session_memory_last_checked_at"] = time.time()
        write_json(meta_path, meta)

    # ── extract hook ─────────────────────────────────────────

    def _launch_extract_hook(
        self,
        client: MWSGPTClient,
        *,
        session_id: str,
        scope_id: str,
        prompt: str,
    ) -> None:
        if session_id in self._extract_in_progress:
            self._extract_trailing[session_id] = (scope_id, "memory_extractor", prompt)
            return
        self._extract_in_progress.add(session_id)

        async def runner():
            try:
                result = await asyncio.to_thread(
                    self.forks.run_named_subagent,
                    client,
                    session_id=session_id,
                    agent_id="memory_extractor",
                    prompt=prompt,
                )
                if result is None:
                    _log.warning("extract hook returned None (no snapshot or spec?) session=%s", session_id)
                elif result.get("truncated"):
                    _log.warning("extract hook truncated (round limit) session=%s", session_id)
            except Exception:
                _log.exception("extract hook FAILED session=%s scope=%s", session_id, scope_id)
            finally:
                self._extract_in_progress.discard(session_id)
                trailing = self._extract_trailing.pop(session_id, None)
                if trailing is not None:
                    next_scope_id, _, next_prompt = trailing
                    self._launch_extract_hook(
                        client,
                        session_id=session_id,
                        scope_id=next_scope_id,
                        prompt=next_prompt,
                    )

        try:
            asyncio.get_running_loop().create_task(runner())
        except RuntimeError:
            self._extract_in_progress.discard(session_id)

    def _launch_post_hook(
        self,
        client: MWSGPTClient,
        *,
        session_id: str,
        scope_id: str,
        agent_id: str,
        prompt: str,
    ) -> None:
        async def runner():
            try:
                await asyncio.to_thread(
                    self.forks.run_named_subagent,
                    client,
                    session_id=session_id,
                    agent_id=agent_id,
                    prompt=prompt,
                )
            except Exception:
                _log.exception("post hook failed agent=%s session=%s", agent_id, session_id)

        try:
            asyncio.get_running_loop().create_task(runner())
        except RuntimeError:
            _log.debug("no running loop for post hook agent=%s session=%s", agent_id, session_id)

    # ── extractor prompt (4c, 4d) ────────────────────────────

    def _extractor_prompt(self, scope_id: str, messages: list[dict[str, Any]]) -> str:
        window = _extract_window_size()
        preview = []
        for item in messages[-window:]:
            role = item.get("role", "unknown")
            content = message_text_content(item)
            if content.strip():
                preview.append(f"<<{role}>>\n{content}")
        body = "\n\n".join(preview)
        example = "\n".join(MEMORY_FRONTMATTER_EXAMPLE)
        types = "\n".join(TYPES_SECTION)
        exclusions = "\n".join(WHAT_NOT_TO_SAVE_SECTION)
        headers = scan_memory_headers(scope_id)
        manifest = format_memory_manifest(headers) if headers else "(no existing memory files)"
        return (
            f"You are now acting as the memory extraction subagent. Analyze only the most recent messages above and update persistent memory files in `{memory_dir(scope_id)}`.\n\n"
            "You have a limited turn budget. Efficient strategy: read candidate memory files first, then update them. Prefer updating an existing topic file over creating duplicates. Do not inspect the codebase to derive facts that are available from the repository state.\n\n"
            "If the user explicitly asked to remember something durable, save it immediately as the best-fitting memory type. If they asked to forget something, remove or update the relevant memory.\n\n"
            f"{types}\n{exclusions}\n\n"
            "## How to save memories\n\n"
            "Write each memory to its own topic file using this frontmatter format:\n\n"
            f"{example}\n\n"
            "Then update MEMORY.md as a concise index: one-line hooks only, no content dump.\n\n"
            "## Existing memory files\n\n"
            "These files already exist in the memory directory. Read and update existing topic files instead of creating duplicates:\n\n"
            f"{manifest}\n\n"
            "## Recent conversation slice\n\n"
            f"{body}"
        )

    # ── session memory prompt (4e) ───────────────────────────

    def _session_memory_prompt(self, session_id: str) -> str:
        existing = read_session_memory(session_id).strip()
        template_note = ""
        if not existing:
            template_note = (
                f"\n\nUse this template structure for the session memory:\n\n{_SESSION_MEMORY_TEMPLATE}\n"
                "Each section has an italic description (keep it). Fill in content below each."
            )
        return (
            "Update the session memory file with the current state of work. "
            "Keep it concise and structured. Update the existing file incrementally, "
            "do not rewrite from scratch unless the structure is missing. "
            f"Target file: {session_id} session memory."
            f"{template_note}"
        )

    # ── auto-dream (4b throttle) ─────────────────────────────

    def _maybe_launch_auto_dream(self, client: MWSGPTClient, *, session_id: str, scope_id: str) -> None:
        now = time.time()
        last_consolidated = read_last_consolidated_at(scope_id)
        if last_consolidated and (now - last_consolidated) < 86400:
            return
        last_scan = self._last_dream_scan_at.get(scope_id, 0.0)
        if now - last_scan < _DREAM_SCAN_THROTTLE_SEC:
            return
        self._last_dream_scan_at[scope_id] = now
        if len(list_scope_sessions(scope_id)) < 5:
            return
        prior = try_acquire_scope_lock(scope_id)
        if prior is None:
            return
        self._launch_post_hook(
            client,
            session_id=session_id,
            scope_id=scope_id,
            agent_id="auto_dream",
            prompt=(
                f"# Dream: Memory Consolidation\n\n"
                f"You are performing a reflective pass over the memory directory `{memory_dir(scope_id)}`.\n"
                f"Rebuild `{memory_index_path(scope_id)}` after consolidating.\n\n"
                "Phase 1 — Orient: list the memory directory, read MEMORY.md, inspect topic files before creating duplicates.\n"
                "Phase 2 — Gather recent signal: use transcript evidence narrowly; do not read the entire world.\n"
                "Phase 3 — Consolidate: merge duplicates, convert relative dates to absolute dates, fix contradicted facts at the source.\n"
                "Phase 4 — Prune and index: keep MEMORY.md concise, one-line hooks only, remove stale pointers and contradictions.\n\n"
                "Return a brief summary of what changed. If nothing changed, say so."
            ),
        )
        meta = read_json(scope_meta_path(scope_id)) or {}
        meta["last_auto_dream_trigger_at"] = now
        meta["auto_dream_lock_previous_mtime"] = prior
        write_json(scope_meta_path(scope_id), meta)

    # ── helpers ──────────────────────────────────────────────

    def _last_user_text(self, messages: list[dict[str, Any]]) -> str:
        for item in reversed(messages):
            if item.get("role") == "user":
                return message_text_content(item)
        return ""

    def _main_agent_wrote_memory(self, scope_id: str, messages: list[dict[str, Any]]) -> bool:
        root = str(memory_dir(scope_id))
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            parsed = parse_agent_response(message_text_content(msg))
            if parsed is None:
                continue
            for call in parsed.get("calls", []):
                name = call.get("name")
                args = call.get("arguments", {})
                path = ""
                if isinstance(args, dict):
                    path = str(args.get("file_path", ""))
                if name in {"file_write", "file_edit", "memory_write", "memory_edit"} and path.startswith(root):
                    return True
        return False

    def _estimate_message_tokens(self, messages: list[dict[str, Any]]) -> int:
        total = 0
        for item in messages:
            total += max(1, len(message_text_content(item).encode("utf-8", errors="replace")) // 4)
        return total

    def _count_recent_tool_calls(self, messages: list[dict[str, Any]]) -> int:
        total = 0
        for item in messages:
            if item.get("role") != "assistant":
                continue
            parsed = parse_agent_response(message_text_content(item))
            if parsed is None:
                continue
            total += len(parsed.get("calls", []))
        return total

    def _last_assistant_has_tool_calls(self, messages: list[dict[str, Any]]) -> bool:
        for item in reversed(messages):
            if item.get("role") != "assistant":
                continue
            parsed = parse_agent_response(message_text_content(item))
            if parsed is None:
                return False
            return bool(parsed.get("calls"))
        return False


def runtime_from_env() -> ClaudeLikeMemoryRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = ClaudeLikeMemoryRuntime()
    return _RUNTIME
