"""Phase 3 — write path tests.

Proves that the mutable head overlay edits drive analysis: an
overlay edit changes what `check()` / `document_symbols()` see without
touching disk, the application revision advances, and `SyncResult`
reports the right delta.
"""

from __future__ import annotations

import pytest
from tyo3._native_impl import TyProject

try:
    from tyo3 import TyO3Session

    _HAS_SESSION = True
except ImportError:
    _HAS_SESSION = False


def _num_diags(check_result) -> int:
    """Count diagnostics in a check result."""
    return len(check_result.diagnostics)


# ── Overlay edit changes diagnostics without touching disk ─────────────


def test_edit_changes_diagnostics_without_disk_write(tmp_path):
    """An overlay edit introduces a type error, but disk is unchanged."""
    p = tmp_path / "a.py"
    p.write_text("x: int = 1\n")
    session = TyO3Session(str(tmp_path))
    try:
        before = session.check()
        r = session.edit("a.py", "x: int = 'not an int'\n")
        assert r.revision > 0
        assert any("a.py" in c for c in r.changed)
        after = session.check()
        assert _num_diags(after) > _num_diags(before)
        # Disk is untouched
        assert p.read_text() == "x: int = 1\n"
    finally:
        session.close()


def test_edit_preserves_existing_file_state(tmp_path):
    """Editing a file that already exists changes what check() sees."""
    p = tmp_path / "a.py"
    p.write_text("x = 1\n")
    session = TyO3Session(str(tmp_path))
    try:
        before = session.check()
        r = session.edit("a.py", "x: int = 'not an int'\n")
        assert r.revision > 0
        assert any("a.py" in c for c in r.changed)
        assert len(r.created) == 0
        after = session.check()
        assert _num_diags(after) > _num_diags(before)
    finally:
        session.close()


# ── Revision advances ──────────────────────────────────────────────────


def test_head_revision_advances(tmp_path):
    """Each edit bumps the application revision."""
    (tmp_path / "a.py").write_text("x = 1\n")
    proj = TyProject.open(str(tmp_path))
    try:
        r0 = proj.head
        proj.edit("a.py", "x = 2\n")
        assert proj.head > r0
    finally:
        proj.close()


def test_head_starts_at_one_after_open_ingest(tmp_path):
    """Fresh project starts at revision 1.

    Convention (Phase 1, Step 1.3): the content store seeds an empty
    ``Revision(0)``; opening ingests the project's relevant files as the first
    batch, advancing head to ``Revision(1)`` before any explicit write.  So a
    freshly-opened project observes ``head == 1``, not 0.
    """
    (tmp_path / "a.py").write_text("x = 1\n")
    proj = TyProject.open(str(tmp_path))
    try:
        assert proj.head == 1
    finally:
        proj.close()


# ── Creating a new file via overlay ─────────────────────────────────────


def test_edit_creates_new_file(tmp_path):
    """Overlaying a path that doesn't exist on disk should report `created`."""
    (tmp_path / "a.py").write_text("x = 1\n")
    proj = TyProject.open(str(tmp_path))
    try:
        r = proj.edit("b.py", "y = 2\n")
        assert r["revision"] > 0
        assert len(r["created"]) > 0
        assert any("b.py" in c for c in r["created"])
        # The new file does not appear on disk
        assert not (tmp_path / "b.py").exists()
    finally:
        proj.close()


# ── Virtual buffers ─────────────────────────────────────────────────────


def test_edit_virtual_is_analyzable(tmp_path):
    """Virtual buffers don't touch disk but can be analyzed."""
    (tmp_path / "a.py").write_text("x = 1\n")
    proj = TyProject.open(str(tmp_path))
    try:
        r = proj.edit_virtual("untitled:1", "y: int = 'bad'\n")
        assert r["revision"] > 0
        # No file written to disk
        assert not (tmp_path / "untitled:1").exists()
    finally:
        proj.close()


