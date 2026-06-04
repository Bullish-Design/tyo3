"""Tests for hints features: inlay_hints, hints."""

from __future__ import annotations

from tyo3.models.editor import Hint, InlayHint

from .conftest import needs_native, shared_session


# ── Inlay Hints ─────────────────────────────────────────────────────────


@needs_native
def test_inlay_hints_returns_list():
    """inlay_hints returns a list[InlayHint]."""
    session = shared_session("simple_package")

    hints = session.inlay_hints("main.py")
    assert isinstance(hints, list)
    for h in hints:
        assert isinstance(h, InlayHint)


@needs_native
def test_inlay_hints_snapshot_parity():
    """Snapshot and session return equivalent inlay hints."""
    session = shared_session("simple_package")

    hints = session.inlay_hints("main.py")
    with session.snapshot() as snap:
        snap_hints = snap.inlay_hints("main.py")

    assert len(hints) == len(snap_hints)


# ── Hints ───────────────────────────────────────────────────────────────


@needs_native
def test_hints_returns_list():
    """hints returns a list[Hint]."""
    session = shared_session("simple_package")

    hints = session.hints("main.py")
    assert isinstance(hints, list)
    for h in hints:
        assert isinstance(h, Hint)


@needs_native
def test_hints_snapshot_parity():
    """Snapshot and session return equivalent hints."""
    session = shared_session("simple_package")

    hints = session.hints("main.py")
    with session.snapshot() as snap:
        snap_hints = snap.hints("main.py")

    assert len(hints) == len(snap_hints)
