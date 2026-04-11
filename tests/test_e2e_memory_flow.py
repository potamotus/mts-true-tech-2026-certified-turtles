"""
End-to-end integration tests for the memory runtime system.

These tests chain multiple subsystems together (storage, prompting, selector,
forking, file_state, request_context) to verify full flows — NOT isolated units.

Run: pytest tests/test_e2e_memory_flow.py -v --tb=short
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Set up an isolated home BEFORE any project imports touch the filesystem.
_E2E_HOME = tempfile.mkdtemp(prefix="ct_e2e_")
os.environ.setdefault("CT_CLAUDE_HOME", _E2E_HOME)

from certified_turtles.memory_runtime.storage import (
    MAX_MEMORY_SESSION_BYTES,
    MAX_RELEVANT_MEMORIES,
    _last_rebuild,
    append_transcript_event,
    delete_memory_file,
    ensure_session_meta,
    list_memory_files,
    memory_dir,
    memory_index_path,
    read_body,
    read_frontmatter,
    read_json,
    read_session_memory,
    read_transcript_events,
    rebuild_memory_index,
    scan_memory_headers,
    session_meta_path,
    write_json,
    write_memory_file,
    write_session_memory,
)
from certified_turtles.memory_runtime.file_state import (
    FileState,
    _SESSION_CACHE,
    _SESSION_SIZES,
    clone_file_state_namespace,
    get_file_state,
    note_file_read,
)
from certified_turtles.memory_runtime.forking import CacheSafeSnapshot, ForkRuntime
from certified_turtles.memory_runtime.request_context import (
    RequestContext,
    current_request_context,
    use_request_context,
)
from certified_turtles.memory_runtime.prompting import (
    MemoryPromptBundle,
    build_memory_prompt,
)
from certified_turtles.memory_runtime.selector import fallback_select
from certified_turtles.memory_runtime.manager import ClaudeLikeMemoryRuntime


# ── Helpers ──────────────────────────────────────────────────

def _fresh_scope() -> str:
    return f"e2e-scope-{os.urandom(6).hex()}"


def _fresh_session() -> str:
    return f"e2e-session-{os.urandom(6).hex()}"


def _mock_client_selecting(filenames: list[str]) -> MagicMock:
    """Return a MWSGPTClient mock whose chat_completions returns selected filenames."""
    client = MagicMock()
    client.chat_completions.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({"selected_memories": filenames}),
                }
            }
        ]
    }
    return client


def _cleanup_rebuild_cache(scope_id: str) -> None:
    """Clear the _last_rebuild throttle so rebuild_memory_index runs immediately."""
    _last_rebuild.pop(scope_id, None)


def _cleanup_file_state_cache(*namespaces: str) -> None:
    """Remove entries from the global file state cache."""
    for ns in namespaces:
        _SESSION_CACHE.pop(ns, None)
        _SESSION_SIZES.pop(ns, None)


# ── 1. Full memory lifecycle ────────────────────────────────

class TestFullMemoryLifecycle:
    def test_write_rebuild_scan_select_prompt(self):
        """write memory -> rebuild index -> scan headers -> select relevant -> build prompt -> verify memory appears."""
        scope = _fresh_scope()
        session = _fresh_session()
        ensure_session_meta(session, scope_id=scope)

        # Write a memory
        write_memory_file(
            scope,
            name="deploy-notes",
            description="Production deployment notes for v2",
            type_="project",
            body="Deploy v2 on 2026-04-10. Rollback plan: revert tag v1.9.",
        )
        _cleanup_rebuild_cache(scope)

        # Rebuild index explicitly
        idx_path = rebuild_memory_index(scope, force=True)
        assert idx_path.is_file()
        idx_text = idx_path.read_text(encoding="utf-8")
        assert "deploy-notes" in idx_text

        # Scan headers
        headers = scan_memory_headers(scope)
        assert len(headers) == 1
        assert headers[0].name == "deploy-notes"
        assert headers[0].type == "project"

        # Select via fallback (no LLM needed for keyword match)
        selected = fallback_select(headers, "deployment rollback", limit=5)
        assert len(selected) == 1
        assert selected[0].endswith(".md")

        # Build prompt with a mock client that returns the file
        client = _mock_client_selecting(selected)
        bundle = build_memory_prompt(
            client,
            model="test-model",
            messages=[{"role": "user", "content": "How do I rollback the deploy?"}],
            scope_id=scope,
            session_id=session,
            user_query="How do I rollback the deploy?",
        )
        assert "deploy-notes" in bundle.prompt
        assert "Rollback plan" in bundle.prompt
        assert len(bundle.selected_memories) == 1


# ── 2. Session lifecycle ────────────────────────────────────

class TestSessionLifecycle:
    def test_create_session_write_memory_append_transcript_read_back(self):
        session = _fresh_session()
        scope = _fresh_scope()

        # Create session
        ensure_session_meta(session, scope_id=scope)
        meta = read_json(session_meta_path(session))
        assert meta is not None
        assert meta["scope_id"] == scope
        assert "created_at" in meta
        assert "updated_at" in meta

        # Write session memory
        write_session_memory(session, "# Task\nFixing login bug\n# Status\nIn progress")
        assert "Fixing login bug" in read_session_memory(session)

        # Append transcript events
        for i in range(5):
            append_transcript_event(session, {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"message {i}",
                "kind": "message",
            })

        events = read_transcript_events(session, limit=10)
        assert len(events) == 5
        for ev in events:
            assert "uuid" in ev
            assert "content" in ev

        # Verify session meta tracking via ensure_session_meta updates
        ensure_session_meta(session, scope_id=scope)
        meta2 = read_json(session_meta_path(session))
        assert meta2["updated_at"] >= meta["updated_at"]


# ── 3. Multi-scope isolation ────────────────────────────────

class TestMultiScopeIsolation:
    def test_memories_dont_leak_across_scopes(self):
        scope_a = _fresh_scope()
        scope_b = _fresh_scope()

        write_memory_file(
            scope_a,
            name="secret-a",
            description="Only for scope A",
            type_="project",
            body="Scope A data",
        )
        _cleanup_rebuild_cache(scope_a)

        write_memory_file(
            scope_b,
            name="secret-b",
            description="Only for scope B",
            type_="project",
            body="Scope B data",
        )
        _cleanup_rebuild_cache(scope_b)

        headers_a = scan_memory_headers(scope_a)
        headers_b = scan_memory_headers(scope_b)

        names_a = {h.name for h in headers_a}
        names_b = {h.name for h in headers_b}

        assert "secret-a" in names_a
        assert "secret-b" not in names_a
        assert "secret-b" in names_b
        assert "secret-a" not in names_b

        # Verify memory directories are distinct
        assert memory_dir(scope_a) != memory_dir(scope_b)

        # Verify index files are separate
        idx_a = memory_index_path(scope_a).read_text(encoding="utf-8")
        idx_b = memory_index_path(scope_b).read_text(encoding="utf-8")
        assert "secret-a" in idx_a
        assert "secret-b" not in idx_a
        assert "secret-b" in idx_b
        assert "secret-a" not in idx_b


# ── 4. Memory prompt with conditional rules ─────────────────

class TestConditionalRulesInPrompt:
    def test_conditional_rule_matches_file_path(self, tmp_path):
        """Set up a rules dir with globs, verify rules appear in prompt when file paths match."""
        scope = _fresh_scope()
        session = _fresh_session()
        ensure_session_meta(session, scope_id=scope)

        # Create a CLAUDE.md and rules dir under tmp_path
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        rules_dir = claude_dir / "rules"
        rules_dir.mkdir()

        # Write a conditional rule with glob matching *.py
        rule_file = rules_dir / "python-style.md"
        rule_file.write_text(
            '---\npaths: ["*.py"]\n---\n\nAlways use type hints in Python files.',
            encoding="utf-8",
        )

        # Write a second rule that should NOT match
        rule_file2 = rules_dir / "rust-style.md"
        rule_file2.write_text(
            '---\npaths: ["*.rs"]\n---\n\nUse clippy for Rust files.',
            encoding="utf-8",
        )

        # Messages that reference a .py file path (via assistant tool call protocol)
        messages = [
            {
                "role": "assistant",
                "content": json.dumps({
                    "assistant_markdown": "",
                    "calls": [{"name": "file_read", "arguments": {"file_path": "src/main.py"}}],
                }),
            },
            {"role": "user", "content": "Fix the type error in main.py"},
        ]

        # Patch load_conditional_rules to use our tmp_path
        with patch(
            "certified_turtles.memory_runtime.prompting.load_conditional_rules",
            return_value=[
                __import__(
                    "certified_turtles.memory_runtime.static_instructions",
                    fromlist=["ConditionalRule"],
                ).ConditionalRule(
                    path=rule_file,
                    content="Always use type hints in Python files.",
                    globs=("*.py",),
                ),
            ],
        ):
            bundle = build_memory_prompt(
                None,  # no LLM needed for this test
                model="test-model",
                messages=messages,
                scope_id=scope,
                session_id=session,
                user_query="Fix the type error in main.py",
            )

        assert "type hints" in bundle.prompt
        # The Rust rule should not be there
        assert "clippy" not in bundle.prompt


# ── 5. Compaction flow ──────────────────────────────────────

class TestCompactionFlow:
    def test_compaction_replaces_old_messages_with_session_memory(self):
        """Feed enough messages to trigger compaction, verify session memory is inserted."""
        scope = _fresh_scope()
        session = _fresh_session()
        ensure_session_meta(session, scope_id=scope)

        # Write session memory (compaction only kicks in if session memory exists)
        write_session_memory(session, "# Summary\nWorking on API refactor. Migrated 3 endpoints.")

        runtime = ClaudeLikeMemoryRuntime()

        # Create messages large enough to exceed the compact threshold.
        # _compact_threshold() defaults to 150_000 tokens (~600KB text).
        # We'll lower it via env var for this test.
        big_chunk = "x" * 4000  # ~1000 tokens per message
        messages = []
        # First, a system message that should be preserved
        messages.append({"role": "system", "content": "You are a helpful assistant."})
        for i in range(200):
            role = "user" if i % 2 == 0 else "assistant"
            messages.append({"role": role, "content": f"Turn {i}: {big_chunk}"})

        with patch.dict(os.environ, {"CT_COMPACT_THRESHOLD": "50000"}):
            compacted = runtime._compact_if_needed(messages, session)

        # Compacted should be shorter than original
        assert len(compacted) < len(messages)
        # System message should be preserved
        assert compacted[0]["role"] == "system"
        assert compacted[0]["content"] == "You are a helpful assistant."
        # Session memory summary should appear
        found_summary = any("API refactor" in m.get("content", "") for m in compacted)
        assert found_summary, "Session memory should appear in compacted messages"
        # The last few messages should still be present (not all compacted away)
        assert compacted[-1]["content"] == messages[-1]["content"]


# ── 6. Memory selection with surfaced tracking ──────────────

class TestSurfacedTracking:
    def test_surfaced_memories_grow_and_deduplicate(self):
        """Build prompt twice with different selected memories, verify surfaced_memories in meta.json grows and deduplicates."""
        scope = _fresh_scope()
        session = _fresh_session()
        ensure_session_meta(session, scope_id=scope)

        # Write two memories
        write_memory_file(scope, name="mem-alpha", description="Alpha info", type_="project", body="Alpha content")
        _cleanup_rebuild_cache(scope)
        write_memory_file(scope, name="mem-beta", description="Beta info", type_="project", body="Beta content")
        _cleanup_rebuild_cache(scope)

        headers = scan_memory_headers(scope)
        filenames = [h.filename for h in headers]
        assert len(filenames) == 2

        # First prompt: select alpha only
        client1 = _mock_client_selecting([filenames[0]])
        build_memory_prompt(
            client1,
            model="m",
            messages=[{"role": "user", "content": "alpha query"}],
            scope_id=scope,
            session_id=session,
            user_query="alpha query",
        )
        meta1 = read_json(session_meta_path(session))
        surfaced1 = meta1.get("surfaced_memories", [])
        assert filenames[0] in surfaced1

        # Second prompt: select beta only
        client2 = _mock_client_selecting([filenames[1]])
        build_memory_prompt(
            client2,
            model="m",
            messages=[{"role": "user", "content": "beta query"}],
            scope_id=scope,
            session_id=session,
            user_query="beta query",
        )
        meta2 = read_json(session_meta_path(session))
        surfaced2 = meta2.get("surfaced_memories", [])
        # Both should be present
        assert filenames[0] in surfaced2
        assert filenames[1] in surfaced2
        # No duplicates
        assert len(surfaced2) == len(set(surfaced2))

        # Third prompt: select alpha again — should NOT duplicate
        client3 = _mock_client_selecting([filenames[0]])
        build_memory_prompt(
            client3,
            model="m",
            messages=[{"role": "user", "content": "alpha again"}],
            scope_id=scope,
            session_id=session,
            user_query="alpha again",
        )
        meta3 = read_json(session_meta_path(session))
        surfaced3 = meta3.get("surfaced_memories", [])
        assert len(surfaced3) == len(set(surfaced3))
        # Still exactly 2 unique entries
        assert len(set(surfaced3)) == 2


# ── 7. Subagent fork-restore cycle ──────────────────────────

class TestForkRestoreCycle:
    def test_save_snapshot_modify_restore(self):
        """Save snapshot -> modify state -> restore -> verify original state."""
        forks = ForkRuntime()
        session = _fresh_session()

        original_messages = [
            {"role": "system", "content": "sys prompt"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        snap = CacheSafeSnapshot(
            model="test-model",
            scope_id="scope-x",
            session_id=session,
            file_state_namespace=session,
            messages=[dict(m) for m in original_messages],
            saved_at=time.time(),
        )
        forks.save_snapshot(snap)

        # Verify snapshot is stored
        restored = forks.get_snapshot(session)
        assert restored is not None
        assert restored.model == "test-model"
        assert len(restored.messages) == 3
        assert restored.messages[1]["content"] == "hello"

        # Save a new snapshot (simulating state modification)
        modified_messages = [*original_messages, {"role": "user", "content": "new message"}]
        snap2 = CacheSafeSnapshot(
            model="test-model-v2",
            scope_id="scope-x",
            session_id=session,
            file_state_namespace=session,
            messages=[dict(m) for m in modified_messages],
            saved_at=time.time(),
        )
        forks.save_snapshot(snap2)

        # Verify updated snapshot
        restored2 = forks.get_snapshot(session)
        assert restored2 is not None
        assert restored2.model == "test-model-v2"
        assert len(restored2.messages) == 4

        # Save original back (restore)
        forks.save_snapshot(snap)
        restored3 = forks.get_snapshot(session)
        assert restored3 is not None
        assert len(restored3.messages) == 3
        assert restored3.model == "test-model"

    def test_different_sessions_isolated(self):
        """Snapshots for different sessions don't interfere."""
        forks = ForkRuntime()
        s1 = _fresh_session()
        s2 = _fresh_session()

        forks.save_snapshot(CacheSafeSnapshot(
            model="m1", scope_id="s", session_id=s1,
            file_state_namespace=s1, messages=[{"role": "user", "content": "s1"}], saved_at=time.time(),
        ))
        forks.save_snapshot(CacheSafeSnapshot(
            model="m2", scope_id="s", session_id=s2,
            file_state_namespace=s2, messages=[{"role": "user", "content": "s2"}], saved_at=time.time(),
        ))

        assert forks.get_snapshot(s1).model == "m1"
        assert forks.get_snapshot(s2).model == "m2"
        assert forks.get_snapshot(s1).messages[0]["content"] == "s1"
        assert forks.get_snapshot(s2).messages[0]["content"] == "s2"


