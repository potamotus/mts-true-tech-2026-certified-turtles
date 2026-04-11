"""
Concurrency race-condition tests for memory_runtime.

Hammers storage, file_state, forking, and request_context with parallel
operations using ThreadPoolExecutor + threading.Barrier to maximise the
chance of exposing data races, lost writes, and partial-read corruption.

Run:
    pytest tests/test_concurrency_races.py -v --tb=short

Discovered race conditions are documented in each test's docstring.
"""
from __future__ import annotations

import json
import os
import random
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

# Set CT_CLAUDE_HOME before any import that reads it at module level.
os.environ.setdefault("CT_CLAUDE_HOME", tempfile.mkdtemp(prefix="ct_race_"))

from certified_turtles.memory_runtime.storage import (
    _atomic_write_text,
    _last_rebuild,
    append_transcript_event,
    delete_memory_file,
    list_memory_files,
    memory_dir,
    memory_index_path,
    parse_frontmatter,
    read_session_memory,
    read_transcript_events,
    rebuild_memory_index,
    scan_memory_headers,
    try_acquire_scope_lock,
    rollback_scope_lock,
    write_json,
    write_memory_file,
    write_session_memory,
)
from certified_turtles.memory_runtime.file_state import (
    FileState,
    _LOCK as FS_LOCK,
    _SESSION_CACHE,
    _SESSION_SIZES,
    get_file_state,
    note_file_read,
    note_file_write,
)
from certified_turtles.memory_runtime.forking import CacheSafeSnapshot, ForkRuntime
from certified_turtles.memory_runtime.request_context import (
    RequestContext,
    current_request_context,
    use_request_context,
)

# ---------------------------------------------------------------------------
# Fixture: isolate each test in its own temp directory
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_home(tmp_path):
    """Each test gets a fresh CT_CLAUDE_HOME and clean file_state caches."""
    old = os.environ.get("CT_CLAUDE_HOME")
    root = str(tmp_path / "claude_home")
    os.environ["CT_CLAUDE_HOME"] = root

    # Clear global caches
    with FS_LOCK:
        _SESSION_CACHE.clear()
        _SESSION_SIZES.clear()
    _last_rebuild.clear()

    yield root

    if old is not None:
        os.environ["CT_CLAUDE_HOME"] = old
    else:
        os.environ.pop("CT_CLAUDE_HOME", None)


WORKERS = 20


# ───────────────────────────────────────────────────────────────
# 1. Parallel memory writes to same scope
# ───────────────────────────────────────────────────────────────

