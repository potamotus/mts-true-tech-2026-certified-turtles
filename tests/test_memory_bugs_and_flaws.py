"""
CRITICAL correctness audit: bugs and design flaws in memory extraction & search.

Each test documents a specific problem — a real bug or a logical flaw that causes
the system to behave incorrectly under real-world conditions.

Tests are grouped:
  BUG_*   — actual bugs: the system does something objectively WRONG
  FLAW_*  — design flaws: the system produces logically incorrect results by design

Tests that demonstrate bugs are marked with xfail(reason="BUG: ...") —
they FAIL until the bug is fixed, then the xfail is removed.

Run: pytest tests/test_memory_bugs_and_flaws.py -v --tb=short
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CT_CLAUDE_HOME", tempfile.mkdtemp(prefix="ct_bugs_"))

from certified_turtles.memory_runtime.prompting import (
    _memory_age_warning,
    build_memory_prompt,
)
from certified_turtles.memory_runtime.selector import (
    _tokenize,
    fallback_select,
    select_relevant_memories,
)
from certified_turtles.memory_runtime.storage import (
    MemoryHeader,
    _last_rebuild,
    _utc_now_iso,
    ensure_session_meta,
    memory_dir,
    read_json,
    scan_memory_headers,
    session_meta_path,
    write_json,
    write_memory_file,
    rebuild_memory_index,
)
from certified_turtles.memory_runtime.manager import ClaudeLikeMemoryRuntime


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
    return f"bugs-{os.urandom(4).hex()}"


def _session() -> str:
    return f"bugs-sess-{os.urandom(4).hex()}"


def _client_selecting(*filenames: str) -> MagicMock:
    client = MagicMock()
    client.chat_completions.return_value = {
        "choices": [{"message": {"content": json.dumps({"selected_memories": list(filenames)})}}]
    }
    return client


def _h(filename: str, name: str, description: str,
       type_: str = "project", mtime: float | None = None) -> MemoryHeader:
    return MemoryHeader(filename, name, description, type_, mtime or time.time())


# ═══════════════════════════════════════════════════════════════
# BUG 1: STALENESS WARNINGS NEVER FIRE
#
# _utc_now_iso() produces "2024-03-15T10:30:00.123Z" (with milliseconds).
# _memory_age_warning() parses with "%Y-%m-%dT%H:%M:%SZ" (no milliseconds).
# Result: strptime raises ValueError → warning returns "" → SILENTLY DISABLED.
#
# Impact: Users act on stale memories thinking they are current facts.
# ═══════════════════════════════════════════════════════════════


class TestBUG_StalenessWarningNeverFires:

    def test_utc_now_iso_has_milliseconds(self):
        """Confirm: _utc_now_iso produces timestamps WITH milliseconds."""
        ts = _utc_now_iso()
        # Format: "2024-03-15T10:30:00.123Z"
        assert "." in ts, f"Expected milliseconds in timestamp: {ts}"
        # The dot is between seconds and millis
        parts = ts.split(".")
        assert len(parts) == 2
        assert parts[1].endswith("Z")

    @pytest.mark.xfail(reason="BUG: _memory_age_warning can't parse timestamps written by _utc_now_iso (milliseconds)")
    def test_staleness_warning_with_real_timestamp(self):
        """_memory_age_warning must handle timestamps from _utc_now_iso.
        Currently FAILS because strptime format doesn't match."""
        from datetime import datetime, timezone, timedelta
        # Produce a "10 days ago" timestamp in the same format as _utc_now_iso
        old_dt = datetime.now(timezone.utc) - timedelta(days=10)
        old_stamp = old_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        # e.g., "2024-03-05T10:30:00.123Z"
        result = _memory_age_warning(old_stamp)
        assert "10 days old" in result, (
            f"Expected '10 days old' warning for {old_stamp}, got: {result!r}"
        )

    @pytest.mark.xfail(reason="BUG: staleness warning never appears for real memories in prompt")
    def test_old_memory_gets_warning_in_prompt(self):
        """A memory written 30 days ago must have a staleness warning in the prompt.
        Currently NO warning appears because timestamp parsing fails."""
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        from certified_turtles.memory_runtime.storage import _atomic_write_text
        mem_root = memory_dir(scope)
        path = mem_root / "old-fact.md"
        from datetime import datetime, timezone, timedelta
        old_dt = datetime.now(timezone.utc) - timedelta(days=30)
        old_stamp = old_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        _atomic_write_text(path, (
            '---\n'
            f'name: "Old Fact"\n'
            f'description: "a fact from 30 days ago"\n'
            f'type: "project"\n'
            f'created: "{old_stamp}"\n'
            f'updated: "{old_stamp}"\n'
            f'source: "memory_extractor"\n'
            '---\n\n'
            'Old fact content.\n'
        ))
        _last_rebuild.clear()
        rebuild_memory_index(scope, force=True)

        client = _client_selecting("old-fact.md")
        bundle = build_memory_prompt(
            client, model="m",
            messages=[{"role": "user", "content": "q"}],
            scope_id=scope, session_id=session, user_query="q",
        )
        assert "days old" in bundle.prompt, (
            f"Expected staleness warning in prompt for 30-day-old memory!\n"
            f"Prompt snippet: ...{bundle.prompt[-500:]}"
        )


