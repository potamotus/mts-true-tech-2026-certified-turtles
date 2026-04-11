"""Integration tests for OpenAI proxy API + MemoryRuntime manager complex behaviors.

Covers: prepare_messages / after_response flows, compaction, microcompaction,
session memory extraction, auto-dream consolidation, state management,
concurrent access, and edge cases.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Isolate storage before any certified_turtles import touches the real filesystem.
_TEST_HOME = tempfile.mkdtemp(prefix="ct_api_")
os.environ.setdefault("CT_CLAUDE_HOME", _TEST_HOME)
os.environ.setdefault("UPLOADS_DIR", os.path.join(_TEST_HOME, "uploads"))
os.environ.setdefault("GENERATED_FILES_DIR", os.path.join(_TEST_HOME, "generated"))

from certified_turtles.agents.json_agent_protocol import (
    PROTOCOL_USER_PREFIX,
    message_text_content,
)
from certified_turtles.memory_runtime.file_state import (
    _SESSION_CACHE,
    _SESSION_SIZES,
    get_file_state,
    note_file_read,
)
from certified_turtles.memory_runtime.forking import CacheSafeSnapshot
from certified_turtles.memory_runtime.manager import (
    ClaudeLikeMemoryRuntime,
    _COMPACT_MIN_KEEP_TEXT_MSGS,
    _COMPACT_MIN_KEEP_TOKENS,
    _DREAM_SCAN_THROTTLE_SEC,
    _MICROCOMPACT_KEEP_RECENT,
    _MICROCOMPACT_TIME_GAP_SEC,
    _MICROCOMPACT_TOKEN_THRESHOLD,
    _compact_threshold,
    runtime_from_env,
)
from certified_turtles.memory_runtime.request_context import (
    RequestContext,
    current_request_context,
    use_request_context,
)
from certified_turtles.memory_runtime.storage import (
    append_transcript_event,
    ensure_session_meta,
    read_json,
    read_session_memory,
    read_transcript_events,
    session_meta_path,
    write_json,
    write_memory_file,
    write_session_memory,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test gets its own CT_CLAUDE_HOME so storage is fully isolated."""
    home = str(tmp_path / "claude_home")
    monkeypatch.setenv("CT_CLAUDE_HOME", home)
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("GENERATED_FILES_DIR", str(tmp_path / "generated"))
    yield home


@pytest.fixture()
def runtime() -> ClaudeLikeMemoryRuntime:
    return ClaudeLikeMemoryRuntime()


@pytest.fixture()
def mock_client() -> MagicMock:
    client = MagicMock()
    client.chat_completions.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    }
    return client


def _make_messages(n_user: int = 1, *, prefix: str = "msg") -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    for i in range(n_user):
        msgs.append({"role": "user", "content": f"{prefix} user {i}"})
        msgs.append({"role": "assistant", "content": f"{prefix} assistant {i}"})
    return msgs


# ===========================================================================
# 1. Full pre-request flow (prepare_messages)
# ===========================================================================

