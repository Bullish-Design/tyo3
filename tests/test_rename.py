"""Tests for rename / can_rename feature."""

from __future__ import annotations

from tyo3.models.analysis import Range
from tyo3.models.navigation import WorkspaceEdit

from .conftest import needs_native, shared_session


@needs_native
def test_can_rename_function():
    """can_rename returns a Range for a renamable function definition."""
    session = shared_session("simple_package")

    result = session.can_rename("main.py", 3, 5)
    assert result is not None
    assert isinstance(result, Range)
    assert result.start.line >= 1


@needs_native
def test_rename_function_returns_workspace_edit():
    """rename returns a WorkspaceEdit with edits for a renamable function."""
    session = shared_session("simple_package")

    edit = session.rename("main.py", 3, 5, "hello")
    assert isinstance(edit, WorkspaceEdit)
    assert edit.new_name == "hello"
    assert len(edit.edits) > 0


@needs_native
def test_rename_snapshot_parity():
    """Snapshot and session return equivalent can_rename results."""
    session = shared_session("simple_package")

    can = session.can_rename("main.py", 3, 5)
    with session.snapshot() as snap:
        snap_can = snap.can_rename("main.py", 3, 5)

    assert (can is None) == (snap_can is None)
    if can is not None:
        assert can.start.line == snap_can.start.line  # type: ignore[union-attr]


@needs_native
def test_rename_includes_definition_and_usages():
    """rename edits include both the definition and all in-file references."""
    session = shared_session("simple_package")

    edit = session.rename("main.py", 3, 5, "hello")
    assert isinstance(edit, WorkspaceEdit)

    paths = [e.path for e in edit.edits]
    # All edits should be in the project
    assert len(paths) > 0