# ═══════════════════════════════════════════════════════════════
# BUG 2: fallback_select IS DEAD CODE — NEVER CALLED IN PIPELINE
#
# select_relevant_memories catches all exceptions and returns [].
# There is NO fallback to keyword matching when the LLM fails.
# fallback_select is defined but only used in tests.
#
# Impact: When LLM is down or errors out, ZERO memories are recalled.
# The system silently degrades to complete amnesia.
# ═══════════════════════════════════════════════════════════════


class TestBUG_FallbackSelectorDeadCode:

    def test_llm_exception_returns_empty_no_fallback(self):
        """When LLM raises an exception, select_relevant_memories returns []
        instead of falling back to keyword matching."""
        headers = [
            _h("pizza.md", "Pizza Preference", "user loves pizza margherita"),
            _h("work.md", "Work Setup", "uses PyCharm on Linux"),
        ]
        client = MagicMock()
        client.chat_completions.side_effect = RuntimeError("LLM is down")

        result = select_relevant_memories(
            client, model="m", query="pizza",
            headers=headers,
        )
        # BUG: returns [] even though fallback_select would find "pizza.md"
        assert result == [], "Confirmed: LLM failure → total amnesia, no fallback"
        # What fallback WOULD return:
        fallback_result = fallback_select(headers, "pizza")
        assert fallback_result == ["pizza.md"], "fallback_select WORKS but is never called"

    def test_llm_invalid_json_returns_empty_no_fallback(self):
        """Even when LLM returns garbage, no fallback to keyword matching."""
        headers = [_h("pizza.md", "Pizza Love", "user loves pizza")]
        client = MagicMock()
        client.chat_completions.return_value = {
            "choices": [{"message": {"content": "I can't help with that."}}]
        }

        result = select_relevant_memories(client, model="m", query="pizza", headers=headers)
        assert result == []
        assert fallback_select(headers, "pizza") == ["pizza.md"]

    @pytest.mark.xfail(reason="BUG: build_memory_prompt with client=None selects ZERO memories")
    def test_no_client_still_selects_memories(self):
        """When client is None (no LLM), the system should use fallback
        keyword matching. Currently it selects NOTHING."""
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        write_memory_file(scope, name="Pizza Love", description="user loves pizza",
                          type_="user", body="PIZZA_BODY", filename="pizza.md")
        _last_rebuild.clear()

        bundle = build_memory_prompt(
            None,  # no LLM client
            model="m",
            messages=[{"role": "user", "content": "what food do I like?"}],
            scope_id=scope, session_id=session, user_query="what food do I like?",
        )
        # BUG: with client=None, selected is always []
        assert "PIZZA_BODY" in bundle.prompt, (
            "Memory body should appear in prompt even without LLM, via fallback keyword matching"
        )


# ═══════════════════════════════════════════════════════════════
# BUG 3: SESSION MEMORY STARVATION
#
# already_surfaced grows during a session. Once ALL memories have been
# surfaced, they are ALL filtered from the selector input → headers
# becomes empty → select_relevant_memories returns [] → PERMANENT
# amnesia for the rest of the session.
#
# Impact: In long sessions with few memories, the system "forgets"
# everything after showing each memory once.
# ═══════════════════════════════════════════════════════════════


