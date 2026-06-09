"""Public TyO3 exception hierarchy.

The native PyO3 exception classes are canonical. This module re-exports them so
exceptions raised from Rust can be caught through ``tyo3.exceptions`` without a
parallel Python-only hierarchy.
"""

from __future__ import annotations

try:
    from tyo3._native_impl import (
        CommitFailed,
        ConfigError,
        FormatVersionError,
        PathResolutionError,
        PositionError,
        ProjectClosedError,
        ReconcileAmbiguous,
        RevisionEvictedError,
        SidecarWriteError,
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

    class SidecarWriteError(TyO3Error):
        """Raised when a commit's sidecar persistence (identity registry or an
        authored record) fails to write atomically; the commit rolls back (§5.10)."""

    class CommitFailed(TyO3Error):
        """Raised when an in-lock commit step fails and the whole commit rolls
        back to the prior revision with no torn publish (§5.3)."""

    class ReconcileAmbiguous(TyO3Error):
        """Raised when identity reconciliation cannot bind deterministically (§5.5)."""


class ProjectOpenError(TyO3Error):
    """Raised when a project cannot be opened for non-config reasons."""


class StoreError(TyO3Error):
    """Base class for artifact-store failures (§5.12).

    Distinguishes the three store conditions so callers never conflate them:
    a genuinely absent artifact (``get`` returns ``None`` — *not* an error),
    a missing optional backend (:class:`StoreBackendUnavailable`), and a
    backend that is present but failing (:class:`StoreBackendBroken`).
    """


class StoreBackendUnavailable(StoreError):
    """Raised when an optional store backend dependency is not installed."""


class StoreBackendBroken(StoreError):
    """Raised when a present store backend fails an IO/query/write operation.

    A genuine "not found" is never this — it is a ``None`` return. This is for
    permission errors, corrupt state, query failures, etc., which must propagate
    rather than masquerade as "missing" (§5.12, the silent-no-op class).
    """


class GeneratorFailed(TyO3Error):
    """A derived-layer generator failed (timeout, non-zero exit, HTTP error).

    Carries the layer name and the offending input DurableIds so callers
    can mark exactly the affected artifacts failed while leaving prior
    artifacts intact (§9.2.6).
    """

    def __init__(self, message: str, *, layer: str, input_ids: list[str]) -> None:
        super().__init__(message)
        self.layer = layer
        self.input_ids = input_ids


class AnalysisError(TyO3Error):
    """Raised when a type-checking operation fails."""


class InternalTyError(TyO3Error):
    """Raised when the underlying ty/Ruff engine encounters an unexpected error."""


class SchemaValidationError(TyO3Error):
    """Raised when an authored value fails its layer's declared schema (AB5).

    Opt-in: only fires for a registered layer whose spec carries a ``schema``.
    Un-schema'd layers stay free-form JSON and never raise this."""


__all__ = [
    "TyO3Error",
    "ConfigError",
    "FormatVersionError",
    "StoreError",
    "StoreBackendUnavailable",
    "StoreBackendBroken",
    "ProjectOpenError",
    "ProjectClosedError",
    "PathResolutionError",
    "PositionError",
    "RevisionEvictedError",
    "SidecarWriteError",
    "CommitFailed",
    "ReconcileAmbiguous",
    "AnalysisError",
    "InternalTyError",
    "SchemaValidationError",
]
