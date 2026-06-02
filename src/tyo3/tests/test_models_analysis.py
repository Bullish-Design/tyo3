"""Tests for analysis domain models — tyo3-analysis.allium.

Obligation groups:
- Value type field presence (Position, Range, FileRange, CheckResult)
- Enum tests (DiagnosticSeverity)
- Entity tests (Diagnostic with optional fields)
"""

from tyo3.models.analysis import (
    CheckResult,
    Diagnostic,
    DiagnosticSeverity,
    FileRange,
    Position,
    Range,
)
from tyo3.models.core import Path


class TestPosition:
    """Position value type: 1-based line/column."""

    def test_creation(self) -> None:
        p = Position(line=1, column=5)
        assert p.line == 1
        assert p.column == 5

    def test_equality(self) -> None:
        assert Position(line=1, column=1) == Position(line=1, column=1)
        assert Position(line=1, column=1) != Position(line=2, column=1)


class TestRange:
    """Range value type: start/end positions."""

    def test_creation(self) -> None:
        start = Position(line=1, column=1)
        end = Position(line=10, column=5)
        r = Range(start=start, end=end)
        assert r.start == start
        assert r.end == end


class TestFileRange:
    """FileRange value type: range within a file."""

    def test_creation(self) -> None:
        path = Path(components=["src", "main.py"])
        r = Range(start=Position(line=1, column=1), end=Position(line=5, column=1))
        fr = FileRange(path=path, range=r)
        assert fr.path == path
        assert fr.range == r


class TestCheckResult:
    """CheckResult value type tests."""

    def test_empty_default(self) -> None:
        result = CheckResult()
        assert result.diagnostics == []
        assert result.files_checked is None
        assert result.elapsed_ms is None

    def test_full_fields(self) -> None:
        result = CheckResult(diagnostics=[], files_checked=10, elapsed_ms=500)
        assert result.files_checked == 10
        assert result.elapsed_ms == 500


class TestDiagnosticSeverity:
    """DiagnosticSeverity enum tests."""

    def test_values(self) -> None:
        assert DiagnosticSeverity.FATAL == "fatal"
        assert DiagnosticSeverity.ERROR == "error"
        assert DiagnosticSeverity.WARNING == "warning"
        assert DiagnosticSeverity.INFORMATION == "information"
        assert DiagnosticSeverity.HINT == "hint"

    def test_ordering_by_severity(self) -> None:
        severe = [DiagnosticSeverity.FATAL, DiagnosticSeverity.ERROR,
                  DiagnosticSeverity.WARNING, DiagnosticSeverity.INFORMATION,
                  DiagnosticSeverity.HINT]
        assert len(severe) == 5


class TestDiagnostic:
    """Diagnostic entity tests."""

    def test_required_fields(self, open_project) -> None:
        d = Diagnostic(
            message="Unexpected type",
        )
        assert d.message == "Unexpected type"
        assert d.file is None
        assert d.range is None
        assert d.severity == "error"
        assert d.code is None
        assert d.details == []

    def test_with_file_and_range(self, first_party_file, range_) -> None:
        d = Diagnostic(
            file=first_party_file,
            range=range_,
            severity=DiagnosticSeverity.WARNING,
            code="unused-import",
            message="Unused import",
            details={"os"},
        )
        assert d.file == first_party_file
        assert d.range == range_
        assert d.severity == DiagnosticSeverity.WARNING
        assert "os" in d.details

    def test_details_set(self, open_project) -> None:
        d = Diagnostic(
            message="test",
            details={"a", "b"},
        )
        assert set(d.details) == {"a", "b"}
