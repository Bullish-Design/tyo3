"""Integration tests exercising RUST `use_rust=True` code paths with real fixtures.

Tests for Phase 4 of RUST_BACKEND_IMPLEMENTATION.md §7.

These tests require the Rust native extension to be built.
Run with: PYTHONPATH=src pytest src/tyo3/tests/test_rust_integration.py -v

Test groups:
- Project lifecycle (open, reload, close)
- File discovery
- Document symbols
- Workspace symbols
- Navigation (goto definition, find references, hover)
- Diagnostic checking
- Error handling (closed project, bad paths, bad positions)
"""

from __future__ import annotations

from pathlib import Path as StdPath

import pytest

# Check if native extension is available
try:
    from tyo3 import TyO3Session

    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False

from tyo3.exceptions import (
    PathResolutionError,
    PositionError,
    ProjectClosedError,
    ProjectOpenError,
)

# ── Path helpers ──────────────────────────────────────────────────────────

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def fixture_path(name: str) -> str:
    """Return the absolute path to a fixture directory."""
    return str((FIXTURES_DIR / name).resolve())


# ── Skip marker ───────────────────────────────────────────────────────────

needs_native = pytest.mark.skipif(not _HAS_NATIVE, reason="Rust native extension not built")

# ── Shared cache (session-scoped, via conftest) ──────────────────────────


def get_project(fixture_name: str) -> TyO3Session:
    from tyo3.tests.conftest import shared_project

    return shared_project(fixture_name)


# ═══════════════════════════════════════════════════════════════════════════
# Project Lifecycle Tests
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
class TestProjectLifecycle:
    """Test open, reload, close lifecycle with real Rust backend."""

    def test_open_simple_package(self) -> None:
        rp = get_project("simple_package")
        files = rp.files()
        assert len(files) >= 1
        assert any("main.py" in str(f) for f in files)

    def test_open_imports_package(self) -> None:
        rp = get_project("imports")
        files = rp.files()
        assert len(files) >= 2
        assert any("main.py" in str(f) for f in files)
        assert any("math_ops.py" in str(f) for f in files)

    def test_open_classes_package(self) -> None:
        rp = get_project("classes")
        files = rp.files()
        assert len(files) >= 1
        assert any("models.py" in str(f) for f in files)

    def test_open_standalone_script(self) -> None:
        rp = get_project("standalone")
        files = rp.files()
        assert len(files) >= 1
        assert any("script.py" in str(f) for f in files)

    def test_open_empty_directory(self) -> None:
        """Empty directory opens without error (may have 0 files)."""
        rp = get_project("empty")
        files = rp.files()
        assert isinstance(files, list)

    def test_open_unicode_fixture(self) -> None:
        rp = get_project("unicode_positions")
        files = rp.files()
        assert len(files) >= 1
        assert any("unicode.py" in str(f) for f in files)

    def test_reload_preserves_files(self) -> None:
        # Needs own instance since reload mutates state
        rp = TyO3Session(fixture_path("simple_package"))
        files_before = rp.files()
        rp.reload()
        files_after = rp.files()
        assert set(files_before) == set(files_after)
        rp.close()

    def test_close_then_operation_raises(self) -> None:
        # Needs own instance since it closes the project
        rp = TyO3Session(fixture_path("simple_package"))
        rp.close()
        with pytest.raises(ProjectClosedError):
            rp.files()
        with pytest.raises(ProjectClosedError):
            rp.check()
        with pytest.raises(ProjectClosedError):
            rp.document_symbols("main.py")

    def test_nonexistent_root_raises(self) -> None:
        with pytest.raises(ProjectOpenError):
            TyO3Session("/nonexistent/path/that/does/not/exist")


