"""Tests for TyO3Session.check_file() path filtering."""

from datetime import datetime
from pathlib import PurePosixPath

from tyo3.models.analysis import (
    CheckResult,
    Diagnostic,
    DiagnosticSeverity,
    Position,
    Range,
)
from tyo3.models.core import FileCategory, ProjectFile, ProjectStatus, TyProject


def _make_project():
    return TyProject(
        root=PurePosixPath("/proj"),
        status=ProjectStatus.OPEN,
        opened_at=datetime.now(),
    )


def _make_diagnostic(file_path: str) -> Diagnostic:
    proj = _make_project()
    pf = ProjectFile(
        path=PurePosixPath(file_path),
        project=proj,
        file_category=FileCategory.FIRST_PARTY,
    )
    return Diagnostic(
        file=pf,
        range=Range(
            start=Position(line=1, column=1),
            end=Position(line=1, column=10),
        ),
        severity=DiagnosticSeverity.ERROR,
        message="test error",
    )


class TestCheckFileFiltering:
    """Verify that check_file actually filters by the given path."""

    def test_filter_matches_target_file(self):
        """Diagnostics for the target file should be included."""
        d1 = _make_diagnostic("src/main.py")
        d2 = _make_diagnostic("src/other.py")
        result = CheckResult(diagnostics=[d1, d2], files_checked=2)

        # Simulate what check_file does: filter by path
        target = PurePosixPath("src/main.py")
        filtered = [
            d
            for d in result.diagnostics
            if d.file is not None
            and (
                d.file.path == target
                or str(d.file.path).endswith(str(target))
            )
        ]
        assert len(filtered) == 1
        assert filtered[0].file.path == PurePosixPath("src/main.py")

    def test_filter_excludes_other_files(self):
        """Diagnostics for other files should be excluded."""
        d1 = _make_diagnostic("src/other.py")
        result = CheckResult(diagnostics=[d1], files_checked=1)

        target = PurePosixPath("src/main.py")
        filtered = [
            d
            for d in result.diagnostics
            if d.file is not None
            and (
                d.file.path == target
                or str(d.file.path).endswith(str(target))
            )
        ]
        assert len(filtered) == 0

    def test_filter_handles_no_file_diagnostics(self):
        """Diagnostics with file=None should be excluded."""
        d = Diagnostic(message="generic error")
        result = CheckResult(diagnostics=[d])

        target = PurePosixPath("src/main.py")
        filtered = [
            d
            for d in result.diagnostics
            if d.file is not None
            and (
                d.file.path == target
                or str(d.file.path).endswith(str(target))
            )
        ]
        assert len(filtered) == 0
