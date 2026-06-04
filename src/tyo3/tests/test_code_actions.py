"""Tests for code_actions feature."""

from __future__ import annotations

from tyo3.models.navigation import QuickFix

from .conftest import needs_native, shared_session


@needs_native
def test_code_actions_returns_list_for_unknown_diagnostic():
    """code_actions returns empty list for unknown diagnostic IDs."""
    session = shared_session("simple_package")

    fixes = session.code_actions("main.py", 1, 1, 1, 10, "unknown-lint-code")
    assert isinstance(fixes, list)
    assert fixes == []


@needs_native
def test_code_actions_snapshot_parity():
    """Snapshot and session return equivalent code_actions results."""
    session = shared_session("simple_package")

    fixes = session.code_actions("main.py", 1, 1, 1, 10, "unknown-lint-code")
    with session.snapshot() as snap:
        snap_fixes = snap.code_actions("main.py", 1, 1, 1, 10, "unknown-lint-code")

    assert len(fixes) == len(snap_fixes)


@needs_native
def test_code_actions_for_unresolved_reference():
    """code_actions may return import quick fixes for unresolved references."""
    session = shared_session("simple_package")

    # Try a known lint code at a valid position
    fixes = session.code_actions("main.py", 19, 1, 19, 3, "unresolved-reference")
    assert isinstance(fixes, list)
    for fix in fixes:
        assert isinstance(fix, QuickFix)