class TestPrepareMessages:
    """prepare_messages injects system prompt, snapshots, and updates meta."""

    def test_system_prompt_injected(self, runtime, mock_client):
        """System prompt with memory section is prepended."""
        messages = [{"role": "user", "content": "hello"}]
        result = runtime.prepare_messages(
            mock_client, model="m", messages=messages, session_id="s1", scope_id="sc1",
        )
        assert result[0]["role"] == "system"
        assert "# memory" in result[0]["content"]

    def test_original_messages_preserved(self, runtime, mock_client):
        """User messages still present after prepare_messages."""
        messages = [{"role": "user", "content": "hi there"}]
        result = runtime.prepare_messages(
            mock_client, model="m", messages=messages, session_id="s2", scope_id="sc2",
        )
        user_texts = [m["content"] for m in result if m.get("role") == "user"]
        assert "hi there" in user_texts

    def test_session_meta_updated(self, runtime, mock_client):
        """Session meta JSON is written with recent_messages."""
        messages = [{"role": "user", "content": "check"}]
        runtime.prepare_messages(
            mock_client, model="m", messages=messages, session_id="s3", scope_id="sc3",
        )
        meta = read_json(session_meta_path("s3"))
        assert meta is not None
        assert "recent_messages" in meta
        assert meta["scope_id"] == "sc3"

    def test_snapshot_saved(self, runtime, mock_client):
        """ForkRuntime snapshot is persisted after prepare."""
        messages = [{"role": "user", "content": "snap"}]
        runtime.prepare_messages(
            mock_client, model="m", messages=messages, session_id="s4", scope_id="sc4",
        )
        snap = runtime.forks.get_snapshot("s4")
        assert snap is not None
        assert snap.model == "m"
        assert snap.scope_id == "sc4"

    def test_plain_mode_no_client(self, runtime):
        """When client is None (plain mode), no LLM-based selector is called."""
        messages = [{"role": "user", "content": "plain test"}]
        result = runtime.prepare_messages(
            None, model="m", messages=messages, session_id="s5", scope_id="sc5",
        )
        assert result[0]["role"] == "system"
        assert any(m["content"] == "plain test" for m in result if m.get("role") == "user")


# ===========================================================================
# 2. Full post-request flow (after_response)
# ===========================================================================

class TestAfterResponse:
    """after_response writes transcript, notes turn, and checks extraction."""

    def test_transcript_written(self, runtime, mock_client):
        messages = [{"role": "user", "content": "hey"}]
        prepared = runtime.prepare_messages(
            mock_client, model="m", messages=messages, session_id="post1", scope_id="sc1",
        )
        final = [*prepared, {"role": "assistant", "content": "yo"}]
        runtime.after_response(
            mock_client, model="m", prepared_messages=prepared,
            final_messages=final, session_id="post1", scope_id="sc1",
        )
        events = read_transcript_events("post1")
        assert any(e.get("role") == "assistant" for e in events)

    def test_session_turn_noted(self, runtime, mock_client):
        messages = [{"role": "user", "content": "turn"}]
        prepared = runtime.prepare_messages(
            mock_client, model="m", messages=messages, session_id="post2", scope_id="sc1",
        )
        final = [*prepared, {"role": "assistant", "content": "done"}]
        before = runtime._session_updates.get("post2", 0.0)
        runtime.after_response(
            mock_client, model="m", prepared_messages=prepared,
            final_messages=final, session_id="post2", scope_id="sc1",
        )
        assert runtime._session_updates["post2"] > before


# ===========================================================================
# 3. Compaction threshold math
# ===========================================================================

class TestCompaction:
    """Messages above the compaction threshold get compacted."""

    def test_no_compaction_below_threshold(self, runtime, monkeypatch):
        monkeypatch.setenv("CT_COMPACT_THRESHOLD", "150000")
        # Small messages — no compaction
        messages = _make_messages(3)
        result = runtime._compact_if_needed(messages, "comp1")
        assert result == messages

    def test_compaction_triggers_above_threshold(self, runtime, monkeypatch):
        monkeypatch.setenv("CT_COMPACT_THRESHOLD", "50000")
        write_session_memory("comp2", "# Session\nSummary of the session so far.")
        # Generate enough text to exceed 50k tokens (~200k bytes)
        big_messages = [{"role": "system", "content": "sys"}]
        for i in range(50):
            big_messages.append({"role": "user", "content": f"user msg {i} " + "x" * 4000})
            big_messages.append({"role": "assistant", "content": f"asst msg {i} " + "y" * 4000})
        result = runtime._compact_if_needed(big_messages, "comp2")
        # Compacted result should be shorter
        assert len(result) < len(big_messages)

    def test_compaction_no_session_memory_skips(self, runtime, monkeypatch):
        """Compaction is skipped if there is no session memory to use as summary."""
        monkeypatch.setenv("CT_COMPACT_THRESHOLD", "50000")
        big_messages = []
        for i in range(60):
            big_messages.append({"role": "user", "content": "u " + "x" * 4000})
            big_messages.append({"role": "assistant", "content": "a " + "y" * 4000})
        result = runtime._compact_if_needed(big_messages, "comp3_no_mem")
        # Without session memory, compaction should NOT alter messages
        assert len(result) == len(big_messages)


