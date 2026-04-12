"""
Functional correctness tests for untested memory runtime flows.

Covers auto-dream, extraction triggers, staleness warnings, selector fallback,
overwrite semantics, compact threshold parsing, session memory budget, and
concurrent extractor queuing.

Run: pytest tests/test_memory_functional_gaps.py -v --tb=short
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CT_CLAUDE_HOME", tempfile.mkdtemp(prefix="ct_funcgap_"))

from certified_turtles.memory_runtime.manager import (
    ClaudeLikeMemoryRuntime,
    _DREAM_SCAN_THROTTLE_SEC,
    _MAX_SECTION_LENGTH,
    _MAX_TOTAL_SESSION_MEMORY_TOKENS,
    _compact_threshold,
)
from certified_turtles.memory_runtime.prompting import _memory_age_warning
from certified_turtles.memory_runtime.selector import fallback_select
from certified_turtles.memory_runtime.storage import (
    MemoryHeader,
    _last_rebuild,
    ensure_session_meta,
    list_scope_sessions,
    memory_dir,
    parse_frontmatter,
    read_frontmatter,
    session_meta_path,
    write_json,
    read_json,
    write_memory_file,
    write_session_memory,
    read_session_memory,
)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path):
    """Each test gets its own CT_CLAUDE_HOME."""
    old = os.environ.get("CT_CLAUDE_HOME")
    root = str(tmp_path / "claude_home")
    os.environ["CT_CLAUDE_HOME"] = root
    _last_rebuild.clear()
    yield tmp_path
    if old is None:
        os.environ.pop("CT_CLAUDE_HOME", None)
    else:
        os.environ["CT_CLAUDE_HOME"] = old


SCOPE = "funcgap-scope"


def _fresh_session() -> str:
    return f"funcgap-session-{os.urandom(6).hex()}"


def _write_quick_memory(scope_id: str, name: str, body: str = "content", **kw) -> Path:
    return write_memory_file(
        scope_id,
        name=name,
        description=kw.get("description", "test"),
        type_=kw.get("type_", "project"),
        body=body,
        filename=kw.get("filename"),
    )


def _make_sessions_for_scope(scope_id: str, count: int) -> list[str]:
    """Create count session dirs with meta.json pointing to scope_id."""
    sessions = []
    for i in range(count):
        sid = f"dream-session-{i}-{os.urandom(4).hex()}"
        ensure_session_meta(sid, scope_id=scope_id)
        sessions.append(sid)
    return sessions


def _mock_client() -> MagicMock:
    client = MagicMock()
    client.chat_completions.return_value = {
        "choices": [{"message": {"content": json.dumps({"selected_memories": []})}}]
    }
    return client


# ═══════════════════════════════════════════════════════════════
# 1-3. AUTO-DREAM CONSOLIDATION
# ═══════════════════════════════════════════════════════════════


class TestAutoDream:
    """Tests for _maybe_launch_auto_dream gating logic."""

    def test_auto_dream_requires_5_sessions(self):
        """Auto-dream does not fire with fewer than 5 sessions."""
        rt = ClaudeLikeMemoryRuntime()
        client = _mock_client()
        session = _fresh_session()
        ensure_session_meta(session, scope_id=SCOPE)

        # Create only 3 sessions
        _make_sessions_for_scope(SCOPE, 3)

        rt._maybe_launch_auto_dream(client, session_id=session, scope_id=SCOPE)

        # _launch_post_hook should NOT have been called (no auto-dream)
        client.chat_completions.assert_not_called()

    def test_auto_dream_triggers_with_5_sessions(self):
        """Auto-dream fires when >= 5 sessions exist for the scope."""
        rt = ClaudeLikeMemoryRuntime()
        client = _mock_client()
        session = _fresh_session()
        ensure_session_meta(session, scope_id=SCOPE)

        _make_sessions_for_scope(SCOPE, 5)

        with patch.object(rt, "_launch_post_hook") as mock_hook:
            rt._maybe_launch_auto_dream(client, session_id=session, scope_id=SCOPE)
            mock_hook.assert_called_once()
            call_kwargs = mock_hook.call_args[1]
            assert call_kwargs["agent_id"] == "auto_dream"

    def test_auto_dream_throttle_prevents_rapid_rescans(self):
        """Scan throttle (_DREAM_SCAN_THROTTLE_SEC) prevents rapid re-triggers."""
        rt = ClaudeLikeMemoryRuntime()
        client = _mock_client()
        session = _fresh_session()
        ensure_session_meta(session, scope_id=SCOPE)

        _make_sessions_for_scope(SCOPE, 5)

        with patch.object(rt, "_launch_post_hook"):
            # First call triggers
            rt._maybe_launch_auto_dream(client, session_id=session, scope_id=SCOPE)

        # Second call within throttle window should NOT trigger
        with patch.object(rt, "_launch_post_hook") as mock_hook:
            rt._maybe_launch_auto_dream(client, session_id=session, scope_id=SCOPE)
            mock_hook.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# 4-5. EXTRACTOR TRIGGER LOGIC
# ═══════════════════════════════════════════════════════════════


class TestExtractorTrigger:
    """Tests for extract hook firing vs skip logic based on _main_agent_wrote_memory."""

    def _agent_wrote_memory_msg(self, scope_id: str) -> list[dict]:
        """Messages where the agent explicitly wrote to memory dir."""
        mem_root = str(memory_dir(scope_id))
        return [{"role": "assistant", "content": json.dumps({
            "assistant_markdown": "saving memory",
            "calls": [{"name": "file_write", "arguments": {"file_path": f"{mem_root}/note.md"}}],
        })}]

    def _plain_messages(self) -> list[dict]:
        """Messages with no memory writes."""
        return [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]

    def test_extractor_fires_when_agent_did_not_write_memory(self):
        """Extract hook launches when main agent didn't write to memory."""
        rt = ClaudeLikeMemoryRuntime()
        msgs = self._plain_messages()

        with patch.object(rt, "_launch_extract_hook") as mock_extract, \
             patch.object(rt, "_should_update_session_memory", return_value=False), \
             patch.object(rt, "_maybe_launch_auto_dream"), \
             patch.object(rt, "_append_transcript"), \
             patch.object(rt, "forks"):
            rt.after_response(
                _mock_client(),
                model="test",
                prepared_messages=msgs,
                final_messages=msgs,
                session_id=_fresh_session(),
                scope_id=SCOPE,
            )
            mock_extract.assert_called_once()

    def test_extractor_skipped_when_agent_wrote_memory(self):
        """Extract hook is skipped when main agent already wrote to memory."""
        rt = ClaudeLikeMemoryRuntime()
        session = _fresh_session()
        ensure_session_meta(session, scope_id=SCOPE)
        msgs = self._agent_wrote_memory_msg(SCOPE)

        with patch.object(rt, "_launch_extract_hook") as mock_extract, \
             patch.object(rt, "_should_update_session_memory", return_value=False), \
             patch.object(rt, "_maybe_launch_auto_dream"), \
             patch.object(rt, "_append_transcript"), \
             patch.object(rt, "forks"):
            rt.after_response(
                _mock_client(),
                model="test",
                prepared_messages=msgs,
                final_messages=msgs,
                session_id=session,
                scope_id=SCOPE,
            )
            mock_extract.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# 6-7. STALENESS WARNINGS
