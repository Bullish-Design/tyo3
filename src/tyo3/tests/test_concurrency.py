"""Concurrency tests — GIL release, snapshot thread-safety, isolation, parity.

Validates that:
- Session reads genuinely release the GIL (control-vs-treatment proof)
- A single Snapshot is safe to share across threads (Mutex-clone soundness)
- Lifecycle: close/context-manager/ResourceWarning
- Isolation: snapshot is pinned, immune to reload
- Equivalence: snapshot results match session results at same revision
- Primary Snapshot parity: every read method works on a snapshot
"""

from __future__ import annotations

import concurrent.futures
import ctypes
import ctypes.util
import shutil
import threading
import time
import warnings
from pathlib import Path as StdPath

import pytest

# ── Native extension detection ──────────────────────────────────────────

try:
    from tyo3 import TyO3Session
    from tyo3.exceptions import PositionError, ProjectClosedError

    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False

needs_native = pytest.mark.skipif(not _HAS_NATIVE, reason="Rust native extension not built")

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def _fixture_path(name: str) -> str:
    return str((FIXTURES_DIR / name).resolve())


# ── GIL progress helpers ────────────────────────────────────────────────


def _python_progress_while(call) -> tuple[int, float]:
    """Count Python-thread progress while *call* runs on this thread."""
    stop = threading.Event()
    ready = threading.Event()
    count = 0

    def worker() -> None:
        nonlocal count
        ready.set()
        while not stop.is_set():
            count += 1

    thread = threading.Thread(target=worker)
    thread.start()
    ready.wait(timeout=1)
    start = count
    t0 = time.perf_counter()
    try:
        call()
    finally:
        elapsed = time.perf_counter() - t0
        advanced = count - start
        stop.set()
        thread.join(timeout=1)
    return advanced, elapsed


def _gil_holding_sleep(seconds: float):
    """Return a libc usleep call made through PyDLL, which keeps the GIL held."""
    libc_path = ctypes.util.find_library("c")
    if libc_path is None:
        pytest.skip("Cannot locate libc for GIL-held control")

    libc = ctypes.PyDLL(libc_path)
    try:
        usleep = libc.usleep
    except AttributeError:
        pytest.skip("libc has no usleep for GIL-held control")

    usleep.argtypes = [ctypes.c_uint]
    usleep.restype = ctypes.c_int

    def run() -> None:
        usleep(int(seconds * 1_000_000))

    return run


# ── Per-thread workloads ────────────────────────────────────────────────


def _run_check(root: str) -> int:
    session = TyO3Session(root)
    try:
        result = session.check()
        return len(result.diagnostics)
    finally:
        session.close()


def _run_document_symbols(root: str) -> int:
    session = TyO3Session(root)
    try:
        symbols = session.document_symbols("main.py")
        return len(symbols)
    finally:
        session.close()


def _do_workloads(root: str) -> tuple[int, int]:
    session = TyO3Session(root)
    try:
        result = session.check()
        symbols = session.document_symbols("main.py")
        return len(result.diagnostics), len(symbols)
    finally:
        session.close()


# ── Equivalence / error helpers ─────────────────────────────────────────


def _dump(obj):
    """Normalize a read result to a plain comparable value.

    Pydantic models → dicts; lists recurse; None and scalars pass through.
    Lets us assert session.<read>(...) == snapshot.<read>(...) for every method
    without caring whether it returned a model, a list, or None.
    """
    if obj is None:
        return None
    if isinstance(obj, list):
        return [_dump(x) for x in obj]
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj


