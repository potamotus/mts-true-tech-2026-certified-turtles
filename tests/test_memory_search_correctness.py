"""
Deep correctness tests for memory SEARCH/RETRIEVAL pipeline.

Tests verify that the right memories are found for the right queries —
tokenization, scoring, ranking, filtering, and end-to-end query→prompt
correctness.

Key invariants tested:
- Tokenizer behaviour: word length threshold, Unicode, hyphens/slashes
- Scoring: substring match in (name + description + type), NOT body/filename
- Ranking: highest score first, ties broken alphabetically by filename
- Filtering: tool references, already_surfaced, type-based
- E2E: query → correct memory body appears in final prompt

Run: pytest tests/test_memory_search_correctness.py -v --tb=short
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CT_CLAUDE_HOME", tempfile.mkdtemp(prefix="ct_search_"))

from certified_turtles.memory_runtime.selector import (
    _TOKEN_RE,
    _WARNING_HINTS,
    _tokenize,
    fallback_select,
    select_relevant_memories,
)
from certified_turtles.memory_runtime.prompting import build_memory_prompt
from certified_turtles.memory_runtime.storage import (
    MemoryHeader,
    _last_rebuild,
    ensure_session_meta,
    memory_dir,
    read_json,
    scan_memory_headers,
    session_meta_path,
    write_json,
    write_memory_file,
    rebuild_memory_index,
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
    return f"search-{os.urandom(4).hex()}"


def _session() -> str:
    return f"search-sess-{os.urandom(4).hex()}"


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
# 1. TOKENIZER (_tokenize) CORRECTNESS
# ═══════════════════════════════════════════════════════════════


class TestTokenizer:
    """_tokenize extracts ≥3-char word tokens matching [\\w/-]+.
    re.UNICODE means \\w includes Cyrillic, CJK, etc."""

    def test_basic_english(self):
        tokens = _tokenize("hello world")
        assert tokens == {"hello", "world"}

    def test_short_words_excluded(self):
        """Words under 3 characters are not tokenized."""
        tokens = _tokenize("I am a go to py dev")
        # "I"(1), "am"(2), "a"(1), "go"(2), "to"(2), "py"(2), "dev"(3)
        assert tokens == {"dev"}

    def test_case_normalized_to_lower(self):
        tokens = _tokenize("FastAPI Python SETUP")
        assert tokens == {"fastapi", "python", "setup"}

    def test_cyrillic_tokenized(self):
        """re.UNICODE flag means Cyrillic characters match \\w."""
        tokens = _tokenize("пользователь любит пиццу")
        assert "пользователь" in tokens
        assert "любит" in tokens
        assert "пиццу" in tokens

    def test_mixed_script_tokenized(self):
        tokens = _tokenize("user любит pizza")
        assert tokens == {"user", "любит", "pizza"}

    def test_hyphenated_words_kept_together(self):
        """Hyphens are in the character class [\\w/-], so kept."""
        tokens = _tokenize("cross-platform setup")
        assert "cross-platform" in tokens
        assert "setup" in tokens

    def test_slashes_kept(self):
        """Slashes are in [\\w/-], so paths tokenize as one token."""
        tokens = _tokenize("check src/main/app.py for errors")
        assert "src/main/app" in tokens  # dot stops the match
        assert "check" in tokens
        assert "for" in tokens
        assert "errors" in tokens

    def test_special_chars_split_tokens(self):
        tokens = _tokenize("deploy@staging#prod")
        # @ and # are NOT in [\w/-], so they split
        assert "deploy" in tokens
        assert "staging" in tokens
        assert "prod" in tokens

    def test_numbers_in_tokens(self):
        tokens = _tokenize("python3 version 3.12")
        assert "python3" in tokens
        assert "version" in tokens
        assert "3.12" not in tokens  # dot splits: "12" is too short
        # "3" alone is too short

    def test_empty_string(self):
        assert _tokenize("") == set()

    def test_underscores_kept(self):
        """\\w includes underscores."""
        tokens = _tokenize("my_function_name is good")
        assert "my_function_name" in tokens


# ═══════════════════════════════════════════════════════════════
# 2. FALLBACK SELECTOR: WHAT IS SEARCHED
# ═══════════════════════════════════════════════════════════════


class TestFallbackSearchTarget:
    """fallback_select searches name + description + type.
    It does NOT search filename or body."""

    def test_keyword_in_name_matches(self):
        headers = [_h("a.md", "Python Setup Guide", "how to set up")]
        selected = fallback_select(headers, "python")
        assert selected == ["a.md"]

    def test_keyword_in_description_matches(self):
        headers = [_h("a.md", "Setup Guide", "python virtualenv configuration")]
        selected = fallback_select(headers, "python")
        assert selected == ["a.md"]

    def test_keyword_in_type_matches(self):
        """The type field (e.g., 'feedback') is included in the search haystack."""
        headers = [_h("a.md", "Coding Style", "use functional patterns", type_="feedback")]
        selected = fallback_select(headers, "feedback")
        assert selected == ["a.md"]

    def test_keyword_in_filename_does_NOT_match(self):
        """Filenames are NOT searched — only name + description + type."""
        headers = [_h("python-setup.md", "Setup Guide", "installation instructions")]
        selected = fallback_select(headers, "python")
        # "python" is only in the filename, not in name/description/type
        assert selected == []

    def test_keyword_match_is_substring(self):
        """Scoring uses `w in hay` — substring match, not word-boundary match.
        Token 'deploy' matches 'deployment' in the haystack."""
        headers = [_h("a.md", "Deployment Notes", "production deployment guide")]
        selected = fallback_select(headers, "deploy")
        # "deploy" is a substring of "deployment" in the lowered haystack
        assert selected == ["a.md"]

    def test_substring_match_in_compound_word(self):
        """'api' in 'fastapi' should match (substring)."""
        headers = [_h("a.md", "FastAPI Backend", "web framework setup")]
        selected = fallback_select(headers, "api backend")
        assert selected == ["a.md"]


# ═══════════════════════════════════════════════════════════════
# 3. FALLBACK SELECTOR: SCORING & RANKING
# ═══════════════════════════════════════════════════════════════


class TestFallbackScoring:
    """Score = count of query tokens found as substrings in haystack.
    Higher score → higher rank. Ties broken by filename ascending."""

    def test_more_matching_keywords_ranks_higher(self):
        headers = [
            _h("one-match.md", "Python Guide", "general programming"),
            _h("two-match.md", "Python Setup", "python environment setup"),
        ]
        selected = fallback_select(headers, "python setup guide", limit=5)
        # "two-match.md": 'python' + 'setup' = 2 hits (both in haystack)
        # "one-match.md": 'python' + 'guide' = 2 hits too
        # Actually let's think: "python setup guide"
        # tokens: {"python", "setup", "guide"}
        # one-match hay: "python guide general programming project" → python(yes), setup(no), guide(yes) = 2
        # two-match hay: "python setup python environment setup project" → python(yes), setup(yes), guide(no) = 2
        # tie! broken by filename: "one-match.md" < "two-match.md"
        assert selected[0] == "one-match.md"

    def test_three_vs_one_keyword_match(self):
        headers = [
            _h("low.md", "Random Notes", "misc notes about stuff"),
            _h("high.md", "Python API Deployment", "python api deployment to production"),
        ]
        selected = fallback_select(headers, "python api deployment", limit=5)
        # low: 0 matches → not included
        # high: 3 matches (python, api, deployment all in haystack)
        assert selected == ["high.md"]

    def test_tie_broken_alphabetically_by_filename(self):
        headers = [
            _h("zebra.md", "Python Setup", "env config"),
            _h("alpha.md", "Python Setup", "env config"),
        ]
        selected = fallback_select(headers, "python setup", limit=5)
        assert selected == ["alpha.md", "zebra.md"]

    def test_zero_score_not_included(self):
        headers = [
            _h("a.md", "Rust Guide", "systems programming"),
            _h("b.md", "Go Backend", "microservices architecture"),
        ]
        selected = fallback_select(headers, "python deployment")
        assert selected == []

    def test_limit_respected(self):
        headers = [_h(f"m{i}.md", f"Python topic {i}", "python related") for i in range(10)]
        selected = fallback_select(headers, "python topic", limit=3)
        assert len(selected) == 3

    def test_single_token_query(self):
        headers = [
            _h("a.md", "Deployment Guide", "how to deploy"),
            _h("b.md", "Testing Guide", "how to test"),
        ]
        selected = fallback_select(headers, "deploy")
        # "deploy" is substring of "deployment" and "deploy"
        assert "a.md" in selected
        assert "b.md" not in selected


# ═══════════════════════════════════════════════════════════════
# 4. FALLBACK SELECTOR: TOOL REFERENCE FILTERING
# ═══════════════════════════════════════════════════════════════


class TestToolReferenceFiltering:
    """Reference-type memories about recently-used tools should be filtered out,
    UNLESS they contain warning keywords."""

    def test_reference_for_active_tool_filtered(self):
        headers = [
            _h("grep-ref.md", "grep_search usage", "grep_search reference guide", type_="reference"),
        ]
        selected = fallback_select(headers, "grep_search", recent_tools=["grep_search"])
        assert selected == []

    def test_reference_with_warning_kept(self):
        for hint in _WARNING_HINTS:
            headers = [
                _h(f"{hint}.md", f"grep_search {hint}", f"grep_search {hint} about edge cases", type_="reference"),
            ]
            selected = fallback_select(headers, "grep_search", recent_tools=["grep_search"])
            assert selected == [f"{hint}.md"], f"Warning keyword '{hint}' should prevent filtering"

    def test_non_reference_type_not_filtered(self):
        """Only type='reference' triggers tool filtering."""
        headers = [
            _h("a.md", "grep_search tips", "grep_search usage tips", type_="feedback"),
        ]
        selected = fallback_select(headers, "grep_search", recent_tools=["grep_search"])
        assert selected == ["a.md"]

    def test_tool_name_case_insensitive(self):
        headers = [
            _h("a.md", "File_Read Guide", "file_read reference", type_="reference"),
        ]
        selected = fallback_select(headers, "file_read", recent_tools=["File_Read"])
        # recent_tools lowered to {"file_read"}, hay contains "file_read" → filtered
        assert selected == []

    def test_multiple_tools_all_filtered(self):
        headers = [
            _h("a.md", "grep_search ref", "grep_search guide", type_="reference"),
            _h("b.md", "file_read ref", "file_read guide", type_="reference"),
            _h("c.md", "deploy notes", "deployment guide", type_="project"),
        ]
        selected = fallback_select(
            headers, "grep_search file_read deploy",
            recent_tools=["grep_search", "file_read"],
        )
        # a.md and b.md filtered (reference + active tool, no warnings)
        # c.md kept (not reference type)
        assert selected == ["c.md"]

    def test_no_recent_tools_no_filtering(self):
        headers = [
            _h("a.md", "grep_search usage", "grep_search reference", type_="reference"),
        ]
        selected = fallback_select(headers, "grep_search")
        assert selected == ["a.md"]

    def test_empty_recent_tools_no_filtering(self):
        headers = [
            _h("a.md", "grep_search usage", "grep_search reference", type_="reference"),
        ]
        selected = fallback_select(headers, "grep_search", recent_tools=[])
        assert selected == ["a.md"]


# ═══════════════════════════════════════════════════════════════
# 5. LLM SELECTOR: INPUT CONSTRUCTION
# ═══════════════════════════════════════════════════════════════


class TestLLMSelectorInput:
    """select_relevant_memories must construct the correct LLM input:
    system prompt, query, manifest, and tools suffix."""

    def test_manifest_sent_to_llm(self):
        """The manifest passed to LLM should contain filenames and descriptions."""
        headers = [
            _h("pizza.md", "Food Preferences", "User loves pizza margherita"),
            _h("work.md", "Work Setup", "Uses PyCharm on Linux"),
        ]
        client = MagicMock()
        client.chat_completions.return_value = {
            "choices": [{"message": {"content": json.dumps({"selected_memories": []})}}]
        }
        select_relevant_memories(client, model="m", query="food", headers=headers)

        call_args = client.chat_completions.call_args
        messages = call_args[0][1]  # second positional arg
        user_content = messages[1]["content"]
        assert "pizza.md" in user_content
        assert "Food Preferences" in user_content or "pizza margherita" in user_content
        assert "work.md" in user_content

    def test_recent_tools_appended_to_user_message(self):
        headers = [_h("a.md", "Test", "test")]
        client = MagicMock()
        client.chat_completions.return_value = {
            "choices": [{"message": {"content": "{}"}}]
        }
        select_relevant_memories(
            client, model="m", query="test", headers=headers,
            recent_tools=["grep_search", "file_read"],
        )
        user_content = client.chat_completions.call_args[0][1][1]["content"]
        assert "grep_search" in user_content
        assert "file_read" in user_content
        assert "Recently used tools" in user_content

    def test_no_recent_tools_no_suffix(self):
        headers = [_h("a.md", "Test", "test")]
        client = MagicMock()
        client.chat_completions.return_value = {
            "choices": [{"message": {"content": "{}"}}]
        }
        select_relevant_memories(client, model="m", query="test", headers=headers)
        user_content = client.chat_completions.call_args[0][1][1]["content"]
        assert "Recently used tools" not in user_content

    def test_already_surfaced_removed_before_llm_call(self):
        """Memories in already_surfaced should not appear in the manifest sent to LLM."""
        headers = [
            _h("old.md", "Old Memory", "already seen"),
            _h("new.md", "New Memory", "fresh info"),
        ]
        client = MagicMock()
        client.chat_completions.return_value = {
            "choices": [{"message": {"content": json.dumps({"selected_memories": ["new.md"]})}}]
        }
        result = select_relevant_memories(
            client, model="m", query="test", headers=headers,
            already_surfaced={"old.md"},
        )
        # old.md should not be in the manifest
        user_content = client.chat_completions.call_args[0][1][1]["content"]
        assert "old.md" not in user_content
        assert "new.md" in user_content

    def test_all_headers_surfaced_returns_empty_no_llm_call(self):
        """If all memories are already surfaced, don't even call the LLM."""
        headers = [_h("a.md", "A", "a")]
        client = MagicMock()
        result = select_relevant_memories(
            client, model="m", query="test", headers=headers,
            already_surfaced={"a.md"},
        )
        assert result == []
        client.chat_completions.assert_not_called()

    def test_system_prompt_contains_instructions(self):
        headers = [_h("a.md", "Test", "test")]
        client = MagicMock()
        client.chat_completions.return_value = {
            "choices": [{"message": {"content": "{}"}}]
        }
        select_relevant_memories(client, model="m", query="test", headers=headers)
        system_msg = client.chat_completions.call_args[0][1][0]
        assert system_msg["role"] == "system"
        assert "selecting memories" in system_msg["content"]


