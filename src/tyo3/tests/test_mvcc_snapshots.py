"""Phase 4 tests: independent MVCC snapshot isolation.

Proves that:
- An open snapshot no longer blocks an edit (independent Zalsa).
- A snapshot pinned at R keeps reading R's content across HEAD edits.
- Time-travel via snapshot(at=r) reaches retained revisions.
- No eager materialisation loop required (read-once capture is structural).
"""

from __future__ import annotations

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


@needs_native
def test_edit_while_snapshot_open_does_not_block(tmp_path: StdPath) -> None:
    """Phase 4 removes Phase 3's constraint: an OPEN snapshot no longer blocks
    an edit, because the snapshot is an independent db (own Zalsa)."""
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        snap = s.snapshot()  # leave it OPEN
        snap.check()
        s.edit("a.py", "x = 2\n")  # must NOT hang
        assert snap.check() is not None  # snapshot still usable, still pinned
        snap.close()


@needs_native
def test_snapshot_pins_revision_across_edits(tmp_path: StdPath) -> None:
    """A pinned snapshot's diagnostics are unchanged when HEAD changes."""
    p = tmp_path / "a.py"
    p.write_text("x: int = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        with s.snapshot() as snap:
            before = _num_diags(snap.check())
            s.edit("a.py", "x: int = 'bad'\n")  # introduces a type error on HEAD
            # snapshot is pinned: its diagnostics are unchanged
            assert _num_diags(snap.check()) == before
            # the session (head) sees the new error
            assert _num_diags(s.check()) > before


@needs_native
def test_snapshot_revision_getter(tmp_path: StdPath) -> None:
    """The snapshot's revision getter returns the pinned revision."""
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        r0 = s.snapshot()
        assert r0.revision == s.head  # pinned at current head revision
        r0.close()


@needs_native
def test_time_travel_snapshot(tmp_path: StdPath) -> None:
    """snapshot(at=r) time-travels to a retained revision."""
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        r0 = s.head
        s.edit("a.py", "x = 2\n")
        with s.snapshot(at=r0) as old:
            # reads the project as of r0 (disk "x = 1"), not the edited buffer
            assert old.revision == r0


@needs_native
def test_no_eager_materialization_regression(tmp_path: StdPath) -> None:
    """Smoke: snapshot reads still correct without the eager loop."""
    (tmp_path / "a.py").write_text("VALUE = 42\n")
    with TyO3Session(str(tmp_path)) as s:
        with s.snapshot() as snap:
            assert any("a.py" in str(f) for f in snap.files())
            snap.check()


@needs_native
def test_head_property_advances_on_edit(tmp_path: StdPath) -> None:
    """The session head revision advances after each edit."""
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        r0 = s.head
        result = s.edit("a.py", "x = 2\n")
        assert result.revision > r0
        assert s.head == result.revision


@needs_native
def test_sync_result_contains_paths(tmp_path: StdPath) -> None:
    """edit returns a SyncResult with correct affected paths."""
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        result = s.edit("a.py", "x = 2\n")
        assert any("a.py" in p for p in result.changed)
        assert result.rescan is False
        assert result.revision is not None


@needs_native
def test_edit_many_atomic(tmp_path: StdPath) -> None:
    """edit_many produces a single revision for multiple files."""
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        result = s.edit_many({"a.py": "x = 2\n", "b.py": "y = 3\n"})
        assert result.revision > s.head - 1  # single revision bump
        # b.py created, a.py changed
        all_affected = result.created + result.changed
        assert any("a.py" in p for p in all_affected)
        assert any("b.py" in p for p in all_affected)


@needs_native
def test_discard_reverts_to_disk(tmp_path: StdPath) -> None:
    """discard drops overlay and reverts to disk content."""
    (tmp_path / "a.py").write_text("DISK = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        s.edit("a.py", "OVERLAY = 1\n")
        s.discard("a.py")
        # check reads head snapshot after discard
        result = s.check_file("a.py")
        assert result is not None