def _capture_exc(fn):
    """Run *fn* and return the type of any exception it raised, else None."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001 — we want the type of whatever it is
        return type(e)
    return None


# ── GIL release proof ───────────────────────────────────────────────────


@needs_native
class TestGilRelease:
    """Prove the GIL is genuinely released during analysis.

    Uses a control-vs-treatment design: compare Python-thread progress while
    a known GIL-held C call runs against progress while Rust analysis runs.
    A real GIL release lets another Python thread make orders of magnitude
    more progress during the Rust read.
    """

    ROOT = _fixture_path("demo_repos")
    WORKSPACE_SYMBOL_REPEATS = 50

    def test_session_reads_release_the_gil(self) -> None:
        """Other Python threads make progress during Rust analysis."""

        def rust_workspace_symbols() -> int:
            s = TyO3Session(self.ROOT)
            try:
                total = 0
                for _ in range(self.WORKSPACE_SYMBOL_REPEATS):
                    total += len(s.workspace_symbols("a"))
                return total
            finally:
                s.close()

        rust_progress, rust_elapsed = _python_progress_while(rust_workspace_symbols)
        control_progress, _ = _python_progress_while(_gil_holding_sleep(rust_elapsed))

        assert rust_progress > max(control_progress * 10, 100_000), (
            "GIL not released? "
            f"rust_progress={rust_progress} control_progress={control_progress} "
            f"elapsed={rust_elapsed:.2f}s"
        )


# ── Snapshot thread-safety ──────────────────────────────────────────────


@needs_native
class TestSnapshotThreadSafety:
    """Validate a single Snapshot is safe to share across threads."""

    ROOT = _fixture_path("simple_package")

    def test_snapshot_shared_across_threads(self) -> None:
        """One snapshot, multiple threads — all valid, consistent results."""
        session = TyO3Session(self.ROOT)
        try:
            snap = session.snapshot()
        finally:
            session.close()

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                futures = [ex.submit(snap.check) for _ in range(4)]
                results = [f.result(timeout=30) for f in futures]

            # All should return valid CheckResult with diagnostics
            diag_counts = [len(r.diagnostics) for r in results]
            assert all(isinstance(c, int) and c >= 0 for c in diag_counts)
            # Snapshot at same revision → identical results
            assert len(set(diag_counts)) == 1, f"Shared snapshot returned differing diagnostic counts: {diag_counts}"
        finally:
            snap.close()

    def test_snapshot_no_deadlock_under_concurrent_reads(self) -> None:
        """Many threads reading from one snapshot — no deadlock, no crash."""
        session = TyO3Session(self.ROOT)
        try:
            snap = session.snapshot()
        finally:
            session.close()

        try:

            def _mixed_reads() -> bool:
                snap.check()
                snap.document_symbols("main.py")
                snap.files()
                return True

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                futures = [ex.submit(_mixed_reads) for _ in range(8)]
                for f in futures:
                    assert f.result(timeout=30) is True
        finally:
            snap.close()


# ── Lifecycle ───────────────────────────────────────────────────────────


@needs_native
class TestSnapshotLifecycle:
    """Snapshot lifecycle: close, context-manager, ResourceWarning."""

    ROOT = _fixture_path("simple_package")

    def test_snapshot_after_session_close_raises(self) -> None:
        """snapshot() on a closed session raises ProjectClosedError."""
        session = TyO3Session(self.ROOT)
        session.close()
        with pytest.raises(ProjectClosedError):
            session.snapshot()

    def test_read_on_closed_snapshot_raises(self) -> None:
        """A read on a closed snapshot raises ProjectClosedError."""
        session = TyO3Session(self.ROOT)
        try:
            snap = session.snapshot()
        finally:
            session.close()
        snap.close()
        with pytest.raises(ProjectClosedError):
            snap.check()

    def test_snapshot_context_manager(self) -> None:
        """Snapshot works as a context manager."""
        session = TyO3Session(self.ROOT)
        try:
            with session.snapshot() as snap:
                result = snap.check()
                assert len(result.diagnostics) >= 0
            # After __exit__, reads should fail
            with pytest.raises(ProjectClosedError):
                snap.check()
        finally:
            session.close()

    def test_snapshot_resource_warning(self) -> None:
        """Unclosed snapshot emits ResourceWarning."""
        session = TyO3Session(self.ROOT)
        try:
            snap = session.snapshot()
        finally:
            session.close()

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            del snap  # trigger __del__
        resource_warnings = [x for x in w if issubclass(x.category, ResourceWarning)]
        assert len(resource_warnings) >= 1, "Expected ResourceWarning for unclosed snapshot"

    def test_snapshot_has_no_snapshot_method(self) -> None:
        """Decision 2: snapshot() is terminal — not on Snapshot."""
        session = TyO3Session(self.ROOT)
        try:
            snap = session.snapshot()
        finally:
            session.close()
        try:
            assert not hasattr(snap, "snapshot"), "Snapshot must not expose snapshot()"
        finally:
            snap.close()


# ── Isolation ───────────────────────────────────────────────────────────


@needs_native
class TestSnapshotIsolation:
    """Prove snapshot is revision-pinned and immune to reload."""

    def test_snapshot_isolated_from_reload(self, tmp_path: StdPath) -> None:
        """Old snapshot stays pinned while session/new snapshot see edits."""
        project_root = tmp_path / "project"
        shutil.copytree(FIXTURES_DIR / "simple_package", project_root)

        added_symbol = "added_symbol_for_snapshot_test"
        main_py = project_root / "main.py"

        session = TyO3Session(project_root)
        snap = session.snapshot()
        new_snap = None
        try:
            pre_names = {s.name for s in snap.document_symbols("main.py")}
            assert added_symbol not in pre_names

            # Use edit() instead of direct disk write — changes flow through
            # the substrate (architecture §2.2 invariant).
            new_text = main_py.read_text() + f"\n\ndef {added_symbol}() -> int:\n    return 1\n"
            session.edit("main.py", new_text)

            old_snapshot_names = {s.name for s in snap.document_symbols("main.py")}
            session_names = {s.name for s in session.document_symbols("main.py")}
            new_snap = session.snapshot()
            new_snapshot_names = {s.name for s in new_snap.document_symbols("main.py")}

            assert added_symbol not in old_snapshot_names
            assert added_symbol in session_names
            assert added_symbol in new_snapshot_names
        finally:
            if new_snap is not None:
                new_snap.close()
            session.close()
            snap.close()

    def test_snapshot_pinned_without_preread(self, tmp_path: StdPath) -> None:
        """Snapshot stays pinned even when its first read happens AFTER edits
        land on HEAD — the scenario-A shape that proves independent Zalsa."""
        project_root = tmp_path / "project"
        shutil.copytree(FIXTURES_DIR / "simple_package", project_root)

        added_symbol = "pinned_symbol_no_preread"
        main_py = project_root / "main.py"

        session = TyO3Session(project_root)
        snap = session.snapshot()
        try:
            # NO pre-read of the snapshot — this is the key difference from
            # test_snapshot_isolated_from_reload.  We edit first.
            new_text = main_py.read_text() + f"\n\ndef {added_symbol}() -> int:\n    return 1\n"
            session.edit("main.py", new_text)

            # First read on the old snapshot happens now — after the edit.
            snapshot_names = {s.name for s in snap.document_symbols("main.py")}
            session_names = {s.name for s in session.document_symbols("main.py")}

            assert added_symbol not in snapshot_names, (
                f"snapshot leaked a later edit: {added_symbol} in {snapshot_names}"
            )
            assert added_symbol in session_names
        finally:
            session.close()
            snap.close()


# ── Equivalence ─────────────────────────────────────────────────────────


@needs_native
class TestSnapshotEquivalence:
    """Snapshot results match session results at the same revision."""

    ROOT = _fixture_path("simple_package")

    def test_check_parity(self) -> None:
        session = TyO3Session(self.ROOT)
        try:
            snap = session.snapshot()
            session_diags = len(session.check().diagnostics)
            snap_diags = len(snap.check().diagnostics)
            assert session_diags == snap_diags, f"Mismatch: session={session_diags} snap={snap_diags}"
        finally:
            session.close()
            snap.close()

    def test_document_symbols_parity(self) -> None:
        session = TyO3Session(self.ROOT)
        try:
            snap = session.snapshot()
            session_symbols = len(session.document_symbols("main.py"))
            snap_symbols = len(snap.document_symbols("main.py"))
            assert session_symbols == snap_symbols
        finally:
            session.close()
            snap.close()

    def test_files_parity(self) -> None:
        session = TyO3Session(self.ROOT)
        try:
            snap = session.snapshot()
            assert session.files() == snap.files()
        finally:
            session.close()
            snap.close()


# ── Snapshot error parity ───────────────────────────────────────────────


@needs_native
class TestSnapshotErrorParity:
    """A snapshot raises the SAME typed exception the session does for bad input."""

    ROOT = _fixture_path("simple_package")

    # Each case is a callable taking a handle (session or snapshot). The inputs are
    # chosen to trip a specific error path:
    #   - missing file        → PathResolutionError (resolved before any position work)
    #   - overflowing column  → OverflowError → PositionError (session.py maps it)
    ERROR_CASES = [
        ("check_file_missing", lambda h: h.check_file("definitely_missing_file.py")),
        ("document_symbols_missing", lambda h: h.document_symbols("definitely_missing_file.py")),
        ("semantic_tokens_missing", lambda h: h.semantic_tokens("definitely_missing_file.py")),
        ("file_occurrences_missing", lambda h: h.file_occurrences("definitely_missing_file.py")),
        ("goto_definition_missing", lambda h: h.goto_definition("definitely_missing_file.py", 1, 1)),
        ("goto_definition_overflow", lambda h: h.goto_definition("main.py", 1, 2**63)),
        ("find_references_overflow", lambda h: h.find_references("main.py", 1, 2**63)),
        ("hover_overflow", lambda h: h.hover("main.py", 1, 2**63)),
        ("type_hierarchy_overflow", lambda h: h.type_hierarchy("main.py", 1, 2**63)),
    ]

    @pytest.mark.parametrize("name,call", ERROR_CASES, ids=[c[0] for c in ERROR_CASES])
    def test_error_parity(self, name, call) -> None:
        session = TyO3Session(self.ROOT)
        try:
            snap = session.snapshot()
            try:
                session_exc = _capture_exc(lambda: call(session))
                snapshot_exc = _capture_exc(lambda: call(snap))
                assert session_exc is not None, f"{name}: session did not raise — fix the test input"
                assert snapshot_exc == session_exc, (
                    f"{name}: snapshot raised {snapshot_exc}, session raised {session_exc}"
                )
            finally:
                snap.close()
        finally:
            session.close()

    def test_read_on_closed_snapshot_parity(self) -> None:
        """Every read method on a closed snapshot raises ProjectClosedError."""
        session = TyO3Session(self.ROOT)
        try:
            snap = session.snapshot()
        finally:
            session.close()
        snap.close()
        for call in (
            lambda: snap.files(),
            lambda: snap.check(),
            lambda: snap.check_file("main.py"),
            lambda: snap.document_symbols("main.py"),
            lambda: snap.workspace_symbols("a"),
            lambda: snap.goto_definition("main.py", 1, 1),
            lambda: snap.goto_declaration("main.py", 1, 1),
            lambda: snap.goto_type_definition("main.py", 1, 1),
            lambda: snap.find_references("main.py", 1, 1),
            lambda: snap.semantic_tokens("main.py"),
            lambda: snap.file_occurrences("main.py"),
            lambda: snap.hover("main.py", 1, 1),
            lambda: snap.type_hierarchy("main.py", 1, 1),
        ):
            with pytest.raises(ProjectClosedError):
                call()


# ── Full Snapshot read surface ───────────────────────────────────────────


@needs_native
class TestSnapshotFullSurface:
    """Every read method inherited from _ReadOps works on a Snapshot."""

    ROOT = _fixture_path("simple_package")

    @pytest.fixture
    def snap(self):
        session = TyO3Session(self.ROOT)
        try:
            s = session.snapshot()
            yield s
        finally:
            session.close()
            s.close()

    def test_files(self, snap) -> None:
        result = snap.files()
        assert isinstance(result, list)
        assert len(result) > 0

    def test_check(self, snap) -> None:
        result = snap.check()
        assert hasattr(result, "diagnostics")

    def test_check_file(self, snap) -> None:
        result = snap.check_file("main.py")
        assert hasattr(result, "diagnostics")

    def test_document_symbols(self, snap) -> None:
        result = snap.document_symbols("main.py")
        assert isinstance(result, list)

    def test_workspace_symbols(self, snap) -> None:
        result = snap.workspace_symbols("hello")
        assert isinstance(result, list)
        result_empty = snap.workspace_symbols("")
        assert result_empty == []

    def test_goto_definition(self, snap) -> None:
        result = snap.goto_definition("main.py", 1, 1)
        assert isinstance(result, list)

    def test_goto_declaration(self, snap) -> None:
        result = snap.goto_declaration("main.py", 1, 1)
        assert isinstance(result, list)

    def test_goto_type_definition(self, snap) -> None:
        result = snap.goto_type_definition("main.py", 1, 1)
        assert isinstance(result, list)

    def test_find_references(self, snap) -> None:
        result = snap.find_references("main.py", 1, 1)
        assert isinstance(result, list)

    def test_semantic_tokens(self, snap) -> None:
        result = snap.semantic_tokens("main.py")
        assert isinstance(result, list)

    def test_file_occurrences(self, snap) -> None:
        result = snap.file_occurrences("main.py")
        assert isinstance(result, list)

    def test_type_hierarchy(self, snap) -> None:
        result = snap.type_hierarchy("main.py", 9, 7)
        # May be None if no hierarchy at that position — that's fine
        assert result is None or hasattr(result, "item")

    def test_hover(self, snap) -> None:
        result = snap.hover("main.py", 3, 10)
        # May be None if no hover at that position
        assert result is None or hasattr(result, "contents")


# ── Full session↔snapshot equivalence ──────────────────────────────────


@needs_native
class TestSnapshotEquivalenceFull:
    """snapshot.<read>(...) == session.<read>(...) at the same revision, all methods."""

    ROOT = _fixture_path("simple_package")

    # (id, callable(handle)). Positions are safe defaults; (1,1) returns a (possibly
    # empty) list for navigation and None for hover/hierarchy — _dump compares either.
    READ_CALLS = [
        ("files", lambda h: h.files()),
        ("check", lambda h: h.check()),
        ("check_file", lambda h: h.check_file("main.py")),
        ("document_symbols", lambda h: h.document_symbols("main.py")),
        ("workspace_symbols", lambda h: h.workspace_symbols("a")),
        ("goto_definition", lambda h: h.goto_definition("main.py", 1, 1)),
        ("goto_declaration", lambda h: h.goto_declaration("main.py", 1, 1)),
        ("goto_type_definition", lambda h: h.goto_type_definition("main.py", 1, 1)),
        ("find_references", lambda h: h.find_references("main.py", 1, 1)),
        ("semantic_tokens", lambda h: h.semantic_tokens("main.py")),
        ("file_occurrences", lambda h: h.file_occurrences("main.py")),
        ("type_hierarchy", lambda h: h.type_hierarchy("main.py", 9, 7)),
        ("hover", lambda h: h.hover("main.py", 3, 10)),
    ]

    @pytest.mark.parametrize("name,call", READ_CALLS, ids=[c[0] for c in READ_CALLS])
    def test_read_parity(self, name, call) -> None:
        session = TyO3Session(self.ROOT)
        try:
            snap = session.snapshot()
            try:
                # No reload between the two calls → identical revision → identical result.
                assert _dump(call(session)) == _dump(call(snap)), f"{name}: session/snapshot mismatch"
            finally:
                snap.close()
        finally:
            session.close()

    def test_hover_some_branch_parity(self) -> None:
        """At a position with real hover info, snapshot matches session and is non-None."""
        session = TyO3Session(self.ROOT)
        try:
            snap = session.snapshot()
            try:
                hit = None
                # Probe a small grid; main.py is tiny. Stop at the first real hover.
                for line in range(1, 30):
                    for col in range(1, 40):
                        try:
                            if session.hover("main.py", line, col) is not None:
                                hit = (line, col)
                                break
                        except PositionError:
                            pass  # column past end-of-line — skip
                    if hit:
                        break
                if hit is None:
                    pytest.skip("fixture yielded no hover anywhere in the probed grid")
                line, col = hit
                s = session.hover("main.py", line, col)
                p = snap.hover("main.py", line, col)
                assert p is not None
                assert _dump(s) == _dump(p)
            finally:
                snap.close()
        finally:
            session.close()


# ── Original concurrency tests (kept) ────────────────────────────────────


# ── Reload / read concurrency ────────────────────────────────────────────


@needs_native
class TestReloadConcurrency:
    """The load-bearing invariant: reload() swaps the db; in-flight reads on a
    cloned db are never invalidated (no salsa::Cancelled, no panic, no error)."""

    def test_reload_during_concurrent_reads(self, tmp_path: StdPath) -> None:
        """Hammer reload() on one thread while N threads read — zero errors."""
        project_root = tmp_path / "project"
        shutil.copytree(FIXTURES_DIR / "simple_package", project_root)

        session = TyO3Session(project_root)
        errors: list[tuple[str, BaseException]] = []
        stop = threading.Event()

        def reader() -> None:
            try:
                while not stop.is_set():
                    # Mix of full-project (rayon) and cursor reads to maximize
                    # the chance a read is mid-flight when the swap lands.
                    session.check()
                    session.document_symbols("main.py")
                    session.files()
            except Exception as e:  # noqa: BLE001
                errors.append(("reader", e))

        def reloader() -> None:
            try:
                for _ in range(25):
                    session.reload()
            except Exception as e:  # noqa: BLE001
                errors.append(("reloader", e))
            finally:
                stop.set()

        threads = [threading.Thread(target=reader) for _ in range(4)]
        threads.append(threading.Thread(target=reloader))
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
            assert not any(t.is_alive() for t in threads), "deadlock: a thread did not finish"
            # A read must NEVER raise here — close() is never called, so there is no
            # closed window, and the swap-don't-mutate invariant means no Cancelled.
            assert not errors, f"Concurrent reload/read raised: {errors}"
        finally:
            stop.set()
            session.close()


# ── Session thread-safety ───────────────────────────────────────────────


@needs_native
class TestSessionThreadSafety:
    """One TyO3Session, shared across threads — same clone+detach path as Snapshot."""

    ROOT = _fixture_path("simple_package")

    def test_session_shared_across_threads(self) -> None:
        session = TyO3Session(self.ROOT)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                futures = [ex.submit(session.check) for _ in range(4)]
                results = [f.result(timeout=30) for f in futures]
            counts = [len(r.diagnostics) for r in results]
            assert all(isinstance(c, int) and c >= 0 for c in counts)
            assert len(set(counts)) == 1, f"Shared session returned differing counts: {counts}"
        finally:
            session.close()

    def test_session_mixed_reads_no_deadlock(self) -> None:
        session = TyO3Session(self.ROOT)
        try:

            def _mixed() -> bool:
                session.check()
                session.document_symbols("main.py")
                session.files()
                session.workspace_symbols("a")
                return True

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                futures = [ex.submit(_mixed) for _ in range(8)]
                for f in futures:
                    assert f.result(timeout=30) is True
        finally:
            session.close()


@needs_native
class TestConcurrency:
    """Validate that concurrent native sessions work correctly."""

    ROOT = _fixture_path("simple_package")

    def test_concurrent_checks(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(_run_check, self.ROOT) for _ in range(4)]
            results = [f.result(timeout=30) for f in futures]

        assert all(isinstance(r, int) for r in results), f"Expected int diagnostic counts, got: {results}"
        assert len(set(results)) == 1, f"Concurrent checks returned differing diagnostic counts: {results}"

    def test_concurrent_document_symbols(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(_run_document_symbols, self.ROOT) for _ in range(4)]
            results = [f.result(timeout=30) for f in futures]

        assert all(isinstance(r, int) for r in results), f"Expected int symbol counts, got: {results}"
        assert len(set(results)) == 1, f"Concurrent symbol lookups returned differing counts: {results}"

    def test_concurrent_mixed_workloads(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(_run_check, self.ROOT),
                executor.submit(_run_document_symbols, self.ROOT),
                executor.submit(_do_workloads, self.ROOT),
                executor.submit(_run_check, self.ROOT),
            ]
            results = [f.result(timeout=30) for f in futures]

        assert isinstance(results[0], int)
        assert isinstance(results[1], int)
        assert isinstance(results[2], tuple) and len(results[2]) == 2
        assert all(isinstance(x, int) for x in results[2])
        assert isinstance(results[3], int)

    def test_no_deadlock_on_repeat_sessions(self) -> None:
        def _open_and_close(root: str) -> None:
            for _ in range(5):
                session = TyO3Session(root)
                session.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(_open_and_close, self.ROOT) for _ in range(8)]
            for f in futures:
                f.result(timeout=30)


# ── Close race ──────────────────────────────────────────────────────────


@needs_native
class TestCloseRace:
    """close() during concurrent reads: benign — ProjectClosedError only, no crash."""

    ROOT = _fixture_path("simple_package")

    def _race(self, make_handle_and_close):
        """make_handle_and_close() -> (handle, close_fn). Reads on `handle` run on
        threads while close_fn() is called from the main thread."""
        handle, close_fn = make_handle_and_close()
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(100):
                    handle.check()
            except ProjectClosedError:
                pass  # expected once close() lands
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        # Let a few reads start, then close out from under them.
        close_fn()
        for t in threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in threads), "deadlock after close()"
        assert not errors, f"close() race produced non-benign errors: {errors}"

    def test_close_session_during_reads(self) -> None:
        def make():
            s = TyO3Session(self.ROOT)
            return s, s.close

        self._race(make)

    def test_close_snapshot_during_reads(self) -> None:
        def make():
            s = TyO3Session(self.ROOT)
            try:
                snap = s.snapshot()
            finally:
                s.close()
            return snap, snap.close

        self._race(make)


# ── Multi-snapshot isolation ────────────────────────────────────────────


@needs_native
class TestMultiSnapshotIsolation:
    """Independent snapshots stay pinned to their own revision across multiple reloads."""

    def test_snapshots_pinned_across_reloads(self, tmp_path: StdPath) -> None:
        project_root = tmp_path / "project"
        shutil.copytree(FIXTURES_DIR / "simple_package", project_root)
        main_py = project_root / "main.py"

        sym1 = "added_symbol_one"
        sym2 = "added_symbol_two"
        base_text = main_py.read_text()

        session = TyO3Session(project_root)
        snaps = []
        try:
            snap_a = session.snapshot()  # revision 0: neither symbol
            snaps.append(snap_a)

            # Use edit() — changes flow through the substrate
            session.edit("main.py", base_text + f"\n\ndef {sym1}() -> int:\n    return 1\n")
            snap_b = session.snapshot()  # revision 1: sym1 only
            snaps.append(snap_b)

            session.edit(
                "main.py",
                base_text + f"\n\ndef {sym1}() -> int:\n    return 1\n\ndef {sym2}() -> int:\n    return 2\n",
            )
            snap_c = session.snapshot()  # revision 2: sym1 + sym2
            snaps.append(snap_c)

            names_a = {s.name for s in snap_a.document_symbols("main.py")}
            names_b = {s.name for s in snap_b.document_symbols("main.py")}
            names_c = {s.name for s in snap_c.document_symbols("main.py")}

            assert sym1 not in names_a and sym2 not in names_a, "snap_a leaked a later edit"
            assert sym1 in names_b and sym2 not in names_b, "snap_b not pinned to revision 1"
            assert sym1 in names_c and sym2 in names_c, "snap_c missing a current symbol"
        finally:
            for s in snaps:
                s.close()
            session.close()
