"""Type checking and analysis service — tyo3-analysis.allium rules."""

from __future__ import annotations

from typing import Optional

from tyo3.models.analysis import (
    CheckResult,
    Diagnostic,
    DiagnosticSeverity,
)
from tyo3.models.core import ProjectFile, TyProject


def run_ty_check(project: TyProject) -> CheckResult:
    """Black-box: runs ty's type checker on the project.

    In production this invokes the Rust ty_ide backend.
    """
    return CheckResult(diagnostics=[], files_checked=0, elapsed_ms=0)


def check_duration() -> int:
    """Returns elapsed wall-clock milliseconds for the current check."""
    return 0


class AnalysisService:
    """Implements type-checking rules from tyo3-analysis.allium."""

    def __init__(self) -> None:
        self._diagnostics: list[Diagnostic] = []

    @property
    def diagnostics(self) -> list[Diagnostic]:
        return list(self._diagnostics)

    # ── CheckProject ───────────────────────────────────────────────────

    def check_project(self, project: TyProject) -> CheckResult:
        """CheckProject: requires project.is_open."""
        if not project.is_open:
            raise ValueError("Project is not open")

        result = run_ty_check(project)
        for d in result.diagnostics:
            diagnostic = Diagnostic(
                project=project,
                file=d.file,
                range=d.range,
                severity=d.severity,
                code=d.code,
                message=d.message,
                details=set(d.details) if d.details else set(),
            )
            self._diagnostics.append(diagnostic)
        return result

    # ── CheckFile ──────────────────────────────────────────────────────

    def check_file(self, project: TyProject, file: ProjectFile) -> CheckResult:
        """CheckFile: requires project.is_open and file.project == project."""
        if not project.is_open:
            raise ValueError("Project is not open")
        if file.project.root != project.root:
            raise ValueError("File does not belong to project")

        all_results = run_ty_check(project)
        file_diagnostics = [
            d
            for d in all_results.diagnostics
            if d.file is not None and d.file.path == file.path
        ]
        for d in file_diagnostics:
            diagnostic = Diagnostic(
                project=project,
                file=file,
                range=d.range,
                severity=d.severity,
                code=d.code,
                message=d.message,
                details=set(d.details) if d.details else set(),
            )
            self._diagnostics.append(diagnostic)

        return CheckResult(
            diagnostics=file_diagnostics, files_checked=1, elapsed_ms=check_duration()
        )

    # ── FilterBySeverity / FilterByCode ────────────────────────────────

    def filter_by_severity(
        self, project: TyProject, severity: str
    ) -> CheckResult:
        if not project.is_open:
            raise ValueError("Project is not open")

        filtered = [
            d for d in self._diagnostics
            if d.project.root == project.root and d.severity == severity
        ]
        return CheckResult(diagnostics=filtered)

    def filter_by_code(self, project: TyProject, code: str) -> CheckResult:
        if not project.is_open:
            raise ValueError("Project is not open")

        filtered = [
            d for d in self._diagnostics
            if d.project.root == project.root and d.code == code
        ]
        return CheckResult(diagnostics=filtered)

    # ── ClearDiagnosticsOnReload ───────────────────────────────────────

    def clear_diagnostics_for_project(self, project: TyProject) -> None:
        """Clear all diagnostics belonging to a project (on reload)."""
        self._diagnostics = [
            d for d in self._diagnostics if d.project.root != project.root
        ]
