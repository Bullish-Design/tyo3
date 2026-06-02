"""Core domain models: project lifecycle, configuration, file management.

Derived from tyo3-core.allium
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, Field

# ── Value Types ──────────────────────────────────────────────────────────


class TyProjectConfig(BaseModel):
    """Configuration for opening a TyO3 project."""

    python_version: str | None = None
    config_path: PurePosixPath | None = None
    extra_search_paths: set[PurePosixPath] = Field(default_factory=set)
    respect_gitignore: bool = True
    force_exclude: bool = False
    check_all_files: bool = True


class BackendInfo(BaseModel):
    """Metadata about the ty backend."""

    tyo3_version: str
    ty_version: str
    ty_commit: str
    ruff_submodule_commit: str
    backend_source: str


# ── Enums ────────────────────────────────────────────────────────────────


class ProjectStatus(StrEnum):
    """Lifecycle status of a TyO3 project."""

    CLOSED = "closed"
    OPEN = "open"
    ERROR = "error"


class FileCategory(StrEnum):
    """Classification of a project file."""

    FIRST_PARTY = "first_party"
    VENDORED = "vendored"
    STUB = "stub"
    DEPENDENCY = "dependency"


# ── Entities ─────────────────────────────────────────────────────────────


class TyProject(BaseModel):
    """A TyO3 project session — the root object owned by one project root."""

    root: PurePosixPath
    status: ProjectStatus
    coordinate_mode: str = "python"
    python_version: str | None = None
    config_path: PurePosixPath | None = None
    extra_search_paths: set[PurePosixPath] = Field(default_factory=set)
    respect_gitignore: bool = True
    force_exclude: bool = False
    check_all_files: bool = True
    opened_at: datetime
    last_reloaded_at: datetime | None = None

    # Derived properties
    @property
    def is_open(self) -> bool:
        return self.status == ProjectStatus.OPEN

    @property
    def has_error(self) -> bool:
        return self.status == ProjectStatus.ERROR


class ProjectFile(BaseModel):
    """A discovered Python file belonging to a TyO3 project."""

    path: PurePosixPath
    project: TyProject
    file_category: FileCategory
    last_checked_at: datetime | None = None
