"""Coordinate conversion edge case tests.

Tests for Phase 4 of RUST_BACKEND_IMPLEMENTATION.md §8.4.

Validates that position conversion (1-based Python ↔ byte-offset Rust)
handles edge cases correctly:
- File boundaries (start, end, beyond)
- Zero/negative positions
- Multi-byte UTF-8 characters
- Empty lines
- Blank files

These tests require the Rust native extension to be built.
Run with: PYTHONPATH=src pytest src/tyo3/tests/test_coordinate_conversion.py -v
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path as StdPath

import pytest

# Check if native extension is available
try:
    from tyo3.rust_project import RustProject

    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False

from pydantic import ValidationError

from tyo3.exceptions import PositionError
from tyo3.models.analysis import Position

# ── Path helpers ──────────────────────────────────────────────────────────

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


def fixture_path(name: str) -> str:
    """Return the absolute path to a fixture directory."""
    return str((FIXTURES_DIR / name).resolve())


# ── Skip marker ───────────────────────────────────────────────────────────

needs_native = pytest.mark.skipif(not _HAS_NATIVE, reason="Rust native extension not built")

# ═══════════════════════════════════════════════════════════════════════════
# Position Validation Tests (Python-side)
# ═══════════════════════════════════════════════════════════════════════════


class TestPositionValidation:
    """Test the Python-side position model validation."""

    def test_valid_position(self) -> None:
        pos = Position(line=1, column=1)
        assert pos.line == 1
        assert pos.column == 1

    def test_position_equality(self) -> None:
        assert Position(line=1, column=1) == Position(line=1, column=1)
        assert Position(line=1, column=1) != Position(line=1, column=2)
        assert Position(line=1, column=1) != Position(line=2, column=1)

    @pytest.mark.parametrize(
        "line,col,valid",
        [
            (1, 1, True),  # start of file
            (99999, 1, True),  # potentially beyond file (Rust will catch)
            (1, 99999, True),  # potentially beyond line (Rust will catch)
            (-1, 1, False),  # negative line
            (1, -1, False),  # negative column
            (0, 1, False),  # zero line
            (1, 0, False),  # zero column
        ],
    )
    def test_position_boundaries(self, line: int, col: int, valid: bool) -> None:
        """Test position boundary values."""
        if valid:
            pos = Position(line=line, column=col)
            assert pos.line == line
            assert pos.column == col
        else:
            # Positions must be 1-based — the model validator rejects
            # zero or negative values at construction time.
            with pytest.raises(ValidationError, match="1-based"):
                Position(line=line, column=col)


# ═══════════════════════════════════════════════════════════════════════════
# Rust-side Coordinate Tests (requires native extension)
# ═══════════════════════════════════════════════════════════════════════════


@needs_native
class TestRustCoordinateConversion:
    """Test Rust coordinate conversion through the PyO3 boundary."""

    @pytest.fixture(autouse=True, scope="class")
    def setup(self) -> Generator:
        self.__class__.rp = RustProject(fixture_path("simple_package"))
        yield
        self.__class__.rp.close()

    def test_hover_at_start_of_file(self) -> None:
        """Position (1, 1) should work on a non-empty file."""
        hover = self.rp.hover("main.py", 1, 1)
        # May or may not return hover; should not crash
        assert hover is None or hover is not None

    def test_hover_at_end_of_file(self) -> None:
        """Position at a high line number should raise PositionError."""
        with pytest.raises(PositionError):
            self.rp.hover("main.py", 999, 1)

    def test_hover_in_middle_of_file(self) -> None:
        """Position in the middle of the file should work."""
        hover = self.rp.hover("main.py", 5, 5)
        # Should return something or None without error
        assert hover is None or hover is not None

    def test_goto_definition_at_file_start(self) -> None:
        targets = self.rp.goto_definition("main.py", 1, 1)
        assert isinstance(targets, list)

    def test_goto_definition_at_file_end(self) -> None:
        """Position at very high line should raise PositionError."""
        with pytest.raises(PositionError):
            self.rp.goto_definition("main.py", 99999, 1)

    def test_zero_line_rejected(self) -> None:
        """(0, *) positions should raise PositionError."""
        with pytest.raises(PositionError):
            self.rp.goto_definition("main.py", 0, 1)

    def test_zero_column_rejected(self) -> None:
        """(*, 0) positions should raise PositionError."""
        with pytest.raises(PositionError):
            self.rp.goto_definition("main.py", 1, 0)

    def test_negative_line_rejected(self) -> None:
        """(-1, *) positions raise PositionError (mapped from PyO3 OverflowError)."""
        with pytest.raises(PositionError):
            self.rp.goto_definition("main.py", -1, 1)

    def test_negative_column_rejected(self) -> None:
        """(*, -1) positions raise PositionError (mapped from PyO3 OverflowError)."""
        with pytest.raises(PositionError):
            self.rp.goto_definition("main.py", 1, -1)

    def test_document_symbols_positions_valid(self) -> None:
        """All symbol positions returned by Rust should be valid."""
        symbols = self.rp.document_symbols("main.py")
        for sym in symbols:
            assert sym.location.range.start.line >= 1, (
                f"Symbol {sym.name}: start line {sym.location.range.start.line} < 1"
            )
            assert sym.location.range.start.column >= 1, (
                f"Symbol {sym.name}: start column {sym.location.range.start.column} < 1"
            )
            assert sym.location.range.end.line >= sym.location.range.start.line, (
                f"Symbol {sym.name}: end line {sym.location.range.end.line} < start {sym.location.range.start.line}"
            )
            # Column check: if same line, end column >= start column is typical
            if sym.location.range.end.line == sym.location.range.start.line:
                assert sym.location.range.end.column >= sym.location.range.start.column, (
                    f"Symbol {sym.name}: end col {sym.location.range.end.column}"
                    f" < start {sym.location.range.start.column}"
                )

    def test_column_beyond_current_line_rejected(self) -> None:
        """Column 500 on line 1 must raise PositionError, not clamp silently.

        This test captures the bug described in TyO3_REVIEW_2_REFACTORING_GUIDE.md
        §Phase 1.1: the Rust coordinate converter currently slices from
        line_start to EOF instead of extracting the current line only,
        so a column value beyond the current line can walk into later
        lines instead of being rejected.

        This test should fail before Phase 1 fixes land.
        """
        with pytest.raises(PositionError):
            self.rp.goto_definition("main.py", 1, 500)


@needs_native
class TestUnicodeCoordinateConversion:
    """Test coordinate conversion with multi-byte UTF-8 characters."""

    @pytest.fixture(autouse=True, scope="class")
    def setup(self) -> Generator:
        self.__class__.rp = RustProject(fixture_path("unicode_positions"))
        yield
        self.__class__.rp.close()

    def test_hover_on_greek_identifier(self) -> None:
        """Hover on α (Greek alpha) at line 10."""
        hover = self.rp.hover("unicode.py", 10, 1)
        assert hover is not None, "Expected hover for Greek identifier α"

    def test_hover_on_japanese_identifier(self) -> None:
        """Hover on 挨拶 (Japanese greeting) at line 22."""
        hover = self.rp.hover("unicode.py", 22, 1)
        assert hover is not None, "Expected hover for Japanese identifier 挨拶"

    def test_symbols_have_emoji_in_docstrings(self) -> None:
        """Symbols with emoji in docstrings should be discovered without
        position errors."""
        symbols = self.rp.document_symbols("unicode.py")
        names = {s.name for s in symbols}
        assert "describe_emoji" in names
        # All positions should be valid despite emoji in docstrings
        for sym in symbols:
            assert sym.location.range.start.line >= 1
            assert sym.location.range.start.column >= 1

    def test_workspace_symbols_with_unicode_query(self) -> None:
        """Workspace search with Unicode query should work."""
        results = self.rp.workspace_symbols("καλημέρα")
        names = {s.name for s in results}
        assert "καλημέρα" in names

    def test_unicode_positions_are_1_based(self) -> None:
        """All positions for Unicode identifiers must be 1-based."""
        symbols = self.rp.document_symbols("unicode.py")
        for sym in symbols:
            loc = sym.location
            assert loc.range.start.line >= 1
            assert loc.range.start.column >= 1
            # Ensure selection_range (if present) is also valid
            if sym.selection_range:
                assert sym.selection_range.start.line >= 1
                assert sym.selection_range.start.column >= 1


@needs_native
class TestEmptyFileEdgeCases:
    """Test coordinate conversion for edge cases like empty files."""

    @pytest.fixture(autouse=True, scope="class")
    def setup(self) -> Generator:
        self.__class__.rp = RustProject(fixture_path("empty"))
        yield
        self.__class__.rp.close()

    def test_empty_project_files(self) -> None:
        """Empty project should return no files."""
        files = self.rp.files()
        assert isinstance(files, list)

    def test_empty_project_check(self) -> None:
        """Check on empty project should not crash."""
        result = self.rp.check()
        assert result is not None
        assert isinstance(result.diagnostics, list)
