"""Tests for the Rust-Python DTO boundary using pythonize.

Verifies that Rust methods return plain Python dicts/lists (via pythonize)
that validate correctly through Pydantic's ``model_validate``.

These tests require the Rust native extension to be built.
Run with: PYTHONPATH=src pytest src/tyo3/tests/test_native_objects.py -v
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

# Check if native extension is available
try:
    import tyo3._native_impl  # noqa: F401 — imported for availability check

    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False

from tyo3.models.analysis import (
    CheckResult,
    Diagnostic,
    DiagnosticSeverity,
    FileRange,
    Range,
)
from tyo3.models.navigation import (
    DefinitionTarget,
    HoverContent,
    HoverContentKind,
    HoverResult,
)
from tyo3.models.symbols import Symbol, SymbolKind

# ── Skip marker ───────────────────────────────────────────────────────────

needs_native = pytest.mark.skipif(not _HAS_NATIVE, reason="Rust native extension not built")


# ═══════════════════════════════════════════════════════════════════════════
# Test that pythonize-produced dicts validate correctly
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
class TestPythonizeDictStructures:
    """Verify Rust methods return plain Python objects that validate."""

    def test_position_dict_structure(self) -> None:
        """Pythonize should produce dicts with line/column keys."""
        from tyo3 import TyO3Session

        rp = TyO3Session("fixtures/simple_package")
        try:
            symbols = rp.document_symbols("main.py")
            assert len(symbols) > 0
            sym = symbols[0]
            # Verify we got proper models back
            assert isinstance(sym, Symbol)
            assert isinstance(sym.name, str)
            assert isinstance(sym.kind, SymbolKind)
            assert isinstance(sym.location, FileRange)
            assert sym.location.range.start.line >= 1
            assert sym.location.range.start.column >= 1
        finally:
            rp.close()

    def test_check_result_roundtrip(self) -> None:
        """Check() returns a dict that validates as CheckResult."""
        from tyo3 import TyO3Session

        rp = TyO3Session("fixtures/simple_package")
        try:
            result = rp.check()
            assert isinstance(result, CheckResult)
            assert isinstance(result.diagnostics, list)
        finally:
            rp.close()

    def test_document_symbols_roundtrip(self) -> None:
        """document_symbols() returns a list of dicts that validate as Symbols."""
        from tyo3 import TyO3Session

        rp = TyO3Session("fixtures/simple_package")
        try:
            symbols = rp.document_symbols("main.py")
            assert len(symbols) > 0
            for sym in symbols:
                assert isinstance(sym.name, str)
                assert isinstance(sym.kind, SymbolKind)
        finally:
            rp.close()

    def test_goto_definition_roundtrip(self) -> None:
        """goto_definition() returns a list of dicts for DefinitionTarget."""
        from tyo3 import TyO3Session

        rp = TyO3Session("fixtures/simple_package")
        try:
            targets = rp.goto_definition("main.py", 1, 1)
            assert isinstance(targets, list)
            for t in targets:
                assert isinstance(t, DefinitionTarget)
                assert isinstance(t.path, PurePosixPath)
                assert isinstance(t.range, Range)
        finally:
            rp.close()

    def test_find_references_roundtrip(self) -> None:
        """find_references() returns a list of dicts for Reference."""
        from tyo3 import TyO3Session

        rp = TyO3Session("fixtures/simple_package")
        try:
            refs = rp.find_references("main.py", 1, 1)
            assert isinstance(refs, list)
        finally:
            rp.close()

    def test_workspace_symbols_roundtrip(self) -> None:
        """workspace_symbols() returns a list of dicts for Symbol."""
        from tyo3 import TyO3Session

        rp = TyO3Session("fixtures/simple_package")
        try:
            symbols = rp.workspace_symbols("greet")
            assert isinstance(symbols, list)
            for sym in symbols:
                assert isinstance(sym, Symbol)
        finally:
            rp.close()

    def test_hover_roundtrip(self) -> None:
        """hover() returns a dict or None for HoverResult."""
        from tyo3 import TyO3Session

        rp = TyO3Session("fixtures/simple_package")
        try:
            result = rp.hover("main.py", 1, 1)
            if result is not None:
                assert isinstance(result, HoverResult)
                assert isinstance(result.location, FileRange)
                assert isinstance(result.contents, list)
                for c in result.contents:
                    assert isinstance(c, HoverContent)
                    assert isinstance(c.kind, HoverContentKind)
        finally:
            rp.close()


@needs_native
class TestPythonizeEdgeCases:
    """Verify edge cases in pythonize serialization."""

    def test_check_file_diagnostics(self) -> None:
        """check_file() returns diagnostics for a specific file."""
        from tyo3 import TyO3Session

        rp = TyO3Session("fixtures/simple_package")
        try:
            result = rp.check_file("main.py")
            assert isinstance(result, CheckResult)
            for d in result.diagnostics:
                assert isinstance(d, Diagnostic)
                assert isinstance(d.severity, DiagnosticSeverity)
                assert isinstance(d.message, str)
        finally:
            rp.close()

    def test_none_hover_handling(self) -> None:
        """hover() can return None for positions without hover info."""
        from tyo3 import TyO3Session

        rp = TyO3Session("fixtures/simple_package")
        try:
            # Position at a non-symbol (e.g., whitespace) may return None
            result = rp.hover("main.py", 1, 1)
            # Either None or a valid HoverResult — both are acceptable
            if result is not None:
                assert isinstance(result, HoverResult)
                assert isinstance(result.location, FileRange)
        finally:
            rp.close()

    def test_exception_types_still_importable(self) -> None:
        """Exception classes remain on the native module."""
        from tyo3._native_impl import (
            ProjectClosedError,  # type: ignore[import-untyped]
        )

        assert issubclass(ProjectClosedError, Exception)

    def test_native_module_exports_typroject_tysnapshot_and_exceptions(self) -> None:
        """The native module exports TyProject, TySnapshot, and exception types."""
        import tyo3._native_impl as _native  # type: ignore[import-untyped]

        expected = {
            "TyProject",
            "TySnapshot",
            "ProjectClosedError",
            "PathResolutionError",
            "PositionError",
            # Gate 1 §1.3.6: typed error for reading an evicted revision.
            "RevisionEvictedError",
        }
        for name in dir(_native):
            if name.startswith("_"):
                continue
            obj = getattr(_native, name)
            if isinstance(obj, type):
                assert name in expected, (
                    f"Unexpected native type {name!r} — should not be a PyO3 class after DTO boundary simplification"
                )