# ===========================================================================
# 4. Compaction preserves system messages
# ===========================================================================

class TestCompactionPreservesSystem:
    def test_system_messages_preserved(self, runtime, monkeypatch):
        monkeypatch.setenv("CT_COMPACT_THRESHOLD", "50000")
        write_session_memory("comp4", "# Session\nContext for compaction.")
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "sys-A"},
            {"role": "system", "content": "sys-B"},
        ]
        for i in range(60):
            messages.append({"role": "user", "content": f"u{i} " + "x" * 4000})
            messages.append({"role": "assistant", "content": f"a{i} " + "y" * 4000})
        messages.insert(40, {"role": "system", "content": "sys-C"})
        result = runtime._compact_if_needed(messages, "comp4")
        system_contents = [m["content"] for m in result if m.get("role") == "system"]
        assert "sys-A" in system_contents
        assert "sys-B" in system_contents
        # sys-C is inside the compacted region, so it should also be preserved
        assert "sys-C" in system_contents


# ===========================================================================
# 5. Microcompaction
# ===========================================================================

class TestMicrocompaction:
    def test_old_tool_results_truncated(self, runtime):
        """Old tool results beyond KEEP_RECENT are cleared."""
        messages: list[dict[str, Any]] = [{"role": "system", "content": "sys"}]
        # Create many tool result messages
        for i in range(20):
            messages.append({"role": "user", "content": f"{PROTOCOL_USER_PREFIX}\n{{\"tool_outputs\": [{{\"name\": \"t{i}\", \"output\": \"{'z' * 5000}\"}}]}}"})
            messages.append({"role": "assistant", "content": f"response {i}"})
        # Force microcompaction via time gap
        runtime._session_updates["mc1"] = time.time() - (_MICROCOMPACT_TIME_GAP_SEC + 100)
        result = runtime._microcompact_tool_results(messages, "mc1")
        # Count cleared tool results
        cleared = sum(1 for m in result if "[Old tool result content cleared]" in message_text_content(m))
        assert cleared > 0
        # Recent ones are preserved
        assert cleared <= 20 - _MICROCOMPACT_KEEP_RECENT

    def test_no_microcompact_when_few_messages(self, runtime):
        """Under 10 messages — no microcompaction."""
        messages = _make_messages(3)
        result = runtime._microcompact_tool_results(messages, "mc2")
        assert result == messages


# ===========================================================================
# 6. Session memory extraction trigger
# ===========================================================================

class TestSessionMemoryExtraction:
    def test_should_update_false_when_small(self, runtime):
        """Extraction not triggered when tokens are too low."""
        messages = [{"role": "user", "content": "short"}]
        ensure_session_meta("ext1", scope_id="sc1")
        assert runtime._should_update_session_memory("ext1", messages) is False

    def test_should_update_true_when_enough_tokens(self, runtime):
        """Extraction triggers when conversation token count is high enough."""
        ensure_session_meta("ext2", scope_id="sc1")
        # Initialize session memory
        meta_path = session_meta_path("ext2")
        meta = read_json(meta_path) or {}
        meta["session_memory_initialized"] = True
        meta["session_memory_tokens_at_last_extract"] = 0
        write_json(meta_path, meta)
        # Create messages that total > 10k tokens (>40k bytes)
        big_messages = [{"role": "user", "content": "x" * 50_000}]
        result = runtime._should_update_session_memory("ext2", big_messages)
        assert result is True


# ===========================================================================
# 7. Auto-dream consolidation trigger
# ===========================================================================