# ═══════════════════════════════════════════════════════════════
# 6. LLM SELECTOR: OUTPUT VALIDATION
# ═══════════════════════════════════════════════════════════════


class TestLLMSelectorOutputValidation:
    """The LLM response must be validated: only existing filenames,
    capped at limit, graceful on malformed output."""

    def test_non_string_entries_filtered(self):
        headers = [_h("a.md", "A", "a")]
        client = MagicMock()
        client.chat_completions.return_value = {
            "choices": [{"message": {"content": json.dumps({"selected_memories": ["a.md", 123, None, True]})}}]
        }
        result = select_relevant_memories(client, model="m", query="test", headers=headers)
        assert result == ["a.md"]

    def test_selected_memories_not_a_list(self):
        """If selected_memories is not a list, return empty."""
        headers = [_h("a.md", "A", "a")]
        client = MagicMock()
        client.chat_completions.return_value = {
            "choices": [{"message": {"content": json.dumps({"selected_memories": "a.md"})}}]
        }
        result = select_relevant_memories(client, model="m", query="test", headers=headers)
        assert result == []

    def test_empty_choices(self):
        headers = [_h("a.md", "A", "a")]
        client = MagicMock()
        client.chat_completions.return_value = {"choices": []}
        result = select_relevant_memories(client, model="m", query="test", headers=headers)
        assert result == []

    def test_null_message(self):
        headers = [_h("a.md", "A", "a")]
        client = MagicMock()
        client.chat_completions.return_value = {"choices": [{"message": None}]}
        result = select_relevant_memories(client, model="m", query="test", headers=headers)
        assert result == []

    def test_duplicate_filenames_preserved(self):
        """If LLM returns same filename twice, both pass validation (dedup is caller's job)."""
        headers = [_h("a.md", "A", "a")]
        client = MagicMock()
        client.chat_completions.return_value = {
            "choices": [{"message": {"content": json.dumps({"selected_memories": ["a.md", "a.md"]})}}]
        }
        result = select_relevant_memories(client, model="m", query="test", headers=headers)
        # Both pass validation but limit=5 so both included
        assert result == ["a.md", "a.md"]