# ── 8. File state cache with forking ────────────────────────

class TestFileStateCacheForking:
    def setup_method(self):
        self.ns_source = f"fstate-src-{os.urandom(4).hex()}"
        self.ns_clone = f"fstate-clone-{os.urandom(4).hex()}"

    def teardown_method(self):
        _cleanup_file_state_cache(self.ns_source, self.ns_clone)

    def test_clone_isolation_and_independence(self):
        """Populate cache -> clone namespace -> verify isolation -> modify clone -> verify original unchanged."""
        source = self.ns_source
        clone = self.ns_clone
        p = Path("/fake/test.py")

        # Populate source
        note_file_read(
            source, p,
            content="original content",
            mtime_ns=1000,
            encoding="utf-8",
            line_ending="\n",
            is_partial_view=False,
        )

        # Clone
        clone_file_state_namespace(source, clone)

        # Both should have the entry
        orig_state = get_file_state(source, p)
        clone_state = get_file_state(clone, p)
        assert orig_state is not None
        assert clone_state is not None
        assert orig_state.content == "original content"
        assert clone_state.content == "original content"

        # Modify clone
        note_file_read(
            clone, p,
            content="modified in clone",
            mtime_ns=2000,
            encoding="utf-8",
            line_ending="\n",
            is_partial_view=False,
        )

        # Original should be unchanged
        orig_again = get_file_state(source, p)
        assert orig_again is not None
        assert orig_again.content == "original content"

        clone_again = get_file_state(clone, p)
        assert clone_again.content == "modified in clone"

    def test_clone_of_empty_source(self):
        """Cloning a nonexistent source clears the target."""
        clone = self.ns_clone
        p = Path("/fake/foo.txt")

        # Put something in clone first
        note_file_read(
            clone, p,
            content="will be wiped",
            mtime_ns=1,
            encoding="utf-8",
            line_ending="\n",
            is_partial_view=False,
        )
        assert get_file_state(clone, p) is not None

        # Clone from a source that was never populated
        clone_file_state_namespace("nonexistent-ns", clone)

        # Clone should now be empty
        assert get_file_state(clone, p) is None


