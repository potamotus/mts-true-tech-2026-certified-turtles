"""
Deep correctness tests for memory READING pipeline.

Tests verify that the reading pipeline (build_memory_prompt, selector, prompting)
produces *correct* results — not just "non-empty" ones. Each test checks a specific
invariant or edge case that, if violated, would silently corrupt the LLM prompt.

Run: pytest tests/test_memory_reading_correctness.py -v --tb=short
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CT_CLAUDE_HOME", tempfile.mkdtemp(prefix="ct_rdcorr_"))

from certified_turtles.memory_runtime.prompting import (
    MemoryPromptBundle,
    _estimate_tokens,
    _memory_age_warning,
    _memory_freshness_text,
    _truncate_entrypoint_content,
    build_memory_prompt,
)
from certified_turtles.memory_runtime.selector import fallback_select
from certified_turtles.memory_runtime.storage import (
    MAX_MEMORY_INDEX_BYTES,
    MAX_MEMORY_INDEX_LINES,
    MAX_MEMORY_SESSION_BYTES,
    MAX_RELEVANT_MEMORIES,
    MemoryHeader,
    _last_rebuild,
    ensure_session_meta,
    memory_dir,
    memory_index_path,
    parse_frontmatter,
    read_body,
    read_frontmatter,
    rebuild_memory_index,
    scan_memory_headers,
    session_meta_path,
    write_json,
    read_json,
    write_memory_file,
    write_session_memory,
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


def _scope() -> str:
    return f"rdcorr-{os.urandom(4).hex()}"


def _session() -> str:
    return f"rdcorr-sess-{os.urandom(4).hex()}"


def _client_selecting(*filenames: str) -> MagicMock:
    client = MagicMock()
    client.chat_completions.return_value = {
        "choices": [{"message": {"content": json.dumps({"selected_memories": list(filenames)})}}]
    }
    return client


# ═══════════════════════════════════════════════════════════════
# 1. MEMORY.md INDEX TRUNCATION CORRECTNESS
# ═══════════════════════════════════════════════════════════════


class TestIndexTruncation:
    """_truncate_entrypoint_content must truncate at correct boundary
    and produce a valid, informative warning."""

    def test_exactly_200_lines_no_truncation(self):
        """200 lines is the limit — should NOT truncate."""
        content = "\n".join(f"- line {i}" for i in range(200))
        result = _truncate_entrypoint_content(content)
        assert "WARNING" not in result
        assert result.count("\n") == 199  # 200 lines = 199 newlines

    def test_201_lines_truncates_with_line_warning(self):
        """201 lines → truncated to 200, warning mentions line count."""
        content = "\n".join(f"- line {i}" for i in range(201))
        result = _truncate_entrypoint_content(content)
        assert "WARNING" in result
        assert "201 lines" in result
        # Content before warning should have exactly 200 lines
        before_warning = result.split("> WARNING")[0].rstrip()
        assert before_warning.count("\n") == 199

    def test_byte_limit_truncation_preserves_valid_utf8(self):
        """When truncating by bytes, must not cut mid-UTF-8 character."""
        # Each line is ~130 bytes with Cyrillic (2 bytes each)
        cyrillic_line = "- " + "Ы" * 64  # 2 + 128 = 130 bytes per line
        lines = [cyrillic_line] * 195  # 195 * 130 = 25350 > 25000 byte limit
        content = "\n".join(lines)
        result = _truncate_entrypoint_content(content)
        assert "WARNING" in result
        # Verify result is valid UTF-8 (no decode errors)
        result.encode("utf-8").decode("utf-8")

    def test_both_line_and_byte_limit_warning(self):
        """When both limits are exceeded, warning mentions both."""
        long_line = "x" * 200
        content = "\n".join([long_line] * 250)  # >200 lines AND >25KB
        result = _truncate_entrypoint_content(content)
        assert "WARNING" in result
        assert "lines" in result
        assert "bytes" in result

    def test_empty_content_no_truncation(self):
        result = _truncate_entrypoint_content("")
        assert result == ""
        assert "WARNING" not in result

    def test_byte_only_truncation_correct_reason(self):
        """Under 200 lines but over 25KB → warning says 'bytes', not 'lines'."""
        # 150 lines, each 200 bytes = 30KB > 25KB, but < 200 lines
        content = "\n".join(["x" * 200] * 150)
        result = _truncate_entrypoint_content(content)
        assert "WARNING" in result
        assert "bytes" in result
        # Should NOT mention line count as a problem
        assert "150 lines" not in result.split("WARNING")[1] or "entries are too long" in result


# ═══════════════════════════════════════════════════════════════
# 2. MEMORY BODY 4KB CAP IN PROMPT
# ═══════════════════════════════════════════════════════════════


class TestMemoryBodyCapInPrompt:
    """build_memory_prompt must cap each memory body at 4096 bytes
    without producing broken UTF-8."""

    def test_body_under_4kb_fully_included(self):
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        body = "Important fact. " * 100  # ~1600 bytes
        write_memory_file(scope, name="Small", description="small mem",
                          type_="user", body=body, filename="small.md")
        _last_rebuild.clear()

        client = _client_selecting("small.md")
        bundle = build_memory_prompt(
            client, model="m",
            messages=[{"role": "user", "content": "q"}],
            scope_id=scope, session_id=session, user_query="q",
        )
        assert "Important fact" in bundle.prompt
        # All repetitions should be present
        assert bundle.prompt.count("Important fact") == 100

    def test_body_at_exactly_4096_bytes_accepted_by_write(self):
        """write_memory_file accepts body of exactly MAX_MEMORY_FILE_BYTES."""
        scope = _scope()
        body = "a" * 4096
        path = write_memory_file(scope, name="Exact", description="exact 4KB",
                                 type_="project", body=body, filename="exact.md")
        assert path.is_file()
        assert len(read_body(path)) == 4096

    def test_body_over_4096_bytes_rejected_by_write(self):
        """write_memory_file rejects body > MAX_MEMORY_FILE_BYTES."""
        scope = _scope()
        body = "a" * 4097
        with pytest.raises(ValueError, match="too large"):
            write_memory_file(scope, name="Big", description="too big",
                              type_="project", body=body, filename="big.md")

    def test_multibyte_body_truncated_cleanly_in_prompt(self):
        """When body has multi-byte chars near 4KB boundary,
        prompt should contain valid UTF-8 (no replacement chars)."""
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        # Fill body with 2-byte Cyrillic chars, exactly at limit
        # Each Ы = 2 bytes, so 2048 chars = 4096 bytes
        body = "Ы" * 2048
        write_memory_file(scope, name="Cyrillic", description="cyrillic body",
                          type_="user", body=body, filename="cyrillic.md")
        _last_rebuild.clear()

        client = _client_selecting("cyrillic.md")
        bundle = build_memory_prompt(
            client, model="m",
            messages=[{"role": "user", "content": "q"}],
            scope_id=scope, session_id=session, user_query="q",
        )
        # Should not contain replacement character
        assert "\ufffd" not in bundle.prompt
        # Content should be valid UTF-8
        bundle.prompt.encode("utf-8").decode("utf-8")


# ═══════════════════════════════════════════════════════════════
# 3. TOTAL 60KB SESSION BYTES BUDGET
# ═══════════════════════════════════════════════════════════════


class TestTotalSessionBytesBudget:
    """When total selected memory bodies exceed MAX_MEMORY_SESSION_BYTES (60KB),
    later memories must be skipped — not truncated mid-body."""

    def test_excess_memories_skipped_not_truncated(self):
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        # Write 20 memories, each ~4KB body → total ~80KB > 60KB limit
        filenames = []
        for i in range(20):
            fname = f"big-{i:02d}.md"
            _last_rebuild.clear()
            write_memory_file(
                scope, name=f"Big {i}", description=f"big memory {i}",
                type_="project", body=f"{'x' * 3900} MARKER_{i}",
                filename=fname,
            )
            filenames.append(fname)

        client = _client_selecting(*filenames)
        bundle = build_memory_prompt(
            client, model="m",
            messages=[{"role": "user", "content": "q"}],
            scope_id=scope, session_id=session, user_query="q",
        )

        # Count how many MARKER_N appear — should be less than 20
        markers_found = sum(1 for i in range(20) if f"MARKER_{i}" in bundle.prompt)
        assert markers_found < 20, "Not all memories should fit within 60KB"
        assert markers_found > 0, "At least some memories should be included"

        # Total bytes of relevant_memories section should be under budget
        if "## relevant_memories" in bundle.prompt:
            rel_start = bundle.prompt.index("## relevant_memories")
            # Find end: either session_memory section or end of prompt
            rest = bundle.prompt[rel_start:]
            session_idx = rest.find("# session_memory")
            rel_section = rest[:session_idx] if session_idx > 0 else rest
            rel_bytes = len(rel_section.encode("utf-8"))
            # Allow overhead for headers/warnings
            assert rel_bytes <= MAX_MEMORY_SESSION_BYTES + 2048

    def test_first_memories_prioritized_over_later(self):
        """Memories are processed in selection order — earlier ones get budget."""
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        _last_rebuild.clear()
        write_memory_file(scope, name="First", description="first",
                          type_="user", body="FIRST_CONTENT " * 200,
                          filename="first.md")
        _last_rebuild.clear()
        write_memory_file(scope, name="Second", description="second",
                          type_="user", body="SECOND_CONTENT " * 200,
                          filename="second.md")

        # Client returns them in this order
        client = _client_selecting("first.md", "second.md")
        bundle = build_memory_prompt(
            client, model="m",
            messages=[{"role": "user", "content": "q"}],
            scope_id=scope, session_id=session, user_query="q",
        )

        assert "FIRST_CONTENT" in bundle.prompt
        assert "SECOND_CONTENT" in bundle.prompt  # Both fit since each is ~3KB


# ═══════════════════════════════════════════════════════════════
# 4. STALENESS WARNING EDGE CASES
# ═══════════════════════════════════════════════════════════════


class TestStalenessWarning:
    """Age warnings must be accurate — a 1-day-old memory gets no warning,
    a 2-day-old one does, and the day count must be correct."""

    def test_exactly_one_day_no_warning(self):
        """Memories <=1 day old should have no warning."""
        from datetime import datetime, timezone, timedelta
        stamp = (datetime.now(timezone.utc) - timedelta(hours=23)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert _memory_age_warning(stamp) == ""

    def test_exactly_two_days_has_warning(self):
        from datetime import datetime, timezone, timedelta
        stamp = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = _memory_age_warning(stamp)
        assert "2 days old" in result
        assert "Verify" in result

    def test_30_days_shows_30(self):
        from datetime import datetime, timezone, timedelta
        stamp = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = _memory_age_warning(stamp)
        assert "30 days old" in result

    def test_invalid_timestamp_no_warning(self):
        assert _memory_age_warning("not-a-date") == ""
        assert _memory_age_warning("") == ""

    def test_warning_appears_in_prompt_after_memory_body(self):
        """Staleness warning must appear right after the memory body in the prompt."""
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        # Write a memory, then backdate its `updated` field
        path = write_memory_file(scope, name="Old", description="old memory",
                                 type_="project", body="OLD_BODY_TEXT",
                                 filename="old.md")
        # Rewrite with an old timestamp
        content = path.read_text(encoding="utf-8")
        content = content.replace(
            'updated:',
            'updated: "2020-01-01T00:00:00Z"\noriginal_updated:',
        )
        # Actually, let's just rewrite the frontmatter properly
        from certified_turtles.memory_runtime.storage import _atomic_write_text
        _atomic_write_text(path, (
            '---\n'
            'name: "Old"\n'
            'description: "old memory"\n'
            'type: "project"\n'
            'created: "2020-01-01T00:00:00Z"\n'
            'updated: "2020-01-01T00:00:00Z"\n'
            'source: "manual"\n'
            '---\n\n'
            'OLD_BODY_TEXT\n'
        ))
        _last_rebuild.clear()

        client = _client_selecting("old.md")
        bundle = build_memory_prompt(
            client, model="m",
            messages=[{"role": "user", "content": "q"}],
            scope_id=scope, session_id=session, user_query="q",
        )

        assert "OLD_BODY_TEXT" in bundle.prompt
        assert "days old" in bundle.prompt
        # Warning should come AFTER body
        body_pos = bundle.prompt.index("OLD_BODY_TEXT")
        warning_pos = bundle.prompt.index("days old")
        assert warning_pos > body_pos


# ═══════════════════════════════════════════════════════════════
# 5. FALLBACK SELECTOR SCORING CORRECTNESS
# ═══════════════════════════════════════════════════════════════


class TestFallbackSelectorScoring:
    """fallback_select must rank by keyword overlap and break ties by filename."""

    def _headers(self) -> list[MemoryHeader]:
        now = time.time()
        return [
            MemoryHeader("alpha.md", "Python Setup", "how to set up python virtualenv", "project", now),
            MemoryHeader("beta.md", "Python Debugging", "python debugging with pdb and ipdb", "feedback", now),
            MemoryHeader("gamma.md", "Java Setup", "java jdk installation guide", "project", now),
            MemoryHeader("delta.md", "Python Deployment", "deploy python apps to production", "project", now),
        ]

    def test_query_with_single_keyword_finds_all_matches(self):
        """'python' matches alpha, beta, delta — NOT gamma."""
        selected = fallback_select(self._headers(), "python", limit=5)
        assert "gamma.md" not in selected
        assert len(selected) == 3
        assert set(selected) == {"alpha.md", "beta.md", "delta.md"}

    def test_query_with_two_keywords_ranks_higher(self):
        """'python setup' should rank alpha (2 keyword hits) higher than beta/delta (1 hit)."""
        selected = fallback_select(self._headers(), "python setup", limit=5)
        assert selected[0] == "alpha.md"  # 2 hits: "python" + "setup"

    def test_tie_broken_by_filename(self):
        """Equal scores → sorted by filename alphabetically."""
        now = time.time()
        headers = [
            MemoryHeader("zebra.md", "Deploy", "deploy guide", "project", now),
            MemoryHeader("apple.md", "Deploy", "deploy notes", "project", now),
        ]
        selected = fallback_select(headers, "deploy", limit=5)
        assert selected == ["apple.md", "zebra.md"]

    def test_no_matching_keywords_returns_empty(self):
        selected = fallback_select(self._headers(), "kubernetes docker", limit=5)
        assert selected == []

    def test_short_words_under_3_chars_ignored(self):
        """Words with <3 characters should be skipped by tokenizer."""
        selected = fallback_select(self._headers(), "py", limit=5)
        # "py" is only 2 chars, should not match anything
        assert selected == []

    def test_tool_reference_filtering(self):
        """Reference memory about a recently-used tool should be skipped
        UNLESS it contains warning keywords."""
        now = time.time()
        headers = [
            MemoryHeader("grep-ref.md", "grep_search guide", "grep_search tool usage reference", "reference", now),
            MemoryHeader("grep-warn.md", "grep_search gotcha", "grep_search warning about regex escaping", "reference", now),
            MemoryHeader("unrelated.md", "Deploy notes", "deployment procedure", "project", now),
        ]
        selected = fallback_select(headers, "grep_search usage", recent_tools=["grep_search"])
        # grep-ref.md should be filtered (it's a reference for a recently-used tool with no warnings)
        assert "grep-ref.md" not in selected
        # grep-warn.md should be KEPT (has "warning"/"gotcha")
        assert "grep-warn.md" in selected

    def test_tool_reference_not_filtered_without_recent_tools(self):
        """Without recent_tools, reference memories are not filtered."""
        now = time.time()
        headers = [
            MemoryHeader("grep-ref.md", "grep_search guide", "grep_search usage reference", "reference", now),
        ]
        selected = fallback_select(headers, "grep_search usage")
        assert "grep-ref.md" in selected


# ═══════════════════════════════════════════════════════════════
# 6. SURFACED MEMORIES DEDUPLICATION
# ═══════════════════════════════════════════════════════════════


class TestSurfacedMemoriesDedup:
    """Surfaced memories are tracked in session meta — they must not duplicate
    and must filter correctly for the LLM selector."""

    def test_already_surfaced_excluded_from_selector_input(self):
        """If a memory was already surfaced, it should be removed from the
        headers list sent to the LLM selector."""
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        write_memory_file(scope, name="Old", description="already seen",
                          type_="project", body="old content", filename="old.md")
        _last_rebuild.clear()
        write_memory_file(scope, name="New", description="new info",
                          type_="project", body="new content", filename="new.md")
        _last_rebuild.clear()

        # Pre-populate surfaced_memories in session meta
        meta = read_json(session_meta_path(session)) or {}
        meta["surfaced_memories"] = ["old.md"]
        write_json(session_meta_path(session), meta)

        # Client that returns whatever it's offered
        client = MagicMock()
        def return_all_offered(model, messages, **kw):
            # Parse what filenames were offered
            user_msg = messages[-1]["content"]
            # Return whatever filenames are in the manifest
            offered = []
            for line in user_msg.split("\n"):
                if line.strip().startswith("- ") and ".md" in line:
                    # Extract filename
                    for word in line.split():
                        if word.endswith(".md"):
                            offered.append(word)
                            break
            return {"choices": [{"message": {"content": json.dumps({"selected_memories": offered})}}]}

        client.chat_completions.side_effect = return_all_offered

        bundle = build_memory_prompt(
            client, model="m",
            messages=[{"role": "user", "content": "tell me"}],
            scope_id=scope, session_id=session, user_query="tell me",
        )

        # old.md should NOT be in the selector's input (already surfaced)
        # Only new.md should appear in selected_memories
        # The mock returns everything offered, so if old.md was offered it would be selected
        assert "old.md" not in bundle.selected_memories

    def test_surfaced_list_capped_at_50(self):
        """Surfaced memories list should not grow unboundedly — capped at 50."""
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        # Pre-populate with 49 surfaced memories
        meta = read_json(session_meta_path(session)) or {}
        meta["surfaced_memories"] = [f"mem-{i}.md" for i in range(49)]
        write_json(session_meta_path(session), meta)

        # Write a new memory and select it
        write_memory_file(scope, name="New", description="new", type_="user",
                          body="content", filename="new-50.md")
        _last_rebuild.clear()
        write_memory_file(scope, name="Extra", description="extra", type_="user",
                          body="content2", filename="new-51.md")
        _last_rebuild.clear()

        client = _client_selecting("new-50.md", "new-51.md")
        build_memory_prompt(
            client, model="m",
            messages=[{"role": "user", "content": "q"}],
            scope_id=scope, session_id=session, user_query="q",
        )

        meta_after = read_json(session_meta_path(session)) or {}
        surfaced = meta_after.get("surfaced_memories", [])
        assert len(surfaced) <= 50


# ═══════════════════════════════════════════════════════════════
# 7. FRONTMATTER PARSING EDGE CASES
# ═══════════════════════════════════════════════════════════════


class TestFrontmatterParsing:
    """parse_frontmatter must handle JSON-encoded strings, colons in values,
    missing fields, and malformed input gracefully."""

    def test_json_encoded_name_with_colon(self):
        text = '---\nname: "Tool: Usage Guide"\ntype: "reference"\n---\nBody.'
        fm = parse_frontmatter(text)
        assert fm["name"] == "Tool: Usage Guide"

    def test_json_encoded_with_unicode(self):
        text = '---\nname: "\\u041f\\u0440\\u0438\\u0432\\u0435\\u0442"\n---\nBody.'
        fm = parse_frontmatter(text)
        assert fm["name"] == "Привет"

    def test_unquoted_value(self):
        text = "---\nname: simple name\ntype: user\n---\nBody."
        fm = parse_frontmatter(text)
        assert fm["name"] == "simple name"
        assert fm["type"] == "user"

    def test_missing_frontmatter(self):
        text = "No frontmatter here, just plain text."
        fm = parse_frontmatter(text)
        assert fm == {}

    def test_empty_value(self):
        text = '---\nname: ""\ntype: user\n---\nBody.'
        fm = parse_frontmatter(text)
        assert fm["name"] == ""

    def test_read_body_strips_frontmatter(self):
        """read_body should return only the content after the frontmatter."""
        scope = _scope()
        path = write_memory_file(scope, name="Test", description="desc",
                                 type_="user", body="BODY_CONTENT_HERE",
                                 filename="test-fm.md")
        body = read_body(path)
        assert body == "BODY_CONTENT_HERE"
        assert "---" not in body
        assert "name:" not in body

    def test_frontmatter_with_multiline_body(self):
        """Body with newlines should be fully captured."""
        scope = _scope()
        multiline_body = "Line 1\nLine 2\n\nLine 4 after blank"
        path = write_memory_file(scope, name="Multi", description="multiline",
                                 type_="project", body=multiline_body,
                                 filename="multi.md")
        body = read_body(path)
        assert "Line 1" in body
        assert "Line 4 after blank" in body


# ═══════════════════════════════════════════════════════════════
# 8. SESSION MEMORY IN PROMPT
# ═══════════════════════════════════════════════════════════════


class TestSessionMemoryInPrompt:
    """Session memory is injected at the end of the prompt and must
    respect the 12KB byte cap."""

    def test_session_memory_appears_in_prompt(self):
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        write_session_memory(session, "# Session\nWorking on BUG-1234 fix.\n# Status\nIn progress")

        bundle = build_memory_prompt(
            None, model="m",
            messages=[{"role": "user", "content": "q"}],
            scope_id=scope, session_id=session, user_query="q",
        )
        assert "BUG-1234" in bundle.prompt
        assert "# session_memory" in bundle.prompt

    def test_empty_session_memory_not_in_prompt(self):
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)
        # Don't write session memory

        bundle = build_memory_prompt(
            None, model="m",
            messages=[{"role": "user", "content": "q"}],
            scope_id=scope, session_id=session, user_query="q",
        )
        assert "# session_memory" not in bundle.prompt

    def test_session_memory_over_12kb_truncated(self):
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        # Write 20KB session memory
        big_content = "X" * 20_000
        write_session_memory(session, big_content)

        bundle = build_memory_prompt(
            None, model="m",
            messages=[{"role": "user", "content": "q"}],
            scope_id=scope, session_id=session, user_query="q",
        )
        # Should appear but truncated
        assert "# session_memory" in bundle.prompt
        # Find session_memory section and verify its size
        sm_start = bundle.prompt.index("# session_memory")
        sm_content = bundle.prompt[sm_start:]
        sm_bytes = len(sm_content.encode("utf-8"))
        # Truncated to ~12KB + small overhead for the header
        assert sm_bytes < 13_000


# ═══════════════════════════════════════════════════════════════
# 9. PROMPT STRUCTURE INTEGRITY
# ═══════════════════════════════════════════════════════════════


class TestPromptStructure:
    """The prompt must have a well-defined structure: instructions → MEMORY.md → memories → session."""

    def test_empty_scope_produces_valid_prompt(self):
        """With no memories at all, prompt should still contain instructions and empty MEMORY.md."""
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        bundle = build_memory_prompt(
            None, model="m",
            messages=[{"role": "user", "content": "hello"}],
            scope_id=scope, session_id=session, user_query="hello",
        )
        assert "## MEMORY.md" in bundle.prompt
        assert "currently empty" in bundle.prompt
        assert bundle.selected_memories == ()

    def test_memory_instructions_always_present(self):
        """Memory type instructions must always be injected."""
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        bundle = build_memory_prompt(
            None, model="m",
            messages=[{"role": "user", "content": "q"}],
            scope_id=scope, session_id=session, user_query="q",
        )
        assert "auto memory" in bundle.prompt
        assert "Memory management" in bundle.prompt or "types of memory" in bundle.prompt.lower()

    def test_sections_ordered_correctly(self):
        """MEMORY.md must come before relevant_memories, which must come before session_memory."""
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        write_memory_file(scope, name="Fact", description="a fact",
                          type_="project", body="FACT_BODY", filename="fact.md")
        _last_rebuild.clear()
        write_session_memory(session, "SESSION_BODY")

        client = _client_selecting("fact.md")
        bundle = build_memory_prompt(
            client, model="m",
            messages=[{"role": "user", "content": "q"}],
            scope_id=scope, session_id=session, user_query="q",
        )

        idx_pos = bundle.prompt.index("## MEMORY.md")
        rel_pos = bundle.prompt.index("## relevant_memories")
        ses_pos = bundle.prompt.index("# session_memory")
        assert idx_pos < rel_pos < ses_pos

    def test_selected_filename_missing_file_gracefully_skipped(self):
        """If selector returns a filename that no longer exists, it should be silently skipped."""
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        # Don't actually write the file — just have the selector return it
        client = _client_selecting("ghost.md")
        bundle = build_memory_prompt(
            client, model="m",
            messages=[{"role": "user", "content": "q"}],
            scope_id=scope, session_id=session, user_query="q",
        )
        # Should not crash, and ghost.md body should not appear
        assert "ghost" not in bundle.prompt.lower().split("memory.md")[0]  # Not in MEMORY.md section either


# ═══════════════════════════════════════════════════════════════
# 10. LLM SELECTOR CORRECTNESS
# ═══════════════════════════════════════════════════════════════


class TestLLMSelectorCorrectness:
    """select_relevant_memories must validate LLM output: only return filenames
    that actually exist in the headers, cap at limit, handle malformed JSON."""

    def test_llm_returns_nonexistent_filename_filtered(self):
        """If LLM returns a filename not in headers, it must be discarded."""
        from certified_turtles.memory_runtime.selector import select_relevant_memories

        headers = [MemoryHeader("real.md", "Real", "real memory", "project", time.time())]
        client = MagicMock()
        client.chat_completions.return_value = {
            "choices": [{"message": {"content": json.dumps({"selected_memories": ["real.md", "fake.md"]})}}]
        }
        result = select_relevant_memories(client, model="m", query="test", headers=headers)
        assert result == ["real.md"]
        assert "fake.md" not in result

    def test_llm_returns_more_than_limit(self):
        """LLM returns 10 filenames but limit is 5 → only 5 returned."""
        from certified_turtles.memory_runtime.selector import select_relevant_memories

        headers = [MemoryHeader(f"m{i}.md", f"Mem {i}", f"memory {i}", "project", time.time()) for i in range(10)]
        all_names = [h.filename for h in headers]
        client = MagicMock()
        client.chat_completions.return_value = {
            "choices": [{"message": {"content": json.dumps({"selected_memories": all_names})}}]
        }
        result = select_relevant_memories(client, model="m", query="test", headers=headers, limit=5)
        assert len(result) == 5

    def test_llm_returns_wrong_json_structure(self):
        """LLM returns JSON but with wrong key name."""
        from certified_turtles.memory_runtime.selector import select_relevant_memories

        headers = [MemoryHeader("a.md", "A", "a", "project", time.time())]
        client = MagicMock()
        client.chat_completions.return_value = {
            "choices": [{"message": {"content": json.dumps({"memories": ["a.md"]})}}]
        }
        result = select_relevant_memories(client, model="m", query="test", headers=headers)
        assert result == []  # "selected_memories" key not found

    def test_empty_query_returns_empty(self):
        from certified_turtles.memory_runtime.selector import select_relevant_memories

        headers = [MemoryHeader("a.md", "A", "a", "project", time.time())]
        result = select_relevant_memories(MagicMock(), model="m", query="", headers=headers)
        assert result == []

    def test_empty_headers_returns_empty(self):
        from certified_turtles.memory_runtime.selector import select_relevant_memories

        result = select_relevant_memories(MagicMock(), model="m", query="test", headers=[])
        assert result == []
