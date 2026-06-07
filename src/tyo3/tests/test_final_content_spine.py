"""Final invariant tests — content authority (→ Phase 1).

Encodes §5.1 / §5.2: committed generations are the complete, authoritative
record of every revision, so a snapshot pins content at its revision and never
reads project content from disk.

These are written before the Phase 1 implementation.  The behaviours that the
current code already violates are marked ``xfail(strict=True)`` and tagged with
their target phase; they flip to failures (forcing the marker's removal) the
moment Phase 1 makes them hold.  Behaviours that already hold are left as live
regression guards.

The disk-read seam (test 4) is the one new piece of machinery Phase 1 must add:
a counter on the project-content disk-read path so "snapshot construction reads
no disk" can be proven structurally rather than by timing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tyo3 import TyO3Session
from tyo3.exceptions import RevisionEvictedError


def _open(root: Path, files: dict[str, str], *, retain_cap: int | None = None) -> TyO3Session:
    (root / "pyproject.toml").write_text("[project]\nname = 'spine'\nversion = '0.1.0'\n")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    if retain_cap is not None:
        cfg = root / ".tyo3"
        cfg.mkdir(exist_ok=True)
        (cfg / "config.toml").write_text(
            f"schema_version = 1\n\n[spine]\nretain_cap = {retain_cap}\n"
        )
    s = TyO3Session(str(root))
    s.sync_all()
    return s


def _symbol_names(snap, path: str) -> set[str]:
    return {s.name for s in snap.document_symbols(path)}


# ── 1. A snapshot does not observe a file created on disk after it is pinned ──


def test_snapshot_does_not_observe_file_created_after_pin(tmp_path):
    s = _open(tmp_path, {"a.py": "x = 1\n"})
    try:
        with s.snapshot() as snap:
            # Create a new relevant file on disk *after* the snapshot is pinned.
            (tmp_path / "b.py").write_text("y = 2\n")
            files = {str(f) for f in snap.files()}
            assert not any(f.endswith("b.py") for f in files), (
                "snapshot pinned before b.py existed must not observe it"
            )
    finally:
        s.close()


# ── 2. A snapshot keeps R's content even after disk changes later ─────────────


def test_snapshot_keeps_revision_content_after_disk_change(tmp_path):
    s = _open(tmp_path, {"mod.py": "def alpha():\n    return 1\n"})
    try:
        with s.snapshot() as snap:
            assert "alpha" in _symbol_names(snap, "mod.py")
            # Mutate disk out from under the pinned snapshot (no sync).
            (tmp_path / "mod.py").write_text("def beta():\n    return 2\n")
            names = _symbol_names(snap, "mod.py")
            assert "alpha" in names, "pinned snapshot must keep R's content"
            assert "beta" not in names, "pinned snapshot must not see later disk content"
    finally:
        s.close()


# ── 3. Two snapshots at the same R are identical despite disk churn ───────────


def test_two_snapshots_at_same_revision_are_identical(tmp_path):
    s = _open(tmp_path, {"mod.py": "def alpha():\n    return 1\n"})
    try:
        r = s.head
        snap1 = s.snapshot(at=r)
        # Disk changes between the two captures of the same revision.
        (tmp_path / "mod.py").write_text("def gamma():\n    return 9\n")
        snap2 = s.snapshot(at=r)
        try:
            assert snap1.revision == snap2.revision == r
            assert _symbol_names(snap1, "mod.py") == _symbol_names(snap2, "mod.py"), (
                "two snapshots at the same revision must be identical regardless "
                "of disk state between their construction"
            )
        finally:
            snap1.close()
            snap2.close()
    finally:
        s.close()


# ── 4. Snapshot construction performs no project-content disk read ───────────


def _disk_read_counter(session) -> int | None:
    """Read the project-content disk-read counter, the Phase 1 test seam.

    Phase 1 must expose a counter on the native handle that increments on every
    project-content disk read (the routine that today walks the live filesystem
    during snapshot construction).  Returns ``None`` if the seam is absent so
    the test can report a precise, actionable failure.
    """
    inner = getattr(session, "_inner", None)
    for attr in ("project_content_disk_reads", "_project_content_disk_reads", "disk_read_count"):
        fn = getattr(inner, attr, None)
        if fn is not None:
            return fn() if callable(fn) else fn
    return None


def test_snapshot_construction_reads_no_disk(tmp_path):
    s = _open(tmp_path, {"a.py": "x = 1\n", "pkg/m.py": "def f():\n    return 1\n"})
    try:
        before = _disk_read_counter(s)
        assert before is not None, "Phase 1 must add a project-content disk-read counter seam"
        snap = s.snapshot()
        snap.files()  # force any lazy capture
        after = _disk_read_counter(s)
        snap.close()
        assert after == before, (
            f"snapshot construction must read no project content from disk "
            f"(counter went {before} -> {after})"
        )
    finally:
        s.close()


# ── 5. Pinning an evicted revision raises a typed RevisionEvicted error ───────


def test_pinning_evicted_revision_raises_typed_error(tmp_path):
    s = _open(tmp_path, {"a.py": "x = 0\n"}, retain_cap=4)
    try:
        r0 = s.head
        # Advance well past the retain window so r0 is evicted oldest-first.
        for i in range(12):
            s.edit("a.py", f"x = {i + 1}\n")
        with pytest.raises(RevisionEvictedError):
            s.snapshot(at=r0)
    finally:
        s.close()
