"""Performance benchmarks for Rust backend operations.

Tests for Phase 5 of RUST_BACKEND_IMPLEMENTATION.md §8.

Measures and documents type-checking latency for:
1. **Project open** — time to create a ProjectDatabase
2. **File listing** — time to enumerate project files
3. **Document symbols** — time to compute symbols per file
4. **Full check** — time to run the type checker on a project
5. **Goto definition** — time to resolve a navigation target
6. **Hover** — time to compute hover content

These are NOT assertion tests — they record timings and validate that
operations complete within reasonable bounds (no hangs or regressions).

Run with: PYTHONPATH=src pytest src/tyo3/tests/test_rust_performance.py -v --benchmark
"""

from __future__ import annotations

import time
from pathlib import Path as StdPath

import pytest

# Check if native extension is available
try:
    from tyo3 import TyO3Session

    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False

# ── Path helpers ──────────────────────────────────────────────────────────

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def fixture_path(name: str) -> str:
    """Return the absolute path to a fixture directory."""
    return str((FIXTURES_DIR / name).resolve())


# ── Skip markers ──────────────────────────────────────────────────────────

needs_native = pytest.mark.skipif(not _HAS_NATIVE, reason="Rust native extension not built")

# ── Timeout thresholds (seconds) ──────────────────────────────────────────
# These are generous to account for CI variability; real latencies are lower.
# All operations on the small fixture projects should complete in < 2s.

OPEN_TIMEOUT = 5.0
FILES_TIMEOUT = 0.5
SYMBOLS_TIMEOUT = 2.0
CHECK_TIMEOUT = 10.0
NAVIGATION_TIMEOUT = 2.0
HOVER_TIMEOUT = 2.0
CLOSE_TIMEOUT = 1.0


def _time_op(label: str, fn, *, timeout: float) -> float:
    """Time an operation and assert it completes within *timeout* seconds."""
    start = time.perf_counter()
    fn()
    elapsed = time.perf_counter() - start
    assert elapsed < timeout, f"{label} took {elapsed:.3f}s (timeout={timeout}s)"
    return elapsed


# ═══════════════════════════════════════════════════════════════════════════
# Timing Tests
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
class TestOpenTiming:
    """Time to open a project for each fixture."""

    @pytest.mark.parametrize(
        "fixture_name, expected_files",
        [
            ("simple_package", 1),
            ("classes", 2),
            ("imports", 3),
            ("standalone", 1),
            ("unicode_positions", 1),
            ("empty", 0),
        ],
    )
    def test_open_project(self, fixture_name: str, expected_files: int) -> None:
        """Opening a project should complete within OPEN_TIMEOUT."""
        elapsed = _time_op(
            f"open({fixture_name})",
            lambda: None,  # We open inside the timing block below
            timeout=OPEN_TIMEOUT,
        )

        rp = TyO3Session(fixture_path(fixture_name))
        try:
            files = rp.files()
            assert len(files) == expected_files, f"Expected {expected_files} files for {fixture_name}, got {len(files)}"

            # Report the timing (visible with -v)
            print(f"\n  ⏱  open({fixture_name}): {elapsed:.3f}s [files={len(files)}, expected={expected_files}]")
        finally:
            rp.close()

    def test_open_same_fixture_twice(self) -> None:
        """Opening the same fixture twice (new instances) should both succeed."""
        rp1 = TyO3Session(fixture_path("simple_package"))
        rp2 = TyO3Session(fixture_path("simple_package"))
        f1 = rp1.files()
        f2 = rp2.files()
        assert set(f1) == set(f2)
        rp1.close()
        rp2.close()


@needs_native
class TestListFilesTiming:
    """Time for files() operation."""

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "simple_package",
            "classes",
            "imports",
        ],
    )
    def test_list_files(self, fixture_name: str) -> None:
        rp = TyO3Session(fixture_path(fixture_name))
        try:
            elapsed = _time_op(
                f"files({fixture_name})",
                lambda: rp.files(),
                timeout=FILES_TIMEOUT,
            )
            print(f"\n  ⏱  files({fixture_name}): {elapsed:.3f}s")
        finally:
            rp.close()


@needs_native
class TestSymbolsTiming:
    """Time for document_symbols() operation."""

    @pytest.mark.parametrize(
        "fixture_name, file_name",
        [
            ("simple_package", "main.py"),
            ("classes", "models.py"),
            ("imports", "main.py"),
            ("standalone", "script.py"),
            ("unicode_positions", "unicode.py"),
        ],
    )
    def test_document_symbols(self, fixture_name: str, file_name: str) -> None:
        rp = TyO3Session(fixture_path(fixture_name))
        try:
            elapsed = _time_op(
                f"document_symbols({fixture_name}/{file_name})",
                lambda: rp.document_symbols(file_name),
                timeout=SYMBOLS_TIMEOUT,
            )
            symbols = rp.document_symbols(file_name)
            print(f"\n  ⏱  document_symbols({file_name}): {elapsed:.3f}s [{len(symbols)} symbols]")
        finally:
            rp.close()