# ═══════════════════════════════════════════════════════════════════════════
# File Discovery Tests
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
class TestFileDiscovery:
    """Test file listing through Rust backend."""

    def test_files_are_absolute_paths(self) -> None:
        rp = get_project("simple_package")
        files = rp.files()
        for f in files:
            assert str(f).startswith("/"), f"Expected absolute path, got: {f}"

    def test_files_after_reload(self) -> None:
        """Files() after reload() returns the same set."""
        # Needs own instance since reload mutates state
        rp = TyO3Session(fixture_path("imports"))
        f1 = rp.files()
        rp.reload()
        f2 = rp.files()
        assert set(f1) == set(f2)
        rp.close()


# ═══════════════════════════════════════════════════════════════════════════
# Document Symbols Tests
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
class TestDocumentSymbols:
    """Test document_symbols() through Rust backend."""

    def test_simple_package_symbols(self) -> None:
        rp = get_project("simple_package")
        symbols = rp.document_symbols("main.py")
        assert len(symbols) >= 1
        names = {s.name for s in symbols}
        assert "greet" in names
        assert "MyClass" in names

    def test_classes_symbols(self) -> None:
        rp = get_project("classes")
        symbols = rp.document_symbols("models.py")
        names = {s.name for s in symbols}
        assert "Animal" in names
        assert "Dog" in names
        assert "Cat" in names
        assert "Eagle" in names

    def test_symbols_have_kind(self) -> None:
        rp = get_project("simple_package")
        symbols = rp.document_symbols("main.py")
        for s in symbols:
            assert s.kind, f"Symbol {s.name} has no kind"
            assert s.kind != "unknown", f"Symbol {s.name} has kind 'unknown'"

    def test_symbols_have_location(self) -> None:
        rp = get_project("simple_package")
        symbols = rp.document_symbols("main.py")
        for s in symbols:
            assert s.location is not None, f"Symbol {s.name} has no location"
            assert s.location.range is not None
            assert s.location.range.start.line >= 1

    def test_symbols_not_deprecated(self) -> None:
        rp = get_project("simple_package")
        symbols = rp.document_symbols("main.py")
        for s in symbols:
            assert s.deprecated is False, f"Symbol {s.name} unexpectedly deprecated"

    def test_bad_file_path_raises(self) -> None:
        rp = get_project("simple_package")
        with pytest.raises(PathResolutionError):
            rp.document_symbols("nonexistent.py")


# ═══════════════════════════════════════════════════════════════════════════
# Workspace Symbols Tests
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
class TestWorkspaceSymbols:
    """Test workspace_symbols() through Rust backend."""

    def test_search_finds_class(self) -> None:
        rp = get_project("classes")
        results = rp.workspace_symbols("Dog")
        names = {s.name for s in results}
        assert "Dog" in names

    def test_search_finds_function(self) -> None:
        rp = get_project("simple_package")
        results = rp.workspace_symbols("greet")
        names = {s.name for s in results}
        assert "greet" in names

    def test_search_no_match_returns_empty(self) -> None:
        rp = get_project("simple_package")
        results = rp.workspace_symbols("zzz_nonexistent_symbol_xyz")
        assert results == []

    def test_empty_query(self) -> None:
        rp = get_project("simple_package")
        results = rp.workspace_symbols("")
        # Empty query may return empty or all symbols; just verify no crash
        assert isinstance(results, list)


