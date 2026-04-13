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
from .memory_types import (
    MEMORY_FRONTMATTER_EXAMPLE,
    TYPES_SECTION,
    WHAT_NOT_TO_SAVE_SECTION,
)
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
_COMPACT_MAX_KEEP_TOKENS = 40_000

# ── Microcompact constants ───────────────────────────────────

_MICROCOMPACT_TIME_GAP_SEC = 3600
_MICROCOMPACT_KEEP_RECENT = 6
_MICROCOMPACT_TOKEN_THRESHOLD = 80_000

# ── Full compact (9-section LLM summary) ────────────────────

AUTOCOMPACT_BUFFER_TOKENS = 13_000
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3

_NO_TOOLS_PREAMBLE = (
    "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n\n"
    "- Do NOT use file_read, execute_python, web_search, fetch_url, or ANY other tool.\n"
    "- You already have all the context you need in the conversation above.\n"
    "- Tool calls will be REJECTED and will waste your only turn — you will fail the task.\n"
    "- Your entire response must be plain text: an <analysis> block followed by a <summary> block.\n\n"
)

_BASE_COMPACT_PROMPT = (
    "Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.\n"
    "This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.\n\n"
    "Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:\n\n"
    "1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:\n"
    "   - The user's explicit requests and intents\n"
    "   - Your approach to addressing the user's requests\n"
    "   - Key decisions, technical concepts and code patterns\n"
    "   - Specific details like:\n"
    "     - file names\n"
    "     - full code snippets\n"
    "     - function signatures\n"
    "     - file edits\n"
    "   - Errors that you ran into and how you fixed them\n"
    "   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.\n"
    "2. Double-check for technical accuracy and completeness, addressing each required element thoroughly.\n\n"
    "Your summary should include the following sections:\n\n"
    "1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail\n"
    "2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.\n"
    "3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Pay special attention to the most recent messages and include full code snippets where applicable and include a summary of why this file read or edit is important.\n"
    "4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.\n"
    "5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.\n"
    "6. All user messages: List ALL user messages that are not tool results. These are critical for understanding the users' feedback and changing intent.\n"
    "7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.\n"
    "8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant. Include file names and code snippets where applicable.\n"
    "9. Optional Next Step: List the next step that you will take that is related to the most recent work you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user's most recent explicit requests, and the task you were working on immediately before this summary request. If your last task was concluded, then only list next steps if they are explicitly in line with the users request. Do not start on tangential requests or really old requests that were already completed without confirming with the user first.\n"
    "                       If there is a next step, include direct quotes from the most recent conversation showing exactly what task you were working on and where you left off. This should be verbatim to ensure there's no drift in task interpretation.\n\n"
    "Please provide your summary based on the conversation so far, following this structure and ensuring precision and thoroughness in your response.\n\n"
    "REMINDER: Do NOT call any tools. Respond with plain text only — "
    "an <analysis> block followed by a <summary> block. "
    "Tool calls will be rejected and you will fail the task."
)

# ── Auto-dream scan throttle ────────────────────────────────

_DREAM_SCAN_THROTTLE_SEC = 600

# ── Session memory template ─────────────────────────────────

_SESSION_MEMORY_TEMPLATE = """\
# Session Title
_A short and distinctive 5-10 word descriptive title for the session. Super info dense, no filler_

# Current State
_What is actively being worked on right now? Pending tasks not yet completed. Immediate next steps._

# Task specification
_What did the user ask to build? Any design decisions or other explanatory context_

# Files and Functions
_What are the important files? In short, what do they contain and why are they relevant?_

# Workflow
_What bash commands are usually run and in what order? How to interpret their output if not obvious?_

# Errors & Corrections
_Errors encountered and how they were fixed. What did the user correct? What approaches failed and should not be tried again?_

# Codebase and System Documentation
_What are the important system components? How do they work/fit together?_

# Learnings
_What has worked well? What has not? What to avoid? Do not duplicate items from other sections_

# Key results
_If the user asked a specific output such as an answer to a question, a table, or other document, repeat the exact result here_

# Worklog
_Step by step, what was attempted, done? Very terse summary for each step_
"""

