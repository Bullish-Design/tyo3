"""Public TyO3 exception hierarchy.

The native PyO3 exception classes are canonical. This module re-exports them so
exceptions raised from Rust can be caught through ``tyo3.exceptions`` without a
parallel Python-only hierarchy.
"""

from __future__ import annotations

try:
    from tyo3._native_impl import (
        ConfigError,
        FormatVersionError,
        PathResolutionError,
        PositionError,
        ProjectClosedError,
        RevisionEvictedError,
        TyO3Error,
    )
except ImportError:
    class TyO3Error(Exception):
        """Base exception for all TyO3 errors."""

    class ConfigError(TyO3Error):
        """Raised when `.tyo3/config.toml` is malformed or invalid."""

    class FormatVersionError(TyO3Error):
        """Raised when a config or sidecar format version is newer than supported."""

    class ProjectClosedError(TyO3Error):
        """Raised when an operation is attempted on a closed project."""

    class PathResolutionError(TyO3Error):
        """Raised when a file path cannot be resolved within the project."""

    class PositionError(TyO3Error):
        """Raised when a line/column position is invalid."""

    class RevisionEvictedError(TyO3Error):
        """Raised when a requested MVCC revision is no longer retained."""


class ProjectOpenError(TyO3Error):
    """Raised when a project cannot be opened for non-config reasons."""


class StoreBackendUnavailable(TyO3Error):
    """Raised when an optional store backend dependency is not installed."""


class AnalysisError(TyO3Error):
    """Raised when a type-checking operation fails."""


class InternalTyError(TyO3Error):
    """Raised when the underlying ty/Ruff engine encounters an unexpected error."""


__all__ = [
    "TyO3Error",
    "ConfigError",
    "FormatVersionError",
    "StoreBackendUnavailable",
    "ProjectOpenError",
    "ProjectClosedError",
    "PathResolutionError",
    "PositionError",
    "RevisionEvictedError",
    "AnalysisError",
    "InternalTyError",
]