# ═══════════════════════════════════════════════════════════════
# 7. CROSS-LINGUAL SEARCH (RUSSIAN + ENGLISH)
# ═══════════════════════════════════════════════════════════════


class TestCrossLingualSearch:
    """The tokenizer uses re.UNICODE, so Cyrillic keywords should match
    Cyrillic descriptions and vice versa."""

    def test_russian_query_finds_russian_description(self):
        headers = [
            _h("food.md", "Еда", "пользователь любит пиццу маргариту"),
            _h("work.md", "Work", "uses PyCharm on Linux"),
        ]
        selected = fallback_select(headers, "пиццу маргариту")
        assert "food.md" in selected
        assert "work.md" not in selected

    def test_russian_query_no_match_english_description(self):
        headers = [_h("a.md", "Food Preferences", "user loves pizza margherita")]
        selected = fallback_select(headers, "пицца")
        assert selected == []

    def test_english_query_no_match_russian_description(self):
        headers = [_h("a.md", "Еда", "пользователь любит пиццу")]
        selected = fallback_select(headers, "pizza")
        assert selected == []

    def test_mixed_language_query_matches_both(self):
        headers = [
            _h("a.md", "Deployment на продакшн", "деплой python приложения"),
        ]
        selected = fallback_select(headers, "python деплой")
        assert selected == ["a.md"]


