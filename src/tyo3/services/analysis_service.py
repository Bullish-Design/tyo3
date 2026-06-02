"""Type checking and analysis service — tyo3-analysis.allium rules.

DEPRECATED: Use tyo3.TyO3Session instead. This module will be removed in v0.2.
"""

from __future__ import annotations

from tyo3.models.analysis import (
    CheckResult,
    Diagnostic,
)
from tyo3.models.core import Path, ProjectFile, TyProject


def run_ty_check(project: TyProject) -> CheckResult:
    """Black-box: runs ty's type checker on the project.

    In production this invokes the Rust ty_ide backend.
    """
    return CheckResult(diagnostics=[], files_checked=0, elapsed_ms=0)


def check_duration() -> int:
    """Returns elapsed wall-clock milliseconds for the current check."""
    return 0


class AnalysisService:
    """Implements type-checking rules from tyo3-analysis.allium.

    DEPRECATED: Use :class:`tyo3.TyO3Session` instead.

    Parameters
    ----------
    use_rust:
        When ``True``, analysis is backed by the Rust ty engine via
        :class:`~tyo3.rust_project.RustProject`.  Default ``False`` keeps
        existing black-box stubs for unit testing.
    """

    def __init__(self, use_rust: bool = False) -> None:
        self._diagnostics_by_project: dict[str, list[Diagnostic]] = {}
        self._use_rust: bool = use_rust
        # RustProject instances keyed by root path string
        self._rust_projects: dict[str, object] = {}

    # ── Rust backend access ─────────────────────────────────────────

    @staticmethod
    def _path_to_str(path: Path) -> str:
        """Convert a Path model to a file-system path string."""
        components = path.components
        if components and components[0] == "/":
            return "/" + "/".join(components[1:])
        return "/".join(components)

    def _get_rust_project(self, root_path: str) -> object | None:
        """Return the RustProject for *root_path*, or ``None``."""
        return self._rust_projects.get(root_path)

    def set_rust_project(self, root_path: str, rp: object) -> None:
        """Register a RustProject instance for dependency injection."""
        self._rust_projects[root_path] = rp

    def _ensure_project_key(self, project: TyProject) -> str:
        """Return the string key for a project and ensure it has a diagnostics list."""
        key = str(project.root)
        if key not in self._diagnostics_by_project:
            self._diagnostics_by_project[key] = []
        return key

    @property
    def diagnostics(self) -> list[Diagnostic]:
        result: list[Diagnostic] = []
        for diags in self._diagnostics_by_project.values():
            result.extend(diags)
        return result

    # ── CheckProject ───────────────────────────────────────────────────

    def check_project(self, project: TyProject) -> CheckResult:
        """CheckProject: requires project.is_open."""
        if not project.is_open:
            raise ValueError("Project is not open")

        key = self._ensure_project_key(project)

        rp = self._get_rust_project(str(project.root))
        if rp is not None and self._use_rust:
            result = rp.check()  # type: ignore[union-attr]
            self._diagnostics_by_project[key].extend(result.diagnostics)
            return result

        result = run_ty_check(project)
        for d in result.diagnostics:
            diagnostic = Diagnostic(
                file=d.file,
                range=d.range,
                severity=d.severity,
                code=d.code,
                message=d.message,
                details=list(d.details) if d.details else [],
            )
            self._diagnostics_by_project[key].append(diagnostic)
        return result

    # ── CheckFile ──────────────────────────────────────────────────────

    def check_file(self, project: TyProject, file: ProjectFile) -> CheckResult:
        """CheckFile: requires project.is_open and file.project == project."""
        if not project.is_open:
            raise ValueError("Project is not open")
        if file.project.root != project.root:
            raise ValueError("File does not belong to project")

        key = self._ensure_project_key(project)

        # With Rust backend: run full check, filter by file path
        rp = self._get_rust_project(str(project.root))
        if rp is not None and self._use_rust:
            full_result = rp.check()  # type: ignore[union-attr]
            file_diagnostics = [
                d for d in full_result.diagnostics
                if d.file is not None
            ]
            self._diagnostics_by_project[key].extend(file_diagnostics)
            return CheckResult(
                diagnostics=file_diagnostics,
                files_checked=1,
                elapsed_ms=full_result.elapsed_ms,
            )

        all_results = run_ty_check(project)
        file_diagnostics = [
            d
            for d in all_results.diagnostics
            if d.file is not None and d.file.path == file.path
        ]
        for d in file_diagnostics:
            diagnostic = Diagnostic(
                file=file,
                range=d.range,
                severity=d.severity,
                code=d.code,
                message=d.message,
                details=list(d.details) if d.details else [],
            )
            self._diagnostics_by_project[key].append(diagnostic)

        return CheckResult(
            diagnostics=file_diagnostics, files_checked=1, elapsed_ms=check_duration()
        )

    # ── FilterBySeverity / FilterByCode ────────────────────────────────

    def filter_by_severity(
        self, project: TyProject, severity: str
    ) -> CheckResult:
        if not project.is_open:
            raise ValueError("Project is not open")

        key = str(project.root)
        diags = self._diagnostics_by_project.get(key, [])
        filtered = [d for d in diags if d.severity == severity]
        return CheckResult(diagnostics=filtered)

    def filter_by_code(self, project: TyProject, code: str) -> CheckResult:
        if not project.is_open:
            raise ValueError("Project is not open")

        key = str(project.root)
        diags = self._diagnostics_by_project.get(key, [])
        filtered = [d for d in diags if d.code == code]
        return CheckResult(diagnostics=filtered)

    # ── ClearDiagnosticsOnReload ───────────────────────────────────────

    def clear_diagnostics_for_project(self, project: TyProject) -> None:
        """Clear all diagnostics belonging to a project (on reload)."""
        key = str(project.root)
        self._diagnostics_by_project.pop(key, None)
