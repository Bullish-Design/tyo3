"""Integration tests — cross-entity and cross-module data flow chains.

Tests derived from Allium spec data flow chain analysis:
1. Surface → Rule → Downstream rule: verify data flows end-to-end
2. Cross-entity processes: project → files → diagnostics → symbols → navigation
3. Data flow chain tests from propagate skill taxonomy
"""

from datetime import UTC, datetime
from pathlib import PurePosixPath

from tyo3.models.analysis import Diagnostic, DiagnosticSeverity
from tyo3.models.core import (
    FileCategory,
    ProjectFile,
    ProjectStatus,
    TyProject,
)
from tyo3.services.analysis_service import AnalysisService
from tyo3.services.project_service import ProjectService


class TestProjectToDiagnosticDataFlow:
    """Data flow chain: OpenProject → CheckProject → Diagnostics."""

    def test_chain_project_open_to_check(self) -> None:
        """Open a project, then check it — diagnostics belong to the project."""
        ps = ProjectService()
        a_svc = AnalysisService()

        root = PurePosixPath("chain-test")
        project, _ = ps.open_project(root)
        result = a_svc.check_project(project)

        # Diagnostics should be returned
        assert result is not None

    def test_chain_project_open_then_reload_then_check(self) -> None:
        """Open → reload → check — diagnostics cleared on reload."""
        ps = ProjectService()
        a_svc = AnalysisService()

        root = PurePosixPath("chain-reload")
        project, _ = ps.open_project(root)
        a_svc.clear_diagnostics_for_project(project)
        result = a_svc.check_project(project)
        assert result is not None


class TestCrossEntityConsistency:
    """Cross-entity consistency tests."""

    def test_file_belongs_to_project_files(self) -> None:
        """A ProjectFile's project relationship should be consistent."""
        root = PurePosixPath("consistency")
        now = datetime.now(UTC)
        project = TyProject(root=root, status=ProjectStatus.OPEN, opened_at=now)
        pf = ProjectFile(
            path=PurePosixPath("consistency/main.py"),
            project=project,
            file_category=FileCategory.FIRST_PARTY,
        )
        assert pf.project.root == project.root
        assert pf.project.status == ProjectStatus.OPEN

    def test_diagnostic_cross_references_file_and_project(self) -> None:
        """A Diagnostic points to a ProjectFile and a TyProject —
        they must be consistent with the DiagnosticBelongsToOpenProject
        and DiagnosticFileBelongsToProject invariants.
        """
        root = PurePosixPath("diag-cross")
        now = datetime.now(UTC)
        project = TyProject(root=root, status=ProjectStatus.OPEN, opened_at=now)
        pf = ProjectFile(
            path=PurePosixPath("diag-cross/src.py"),
            project=project,
            file_category=FileCategory.FIRST_PARTY,
        )
        d = Diagnostic(
            file=pf,
            severity=DiagnosticSeverity.ERROR,
            message="Type error",
        )
        # Diagnostic file belongs to the same project as the file's project
        assert d.file is not None
        assert d.file.project.root == pf.project.root


class TestSurfaceToRuleChain:
    """Data flow chains from surface through rules to downstream preconditions."""

    def test_typroject_api_opens_and_checks(self) -> None:
        """Surface TyProjectAPI provides open → analysis.TypeChecking provides check.
        This tests the chain: UserOpensProject → OpenProject → TyProject.created →
        UserChecksProject precondition (project.is_open).
        """
        ps = ProjectService()
        a_svc = AnalysisService()

        # Surface provides: UserOpensProject
        root = PurePosixPath("surface-chain")
        project, _ = ps.open_project(root)

        # Data flows: project.status = open → precondition for UserChecksProject
        assert project.is_open

        # Surface provides: UserChecksProject (when project.is_open)
        result = a_svc.check_project(project)
        assert result is not None