# ═══════════════════════════════════════════════════════════════
# 8. END-TO-END: QUERY → CORRECT MEMORY IN PROMPT
# ═══════════════════════════════════════════════════════════════


class TestE2ESearchToPrompt:
    """Full pipeline: write memories → build_memory_prompt with query →
    verify the CORRECT memory body appears in the prompt and others don't."""

    def test_relevant_memory_body_in_prompt_irrelevant_absent(self):
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        write_memory_file(scope, name="Pizza Preference", description="user loves pizza",
                          type_="user", body="PIZZA_BODY: The user adores margherita pizza.",
                          filename="pizza.md")
        _last_rebuild.clear()
        write_memory_file(scope, name="IDE Setup", description="uses vim with tmux",
                          type_="project", body="VIM_BODY: The user uses neovim.",
                          filename="ide.md")
        _last_rebuild.clear()

        # Mock client selects only pizza.md
        client = _client_selecting("pizza.md")
        bundle = build_memory_prompt(
            client, model="m",
            messages=[{"role": "user", "content": "what food do I like?"}],
            scope_id=scope, session_id=session, user_query="what food do I like?",
        )
        assert "PIZZA_BODY" in bundle.prompt
        assert "margherita pizza" in bundle.prompt
        # VIM_BODY should NOT be in the prompt (not selected)
        assert "VIM_BODY" not in bundle.prompt

    def test_multiple_relevant_memories_all_in_prompt(self):
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        write_memory_file(scope, name="Sushi Pref", description="loves sushi",
                          type_="user", body="SUSHI_MARKER", filename="sushi.md")
        _last_rebuild.clear()
        write_memory_file(scope, name="Ramen Pref", description="loves ramen",
                          type_="user", body="RAMEN_MARKER", filename="ramen.md")
        _last_rebuild.clear()

        client = _client_selecting("sushi.md", "ramen.md")
        bundle = build_memory_prompt(
            client, model="m",
            messages=[{"role": "user", "content": "food"}],
            scope_id=scope, session_id=session, user_query="food",
        )
        assert "SUSHI_MARKER" in bundle.prompt
        assert "RAMEN_MARKER" in bundle.prompt

    def test_overwritten_memory_shows_latest_content(self):
        """After overwriting a memory, search should return the new content."""
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        write_memory_file(scope, name="Color", description="favorite color",
                          type_="user", body="OLD: blue", filename="color.md")
        _last_rebuild.clear()
        write_memory_file(scope, name="Color", description="favorite color",
                          type_="user", body="NEW: red", filename="color.md")
        _last_rebuild.clear()

        client = _client_selecting("color.md")
        bundle = build_memory_prompt(
            client, model="m",
            messages=[{"role": "user", "content": "color"}],
            scope_id=scope, session_id=session, user_query="color",
        )
        assert "NEW: red" in bundle.prompt
        assert "OLD: blue" not in bundle.prompt

    def test_deleted_memory_not_in_prompt(self):
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        write_memory_file(scope, name="Temp", description="temporary",
                          type_="user", body="DELETED_MARKER", filename="temp.md")
        _last_rebuild.clear()
        from certified_turtles.memory_runtime.storage import delete_memory_file
        delete_memory_file(scope, "temp.md")

        # Even if selector returns temp.md, file is gone → gracefully skipped
        client = _client_selecting("temp.md")
        bundle = build_memory_prompt(
            client, model="m",
            messages=[{"role": "user", "content": "q"}],
            scope_id=scope, session_id=session, user_query="q",
        )
        assert "DELETED_MARKER" not in bundle.prompt

    def test_memory_type_appears_in_prompt_header(self):
        """Selected memory's type should appear in the prompt section header."""
        scope = _scope()
        session = _session()
        ensure_session_meta(session, scope_id=scope)

        write_memory_file(scope, name="Deploy Warning", description="deploy warning",
                          type_="feedback", body="Always run migrations first.",
                          filename="deploy-warn.md")
        _last_rebuild.clear()

        client = _client_selecting("deploy-warn.md")
        bundle = build_memory_prompt(
            client, model="m",
            messages=[{"role": "user", "content": "deploy"}],
            scope_id=scope, session_id=session, user_query="deploy",
        )
        # The prompt should include "### Deploy Warning (feedback)"
        assert "Deploy Warning" in bundle.prompt
        assert "(feedback)" in bundle.prompt


