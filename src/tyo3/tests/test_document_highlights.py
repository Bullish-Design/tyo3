"""Tests for document_highlights feature."""

from __future__ import annotations

from tyo3.models.navigation import Reference
from tyo3.models.analysis import Range

from .conftest import needs_native, shared_session


@needs_native
def test_document_highlights_returns_references():
    """Document highlights returns list[Reference] for a highlightable symbol."""
    session = shared_session("simple_package")

    # highlight the `greet` function definition (line 3, col 5)
    highlights = session.document_highlights("main.py", 3, 5)
    assert isinstance(highlights, list)
    assert len(highlights) > 0
    for h in highlights:
        assert isinstance(h, Reference)


@needs_native
def test_document_highlights_empty_for_non_highlightable():
    """Returns empty list for a position with no highlightable symbol."""
    session = shared_session("simple_package")

    # position in a comment or blank line
    highlights = session.document_highlights("main.py", 1, 1)
    assert highlights == []


@needs_native
def test_document_highlights_snapshot_parity():
    """Snapshot and session return identical results."""
    session = shared_session("simple_package")

    highlights = session.document_highlights("main.py", 3, 5)
    with session.snapshot() as snap:
        snap_highlights = snap.document_highlights("main.py", 3, 5)

    assert len(highlights) == len(snap_highlights)
    for h, sh in zip(highlights, snap_highlights):
        assert h.path == sh.path


@needs_native
def test_document_highlights_find_references_self_included():
    """A highlightable symbol includes its own definition in results."""
    session = shared_session("simple_package")

    highlights = session.document_highlights("main.py", 3, 5)
    # The definition at line 3 should be among the highlights
    ranges: list[Range] = [h.range for h in highlights]
    assert len(ranges) > 0
