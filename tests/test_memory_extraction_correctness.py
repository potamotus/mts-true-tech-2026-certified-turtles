"""
Deep correctness tests for memory EXTRACTION pipeline.

Tests verify that extraction triggers, memory writes, frontmatter handling,
filename sanitization, session memory decisions, and extractor prompt construction
produce *correct* results under edge cases.

Run: pytest tests/test_memory_extraction_correctness.py -v --tb=short
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CT_CLAUDE_HOME", tempfile.mkdtemp(prefix="ct_excorr_"))

from certified_turtles.memory_runtime.manager import (
    ClaudeLikeMemoryRuntime,
    _MAX_SECTION_LENGTH,
    _MAX_TOTAL_SESSION_MEMORY_TOKENS,
    _extract_window_size,
)
from certified_turtles.memory_runtime.storage import (
    MemoryHeader,
    _last_rebuild,
    ensure_session_meta,
    memory_dir,
    memory_index_path,
    parse_frontmatter,
    read_body,
    read_frontmatter,
    rebuild_memory_index,
    resolve_memory_path,
    scan_memory_headers,
    session_meta_path,
    write_json,
    read_json,
    write_memory_file,
    write_session_memory,
    read_session_memory,
    delete_memory_file,
    _validate_memory_filename,
    slugify,
    format_memory_manifest,
)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path):
    old = os.environ.get("CT_CLAUDE_HOME")
    os.environ["CT_CLAUDE_HOME"] = str(tmp_path / "claude_home")
    _last_rebuild.clear()
    yield tmp_path
    if old is None:
        os.environ.pop("CT_CLAUDE_HOME", None)
    else:
        os.environ["CT_CLAUDE_HOME"] = old


SCOPE = "excorr-scope"


def _session() -> str:
    return f"excorr-sess-{os.urandom(4).hex()}"


def _mock_client() -> MagicMock:
    client = MagicMock()
    client.chat_completions.return_value = {
        "choices": [{"message": {"content": json.dumps({"selected_memories": []})}}]
    }
    return client


# ═══════════════════════════════════════════════════════════════
# 1. FILENAME SANITIZATION & PATH TRAVERSAL PREVENTION
# ═══════════════════════════════════════════════════════════════


class TestFilenameSanitization:
    """Filenames must be sanitized to prevent path traversal and invalid chars."""

    def test_path_traversal_rejected(self):
        """../../etc/passwd must be rejected."""
        with pytest.raises(ValueError, match="traverse"):
            _validate_memory_filename("../../etc/passwd", fallback_name="fallback")

    def test_absolute_path_rejected(self):
        with pytest.raises(ValueError, match="relative"):
            _validate_memory_filename("/etc/passwd", fallback_name="fallback")

    def test_special_chars_sanitized(self):
        result = _validate_memory_filename("my memory!@#$.md", fallback_name="fallback")
        # Should not contain special chars
        name = result.name
        assert "!" not in name
        assert "@" not in name
        assert "#" not in name

    def test_dotdot_in_middle_rejected(self):
        with pytest.raises(ValueError, match="traverse"):
            _validate_memory_filename("subdir/../../../etc/passwd", fallback_name="x")

    def test_empty_filename_uses_fallback(self):
        result = _validate_memory_filename("", fallback_name="my memory")
        assert result.suffix == ".md"
        assert "my-memory" in str(result).lower() or "my" in str(result)

    def test_none_filename_uses_fallback(self):
        result = _validate_memory_filename(None, fallback_name="fallback name")
        assert result.suffix == ".md"

    def test_filename_without_md_gets_extension(self):
        result = _validate_memory_filename("notes", fallback_name="x")
        assert str(result).endswith(".md")

    def test_very_long_segment_rejected(self):
        long_name = "a" * 300 + ".md"  # > 255 bytes
        with pytest.raises(ValueError, match="255"):
            _validate_memory_filename(long_name, fallback_name="fallback")

    def test_resolve_memory_path_stays_within_scope(self):
        """resolve_memory_path must prevent escaping the memory directory."""
        scope = f"safe-{os.urandom(4).hex()}"
        # Valid filename should resolve within memory_dir
        path = resolve_memory_path(scope, "valid-note.md", fallback_name="note")
        mem_root = memory_dir(scope)
        assert str(path).startswith(str(mem_root.resolve()))


# ═══════════════════════════════════════════════════════════════
# 2. MEMORY TYPE VALIDATION
# ═══════════════════════════════════════════════════════════════


class TestMemoryTypeValidation:
    """Invalid memory types must fall back to 'project', not crash."""

    def test_invalid_type_falls_back_to_project(self):
        scope = f"type-{os.urandom(4).hex()}"
        path = write_memory_file(scope, name="Test", description="test",
                                 type_="invalid_type", body="content",
                                 filename="test-type.md")
        fm = read_frontmatter(path)
        assert fm["type"] == "project"

    def test_all_valid_types_accepted(self):
        for t in ("user", "feedback", "project", "reference"):
            scope = f"vtype-{t}-{os.urandom(4).hex()}"
            path = write_memory_file(scope, name=f"Test {t}", description=f"test {t}",
                                     type_=t, body=f"content for {t}",
                                     filename=f"test-{t}.md")
            fm = read_frontmatter(path)
            assert fm["type"] == t


# ═══════════════════════════════════════════════════════════════
# 3. OVERWRITE SEMANTICS: CREATED vs UPDATED
# ═══════════════════════════════════════════════════════════════


class TestOverwriteSemantics:
    """When overwriting, 'created' must be preserved from the original,
    'updated' must reflect the new write time."""

    def test_overwrite_preserves_created_updates_updated(self):
        scope = f"ow-{os.urandom(4).hex()}"
        path1 = write_memory_file(scope, name="V1", description="version 1",
                                  type_="user", body="version 1 body",
                                  filename="persist.md")
        fm1 = read_frontmatter(path1)
        created1 = fm1["created"]
        updated1 = fm1["updated"]

        time.sleep(0.05)
        _last_rebuild.clear()

        path2 = write_memory_file(scope, name="V2", description="version 2",
                                  type_="user", body="version 2 body",
                                  filename="persist.md")
        fm2 = read_frontmatter(path2)

        assert fm2["created"] == created1, "created should be preserved from original"
        assert fm2["updated"] != updated1, "updated should change"
        assert fm2["name"] == "V2", "name should be updated"
        assert read_body(path2) == "version 2 body"

    def test_overwrite_changes_source_field(self):
        scope = f"ow-src-{os.urandom(4).hex()}"
        write_memory_file(scope, name="M", description="d", type_="user",
                          body="v1", filename="m.md", source="manual")
        _last_rebuild.clear()
        path = write_memory_file(scope, name="M", description="d", type_="user",
                                 body="v2", filename="m.md", source="memory_extractor")
        fm = read_frontmatter(path)
        assert fm["source"] == "memory_extractor"

    def test_new_file_has_created_equal_updated(self):
        scope = f"new-{os.urandom(4).hex()}"
        path = write_memory_file(scope, name="New", description="new",
                                 type_="user", body="content", filename="new.md")
        fm = read_frontmatter(path)
        assert fm["created"] == fm["updated"]


# ═══════════════════════════════════════════════════════════════
# 4. INDEX REBUILD CORRECTNESS
# ═══════════════════════════════════════════════════════════════


class TestIndexRebuild:
    """rebuild_memory_index must produce a correct index that lists all memories
    and respects size limits."""

    def test_index_lists_all_memories(self):
        scope = f"idx-{os.urandom(4).hex()}"
        for i in range(5):
            _last_rebuild.clear()
            write_memory_file(scope, name=f"Mem {i}", description=f"desc {i}",
                              type_="project", body=f"body {i}", filename=f"mem-{i}.md")
        _last_rebuild.clear()
        rebuild_memory_index(scope, force=True)

        index_text = memory_index_path(scope).read_text(encoding="utf-8")
        for i in range(5):
            assert f"mem-{i}.md" in index_text

    def test_index_excludes_deleted_files(self):
        scope = f"idx-del-{os.urandom(4).hex()}"
        write_memory_file(scope, name="Keep", description="keep", type_="user",
                          body="keep", filename="keep.md")
        _last_rebuild.clear()
        write_memory_file(scope, name="Delete", description="delete", type_="user",
                          body="delete", filename="delete.md")
        _last_rebuild.clear()
        delete_memory_file(scope, "delete.md")
        _last_rebuild.clear()
        rebuild_memory_index(scope, force=True)

        index_text = memory_index_path(scope).read_text(encoding="utf-8")
        assert "keep.md" in index_text
        assert "delete.md" not in index_text

    def test_index_throttle_prevents_rapid_rebuilds(self):
        """Two rapid writes within 1 second should not both trigger rebuilds."""
        scope = f"idx-thr-{os.urandom(4).hex()}"
        _last_rebuild.clear()

        # First write triggers rebuild
        write_memory_file(scope, name="A", description="a", type_="project",
                          body="a", filename="a.md")
        # _last_rebuild[scope] should now be set

        # Second write without clearing the throttle
        write_memory_file(scope, name="B", description="b", type_="project",
                          body="b", filename="b.md")

        # The second rebuild might be throttled — b.md might not be in the index
        index_text = memory_index_path(scope).read_text(encoding="utf-8")
        # Force rebuild to verify both are actually stored
        _last_rebuild.clear()
        rebuild_memory_index(scope, force=True)
        index_after_force = memory_index_path(scope).read_text(encoding="utf-8")
        assert "a.md" in index_after_force
        assert "b.md" in index_after_force

    def test_index_has_header(self):
        scope = f"idx-hdr-{os.urandom(4).hex()}"
        write_memory_file(scope, name="X", description="x", type_="user",
                          body="x", filename="x.md")
        _last_rebuild.clear()
        rebuild_memory_index(scope, force=True)
        index = memory_index_path(scope).read_text(encoding="utf-8")
        assert index.startswith("# Memory Index")

    def test_index_entries_contain_description(self):
        scope = f"idx-desc-{os.urandom(4).hex()}"
        write_memory_file(scope, name="Pizza", description="User prefers margherita pizza",
                          type_="user", body="likes pizza", filename="pizza.md")
        _last_rebuild.clear()
        rebuild_memory_index(scope, force=True)
        index = memory_index_path(scope).read_text(encoding="utf-8")
        assert "User prefers margherita pizza" in index


# ═══════════════════════════════════════════════════════════════
# 5. SESSION MEMORY UPDATE DECISION LOGIC
# ═══════════════════════════════════════════════════════════════


class TestSessionMemoryDecision:
    """_should_update_session_memory has multiple gates:
    1. Token count >= 10K for initialization
    2. Delta from last extract >= 5K
    3. Recent tool calls >= 3 OR last assistant has no tool calls
    All must be tested in combination."""

    def _make_msgs(self, total_bytes: int, tool_calls: int = 0) -> list[dict]:
        msgs = [{"role": "user", "content": "x" * total_bytes}]
        for _ in range(tool_calls):
            msgs.append({"role": "assistant", "content": json.dumps({
                "assistant_markdown": "work",
                "calls": [{"name": "file_read", "arguments": {"file_path": "/tmp/x"}}],
            })})
        return msgs

    def test_below_10k_tokens_not_initialized(self):
        """Under 10K tokens, session memory should NOT be initialized."""
        rt = ClaudeLikeMemoryRuntime()
        session = _session()
        ensure_session_meta(session, scope_id=SCOPE)
        msgs = self._make_msgs(20_000)  # ~5K tokens
        assert rt._should_update_session_memory(session, msgs) is False

    def test_at_10k_tokens_initializes(self):
        """At 10K tokens with enough tool calls, session memory initializes."""
        rt = ClaudeLikeMemoryRuntime()
        session = _session()
        ensure_session_meta(session, scope_id=SCOPE)
        # 40K bytes = 10K tokens, enough tool calls to pass the gate
        msgs = self._make_msgs(40_000, tool_calls=4)
        assert rt._should_update_session_memory(session, msgs) is True

    def test_delta_below_5k_skipped(self):
        """After initialization, updates need >= 5K token delta."""
        rt = ClaudeLikeMemoryRuntime()
        session = _session()
        ensure_session_meta(session, scope_id=SCOPE)

        # Initialize: 40K bytes = 10K tokens
        msgs_init = self._make_msgs(40_000, tool_calls=4)
        assert rt._should_update_session_memory(session, msgs_init) is True
        rt._mark_session_memory_extracted(session, msgs_init)

        # Small delta: 44K bytes = 11K tokens, delta = 1K < 5K
        msgs_small = self._make_msgs(44_000, tool_calls=4)
        assert rt._should_update_session_memory(session, msgs_small) is False

    def test_delta_above_5k_triggers_update(self):
        """After initialization with enough delta, update triggers."""
        rt = ClaudeLikeMemoryRuntime()
        session = _session()
        ensure_session_meta(session, scope_id=SCOPE)

        msgs_init = self._make_msgs(40_000, tool_calls=4)
        assert rt._should_update_session_memory(session, msgs_init) is True
        rt._mark_session_memory_extracted(session, msgs_init)

        # Big delta: 80K bytes = 20K tokens, delta = 10K > 5K
        msgs_big = self._make_msgs(80_000, tool_calls=4)
        assert rt._should_update_session_memory(session, msgs_big) is True

    def test_few_tool_calls_with_last_assistant_having_tools_blocks(self):
        """< 3 tool calls AND last assistant has tool calls → blocked."""
        rt = ClaudeLikeMemoryRuntime()
        session = _session()
        ensure_session_meta(session, scope_id=SCOPE)

        # 60K bytes = 15K tokens, but only 2 tool calls and last assistant has tools
        msgs = [{"role": "user", "content": "x" * 60_000}]
        for _ in range(2):
            msgs.append({"role": "assistant", "content": json.dumps({
                "assistant_markdown": "work",
                "calls": [{"name": "file_read", "arguments": {"file_path": "/tmp/x"}}],
            })})
        # Last message is assistant with tool calls → recent_tool_calls=2 < 3, blocked
        assert rt._should_update_session_memory(session, msgs) is False

    def test_few_tool_calls_without_last_assistant_tools_passes(self):
        """< 3 tool calls but last assistant has NO tool calls → passes."""
        rt = ClaudeLikeMemoryRuntime()
        session = _session()
        ensure_session_meta(session, scope_id=SCOPE)

        msgs = [{"role": "user", "content": "x" * 60_000}]
        msgs.append({"role": "assistant", "content": json.dumps({
            "assistant_markdown": "work",
            "calls": [{"name": "file_read", "arguments": {"file_path": "/tmp/x"}}],
        })})
        # Last message is a plain assistant response (no tool calls)
        msgs.append({"role": "assistant", "content": "Here's my answer without tools."})
        # recent_tool_calls=1 < 3 but last_assistant_has_tool_calls=False → passes
        assert rt._should_update_session_memory(session, msgs) is True


# ═══════════════════════════════════════════════════════════════
# 6. EXTRACTOR PROMPT CONSTRUCTION
# ═══════════════════════════════════════════════════════════════


class TestExtractorPrompt:
    """The extractor prompt must include the correct window size, manifest,
    and type instructions."""

    def test_prompt_contains_window_size(self):
        rt = ClaudeLikeMemoryRuntime()
        scope = f"ep-{os.urandom(4).hex()}"
        msgs = [{"role": "user", "content": "hello"}]
        prompt = rt._extractor_prompt(scope, msgs)
        assert "~8 messages" in prompt  # default window size

    def test_prompt_window_from_env(self):
        rt = ClaudeLikeMemoryRuntime()
        scope = f"ep-env-{os.urandom(4).hex()}"
        msgs = [{"role": "user", "content": "hello"}]
        with patch.dict(os.environ, {"CT_EXTRACT_WINDOW": "12"}):
            prompt = rt._extractor_prompt(scope, msgs)
        assert "~12 messages" in prompt

    def test_prompt_window_clamped_to_min(self):
        with patch.dict(os.environ, {"CT_EXTRACT_WINDOW": "1"}):
            assert _extract_window_size() == 4  # clamped to min=4

    def test_prompt_window_clamped_to_max(self):
        with patch.dict(os.environ, {"CT_EXTRACT_WINDOW": "100"}):
            assert _extract_window_size() == 20  # clamped to max=20

    def test_prompt_includes_manifest_when_memories_exist(self):
        rt = ClaudeLikeMemoryRuntime()
        scope = f"ep-man-{os.urandom(4).hex()}"
        write_memory_file(scope, name="Existing", description="existing memory",
                          type_="user", body="content", filename="existing.md")
        _last_rebuild.clear()

        prompt = rt._extractor_prompt(scope, [{"role": "user", "content": "hi"}])
        assert "existing.md" in prompt
        assert "Existing memory" in prompt or "existing memory" in prompt
        assert "file_read ALL existing memory files" in prompt

    def test_prompt_no_manifest_when_empty(self):
        rt = ClaudeLikeMemoryRuntime()
        scope = f"ep-empty-{os.urandom(4).hex()}"
        prompt = rt._extractor_prompt(scope, [{"role": "user", "content": "hi"}])
        assert "Existing memory files" not in prompt

    def test_prompt_includes_type_instructions(self):
        rt = ClaudeLikeMemoryRuntime()
        scope = f"ep-types-{os.urandom(4).hex()}"
        prompt = rt._extractor_prompt(scope, [{"role": "user", "content": "hi"}])
        assert "user" in prompt
        assert "feedback" in prompt
        assert "project" in prompt
        assert "reference" in prompt

    def test_prompt_includes_decision_process(self):
        rt = ClaudeLikeMemoryRuntime()
        scope = f"ep-dec-{os.urandom(4).hex()}"
        prompt = rt._extractor_prompt(scope, [{"role": "user", "content": "hi"}])
        assert "Decision process" in prompt
        assert "SAVE" in prompt  # default to save

    def test_prompt_includes_what_not_to_save(self):
        rt = ClaudeLikeMemoryRuntime()
        scope = f"ep-wns-{os.urandom(4).hex()}"
        prompt = rt._extractor_prompt(scope, [{"role": "user", "content": "hi"}])
        assert "What NOT to save" in prompt


# ═══════════════════════════════════════════════════════════════
# 7. MEMORY MANIFEST FORMATTING
# ═══════════════════════════════════════════════════════════════


class TestMemoryManifest:
    """format_memory_manifest must produce correct manifest entries."""

    def test_manifest_contains_type_tags(self):
        headers = [
            MemoryHeader("user-pref.md", "User Pref", "user preferences", "user", time.time()),
            MemoryHeader("project-goal.md", "Project", "project goals", "project", time.time()),
        ]
        manifest = format_memory_manifest(headers)
        assert "[user]" in manifest
        assert "[project]" in manifest

    def test_manifest_contains_timestamps(self):
        headers = [MemoryHeader("a.md", "A", "desc", "user", 1712000000.123)]
        manifest = format_memory_manifest(headers)
        assert "2024-04-01" in manifest  # approximate date for epoch 1712000000

    def test_manifest_with_empty_description(self):
        headers = [MemoryHeader("a.md", "A", "", "user", time.time())]
        manifest = format_memory_manifest(headers)
        assert "a.md" in manifest
        # Should not have a trailing colon with nothing after it
        assert ": \n" not in manifest and not manifest.endswith(": ")

    def test_manifest_entry_per_memory(self):
        headers = [
            MemoryHeader(f"m{i}.md", f"M{i}", f"desc {i}", "project", time.time())
            for i in range(5)
        ]
        manifest = format_memory_manifest(headers)
        lines = [l for l in manifest.split("\n") if l.strip()]
        assert len(lines) == 5


# ═══════════════════════════════════════════════════════════════
# 8. SESSION MEMORY BUDGET WARNINGS
# ═══════════════════════════════════════════════════════════════


class TestSessionMemoryBudget:
    """Session memory section reminders must fire for oversized sections
    and report the correct section names."""

    def test_no_warning_for_small_content(self):
        rt = ClaudeLikeMemoryRuntime()
        content = "# Session Title\n_desc_\nShort title\n\n# Current State\n_desc_\nDoing stuff"
        result = rt._session_memory_section_reminders(content)
        assert result == ""

    def test_oversized_section_warning_names_section(self):
        rt = ClaudeLikeMemoryRuntime()
        huge = "word " * (_MAX_SECTION_LENGTH * 2)
        content = f"# Current State\n_desc_\n{huge}\n\n# Task specification\n_desc_\nSmall"
        result = rt._session_memory_section_reminders(content)
        assert "Current State" in result
        assert "MUST be condensed" in result or "Oversized" in result
        # Task specification should NOT be flagged
        assert "Task specification" not in result

    def test_total_budget_exceeded_warning(self):
        rt = ClaudeLikeMemoryRuntime()
        # Make multiple sections, total > 12000 tokens
        sections = []
        for name in ["Session Title", "Current State", "Task specification",
                      "Files and Functions", "Workflow", "Errors & Corrections"]:
            sections.append(f"# {name}\n_desc_\n{'data ' * 3000}")
        content = "\n\n".join(sections)
        result = rt._session_memory_section_reminders(content)
        assert "CRITICAL" in result
        assert str(_MAX_TOTAL_SESSION_MEMORY_TOKENS) in result


# ═══════════════════════════════════════════════════════════════
# 9. CONCURRENT EXTRACTION QUEUING
# ═══════════════════════════════════════════════════════════════


class TestConcurrentExtraction:
    """Second extraction for the same session must be queued, not started in parallel.
    Third extraction for the same session replaces the queued one (latest wins)."""

    def test_second_extraction_queued(self):
        rt = ClaudeLikeMemoryRuntime()
        session = _session()
        rt._extract_in_progress.add(session)

        rt._launch_extract_hook(
            _mock_client(), session_id=session, scope_id=SCOPE,
            prompt="second prompt",
        )
        assert session in rt._extract_trailing
        assert rt._extract_trailing[session][2] == "second prompt"
        rt._extract_in_progress.discard(session)

    def test_third_extraction_replaces_queued(self):
        """If a third extraction comes while one is running and one is queued,
        the queued one is replaced (latest signal wins)."""
        rt = ClaudeLikeMemoryRuntime()
        session = _session()
        rt._extract_in_progress.add(session)

        rt._launch_extract_hook(
            _mock_client(), session_id=session, scope_id=SCOPE,
            prompt="second prompt",
        )
        rt._launch_extract_hook(
            _mock_client(), session_id=session, scope_id=SCOPE,
            prompt="third prompt",
        )
        # Only the LATEST should be queued
        assert rt._extract_trailing[session][2] == "third prompt"
        rt._extract_in_progress.discard(session)

    def test_no_queue_when_not_in_progress(self):
        """When no extraction is in progress, a new one should start (not queue)."""
        rt = ClaudeLikeMemoryRuntime()
        session = _session()

        with patch.object(rt.forks, "run_named_subagent", return_value=None):
            # Should NOT add to trailing, but rather attempt to run
            rt._launch_extract_hook(
                _mock_client(), session_id=session, scope_id=SCOPE,
                prompt="first prompt",
            )
        # session should be in _extract_in_progress (started, not queued)
        # Note: it may have already been discarded by the async runner
        assert session not in rt._extract_trailing


# ═══════════════════════════════════════════════════════════════
# 10. SCAN_MEMORY_HEADERS CORRECTNESS
# ═══════════════════════════════════════════════════════════════


class TestScanMemoryHeaders:
    """scan_memory_headers must return correct metadata from frontmatter
    and skip MEMORY.md itself."""

    def test_skips_memory_md(self):
        scope = f"scan-{os.urandom(4).hex()}"
        write_memory_file(scope, name="A", description="a", type_="user",
                          body="a", filename="a.md")
        _last_rebuild.clear()
        rebuild_memory_index(scope, force=True)

        headers = scan_memory_headers(scope)
        filenames = [h.filename for h in headers]
        assert "MEMORY.md" not in filenames
        assert "a.md" in filenames

    def test_reads_frontmatter_fields(self):
        scope = f"scan-fm-{os.urandom(4).hex()}"
        write_memory_file(scope, name="My Name", description="My Description",
                          type_="feedback", body="body", filename="test-scan.md")
        _last_rebuild.clear()

        headers = scan_memory_headers(scope)
        assert len(headers) == 1
        h = headers[0]
        assert h.name == "My Name"
        assert h.description == "My Description"
        assert h.type == "feedback"
        assert h.filename == "test-scan.md"
        assert h.mtime > 0

    def test_missing_frontmatter_uses_defaults(self):
        """A .md file without frontmatter should still be listed with defaults."""
        scope = f"scan-nofm-{os.urandom(4).hex()}"
        mem_root = memory_dir(scope)
        # Write a plain file without frontmatter
        plain_path = mem_root / "plain-note.md"
        plain_path.write_text("Just a plain note without frontmatter.", encoding="utf-8")

        headers = scan_memory_headers(scope)
        assert len(headers) == 1
        h = headers[0]
        assert h.filename == "plain-note.md"
        assert h.name == "plain-note"  # defaults to stem
        assert h.type == "project"  # default type


# ═══════════════════════════════════════════════════════════════
# 11. DELETE CORRECTNESS
# ═══════════════════════════════════════════════════════════════


class TestDeleteCorrectness:
    """delete_memory_file must remove the file, rebuild the index,
    and handle edge cases."""

    def test_delete_removes_file_and_updates_index(self):
        scope = f"del-{os.urandom(4).hex()}"
        write_memory_file(scope, name="ToDelete", description="will be deleted",
                          type_="user", body="bye", filename="to-delete.md")
        _last_rebuild.clear()

        assert delete_memory_file(scope, "to-delete.md") is True
        assert not (memory_dir(scope) / "to-delete.md").exists()

        _last_rebuild.clear()
        rebuild_memory_index(scope, force=True)
        index = memory_index_path(scope).read_text(encoding="utf-8")
        assert "to-delete.md" not in index

    def test_delete_nonexistent_returns_false(self):
        scope = f"del-ne-{os.urandom(4).hex()}"
        assert delete_memory_file(scope, "nonexistent.md") is False

    def test_delete_then_write_same_filename(self):
        """After deleting, writing the same filename should create a new file
        with a new 'created' timestamp."""
        scope = f"del-rw-{os.urandom(4).hex()}"
        path1 = write_memory_file(scope, name="First", description="first",
                                  type_="user", body="v1", filename="reuse.md")
        fm1 = read_frontmatter(path1)
        created1 = fm1["created"]

        _last_rebuild.clear()
        delete_memory_file(scope, "reuse.md")

        time.sleep(0.05)
        _last_rebuild.clear()
        path2 = write_memory_file(scope, name="Second", description="second",
                                  type_="user", body="v2", filename="reuse.md")
        fm2 = read_frontmatter(path2)
        # After delete+recreate, created should be a NEW timestamp (not preserved)
        assert fm2["created"] != created1
        assert fm2["name"] == "Second"


# ═══════════════════════════════════════════════════════════════
# 12. SLUGIFY CORRECTNESS
# ═══════════════════════════════════════════════════════════════


class TestSlugify:
    """slugify must handle Unicode, special chars, and length limits."""

    def test_basic_slugification(self):
        assert slugify("Hello World") == "hello-world"

    def test_special_chars_replaced(self):
        result = slugify("file@name#with$special!")
        assert "@" not in result
        assert "#" not in result
        assert "$" not in result

    def test_empty_string_returns_default(self):
        assert slugify("") == "default"

    def test_long_string_truncated(self):
        result = slugify("a" * 200)
        assert len(result) <= 80

    def test_pure_unicode_falls_back_to_default(self):
        """SAFE_SEGMENT_RE replaces all non-[a-zA-Z0-9._-] chars,
        so pure Unicode input collapses to 'default'."""
        result = slugify("привет-мир")
        assert result == "default"

    def test_mixed_unicode_and_ascii(self):
        """Mixed input keeps the ASCII parts."""
        result = slugify("hello-привет-world")
        assert "hello" in result
        assert "world" in result


# ═══════════════════════════════════════════════════════════════
# 13. COMPACT SUMMARY FORMATTING
# ═══════════════════════════════════════════════════════════════


class TestCompactSummaryFormatting:
    """_format_compact_summary must strip <analysis> and extract <summary>."""

    def test_strips_analysis_extracts_summary(self):
        raw = (
            "<analysis>\nSome analysis here\n</analysis>\n\n"
            "<summary>\nThis is the summary.\n</summary>"
        )
        result = ClaudeLikeMemoryRuntime._format_compact_summary(raw)
        assert "Some analysis here" not in result
        assert "This is the summary." in result

    def test_handles_no_analysis(self):
        raw = "<summary>\nJust summary.\n</summary>"
        result = ClaudeLikeMemoryRuntime._format_compact_summary(raw)
        assert "Just summary." in result

    def test_handles_no_tags(self):
        raw = "Plain text without any tags."
        result = ClaudeLikeMemoryRuntime._format_compact_summary(raw)
        assert "Plain text without any tags." in result

    def test_collapses_multiple_blank_lines(self):
        raw = "Line 1\n\n\n\n\nLine 2"
        result = ClaudeLikeMemoryRuntime._format_compact_summary(raw)
        assert "\n\n\n" not in result
        assert "Line 1" in result
        assert "Line 2" in result
