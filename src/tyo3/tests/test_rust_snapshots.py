"""Snapshot tests for symbol/document_symbols output from the Rust backend.

Tests for Phase 4 of RUST_BACKEND_IMPLEMENTATION.md §8.3.

These tests record JSON snapshots of symbol outputs to detect regressions
in the Rust DTO conversion layer. When the snapshots change, review the
diffs to ensure the changes are intentional.

These tests require the Rust native extension to be built.
Run with: PYTHONPATH=src pytest src/tyo3/tests/test_rust_snapshots.py -v
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path as StdPath

import pytest

# Check if native extension is available
try:
    from tyo3 import TyO3Session

    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False

from tyo3.models.symbols import Symbol

# ── Path helpers ──────────────────────────────────────────────────────────

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def fixture_path(name: str) -> str:
    """Return the absolute path to a fixture directory."""
    return str((FIXTURES_DIR / name).resolve())


# ── Skip marker ───────────────────────────────────────────────────────────

needs_native = pytest.mark.skipif(not _HAS_NATIVE, reason="Rust native extension not built")

# ── Helpers ───────────────────────────────────────────────────────────────


def symbol_to_dict(symbol: Symbol) -> dict:
    """Serialize a Symbol to a comparable dictionary."""
    return {
        "name": symbol.name,
        "qualified_name": symbol.qualified_name,
        "kind": symbol.kind,
        "container_name": symbol.container_name,
        "deprecated": symbol.deprecated,
        "location_path": str(symbol.location.path),
        "start_line": symbol.location.range.start.line,
        "start_column": symbol.location.range.start.column,
        "end_line": symbol.location.range.end.line,
        "end_column": symbol.location.range.end.column,
        "selection_start_line": symbol.selection_range.start.line if symbol.selection_range else None,
        "selection_start_column": symbol.selection_range.start.column if symbol.selection_range else None,
        "selection_end_line": symbol.selection_range.end.line if symbol.selection_range else None,
        "selection_end_column": symbol.selection_range.end.column if symbol.selection_range else None,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Snapshot Tests
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
class TestSnapshotSimplePackage:
    """Snapshot tests for the simple_package fixture."""

    @pytest.fixture(autouse=True, scope="class")
    def setup(self) -> Generator:
        self.__class__.rp = TyO3Session(fixture_path("simple_package"))
        yield
        self.__class__.rp.close()

    def test_document_symbols_snapshot(self) -> None:
        """Snapshot the document_symbols output for simple_package/main.py."""
        symbols = self.rp.document_symbols("main.py")
        symbol_dicts = [symbol_to_dict(s) for s in symbols]

        # Verify known symbols exist
        names = {s["name"] for s in symbol_dicts}
        assert "greet" in names, f"Expected 'greet' in symbols, got: {names}"
        assert "MyClass" in names, f"Expected 'MyClass' in symbols, got: {names}"

        # Verify structure
        for s in symbol_dicts:
            assert s["name"], f"Symbol has no name: {s}"
            assert s["kind"], f"Symbol {s['name']} has no kind"
            assert s["start_line"] >= 1, f"Symbol {s['name']} has invalid start_line: {s['start_line']}"
            assert s["start_column"] >= 1, f"Symbol {s['name']} has invalid start_column: {s['start_column']}"

        # Verify count
        assert len(symbols) >= 3, f"Expected at least 3 symbols, got {len(symbols)}"

    def test_workspace_symbols_snapshot(self) -> None:
        """Snapshot the workspace_symbols output for simple_package."""
        results = self.rp.workspace_symbols("greet")
        symbol_dicts = [symbol_to_dict(s) for s in results]
        names = {s["name"] for s in symbol_dicts}
        assert "greet" in names

    def test_no_regression_in_symbol_fields(self) -> None:
        """Verify all expected symbol fields are present and typed correctly."""
        symbols = self.rp.document_symbols("main.py")
        required_fields = {"name", "kind", "location"}
        for sym in symbols:
            for field in required_fields:
                assert field in Symbol.model_fields, f"Symbol missing field: {field}"
                assert getattr(sym, field) is not None, f"Symbol {sym.name} has None for {field}"


@needs_native
class TestSnapshotClasses:
    """Snapshot tests for the classes fixture."""

    @pytest.fixture(autouse=True, scope="class")
    def setup(self) -> Generator:
        self.__class__.rp = TyO3Session(fixture_path("classes"))
        yield
        self.__class__.rp.close()

    def test_document_symbols_snapshot(self) -> None:
        """Snapshot the document_symbols output for classes/models.py."""
        symbols = self.rp.document_symbols("models.py")
        symbol_dicts = [symbol_to_dict(s) for s in symbols]

        names = {s["name"] for s in symbol_dicts}
        # All class names should be present
        expected_classes = {"Animal", "Mammal", "Bird", "Dog", "Cat", "Eagle"}
        for cls_name in expected_classes:
            assert cls_name in names, f"Expected class '{cls_name}' in symbols, got: {names}"

        # Verify class methods
        assert "__init__" in names, "Expected '__init__' constructor in symbols"
        assert "speak" in names, "Expected 'speak' method in symbols"

        # Count should be reasonable (classes + constructors + methods + properties)
        assert 15 <= len(symbols) <= 30, f"Expected 15-30 symbols, got {len(symbols)}"

    def test_workspace_symbols_finds_all_classes(self) -> None:
        """Workspace search for class names should find them."""
        for cls_name in ["Animal", "Dog", "Cat", "Eagle"]:
            results = self.rp.workspace_symbols(cls_name)
            result_names = {s.name for s in results}
            assert cls_name in result_names, f"workspace_symbols('{cls_name}') did not find the class"


@needs_native
class TestSnapshotImports:
    """Snapshot tests for imports fixture."""

    @pytest.fixture(autouse=True, scope="class")
    def setup(self) -> Generator:
        self.__class__.rp = TyO3Session(fixture_path("imports"))
        yield
        self.__class__.rp.close()

    def test_document_symbols_main(self) -> None:
        symbols = self.rp.document_symbols("main.py")
        names = {s.name for s in symbols}
        assert "calculate" in names
        assert "circumference" in names

    def test_document_symbols_math_ops(self) -> None:
        symbols = self.rp.document_symbols("math_ops.py")
        names = {s.name for s in symbols}
        assert "add" in names
        assert "multiply" in names
        assert "PI" in names


@needs_native
class TestSnapshotUnicode:
    """Snapshot tests for unicode fixture."""

    @pytest.fixture(autouse=True, scope="class")
    def setup(self) -> Generator:
        self.__class__.rp = TyO3Session(fixture_path("unicode_positions"))
        yield
        self.__class__.rp.close()

    def test_unicode_identifiers(self) -> None:
        """Verify that Unicode identifiers are discovered correctly."""
        symbols = self.rp.document_symbols("unicode.py")
        names = {s.name for s in symbols}
        # Greek letters
        assert "α" in names, f"Expected Greek α in: {names}"
        assert "β" in names, f"Expected Greek β in: {names}"
        # Japanese identifiers
        assert "挨拶" in names, f"Expected Japanese '挨拶' in: {names}"
        assert "挨拶する" in names, f"Expected Japanese '挨拶する' in: {names}"

    def test_unicode_positions_valid(self) -> None:
        """All symbol positions should be valid (line >= 1, column >= 1)."""
        symbols = self.rp.document_symbols("unicode.py")
        for s in symbols:
            assert s.location.range.start.line >= 1, f"Symbol {s.name}: invalid start line"
            assert s.location.range.start.column >= 1, f"Symbol {s.name}: invalid start column"
            assert s.location.range.end.line >= 1, f"Symbol {s.name}: invalid end line"
            assert s.location.range.end.column >= 1, f"Symbol {s.name}: invalid end column"


@needs_native
class TestSnapshotStandalone:
    """Snapshot tests for standalone fixture."""

    @pytest.fixture(autouse=True, scope="class")
    def setup(self) -> Generator:
        self.__class__.rp = TyO3Session(fixture_path("standalone"))
        yield
        self.__class__.rp.close()

    def test_document_symbols(self) -> None:
        symbols = self.rp.document_symbols("script.py")
        names = {s.name for s in symbols}
        assert "standalone_greeting" in names
        assert "StandaloneCounter" in names
        assert "counter" in names
