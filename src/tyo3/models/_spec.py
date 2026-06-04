"""Spec-anticipation models: deferred backend implementations.

These models are defined from the Allium specification but do not yet have
a Rust backend implementation.  They are kept here so the spec shape is
preserved for future work without cluttering the active API surface.

When a backend implementation arrives, the relevant models should be
promoted to the appropriate active module (e.g. ``core.py``).
"""

from __future__ import annotations

from pathlib import PurePosixPath

from pydantic import BaseModel, Field

# ── Config / Metadata (no backend yet) ────────────────────────────────────


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


__all__ = [
    "TyProjectConfig",
    "BackendInfo",
]
