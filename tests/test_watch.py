"""Phase 8: Watcher as a change source — deterministic and real-FS tests."""

from __future__ import annotations

import time

import pytest

from tyo3 import TyO3Session


def test_poll_changes_none_when_empty(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        assert s.poll_changes() is None


def test_injected_change_matches_expected_delta(tmp_path):
    (tmp_path / "a.py").write_text("x: int = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        r0 = s.head
        (tmp_path / "a.py").write_text("x: str = 'two'\n")  # change on disk
        s._inject_changes([("changed", "a.py")])
        sync = s.poll_changes()
        assert sync is not None
        assert sync.revision > r0
        assert any(p.endswith("a.py") for p in sync.changed)
        # HEAD now reflects disk:
        with s.snapshot() as snap:
            names = [sym.name for sym in snap.document_symbols("a.py")]
            # x should still be a symbol
            assert "x" in names


def test_overlaid_path_survives_disk_event(tmp_path):
    (tmp_path / "a.py").write_text("DISK = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        s.edit("a.py", "BUFFER = 2\n")  # unsaved buffer
        (tmp_path / "a.py").write_text("DISK = 999\n")
        s._inject_changes([("changed", "a.py")])
        assert s.poll_changes() is None  # buffer wins; nothing applied
        with s.snapshot() as snap:
            names = [sym.name for sym in snap.document_symbols("a.py")]
            assert "BUFFER" in names


def test_deleted_event(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        r0 = s.head
        s._inject_changes([("deleted", "a.py")])
        sync = s.poll_changes()
        assert sync is not None
        assert sync.revision > r0
        assert any(p.endswith("a.py") for p in sync.deleted)


def test_created_event(tmp_path):
    with TyO3Session(str(tmp_path)) as s:
        r0 = s.head
        (tmp_path / "new.py").write_text("y = 2\n")
        s._inject_changes([("created", "new.py")])
        sync = s.poll_changes()
        assert sync is not None
        assert sync.revision > r0
        assert any(p.endswith("new.py") for p in sync.created)


def test_injected_cross_file_move_preserves_id_and_reports_structured_move(tmp_path):
    body = "def helper():\n    return 1\n"
    (tmp_path / "a.py").write_text(body)
    (tmp_path / "b.py").write_text("")  # indexed destination placeholder

    with TyO3Session(str(tmp_path)) as s:
        s.sync_all()
        helper_id = s.id_for("a.py", 1, 5)
        assert helper_id is not None
        before = s.head

        (tmp_path / "a.py").unlink()
        (tmp_path / "b.py").write_text(body)
        s._inject_changes([("deleted", "a.py"), ("created", "b.py")])
        delta = s.poll_changes()

        assert delta is not None
        assert delta.revision == before + 1
        assert len(delta.moved) == 1
        moved = delta.moved[0]
        assert moved.id == helper_id
        assert moved.old_qualified_path.endswith("a.py::helper")
        assert moved.new_qualified_path.endswith("b.py::helper")
        assert moved.old_file.endswith("a.py")
        assert moved.new_file.endswith("b.py")
        assert helper_id not in delta.created_ids
        assert helper_id not in delta.deleted_ids
        assert any(path.endswith("a.py") for path in delta.deleted)
        assert any(path.endswith("b.py") for path in delta.created)
        assert s.locate(helper_id).endswith("b.py::helper")


def test_rescan_short_circuit(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        s._inject_changes([("changed", "a.py"), ("rescan", "/nonexistent")])
        sync = s.poll_changes()
        assert sync is not None
        assert sync.rescan


# ── Tolerant end-to-end watcher smoke test (real FS) ──────────────────


def _poll_until_change(session, *, timeout=5.0, interval=0.05):
    """Poll repeatedly until a SyncResult arrives or the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session.flush_watch()
        result = session.poll_changes()
        if result is not None:
            return result
        time.sleep(interval)
    return None


@pytest.mark.watcher
def test_real_watcher_observes_disk_change(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        s.watch()
        try:
            time.sleep(0.2)  # let the watcher register paths
            (tmp_path / "a.py").write_text("x = 2\ny = 3\n")
            result = _poll_until_change(s)
            assert result is not None, "watcher did not observe the disk change within the timeout"
            assert any(p.endswith("a.py") for p in result.changed) or result.rescan
        finally:
            s.unwatch()


@pytest.mark.watcher
def test_real_watcher_preserves_cross_file_move(tmp_path):
    body = "def helper():\n    return 1\n"
    (tmp_path / "a.py").write_text(body)
    (tmp_path / "b.py").write_text("")
    with TyO3Session(str(tmp_path)) as s:
        s.sync_all()
        helper_id = s.id_for("a.py", 1, 5)
        assert helper_id is not None
        before = s.head
        s.watch()
        try:
            time.sleep(0.2)  # let the watcher register paths
            (tmp_path / "a.py").rename(tmp_path / "b.py")
            result = _poll_until_change(s)
            assert result is not None, "watcher did not observe the move within the timeout"
            assert result.revision == before + 1
            assert len(result.moved) == 1
            moved = result.moved[0]
            assert moved.id == helper_id
            assert moved.old_qualified_path.endswith("a.py::helper")
            assert moved.new_qualified_path.endswith("b.py::helper")
            assert helper_id not in result.created_ids
            assert helper_id not in result.deleted_ids
            assert s.locate(helper_id).endswith("b.py::helper")
        finally:
            s.unwatch()
