"""Project lifecycle service — tyo3-core.allium rules.

DEPRECATED: Use tyo3.TyO3Session instead. This module will be removed in v0.2.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath

from tyo3.models.core import (
    BackendInfo,
    FileCategory,
    ProjectFile,
    ProjectStatus,
    TyProject,
    TyProjectConfig,
)

# ── Black-box stubs (used when `use_rust=False`) ────────────────────────


def project_files(root: PurePosixPath, config: TyProjectConfig) -> list[PurePosixPath]:
    """Black-box: discover Python files belonging to a project root.

    In production the real implementation calls ty's native file discovery.
    For testing, this is a stub.
    """
    return []


def is_directory(path: PurePosixPath) -> bool:
    """Black-box: returns True if the path is a directory on disk."""
    return True


def is_first_party(path: PurePosixPath) -> bool:
    """Black-box: returns True if the file is a first-party project file."""
    return True


def is_vendored(path: PurePosixPath) -> bool:
    """Black-box: returns True if the file is vendored."""
    return False


def is_stub(path: PurePosixPath) -> bool:
    """Black-box: returns True if the file is a type stub (.pyi)."""
    return False


def is_dependency(path: PurePosixPath) -> bool:
    """Black-box: returns True if the file is a dependency."""
    return False


# ── ProjectService ───────────────────────────────────────────────────────


class ProjectService:
    """Implements project lifecycle rules from tyo3-core.allium.

    Parameters
    ----------
    use_rust:
        When ``True``, operations are backed by the Rust ty engine via
        :class:`~tyo3.rust_project.RustProject`.  Default ``False`` keeps
        existing black-box stubs for unit testing.
    """

    def __init__(self, use_rust: bool = False) -> None:
        self._projects: list[TyProject] = []
        self._use_rust: bool = use_rust
        # RustProject instances keyed by root path string
        self._rust_projects: dict[str, object] = {}

    # ── Rust backend access ──────────────────────────────────────────

    def _get_rust_project(self, root: PurePosixPath) -> object | None:
        """Return the RustProject for *root*, or ``None``."""
        key = str(root)
        return self._rust_projects.get(key)

    def _register_rust_project(self, root: PurePosixPath, rp: object) -> None:
        """Store a RustProject instance keyed by root."""
        self._rust_projects[str(root)] = rp

    def _remove_rust_project(self, root: PurePosixPath) -> None:
        """Remove the stored RustProject for *root*."""
        self._rust_projects.pop(str(root), None)

    # ── Query helpers ──────────────────────────────────────────────────

    def find_open_project(self, root: PurePosixPath) -> TyProject | None:
        for p in self._projects:
            if p.root == root and p.is_open:
                return p
        return None

    def find_project(self, root: PurePosixPath) -> TyProject | None:
        for p in self._projects:
            if p.root == root:
                return p
        return None

    @property
    def projects(self) -> list[TyProject]:
        return list(self._projects)

    # ── OpenProject ────────────────────────────────────────────────────

    def open_project(
        self, root: PurePosixPath, config: TyProjectConfig | None = None
    ) -> tuple[TyProject, list[ProjectFile]]:
        """OpenProject: requires root is a directory and no open project exists for root."""
        # ── Precondition: path is a directory ──
        if not self._use_rust and not is_directory(root):
            raise ValueError(f"Path not found: {root}")

        # ── Precondition: no duplicate open project ──
        existing = self.find_open_project(root)
        if existing is not None:
            raise ValueError(f"Project already open: {root}")

        default_config = TyProjectConfig()
        effective = config if config is not None else default_config

        # ── Open Rust backend (if available) ──
        if self._use_rust:
            from tyo3.rust_project import RustProject

            rp = RustProject(str(root))
            self._register_rust_project(root, rp)

            # Use Rust for file discovery
            discovered_paths: list[PurePosixPath] = []
            for f in rp.files():
                discovered_paths.append(PurePosixPath(f))
        else:
            discovered_paths = project_files(root, effective)

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
            opened_at=datetime.now(UTC),
        )
        self._projects.append(project)

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
            files.append(ProjectFile(path=path, project=project, file_category=cat))

        return project, files

    # ── ReloadProject ──────────────────────────────────────────────────

    def reload_project(self, project: TyProject) -> TyProject:
        """ReloadProject: requires project.is_open."""
        if not project.is_open:
            raise ValueError("Project is not open")

        # Reload Rust backend if available
        rp = self._get_rust_project(project.root)
        if rp is not None:
            rp.reload()  # type: ignore[union-attr]

        project.last_reloaded_at = datetime.now(UTC)
        project.status = ProjectStatus.OPEN
        return project

    # ── CloseProject ───────────────────────────────────────────────────

    def close_project(self, project: TyProject) -> TyProject:
        """CloseProject: requires project.is_open."""
        if not project.is_open:
            raise ValueError("Project is not open")

        # Close Rust backend if available
        rp = self._get_rust_project(project.root)
        if rp is not None:
            rp.close()  # type: ignore[union-attr]
            self._remove_rust_project(project.root)

        project.status = ProjectStatus.CLOSED
        return project

    # ── ListFiles / FilterFilesByCategory ──────────────────────────────

    def list_files(self, project: TyProject) -> list[ProjectFile]:
        if not project.is_open:
            raise ValueError("Project is not open")

        rp = self._get_rust_project(project.root)
        if rp is not None:
            result: list[ProjectFile] = []
            for f in rp.files():  # type: ignore[union-attr]
                result.append(
                    ProjectFile(
                        path=PurePosixPath(f),
                        project=project,
                        file_category=FileCategory.FIRST_PARTY,
                    )
                )
            return result

        return [f for f in self._get_all_files() if f.project.root == project.root]

    def filter_files_by_category(self, project: TyProject, category: str) -> list[ProjectFile]:
        if not project.is_open:
            raise ValueError("Project is not open")
        return [f for f in self._get_all_files() if f.project.root == project.root and f.file_category == category]

    def _get_all_files(self) -> list[ProjectFile]:
        """Return all files across all projects. Stub-mode only."""
        return []

    # ── QueryBackendInfo ───────────────────────────────────────────────

    def query_backend_info(self) -> BackendInfo:
        return BackendInfo(
            tyo3_version="0.1.0",
            ty_version="0.0.40",
            ty_commit="7b95bc219d1dcebc3ce39d222c66c14a3825c9a0",
            ruff_submodule_commit="3cb09eba689ebb49e799131092121928cc789c18",
            backend_source="astral-sh/ty@0.0.40",
        )