# ── 9. Transcript tail-read correctness ─────────────────────

class TestTranscriptTailRead:
    def test_200_events_read_last_10(self):
        """Write 200 events, read with limit=10, verify we get the LAST 10."""
        session = _fresh_session()

        for i in range(200):
            append_transcript_event(session, {
                "seq": i,
                "role": "user",
                "content": f"event-{i}",
            })

        events = read_transcript_events(session, limit=10)
        assert len(events) == 10

        # Verify these are the LAST 10
        seqs = [e["seq"] for e in events]
        assert seqs == list(range(190, 200))

    def test_read_more_than_available(self):
        """If fewer events exist than limit, return all of them."""
        session = _fresh_session()

        for i in range(3):
            append_transcript_event(session, {"seq": i, "content": f"e{i}"})

        events = read_transcript_events(session, limit=100)
        assert len(events) == 3
        assert [e["seq"] for e in events] == [0, 1, 2]

    def test_empty_transcript(self):
        session = _fresh_session()
        events = read_transcript_events(session, limit=10)
        assert events == []


# ── 10. Full request context flow ───────────────────────────

class TestRequestContextFlow:
    def test_context_propagation(self):
        """Set context -> call functions that read context -> verify scope_id/session_id propagate."""
        scope = _fresh_scope()
        session = _fresh_session()
        ctx = RequestContext(session_id=session, scope_id=scope, file_state_namespace=session)

        # Before context is set, it should be None
        assert current_request_context() is None

        with use_request_context(ctx):
            inner = current_request_context()
            assert inner is not None
            assert inner.session_id == session
            assert inner.scope_id == scope
            assert inner.file_state_namespace == session

        # After context manager exits
        assert current_request_context() is None

    def test_nested_context_restore(self):
        """Nested contexts should restore the outer context on exit."""
        outer = RequestContext(session_id="outer", scope_id="scope-outer")
        inner = RequestContext(session_id="inner", scope_id="scope-inner")

        with use_request_context(outer):
            assert current_request_context().session_id == "outer"
            with use_request_context(inner):
                assert current_request_context().session_id == "inner"
            # After inner exits, outer should be restored
            assert current_request_context().session_id == "outer"

        assert current_request_context() is None

    def test_context_in_different_function(self):
        """Context should be readable from functions called within the context."""
        scope = _fresh_scope()
        session = _fresh_session()

        def read_context_values():
            ctx = current_request_context()
            return (ctx.scope_id, ctx.session_id) if ctx else (None, None)

        with use_request_context(RequestContext(session_id=session, scope_id=scope)):
            s, sess = read_context_values()
            assert s == scope
            assert sess == session