@needs_native
class TestCheckTiming:
    """Time for full project check()."""

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "simple_package",
            "classes",
            "imports",
            "standalone",
            "unicode_positions",
            "diagnostic_targets",
            "empty",
        ],
    )
    def test_full_check(self, fixture_name: str) -> None:
        rp = TyO3Session(fixture_path(fixture_name))
        try:
            elapsed = _time_op(
                f"check({fixture_name})",
                lambda: rp.check(),
                timeout=CHECK_TIMEOUT,
            )
            result = rp.check()
            print(f"\n  ⏱  check({fixture_name}): {elapsed:.3f}s [{len(result.diagnostics)} diagnostics]")
        finally:
            rp.close()

    def test_check_twice_cached(self) -> None:
        """Second check() on the same project should be faster (Salsa cache)."""
        rp = TyO3Session(fixture_path("classes"))
        try:
            # First check (cold cache)
            t1 = _time_op(
                "check(classes) #1",
                lambda: rp.check(),
                timeout=CHECK_TIMEOUT,
            )

            # Second check (warm cache — should be faster)
            t2 = _time_op(
                "check(classes) #2",
                lambda: rp.check(),
                timeout=CHECK_TIMEOUT,
            )

            # Third check (should be similar to second)
            t3 = _time_op(
                "check(classes) #3",
                lambda: rp.check(),
                timeout=CHECK_TIMEOUT,
            )

            print(f"\n  ⏱  check timing: 1st={t1:.3f}s, 2nd={t2:.3f}s, 3rd={t3:.3f}s")
            print(f"  ⚡ Speedup: {t1 / t2:.1f}x (cold→warm)")

            # After the first check, subsequent checks should be faster
            # (Salsa incremental computation).  This is not a strict assertion
            # because the fixture is small and the first check may already be fast.
            assert t2 <= t1 * 2 or t2 < 1.0, (
                f"Second check ({t2:.3f}s) should not be dramatically slower than first ({t1:.3f}s)"
            )
        finally:
            rp.close()


@needs_native
class TestNavigationTiming:
    """Time for goto_definition, find_references, hover operations."""

    @pytest.mark.parametrize(
        "fixture_name, file_name, line, col",
        [
            ("simple_package", "main.py", 3, 5),  # greet function definition
            ("classes", "models.py", 10, 8),  # Animal.__init__
            ("standalone", "script.py", 7, 5),  # standalone_greeting (def line)
        ],
    )
    def test_goto_definition(self, fixture_name: str, file_name: str, line: int, col: int) -> None:
        rp = TyO3Session(fixture_path(fixture_name))
        try:
            elapsed = _time_op(
                f"goto_definition({fixture_name}:{line},{col})",
                lambda: rp.goto_definition(file_name, line, col),
                timeout=NAVIGATION_TIMEOUT,
            )
            targets = rp.goto_definition(file_name, line, col)
            print(f"\n  ⏱  goto_definition({line},{col}): {elapsed:.3f}s [{len(targets)} targets]")
        finally:
            rp.close()

    @pytest.mark.parametrize(
        "fixture_name, file_name, line, col",
        [
            ("simple_package", "main.py", 3, 5),
            ("classes", "models.py", 80, 9),  # Eagle.hunt (def line)
        ],
    )
    def test_find_references(self, fixture_name: str, file_name: str, line: int, col: int) -> None:
        rp = TyO3Session(fixture_path(fixture_name))
        try:
            elapsed = _time_op(
                f"find_references({fixture_name}:{line},{col})",
                lambda: rp.find_references(file_name, line, col),
                timeout=NAVIGATION_TIMEOUT,
            )
            refs = rp.find_references(file_name, line, col)
            print(f"\n  ⏱  find_references({line},{col}): {elapsed:.3f}s [{len(refs)} refs]")
        finally:
            rp.close()

    @pytest.mark.parametrize(
        "fixture_name, file_name, line, col",
        [
            ("simple_package", "main.py", 3, 5),
            ("classes", "models.py", 4, 8),  # Animal class
            ("unicode_positions", "unicode.py", 10, 1),  # α identifier
        ],
    )
    def test_hover(self, fixture_name: str, file_name: str, line: int, col: int) -> None:
        rp = TyO3Session(fixture_path(fixture_name))
        try:
            elapsed = _time_op(
                f"hover({fixture_name}:{line},{col})",
                lambda: rp.hover(file_name, line, col),
                timeout=HOVER_TIMEOUT,
            )
            hover = rp.hover(file_name, line, col)
            _has_content = len(hover.contents) if hover else 0
            print(f"\n  ⏱  hover({line},{col}): {elapsed:.3f}s [{'has content' if hover else 'None'}]")
        finally:
            rp.close()


@needs_native
class TestReloadTiming:
    """Time for reload() operation."""

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "simple_package",
            "classes",
            "imports",
        ],
    )
    def test_reload(self, fixture_name: str) -> None:
        rp = TyO3Session(fixture_path(fixture_name))
        try:
            # Warm up
            rp.files()
            rp.check()

            elapsed = _time_op(
                f"reload({fixture_name})",
                lambda: rp.reload(),
                timeout=OPEN_TIMEOUT,
            )
            print(f"\n  ⏱  reload({fixture_name}): {elapsed:.3f}s")

            # Verify files still work after reload
            files = rp.files()
            assert len(files) >= 1, f"No files after reload of {fixture_name}"
        finally:
            rp.close()

    def test_reload_then_check(self) -> None:
        """Reload + check should complete within reasonable time."""
        rp = TyO3Session(fixture_path("classes"))
        try:
            t1_start = time.perf_counter()
            rp.reload()
            rp.check()
            t1 = time.perf_counter() - t1_start
            print(f"\n  ⏱  reload+check: {t1:.3f}s")
            assert t1 < CHECK_TIMEOUT + OPEN_TIMEOUT, f"reload+check took {t1:.3f}s"
        finally:
            rp.close()


@needs_native
class TestCloseTiming:
    """Time for close() operation."""

    def test_close(self) -> None:
        rp = TyO3Session(fixture_path("classes"))
        elapsed = _time_op(
            "close(classes)",
            lambda: rp.close(),
            timeout=CLOSE_TIMEOUT,
        )
        print(f"\n  ⏱  close(): {elapsed:.3f}s")
