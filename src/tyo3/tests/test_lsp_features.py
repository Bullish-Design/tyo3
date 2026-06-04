"""Tests for LSP support features: signature_help, completions."""

from __future__ import annotations

from tyo3.models.lsp import Completion, SignatureHelp

from .conftest import needs_native, shared_session


# ── Signature Help ───────────────────────────────────────────────────────


@needs_native
def test_signature_help_returns_none_for_non_call():
    """signature_help returns None for positions not inside a function call."""
    session = shared_session("simple_package")

    # line 1 column 1 is a comment/docstring
    result = session.signature_help("main.py", 1, 1)
    assert result is None


@needs_native
def test_signature_help_snapshot_parity():
    """Snapshot and session return equivalent signature_help."""
    session = shared_session("simple_package")

    result = session.signature_help("main.py", 13, 1)
    with session.snapshot() as snap:
        snap_result = snap.signature_help("main.py", 13, 1)

    assert (result is None) == (snap_result is None)


# ── Completions ──────────────────────────────────────────────────────────


@needs_native
def test_completions_returns_list():
    """completions returns a list[Completion]."""
    session = shared_session("simple_package")

    completions = session.completions("main.py", 13, 1)
    assert isinstance(completions, list)
    for c in completions:
        assert isinstance(c, Completion)


@needs_native
def test_completions_snapshot_parity():
    """Snapshot and session return equivalent list lengths."""
    session = shared_session("simple_package")

    completions = session.completions("main.py", 13, 1)
    with session.snapshot() as snap:
        snap_completions = snap.completions("main.py", 13, 1)

    assert len(completions) == len(snap_completions)
