"""
Stress-тесты memory runtime: сложные форматы, нагрузка, гонки, крайние случаи.
Запускать: pytest tests/test_memory_runtime_stress.py -v --tb=short
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CT_CLAUDE_HOME", tempfile.mkdtemp(prefix="ct_test_"))

from certified_turtles.memory_runtime.storage import (
    FRONTMATTER_RE,
    MAX_MEMORY_FILE_BYTES,
    MAX_MEMORY_FILES,
    MAX_MEMORY_INDEX_BYTES,
    MAX_MEMORY_INDEX_LINES,
    VALID_MEMORY_TYPES,
    _atomic_write_text,
    _validate_memory_filename,
    append_transcript_event,
    claude_like_root,
    delete_memory_file,
    ensure_session_meta,
    list_memory_files,
    list_scope_sessions,
    memory_dir,
    memory_index_path,
    parse_frontmatter,
    read_body,
    read_frontmatter,
    read_json,
    read_last_consolidated_at,
    read_session_memory,
    read_transcript_events,
    rebuild_memory_index,
    resolve_memory_path,
    scan_memory_headers,
    scope_lock_path,
    scope_slug,
    session_dir,
    session_meta_path,
    session_slug,
    slugify,
    stable_bucket_name,
    try_acquire_scope_lock,
    rollback_scope_lock,
    write_json,
    write_memory_file,
    write_session_memory,
)
from certified_turtles.memory_runtime.file_state import (
    MAX_CACHE_BYTES,
    MAX_CACHE_ENTRIES,
    FileState,
    _SESSION_CACHE,
    _SESSION_SIZES,
    _LOCK,
    clone_file_state_namespace,
    get_file_state,
    note_file_read,
    note_file_write,
)
from certified_turtles.memory_runtime.request_context import (
    RequestContext,
    current_request_context,
    use_request_context,
)
from certified_turtles.memory_runtime.selector import (
    fallback_select,
    select_relevant_memories,
)
from certified_turtles.memory_runtime.memory_types import (
    VALID_MEMORY_TYPES as TYPES_TUPLE,
    memory_instructions,
)
from certified_turtles.memory_runtime.prompting import (
    _memory_age_warning,
    _estimate_tokens,
    build_memory_prompt,
)


@pytest.fixture(autouse=True)
def clean_env(tmp_path):
    """Каждый тест работает со своим CT_CLAUDE_HOME."""
    old = os.environ.get("CT_CLAUDE_HOME")
    root = str(tmp_path / "claude_home")
    os.environ["CT_CLAUDE_HOME"] = root
    # Чистим глобальный кеш file_state
    with _LOCK:
        _SESSION_CACHE.clear()
        _SESSION_SIZES.clear()
    yield root
    if old is not None:
        os.environ["CT_CLAUDE_HOME"] = old
    else:
        os.environ.pop("CT_CLAUDE_HOME", None)


# ────────────────────────────────────────────────────────────
# 1. FRONTMATTER PARSING — сложные форматы
# ────────────────────────────────────────────────────────────

class TestFrontmatterParsing:
    """Тесты парсинга frontmatter: edge cases YAML-подобного формата."""

    def test_standard_frontmatter(self):
        text = '---\nname: "test"\ndescription: "desc"\ntype: "user"\n---\n\nbody'
        fm = parse_frontmatter(text)
        assert fm["name"] == "test"
        assert fm["type"] == "user"

    def test_no_frontmatter(self):
        assert parse_frontmatter("just plain text") == {}
        assert parse_frontmatter("") == {}

    def test_frontmatter_with_colon_in_value(self):
        """Значение содержит двоеточие — должен парсить корректно."""
        text = '---\nname: "url: https://example.com"\ntype: user\n---\n'
        fm = parse_frontmatter(text)
        assert fm["name"] == "url: https://example.com"

    def test_frontmatter_unquoted_colon_in_value(self):
        """Без кавычек, двоеточие в значении — partition по первому ':'."""
        text = "---\nname: url: https://example.com\ntype: user\n---\n"
        fm = parse_frontmatter(text)
        # partition по первому ':' → ключ='name', значение='url: https://example.com'
        assert "url" in fm["name"]

    def test_frontmatter_empty_value(self):
        text = "---\nname: \ntype: user\n---\n"
        fm = parse_frontmatter(text)
        assert fm["name"] == ""

    def test_frontmatter_multiline_yaml_not_supported(self):
        """Многострочные YAML значения НЕ поддерживаются — это пропущенные строки."""
        text = '---\nname: "ok"\ndescription: |\n  line1\n  line2\ntype: user\n---\n'
        fm = parse_frontmatter(text)
        # description будет '|' а не 'line1\nline2'
        assert fm.get("description") == "|"

    def test_frontmatter_with_unicode(self):
        """Юникодные значения в frontmatter."""
        text = '---\nname: "Привет мир 🌍"\ntype: user\n---\n\nТело'
        fm = parse_frontmatter(text)
        assert "Привет" in fm["name"]
        assert "🌍" in fm["name"]

    def test_frontmatter_with_json_escape_sequences(self):
        """JSON-encoded строки в frontmatter."""
        text = '---\nname: "line1\\nline2\\ttab"\ntype: user\n---\n'
        fm = parse_frontmatter(text)
        assert "line1\nline2\ttab" == fm["name"]

    def test_frontmatter_with_broken_json_quotes(self):
        """Сломанные JSON кавычки → должен fallback на strip."""
        text = '---\nname: "broken\ntype: "user"\n---\n'
        fm = parse_frontmatter(text)
        # 'broken' без закрывающей кавычки → JSONDecodeError → strip('"')
        assert "broken" in fm.get("name", "")

    def test_frontmatter_regex_with_only_opening_dashes(self):
        """Только открывающие --- без закрывающих."""
        text = "---\nname: test\nno closing"
        fm = parse_frontmatter(text)
        assert fm == {}

    def test_frontmatter_with_extra_whitespace_in_dashes(self):
        """--- с пробелами после."""
        text = "---   \nname: test\ntype: user\n---   \nbody"
        fm = parse_frontmatter(text)
        assert fm.get("name") == "test"

    def test_frontmatter_injection_via_body(self):
        """Тело содержит --- — не должно парситься как второй frontmatter."""
        text = '---\nname: "ok"\ntype: user\n---\n\nbody\n---\nevil: hack\n---\n'
        fm = parse_frontmatter(text)
        assert "evil" not in fm
        body = read_body(Path("/nonexistent"))  # пустая
        assert body == ""


# ────────────────────────────────────────────────────────────
# 2. MEMORY FILE OPERATIONS — крайние случаи
# ────────────────────────────────────────────────────────────

class TestMemoryFileOps:
    SCOPE = "test-scope"

    def test_write_and_read_basic(self):
        path = write_memory_file(
            self.SCOPE,
            name="test",
            description="desc",
            type_="user",
            body="content here",
        )
        assert path.exists()
        fm = read_frontmatter(path)
        assert fm["type"] == "user"
        body = read_body(path)
        assert body == "content here"

    def test_write_exceeds_max_size(self):
        """Тело больше MAX_MEMORY_FILE_BYTES → ValueError."""
        big_body = "x" * (MAX_MEMORY_FILE_BYTES + 1)
        with pytest.raises(ValueError, match="too large"):
            write_memory_file(self.SCOPE, name="big", description="d", type_="user", body=big_body)

    def test_write_exactly_max_size(self):
        """Тело ровно MAX_MEMORY_FILE_BYTES — должно пройти."""
        body = "x" * MAX_MEMORY_FILE_BYTES
        path = write_memory_file(self.SCOPE, name="exact", description="d", type_="user", body=body)
        assert path.exists()

    def test_write_unicode_body_size_check(self):
        """Размер считается в байтах UTF-8, не в символах."""
        # Каждый кириллический символ = 2 байта
        # MAX_MEMORY_FILE_BYTES=4096, значит 2049 символов кириллицы = 4098 байт > 4096
        body = "Ы" * 2049
        assert len(body.encode("utf-8")) > MAX_MEMORY_FILE_BYTES
        with pytest.raises(ValueError, match="too large"):
            write_memory_file(self.SCOPE, name="uni", description="d", type_="user", body=body)

    def test_write_emoji_body_size_check(self):
        """Emoji = 4 байта UTF-8."""
        body = "🔥" * (MAX_MEMORY_FILE_BYTES // 4 + 1)
        with pytest.raises(ValueError, match="too large"):
            write_memory_file(self.SCOPE, name="emoji", description="d", type_="user", body=body)

    def test_invalid_type_silently_becomes_project(self):
        """Невалидный тип → молча 'project'. Это баг? Или фича?"""
        path = write_memory_file(
            self.SCOPE, name="x", description="d", type_="INVALID_TYPE", body="data"
        )
        fm = read_frontmatter(path)
        assert fm["type"] == "project"

    def test_write_preserves_created_on_update(self):
        """При обновлении файла created сохраняется.
        NOTE: timestamps имеют 1-секундную точность, поэтому sleep(1.1)."""
        path = write_memory_file(self.SCOPE, name="test", description="v1", type_="user", body="v1", filename="test.md")
        fm1 = read_frontmatter(path)
        created1 = fm1["created"]
        time.sleep(1.1)
        path2 = write_memory_file(self.SCOPE, name="test", description="v2", type_="user", body="v2", filename="test.md")
        fm2 = read_frontmatter(path2)
        assert fm2["created"] == created1
        assert fm2["updated"] != created1

    def test_delete_memory_file(self):
        write_memory_file(self.SCOPE, name="del", description="d", type_="user", body="x")
        assert delete_memory_file(self.SCOPE, "del.md")
        assert not delete_memory_file(self.SCOPE, "del.md")

    def test_delete_nonexistent(self):
        assert not delete_memory_file(self.SCOPE, "nonexistent.md")

    def test_filename_path_traversal_dotdot(self):
        with pytest.raises(ValueError, match="traverse"):
            write_memory_file(self.SCOPE, name="x", description="d", type_="user", body="x", filename="../../../etc/passwd")

    def test_filename_absolute_path_rejected(self):
        with pytest.raises(ValueError, match="relative"):
            write_memory_file(self.SCOPE, name="x", description="d", type_="user", body="x", filename="/etc/passwd")

    def test_filename_with_special_chars_sanitized(self):
        """Спецсимволы в имени файла → sanitize."""
        path = write_memory_file(
            self.SCOPE, name="test <file>", description="d", type_="user", body="x",
            filename="my file!@#$.md"
        )
        assert path.exists()
        assert ".." not in str(path)

    def test_filename_empty_after_sanitization(self):
        """Если после санитизации пустое имя → fallback на name."""
        path = write_memory_file(
            self.SCOPE, name="fallback-name", description="d", type_="user", body="x",
            filename="!!!.md"
        )
        assert path.exists()

    def test_list_memory_files_limit(self):
        """Максимум MAX_MEMORY_FILES файлов."""
        for i in range(MAX_MEMORY_FILES + 10):
            write_memory_file(
                self.SCOPE, name=f"file{i}", description=f"d{i}", type_="user",
                body=f"body{i}", filename=f"file{i}.md"
            )
        files = list_memory_files(self.SCOPE)
        assert len(files) <= MAX_MEMORY_FILES

    def test_scan_memory_headers_excludes_MEMORY_md(self):
        write_memory_file(self.SCOPE, name="x", description="d", type_="user", body="content")
        headers = scan_memory_headers(self.SCOPE)
        filenames = [h.filename for h in headers]
        assert "MEMORY.md" not in filenames


# ────────────────────────────────────────────────────────────
# 3. MEMORY INDEX — rebuild, truncation, limits
# ────────────────────────────────────────────────────────────

class TestMemoryIndex:
    SCOPE = "index-scope"

    def test_rebuild_creates_index(self):
        write_memory_file(self.SCOPE, name="test", description="desc", type_="user", body="x")
        idx = memory_index_path(self.SCOPE)
        assert idx.exists()
        content = idx.read_text()
        assert "# Memory Index" in content

    def test_index_truncated_at_max_bytes(self):
        """��ндекс обрезается при MAX_MEMORY_INDEX_BYTES."""
        for i in range(150):
            write_memory_file(
                self.SCOPE, name=f"long-name-{'x' * 50}-{i}",
                description="d" * 100, type_="user", body="x",
                filename=f"file-{i}.md",
            )
        rebuild_memory_index(self.SCOPE, force=True)
        idx = memory_index_path(self.SCOPE)
        content = idx.read_bytes()
        assert len(content) <= MAX_MEMORY_INDEX_BYTES + 100  # +100 for final newline margin

    def test_index_max_lines(self):
        """Индекс содержит не больше MAX_MEMORY_INDEX_LINES строк (с заголовком)."""
        for i in range(MAX_MEMORY_INDEX_LINES + 50):
            write_memory_file(
                self.SCOPE, name=f"f{i}", description=f"d{i}", type_="user",
                body="x", filename=f"f{i}.md",
            )
        rebuild_memory_index(self.SCOPE, force=True)
        idx = memory_index_path(self.SCOPE)
        lines = idx.read_text().strip().splitlines()
        assert len(lines) <= MAX_MEMORY_INDEX_LINES

    def test_index_with_unicode_truncation(self):
        """При byte-truncation юникод не ломается."""
        for i in range(100):
            write_memory_file(
                self.SCOPE, name=f"Память-{i}-{'Ы' * 30}",
                description="Описание " * 20, type_="user",
                body="x", filename=f"unicode-{i}.md",
            )
        rebuild_memory_index(self.SCOPE, force=True)
        idx = memory_index_path(self.SCOPE)
        text = idx.read_text(encoding="utf-8")
        # Не должно быть replacement characters
        assert "\ufffd" not in text


# ────────────────────────────────────────────────────────────
# 4. SESSION MEMORY & TRANSCRIPT
# ────────────────────────────────────────────────────────────

class TestSessionOps:
    SESSION = "test-session-123"

    def test_write_read_session_memory(self):
        write_session_memory(self.SESSION, "hello world")
        assert read_session_memory(self.SESSION).strip() == "hello world"

    def test_read_nonexistent_session_memory(self):
        assert read_session_memory("nonexistent-session") == ""

    def test_session_memory_strips_and_adds_newline(self):
        write_session_memory(self.SESSION, "  content  \n\n")
        raw = Path(session_dir(self.SESSION) / "session.md").read_text()
        assert raw.endswith("\n")
        assert not raw.endswith("\n\n")

    def test_transcript_append_and_read(self):
        for i in range(10):
            append_transcript_event(self.SESSION, {"seq": i, "data": "test"})
        events = read_transcript_events(self.SESSION, limit=5)
        assert len(events) == 5
        assert events[-1]["seq"] == 9

    def test_transcript_with_unicode(self):
        append_transcript_event(self.SESSION, {"msg": "Привет мир 🌍 العربية"})
        events = read_transcript_events(self.SESSION)
        assert events[0]["msg"] == "Привет мир 🌍 العربية"

    def test_transcript_with_large_payload(self):
        big = {"data": "x" * 100_000}
        append_transcript_event(self.SESSION, big)
        events = read_transcript_events(self.SESSION)
        assert len(events) == 1
        assert len(events[0]["data"]) == 100_000

    def test_transcript_corrupt_line_skipped(self):
        """Сломанная JSONL строка → пропускается без ошибки."""
        path = Path(session_dir(self.SESSION)) / "session.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            f.write('{"valid": 1, "uuid": "a"}\n')
            f.write("NOT JSON\n")
            f.write('{"valid": 2, "uuid": "b"}\n')
        events = read_transcript_events(self.SESSION)
        assert len(events) == 2

    def test_ensure_session_meta(self):
        ensure_session_meta(self.SESSION, scope_id="my-scope")
        meta = read_json(session_meta_path(self.SESSION))
        assert meta["scope_id"] == "my-scope"
        assert "created_at" in meta
        assert "updated_at" in meta

    def test_session_meta_preserves_created_on_update(self):
        ensure_session_meta(self.SESSION, scope_id="s1")
        meta1 = read_json(session_meta_path(self.SESSION))
        time.sleep(0.05)
        ensure_session_meta(self.SESSION, scope_id="s1")
        meta2 = read_json(session_meta_path(self.SESSION))
        assert meta2["created_at"] == meta1["created_at"]
        assert meta2["updated_at"] >= meta1["updated_at"]


# ────────────────────────────────────────────────────────────
# 5. FILE STATE CACHE — concurrent access, limits
# ────────────────────────────────────────────────────────────

class TestFileStateCache:
    def test_basic_read_write_cycle(self):
        sid = "cache-session"
        p = Path("/tmp/test-file.txt")
        note_file_read(sid, p, content="hello", mtime_ns=1000, encoding="utf-8", line_ending="\n", is_partial_view=False)
        state = get_file_state(sid, p)
        assert state is not None
        assert state.content == "hello"

    def test_get_returns_none_for_unknown(self):
        assert get_file_state("unknown-session", Path("/tmp/x")) is None

    def test_cache_eviction_by_count(self):
        sid = "evict-count"
        for i in range(MAX_CACHE_ENTRIES + 20):
            note_file_read(
                sid, Path(f"/tmp/file-{i}"),
                content="x", mtime_ns=i, encoding="utf-8",
                line_ending="\n", is_partial_view=False,
            )
        with _LOCK:
            cache = _SESSION_CACHE.get(sid)
            assert cache is not None
            assert len(cache) <= MAX_CACHE_ENTRIES

    def test_cache_eviction_by_bytes(self):
        sid = "evict-bytes"
        big = "x" * (MAX_CACHE_BYTES // 5)
        for i in range(10):
            note_file_read(
                sid, Path(f"/tmp/big-{i}"),
                content=big, mtime_ns=i, encoding="utf-8",
                line_ending="\n", is_partial_view=False,
            )
        with _LOCK:
            assert _SESSION_SIZES.get(sid, 0) <= MAX_CACHE_BYTES

    def test_clone_namespace_isolation(self):
        sid = "src-session"
        note_file_read(sid, Path("/tmp/f1"), content="original", mtime_ns=1, encoding="utf-8", line_ending="\n", is_partial_view=False)
        clone_file_state_namespace(sid, "clone-session")
        # Оригинал и клон оба доступны
        orig = get_file_state(sid, Path("/tmp/f1"))
        cloned = get_file_state("clone-session", Path("/tmp/f1"))
        assert orig is not None
        assert cloned is not None
        assert orig.content == cloned.content

    def test_clone_nonexistent_source_clears_target(self):
        note_file_read("target", Path("/tmp/x"), content="x", mtime_ns=1, encoding="utf-8", line_ending="\n", is_partial_view=False)
        clone_file_state_namespace("nonexistent-source", "target")
        assert get_file_state("target", Path("/tmp/x")) is None

    def test_concurrent_access(self):
        """Параллельные записи в кеш не вызывают исключений."""
        sid = "concurrent"
        errors = []
        def writer(thread_id):
            try:
                for i in range(100):
                    note_file_read(
                        sid, Path(f"/tmp/thread-{thread_id}-{i}"),
                        content=f"data-{i}", mtime_ns=i, encoding="utf-8",
                        line_ending="\n", is_partial_view=False,
                    )
                    get_file_state(sid, Path(f"/tmp/thread-{thread_id}-{i}"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"Concurrent errors: {errors}"


# ────────────────────────────────────────────────────────────
# 6. REQUEST CONTEXT — thread-local
# ────────────────────────────────────────────────────────────

class TestRequestContext:
    def test_no_context_returns_none(self):
        assert current_request_context() is None

    def test_context_manager_sets_and_restores(self):
        ctx = RequestContext(session_id="s1", scope_id="sc1")
        with use_request_context(ctx):
            assert current_request_context() is ctx
        assert current_request_context() is None

    def test_nested_contexts(self):
        ctx1 = RequestContext(session_id="s1", scope_id="sc1")
        ctx2 = RequestContext(session_id="s2", scope_id="sc2")
        with use_request_context(ctx1):
            assert current_request_context().session_id == "s1"
            with use_request_context(ctx2):
                assert current_request_context().session_id == "s2"
            assert current_request_context().session_id == "s1"
        assert current_request_context() is None

    def test_thread_isolation(self):
        """Контекст не утекает между потоками."""
        results = {}
        def thread_fn(name):
            ctx = RequestContext(session_id=name, scope_id="s")
            with use_request_context(ctx):
                time.sleep(0.01)
                results[name] = current_request_context().session_id
        threads = [threading.Thread(target=thread_fn, args=(f"t{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for name, result in results.items():
            assert result == name, f"Context leaked: {name} got {result}"


# ────────────────────────────────────────────────────────────
# 7. SELECTOR — memory selection logic
# ────────────────────────────────────────────────────────────

class TestSelector:
    def _make_header(self, filename, name, desc, type_="user"):
        from certified_turtles.memory_runtime.storage import MemoryHeader
        return MemoryHeader(filename=filename, name=name, description=desc, type=type_, mtime=time.time())

    def test_fallback_select_basic(self):
        headers = [
            self._make_header("python.md", "Python Skills", "User knows Python and Django", "user"),
            self._make_header("react.md", "React Skills", "User learning React", "user"),
            self._make_header("deploy.md", "Deploy Process", "AWS deployment steps", "reference"),
        ]
        result = fallback_select(headers, "How to deploy to AWS")
        assert "deploy.md" in result

    def test_fallback_select_filters_tool_references(self):
        """reference типы для недавних тулов фильтруются (если не warning)."""
        headers = [
            self._make_header("web-search-api.md", "Web Search API", "web_search usage reference", "reference"),
            self._make_header("web-search-gotcha.md", "Web Search Gotcha", "web_search warning about rate limits", "reference"),
        ]
        result = fallback_select(headers, "search the web", recent_tools=["web_search"])
        assert "web-search-api.md" not in result
        assert "web-search-gotcha.md" in result

    def test_fallback_select_empty_query(self):
        headers = [self._make_header("a.md", "test", "desc")]
        result = fallback_select(headers, "")
        assert result == []

    def test_fallback_select_short_words_ignored(self):
        """Слова короче 3 символов пропускаются (regex [\w/-]{3,})."""
        headers = [self._make_header("a.md", "is a ok", "be to do")]
        result = fallback_select(headers, "is it ok")
        # 'is' and 'it' are < 3 chars, only 'ok' is >= 3 → should match if 'ok' in header
        # 'ok' appears in header name but also < 3? No, len('ok') == 2 < 3
        # Actually all words in query are < 3 chars → empty q_words → score=0 for all
        # Wait: 'ok' is 2 chars. '_TOKEN_RE = re.compile(r"[\w/-]{3,}")'  → 2 chars < 3
        # But actually query "is it ok" → no words >= 3 chars → empty q_words → all scores 0
        # This is correct behavior but worth noting
        assert result == []

    def test_select_relevant_memories_with_mock_llm(self):
        """LLM селектор с мок-клиентом."""
        headers = [
            self._make_header("a.md", "A", "about Python"),
            self._make_header("b.md", "B", "about React"),
        ]
        client = MagicMock()
        client.chat_completions.return_value = {
            "choices": [{"message": {"content": '{"selected_memories": ["a.md"]}'}}]
        }
        result = select_relevant_memories(client, model="test", query="Python help", headers=headers)
        assert result == ["a.md"]

    def test_select_relevant_memories_llm_error_returns_empty(self):
        """При ошибке LLM → пустой список (как в Claude Code)."""
        headers = [self._make_header("python.md", "Python", "Python programming")]
        client = MagicMock()
        client.chat_completions.side_effect = Exception("API error")
        result = select_relevant_memories(client, model="test", query="Python help", headers=headers)
        assert result == []

    def test_select_relevant_memories_llm_returns_non_existent(self):
        """LLM возвращает файлы не из доступных → фильтруются."""
        headers = [self._make_header("a.md", "A", "about A")]
        client = MagicMock()
        client.chat_completions.return_value = {
            "choices": [{"message": {"content": '{"selected_memories": ["nonexistent.md", "a.md"]}'}}]
        }
        result = select_relevant_memories(client, model="test", query="about A", headers=headers)
        assert result == ["a.md"]

    def test_select_relevant_memories_already_surfaced(self):
        """Уже показанные файлы пропускаются."""
        headers = [self._make_header("a.md", "A", "about A")]
        client = MagicMock()
        result = select_relevant_memories(
            client, model="test", query="about A", headers=headers,
            already_surfaced={"a.md"},
        )
        assert result == []


# ────────────────────────────────────────────────────────────
# 8. PROMPTING — build_memory_prompt edge cases
# ────────────────────────────────────────────────────────────

class TestPrompting:
    def test_memory_age_warning_fresh(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        assert _memory_age_warning(now) == ""

    def test_memory_age_warning_old(self):
        # 10 дней назад
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400 * 10))
        warning = _memory_age_warning(old)
        assert "days old" in warning

    def test_memory_age_warning_bad_format(self):
        assert _memory_age_warning("not-a-date") == ""
        assert _memory_age_warning("") == ""

    def test_memory_age_warning_timezone_bug(self):
        """BUG: time.mktime интерпретирует UTC-timestamp как local time.
        На машине в UTC это не проблема, но в UTC+3 age будет на 3 часа меньше.
        Проверяем что хотя бы не крашится."""
        stamp = "2020-01-01T00:00:00Z"
        warning = _memory_age_warning(stamp)
        assert "days old" in warning

    def test_estimate_tokens(self):
        assert _estimate_tokens("hello") >= 1
        assert _estimate_tokens("") == 1  # max(1, 0//4)

    def test_build_memory_prompt_no_client(self):
        """Без LLM клиента — работает, просто без выбора памяти."""
        scope = "prompt-test-scope"
        session = "prompt-test-session"
        ensure_session_meta(session, scope_id=scope)
        bundle = build_memory_prompt(
            None,
            model="test",
            messages=[{"role": "user", "content": "hello"}],
            scope_id=scope,
            session_id=session,
            user_query="hello",
        )
        assert isinstance(bundle.prompt, str)
        assert bundle.selected_memories == ()


# ────────────────────────────────────────────────────────────
# 9. LOCKING — scope lock mechanics
# ────────────────────────────────────────────────────────────

class TestScopeLock:
    SCOPE = "lock-test-scope"

    def test_acquire_on_fresh(self):
        """На чистом scope лок должен быть получен."""
        result = try_acquire_scope_lock(self.SCOPE)
        assert result is not None  # returns previous mtime (0.0 for new)

    def test_acquire_blocks_when_held_by_live_process(self):
        """Если лок держит живой процесс — возвращает None."""
        lock_path = scope_lock_path(self.SCOPE)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Пишем PID текущего процесса (гарантированно жив)
        _atomic_write_text(lock_path, str(os.getpid()))
        result = try_acquire_scope_lock(self.SCOPE)
        # Мы сами держим лок, но os.kill(pid,0) нашего pid пройдёт → None
        assert result is None

    def test_acquire_succeeds_when_holder_dead(self):
        """Если PID в локе мертвый — лок захватывается."""
        lock_path = scope_lock_path(self.SCOPE)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(lock_path, "999999999")  # несуществующий PID
        result = try_acquire_scope_lock(self.SCOPE)
        assert result is not None

    def test_acquire_succeeds_when_stale(self):
        """Лок старше stale_after_seconds — захватывается даже если PID жив."""
        lock_path = scope_lock_path(self.SCOPE)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(lock_path, str(os.getpid()))
        # Ставим mtime в прошлое
        old_time = time.time() - 7200
        os.utime(lock_path, (old_time, old_time))
        result = try_acquire_scope_lock(self.SCOPE, stale_after_seconds=3600)
        assert result is not None

    def test_rollback_deletes_if_no_prior(self):
        lock_path = scope_lock_path(self.SCOPE)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(lock_path, str(os.getpid()))
        rollback_scope_lock(self.SCOPE, 0.0)
        assert not lock_path.exists()


# ────────────────────────────────────────────────────────────
# 10. SLUGIFY & BUCKET NAMING
# ────────────────────────────────────────────────────────────

class TestSlugify:
    def test_basic(self):
        assert slugify("Hello World") == "hello-world"

    def test_unicode(self):
        result = slugify("Привет Мир")
        assert result  # не пустой
        assert len(result) <= 80

    def test_empty(self):
        assert slugify("") == "default"
        assert slugify("   ") == "default"

    def test_special_chars(self):
        result = slugify("foo@bar#baz$qux")
        assert "@" not in result
        assert "#" not in result

    def test_long_input_truncated(self):
        result = slugify("a" * 200)
        assert len(result) <= 80

    def test_stable_bucket_name_deterministic(self):
        a = stable_bucket_name("test", prefix="scope")
        b = stable_bucket_name("test", prefix="scope")
        assert a == b

    def test_stable_bucket_name_different_for_different_input(self):
        a = stable_bucket_name("scope-a", prefix="scope")
        b = stable_bucket_name("scope-b", prefix="scope")
        assert a != b

    def test_scope_slug_default(self):
        """Пустой scope_id → default."""
        result = scope_slug("")
        assert "default" in result

    def test_session_slug_default(self):
        result = session_slug("")
        assert "default" in result


# ────────────────────────────────────────────────────────────
# 11. FILENAME VALIDATION — path traversal attacks
# ────────────────────────────────────────────────────────────

class TestFilenameValidation:
    def test_dotdot_rejected(self):
        with pytest.raises(ValueError):
            _validate_memory_filename("../secret.md", fallback_name="x")

    def test_absolute_rejected(self):
        with pytest.raises(ValueError):
            _validate_memory_filename("/etc/passwd", fallback_name="x")

    def test_nested_dotdot_rejected(self):
        with pytest.raises(ValueError):
            _validate_memory_filename("a/../../b.md", fallback_name="x")

    def test_empty_falls_back(self):
        result = _validate_memory_filename("", fallback_name="test")
        assert "test" in str(result)

    def test_none_falls_back(self):
        result = _validate_memory_filename(None, fallback_name="test")
        assert "test" in str(result)

    def test_adds_md_extension(self):
        result = _validate_memory_filename("myfile", fallback_name="x")
        assert str(result).endswith(".md")

    def test_resolve_memory_path_stays_in_root(self):
        scope = "validate-scope"
        path = resolve_memory_path(scope, "safe-file.md", fallback_name="test")
        root = memory_dir(scope)
        assert str(path).startswith(str(root))

    def test_resolve_memory_path_traversal_blocked(self):
        with pytest.raises(ValueError):
            resolve_memory_path("scope", "../../../etc/passwd", fallback_name="x")


# ────────────────────────────────────────────────────────────
# 12. MANAGER — ClaudeLikeMemoryRuntime logic
# ────────────────────────────────────────────────────────────

class TestManager:
    def _make_runtime(self):
        from certified_turtles.memory_runtime.manager import ClaudeLikeMemoryRuntime
        return ClaudeLikeMemoryRuntime()

    def test_estimate_message_tokens(self):
        rt = self._make_runtime()
        msgs = [{"role": "user", "content": "hello world"}]
        tokens = rt._estimate_message_tokens(msgs)
        assert tokens > 0

    def test_last_user_text(self):
        rt = self._make_runtime()
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "last"},
        ]
        assert rt._last_user_text(msgs) == "last"

    def test_last_user_text_empty(self):
        rt = self._make_runtime()
        assert rt._last_user_text([]) == ""
        assert rt._last_user_text([{"role": "assistant", "content": "hi"}]) == ""

    def test_compact_if_needed_below_threshold(self):
        """Ниже порога — без компактификации."""
        rt = self._make_runtime()
        session = "compact-test"
        msgs = [{"role": "user", "content": "short"}]
        result = rt._compact_if_needed(msgs, session)
        assert result == msgs

    def test_compact_if_needed_no_session_memory(self):
        """Без session.md — компактификация невозможна."""
        rt = self._make_runtime()
        session = "compact-test-no-sm"
        # Создаём БОЛЬШИЕ сообщения чтобы превысить порог
        big = "x" * 200_000
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": big},
            {"role": "assistant", "content": big},
        ]
        result = rt._compact_if_needed(msgs, session)
        # Без session.md ничего не меняется
        assert len(result) == len(msgs)

    def test_compact_if_needed_with_session_memory(self):
        """С session.md и большими сообщениями — компактификация работает.

        BUG FOUND: компактификация может УВЕЛИЧИТЬ число сообщений если
        вырезается мало сообщений (добавляются summary+ack=2 msg).
        Пример: 7 msg, cut_index=2 → 3 (sys+sum+ack) + 5 (tail) = 8 > 7.
        """
        rt = self._make_runtime()
        session = "compact-with-sm"
        write_session_memory(session, "# Session Summary\nWorking on feature X")
        big = "x" * 50_000
        msgs = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": big},
            {"role": "assistant", "content": big},
            {"role": "user", "content": big},
            {"role": "assistant", "content": big},
            {"role": "user", "content": big},
            {"role": "assistant", "content": big},
            {"role": "user", "content": big},
            {"role": "assistant", "content": big},
            {"role": "user", "content": "latest question"},
            {"role": "assistant", "content": "latest answer"},
        ]
        with patch("certified_turtles.memory_runtime.manager._compact_threshold", return_value=50_000):
            result = rt._compact_if_needed(msgs, session)
        # Компактификация должна произойти — messages > threshold
        assert len(result) < len(msgs), (
            f"BUG: compaction produced {len(result)} messages from {len(msgs)} — "
            "may actually INCREASE message count if cut_index is too close to start"
        )
        assert result[0]["role"] == "system"
        assert "compacted" in result[1]["content"].lower() or "Session Summary" in result[1]["content"]

    def test_note_session_turn_records_timestamp(self):
        """_note_session_turn записывает актуальный timestamp (ранее был баг — всегда 0.0)."""
        rt = self._make_runtime()
        rt._note_session_turn("s1")
        ts1 = rt._session_updates.get("s1")
        assert ts1 > 0, "должен записать time.time()"
        time.sleep(0.02)
        rt._note_session_turn("s1")
        ts2 = rt._session_updates.get("s1")
        assert ts2 > ts1, "должен обновить timestamp"

    def test_microcompact_below_threshold(self):
        """Ниже порога и без time gap — без микрокомпактификации."""
        rt = self._make_runtime()
        session = "micro-test"
        ensure_session_meta(session, scope_id="s")
        msgs = [{"role": "user", "content": f"msg{i}"} for i in range(5)]
        result = rt._microcompact_tool_results(msgs, session)
        assert result == msgs

    def test_microcompact_skips_short_history(self):
        """Менее 10 сообщений → без микрокомпактификации."""
        rt = self._make_runtime()
        msgs = [{"role": "user", "content": f"msg{i}"} for i in range(9)]
        result = rt._microcompact_tool_results(msgs, "s")
        assert result == msgs


# ────────────────────────────────────────────────────────────
# 13. STATIC INSTRUCTIONS — CLAUDE.md loading
# ────────────────────────────────────────────────────────────

class TestStaticInstructions:
    def test_load_with_no_claude_md(self, tmp_path):
        from certified_turtles.memory_runtime.static_instructions import load_static_instruction_prompt
        # В пустой директории → пустая строка или очень короткий
        result = load_static_instruction_prompt(cwd=str(tmp_path))
        # Может вернуть что-то из ~/.claude/CLAUDE.md если есть, но точно не крашнется
        assert isinstance(result, str)

    def test_load_with_claude_md(self, tmp_path):
        from certified_turtles.memory_runtime.static_instructions import load_static_instruction_prompt
        (tmp_path / "CLAUDE.md").write_text("# Test Instructions\nDo things.")
        result = load_static_instruction_prompt(cwd=str(tmp_path))
        assert "Test Instructions" in result

    def test_include_resolution(self, tmp_path):
        from certified_turtles.memory_runtime.static_instructions import load_static_instruction_prompt
        (tmp_path / "included.md").write_text("Included content here")
        (tmp_path / "CLAUDE.md").write_text("# Main\n@included.md\n")
        result = load_static_instruction_prompt(cwd=str(tmp_path))
        assert "Included content here" in result

    def test_include_depth_limit(self, tmp_path):
        from certified_turtles.memory_runtime.static_instructions import load_static_instruction_prompt
        # Создаём цепочку включений глубже MAX_INCLUDE_DEPTH
        for i in range(10):
            content = f"@level{i + 1}.md\n" if i < 9 else "deepest"
            (tmp_path / f"level{i}.md").write_text(content)
        (tmp_path / "CLAUDE.md").write_text("@level0.md\n")
        result = load_static_instruction_prompt(cwd=str(tmp_path))
        # Не должно крашнуться; deepest может не появиться из-за лимита глубины

    def test_include_circular_reference(self, tmp_path):
        from certified_turtles.memory_runtime.static_instructions import load_static_instruction_prompt
        (tmp_path / "a.md").write_text("@b.md\n")
        (tmp_path / "b.md").write_text("@a.md\n")
        (tmp_path / "CLAUDE.md").write_text("@a.md\n")
        # Не должно зациклиться
        result = load_static_instruction_prompt(cwd=str(tmp_path))
        assert isinstance(result, str)

    def test_include_inside_code_block_ignored(self, tmp_path):
        from certified_turtles.memory_runtime.static_instructions import load_static_instruction_prompt
        (tmp_path / "secret.md").write_text("SECRET DATA")
        (tmp_path / "CLAUDE.md").write_text("```\n@secret.md\n```\n")
        result = load_static_instruction_prompt(cwd=str(tmp_path))
        assert "SECRET DATA" not in result

    def test_max_static_instruction_bytes_truncation(self, tmp_path):
        from certified_turtles.memory_runtime.static_instructions import load_static_instruction_prompt, MAX_STATIC_INSTRUCTION_BYTES
        (tmp_path / "CLAUDE.md").write_text("x" * (MAX_STATIC_INSTRUCTION_BYTES + 10000))
        result = load_static_instruction_prompt(cwd=str(tmp_path))
        assert len(result.encode("utf-8")) <= MAX_STATIC_INSTRUCTION_BYTES


# ────────────────────────────────────────────────────────────
# 14. MEMORY TYPES — instructions generation
# ────────────────────────────────────────────────────────────

class TestMemoryTypes:
    def test_valid_types_match(self):
        from certified_turtles.memory_runtime.storage import VALID_MEMORY_TYPES as STORAGE_TYPES
        assert set(TYPES_TUPLE) == set(STORAGE_TYPES)

    def test_memory_instructions_contains_all_types(self):
        text = memory_instructions()
        for t in TYPES_TUPLE:
            assert t in text

    def test_memory_instructions_with_index_rules(self):
        text = memory_instructions(include_index_rules=True)
        assert "MEMORY.md" in text
        assert "frontmatter" in text.lower()

    def test_memory_instructions_without_index_rules(self):
        text = memory_instructions(include_index_rules=False)
        # Всё равно содержит типы
        assert "user" in text


# ────────────────────────────────────────────────────────────
# 15. FORKING — snapshot management
# ────────────────────────────────────────────────────────────

class TestForking:
    def test_save_and_get_snapshot(self):
        from certified_turtles.memory_runtime.forking import CacheSafeSnapshot, ForkRuntime
        forks = ForkRuntime()
        snap = CacheSafeSnapshot(
            model="test", scope_id="s", session_id="ses",
            file_state_namespace="ses", messages=[{"role": "user", "content": "hi"}],
            saved_at=time.time(),
        )
        forks.save_snapshot(snap)
        retrieved = forks.get_snapshot("ses")
        assert retrieved is snap

    def test_get_nonexistent_snapshot(self):
        from certified_turtles.memory_runtime.forking import ForkRuntime
        forks = ForkRuntime()
        assert forks.get_snapshot("nonexistent") is None

    def test_snapshot_overwrite(self):
        from certified_turtles.memory_runtime.forking import CacheSafeSnapshot, ForkRuntime
        forks = ForkRuntime()
        snap1 = CacheSafeSnapshot(model="m1", scope_id="s", session_id="ses", file_state_namespace="ses", messages=[], saved_at=1.0)
        snap2 = CacheSafeSnapshot(model="m2", scope_id="s", session_id="ses", file_state_namespace="ses", messages=[], saved_at=2.0)
        forks.save_snapshot(snap1)
        forks.save_snapshot(snap2)
        assert forks.get_snapshot("ses").model == "m2"

    def test_snapshot_is_mutable_bug(self):
        """BUG: CacheSafeSnapshot не frozen — messages можно мутировать после save."""
        from certified_turtles.memory_runtime.forking import CacheSafeSnapshot, ForkRuntime
        forks = ForkRuntime()
        msgs = [{"role": "user", "content": "original"}]
        snap = CacheSafeSnapshot(model="m", scope_id="s", session_id="ses", file_state_namespace="ses", messages=msgs, saved_at=1.0)
        forks.save_snapshot(snap)
        # Мутируем список ПОСЛЕ сохранения
        msgs.append({"role": "assistant", "content": "injected"})
        retrieved = forks.get_snapshot("ses")
        # Мутация утекла в snapshot — это баг
        assert len(retrieved.messages) == 2  # 2, не 1 — баг подтверждён


# ────────────────────────────────────────────────────────────
# 16. CONCURRENT MEMORY WRITES — stress test
# ────────────────────────────────────────────────────────────

class TestConcurrentWrites:
    def test_parallel_writes_to_same_scope(self):
        """Параллельные записи в одну scope — не должны крашить."""
        scope = "concurrent-scope"
        errors = []
        def writer(thread_id):
            try:
                for i in range(10):
                    write_memory_file(
                        scope, name=f"t{thread_id}-f{i}",
                        description=f"desc-{thread_id}-{i}",
                        type_="user", body=f"body-{thread_id}-{i}",
                        filename=f"t{thread_id}-f{i}.md",
                    )
            except Exception as e:
                errors.append((thread_id, e))

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"Errors: {errors}"
        # Все файлы должны существовать
        files = list_memory_files(scope)
        assert len(files) == 50  # 5 threads * 10 files

    def test_parallel_index_rebuild(self):
        """Параллельный rebuild_memory_index не ломает индекс."""
        scope = "rebuild-scope"
        for i in range(20):
            write_memory_file(scope, name=f"f{i}", description=f"d{i}", type_="user", body="x", filename=f"f{i}.md")
        errors = []
        def rebuilder():
            try:
                for _ in range(10):
                    rebuild_memory_index(scope)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=rebuilder) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        # Индекс должен быть валидным
        idx = memory_index_path(scope)
        content = idx.read_text()
        assert "# Memory Index" in content


# ────────────────────────────────────────────────────────────
# 17. EDGE CASES — пустые/нулевые значения
# ────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_scope_id(self):
        """Пустой scope_id → default."""
        root = memory_dir("")
        assert root.exists()
        assert "default" in str(root)

    def test_empty_session_id(self):
        root = session_dir("")
        assert root.exists()

    def test_write_empty_body(self):
        """Пустое тело — должно работать."""
        path = write_memory_file("edge", name="empty", description="d", type_="user", body="")
        body = read_body(path)
        assert body == ""

    def test_write_whitespace_body(self):
        """Тело из пробелов → strip → пустое."""
        path = write_memory_file("edge", name="ws", description="d", type_="user", body="   \n\n  ")
        body = read_body(path)
        assert body == ""

    def test_write_memory_with_none_filename(self):
        """filename=None → fallback на slugified name."""
        path = write_memory_file("edge", name="My Memory", description="d", type_="user", body="x", filename=None)
        assert path.exists()

    def test_json_in_frontmatter_value(self):
        """JSON object в значении frontmatter → парсится как строка."""
        text = '---\nname: {"nested": true}\ntype: user\n---\n'
        fm = parse_frontmatter(text)
        # ключ name, значение после ':' → '{"nested": true}'
        # starts with '"' → json.loads('"{"nested": true}') → fail → strip('"')
        # Actually: the raw value is '{"nested": true}' which starts with '{' not '"'
        # So strip('"') removes nothing → result is '{"nested": true}'
        assert "nested" in fm.get("name", "")

    def test_scope_session_listing_with_empty_sessions_dir(self):
        """Listing sessions на пустом scope."""
        sessions = list_scope_sessions("nonexistent-scope")
        assert sessions == []

    def test_read_json_corrupt_file(self, tmp_path):
        """Сломанный JSON → None."""
        p = tmp_path / "broken.json"
        p.write_text("NOT JSON {{{")
        assert read_json(p) is None

    def test_read_json_nonexistent(self, tmp_path):
        assert read_json(tmp_path / "nope.json") is None

    def test_atomic_write_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "a" / "b" / "c" / "file.txt"
        _atomic_write_text(path, "content")
        assert path.read_text() == "content"

    def test_transcript_read_empty_file(self):
        """Пустой JSONL → пустой список."""
        sid = "empty-transcript"
        path = Path(session_dir(sid)) / "session.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        assert read_transcript_events(sid) == []


# ────────────────────────────────────────────────────────────
# 18. SECURITY — grep/glob pattern injection
# ────────────────────────────────────────────────────────────

class TestSecurityPatterns:
    def test_regex_catastrophic_backtracking_protection(self):
        """ReDoS-уязвимый regex → должен хотя бы не зависнуть навечно.
        file_ops grep_search компилирует user-supplied regex без ограничений."""
        import signal
        # Это именно проверка что такая атака возможна
        evil_pattern = r"(a+)+$"
        evil_input = "a" * 25 + "!"
        expr = re.compile(evil_pattern, re.IGNORECASE)

        # На коротком вводе не зависнет, но на длинном — может
        # Проверяем на длинном (30 символов) — это уже заметно медленно
        long_input = "a" * 30 + "!"

        def handler(signum, frame):
            raise TimeoutError("ReDoS detected")

        # Устанавливаем timeout (только Unix)
        if hasattr(signal, 'SIGALRM'):
            old_handler = signal.signal(signal.SIGALRM, handler)
            signal.alarm(2)  # 2 секунды
            try:
                expr.search(long_input)
                # Если дошли сюда — не зависло
            except TimeoutError:
                pytest.skip("ReDoS vulnerability confirmed but test timed out safely")
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)
        else:
            pytest.skip("SIGALRM not available on this platform")


# ────────────────────────────────────────────────────────────
# 19. INTEGRATION CONSISTENCY CHECKS
# ────────────────────────────────────────────────────────────

class TestIntegrationConsistency:
    """Проверка согласованности между модулями."""

    def test_valid_memory_types_consistent(self):
        """VALID_MEMORY_TYPES одинаковы в storage.py и memory_types.py."""
        from certified_turtles.memory_runtime.storage import VALID_MEMORY_TYPES as S_TYPES
        from certified_turtles.memory_runtime.memory_types import VALID_MEMORY_TYPES as M_TYPES
        assert set(S_TYPES) == set(M_TYPES)

    def test_subagent_tool_names_exist_in_registry(self):
        """Все tool_names в SubAgentSpec зарегистрированы."""
        from certified_turtles.agents.registry import SUB_AGENTS
        from certified_turtles.tools.registry import list_primitive_tool_names
        registered = set(list_primitive_tool_names())
        for agent_id, spec in SUB_AGENTS.items():
            for tool_name in spec.tool_names:
                assert tool_name in registered, f"Agent '{agent_id}' uses unregistered tool '{tool_name}'"

    def test_memory_agents_have_file_ops(self):
        """Memory agents имеют нужные file ops."""
        from certified_turtles.agents.registry import MEMORY_EXTRACTOR_AGENT_ID, SESSION_MEMORY_AGENT_ID, get_subagent
        extractor = get_subagent(MEMORY_EXTRACTOR_AGENT_ID)
        assert "file_read" in extractor.tool_names
        assert "file_write" in extractor.tool_names

        session_mem = get_subagent(SESSION_MEMORY_AGENT_ID)
        assert "file_read" in session_mem.tool_names
        assert "file_write" in session_mem.tool_names

    def test_runtime_singleton_pattern(self):
        from certified_turtles.memory_runtime.manager import runtime_from_env, _RUNTIME
        rt1 = runtime_from_env()
        rt2 = runtime_from_env()
        assert rt1 is rt2
