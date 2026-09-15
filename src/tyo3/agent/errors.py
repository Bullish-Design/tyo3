"""Typed failures raised by :mod:`tyo3.agent.client`."""

from __future__ import annotations


class AgentError(Exception):
    """Base for every client-side failure."""


class DaemonUnavailable(AgentError):
    """The daemon is not running and could not be started."""


class EngineError(AgentError):
    """The daemon raised an engine error described by typed wire metadata."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str,
        method: str,
        code: int | None = None,
        data: object | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.method = method
        self.code = code
        self.data = data


class RevisionEvicted(EngineError):
    """The requested revision is no longer retained (256-revision window)."""


class SessionClosed(EngineError):
    """The daemon closed the project."""


class RequestTimeout(AgentError):
    """The result is UNKNOWN; it is not a cancellation.

    The daemon may have committed. Reconcile with ``status`` and ``changed``
    before retrying.
    """

    def __init__(self, method: str, timeout: float) -> None:
        super().__init__(f"request {method!r} timed out after {timeout:g}s; result is UNKNOWN")
        self.method = method
        self.timeout = timeout