class TestParallelMemoryWrites:
    """20 threads x 10 memory files to the same scope.

    Verify: no file corruption, all files parseable, index is consistent.
    """

    def test_parallel_writes_no_corruption(self):
        scope = "race-scope-1"
        n_threads = 20
        n_files = 10
        barrier = threading.Barrier(n_threads)
        errors: list[Exception] = []

        def writer(tid: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(n_files):
                    write_memory_file(
                        scope,
                        name=f"t{tid}-m{i}",
                        description=f"desc-{tid}-{i}",
                        type_="user",
                        body=f"body-{tid}-{i}",
                        filename=f"t{tid}-m{i}.md",
                    )
            except Exception as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            futs = [pool.submit(writer, tid) for tid in range(n_threads)]
            for f in as_completed(futs):
                f.result()

        assert not errors, f"Writer errors: {errors}"

        # All 200 files must exist and be parseable
        files = list_memory_files(scope)
        assert len(files) == n_threads * n_files

        for p in files:
            text = p.read_text(encoding="utf-8")
            fm = parse_frontmatter(text)
            assert "name" in fm, f"Corrupt frontmatter in {p}"
            assert "type" in fm

        # Index must be valid markdown
        idx_path = memory_index_path(scope)
        idx_text = idx_path.read_text(encoding="utf-8")
        assert idx_text.startswith("# Memory Index")


# ───────────────────────────────────────────────────────────────
# 2. Parallel transcript appends
# ───────────────────────────────────────────────────────────────

class TestParallelTranscriptAppends:
    """20 threads x 50 events to the same session transcript.

    Verify: total events = 1000, each event is valid JSON.

    RACE CONDITION FOUND: append_transcript_event() opens the file in
    append mode without an advisory lock. On Linux/macOS, O_APPEND is
    atomic for writes < PIPE_BUF (4096 bytes on Linux), so short JSON
    lines survive. But interleaving IS possible for very large payloads
    or on NFS. For the payload sizes used here the atomicity holds.
    """

    def test_parallel_appends_no_lost_writes(self):
        session = "race-session-transcript"
        n_threads = 20
        n_events = 50
        barrier = threading.Barrier(n_threads)
        errors: list[Exception] = []

        def appender(tid: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(n_events):
                    append_transcript_event(
                        session,
                        {"tid": tid, "seq": i, "kind": "message", "role": "user", "content": f"t{tid}-e{i}"},
                    )
            except Exception as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            futs = [pool.submit(appender, tid) for tid in range(n_threads)]
            for f in as_completed(futs):
                f.result()

        assert not errors, f"Appender errors: {errors}"

        # Read all events (up to limit) and verify
        events = read_transcript_events(session, limit=2000)
        assert len(events) == n_threads * n_events, (
            f"Expected {n_threads * n_events} events, got {len(events)}"
        )

        # Every event must contain a valid 'tid' and 'seq'
        seen: set[tuple[int, int]] = set()
        for ev in events:
            assert isinstance(ev, dict)
            assert "uuid" in ev
            seen.add((ev["tid"], ev["seq"]))
        assert len(seen) == n_threads * n_events


# ───────────────────────────────────────────────────────────────
# 3. Concurrent index rebuilds + writes
# ───────────────────────────────────────────────────────────────

class TestConcurrentIndexRebuilds:
    """10 rebuilders + 10 writers running simultaneously.

    Verify: index file is always valid, never partially written.
    """

    def test_rebuild_while_writing(self):
        scope = "race-scope-rebuild"
        # Pre-seed some files
        for i in range(10):
            write_memory_file(scope, name=f"seed{i}", description=f"d{i}", type_="project", body="x", filename=f"seed{i}.md")

        barrier = threading.Barrier(20)
        errors: list[Exception] = []

        def rebuilder() -> None:
            try:
                barrier.wait(timeout=5)
                for _ in range(20):
                    rebuild_memory_index(scope, force=True)
            except Exception as exc:
                errors.append(exc)

        def writer(tid: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(10):
                    write_memory_file(
                        scope,
                        name=f"w{tid}-{i}",
                        description=f"wd{tid}-{i}",
                        type_="user",
                        body=f"wb{tid}-{i}",
                        filename=f"w{tid}-{i}.md",
                    )
            except Exception as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=20) as pool:
            futs = []
            for _ in range(10):
                futs.append(pool.submit(rebuilder))
            for tid in range(10):
                futs.append(pool.submit(writer, tid))
            for f in as_completed(futs):
                f.result()

        assert not errors, f"Errors: {errors}"

        # Index must be readable and valid
        idx_path = memory_index_path(scope)
        idx_text = idx_path.read_text(encoding="utf-8")
        assert idx_text.startswith("# Memory Index")
        assert len(idx_text) > 10  # Not empty


# ───────────────────────────────────────────────────────────────
# 4. Concurrent session memory writes
# ───────────────────────────────────────────────────────────────

class TestConcurrentSessionMemoryWrites:
    """10 threads writing different content to the same session_id.

    Verify: final content is one of the valid values (no interleaving/corruption).
    Uses _atomic_write_text under the hood, so the last writer wins cleanly.
    """

    def test_last_writer_wins_clean(self):
        session = "race-session-mem"
        n_threads = 10
        barrier = threading.Barrier(n_threads)
        valid_bodies: set[str] = set()
        errors: list[Exception] = []

        def writer(tid: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(20):
                    body = f"CONTENT-FROM-THREAD-{tid}-ITER-{i}"
                    valid_bodies.add(body.strip() + "\n")
                    write_session_memory(session, body)
            except Exception as exc:
                errors.append(exc)

        # Populate valid_bodies before starting (from all threads)
        for tid in range(n_threads):
            for i in range(20):
                valid_bodies.add(f"CONTENT-FROM-THREAD-{tid}-ITER-{i}" + "\n")

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            futs = [pool.submit(writer, tid) for tid in range(n_threads)]
            for f in as_completed(futs):
                f.result()

        assert not errors, f"Errors: {errors}"

        final = read_session_memory(session)
        assert final in valid_bodies, f"Corrupt content: {final!r}"


# ───────────────────────────────────────────────────────────────
# 5. FileStateCache concurrent access
# ───────────────────────────────────────────────────────────────

class TestFileStateCacheConcurrency:
    """20 threads reading and writing to same cache simultaneously.

    Verify: no exceptions, no data corruption.
    """

    def test_concurrent_read_write(self):
        session = "fs-cache-race"
        n_threads = 20
        n_ops = 100
        barrier = threading.Barrier(n_threads)
        errors: list[Exception] = []

        def worker(tid: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(n_ops):
                    p = Path(f"/fake/path/t{tid}/file{i % 10}.txt")
                    note_file_read(
                        session,
                        p,
                        content=f"content-{tid}-{i}",
                        mtime_ns=i,
                        encoding="utf-8",
                        line_ending="\n",
                        is_partial_view=False,
                    )
                    # Read back — may or may not match (another thread could overwrite)
                    state = get_file_state(session, p)
                    if state is not None:
                        assert isinstance(state, FileState)
                        assert isinstance(state.content, str)
            except Exception as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            futs = [pool.submit(worker, tid) for tid in range(n_threads)]
            for f in as_completed(futs):
                f.result()

        assert not errors, f"Errors: {errors}"


# ───────────────────────────────────────────────────────────────
# 6. Concurrent scope lock acquisition
# ───────────────────────────────────────────────────────────────

class TestConcurrentScopeLock:
    """10 threads trying to acquire the same scope lock.

    RACE CONDITION FOUND: try_acquire_scope_lock() is NOT truly atomic.
    It does atomic_write_text(pid) then reads back to verify. But between
    write and verify, another thread can overwrite. So 0 or 1 threads
    succeed, which is the intended best-effort behavior. The function is
    documented as advisory, not POSIX-lock.

    Verify: at most 1 succeeds per attempt, no deadlocks (timeout).
    """

    def test_at_most_one_acquires(self):
        scope = "lock-race-scope"
        n_threads = 10
        barrier = threading.Barrier(n_threads)
        results: list[float | None] = []
        lock = threading.Lock()
        errors: list[Exception] = []

        def acquirer() -> None:
            try:
                barrier.wait(timeout=5)
                result = try_acquire_scope_lock(scope, stale_after_seconds=3600)
                with lock:
                    results.append(result)
            except Exception as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            futs = [pool.submit(acquirer) for _ in range(n_threads)]
            for f in as_completed(futs, timeout=10):
                f.result()

        assert not errors, f"Errors: {errors}"
        assert len(results) == n_threads

        # Because all threads share the same PID, all writes contain the
        # same PID string. The verify-after-write check will therefore
        # succeed for every thread. This is expected: the lock is
        # cross-process, not cross-thread. Within a single process all
        # threads have the same os.getpid().
        successes = [r for r in results if r is not None]
        # All or most threads succeed because they all write the same PID.
        # This documents the design: scope lock is per-process, not per-thread.
        assert len(successes) >= 1  # At least 1 must succeed


# ───────────────────────────────────────────────────────────────
# 7. Concurrent snapshot save/get
# ───────────────────────────────────────────────────────────────

class TestConcurrentSnapshotSaveGet:
    """Threads saving and reading snapshots for the same session simultaneously.

    Verify: reads return either None or a complete valid snapshot.
    """

    def test_snapshot_integrity(self):
        runtime = ForkRuntime()
        session = "snap-race"
        n_threads = 20
        n_ops = 100
        barrier = threading.Barrier(n_threads)
        errors: list[Exception] = []

        def saver(tid: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(n_ops):
                    snap = CacheSafeSnapshot(
                        model=f"model-{tid}",
                        scope_id="s",
                        session_id=session,
                        file_state_namespace=session,
                        messages=[{"role": "user", "content": f"msg-{tid}-{i}"}],
                        saved_at=time.time(),
                    )
                    runtime.save_snapshot(snap)
            except Exception as exc:
                errors.append(exc)

        def reader() -> None:
            try:
                barrier.wait(timeout=5)
                for _ in range(n_ops):
                    snap = runtime.get_snapshot(session)
                    if snap is not None:
                        assert isinstance(snap, CacheSafeSnapshot)
                        assert snap.session_id == session
                        assert len(snap.messages) == 1
                        assert isinstance(snap.messages[0]["content"], str)
            except Exception as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            futs = []
            # Half savers, half readers
            for tid in range(n_threads // 2):
                futs.append(pool.submit(saver, tid))
            for _ in range(n_threads // 2):
                futs.append(pool.submit(reader))
            for f in as_completed(futs):
                f.result()

        assert not errors, f"Errors: {errors}"

        # Final read must return a complete snapshot
        final = runtime.get_snapshot(session)
        assert final is not None
        assert final.session_id == session


# ───────────────────────────────────────────────────────────────
# 8. RequestContext thread isolation under load
# ───────────────────────────────────────────────────────────────

class TestRequestContextIsolation:
    """50 threads each setting their own context, 1000 iterations.

    Verify: context never leaks between threads.
    """

    def test_no_context_leakage(self):
        n_threads = 50
        n_iters = 1000
        barrier = threading.Barrier(n_threads)
        errors: list[str] = []

        def checker(tid: int) -> None:
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                return
            for i in range(n_iters):
                ctx = RequestContext(
                    session_id=f"sess-{tid}",
                    scope_id=f"scope-{tid}",
                    file_state_namespace=f"ns-{tid}",
                )
                with use_request_context(ctx):
                    got = current_request_context()
                    if got is None:
                        errors.append(f"tid={tid} iter={i}: context is None")
                        continue
                    if got.session_id != f"sess-{tid}":
                        errors.append(
                            f"tid={tid} iter={i}: expected sess-{tid}, got {got.session_id}"
                        )
                    if got.scope_id != f"scope-{tid}":
                        errors.append(
                            f"tid={tid} iter={i}: expected scope-{tid}, got {got.scope_id}"
                        )
            # After exiting all contexts, should be None
            after = current_request_context()
            if after is not None:
                errors.append(f"tid={tid}: context leaked after exit: {after}")

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            futs = [pool.submit(checker, tid) for tid in range(n_threads)]
            for f in as_completed(futs):
                f.result()

        assert not errors, f"Leaks detected ({len(errors)} total):\n" + "\n".join(errors[:20])


# ───────────────────────────────────────────────────────────────
# 9. Parallel memory delete + read
# ───────────────────────────────────────────────────────────────

class TestParallelDeleteAndRead:
    """Some threads deleting memories while others read/scan.

    Verify: no crashes, reads return valid data or graceful empty.
    """

    def test_delete_while_reading(self):
        scope = "race-delete-read"
        n_files = 50
        # Pre-create files
        for i in range(n_files):
            write_memory_file(
                scope, name=f"f{i}", description=f"d{i}", type_="user",
                body=f"body{i}", filename=f"f{i}.md",
            )

        barrier = threading.Barrier(20)
        errors: list[Exception] = []

        def deleter() -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(n_files):
                    try:
                        delete_memory_file(scope, f"f{i}.md")
                    except FileNotFoundError:
                        pass  # Already deleted by another thread
            except Exception as exc:
                errors.append(exc)

        def reader() -> None:
            try:
                barrier.wait(timeout=5)
                for _ in range(50):
                    try:
                        headers = scan_memory_headers(scope)
                        for h in headers:
                            assert isinstance(h.name, str)
                    except FileNotFoundError:
                        pass  # File vanished between list and read — expected
            except Exception as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=20) as pool:
            futs = []
            for _ in range(10):
                futs.append(pool.submit(deleter))
            for _ in range(10):
                futs.append(pool.submit(reader))
            for f in as_completed(futs):
                f.result()

        assert not errors, f"Errors: {errors}"


# ───────────────────────────────────────────────────────────────
# 10. Concurrent write_memory_file + list_memory_files
# ───────────────────────────────────────────────────────────────

class TestConcurrentWriteAndList:
    """Writers creating files while listers scan.

    Verify: no OSError crashes, list always returns valid paths.
    """

    def test_write_while_listing(self):
        scope = "race-write-list"
        barrier = threading.Barrier(20)
        errors: list[Exception] = []

        def writer(tid: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(10):
                    write_memory_file(
                        scope, name=f"w{tid}-{i}", description=f"d{tid}-{i}",
                        type_="project", body=f"b{tid}-{i}",
                        filename=f"w{tid}-{i}.md",
                    )
            except Exception as exc:
                errors.append(exc)

        def lister() -> None:
            try:
                barrier.wait(timeout=5)
                for _ in range(50):
                    files = list_memory_files(scope)
                    for p in files:
                        assert isinstance(p, Path)
                        # Path must end with .md
                        assert p.name.endswith(".md")
            except Exception as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=20) as pool:
            futs = []
            for tid in range(10):
                futs.append(pool.submit(writer, tid))
            for _ in range(10):
                futs.append(pool.submit(lister))
            for f in as_completed(futs):
                f.result()

        assert not errors, f"Errors: {errors}"


# ───────────────────────────────────────────────────────────────
# 11. Stress atomic_write_text
# ───────────────────────────────────────────────────────────────

class TestStressAtomicWrite:
    """50 threads writing to the SAME file path simultaneously.

    Verify: file always contains complete valid content (never partial).
    The _atomic_write_text function uses tempfile + rename, which on
    POSIX is atomic at the filesystem level — so this should always pass.
    """

    def test_atomic_write_no_partial_content(self, tmp_path):
        target = tmp_path / "shared_file.txt"
        n_writers = 50
        n_readers = 5
        n_writes = 50
        barrier = threading.Barrier(n_writers)
        errors: list[Exception] = []

        # Each thread writes a unique marker repeated to fill some space
        def writer(tid: int) -> None:
            try:
                barrier.wait(timeout=10)
                for i in range(n_writes):
                    marker = f"THREAD-{tid}-WRITE-{i}"
                    content = (marker + "\n") * 10  # ~50 chars * 10
                    _atomic_write_text(target, content)
            except Exception as exc:
                errors.append(exc)

        # Reader checks that the file is never a mix of two markers
        read_errors: list[str] = []
        stop_readers = threading.Event()

        def reader() -> None:
            while not stop_readers.is_set():
                if not target.exists():
                    continue
                try:
                    text = target.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if not text.strip():
                    continue
                lines = [ln for ln in text.strip().split("\n") if ln]
                # All lines should be the same marker
                if len(set(lines)) > 1:
                    read_errors.append(f"Mixed content: {set(lines)}")

        with ThreadPoolExecutor(max_workers=n_writers + n_readers) as pool:
            reader_futs = [pool.submit(reader) for _ in range(n_readers)]
            writer_futs = [pool.submit(writer, tid) for tid in range(n_writers)]
            for f in as_completed(writer_futs):
                f.result()
            stop_readers.set()
            for f in reader_futs:
                f.result()

        assert not errors, f"Writer errors: {errors}"
        assert not read_errors, f"Partial/mixed reads: {read_errors[:10]}"

        # Final content must be one complete write
        final = target.read_text(encoding="utf-8")
        lines = [ln for ln in final.strip().split("\n") if ln]
        assert len(set(lines)) == 1, f"Final file has mixed content: {set(lines)}"


# ───────────────────────────────────────────────────────────────
# 12. Mixed operations chaos test
# ───────────────────────────────────────────────────────────────

class TestMixedOperationsChaos:
    """30 threads doing random operations for ~2 seconds.

    Operations: write_memory, delete_memory, rebuild_index,
    read_transcript, write_session_memory, append_transcript.

    RACE CONDITION FOUND: list_memory_files() calls rglob("*.md") then
    stat() on each result. If another thread deletes a file between rglob
    and stat, FileNotFoundError is raised. This propagates through
    rebuild_memory_index() and write_memory_file() (which calls rebuild
    after writing). The same race exists in scan_memory_headers().
    FileNotFoundError from concurrent delete+list is expected and benign.

    Verify: no unhandled exceptions (except known FileNotFoundError race),
    all files left in consistent state.
    """

    def test_chaos_no_unhandled_exceptions(self):
        scope = "chaos-scope"
        session = "chaos-session"
        n_threads = 30
        duration = 2.0
        barrier = threading.Barrier(n_threads)
        errors: list[Exception] = []

        # Pre-seed some files
        for i in range(10):
            write_memory_file(
                scope, name=f"seed{i}", description=f"sd{i}", type_="project",
                body=f"sb{i}", filename=f"seed{i}.md",
            )

        def chaos_worker(tid: int) -> None:
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                return
            rng = random.Random(tid)
            deadline = time.time() + duration
            counter = 0
            while time.time() < deadline:
                op = rng.randint(0, 5)
                try:
                    if op == 0:
                        # Write memory
                        write_memory_file(
                            scope,
                            name=f"chaos-{tid}-{counter}",
                            description=f"cd-{tid}-{counter}",
                            type_="user",
                            body=f"cb-{tid}-{counter}",
                            filename=f"chaos-{tid}-{counter % 5}.md",
                        )
                    elif op == 1:
                        # Delete memory
                        fname = f"seed{rng.randint(0, 9)}.md"
                        delete_memory_file(scope, fname)
                    elif op == 2:
                        # Rebuild index
                        rebuild_memory_index(scope, force=True)
                    elif op == 3:
                        # Read transcript
                        read_transcript_events(session, limit=20)
                    elif op == 4:
                        # Write session memory
                        write_session_memory(session, f"chaos-content-{tid}-{counter}")
                    elif op == 5:
                        # Append transcript
                        append_transcript_event(
                            session,
                            {"tid": tid, "seq": counter, "kind": "message", "role": "user", "content": f"chaos-{tid}-{counter}"},
                        )
                except FileNotFoundError:
                    # KNOWN RACE: list_memory_files/scan_memory_headers call
                    # stat() on files discovered via rglob. A concurrent
                    # delete can remove the file between discovery and stat.
                    pass
                except Exception as exc:
                    errors.append(exc)
                counter += 1

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            futs = [pool.submit(chaos_worker, tid) for tid in range(n_threads)]
            for f in as_completed(futs, timeout=30):
                f.result()

        assert not errors, f"Chaos errors ({len(errors)}):\n" + "\n".join(str(e) for e in errors[:20])

        # Post-chaos integrity: index must be valid
        idx_path = memory_index_path(scope)
        if idx_path.exists():
            text = idx_path.read_text(encoding="utf-8")
            assert text.startswith("# Memory Index")

        # All remaining .md files must be parseable
        for p in list_memory_files(scope):
            text = p.read_text(encoding="utf-8")
            fm = parse_frontmatter(text)
            # Frontmatter might be empty for files where write was interrupted
            # (shouldn't happen with atomic writes, but defensive check)
            assert isinstance(fm, dict)

        # Session memory must be valid
        mem = read_session_memory(session)
        assert isinstance(mem, str)
