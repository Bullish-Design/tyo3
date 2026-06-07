"""Property-based tests for TyO3 coordinate conversion and symbol invariants.

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
    from tyo3 import TyO3Session

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


# ── Shared cache (session-scoped, via conftest) ──────────────────────────


def get_project(fixture_name: str) -> TyO3Session:
    from tyo3.tests.conftest import shared_project

    return shared_project(fixture_name)


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


def _path_in_fixture(rp: TyO3Session, filename: str) -> bool:
    """Check if *filename* (basename) exists in the project."""
    try:
        files = rp.files()
        return any(f.name == filename for f in files)
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
    rp = get_project(fixture_name)
    try:
        hover = rp.hover(path, line, column)
        if hover is not None:
            assert isinstance(hover.contents, list)
            assert hover.location.range.start.line >= 1
            assert hover.location.range.start.column >= 1
    except (PositionError, PathResolutionError, OverflowError):
        pass


@needs_native
@settings(max_examples=100, deadline=None)
@given(pair=fixture_file_strategy, line=line_numbers, column=column_numbers)
def test_goto_never_panics(pair, line: int, column: int) -> None:
    """goto_definition() with arbitrary inputs should never panic."""
    fixture_name, path = pair
    rp = get_project(fixture_name)
    try:
        targets = rp.goto_definition(path, line, column)
        assert isinstance(targets, list)
        for t in targets:
            assert t.path is not None
            assert t.range.start.line >= 1
            assert t.range.start.column >= 1
    except (PositionError, PathResolutionError, OverflowError):
        pass


@needs_native
@settings(max_examples=100, deadline=None)
@given(pair=fixture_file_strategy, line=line_numbers, column=column_numbers)
def test_find_references_never_panics(pair, line: int, column: int) -> None:
    """find_references() with arbitrary inputs should never panic."""
    fixture_name, path = pair
    rp = get_project(fixture_name)
    try:
        refs = rp.find_references(path, line, column)
        assert isinstance(refs, list)
    except (PositionError, PathResolutionError, OverflowError):
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
    rp = get_project(fixture_name)
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


@needs_native
@settings(max_examples=30, deadline=None)
@given(data=st.data())
def test_symbols_are_deterministic(data: st.DataObject) -> None:
    """document_symbols() returns identical results on repeated calls."""
    fixture_name = data.draw(st.sampled_from(NON_EMPTY_FIXTURES))
    rp = get_project(fixture_name)
    files = rp.files()
    assume(len(files) > 0)
    file_path = data.draw(st.sampled_from(files))
    file_name = StdPath(file_path).name
    assume(file_name.endswith(".py"))

    s1 = rp.document_symbols(file_name)
    s2 = rp.document_symbols(file_name)

    assert len(s1) == len(s2), f"Count mismatch: {len(s1)} vs {len(s2)}"
    for a, b in zip(s1, s2, strict=True):
        assert a.name == b.name
        assert a.kind == b.kind
        assert a.location.range.start.line == b.location.range.start.line


# ═══════════════════════════════════════════════════════════════════════════
# Property 3: Closed-project invariant
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
@settings(max_examples=15, deadline=None)
@given(fixture_name=st.sampled_from(NON_EMPTY_FIXTURES))
def test_all_operations_raise_after_close(fixture_name: str) -> None:
    """No operation should succeed after close()."""
    rp = TyO3Session(fixture_path(fixture_name))
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

    for _name, op in ops:
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
    rp = get_project("empty")
    assert rp.files() == []
    with pytest.raises((PathResolutionError, PositionError)):
        rp.document_symbols("nonexistent.py")
    with pytest.raises((PathResolutionError, PositionError)):
        rp.goto_definition("nonexistent.py", line, column)
    result = rp.check()
    assert result.diagnostics == []


# ═══════════════════════════════════════════════════════════════════════════
# Property 5: Files are unique and absolute
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
@settings(max_examples=20, deadline=None)
@given(fixture_name=st.sampled_from(NON_EMPTY_FIXTURES))
def test_files_are_unique_and_absolute(fixture_name: str) -> None:
    """files() returns unique, absolute paths that exist on disk."""
    rp = get_project(fixture_name)
    files = rp.files()
    assert len(files) == len(set(files)), "Duplicate file paths"
    for f in files:
        assert str(f).startswith("/"), f"Non-absolute: {f}"
        assert StdPath(f).exists(), f"Missing on disk: {f}"


# ═══════════════════════════════════════════════════════════════════════════
# Gate 7 property: Diff parity between live snapshots and time-travel rebuilds
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
@settings(max_examples=15, deadline=None)
@given(
    num_edits=st.integers(min_value=1, max_value=6),
    seed=st.integers(min_value=0, max_value=9999),
)
def test_diff_parity_live_vs_time_travel(num_edits: int, seed: int) -> None:
    """Gate 7 §10.3 code-diff property: a.code.diff(b.code) from live
    snapshots equals the diff from time-travel rebuilds at the same R0/R1.

    Code-only (no derived/authored layers) for speed; full-layer parity
    is tested in test_gate7_read_surface.py::test_diff_parity_all_layers.
    """
    import random
    import tempfile
    import shutil
    from tyo3 import TyO3Session

    rng = random.Random(seed)

    tmp_root = StdPath(tempfile.mkdtemp(prefix="tyo3_prop_"))
    try:
        proj = tmp_root / "proj"
        proj.mkdir(parents=True)
        (proj / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
        (proj / "a.py").write_text(
            "def foo():\n    return 10\n"
            "def bar():\n    return 20\n"
        )

        with TyO3Session(str(proj)) as session:
            session.sync_all()
            foo_id = session.id_for("a.py", 1, 5)
            assume(foo_id is not None)

            snap_r0 = session.snapshot()
            r0 = snap_r0.revision

            for i in range(num_edits):
                val = rng.randint(1, 999)
                session.edit(
                    "a.py",
                    f"def foo():\n    return {val}\ndef bar():\n    return 20\n",
                )

            snap_r1 = session.snapshot()
            r1 = snap_r1.revision

            d_live = snap_r1.diff(snap_r0)

            snap_r0_tt = session.snapshot(at=r0)
            snap_r1_tt = session.snapshot(at=r1)
            d_tt = snap_r1_tt.diff(snap_r0_tt)

            assert d_live.code.changed == d_tt.code.changed, (
                f"code.changed mismatch: live={d_live.code.changed}, tt={d_tt.code.changed}"
            )
            assert d_live.code.added == d_tt.code.added
            assert d_live.code.removed == d_tt.code.removed
            assert d_live.code.moved == d_tt.code.moved
            assert d_live.entities() == d_tt.entities()

            snap_r0.close()
            snap_r1.close()
            snap_r0_tt.close()
            snap_r1_tt.close()
    finally:
        shutil.rmtree(str(tmp_root), ignore_errors=True)
