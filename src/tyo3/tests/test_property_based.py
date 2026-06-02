"""Property-based tests for TyO3 coordinate conversion and symbol invariants.

Tests for Phase 5 of RUST_BACKEND_IMPLEMENTATION.md §8.

Uses Hypothesis to generate random positions, paths, and fixture combinations
to verify:

1. **No-crash invariant**: the Rust backend should never panic given any
   (path, line, column) combination — it should return a well-defined error.
2. **Symbol invariants**: every symbol has a valid name, kind, and 1-based location.
3. **Closed-project invariant**: no operation succeeds after close().
4. **File properties**: files() returns unique, absolute paths.

Run with: PYTHONPATH=src pytest src/tyo3/tests/test_property_based.py -v
"""

from __future__ import annotations

from pathlib import Path as StdPath

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

# Check if native extension is available
try:
    from tyo3.rust_project import RustProject

    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False

from tyo3.exceptions import PathResolutionError, PositionError, ProjectClosedError

# ── Path helpers ──────────────────────────────────────────────────────────

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"

ALL_FIXTURES = [
    "simple_package",
    "imports",
    "classes",
    "diagnostic_targets",
    "unicode_positions",
    "standalone",
    "empty",
]

NON_EMPTY_FIXTURES = [f for f in ALL_FIXTURES if f != "empty"]


def fixture_path(name: str) -> str:
    """Return the absolute path to a fixture directory."""
    return str((FIXTURES_DIR / name).resolve())


# ── Skip marker ───────────────────────────────────────────────────────────

needs_native = pytest.mark.skipif(not _HAS_NATIVE, reason="Rust native extension not built")

# ── Hypothesis strategies ─────────────────────────────────────────────────

# 1-based line numbers (generous range for small fixtures)
line_numbers = st.integers(min_value=1, max_value=200)

# 1-based column numbers
column_numbers = st.integers(min_value=1, max_value=100)

# Fixture names
fixture_names = st.sampled_from(ALL_FIXTURES)

# Common file names found across fixtures
common_paths = st.sampled_from(
    [
        "main.py",
        "models.py",
        "script.py",
        "math_ops.py",
        "unicode.py",
        "errors.py",
    ]
)

# ═══════════════════════════════════════════════════════════════════════════
# Helper: open a fixture, run checks, close
# ═══════════════════════════════════════════════════════════════════════════


def _path_in_fixture(rp: RustProject, filename: str) -> bool:
    """Check if *filename* (basename) exists in the project."""
    try:
        files = rp.files()
        return any(StdPath(f).name == filename for f in files)
    except Exception:
        return False


# ── Valid (fixture, filename) pairs ───────────────────────────────────
# Instead of generating random combos and filtering, pre-define valid pairs.

FIXTURE_FILE_PAIRS = [
    ("simple_package", "main.py"),
    ("classes", "models.py"),
    ("classes", "__init__.py"),
    ("imports", "main.py"),
    ("imports", "math_ops.py"),
    ("standalone", "script.py"),
    ("unicode_positions", "unicode.py"),
    ("diagnostic_targets", "errors.py"),
]

fixture_file_strategy = st.sampled_from(FIXTURE_FILE_PAIRS)

# ═══════════════════════════════════════════════════════════════════════════
# Property 1: Coordinate conversion never panics
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
@settings(max_examples=100, deadline=None)
@given(pair=fixture_file_strategy, line=line_numbers, column=column_numbers)
def test_hover_never_panics(pair, line: int, column: int) -> None:
    """hover() with arbitrary inputs should never panic."""
    fixture_name, path = pair
    rp = RustProject(fixture_path(fixture_name))
    try:
        try:
            hover = rp.hover(path, line, column)
            if hover is not None:
                assert isinstance(hover.contents, list)
                assert hover.location.range.start.line >= 1
                assert hover.location.range.start.column >= 1
        except (PositionError, PathResolutionError, OverflowError):
            pass
    finally:
        try:
            rp.close()
        except Exception:
            pass


@needs_native
@settings(max_examples=100, deadline=None)
@given(pair=fixture_file_strategy, line=line_numbers, column=column_numbers)
def test_goto_never_panics(pair, line: int, column: int) -> None:
    """goto_definition() with arbitrary inputs should never panic."""
    fixture_name, path = pair
    rp = RustProject(fixture_path(fixture_name))
    try:
        try:
            targets = rp.goto_definition(path, line, column)
            assert isinstance(targets, list)
            for t in targets:
                assert t.path is not None
                assert t.range.start.line >= 1
                assert t.range.start.column >= 1
        except (PositionError, PathResolutionError, OverflowError):
            pass
    finally:
        try:
            rp.close()
        except Exception:
            pass