def test_edit_virtual_reports_created(tmp_path):
    """First edit_virtual call for a URI reports it as created."""
    (tmp_path / "a.py").write_text("x = 1\n")
    proj = TyProject.open(str(tmp_path))
    try:
        r = proj.edit_virtual("untitled:2", "z = 3\n")
        assert len(r["created"]) > 0
        assert any("untitled:2" in c for c in r["created"])
    finally:
        proj.close()


# ── edit_many ───────────────────────────────────────────────────────────


def test_edit_many_batches_atomically(tmp_path):
    """Multiple files in one edit_many: single revision, single publish."""
    (tmp_path / "a.py").write_text("x = 1\n")
    proj = TyProject.open(str(tmp_path))
    try:
        r = proj.edit_many({"a.py": "x = 2\n", "b.py": "y = 3\n"})
        assert r["revision"] > 0
        # a.py is changed (exists), b.py is created
        assert any("a.py" in c for c in r["changed"])
        assert any("b.py" in c for c in r["created"])
    finally:
        proj.close()


# ── sync_path ───────────────────────────────────────────────────────────


def test_sync_path_ingests_disk_change(tmp_path):
    """sync_path re-reads disk truth after dropping the overlay."""
    p = tmp_path / "a.py"
    p.write_text("disk_v1 = 1\n")
    proj = TyProject.open(str(tmp_path))
    try:
        # Overlay it, then change disk, then sync
        proj.edit("a.py", "overlay = 99\n")
        p.write_text("disk_v2 = 2\n")
        r = proj.sync_path("a.py")
        assert r["revision"] > 0
    finally:
        proj.close()


# ── discard ─────────────────────────────────────────────────────────────


def test_discard_reverts_to_disk(tmp_path):
    """discard drops the overlay and re-reads disk."""
    p = tmp_path / "a.py"
    p.write_text("disk = 42\n")
    proj = TyProject.open(str(tmp_path))
    try:
        proj.edit("a.py", "overlay = 99\n")
        r = proj.discard("a.py")
        assert r["revision"] > 0
    finally:
        proj.close()


# ── sync_all ────────────────────────────────────────────────────────────


def test_sync_all_advances_revision(tmp_path):
    """sync_all triggers a rescan and bumps the revision."""
    (tmp_path / "a.py").write_text("x = 1\n")
    proj = TyProject.open(str(tmp_path))
    try:
        r0 = proj.head
        r = proj.sync_all()
        assert r["revision"] > r0
        assert r["rescan"] is True
    finally:
        proj.close()


# ── Snapshot-must-be-closed-before-edit (Phase 3 contract) ─────────────


def test_snapshot_must_be_closed_before_edit(tmp_path):
    """Phase 3 contract: a live snapshot shares the HEAD Zalsa and blocks the
    writer (architecture §0). Close it before editing. (Phase 4 removes this.)"""
    (tmp_path / "a.py").write_text("x = 1\n")
    proj = TyProject.open(str(tmp_path))
    try:
        snap = proj.snapshot()
        snap.check()
        snap.close()  # <- MUST close before edit, or edit() hangs
        proj.edit("a.py", "x = 2\n")
    finally:
        proj.close()


# ── SyncResult shape assertions ────────────────────────────────────────


def test_sync_result_has_expected_keys(tmp_path):
    """Every write method returns a SyncResult dict with the expected keys."""
    (tmp_path / "a.py").write_text("x = 1\n")
    proj = TyProject.open(str(tmp_path))
    try:
        expected_keys = {
            "revision",
            "created",
            "changed",
            "deleted",
            "project_changed",
            "custom_stdlib_changed",
            "rescan",
        }
        for result in [
            proj.edit("a.py", "x = 2\n"),
            proj.sync_all(),
        ]:
            assert expected_keys <= set(result.keys()), f"missing keys in {result}"
    finally:
        proj.close()
