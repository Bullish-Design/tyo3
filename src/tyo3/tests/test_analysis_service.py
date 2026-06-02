"""Tests for AnalysisService — tyo3-analysis.allium rules.

Obligation groups:
- CheckProject: success, rejection when not open
- CheckFile: success, rejection when file not in project
- FilterBySeverity / FilterByCode: filtered results
- ClearDiagnosticsOnReload: diagnostics cleared on project reload
"""

from datetime import UTC
from pathlib import PurePosixPath

import pytest

from tyo3.models.analysis import DiagnosticSeverity
from tyo3.models.core import FileCategory


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
        from datetime import datetime

        from tyo3.models.core import ProjectStatus, TyProject

        other_project = TyProject(
            root=PurePosixPath("other"),
            status=ProjectStatus.OPEN,
            opened_at=datetime.now(UTC),
        )
        other_file = type("obj", (object,), {"path": PurePosixPath("x"), "project": other_project})()
        # We need a real ProjectFile for this
        from tyo3.models.core import ProjectFile

        other_file = ProjectFile(
            path=PurePosixPath("other/f.py"),
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
        key = str(open_project.root)
        analysis_service._diagnostics_by_project[key] = [diagnostic]
        assert len(analysis_service.diagnostics) == 1
        analysis_service.clear_diagnostics_for_project(open_project)
        assert len(analysis_service.diagnostics) == 0

    def test_does_not_clear_other_projects(self, analysis_service, open_project) -> None:
        from datetime import datetime

        from tyo3.models.core import ProjectStatus, TyProject

        other = TyProject(
            root=PurePosixPath("other"),
            status=ProjectStatus.OPEN,
            opened_at=datetime.now(UTC),
        )
        from tyo3.models.analysis import Diagnostic

        diag_a = Diagnostic(
            message="A",
        )
        diag_b = Diagnostic(
            message="B",
        )
        analysis_service._diagnostics_by_project[str(open_project.root)] = [diag_a]
        analysis_service._diagnostics_by_project[str(other.root)] = [diag_b]
        analysis_service.clear_diagnostics_for_project(open_project)
        assert len(analysis_service.diagnostics) == 1
        assert analysis_service.diagnostics[0].message == "B"
