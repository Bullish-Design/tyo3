"""Concurrency test — validates GIL release during native analysis.

Phase 3 of the final refactor releases the GIL during heavy ty/Salsa
computation via ``py.allow_threads``.  This test proves the headline:
multiple ``TyO3Session`` instances on separate threads can call analysis
methods concurrently without deadlock and without crashing.

Per the README, one session must not be shared across threads — each
thread opens its own session on the same project root.
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path as StdPath

import pytest

# ── Native extension detection ──────────────────────────────────────────

try:
    from tyo3.session import TyO3Session

    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False

needs_native = pytest.mark.skipif(not _HAS_NATIVE, reason="Rust native extension not built")

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def _fixture_path(name: str) -> str:
    return str((FIXTURES_DIR / name).resolve())


# ── Per-thread workloads ────────────────────────────────────────────────


def _run_check(root: str) -> int:
    """Open a session, run check, return diagnostic count."""
    session = TyO3Session(root)
    try:
        result = session.check()
        return len(result.diagnostics)
    finally:
        session.close()


def _run_document_symbols(root: str) -> int:
    """Open a session, get document symbols for main.py, return symbol count."""
    session = TyO3Session(root)
    try:
        symbols = session.document_symbols("main.py")
        return len(symbols)
    finally:
        session.close()


def _do_workloads(root: str) -> tuple[int, int]:
    """Run check and document_symbols in the same session and return counts."""
    session = TyO3Session(root)
    try:
        result = session.check()
        symbols = session.document_symbols("main.py")
        return len(result.diagnostics), len(symbols)
    finally:
        session.close()


# ── Tests ────────────────────────────────────────────────────────────────


@needs_native
class TestConcurrency:
    """Validate that concurrent native sessions work correctly."""

    ROOT = _fixture_path("simple_package")

    def test_concurrent_checks(self) -> None:
        """Multiple threads opening separate sessions and running check()."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(_run_check, self.ROOT) for _ in range(4)]
            results = [f.result(timeout=30) for f in futures]

        # All should return valid results (diagnostic count including info-level)
        assert all(isinstance(r, int) for r in results), f"Expected int diagnostic counts, got: {results}"
        # All sessions from same pristine root should return identical counts
        assert len(set(results)) == 1, f"Concurrent checks returned differing diagnostic counts: {results}"

    def test_concurrent_document_symbols(self) -> None:
        """Multiple threads opening separate sessions and getting document symbols."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(_run_document_symbols, self.ROOT) for _ in range(4)]
            results = [f.result(timeout=30) for f in futures]

        assert all(isinstance(r, int) for r in results), f"Expected int symbol counts, got: {results}"
        assert len(set(results)) == 1, f"Concurrent symbol lookups returned differing counts: {results}"

    def test_concurrent_mixed_workloads(self) -> None:
        """Threads running different analysis methods simultaneously."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(_run_check, self.ROOT),
                executor.submit(_run_document_symbols, self.ROOT),
                executor.submit(_do_workloads, self.ROOT),
                executor.submit(_run_check, self.ROOT),
            ]
            results = [f.result(timeout=30) for f in futures]

        # First, third, fourth are check → int
        assert isinstance(results[0], int)
        # Second is document_symbols → int
        assert isinstance(results[1], int)
        # Third is (diagnostics, symbols) tuple
        assert isinstance(results[2], tuple) and len(results[2]) == 2
        assert all(isinstance(x, int) for x in results[2])
        # Fourth is check → int
        assert isinstance(results[3], int)

    def test_no_deadlock_on_repeat_sessions(self) -> None:
        """Stress: open/close sessions in rapid succession across threads."""

        def _open_and_close(root: str) -> None:
            for _ in range(5):
                session = TyO3Session(root)
                session.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(_open_and_close, self.ROOT) for _ in range(8)]
            # If there's a deadlock, this raises TimeoutError
            for f in futures:
                f.result(timeout=30)