@needs_native
@settings(max_examples=100, deadline=None)
@given(pair=fixture_file_strategy, line=line_numbers, column=column_numbers)
def test_find_references_never_panics(pair, line: int, column: int) -> None:
    """find_references() with arbitrary inputs should never panic."""
    fixture_name, path = pair
    rp = RustProject(fixture_path(fixture_name))
    try:
        try:
            refs = rp.find_references(path, line, column)
            assert isinstance(refs, list)
        except (PositionError, PathResolutionError, OverflowError):
            pass
    finally:
        try:
            rp.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# Property 2: Document symbols are well-formed
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
@settings(max_examples=30, deadline=None)
@given(data=st.data())
def test_symbols_have_valid_positions(data: st.DataObject) -> None:
    """Every symbol must have a name, kind, and valid 1-based position."""
    fixture_name = data.draw(st.sampled_from(NON_EMPTY_FIXTURES))
    rp = RustProject(fixture_path(fixture_name))
    try:
        files = rp.files()
        assume(len(files) > 0)
        file_path = data.draw(st.sampled_from(files))
        file_name = StdPath(file_path).name
        assume(file_name.endswith(".py"))

        symbols = rp.document_symbols(file_name)
        for s in symbols:
            assert s.name, "Symbol with empty name"
            assert s.kind, f"Symbol {s.name} has no kind"

            loc = s.location
            assert loc.range.start.line >= 1, f"{s.name}: start line < 1"
            assert loc.range.start.column >= 1, f"{s.name}: start column < 1"
            assert loc.range.end.line >= 1, f"{s.name}: end line < 1"

            if s.selection_range:
                assert s.selection_range.start.line >= 1
                assert s.selection_range.start.column >= 1
    finally:
        try:
            rp.close()
        except Exception:
            pass


@needs_native
@settings(max_examples=30, deadline=None)
@given(data=st.data())
def test_symbols_are_deterministic(data: st.DataObject) -> None:
    """document_symbols() returns identical results on repeated calls."""
    fixture_name = data.draw(st.sampled_from(NON_EMPTY_FIXTURES))
    rp = RustProject(fixture_path(fixture_name))
    try:
        files = rp.files()
        assume(len(files) > 0)
        file_path = data.draw(st.sampled_from(files))
        file_name = StdPath(file_path).name
        assume(file_name.endswith(".py"))

        s1 = rp.document_symbols(file_name)
        s2 = rp.document_symbols(file_name)

        assert len(s1) == len(s2), f"Count mismatch: {len(s1)} vs {len(s2)}"
        for a, b in zip(s1, s2):
            assert a.name == b.name
            assert a.kind == b.kind
            assert a.location.range.start.line == b.location.range.start.line
    finally:
        try:
            rp.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# Property 3: Closed-project invariant
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
@settings(max_examples=15, deadline=None)
@given(fixture_name=st.sampled_from(NON_EMPTY_FIXTURES))
def test_all_operations_raise_after_close(fixture_name: str) -> None:
    """No operation should succeed after close()."""
    rp = RustProject(fixture_path(fixture_name))
    rp.close()

    ops = [
        ("files", lambda: rp.files()),
        ("check", lambda: rp.check()),
        ("document_symbols", lambda: rp.document_symbols("main.py")),
        ("workspace_symbols", lambda: rp.workspace_symbols("x")),
        ("goto_definition", lambda: rp.goto_definition("main.py", 1, 1)),
        ("find_references", lambda: rp.find_references("main.py", 1, 1)),
        ("hover", lambda: rp.hover("main.py", 1, 1)),
        ("reload", lambda: rp.reload()),
    ]

    for name, op in ops:
        with pytest.raises(ProjectClosedError):
            op()


# ═══════════════════════════════════════════════════════════════════════════
# Property 4: Empty project invariants
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
@settings(max_examples=10, deadline=None)
@given(line=line_numbers, column=column_numbers)
def test_empty_project_operations(line: int, column: int) -> None:
    """Empty project: files() empty, path ops raise clean errors."""
    rp = RustProject(fixture_path("empty"))
    try:
        assert rp.files() == []
        with pytest.raises((PathResolutionError, PositionError)):
            rp.document_symbols("nonexistent.py")
        with pytest.raises((PathResolutionError, PositionError)):
            rp.goto_definition("nonexistent.py", line, column)
        result = rp.check()
        assert result.diagnostics == []
    finally:
        rp.close()


# ═══════════════════════════════════════════════════════════════════════════
# Property 5: Files are unique and absolute
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
@settings(max_examples=20, deadline=None)
@given(fixture_name=st.sampled_from(NON_EMPTY_FIXTURES))
def test_files_are_unique_and_absolute(fixture_name: str) -> None:
    """files() returns unique, absolute paths that exist on disk."""
    rp = RustProject(fixture_path(fixture_name))
    try:
        files = rp.files()
        assert len(files) == len(set(files)), "Duplicate file paths"
        for f in files:
            assert f.startswith("/"), f"Non-absolute: {f}"
            assert StdPath(f).exists(), f"Missing on disk: {f}"
    finally:
        rp.close()