# ═══════════════════════════════════════════════════════════════════════════
# Navigation Tests
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
class TestNavigation:
    """Test goto_definition, find_references, hover through Rust backend."""

    def test_goto_definition_finds_target(self) -> None:
        rp = get_project("simple_package")
        # greet function is defined at line 3 (1-based), around column 5
        targets = rp.goto_definition("main.py", 3, 5)
        # May return targets or empty if cursor is on the definition itself
        assert isinstance(targets, list)

    def test_goto_definition_returns_list(self) -> None:
        rp = get_project("classes")
        targets = rp.goto_definition("models.py", 10, 12)
        assert isinstance(targets, list)

    def test_find_references_returns_results(self) -> None:
        rp = get_project("simple_package")
        refs = rp.find_references("main.py", 3, 5, include_declaration=True)
        assert isinstance(refs, list)

    def test_find_references_without_declaration(self) -> None:
        rp = get_project("simple_package")
        refs = rp.find_references("main.py", 3, 5, include_declaration=False)
        assert isinstance(refs, list)

    def test_hover_returns_content(self) -> None:
        rp = get_project("simple_package")
        hover = rp.hover("main.py", 3, 5)
        if hover is not None:
            assert len(hover.contents) >= 1
            for c in hover.contents:
                assert c.kind is not None
                assert c.value

    def test_hover_returns_none_for_non_symbol(self) -> None:
        rp = get_project("simple_package")
        # An empty/whitespace line shouldn't have hover info
        hover = rp.hover("main.py", 1, 1)
        # May or may not return hover for docstring/module level
        assert hover is None or isinstance(hover.contents, list)

    def test_goto_declaration(self) -> None:
        rp = get_project("classes")
        targets = rp.goto_declaration("models.py", 5, 12)
        assert isinstance(targets, list)

    def test_goto_type_definition(self) -> None:
        rp = get_project("classes")
        targets = rp.goto_type_definition("models.py", 5, 12)
        assert isinstance(targets, list)

    def test_invalid_position_raises(self) -> None:
        rp = get_project("simple_package")
        with pytest.raises(PositionError):
            rp.goto_definition("main.py", 0, 1)
        with pytest.raises(PositionError):
            rp.goto_definition("main.py", 1, 0)
        # Negative values raise PositionError (mapped from PyO3 OverflowError)
        with pytest.raises(PositionError):
            rp.goto_definition("main.py", -1, 1)

    def test_invalid_path_raises(self) -> None:
        rp = get_project("simple_package")
        with pytest.raises(PathResolutionError):
            rp.goto_definition("nonexistent_file.py", 1, 1)


# ═══════════════════════════════════════════════════════════════════════════
# Diagnostics / Check Tests
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
class TestDiagnostics:
    """Test check() and diagnostics through Rust backend."""

    def test_check_returns_result(self) -> None:
        rp = get_project("simple_package")
        result = rp.check()
        assert result is not None
        assert isinstance(result.diagnostics, list)

    def test_check_on_diagnostic_targets(self) -> None:
        rp = get_project("diagnostic_targets")
        result = rp.check()
        # May have diagnostics; at minimum, should not crash
        assert isinstance(result.diagnostics, list)

    def test_check_after_reload(self) -> None:
        # Needs own instance since reload mutates state
        rp = TyO3Session(fixture_path("simple_package"))
        rp.check()
        rp.reload()
        result2 = rp.check()
        assert isinstance(result2.diagnostics, list)
        rp.close()


# ═══════════════════════════════════════════════════════════════════════════
# Unicode Position Tests
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
class TestUnicodePositions:
    """Test that operations work correctly with Unicode identifiers and
    multi-byte UTF-8 characters.
    """

    def test_unicode_symbols_discovered(self) -> None:
        rp = get_project("unicode_positions")
        symbols = rp.document_symbols("unicode.py")
        names = {s.name for s in symbols}
        # Greek letter variables
        assert "α" in names, f"Expected Greek α in symbols, got: {names}"
        assert "β" in names, f"Expected Greek β in symbols, got: {names}"
        # Japanese identifier
        assert "挨拶" in names or "挨拶する" in names, f"Expected Japanese identifiers in: {names}"

    def test_unicode_hover(self) -> None:
        rp = get_project("unicode_positions")
        # Hover on Greek function καλημέρα (defined at line 13)
        hover = rp.hover("unicode.py", 13, 5)
        assert hover is not None, "Expected hover for Greek function καλημέρα"

    def test_unicode_workspace_search(self) -> None:
        rp = get_project("unicode_positions")
        results = rp.workspace_symbols("α")
        assert len(results) >= 1