# ═══════════════════════════════════════════════════════════════
# 9. SEARCH WITH REAL scan_memory_headers + fallback_select
# ═══════════════════════════════════════════════════════════════


class TestSearchWithRealHeaders:
    """Integration: write memories → scan_memory_headers → fallback_select →
    verify the right files are found by keyword matching on real frontmatter."""

    def test_finds_memory_by_description_keyword(self):
        scope = _scope()
        write_memory_file(scope, name="Backend Setup", description="FastAPI with PostgreSQL database",
                          type_="project", body="details", filename="backend.md")
        _last_rebuild.clear()
        write_memory_file(scope, name="Frontend Setup", description="React with TypeScript",
                          type_="project", body="details", filename="frontend.md")
        _last_rebuild.clear()

        headers = scan_memory_headers(scope)
        selected = fallback_select(headers, "postgresql database")
        assert "backend.md" in selected
        assert "frontend.md" not in selected

    def test_finds_memory_by_name_keyword(self):
        scope = _scope()
        write_memory_file(scope, name="Authentication System", description="how login works",
                          type_="project", body="details", filename="auth.md")
        _last_rebuild.clear()

        headers = scan_memory_headers(scope)
        selected = fallback_select(headers, "authentication")
        assert "auth.md" in selected

    def test_ambiguous_query_returns_multiple(self):
        scope = _scope()
        write_memory_file(scope, name="Python Backend", description="python fastapi",
                          type_="project", body="d", filename="py-back.md")
        _last_rebuild.clear()
        write_memory_file(scope, name="Python Testing", description="python pytest guide",
                          type_="project", body="d", filename="py-test.md")
        _last_rebuild.clear()
        write_memory_file(scope, name="Go Backend", description="golang gin framework",
                          type_="project", body="d", filename="go-back.md")
        _last_rebuild.clear()

        headers = scan_memory_headers(scope)
        selected = fallback_select(headers, "python")
        assert "py-back.md" in selected
        assert "py-test.md" in selected
        assert "go-back.md" not in selected

    def test_empty_scope_returns_empty(self):
        scope = _scope()
        headers = scan_memory_headers(scope)
        selected = fallback_select(headers, "anything")
        assert selected == []