# ── 11. Memory write → delete → rebuild index ──────────────

class TestWriteDeleteRebuild:
    def test_write_5_delete_2_rebuild_has_3(self):
        scope = _fresh_scope()

        # Write 5 memories
        filenames = []
        for i in range(5):
            _cleanup_rebuild_cache(scope)
            path = write_memory_file(
                scope,
                name=f"mem-{i}",
                description=f"Memory number {i}",
                type_="project",
                body=f"Content for memory {i}",
                filename=f"mem-{i}.md",
            )
            filenames.append(f"mem-{i}.md")

        # Verify 5 exist
        _cleanup_rebuild_cache(scope)
        headers = scan_memory_headers(scope)
        assert len(headers) == 5

        # Delete 2
        _cleanup_rebuild_cache(scope)
        assert delete_memory_file(scope, "mem-1.md") is True
        _cleanup_rebuild_cache(scope)
        assert delete_memory_file(scope, "mem-3.md") is True

        # Rebuild
        _cleanup_rebuild_cache(scope)
        rebuild_memory_index(scope, force=True)

        # Verify index only has 3 entries
        headers_after = scan_memory_headers(scope)
        assert len(headers_after) == 3
        remaining_names = {h.name for h in headers_after}
        assert remaining_names == {"mem-0", "mem-2", "mem-4"}

        # Verify index file content
        idx_text = memory_index_path(scope).read_text(encoding="utf-8")
        assert "mem-1" not in idx_text
        assert "mem-3" not in idx_text
        assert "mem-0" in idx_text
        assert "mem-2" in idx_text
        assert "mem-4" in idx_text

    def test_delete_nonexistent_returns_false(self):
        scope = _fresh_scope()
        assert delete_memory_file(scope, "does-not-exist.md") is False


