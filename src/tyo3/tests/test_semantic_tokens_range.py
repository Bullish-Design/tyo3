"""Tests for semantic_tokens range overload."""

from __future__ import annotations

from tyo3.models.advanced import SemanticToken

from .conftest import needs_native, shared_session


@needs_native
def test_semantic_tokens_no_range_still_works():
    """semantic_tokens without range args still returns tokens for whole file."""
    session = shared_session("simple_package")

    tokens = session.semantic_tokens("main.py")
    assert isinstance(tokens, list)
    assert len(tokens) > 0
    for t in tokens:
        assert isinstance(t, SemanticToken)


@needs_native
def test_semantic_tokens_with_range_returns_subset():
    """semantic_tokens with a range returns tokens within that range."""
    session = shared_session("simple_package")

    # Get all tokens
    all_tokens = session.semantic_tokens("main.py")

    # Get tokens for just line 3 (col 1 to end of line, which is col 30)
    range_tokens = session.semantic_tokens(
        "main.py", start_line=3, start_col=1, end_line=3, end_col=30
    )

    assert isinstance(range_tokens, list)
    # Range tokens should be a subset of all tokens
    assert len(range_tokens) <= len(all_tokens)


@needs_native
def test_semantic_tokens_range_snapshot_parity():
    """Snapshot and session return equivalent range tokens."""
    session = shared_session("simple_package")

    tokens = session.semantic_tokens(
        "main.py", start_line=1, start_col=1, end_line=5, end_col=1
    )
    with session.snapshot() as snap:
        snap_tokens = snap.semantic_tokens(
            "main.py", start_line=1, start_col=1, end_line=5, end_col=1
        )

    assert len(tokens) == len(snap_tokens)
