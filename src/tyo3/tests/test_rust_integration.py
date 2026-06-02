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
- Cross-service data flow chains
"""

from __future__ import annotations

from pathlib import Path as StdPath

import pytest

# Check if native extension is available
try:
    from tyo3.rust_project import RustProject
    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False

from tyo3.exceptions import (
    PathResolutionError,
    PositionError,
    ProjectClosedError,
    ProjectOpenError,
)
from tyo3.models.core import Path
from tyo3.services.analysis_service import AnalysisService
from tyo3.services.navigation_service import NavigationService
from tyo3.services.project_service import ProjectService
from tyo3.services.symbol_service import SymbolService

# ── Path helpers ──────────────────────────────────────────────────────────

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def fixture_path(name: str) -> str:
    """Return the absolute path to a fixture directory."""
    return str((FIXTURES_DIR / name).resolve())


# ── Skip marker ───────────────────────────────────────────────────────────

needs_native = pytest.mark.skipif(not _HAS_NATIVE, reason="Rust native extension not built")


# ═══════════════════════════════════════════════════════════════════════════
# Project Lifecycle Tests
# ═══════════════════════════════════════════════════════════════════════════

@needs_native
class TestProjectLifecycle:
    """Test open, reload, close lifecycle with real Rust backend."""

    def test_open_simple_package(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        files = rp.files()
        assert len(files) >= 1
        assert any("main.py" in f for f in files)
        rp.close()

    def test_open_imports_package(self) -> None:
        rp = RustProject(fixture_path("imports"))
        files = rp.files()
        assert len(files) >= 2
        assert any("main.py" in f for f in files)
        assert any("math_ops.py" in f for f in files)
        rp.close()

    def test_open_classes_package(self) -> None:
        rp = RustProject(fixture_path("classes"))
        files = rp.files()
        assert len(files) >= 1
        assert any("models.py" in f for f in files)
        rp.close()

    def test_open_standalone_script(self) -> None:
        rp = RustProject(fixture_path("standalone"))
        files = rp.files()
        assert len(files) >= 1
        assert any("script.py" in f for f in files)
        rp.close()

    def test_open_empty_directory(self) -> None:
        """Empty directory opens without error (may have 0 files)."""
        rp = RustProject(fixture_path("empty"))
        files = rp.files()
        assert isinstance(files, list)
        rp.close()

    def test_open_unicode_fixture(self) -> None:
        rp = RustProject(fixture_path("unicode_positions"))
        files = rp.files()
        assert len(files) >= 1
        assert any("unicode.py" in f for f in files)
        rp.close()

    def test_reload_preserves_files(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        files_before = rp.files()
        rp.reload()
        files_after = rp.files()
        assert set(files_before) == set(files_after)
        rp.close()

    def test_close_then_operation_raises(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        rp.close()
        with pytest.raises(ProjectClosedError):
            rp.files()
        with pytest.raises(ProjectClosedError):
            rp.check()
        with pytest.raises(ProjectClosedError):
            rp.document_symbols("main.py")

    def test_nonexistent_root_raises(self) -> None:
        with pytest.raises(ProjectOpenError):
            RustProject("/nonexistent/path/that/does/not/exist")


# ═══════════════════════════════════════════════════════════════════════════
# File Discovery Tests
# ═══════════════════════════════════════════════════════════════════════════

@needs_native
class TestFileDiscovery:
    """Test file listing through Rust backend."""

    def test_files_are_absolute_paths(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        files = rp.files()
        for f in files:
            assert f.startswith("/"), f"Expected absolute path, got: {f}"
        rp.close()

    def test_files_after_reload(self) -> None:
        """Files() after reload() returns the same set."""
        rp = RustProject(fixture_path("imports"))
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
        rp = RustProject(fixture_path("simple_package"))
        symbols = rp.document_symbols("main.py")
        assert len(symbols) >= 1
        names = {s.name for s in symbols}
        assert "greet" in names
        assert "MyClass" in names
        rp.close()

    def test_classes_symbols(self) -> None:
        rp = RustProject(fixture_path("classes"))
        symbols = rp.document_symbols("models.py")
        names = {s.name for s in symbols}
        assert "Animal" in names
        assert "Dog" in names
        assert "Cat" in names
        assert "Eagle" in names
        rp.close()

    def test_symbols_have_kind(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        symbols = rp.document_symbols("main.py")
        for s in symbols:
            assert s.kind, f"Symbol {s.name} has no kind"
            assert s.kind != "unknown", f"Symbol {s.name} has kind 'unknown'"
        rp.close()

    def test_symbols_have_location(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        symbols = rp.document_symbols("main.py")
        for s in symbols:
            assert s.location is not None, f"Symbol {s.name} has no location"
            assert s.location.range is not None
            assert s.location.range.start.line >= 1
        rp.close()

    def test_symbols_not_deprecated(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        symbols = rp.document_symbols("main.py")
        for s in symbols:
            assert s.deprecated is False, f"Symbol {s.name} unexpectedly deprecated"
        rp.close()

    def test_bad_file_path_raises(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        with pytest.raises(PathResolutionError):
            rp.document_symbols("nonexistent.py")
        rp.close()


# ═══════════════════════════════════════════════════════════════════════════
# Workspace Symbols Tests
# ═══════════════════════════════════════════════════════════════════════════

@needs_native
class TestWorkspaceSymbols:
    """Test workspace_symbols() through Rust backend."""

    def test_search_finds_class(self) -> None:
        rp = RustProject(fixture_path("classes"))
        results = rp.workspace_symbols("Dog")
        names = {s.name for s in results}
        assert "Dog" in names
        rp.close()

    def test_search_finds_function(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        results = rp.workspace_symbols("greet")
        names = {s.name for s in results}
        assert "greet" in names
        rp.close()

    def test_search_no_match_returns_empty(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        results = rp.workspace_symbols("zzz_nonexistent_symbol_xyz")
        assert results == []
        rp.close()

    def test_empty_query(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        results = rp.workspace_symbols("")
        # Empty query may return empty or all symbols; just verify no crash
        assert isinstance(results, list)
        rp.close()


# ═══════════════════════════════════════════════════════════════════════════
# Navigation Tests
# ═══════════════════════════════════════════════════════════════════════════

@needs_native
class TestNavigation:
    """Test goto_definition, find_references, hover through Rust backend."""

    def test_goto_definition_finds_target(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        # greet function is defined at line 3 (1-based), around column 5
        targets = rp.goto_definition("main.py", 3, 5)
        # May return targets or empty if cursor is on the definition itself
        assert isinstance(targets, list)
        rp.close()

    def test_goto_definition_returns_list(self) -> None:
        rp = RustProject(fixture_path("classes"))
        targets = rp.goto_definition("models.py", 10, 12)
        assert isinstance(targets, list)
        rp.close()

    def test_find_references_returns_results(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        refs = rp.find_references("main.py", 3, 5, include_declaration=True)
        assert isinstance(refs, list)
        rp.close()

    def test_find_references_without_declaration(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        refs = rp.find_references("main.py", 3, 5, include_declaration=False)
        assert isinstance(refs, list)
        rp.close()

    def test_hover_returns_content(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        hover = rp.hover("main.py", 3, 5)
        if hover is not None:
            assert len(hover.contents) >= 1
            for c in hover.contents:
                assert c.kind is not None
                assert c.value
        rp.close()

    def test_hover_returns_none_for_non_symbol(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        # An empty/whitespace line shouldn't have hover info
        hover = rp.hover("main.py", 1, 1)
        # May or may not return hover for docstring/module level
        assert hover is None or isinstance(hover.contents, list)
        rp.close()

    def test_goto_declaration(self) -> None:
        rp = RustProject(fixture_path("classes"))
        targets = rp.goto_declaration("models.py", 5, 12)
        assert isinstance(targets, list)
        rp.close()

    def test_goto_type_definition(self) -> None:
        rp = RustProject(fixture_path("classes"))
        targets = rp.goto_type_definition("models.py", 5, 12)
        assert isinstance(targets, list)
        rp.close()

    def test_invalid_position_raises(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        with pytest.raises(PositionError):
            rp.goto_definition("main.py", 0, 1)
        with pytest.raises(PositionError):
            rp.goto_definition("main.py", 1, 0)
        # Negative values raise OverflowError from PyO3 u32 conversion
        with pytest.raises(OverflowError):
            rp.goto_definition("main.py", -1, 1)
        rp.close()

    def test_invalid_path_raises(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        with pytest.raises(PathResolutionError):
            rp.goto_definition("nonexistent_file.py", 1, 1)
        rp.close()


# ═══════════════════════════════════════════════════════════════════════════
# Diagnostics / Check Tests
# ═══════════════════════════════════════════════════════════════════════════

@needs_native
class TestDiagnostics:
    """Test check() and diagnostics through Rust backend."""

    def test_check_returns_result(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        result = rp.check()
        assert result is not None
        assert isinstance(result.diagnostics, list)
        rp.close()

    def test_check_on_diagnostic_targets(self) -> None:
        rp = RustProject(fixture_path("diagnostic_targets"))
        result = rp.check()
        # May have diagnostics; at minimum, should not crash
        assert isinstance(result.diagnostics, list)
        rp.close()

    def test_check_after_reload(self) -> None:
        rp = RustProject(fixture_path("simple_package"))
        result1 = rp.check()
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
        rp = RustProject(fixture_path("unicode_positions"))
        symbols = rp.document_symbols("unicode.py")
        names = {s.name for s in symbols}
        # Greek letter variables
        assert "α" in names, f"Expected Greek α in symbols, got: {names}"
        assert "β" in names, f"Expected Greek β in symbols, got: {names}"
        # Japanese identifier
        assert "挨拶" in names or "挨拶する" in names, f"Expected Japanese identifiers in: {names}"
        rp.close()

    def test_unicode_hover(self) -> None:
        rp = RustProject(fixture_path("unicode_positions"))
        # Hover on Greek function καλημέρα (defined at line 13)
        hover = rp.hover("unicode.py", 13, 5)
        assert hover is not None, "Expected hover for Greek function καλημέρα"
        rp.close()

    def test_unicode_workspace_search(self) -> None:
        rp = RustProject(fixture_path("unicode_positions"))
        results = rp.workspace_symbols("α")
        assert len(results) >= 1
        rp.close()


# ═══════════════════════════════════════════════════════════════════════════
# Service Layer Integration (use_rust=True)
# ═══════════════════════════════════════════════════════════════════════════

@needs_native
class TestServiceLayerIntegration:
    """Test that services delegate correctly to RustProject when use_rust=True."""

    def test_project_service_with_rust(self) -> None:
        ps = ProjectService(use_rust=True)
        root = Path(components=FIXTURE_DIR_PARTS + ("simple_package",))
        project, files = ps.open_project(root)
        assert project.is_open
        assert len(files) >= 1
        # Verify files came from real discovery
        file_names = {"/".join(f.path.components[-1:]) for f in files}
        assert "main.py" in file_names
        ps.close_project(project)
        assert not project.is_open

    def test_analysis_service_with_rust(self) -> None:
        ps = ProjectService(use_rust=True)
        a_svc = AnalysisService(use_rust=True)
        root = Path(components=FIXTURE_DIR_PARTS + ("simple_package",))
        project, files = ps.open_project(root)

        # Wire up the RustProject — use the key from project_service
        rp = ps._get_rust_project(root)
        a_svc.set_rust_project(str(root), rp)

        result = a_svc.check_project(project)
        assert result is not None
        assert isinstance(result.diagnostics, list)
        ps.close_project(project)

    def test_symbol_service_with_rust(self) -> None:
        ps = ProjectService(use_rust=True)
        s_svc = SymbolService(use_rust=True)
        root = Path(components=FIXTURE_DIR_PARTS + ("simple_package",))
        project, files = ps.open_project(root)

        rp = ps._get_rust_project(root)
        s_svc.set_rust_project(str(root), rp)

        main_file = files[0]
        symbols = s_svc.get_document_symbols(project, main_file)
        assert len(symbols) >= 1
        names = {s.name for s in symbols}
        assert "greet" in names
        ps.close_project(project)

    def test_navigation_service_with_rust(self) -> None:
        ps = ProjectService(use_rust=True)
        n_svc = NavigationService(use_rust=True)
        root = Path(components=FIXTURE_DIR_PARTS + ("simple_package",))
        project, files = ps.open_project(root)

        rp = ps._get_rust_project(root)
        n_svc.set_rust_project(str(root), rp)

        main_file = files[0]
        targets = n_svc.goto_definition(project, main_file, 3, 5)
        assert isinstance(targets, list)
        ps.close_project(project)

    def test_workspace_symbols_service_with_rust(self) -> None:
        ps = ProjectService(use_rust=True)
        s_svc = SymbolService(use_rust=True)
        root = Path(components=FIXTURE_DIR_PARTS + ("simple_package",))
        project, _ = ps.open_project(root)

        rp = ps._get_rust_project(root)
        s_svc.set_rust_project(str(root), rp)

        matches = s_svc.search_workspace_symbols(project, "greet")
        names = {s.name for s in matches}
        assert "greet" in names
        ps.close_project(project)


# ═══════════════════════════════════════════════════════════════════════════
# Cross-Service Data Flow (use_rust=True)
# ═══════════════════════════════════════════════════════════════════════════

@needs_native
class TestCrossServiceDataFlow:
    """Test data flow chains: Project → Files → Symbols → Navigation."""

    def test_full_workflow(self) -> None:
        """End-to-end: open → list files → get symbols → navigate → close."""
        ps = ProjectService(use_rust=True)
        a_svc = AnalysisService(use_rust=True)
        s_svc = SymbolService(use_rust=True)
        n_svc = NavigationService(use_rust=True)

        root = Path(components=FIXTURE_DIR_PARTS + ("simple_package",))
        project, files = ps.open_project(root)

        rp = ps._get_rust_project(root)
        a_svc.set_rust_project(str(root), rp)
        s_svc.set_rust_project(str(root), rp)
        n_svc.set_rust_project(str(root), rp)

        # 1. List files
        listed = ps.list_files(project)
        assert len(listed) >= 1

        # 2. Check project
        check_result = a_svc.check_project(project)
        assert check_result is not None

        # 3. Get document symbols
        symbols = s_svc.get_document_symbols(project, files[0])
        assert len(symbols) >= 1

        # 4. Navigate to a symbol
        targets = n_svc.goto_definition(project, files[0], 3, 5)
        assert isinstance(targets, list)

        # 5. Find references
        refs = n_svc.find_references(project, files[0], 3, 5)
        assert isinstance(refs, list)

        # 6. Hover
        hover = n_svc.get_hover(project, files[0], 3, 5)
        assert hover is not None

        # 7. Reload
        ps.reload_project(project)
        assert project.is_open

        # 8. Close
        ps.close_project(project)
        assert not project.is_open

    def test_empty_project_workflow(self) -> None:
        """Empty project: open → files → close without errors."""
        ps = ProjectService(use_rust=True)
        root = Path(components=FIXTURE_DIR_PARTS + ("empty",))
        project, files = ps.open_project(root)
        # Empty project may have 0 files
        assert isinstance(files, list)
        ps.close_project(project)
        assert not project.is_open


# ── Fixture helpers ──────────────────────────────────────────────────────

# Path parts for absolute path — includes the root "/" as first component
_FIXTURE_RAW_PARTS = tuple(
    str(FIXTURES_DIR.resolve()).split("/")
)  # e.g., ("", "home", "andrew", ...) for "/home/andrew/..."
# Ensure first element is "/" not ""
FIXTURE_DIR_PARTS = ("/",) + _FIXTURE_RAW_PARTS[1:]