class TestBUG_SurfacedMemoryStarvation:

    def test_all_memories_surfaced_then_nothing_selected(self):
        """After every memory has been surfaced once, no memories can be
        selected for the rest of the session."""
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        # Write 3 memories
        for i in range(3):
            _last_rebuild.clear()
            write_memory_file(scope, name=f"Fact {i}", description=f"fact number {i}",
                              type_="project", body=f"content {i}",
                              filename=f"fact-{i}.md")

        filenames = [f"fact-{i}.md" for i in range(3)]

        # Turn 1: surface all 3
        client = _client_selecting(*filenames)
        build_memory_prompt(
            client, model="m",
            messages=[{"role": "user", "content": "tell me everything"}],
            scope_id=scope, session_id=session, user_query="tell me everything",
        )

        # Now all 3 are in surfaced_memories
        meta = read_json(session_meta_path(session)) or {}
        surfaced = set(meta.get("surfaced_memories", []))
        assert surfaced == set(filenames), "All 3 should be surfaced"

        # Turn 2: try to select again
        # The LLM selector filters out already_surfaced BEFORE sending to LLM
        # Since all 3 are surfaced → empty headers → returns []
        client2 = _client_selecting(*filenames)  # LLM would return them, but they're filtered
        bundle = build_memory_prompt(
            client2, model="m",
            messages=[{"role": "user", "content": "remind me about fact 0"}],
            scope_id=scope, session_id=session, user_query="remind me about fact 0",
        )

        # BUG: no memories selected — system has "forgotten" everything
        assert bundle.selected_memories == (), (
            "Confirmed: after surfacing all memories, the system can't recall any of them"
        )
        # The user asks "remind me about fact 0" and gets NOTHING
        assert "content 0" not in bundle.prompt, (
            "The memory body is gone from the prompt — permanent session amnesia"
        )


# ═══════════════════════════════════════════════════════════════
# FLAW 1: BODY CONTENT IS INVISIBLE TO SEARCH
#
# Both fallback_select and LLM selector only see name + description + type.
# The actual body content is NEVER searchable.
#
# Impact: A memory with name="Preferences" description="user choices"
# and body="loves pizza, hates sushi" — query "pizza" finds NOTHING.
# ═══════════════════════════════════════════════════════════════


class TestFLAW_BodyInvisibleToSearch:

    def test_keyword_in_body_not_found_by_fallback(self):
        """Keyword only in body → not found by fallback_select."""
        scope = _scope()
        write_memory_file(scope, name="Food Choices", description="user dietary preferences",
                          type_="user", body="User loves pizza margherita and hates sushi.",
                          filename="food.md")
        _last_rebuild.clear()

        headers = scan_memory_headers(scope)
        selected = fallback_select(headers, "pizza")
        # "pizza" is ONLY in the body, not in name/description/type
        assert selected == [], (
            "FLAW: 'pizza' is in the body but fallback only searches "
            "name+description+type. Body content is invisible to search."
        )

    def test_keyword_in_body_not_sent_to_llm(self):
        """LLM selector receives manifest (filename, description, type) — no body.
        So even the LLM can't know about body content."""
        scope = _scope()
        write_memory_file(scope, name="Preferences", description="general prefs",
                          type_="user", body="User LOVES Kubernetes and Docker.",
                          filename="prefs.md")
        _last_rebuild.clear()

        headers = scan_memory_headers(scope)
        client = MagicMock()
        client.chat_completions.return_value = {
            "choices": [{"message": {"content": json.dumps({"selected_memories": []})}}]
        }
        select_relevant_memories(client, model="m", query="tell me about tools", headers=headers)

        # Check what was sent to LLM — body content should NOT be there
        user_msg = client.chat_completions.call_args[0][1][1]["content"]
        # Strip the query line to only check the manifest portion
        manifest_part = user_msg.split("Available memories:")[1] if "Available memories:" in user_msg else ""
        assert "Kubernetes" not in manifest_part and "Docker" not in manifest_part, (
            "FLAW: body content ('Kubernetes', 'Docker') is not in the manifest sent to LLM. "
            "The LLM has no way to know this memory is relevant to infrastructure queries."
        )

    def test_good_description_is_the_only_way(self):
        """The ONLY way a memory is findable is if the description/name
        contains the right keywords. Bad descriptions = lost memories."""
        scope = _scope()
        # GOOD: description contains key terms
        write_memory_file(scope, name="Pizza Love", description="user loves pizza margherita",
                          type_="user", body="details...", filename="good.md")
        _last_rebuild.clear()
        # BAD: vague description, specifics only in body
        write_memory_file(scope, name="Food Notes", description="some dietary information",
                          type_="user", body="user loves pizza margherita", filename="bad.md")
        _last_rebuild.clear()

        headers = scan_memory_headers(scope)
        selected = fallback_select(headers, "pizza")
        assert "good.md" in selected, "Good description → found"
        assert "bad.md" not in selected, "FLAW: bad description → lost even though body matches"


