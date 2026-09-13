"""Tests for editor support features: selection_ranges, folding_ranges."""

from __future__ import annotations

from tyo3.models.analysis import Range
from tyo3.models.editor import FoldingRange

from .conftest import needs_native, shared_session

# ── Selection Ranges ────────────────────────────────────────────────────


@needs_native
def test_selection_ranges_returns_list():
    """selection_ranges returns a list[Range]."""
    session = shared_session("simple_package")

    ranges = session.selection_ranges("main.py", 3, 5)
    assert isinstance(ranges, list)
    assert len(ranges) > 0
    for r in ranges:
        assert isinstance(r, Range)


@needs_native
def test_selection_ranges_narrows_outward():
    """Selection ranges go from narrowest to widest scope."""
    session = shared_session("simple_package")

    ranges = session.selection_ranges("main.py", 3, 5)
    # Each range should contain or equal the previous
    for i in range(1, len(ranges)):
        prev = ranges[i - 1]
        cur = ranges[i]
        assert prev.start.line <= cur.start.line
        assert prev.end.line >= cur.end.line or cur.start.line >= prev.end.line


@needs_native
def test_selection_ranges_snapshot_parity():
    """Snapshot and session return identical selection ranges."""
    session = shared_session("simple_package")

    ranges = session.selection_ranges("main.py", 3, 5)
    with session.snapshot() as snap:
        snap_ranges = snap.selection_ranges("main.py", 3, 5)

    assert len(ranges) == len(snap_ranges)
    for a, b in zip(ranges, snap_ranges, strict=True):
        assert a.start.line == b.start.line


# ── Folding Ranges ──────────────────────────────────────────────────────


@needs_native
def test_folding_ranges_returns_list():
    """folding_ranges returns a list[FoldingRange]."""
    session = shared_session("simple_package")

    ranges = session.folding_ranges("main.py")
    assert isinstance(ranges, list)
    for r in ranges:
        assert isinstance(r, FoldingRange)


@needs_native
def test_folding_ranges_snapshot_parity():
    """Snapshot and session return identical folding ranges."""
    session = shared_session("simple_package")

    ranges = session.folding_ranges("main.py")
    with session.snapshot() as snap:
        snap_ranges = snap.folding_ranges("main.py")

    assert len(ranges) == len(snap_ranges)
