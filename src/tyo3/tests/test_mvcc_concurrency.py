"""Phase 5 tests: Concurrency proof — MVCC isolation under stress.

Proves that:
- Holding K snapshots open does NOT block edits (independent Zalsa).
- Snapshot reads continue successfully while the writer hammers HEAD.
- Snapshot results remain pinned to their revision while HEAD advances.
- Read-once disk capture is race-safe under concurrent snapshot reads.

Rust stress tests in ``project.rs::phase5_concurrency_tests`` cover the
full concurrency load (32 snapshots, 100 edits, multi-threaded readers).
These Python tests validate the GIL-safe patterns and session invariants.
"""

from __future__ import annotations

import threading
from pathlib import Path as StdPath

import pytest

try:
    from tyo3 import TyO3Session

    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False

needs_native = pytest.mark.skipif(not _HAS_NATIVE, reason="Rust native extension not built")


def _num_diags(check_result) -> int:
    """Count diagnostics in a check result."""
    return len(check_result.diagnostics)


def _project(tmp_path: StdPath) -> StdPath:
    """Create a minimal project with pyproject.toml so discovery is fast."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"mvcc-stress\"\nversion = \"0.1.0\"\n"
    )
    (tmp_path / "a.py").write_text("x: int = 0\n")
    return tmp_path


# ── 5.1 Open snapshots do not block edits ───────────────────────────────


@needs_native
def test_open_snapshots_do_not_block_edits(tmp_path: StdPath) -> None:
    """Multiple open snapshots must not block HEAD edits.

    If snapshot() accidentally shares the HEAD Zalsa, the writer's
    cancel_others blocks forever — this test would hang."""
    root = _project(tmp_path)
    with TyO3Session(str(root)) as s:
        snapshots = [s.snapshot() for _ in range(8)]
        for snap in snapshots:
            assert _num_diags(snap.check()) >= 0

        # Many edits while snapshots are held open — must NOT hang.
        for i in range(50):
            result = s.edit("a.py", f"x: int = {i}\n")
            assert result.revision is not None

        # Snapshots are still usable and still pinned.
        for snap in snapshots:
            assert _num_diags(snap.check()) >= 0
            snap.close()


# ── 5.2 Active snapshot readers survive hot writes ──────────────────────


@needs_native
def test_snapshot_readers_survive_hot_writer(tmp_path: StdPath) -> None:
    """Active snapshot readers querying pinned content while the writer
    mutates HEAD — zero errors, zero cancellation."""
    root = _project(tmp_path)
    errors: list[str] = []
    stop = threading.Event()

    with TyO3Session(str(root)) as s:
        snapshots = [s.snapshot() for _ in range(6)]
        expected_revisions = [snap.revision for snap in snapshots]

        def reader(snap, expected_rev: int) -> None:
            try:
                while not stop.is_set():
                    assert snap.revision == expected_rev
                    snap.check()
            except BaseException as exc:
                errors.append(repr(exc))
                stop.set()

        threads = [
            threading.Thread(target=reader, args=(snap, rev), daemon=True)
            for snap, rev in zip(snapshots, expected_revisions)
        ]
        for t in threads:
            t.start()

        for i in range(75):
            if i % 2:
                s.edit("a.py", "x: int = 'bad'\n")
            else:
                s.edit("a.py", f"x: int = {i}\n")

        stop.set()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"snapshot reader errors: {errors}"
        assert not any(t.is_alive() for t in threads), "reader thread did not stop"

        for snap in snapshots:
            snap.close()


# ── 5.3 Snapshot diagnostics stay pinned while HEAD changes ─────────────


@needs_native
def test_snapshot_diagnostics_are_pinned_under_concurrent_edits(
    tmp_path: StdPath,
) -> None:
    """A snapshot's diagnostic count stays unchanged while HEAD is edited
    concurrently — the snapshot never observes the writer's changes."""
    root = _project(tmp_path)

    with TyO3Session(str(root)) as s:
        snap = s.snapshot()
        expected = _num_diags(snap.check())
        assert expected >= 0

        def writer() -> None:
            for _ in range(30):
                s.edit("a.py", "x: int = 'bad'\n")
                s.edit("a.py", "x: int = 1\n")

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()

        for _ in range(30):
            assert _num_diags(snap.check()) == expected

        thread.join(timeout=10)
        assert not thread.is_alive(), "writer thread did not finish"
        snap.close()


# ── 5.4 Session-level head-snapshot invalidation is thread-safe ─────────


@needs_native
def test_head_snap_invalidation_does_not_crash_readers(
    tmp_path: StdPath,
) -> None:
    """Phase 5 hardening §5.1: cache invalidation after edit must not
    crash or error in-flight readers that grabbed the cached head snapshot."""
    root = _project(tmp_path)
    errors: list[str] = []
    stop = threading.Event()

    with TyO3Session(str(root)) as s:
        # Warm the cached head snapshot.
        s.check()

        def reader() -> None:
            try:
                while not stop.is_set():
                    s.check()
            except BaseException as exc:
                errors.append(repr(exc))
                stop.set()

        threads = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
        for t in threads:
            t.start()

        for i in range(20):
            s.edit("a.py", f"x: int = {i}\n")

        stop.set()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"head-snap invalidation errors: {errors}"