# ═══════════════════════════════════════════════════════════════
# FLAW 2: TYPE FIELD POLLUTES SEARCH RESULTS
#
# The haystack is `name + " " + description + " " + type`.
# So any query containing "user", "project", "feedback", or "reference"
# gets boosted matches on ALL memories of that type.
#
# Impact: "how does user authentication work?" boosts every type=user
# memory even if they're about food preferences.
# ═══════════════════════════════════════════════════════════════


class TestFLAW_TypeFieldPollutesSearch:

    def test_query_user_matches_all_user_type_memories(self):
        """Query 'user authentication' matches ALL type='user' memories
        because 'user' is in the haystack via the type field."""
        headers = [
            _h("food.md", "Pizza Love", "loves pizza", type_="user"),
            _h("pet.md", "Has a Cat", "owns two cats", type_="user"),
            _h("auth.md", "Auth System", "authentication middleware", type_="project"),
        ]
        selected = fallback_select(headers, "user authentication")
        # Expected: only auth.md (about authentication)
        # Actual: food.md and pet.md ALSO match because type="user"
        # contains the word "user"
        assert "food.md" in selected, (
            "FLAW: 'Pizza Love' matches query 'user authentication' "
            "because type='user' contains 'user'"
        )
        assert "pet.md" in selected, (
            "FLAW: 'Has a Cat' matches query 'user authentication' "
            "because type='user' contains 'user'"
        )

    def test_query_project_matches_all_project_memories(self):
        headers = [
            _h("food.md", "Lunch Spot", "favorite restaurant", type_="project"),
            _h("deploy.md", "Deploy Notes", "deployment guide", type_="project"),
        ]
        selected = fallback_select(headers, "project deadline")
        # Both match because type="project" contains "project"
        assert len(selected) == 2, (
            "FLAW: 'project deadline' query matches ALL project-type memories"
        )

    def test_query_feedback_matches_all_feedback_memories(self):
        headers = [
            _h("a.md", "Code Style", "use tabs", type_="feedback"),
            _h("b.md", "Test Approach", "integration tests", type_="feedback"),
        ]
        selected = fallback_select(headers, "give me feedback on my PR")
        assert "a.md" in selected and "b.md" in selected


# ═══════════════════════════════════════════════════════════════
# FLAW 3: SUBSTRING MATCHING CAUSES FALSE POSITIVES
#
# Scoring uses `w in hay` (Python substring match).
# Token "api" matches "capital", "therapy", "fastapi".
# Token "log" matches "dialogue", "catalog", "login".
#
# Impact: Unrelated memories get false-positive matches.
# ═══════════════════════════════════════════════════════════════