# ═══════════════════════════════════════════════════════════════
# 10. EDGE CASES IN SEARCH
# ═══════════════════════════════════════════════════════════════


class TestSearchEdgeCases:

    def test_query_with_only_short_words(self):
        """If all words in query are <3 chars, no tokens → no matches."""
        headers = [_h("a.md", "Python Setup", "python env")]
        selected = fallback_select(headers, "py go")
        assert selected == []

    def test_query_with_special_chars_only(self):
        headers = [_h("a.md", "Notes", "some notes")]
        selected = fallback_select(headers, "!@# $%^")
        assert selected == []

    def test_many_memories_same_score(self):
        """All memories have the same score → sorted by filename."""
        headers = [
            _h("c.md", "Python C", "python stuff"),
            _h("a.md", "Python A", "python stuff"),
            _h("b.md", "Python B", "python stuff"),
        ]
        selected = fallback_select(headers, "python", limit=5)
        assert selected == ["a.md", "b.md", "c.md"]

    def test_description_with_newlines(self):
        """Descriptions with newlines (shouldn't happen, but handle gracefully)."""
        headers = [_h("a.md", "Multi", "line one\nline two with python")]
        selected = fallback_select(headers, "python")
        assert selected == ["a.md"]

    def test_very_long_description(self):
        long_desc = "python " * 1000
        headers = [_h("a.md", "Long", long_desc)]
        selected = fallback_select(headers, "python")
        assert selected == ["a.md"]

    def test_duplicate_headers_same_filename(self):
        """If two headers have same filename (shouldn't happen), both get scored."""
        headers = [
            _h("a.md", "Version 1", "python"),
            _h("a.md", "Version 2", "python"),
        ]
        selected = fallback_select(headers, "python")
        # Both match, both have same filename → appears twice
        assert selected.count("a.md") == 2

    def test_partial_token_match(self):
        """Token 'deploy' matches substring in 'pre-deployment' (via substring search)."""
        headers = [_h("a.md", "Pre-deployment Checklist", "before deploying")]
        selected = fallback_select(headers, "deploy")
        # hay = "pre-deployment checklist before deploying project"
        # token "deploy" is substring of "pre-deployment" and "deploying" → match
        assert selected == ["a.md"]