_MAX_SECTION_LENGTH = 2000
_MAX_TOTAL_SESSION_MEMORY_TOKENS = 12000


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

    # ── compaction (4a) — session-memory-first, then full LLM compact ──

    _consecutive_compact_failures: dict[str, int] = {}

    def _compact_if_needed(self, messages: list[dict[str, Any]], session_id: str) -> list[dict[str, Any]]:
        total_tokens = self._estimate_message_tokens(messages)
        threshold = _compact_threshold()
        if total_tokens < threshold:
            return messages

        # Strategy 1: session-memory compact (cheap, no LLM call)
        session_mem = read_session_memory(session_id).strip()
        if session_mem:
            result = self._session_memory_compact(messages, session_mem)
            if result is not None:
                post_tokens = self._estimate_message_tokens(result)
                if post_tokens < threshold:
                    _log.debug("compact_if_needed: session-memory compact %d -> %d messages", len(messages), len(result))
                    return result

        # Strategy 2: full 9-section LLM compact
        failures = self._consecutive_compact_failures.get(session_id, 0)
        if failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
            _log.warning("compact_if_needed: circuit breaker open (%d consecutive failures) session=%s", failures, session_id)
            return messages

        result = self._full_compact(messages, session_id)
        if result is not None:
            self._consecutive_compact_failures[session_id] = 0
            return result
        else:
            self._consecutive_compact_failures[session_id] = failures + 1
            _log.warning("compact_if_needed: full compact failed (failures=%d) session=%s", failures + 1, session_id)
            return messages

    def _session_memory_compact(self, messages: list[dict[str, Any]], session_mem: str) -> list[dict[str, Any]] | None:
        """Cheap compaction using existing session memory as summary (no LLM call)."""
        kept_tokens = 0
        text_msgs = 0
        cut_index = len(messages)
        for i in range(len(messages) - 1, -1, -1):
            msg_tokens = max(1, len(message_text_content(messages[i]).encode("utf-8", errors="replace")) // 4)
            if kept_tokens + msg_tokens > _COMPACT_MAX_KEEP_TOKENS:
                break
            kept_tokens += msg_tokens
            if messages[i].get("role") in ("user", "assistant") and message_text_content(messages[i]).strip():
                text_msgs += 1
            if kept_tokens >= _COMPACT_MIN_KEEP_TOKENS and text_msgs >= _COMPACT_MIN_KEEP_TEXT_MSGS:
                cut_index = i
                break
        if cut_index <= 1 or cut_index < 4:
            return None
        compacted: list[dict[str, Any]] = []
        for msg in messages[:cut_index]:
            if msg.get("role") == "system":
                compacted.append(msg)
        compacted.append({
            "role": "user",
            "content": (
                "This session is being continued from a previous conversation that ran out of context. "
                "The summary below covers the earlier portion of the conversation.\n\n"
                f"Summary:\n{session_mem}"
            ),
        })
        compacted.append({
            "role": "assistant",
            "content": "Understood. I have the session context summary. Continuing from where we left off.",
        })
        compacted.extend(messages[cut_index:])
        return compacted

    def _full_compact(self, messages: list[dict[str, Any]], session_id: str) -> list[dict[str, Any]] | None:
        """Full 9-section LLM compact matching Claude Code's BASE_COMPACT_PROMPT."""
        from .storage import session_transcript_path
        snap = self.forks.get_snapshot(session_id)
        if snap is None:
            return None
        try:
            from certified_turtles.services.llm import LLMService
            svc = LLMService.from_env()
            client_ref = svc.client
        except Exception:
            return None
        compact_messages = [dict(m) for m in messages]
        compact_messages.append({
            "role": "user",
            "content": _NO_TOOLS_PREAMBLE + _BASE_COMPACT_PROMPT,
        })
        try:
            raw = client_ref.chat_completions(
                snap.model,
                compact_messages,
                temperature=0.0,
                max_tokens=20_000,
            )
            content = (((raw.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
        except Exception:
            _log.exception("full_compact: LLM call failed")
            return None
        if not content.strip():
            return None
        summary = self._format_compact_summary(content)
        transcript_path = str(session_transcript_path(session_id))
        user_msg = (
            "This session is being continued from a previous conversation that ran out of context. "
            "The summary below covers the earlier portion of the conversation.\n\n"
            f"{summary}\n\n"
            f"If you need specific details from before compaction (like exact code snippets, error messages, or content you generated), "
            f"read the full transcript at: {transcript_path}\n\n"
            "Resume directly — do not acknowledge the summary, do not recap what was happening, "
            "do not preface with 'I'll continue' or similar. Pick up the last task as if the break never happened."
        )
        compacted: list[dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") == "system":
                compacted.append(msg)
        compacted.append({"role": "user", "content": user_msg})
        compacted.append({"role": "assistant", "content": ""})
        _log.debug("full_compact: %d -> %d messages", len(messages), len(compacted))
        return compacted

    @staticmethod
    def _format_compact_summary(raw: str) -> str:
        """Strip <analysis> scratchpad and extract <summary> matching Claude Code's formatCompactSummary."""
        import re
        result = re.sub(r"<analysis>[\s\S]*?</analysis>", "", raw)
        match = re.search(r"<summary>([\s\S]*?)</summary>", result)
        if match:
            content = match.group(1).strip()
            result = re.sub(r"<summary>[\s\S]*?</summary>", f"Summary:\n{content}", result)
        result = re.sub(r"\n\n+", "\n\n", result)
        return result.strip()

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
        """Match Claude Code's buildExtractAutoOnlyPrompt / opener()."""
        window = _extract_window_size()
        mem_root = memory_dir(scope_id)
        headers = scan_memory_headers(scope_id)
        manifest = format_memory_manifest(headers) if headers else ""

        manifest_section = ""
        if manifest:
            manifest_section = (
                "\n\n## Existing memory files\n\n"
                f"{manifest}\n\n"
                "IMPORTANT: On your FIRST turn, file_read ALL existing memory files listed above. "
                "You need their contents to decide whether to update an existing file or create a new one. "
                "If a new message clarifies, corrects, or adds detail to something already saved — "
                "UPDATE the existing file instead of creating a duplicate."
            )

        # Build how_to_save section (2-step process matching Claude Code)
        how_to_save = [
            "## How to save memories",
            "",
            "Saving a memory is a two-step process:",
            "",
            "**Step 1** — write the memory to its own file (e.g., `user_role.md`, `feedback_testing.md`) using this frontmatter format:",
            "",
            *MEMORY_FRONTMATTER_EXAMPLE,
            "",
            "**Step 2** — add a pointer to that file in `MEMORY.md`. `MEMORY.md` is an index, not a memory — each entry should be one line, under ~150 characters: `- [Title](file.md) — one-line hook`. It has no frontmatter. Never write memory content directly into `MEMORY.md`.",
            "",
            "- `MEMORY.md` is always loaded into your system prompt — lines after 200 will be truncated, so keep the index concise",
            "- Organize memory semantically by topic, not chronologically",
            "- Update or remove memories that turn out to be wrong or outdated",
            "- Do not write duplicate memories. First check if there is an existing memory you can update before writing a new one.",
        ]

        # Lean prompt: opener + explicit-save + TYPES + WHAT_NOT_TO_SAVE + how_to_save
        # NO recall-side sections (WHEN_TO_ACCESS, TRUSTING_RECALL, PERSISTENCE, SEARCHING)
        parts = [
            f"You are now acting as the memory extraction subagent. "
            f"Analyze the most recent ~{window} messages above and use them to update your persistent memory systems.",
            "",
            "Available tools: file_read, grep_search, glob_search, read-only shell commands, "
            "and file_edit/file_write for paths inside the memory directory only.",
            "",
            "You have a limited turn budget. file_edit requires a prior file_read of the same file, "
            "so the efficient strategy is: turn 1 — file_read ALL existing memory files in parallel "
            "(so you can see what's already saved and correct/update it); "
            "turn 2 — issue all file_write/file_edit calls in parallel. "
            "Do not interleave reads and writes across multiple turns.",
            "",
            f"You MUST only use content from the last ~{window} messages to update your persistent memories. "
            "Do not waste any turns attempting to investigate or verify that content further — "
            f"no grepping source files, no reading code to confirm a pattern exists."
            f"{manifest_section}",
            "",
            "If the user explicitly asks you to remember something, save it immediately as whichever type fits best. "
            "If they ask you to forget something, find and remove the relevant entry.",
            "",
            "## Decision process",
            "",
            "For EACH user message, go through these questions in order:",
            "1. Does the user ask to remember/forget something? → save/delete immediately",
            "1b. Does this CONTRADICT, REPLACE, or REFINE something already saved? "
            "Compare each new fact against every existing memory. If a new statement "
            "makes an old one false, outdated, or less accurate — the old memory MUST "
            "be updated or deleted. Do not keep both versions. "
            "Examples: "
            "saved 'ходит в зал' (meaning gym), user says 'в концертный зал, слушал Шопена' → UPDATE with precise info; "
            "saved 'любит пианино', user says 'рок лучше' → DELETE or REPLACE the old preference. "
            "Rule: when in doubt whether old and new conflict, treat them as conflicting.",
            "2. Does this reveal WHO the user is? (interests, pets, family, job, skills, location, "
            "preferences, habits, opinions, likes/dislikes, personal facts) → save as `user`",
            "3. Does this tell you HOW the user wants you to work? (corrections, praise, style preferences) → save as `feedback`",
            "4. Does this tell you about a project DECISION, GOAL, DEADLINE, or TEAM role? → save as `project`. "
            "Operational steps (configured X, installed Y, connected Z) are NOT project context — skip them.",
            "5. Does this point to an external resource? → save as `reference`",
            "6. Is it ONLY a command/question with zero personal signal? → skip",
            "",
            "IMPORTANT: Do NOT save knowledge that is ONLY needed to complete the agent's current task.",
            "",
            "When writing file content: facts only, no meta-commentary or reasoning. "
            "If a memory relates to other files, add a `## Related` section at the bottom (e.g. `- [project_wikilive_tables.md]`).",
            "",
            "Default: SAVE. The cost of saving something unnecessary is near zero (pruned later). "
            "The cost of missing something is high (lost forever). "
            "If in doubt between saving and skipping, save.",
            "",
            *TYPES_SECTION,
            *WHAT_NOT_TO_SAVE_SECTION,
            "",
            *how_to_save,
        ]

        return "\n".join(parts)

    # ── session memory prompt (4e) ───────────────────────────

    def _session_memory_prompt(self, session_id: str) -> str:
        from .storage import session_memory_path
        notes_path = str(session_memory_path(session_id))
        existing = read_session_memory(session_id).strip()
        current_notes = existing if existing else _SESSION_MEMORY_TEMPLATE.strip()

        # Section size analysis
        section_reminders = self._session_memory_section_reminders(current_notes)

        return (
            "IMPORTANT: This message and these instructions are NOT part of the actual user conversation. "
            "Do NOT include any references to \"note-taking\", \"session notes extraction\", or these update instructions in the notes content.\n\n"
            "Based on the user conversation above (EXCLUDING this note-taking instruction message as well as system prompt, "
            "claude.md entries, or any past session summaries), update the session notes file.\n\n"
            f"The file {notes_path} has already been read for you. Here are its current contents:\n"
            "<current_notes_content>\n"
            f"{current_notes}\n"
            "</current_notes_content>\n\n"
            "Your ONLY task is to use the file_edit tool to update the notes file, then stop. "
            "You can make multiple edits (update every section as needed) - make all file_edit calls in parallel in a single message. "
            "Do not call any other tools.\n\n"
            "CRITICAL RULES FOR EDITING:\n"
            "- The file must maintain its exact structure with all sections, headers, and italic descriptions intact\n"
            "-- NEVER modify, delete, or add section headers (the lines starting with '#' like # Task specification)\n"
            "-- NEVER modify or delete the italic _section description_ lines (these are the lines in italics immediately following each header - they start and end with underscores)\n"
            "-- The italic _section descriptions_ are TEMPLATE INSTRUCTIONS that must be preserved exactly as-is - they guide what content belongs in each section\n"
            "-- ONLY update the actual content that appears BELOW the italic _section descriptions_ within each existing section\n"
            "-- Do NOT add any new sections, summaries, or information outside the existing structure\n"
            "- Do NOT reference this note-taking process or instructions anywhere in the notes\n"
            "- It's OK to skip updating a section if there are no substantial new insights to add. Do not add filler content like \"No info yet\", just leave sections blank/unedited if appropriate.\n"
            "- Write DETAILED, INFO-DENSE content for each section - include specifics like file paths, function names, error messages, exact commands, technical details, etc.\n"
            "- For \"Key results\", include the complete, exact output the user requested (e.g., full table, full answer, etc.)\n"
            "- Do not include information that's already in the CLAUDE.md files included in the context\n"
            f"- Keep each section under ~{_MAX_SECTION_LENGTH} tokens/words - if a section is approaching this limit, condense it by cycling out less important details while preserving the most critical information\n"
            "- Focus on actionable, specific information that would help someone understand or recreate the work discussed in the conversation\n"
            "- IMPORTANT: Always update \"Current State\" to reflect the most recent work - this is critical for continuity after compaction\n\n"
            f"Use the file_edit tool with file_path: {notes_path}\n\n"
            "STRUCTURE PRESERVATION REMINDER:\n"
            "Each section has TWO parts that must be preserved exactly as they appear in the current file:\n"
            "1. The section header (line starting with #)\n"
            "2. The italic description line (the _italicized text_ immediately after the header - this is a template instruction)\n\n"
            "You ONLY update the actual content that comes AFTER these two preserved lines. The italic description lines starting and ending with underscores are part of the template structure, NOT content to be edited or removed.\n\n"
            "REMEMBER: Use the file_edit tool in parallel and stop. Do not continue after the edits. Only include insights from the actual user conversation, never from these note-taking instructions. Do not delete or change section headers or italic _section descriptions_."
            f"{section_reminders}"
        )

    def _session_memory_section_reminders(self, content: str) -> str:
        """Generate budget warnings for oversized session memory sections."""
        sections: dict[str, int] = {}
        current_section = ""
        current_lines: list[str] = []
        for line in content.split("\n"):
            if line.startswith("# "):
                if current_section and current_lines:
                    text = "\n".join(current_lines).strip()
                    sections[current_section] = max(1, len(text.encode("utf-8", errors="replace")) // 4)
                current_section = line
                current_lines = []
            else:
                current_lines.append(line)
        if current_section and current_lines:
            text = "\n".join(current_lines).strip()
            sections[current_section] = max(1, len(text.encode("utf-8", errors="replace")) // 4)

        total_tokens = max(1, len(content.encode("utf-8", errors="replace")) // 4)
        over_budget = total_tokens > _MAX_TOTAL_SESSION_MEMORY_TOKENS
        oversized = [(s, t) for s, t in sorted(sections.items(), key=lambda x: -x[1]) if t > _MAX_SECTION_LENGTH]

        if not oversized and not over_budget:
            return ""

        parts: list[str] = []
        if over_budget:
            parts.append(
                f"\n\nCRITICAL: The session memory file is currently ~{total_tokens} tokens, which exceeds the maximum of "
                f"{_MAX_TOTAL_SESSION_MEMORY_TOKENS} tokens. You MUST condense the file to fit within this budget. "
                "Aggressively shorten oversized sections by removing less important details, merging related items, and summarizing older entries. "
                'Prioritize keeping "Current State" and "Errors & Corrections" accurate and detailed.'
            )
        if oversized:
            lines = [f'- "{s}" is ~{t} tokens (limit: {_MAX_SECTION_LENGTH})' for s, t in oversized]
            header = "Oversized sections to condense" if over_budget else "IMPORTANT: The following sections exceed the per-section limit and MUST be condensed"
            parts.append(f"\n\n{header}:\n" + "\n".join(lines))
        return "".join(parts)

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