class TestFLAW_SubstringFalsePositives:

    def test_api_matches_capital(self):
        """Token 'api' is a substring of 'capital' → false positive."""
        headers = [_h("a.md", "Capital Investment", "capital markets overview")]
        selected = fallback_select(headers, "api documentation")
        assert "a.md" in selected, (
            "FLAW: 'api documentation' query matches 'Capital Investment' "
            "because 'api' is a substring of 'capital'"
        )

    def test_log_matches_catalog(self):
        """Token 'log' is a substring of 'catalog' → false positive."""
        headers = [_h("a.md", "Product Catalog", "catalog of items")]
        selected = fallback_select(headers, "log analysis")
        assert "a.md" in selected, (
            "FLAW: 'log analysis' matches 'Product Catalog' "
            "because 'log' is substring of 'catalog'"
        )

    def test_age_matches_package(self):
        """Token 'age' is substring of 'package' → false positive."""
        headers = [_h("a.md", "Package Manager", "npm package setup")]
        selected = fallback_select(headers, "user age calculation")
        assert "a.md" in selected, (
            "FLAW: 'age' is substring of 'package'"
        )

    def test_run_matches_runtime(self):
        """Token 'run' (3 chars, tokenized) is substring of 'runtime' → false positive."""
        headers = [_h("a.md", "Runtime Config", "runtime environment setup")]
        selected = fallback_select(headers, "run tests")
        assert "a.md" in selected, (
            "FLAW: 'run tests' matches 'Runtime Config' "
            "because 'run' is a substring of 'runtime'"
        )

    def test_car_matches_discard(self):
        headers = [_h("a.md", "Discard Policy", "when to discard old data")]
        selected = fallback_select(headers, "car rental")
        # 'car' (3 chars) is substring of 'discard'
        assert "a.md" in selected, "FLAW: 'car' matches 'discard'"

    def test_false_positive_ranking(self):
        """False positives can outrank true positives in multi-keyword queries."""
        headers = [
            _h("true.md", "API Documentation", "api reference guide"),
            _h("false.md", "Capital Therapy Catalog", "capital therapy catalog"),
        ]
        selected = fallback_select(headers, "api log therapy")
        # true.md: "api" in hay → 1 match. "log" in hay → no. "therapy" → no. Score=1
        # false.md: "api" in "capital" → yes. "log" in "catalog" → yes. "therapy" → yes. Score=3
        assert selected[0] == "false.md", (
            "FLAW: 'Capital Therapy Catalog' scores HIGHER than 'API Documentation' "
            "for query 'api log therapy' due to substring false positives"
        )


# ═══════════════════════════════════════════════════════════════
# FLAW 5: _should_update_session_memory BLOCKS DURING ACTIVE TOOL USE
#
# The condition is:
#   if recent_tool_calls < 3 and last_assistant_has_tool_calls: return False
#
# When an assistant is actively using tools (common during coding sessions),
# last_assistant_has_tool_calls is always True. Session memory only updates
# when tool_calls >= 3 in the last 8 messages. If the assistant makes
# 1-2 tool calls per turn, session memory NEVER updates.
#
# Impact: Session memory goes stale during slow-paced tool-heavy conversations.
# ═══════════════════════════════════════════════════════════════


class TestFLAW_SessionMemoryBlockedDuringToolUse:

    def test_one_tool_call_per_turn_blocks_update(self):
        """If assistant makes exactly 1 tool call per turn (common pattern),
        session memory never updates even after many turns."""
        rt = ClaudeLikeMemoryRuntime()
        session = _session()
        ensure_session_meta(session, scope_id="scope")

        # Simulate 10 turns, each with 1 tool call — plenty of content
        msgs = [{"role": "user", "content": "x" * 60_000}]  # 15K tokens
        # 10 turns but only 1 tool call each → last 8 messages have ~4 tool calls
        # Wait — let me be precise about the last 8 messages
        for i in range(10):
            msgs.append({"role": "user", "content": f"do step {i}"})
            msgs.append({"role": "assistant", "content": json.dumps({
                "assistant_markdown": f"doing step {i}",
                "calls": [{"name": "file_read", "arguments": {"file_path": f"/tmp/{i}"}}],
            })})

        # Last 8 messages: 4 user + 4 assistant, each assistant has 1 call → 4 calls ≥ 3
        # But the VERY LAST message is assistant with tool calls
        # So: recent_tool_calls >= 3 → the condition "< 3 and last_has_tools" is False
        # Actually this passes. Let me construct the exact blocking scenario.

        # Blocking scenario: few tool calls in window, last message IS a tool call
        msgs_blocking = [{"role": "user", "content": "x" * 60_000}]  # 15K tokens
        msgs_blocking.append({"role": "user", "content": "do something"})
        msgs_blocking.append({"role": "assistant", "content": json.dumps({
            "assistant_markdown": "reading file",
            "calls": [{"name": "file_read", "arguments": {"file_path": "/tmp/a"}}],
        })})
        # Last 8 messages: 2 user + 1 assistant with 1 tool call
        # recent_tool_calls = 1 < 3, last_assistant_has_tool_calls = True → BLOCKED
        result = rt._should_update_session_memory(session, msgs_blocking)
        assert result is False, (
            "FLAW: Session memory blocked because recent_tool_calls=1 < 3 "
            "and last assistant has tool calls. During early active tool use, "
            "session memory never gets initialized."
        )