# ── 12. Large-scale memory selection ────────────────────────

class TestLargeScaleMemorySelection:
    def test_50_memories_max_relevant_limit(self):
        """Create 50 memories, build prompt, verify MAX_RELEVANT_MEMORIES limit respected."""
        scope = _fresh_scope()
        session = _fresh_session()
        ensure_session_meta(session, scope_id=scope)

        # Write 50 memories
        for i in range(50):
            _cleanup_rebuild_cache(scope)
            write_memory_file(
                scope,
                name=f"topic-{i:03d}",
                description=f"Topic number {i} about deployment",
                type_="project",
                body=f"Details about topic {i}. Deployment related info.",
                filename=f"topic-{i:03d}.md",
            )

        _cleanup_rebuild_cache(scope)
        headers = scan_memory_headers(scope)
        assert len(headers) == 50

        # Mock client to select more than MAX_RELEVANT_MEMORIES
        all_filenames = [h.filename for h in headers]
        # Client tries to return 10, but limit should cap it
        client = _mock_client_selecting(all_filenames[:10])

        bundle = build_memory_prompt(
            client,
            model="m",
            messages=[{"role": "user", "content": "deployment info"}],
            scope_id=scope,
            session_id=session,
            user_query="deployment info",
        )

        # selected_memories should be at most MAX_RELEVANT_MEMORIES
        assert len(bundle.selected_memories) <= MAX_RELEVANT_MEMORIES

        # Total bytes in the prompt's relevant memories section should be under MAX_MEMORY_SESSION_BYTES
        prompt_bytes = len(bundle.prompt.encode("utf-8"))
        # The relevant_memories section itself won't exceed MAX_MEMORY_SESSION_BYTES
        # (The full prompt includes instructions, so it's larger, but memory body is capped.)
        if "## relevant_memories" in bundle.prompt:
            relevant_start = bundle.prompt.index("## relevant_memories")
            # Find the next major section or end of prompt
            remaining = bundle.prompt[relevant_start:]
            # Session memory section typically follows
            session_idx = remaining.find("# session_memory")
            if session_idx > 0:
                relevant_section = remaining[:session_idx]
            else:
                relevant_section = remaining
            relevant_bytes = len(relevant_section.encode("utf-8"))
            assert relevant_bytes <= MAX_MEMORY_SESSION_BYTES + 1024  # small header overhead

    def test_fallback_select_respects_limit(self):
        """fallback_select should never return more than limit."""
        from certified_turtles.memory_runtime.storage import MemoryHeader

        headers = [
            MemoryHeader(
                filename=f"file-{i}.md",
                name=f"deploy-topic-{i}",
                description=f"Deployment topic {i}",
                type="project",
                mtime=time.time(),
            )
            for i in range(50)
        ]

        selected = fallback_select(headers, "deploy topic", limit=MAX_RELEVANT_MEMORIES)
        assert len(selected) <= MAX_RELEVANT_MEMORIES
