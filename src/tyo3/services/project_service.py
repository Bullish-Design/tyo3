"""Project lifecycle service — tyo3-core.allium rules."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from tyo3.models.core import (
    BackendInfo,
    FileCategory,
    Path,
    ProjectFile,
    ProjectStatus,
    TyProject,
    TyProjectConfig,
)


def project_files(root: Path, config: TyProjectConfig) -> list[Path]:
    """Black-box: discover Python files belonging to a project root.

    In production the real implementation calls ty's native file discovery.
    For testing, this is a stub.
    """
    return []


def is_directory(path: Path) -> bool:
    """Black-box: returns True if the path is a directory on disk."""
    return True


def is_first_party(path: Path) -> bool:
    """Black-box: returns True if the file is a first-party project file."""
    return True


def is_vendored(path: Path) -> bool:
    """Black-box: returns True if the file is vendored."""
    return False


def is_stub(path: Path) -> bool:
    """Black-box: returns True if the file is a type stub (.pyi)."""
    return False


def is_dependency(path: Path) -> bool:
    """Black-box: returns True if the file is a dependency."""
    return False


class ProjectService:
    """Implements project lifecycle rules from tyo3-core.allium."""

    def __init__(self) -> None:
        self._projects: list[TyProject] = []

    # ── Query helpers ──────────────────────────────────────────────────

    def find_open_project(self, root: Path) -> Optional[TyProject]:
        for p in self._projects:
            if p.root == root and p.is_open:
                return p
        return None

    def find_project(self, root: Path) -> Optional[TyProject]:
        for p in self._projects:
            if p.root == root:
                return p
        return None

    @property
    def projects(self) -> list[TyProject]:
        return list(self._projects)

    # ── OpenProject ────────────────────────────────────────────────────

    def open_project(
        self, root: Path, config: Optional[TyProjectConfig] = None
    ) -> tuple[TyProject, list[ProjectFile]]:
        """OpenProject: requires root is a directory and no open project exists for root."""
        if not is_directory(root):
            raise ValueError(f"Path not found: {root}")

        existing = self.find_open_project(root)
        if existing is not None:
            raise ValueError(f"Project already open: {root}")

        default_config = TyProjectConfig()
        effective = config if config is not None else default_config

        project = TyProject(
            root=root,
            status=ProjectStatus.OPEN,
            coordinate_mode="python",
            python_version=effective.python_version,
            config_path=effective.config_path,
            extra_search_paths=effective.extra_search_paths,
            respect_gitignore=effective.respect_gitignore,
            force_exclude=effective.force_exclude,
            check_all_files=effective.check_all_files,
            opened_at=datetime.now(timezone.utc),
        )
        self._projects.append(project)

        discovered_paths = project_files(root, effective)
        files: list[ProjectFile] = []
        for path in discovered_paths:
            if is_first_party(path):
                cat = FileCategory.FIRST_PARTY
            elif is_vendored(path):
                cat = FileCategory.VENDORED
            elif is_stub(path):
                cat = FileCategory.STUB
            else:
                cat = FileCategory.DEPENDENCY
            files.append(
                ProjectFile(path=path, project=project, file_category=cat)
            )

        return project, files

    # ── ReloadProject ──────────────────────────────────────────────────

    def reload_project(self, project: TyProject) -> TyProject:
        """ReloadProject: requires project.is_open."""
        if not project.is_open:
            raise ValueError("Project is not open")

        project.last_reloaded_at = datetime.now(timezone.utc)
        project.status = ProjectStatus.OPEN
        return project

    # ── CloseProject ───────────────────────────────────────────────────

    def close_project(self, project: TyProject) -> TyProject:
        """CloseProject: requires project.is_open."""
        if not project.is_open:
            raise ValueError("Project is not open")

        project.status = ProjectStatus.CLOSED
        return project

    # ── ListFiles / FilterFilesByCategory ──────────────────────────────

    def list_files(self, project: TyProject) -> list[ProjectFile]:
        if not project.is_open:
            raise ValueError("Project is not open")
        return [f for f in self._get_all_files() if f.project.root == project.root]

    def filter_files_by_category(
        self, project: TyProject, category: str
    ) -> list[ProjectFile]:
        if not project.is_open:
            raise ValueError("Project is not open")
        return [
            f
            for f in self._get_all_files()
            if f.project.root == project.root and f.file_category == category
        ]

    def _get_all_files(self) -> list[ProjectFile]:
        all_files: list[ProjectFile] = []
        for p in self._projects:
            all_files.extend(p.files) if hasattr(p, "files") else None
        return all_files

    # ── QueryBackendInfo ───────────────────────────────────────────────

    def query_backend_info(self) -> BackendInfo:
        return BackendInfo(
            tyo3_version="0.1.0",
            ty_version="0.0.40",
            ty_commit="7b95bc219d1dcebc3ce39d222c66c14a3825c9a0",
            ruff_submodule_commit="3cb09eba689ebb49e799131092121928cc789c18",
            backend_source="astral-sh/ty@0.0.40",
        )