# ═══════════════════════════════════════════════════════════════
# FLAW 6: max_visible_bytes DOUBLE-CAPS BODY SIZE
#
# In build_memory_prompt:
#   max_visible_bytes = min(MAX_MEMORY_SESSION_BYTES, 4096)
#   → always 4096 (since 60KB > 4KB)
#
# This means every memory body is capped at 4096 bytes AGAIN on read,
# even though write_memory_file already enforces MAX_MEMORY_FILE_BYTES
# (also 4096). This is redundant but correct.
#
# HOWEVER: if MAX_MEMORY_FILE_BYTES were ever raised (say to 8KB),
# the prompt builder would still silently truncate to 4KB. The
# hardcoded min() doesn't use the same constant.
# ═══════════════════════════════════════════════════════════════


class TestFLAW_DoubleCapBodySize:

    def test_visible_bytes_always_4096(self):
        """min(MAX_MEMORY_SESSION_BYTES, 4096) is always 4096."""
        from certified_turtles.memory_runtime.storage import MAX_MEMORY_SESSION_BYTES
        max_visible = min(MAX_MEMORY_SESSION_BYTES, 4096)
        assert max_visible == 4096, (
            f"FLAW: max_visible_bytes = min({MAX_MEMORY_SESSION_BYTES}, 4096) = {max_visible}. "
            "This hardcoded 4096 doesn't reference MAX_MEMORY_FILE_BYTES. "
            "If MAX_MEMORY_FILE_BYTES were raised, bodies would be silently truncated."
        )


# ═══════════════════════════════════════════════════════════════
# FLAW 7: QUERY TOKEN IN TYPE CAN SUPPRESS REAL RESULTS
#
# If query produces a token that matches a type name, ALL memories
# of that type get score ≥ 1. With a limit of 5, these false positives
# can push out genuinely relevant memories.
# ═══════════════════════════════════════════════════════════════


class TestFLAW_TypeMatchPushesOutRealResults:

    def test_type_matches_crowd_out_relevant_memory(self):
        """Query 'user settings page' → 'user' matches all type=user memories.
        If there are 5+ user-type memories about irrelevant topics,
        a project-type memory about 'settings page' may be pushed out."""
        headers = [
            # 5 irrelevant user-type memories — all match "user" via type
            _h("u1.md", "Breakfast", "morning routine", type_="user"),
            _h("u2.md", "Commute", "daily commute", type_="user"),
            _h("u3.md", "Hobbies", "weekend hobbies", type_="user"),
            _h("u4.md", "Languages", "speaks French", type_="user"),
            _h("u5.md", "Timezone", "lives in UTC+3", type_="user"),
            # 1 actually relevant memory
            _h("settings.md", "Settings Page", "settings page architecture", type_="project"),
        ]
        selected = fallback_select(headers, "user settings page", limit=5)

        # "settings.md" scores 2: "settings" + "page" match
        # Each u*.md scores 1: "user" matches via type
        # settings.md SHOULD be in top 5 (score 2 > score 1)
        assert "settings.md" in selected, (
            "The actually relevant 'settings page' memory should be selected"
        )
        # But: if query had fewer unique tokens matching settings.md,
        # type pollution would push it out. Verify that type-only matches
        # consume 5 of 5 slots when they tie with the real result:
        headers_tie = [
            _h("u1.md", "Settings Breakfast", "morning settings", type_="user"),  # score 2
            _h("u2.md", "Settings Commute", "commute settings", type_="user"),    # score 2
            _h("u3.md", "Settings Hobbies", "hobby settings", type_="user"),      # score 2
            _h("u4.md", "Settings Lang", "language settings", type_="user"),      # score 2
            _h("u5.md", "Settings TZ", "timezone settings", type_="user"),        # score 2
            _h("real.md", "Settings Page", "settings page layout", type_="project"),  # score 2
        ]
        selected_tie = fallback_select(headers_tie, "user settings", limit=5)
        # All score 2. Tie broken by filename. "real.md" sorts after "u1"..."u5"
        assert "real.md" not in selected_tie, (
            "FLAW: When type-polluted memories tie with the real result, "
            "alphabetical tie-breaking can push out the relevant memory. "
            f"Selected: {selected_tie}"
        )