# ═══════════════════════════════════════════════════════════════


class TestStalenessWarning:
    """_memory_age_warning based on updated timestamp."""

    def test_stale_memory_gets_warning(self):
        """Memories >1 day old produce a staleness warning."""
        old_stamp = "2020-01-01T00:00:00Z"
        result = _memory_age_warning(old_stamp)
        assert "days old" in result
        assert "Verify" in result

    def test_fresh_memory_no_warning(self):
        """Memories <=1 day old produce no warning."""
        from datetime import datetime, timezone
        now_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = _memory_age_warning(now_stamp)
        assert result == ""


# ═══════════════════════════════════════════════════════════════
# 8-9. SELECTOR LLM FAILURE → FALLBACK
# ═══════════════════════════════════════════════════════════════


class TestSelectorFallback:
    """LLM selector error → keyword fallback still returns results."""

    def _sample_headers(self) -> list[MemoryHeader]:
        return [
            MemoryHeader("python-setup.md", "Python Setup", "how to set up python env", "project", time.time()),
            MemoryHeader("api-keys.md", "API Keys", "api key management guide", "reference", time.time()),
            MemoryHeader("debugging.md", "Debugging Tips", "python debugging techniques", "feedback", time.time()),
        ]

    def test_selector_llm_failure_returns_empty(self):
        """When LLM raises an exception, return empty list (match Claude Code)."""
        from certified_turtles.memory_runtime.selector import select_relevant_memories

        client = MagicMock()
        client.chat_completions.side_effect = RuntimeError("LLM is down")

        result = select_relevant_memories(
            client,
            model="test",
            query="python setup",
            headers=self._sample_headers(),
        )
        assert result == []

    def test_selector_invalid_json_returns_empty(self):
        """When LLM returns invalid JSON, return empty list (match Claude Code)."""
        from certified_turtles.memory_runtime.selector import select_relevant_memories

        client = MagicMock()
        client.chat_completions.return_value = {
            "choices": [{"message": {"content": "not json at all"}}]
        }

        result = select_relevant_memories(
            client,
            model="test",
            query="debugging",
            headers=self._sample_headers(),
        )
        assert result == []


# ═══════════════════════════════════════════════════════════════
# 10. OVERWRITE PRESERVES CREATED TIMESTAMP
# ═══════════════════════════════════════════════════════════════


class TestOverwritePreservesCreated:
    """Overwriting same filename preserves created, updates updated and index."""

    def test_overwrite_memory_preserves_created_updates_index(self):
        """Second write to same filename keeps original 'created' but updates 'updated'."""
        path1 = _write_quick_memory(SCOPE, "persist", body="v1", filename="persist.md")
        fm1 = read_frontmatter(path1)
        created1 = fm1["created"]

        time.sleep(0.05)  # ensure timestamp difference
        _last_rebuild.clear()  # allow rebuild

        path2 = _write_quick_memory(SCOPE, "persist", body="v2", filename="persist.md")
        fm2 = read_frontmatter(path2)

        assert fm2["created"] == created1  # preserved
        assert fm2["updated"] != created1  # updated changed

        # Index was rebuilt
        index = (memory_dir(SCOPE) / "MEMORY.md").read_text(encoding="utf-8")
        assert "persist.md" in index


