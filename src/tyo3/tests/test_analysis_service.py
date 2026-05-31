"""Tests for AnalysisService — tyo3-analysis.allium rules.

Obligation groups:
- CheckProject: success, rejection when not open
- CheckFile: success, rejection when file not in project
- FilterBySeverity / FilterByCode: filtered results
- ClearDiagnosticsOnReload: diagnostics cleared on project reload
"""

import pytest

from tyo3.models.analysis import DiagnosticSeverity
from tyo3.models.core import FileCategory, Path


class TestCheckProject:
    """CheckProject rule tests."""

    def test_check_open_project(self, analysis_service, open_project) -> None:
        result = analysis_service.check_project(open_project)
        assert result is not None
        assert result.diagnostics == []
        assert result.files_checked == 0
        assert result.elapsed_ms == 0

    def test_rejects_closed_project(self, analysis_service, closed_project) -> None:
        with pytest.raises(ValueError, match="not open"):
            analysis_service.check_project(closed_project)


class TestCheckFile:
    """CheckFile rule tests."""

    def test_check_file_in_project(self, analysis_service, open_project, first_party_file) -> None:
        result = analysis_service.check_file(open_project, first_party_file)
        assert result is not None
        assert result.files_checked == 1

    def test_rejects_file_not_in_project(self, analysis_service, open_project) -> None:
        from tyo3.models.core import TyProject, ProjectStatus
        from datetime import datetime, timezone

        other_project = TyProject(
            root=Path(components=["other"]),
            status=ProjectStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
        )
        other_file = type('obj', (object,), {'path': Path(components=["x"]), 'project': other_project})()
        # We need a real ProjectFile for this
        from tyo3.models.core import ProjectFile
        other_file = ProjectFile(
            path=Path(components=["other", "f.py"]),
            project=other_project,
            file_category=FileCategory.FIRST_PARTY,
        )
        with pytest.raises(ValueError, match="does not belong"):
            analysis_service.check_file(open_project, other_file)

    def test_rejects_closed_project(self, analysis_service, closed_project, first_party_file) -> None:
        with pytest.raises(ValueError, match="not open"):
            analysis_service.check_file(closed_project, first_party_file)


class TestFilterBySeverity:
    """FilterBySeverity rule tests."""

    def test_returns_empty_when_no_matches(self, analysis_service, open_project) -> None:
        result = analysis_service.filter_by_severity(open_project, DiagnosticSeverity.ERROR)
        assert result.diagnostics == []

    def test_rejects_closed_project(self, analysis_service, closed_project) -> None:
        with pytest.raises(ValueError, match="not open"):
            analysis_service.filter_by_severity(closed_project, DiagnosticSeverity.ERROR)


class TestFilterByCode:
    """FilterByCode rule tests."""

    def test_returns_empty_when_no_matches(self, analysis_service, open_project) -> None:
        result = analysis_service.filter_by_code(open_project, "type-arg")
        assert result.diagnostics == []

    def test_rejects_closed_project(self, analysis_service, closed_project) -> None:
        with pytest.raises(ValueError, match="not open"):
            analysis_service.filter_by_code(closed_project, "type-arg")


class TestClearDiagnosticsOnReload:
    """ClearDiagnosticsOnReload rule tests."""

    def test_clears_diagnostics(self, analysis_service, open_project, diagnostic) -> None:
        analysis_service._diagnostics.append(diagnostic)
        assert len(analysis_service._diagnostics) == 1
        analysis_service.clear_diagnostics_for_project(open_project)
        assert len(analysis_service._diagnostics) == 0

    def test_does_not_clear_other_projects(self, analysis_service, open_project) -> None:
        from datetime import datetime, timezone
        from tyo3.models.core import TyProject, ProjectStatus

        other = TyProject(
            root=Path(components=["other"]),
            status=ProjectStatus.OPEN,
            opened_at=datetime.now(timezone.utc),
        )
        other_diag = type('d', (object,), {'project': other})()
        # We need proper Diagnostic objects
        from tyo3.models.analysis import Diagnostic
        from tyo3.models.analysis import Position, Range

        diag_a = Diagnostic(
            project=open_project,
            message="A",
            severity=DiagnosticSeverity.ERROR,
        )
        diag_b = Diagnostic(
            project=other,
            message="B",
            severity=DiagnosticSeverity.ERROR,
        )
        analysis_service._diagnostics = [diag_a, diag_b]
        analysis_service.clear_diagnostics_for_project(open_project)
        assert len(analysis_service._diagnostics) == 1
        assert analysis_service._diagnostics[0].message == "B"
