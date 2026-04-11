from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from certified_turtles.agent_debug_log import agent_logger
from certified_turtles.agents.json_agent_protocol import message_text_content, parse_agent_response
from certified_turtles.mws_gpt.client import MWSGPTClient

from .forking import CacheSafeSnapshot, ForkRuntime
from .prompting import build_memory_prompt
from .storage import (
    append_transcript_event,
    ensure_session_meta,
    list_scope_sessions,
    memory_dir,
    memory_index_path,
    read_json,
    rebuild_memory_index,
    scope_meta_path,
)


_log = agent_logger("memory_runtime")
_RUNTIME: "ClaudeLikeMemoryRuntime | None" = None


class ClaudeLikeMemoryRuntime:
    def __init__(self):
        self.forks = ForkRuntime()
        self._extract_in_progress: set[str] = set()
        self._extract_trailing: dict[str, tuple[str, str, str]] = {}
        self._session_updates: dict[str, float] = {}

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
            scope_id=scope_id,
            session_id=session_id,
            user_query=query,
        )
        work = [dict(m) for m in messages]
        if prompt:
            work.insert(0, {"role": "system", "content": prompt})
        self.forks.save_snapshot(
            CacheSafeSnapshot(
                model=model,
                scope_id=scope_id,
                session_id=session_id,
                messages=[dict(m) for m in work],
                saved_at=time.time(),
            )
        )
        return work

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
        self._maybe_launch_auto_dream(client, session_id=session_id, scope_id=scope_id)

    def _append_transcript(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        for msg in messages[-12:]:
            append_transcript_event(
                session_id,
                {
                    "timestamp": time.time(),
                    "role": msg.get("role"),
                    "content": message_text_content(msg),
                    "kind": "message",
                },
            )

    def _note_session_turn(self, session_id: str) -> None:
        self._session_updates.setdefault(session_id, 0.0)

    def _should_update_session_memory(self, session_id: str, messages: list[dict[str, Any]]) -> bool:
        latest = "\n".join(message_text_content(m) for m in messages[-6:])
        if len(latest) < 600:
            return False
        now = time.time()
        last = self._session_updates.get(session_id, 0.0)
        if now - last < 20:
            return False
        self._session_updates[session_id] = now
        return True

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
                await asyncio.to_thread(
                    self.forks.run_named_subagent,
                    client,
                    session_id=session_id,
                    agent_id="memory_extractor",
                    prompt=prompt,
                )
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
            await asyncio.to_thread(
                self.forks.run_named_subagent,
                client,
                session_id=session_id,
                agent_id=agent_id,
                prompt=prompt,
            )

        try:
            asyncio.get_running_loop().create_task(runner())
        except RuntimeError:
            _log.debug("no running loop for post hook agent=%s session=%s", agent_id, session_id)

    def _extractor_prompt(self, scope_id: str, messages: list[dict[str, Any]]) -> str:
        preview = []
        for item in messages[-8:]:
            role = item.get("role", "unknown")
            content = message_text_content(item)
            if content.strip():
                preview.append(f"<<{role}>>\n{content}")
        return (
            f"Analyze the most recent messages and update persistent memory files in {memory_dir(scope_id)}.\n"
            "Use only durable facts from the recent conversation. Do not inspect code to derive facts. Update MEMORY.md when needed.\n\n"
            + "\n\n".join(preview)
        )

    def _session_memory_prompt(self, session_id: str) -> str:
        return (
            "Update the session memory file with the current state of work. "
            f"Keep it concise and structured. Target file: {session_id} session memory."
        )

    def _maybe_launch_auto_dream(self, client: MWSGPTClient, *, session_id: str, scope_id: str) -> None:
        meta_path = scope_meta_path(scope_id)
        meta = read_json(meta_path) or {}
        now = time.time()
        if meta.get("auto_dream_lock_until", 0) > now:
            return
        if (now - float(meta.get("last_auto_dream_at", 0))) < 86400:
            return
        if len(list_scope_sessions(scope_id)) < 5:
            return
        meta["last_auto_dream_at"] = now
        meta["auto_dream_lock_until"] = now + 600
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        self._launch_post_hook(
            client,
            session_id=session_id,
            scope_id=scope_id,
            agent_id="auto_dream",
            prompt=(
                f"Consolidate the memory directory {memory_dir(scope_id)}. "
                f"Rebuild {memory_index_path(scope_id)}. Merge duplicates, improve descriptions, prune stale items."
            ),
        )

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


def runtime_from_env() -> ClaudeLikeMemoryRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = ClaudeLikeMemoryRuntime()
    return _RUNTIME