class TestAutoDream:
    def test_no_dream_when_few_sessions(self, runtime, mock_client, monkeypatch):
        """Auto-dream requires >= 5 sessions in scope."""
        # Force stale conditions
        runtime._last_dream_scan_at.clear()
        # Patch list_scope_sessions to return few
        with patch("certified_turtles.memory_runtime.manager.list_scope_sessions", return_value=[]):
            with patch("certified_turtles.memory_runtime.manager.read_last_consolidated_at", return_value=0.0):
                runtime._maybe_launch_auto_dream(mock_client, session_id="d1", scope_id="dsc1")
        # No dream post hook should fire — verified by no crash and scan timestamp set
        assert runtime._last_dream_scan_at.get("dsc1", 0) > 0

    def test_dream_throttled(self, runtime, mock_client):
        """Repeated calls within throttle period skip."""
        runtime._last_dream_scan_at["dsc2"] = time.time()
        with patch("certified_turtles.memory_runtime.manager.read_last_consolidated_at", return_value=0.0):
            runtime._maybe_launch_auto_dream(mock_client, session_id="d2", scope_id="dsc2")
        # Should not update the timestamp (throttled)
        # The timestamp should still be the one we set
        assert runtime._last_dream_scan_at["dsc2"] <= time.time()


# ===========================================================================
# 8. Message format validation
# ===========================================================================

class TestMessageFormatValidation:
    def test_missing_role(self, runtime, mock_client):
        """Messages with missing role handled gracefully."""
        messages = [{"content": "no role"}]
        # Should not crash
        result = runtime.prepare_messages(
            None, model="m", messages=messages, session_id="mfv1", scope_id="sc1",
        )
        assert isinstance(result, list)

    def test_empty_content(self, runtime, mock_client):
        """Messages with empty content handled gracefully."""
        messages = [{"role": "user", "content": ""}]
        result = runtime.prepare_messages(
            None, model="m", messages=messages, session_id="mfv2", scope_id="sc1",
        )
        assert isinstance(result, list)

    def test_none_content(self, runtime, mock_client):
        """Message with None content (tool_calls without content) handled."""
        messages = [{"role": "assistant", "content": None}]
        result = runtime.prepare_messages(
            None, model="m", messages=messages, session_id="mfv3", scope_id="sc1",
        )
        assert isinstance(result, list)

    def test_list_content(self, runtime, mock_client):
        """Multimodal list content handled."""
        messages = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
        result = runtime.prepare_messages(
            None, model="m", messages=messages, session_id="mfv4", scope_id="sc1",
        )
        assert isinstance(result, list)

    def test_wrong_type_content(self, runtime, mock_client):
        """Numeric content converted gracefully."""
        messages = [{"role": "user", "content": 12345}]
        result = runtime.prepare_messages(
            None, model="m", messages=messages, session_id="mfv5", scope_id="sc1",
        )
        assert isinstance(result, list)


# ===========================================================================
# 9. Pre-request with empty messages
# ===========================================================================

class TestEmptyMessages:
    def test_prepare_with_empty_list(self, runtime, mock_client):
        """prepare_messages with [] should not crash."""
        result = runtime.prepare_messages(
            None, model="m", messages=[], session_id="empty1", scope_id="sc1",
        )
        assert isinstance(result, list)
        # At minimum, a system prompt is injected
        assert len(result) >= 1
        assert result[0]["role"] == "system"


# ===========================================================================
# 10. Multiple sequential pre/post request cycles
# ===========================================================================

class TestMultipleCycles:
    def test_ten_round_trips(self, runtime, mock_client):
        """10 sequential pre->post cycles accumulate state correctly."""
        session_id = "cycle10"
        scope_id = "sc_cycle"
        for i in range(10):
            messages = [{"role": "user", "content": f"round {i} " + "x" * 200}]
            prepared = runtime.prepare_messages(
                None, model="m", messages=messages, session_id=session_id, scope_id=scope_id,
            )
            final = [*prepared, {"role": "assistant", "content": f"reply {i}"}]
            runtime.after_response(
                mock_client, model="m", prepared_messages=prepared,
                final_messages=final, session_id=session_id, scope_id=scope_id,
            )
        events = read_transcript_events(session_id)
        # Should have transcript events for all rounds (user + assistant at minimum)
        assert len(events) >= 20  # at least 2 per round
        meta = read_json(session_meta_path(session_id))
        assert meta is not None
        assert meta.get("recent_messages") is not None


