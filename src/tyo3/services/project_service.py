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

# ── Black-box stubs (used when `use_rust=False`) ────────────────────────


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

    def _get_rust_project(self, root: Path) -> Optional[object]:
        """Return the RustProject for *root*, or ``None``."""
        key = str(root)
        return self._rust_projects.get(key)

    def _register_rust_project(self, root: Path, rp: object) -> None:
        """Store a RustProject instance keyed by root."""
        self._rust_projects[str(root)] = rp

    def _remove_rust_project(self, root: Path) -> None:
        """Remove the stored RustProject for *root*."""
        self._rust_projects.pop(str(root), None)

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
            from pathlib import Path as StdPath

            real_path = StdPath(*root.components)  # type: ignore[arg-type]
            rp = RustProject(real_path)
            self._register_rust_project(root, rp)

            # Use Rust for file discovery
            discovered_paths: list[Path] = []
            for f in rp.files():
                discovered_paths.append(Path(components=StdPath(f).parts))  # type: ignore[arg-type]
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
            opened_at=datetime.now(timezone.utc),
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
            files.append(
                ProjectFile(path=path, project=project, file_category=cat)
            )

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

        project.last_reloaded_at = datetime.now(timezone.utc)
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
                from pathlib import Path as StdPath
                p = StdPath(f)
                result.append(
                    ProjectFile(
                        path=Path(components=list(p.parts)),  # type: ignore[arg-type]
                        project=project,
                        file_category=FileCategory.FIRST_PARTY,
                    )
                )
            return result

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
