"""TyO3 exception hierarchy for mapping Rust errors to Python exceptions.

Derived from RUST_BACKEND_IMPLEMENTATION.md §6.3
"""

from __future__ import annotations


class TyO3Error(Exception):
    """Base exception for all TyO3 errors."""


class ProjectOpenError(TyO3Error):
    """Raised when a project cannot be opened (invalid path, corrupt config, etc.)."""


class ProjectClosedError(TyO3Error):
    """Raised when an operation is attempted on a closed project."""


class PathResolutionError(TyO3Error):
    """Raised when a file path cannot be resolved within the project."""


class PositionError(TyO3Error):
    """Raised when a line/column position is invalid (out of bounds, zero, etc.)."""


class AnalysisError(TyO3Error):
    """Raised when a type-checking operation fails."""


class InternalTyError(TyO3Error):
    """Raised when the underlying ty/Ruff engine encounters an unexpected error."""

__all__ = [
    "TyO3Error",
    "ProjectOpenError",
    "ProjectClosedError",
    "PathResolutionError",
    "PositionError",
    "AnalysisError",
    "InternalTyError",
]
