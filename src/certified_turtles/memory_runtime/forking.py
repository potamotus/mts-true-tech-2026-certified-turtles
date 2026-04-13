from __future__ import annotations

from dataclasses import dataclass
import json
import threading
import time
from typing import Any

from certified_turtles.agent_debug_log import agent_logger, debug_clip
from certified_turtles.agents.loop import run_agent_chat
from certified_turtles.agents.registry import get_subagent
from certified_turtles.mws_gpt.client import MWSGPTClient
from certified_turtles.memory_runtime.file_state import clone_file_state_namespace
from certified_turtles.memory_runtime.request_context import RequestContext
from certified_turtles.tools.registry import openai_tools_for_names


_log = agent_logger("fork")


@dataclass(frozen=True)
class CacheSafeSnapshot:
    model: str
    scope_id: str
    session_id: str
    file_state_namespace: str
    messages: list[dict[str, Any]]
    saved_at: float


class ForkRuntime:
    def __init__(self):
        self._lock = threading.Lock()
        self._snapshots: dict[str, CacheSafeSnapshot] = {}

    def save_snapshot(self, snapshot: CacheSafeSnapshot) -> None:
        with self._lock:
            self._snapshots[snapshot.session_id] = snapshot

    def get_snapshot(self, session_id: str) -> CacheSafeSnapshot | None:
        with self._lock:
            return self._snapshots.get(session_id)

    def run_named_subagent(
        self,
        client: MWSGPTClient,
        *,
        session_id: str,
        agent_id: str,
        prompt: str,
        max_agent_tokens: int | None = None,
    ) -> dict[str, Any] | None:
        snap = self.get_snapshot(session_id)
        spec = get_subagent(agent_id)
        if snap is None or spec is None:
            return None
        tool_list = openai_tools_for_names(spec.tool_names)
        child_namespace = f"{snap.file_state_namespace}::agent::{agent_id}"
        clone_file_state_namespace(snap.file_state_namespace, child_namespace)
        work = [
            *snap.messages,
            {"role": "system", "content": spec.system_prompt},
            {"role": "user", "content": prompt},
        ]
        _log.debug(
            "fork start session=%s agent=%s messages=%s prompt=\n%s",
            session_id,
            agent_id,
            len(work),
            debug_clip(prompt),
        )
        out = run_agent_chat(
            client,
            snap.model,
            work,
            tools=tool_list,
            request_context=RequestContext(
                session_id=snap.session_id,
                scope_id=snap.scope_id,
                file_state_namespace=child_namespace,
            ),
        )
        _log.debug(
            "fork end session=%s agent=%s truncated=%s completion=\n%s",
            session_id,
            agent_id,
            out.get("truncated"),
            debug_clip(json.dumps(out.get("completion"), ensure_ascii=False)),
        )
        return out