# ═══════════════════════════════════════════════════════════════
# 11-13. COMPACT THRESHOLD PARSING
# ═══════════════════════════════════════════════════════════════


class TestCompactThreshold:
    """CT_COMPACT_THRESHOLD env var: reads value, clamps to 50k minimum, handles invalid."""

    def test_compact_threshold_reads_env(self):
        """Reads CT_COMPACT_THRESHOLD from environment."""
        with patch.dict(os.environ, {"CT_COMPACT_THRESHOLD": "200000"}):
            assert _compact_threshold() == 200_000

    def test_compact_threshold_clamps_to_50k_minimum(self):
        """Values below 50000 are clamped to 50000."""
        with patch.dict(os.environ, {"CT_COMPACT_THRESHOLD": "10000"}):
            assert _compact_threshold() == 50_000

    def test_compact_threshold_handles_invalid_input(self):
        """Non-numeric input returns default 150000."""
        with patch.dict(os.environ, {"CT_COMPACT_THRESHOLD": "garbage"}):
            assert _compact_threshold() == 150_000


# ═══════════════════════════════════════════════════════════════
# 14. SESSION MEMORY SECTION OVER-BUDGET WARNING
# ═══════════════════════════════════════════════════════════════


class TestSessionMemoryBudget:
    """Oversized session memory section generates warning."""

    def test_session_memory_section_over_budget_warning(self):
        """Sections exceeding _MAX_SECTION_LENGTH produce budget warnings."""
        rt = ClaudeLikeMemoryRuntime()
        # Create a section with content vastly exceeding the limit
        huge_section = "x " * (_MAX_SECTION_LENGTH * 5)
        content = f"# Session Title\n_desc_\nShort title\n\n# Current State\n_desc_\n{huge_section}"

        result = rt._session_memory_section_reminders(content)
        assert "MUST be condensed" in result or "Oversized" in result


# ═══════════════════════════════════════════════════════════════
# 15. CONCURRENT EXTRACTORS QUEUED
# ═══════════════════════════════════════════════════════════════


class TestConcurrentExtractors:
    """Second extraction for same session is queued via _extract_trailing."""

    def test_concurrent_extractors_queued_not_duplicated(self):
        """If extraction is in progress, second call queues via _extract_trailing."""
        rt = ClaudeLikeMemoryRuntime()
        session = _fresh_session()

        # Simulate first extraction already in progress
        rt._extract_in_progress.add(session)

        # Second call should queue, not start another
        rt._launch_extract_hook(
            _mock_client(),
            session_id=session,
            scope_id=SCOPE,
            prompt="second extraction prompt",
        )

        assert session in rt._extract_trailing
        queued = rt._extract_trailing[session]
        assert queued[0] == SCOPE  # scope_id
        assert queued[2] == "second extraction prompt"  # prompt

        # Clean up
        rt._extract_in_progress.discard(session)


# ═══════════════════════════════════════════════════════════════
# 16-17. SESSION MEMORY UPDATE TOKEN THRESHOLD
# ═══════════════════════════════════════════════════════════════


class TestSessionMemoryTokenThreshold:
    """Session memory update gated by 10k token initialization threshold."""

    def _make_messages(self, total_bytes: int) -> list[dict]:
        """Create messages totaling approximately total_bytes / 4 tokens."""
        content = "x" * total_bytes
        return [{"role": "user", "content": content}]

    def test_session_memory_not_updated_below_10k_tokens(self):
        """Session memory update rejected when < 10k tokens (uninitialized)."""
        rt = ClaudeLikeMemoryRuntime()
        session = _fresh_session()
        ensure_session_meta(session, scope_id=SCOPE)

        # ~5k tokens = 20k bytes (1 token ≈ 4 bytes)
        msgs = self._make_messages(20_000)
        assert rt._should_update_session_memory(session, msgs) is False

    def test_session_memory_updated_after_10k_tokens(self):
        """Session memory update allowed when >= 10k tokens and delta >= 5k."""
        rt = ClaudeLikeMemoryRuntime()
        session = _fresh_session()
        ensure_session_meta(session, scope_id=SCOPE)

        # ~12k tokens = 48k bytes, and delta from 0 is > 5k
        msgs = self._make_messages(48_000)
        # Also need enough tool calls to pass the guard
        # Add assistant messages with tool calls to satisfy the recent_tool_calls >= 3 check
        for _ in range(4):
            msgs.append({"role": "assistant", "content": json.dumps({
                "assistant_markdown": "doing work",
                "calls": [{"name": "file_read", "arguments": {"file_path": "/tmp/x"}}],
            })})

        result = rt._should_update_session_memory(session, msgs)
        assert result is True