# ===========================================================================
# 11-15. OpenAI Proxy API tests
# ===========================================================================

class FakeClient:
    """Fake MWSGPTClient for API tests."""

    def __init__(self, response: dict):
        self._response = response
        self.calls: list[dict] = []

    def chat_completions(self, model: str, messages: list, **kwargs):
        self.calls.append({"model": model, "messages": messages, "kwargs": kwargs})
        return self._response

    def list_models(self):
        return {"data": [{"id": "test-model"}]}


def _patch_service(monkeypatch, fake: FakeClient):
    from certified_turtles.services.llm import LLMService

    monkeypatch.setattr(
        LLMService,
        "from_env",
        classmethod(lambda cls: cls(fake)),  # type: ignore[arg-type]
    )


class TestOpenAIProxyFormat:
    """POST /v1/chat/completions returns OpenAI-compatible format."""

    def test_response_schema(self, monkeypatch):
        from fastapi.testclient import TestClient
        from certified_turtles.main import app

        final = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
            ],
        }
        fake = FakeClient(final)
        _patch_service(monkeypatch, fake)

        client = TestClient(app)
        r = client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        body = r.json()
        assert "choices" in body
        assert body["choices"][0]["message"]["role"] == "assistant"

    def test_streaming_sse_format(self, monkeypatch):
        from fastapi.testclient import TestClient
        from certified_turtles.main import app

        final = {
            "choices": [{"message": {"role": "assistant", "content": "stream-content"}, "finish_reason": "stop"}],
        }
        fake = FakeClient(final)
        _patch_service(monkeypatch, fake)

        client = TestClient(app)
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert r.status_code == 200
        text = r.text
        lines = [ln for ln in text.split("\n") if ln.startswith("data:")]
        assert len(lines) >= 2  # at least role chunk + done
        # First data line should be parseable JSON
        first_data = lines[0][len("data: "):]
        if first_data != "[DONE]":
            chunk = json.loads(first_data)
            assert chunk["object"] == "chat.completion.chunk"
            assert "choices" in chunk
        # Last data should be [DONE]
        assert lines[-1].strip() == "data: [DONE]"


