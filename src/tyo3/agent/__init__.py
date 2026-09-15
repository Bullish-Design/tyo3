"""Headless agent access to a TyO3 daemon.

The agent owns the filesystem: it writes source files with its own tools.
TyO3 never writes source files, and the agent never sends overlay text through
``sync_buffer`` or ``sync_buffers``. Those methods are the Neovim unsaved-buffer
path; agents use :meth:`AgentClient.sync` to reindex the working tree instead.
"""

from tyo3.agent.client import AgentClient
from tyo3.agent.errors import (
    AgentError,
    DaemonUnavailable,
    EngineError,
    RequestTimeout,
    RevisionEvicted,
    SessionClosed,
)

__all__ = [
    "AgentClient",
    "AgentError",
    "DaemonUnavailable",
    "EngineError",
    "RequestTimeout",
    "RevisionEvicted",
    "SessionClosed",
]
