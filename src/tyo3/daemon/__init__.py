"""``tyo3d`` — the TyO3 daemon.

A long-running process that owns exactly one :class:`~tyo3.TyO3Session` per
project root and projects it to editors over a unix-domain socket. It wraps the
existing engine — it does **not** change it: a socket server speaking
newline-delimited JSON-RPC 2.0, a single-threaded session actor that serialises
all session access, and a bus pump that forwards committed deltas (and async
precision refinements) to connected clients as notifications.

Entry point: ``python -m tyo3.daemon --root <dir>`` (or the ``tyo3-daemon``
console script). See ``.scratch/projects/20-neovim-integration/`` for the design.
"""

from __future__ import annotations

from tyo3.daemon.handlers import Handlers
from tyo3.daemon.protocol import Request, parse_request
from tyo3.daemon.session_actor import SessionActor

__all__ = [
    "Handlers",
    "Request",
    "parse_request",
    "SessionActor",
]