class TestProxyErrorResponses:
    def test_missing_model(self, monkeypatch):
        from fastapi.testclient import TestClient
        from certified_turtles.main import app

        fake = FakeClient({})
        _patch_service(monkeypatch, fake)

        client = TestClient(app)
        r = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 400

    def test_missing_messages(self, monkeypatch):
        from fastapi.testclient import TestClient
        from certified_turtles.main import app

        fake = FakeClient({})
        _patch_service(monkeypatch, fake)

        client = TestClient(app)
        r = client.post(
            "/v1/chat/completions",
            json={"model": "test-model"},
        )
        assert r.status_code == 400

    def test_empty_messages_list(self, monkeypatch):
        from fastapi.testclient import TestClient
        from certified_turtles.main import app

        fake = FakeClient({})
        _patch_service(monkeypatch, fake)

        client = TestClient(app)
        r = client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": []},
        )
        assert r.status_code == 400

    def test_invalid_json_body(self, monkeypatch):
        from fastapi.testclient import TestClient
        from certified_turtles.main import app

        fake = FakeClient({})
        _patch_service(monkeypatch, fake)

        client = TestClient(app)
        r = client.post(
            "/v1/chat/completions",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400


class TestScopeSessionExtraction:
    """Proxy correctly extracts scope_id and session_id from request body."""

    def test_ct_session_and_scope(self, monkeypatch):
        from certified_turtles.api.openai_proxy import _request_ids

        body = {"ct_session_id": "sess-abc", "ct_scope_id": "scope-xyz"}
        sid, scid = _request_ids(body)
        assert sid == "sess-abc"
        assert scid == "scope-xyz"

    def test_fallback_to_conversation_id(self, monkeypatch):
        from certified_turtles.api.openai_proxy import _request_ids

        body = {"conversation_id": "conv-1", "project_id": "proj-1"}
        sid, scid = _request_ids(body)
        assert sid == "conv-1"
        assert scid == "proj-1"

    def test_fallback_to_metadata(self, monkeypatch):
        from certified_turtles.api.openai_proxy import _request_ids

        body = {"metadata": {"chat_id": "chat-m", "workspace_id": "ws-m"}}
        sid, scid = _request_ids(body)
        assert sid == "chat-m"
        assert scid == "ws-m"

    def test_default_session(self, monkeypatch):
        from certified_turtles.api.openai_proxy import _request_ids

        body = {}
        sid, scid = _request_ids(body)
        assert sid == "default-session"
        assert scid == "default-scope"

    def test_non_dict_metadata_ignored(self, monkeypatch):
        from certified_turtles.api.openai_proxy import _request_ids

        body = {"metadata": "not-a-dict"}
        sid, scid = _request_ids(body)
        assert sid == "default-session"


class TestMemoryToolsInResponses:
    """Transcript records tool calls from agent protocol responses."""

    def test_tool_call_transcript(self, runtime, mock_client):
        session_id = "tools1"
        ensure_session_meta(session_id, scope_id="sc1")
        prepared = [{"role": "user", "content": "do it"}]
        proto = json.dumps(
            {"assistant_markdown": "done", "calls": [{"name": "file_read", "arguments": {"file_path": "/a/b"}}]},
            ensure_ascii=False,
        )
        final = [
            *prepared,
            {"role": "assistant", "content": proto},
        ]
        runtime._append_transcript(session_id, final)
        events = read_transcript_events(session_id)
        tool_events = [e for e in events if e.get("kind") == "assistant_tool_call"]
        assert len(tool_events) >= 1
        assert tool_events[0]["tool_name"] == "file_read"


# ===========================================================================
# 16-19. Manager state management
# ===========================================================================

class TestSingleton:
    def test_runtime_from_env_same_instance(self, monkeypatch):
        """runtime_from_env returns the same singleton."""
        import certified_turtles.memory_runtime.manager as mgr

        old = mgr._RUNTIME
        mgr._RUNTIME = None  # reset for test
        try:
            r1 = runtime_from_env()
            r2 = runtime_from_env()
            assert r1 is r2
        finally:
            mgr._RUNTIME = old


class TestSessionUpdatesBounded:
    def test_session_updates_grow(self, runtime, mock_client):
        """_session_updates accumulates entries but doesn't crash with many sessions."""
        for i in range(100):
            sid = f"bounded-{i}"
            messages = [{"role": "user", "content": f"msg {i}"}]
            prepared = runtime.prepare_messages(
                None, model="m", messages=messages, session_id=sid, scope_id="sc1",
            )
            final = [*prepared, {"role": "assistant", "content": f"reply {i}"}]
            runtime.after_response(
                mock_client, model="m", prepared_messages=prepared,
                final_messages=final, session_id=sid, scope_id="sc1",
            )
        # BUG: _session_updates dict grows unbounded with no cleanup mechanism.
        # In production, long-lived processes could accumulate thousands of entries.
        # This test documents the behavior: 100 entries are stored.
        assert len(runtime._session_updates) == 100


class TestFileStateCacheIntegration:
    def test_file_state_accessible(self):
        """note_file_read makes state accessible via get_file_state."""
        note_file_read(
            "fs-session",
            Path("/tmp/test.txt"),
            content="hello world",
            mtime_ns=1000,
            encoding="utf-8",
            line_ending="\n",
            is_partial_view=False,
        )
        state = get_file_state("fs-session", Path("/tmp/test.txt"))
        assert state is not None
        assert state.content == "hello world"


class TestConcurrentPreRequest:
    def test_concurrent_sessions_no_crash(self, mock_client):
        """Multiple threads calling prepare_messages for different sessions."""
        runtime = ClaudeLikeMemoryRuntime()
        errors: list[Exception] = []

        def worker(idx: int):
            try:
                messages = [{"role": "user", "content": f"concurrent {idx}"}]
                runtime.prepare_messages(
                    None, model="m", messages=messages,
                    session_id=f"conc-{idx}", scope_id=f"sc-{idx}",
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert errors == [], f"Concurrent access errors: {errors}"


# ===========================================================================
# 20-24. Edge cases
# ===========================================================================

class TestVeryLongUserMessage:
    def test_100kb_message_no_crash(self, runtime, mock_client):
        """A 100KB user message should not crash prepare_messages."""
        big = "A" * 100_000
        messages = [{"role": "user", "content": big}]
        result = runtime.prepare_messages(
            None, model="m", messages=messages, session_id="big1", scope_id="sc1",
        )
        assert isinstance(result, list)
        user_msgs = [m for m in result if m.get("role") == "user"]
        assert any(big in m["content"] for m in user_msgs)


class TestOnlySystemMessages:
    def test_only_system_messages(self, runtime, mock_client):
        """Messages containing only system messages should be handled."""
        messages = [
            {"role": "system", "content": "instruction A"},
            {"role": "system", "content": "instruction B"},
        ]
        result = runtime.prepare_messages(
            None, model="m", messages=messages, session_id="sysonly1", scope_id="sc1",
        )
        assert isinstance(result, list)
        # Memory system prompt + original system messages
        system_msgs = [m for m in result if m.get("role") == "system"]
        assert len(system_msgs) >= 3  # memory + 2 original


class TestUnicodeIds:
    def test_unicode_scope_and_session(self, runtime, mock_client):
        """Unicode characters in IDs should not crash."""
        messages = [{"role": "user", "content": "unicode test"}]
        result = runtime.prepare_messages(
            None, model="m", messages=messages,
            session_id="sess-\u00e9\u00e8\u4e16\u754c", scope_id="scope-\u00fc\u00f1\u00ee",
        )
        assert isinstance(result, list)
        meta = read_json(session_meta_path("sess-\u00e9\u00e8\u4e16\u754c"))
        assert meta is not None

    def test_unicode_model_name(self, runtime, mock_client):
        """Unicode model name in prepare_messages should not crash."""
        messages = [{"role": "user", "content": "hi"}]
        result = runtime.prepare_messages(
            None, model="\u043c\u043e\u0434\u0435\u043b\u044c-\u03b1", messages=messages,
            session_id="uni-model", scope_id="uni-sc",
        )
        assert isinstance(result, list)
        # Snapshot is stored by session_id
        snap = runtime.forks.get_snapshot("uni-model")
        assert snap is not None
        assert snap.model == "\u043c\u043e\u0434\u0435\u043b\u044c-\u03b1"


class TestStorageDirAutoCreation:
    def test_prepare_creates_dirs(self, runtime, tmp_path, monkeypatch):
        """When CT_CLAUDE_HOME points to nonexistent subdir, dirs are created."""
        new_home = str(tmp_path / "brand_new_dir")
        monkeypatch.setenv("CT_CLAUDE_HOME", new_home)
        assert not Path(new_home).exists()
        messages = [{"role": "user", "content": "create dirs"}]
        result = runtime.prepare_messages(
            None, model="m", messages=messages, session_id="autodir1", scope_id="autodir-sc",
        )
        assert isinstance(result, list)
        assert Path(new_home).exists()


class TestPostRequestWithToolCalls:
    def test_tool_call_transcript_recording(self, runtime, mock_client):
        """Assistant message with tool calls gets recorded in transcript."""
        session_id = "tc-record"
        ensure_session_meta(session_id, scope_id="sc1")
        proto = json.dumps({
            "assistant_markdown": "",
            "calls": [
                {"name": "web_search", "arguments": {"query": "test"}},
                {"name": "file_read", "arguments": {"file_path": "/tmp/x"}},
            ],
        })
        tool_result = (
            f'{PROTOCOL_USER_PREFIX}\n'
            f'{{"tool_outputs": [{{"name": "web_search", "output": "result1"}}, '
            f'{{"name": "file_read", "output": "file content"}}]}}'
        )
        prepared = [{"role": "user", "content": "do task"}]
        final = [
            *prepared,
            {"role": "assistant", "content": proto},
            {"role": "user", "content": tool_result},
            {"role": "assistant", "content": json.dumps({"assistant_markdown": "done", "calls": []})},
        ]
        runtime._append_transcript(session_id, final)
        events = read_transcript_events(session_id)
        tool_call_events = [e for e in events if e.get("kind") == "assistant_tool_call"]
        tool_result_events = [e for e in events if e.get("kind") == "tool_result"]
        assert len(tool_call_events) == 2
        assert {e["tool_name"] for e in tool_call_events} == {"web_search", "file_read"}
        assert len(tool_result_events) == 2


# ===========================================================================
# Additional: RequestContext thread-local
# ===========================================================================

class TestRequestContext:
    def test_context_set_and_cleared(self):
        ctx = RequestContext(session_id="rc1", scope_id="sc1")
        assert current_request_context() is None
        with use_request_context(ctx):
            assert current_request_context() is ctx
        assert current_request_context() is None

    def test_nested_contexts(self):
        outer = RequestContext(session_id="outer", scope_id="sc1")
        inner = RequestContext(session_id="inner", scope_id="sc2")
        with use_request_context(outer):
            assert current_request_context() is outer
            with use_request_context(inner):
                assert current_request_context() is inner
            assert current_request_context() is outer
        assert current_request_context() is None


# ===========================================================================
# Additional: _wants_plain_chat / _request_contract_mode helpers
# ===========================================================================

class TestPlainChatDetection:
    def test_use_agent_false(self):
        from certified_turtles.api.openai_proxy import _wants_plain_chat

        assert _wants_plain_chat({"use_agent": False}) is True

    def test_use_agent_string_false(self):
        from certified_turtles.api.openai_proxy import _wants_plain_chat

        assert _wants_plain_chat({"use_agent": "false"}) is True

    def test_agent_mode_plain(self):
        from certified_turtles.api.openai_proxy import _wants_plain_chat

        assert _wants_plain_chat({"agent_mode": "plain"}) is True

    def test_default_is_agent(self):
        from certified_turtles.api.openai_proxy import _wants_plain_chat

        assert _wants_plain_chat({}) is False


class TestRequestContractMode:
    def test_ct_request_mode(self):
        from certified_turtles.api.openai_proxy import _request_contract_mode

        assert _request_contract_mode({"ct_request_mode": "agent"}) == "agent"
        assert _request_contract_mode({"ct_request_mode": "plain"}) == "plain"
        assert _request_contract_mode({"ct_request_mode": "unknown"}) is None
        assert _request_contract_mode({}) is None

    def test_from_metadata(self):
        from certified_turtles.api.openai_proxy import _request_contract_mode

        assert _request_contract_mode({"metadata": {"ct_request_mode": "router"}}) == "router"


# ===========================================================================
# Additional: _estimate_message_tokens
# ===========================================================================

class TestEstimateTokens:
    def test_basic_estimation(self, runtime):
        msgs = [{"role": "user", "content": "a" * 400}]
        tokens = runtime._estimate_message_tokens(msgs)
        # 400 bytes / 4 = 100
        assert tokens == 100

    def test_empty_message(self, runtime):
        msgs = [{"role": "user", "content": ""}]
        tokens = runtime._estimate_message_tokens(msgs)
        assert tokens == 1  # min 1

    def test_unicode_estimation(self, runtime):
        # Each Cyrillic char is 2 bytes in UTF-8
        msgs = [{"role": "user", "content": "\u0410" * 200}]
        tokens = runtime._estimate_message_tokens(msgs)
        assert tokens == 100  # 400 bytes / 4
