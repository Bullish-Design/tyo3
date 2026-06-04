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
import time
import warnings
from pathlib import Path as StdPath

import pytest

# ── Native extension detection ──────────────────────────────────────────

try:
    from tyo3 import TyO3Session
    from tyo3.exceptions import ProjectClosedError

    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False

needs_native = pytest.mark.skipif(not _HAS_NATIVE, reason="Rust native extension not built")

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def _fixture_path(name: str) -> str:
    return str((FIXTURES_DIR / name).resolve())


# ── Timing helpers ──────────────────────────────────────────────────────


def _serial(make_call, n: int) -> float:
    """Run *n* calls in series. *make_call* returns a zero-arg callable."""
    calls = [make_call() for _ in range(n)]
    t0 = time.perf_counter()
    for c in calls:
        c()
    return time.perf_counter() - t0


def _parallel(make_call, n: int) -> float:
    """Run *n* calls concurrently on a thread pool."""
    calls = [make_call() for _ in range(n)]
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
        list(ex.map(lambda c: c(), calls))
    return time.perf_counter() - t0


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


# ── GIL release proof ───────────────────────────────────────────────────


@needs_native
class TestGilRelease:
    """Prove the GIL is genuinely released during analysis.

    Uses a control-vs-treatment design: compare speedup of a pure-Python
    CPU function (GIL held → speedup ≈ 1.0) against speedup of multiple
    cold sessions running check() on separate threads. A real GIL release
    produces a meaningfully higher speedup.
    """

    ROOT = _fixture_path("demo_repos")
    N = 4

    def test_check_releases_the_gil(self) -> None:
        """Rust check() speedup exceeds a Python-CPU control speedup."""

        # Control: pure-Python CPU work — GIL serializes, no speedup expected.
        def py_busy():
            return lambda: sum(i * i for i in range(3_000_000))

        py_busy()()  # warm
        control_serial = _serial(py_busy, self.N)
        control_parallel = _parallel(py_busy, self.N)
        control_speedup = control_serial / control_parallel

        # Treatment: each call is check() on its OWN cold session.
        def make_check():
            s = TyO3Session(self.ROOT)
            return lambda: (s.check(), s.close())

        rust_serial = _serial(make_check, self.N)
        rust_parallel = _parallel(make_check, self.N)
        rust_speedup = rust_serial / rust_parallel

        # If the GIL were still held, rust_speedup ≈ control_speedup (≈1.0).
        # Released, it is meaningfully higher. Relative comparison is stable.
        assert rust_speedup > control_speedup * 1.15, (
            f"GIL not released? rust={rust_speedup:.2f} control={control_speedup:.2f}"
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

    ROOT = _fixture_path("simple_package")

    def test_snapshot_isolated_from_reload(self) -> None:
        """Snapshot retains pre-reload state after session reloads."""
        session = TyO3Session(self.ROOT)
        try:
            snap = session.snapshot()
            pre_reload_files = snap.files()
            pre_reload_check = snap.check()

            # Reload the session — snapshot must not see this
            session.reload()
            post_reload_files = snap.files()
            post_reload_check = snap.check()

            # Same snapshot should return identical results
            assert pre_reload_files == post_reload_files, "Snapshot files changed after reload"
            assert len(pre_reload_check.diagnostics) == len(post_reload_check.diagnostics), (
                "Snapshot diagnostics changed after reload"
            )
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


# ── Original concurrency tests (kept) ────────────────────────────────────


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
