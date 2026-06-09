"""JSON-RPC 2.0 wire protocol for ``tyo3d``.

The daemon speaks **newline-delimited JSON-RPC 2.0** over a unix-domain socket:
one JSON object per line (``\\n``-terminated). Requests (editor → daemon) carry an
``id`` and get a matching response; the bus pump emits **notifications**
(daemon → editor) with no ``id``.

This module is pure (de)serialisation — no sockets, no session. It is the one
place the wire shape is defined, so the handler layer and the Lua client agree
byte-for-byte.

Positions on the wire are **1-based** (line and column), matching TyO3's native
convention (``models.analysis.Position``). The Lua client converts from Neovim's
0-based rows/columns before sending.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

JSONRPC_VERSION = "2.0"

# ── Standard JSON-RPC 2.0 error codes ───────────────────────────────────────
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# Application-level: a TyO3 engine call raised (session error, bad id, …).
ENGINE_ERROR = -32000
SERVER_CLOSING = -32001


class ProtocolError(Exception):
    """A malformed JSON-RPC frame (parse / shape error).

    Carries a JSON-RPC error ``code`` so the server can answer the offending
    request — or, for an unparseable line, an id-less error frame.
    """

    def __init__(self, message: str, *, code: int = INVALID_REQUEST, request_id: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id


@dataclass(frozen=True)
class Request:
    """A decoded JSON-RPC request (or notification when ``id is None``)."""

    method: str
    params: dict[str, Any] = field(default_factory=dict)
    id: Any = None

    @property
    def is_notification(self) -> bool:
        return self.id is None


def parse_request(line: str) -> Request:
    """Decode one newline-delimited JSON-RPC request line into a :class:`Request`.

    Raises :class:`ProtocolError` (with the appropriate code, and the request id
    when it could be recovered) on a malformed frame.
    """
    text = line.strip()
    if not text:
        raise ProtocolError("empty frame", code=INVALID_REQUEST)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"parse error: {e}", code=PARSE_ERROR) from e
    if not isinstance(obj, dict):
        raise ProtocolError("request must be a JSON object", code=INVALID_REQUEST)

    req_id = obj.get("id")
    method = obj.get("method")
    if not isinstance(method, str) or not method:
        raise ProtocolError("missing or invalid 'method'", code=INVALID_REQUEST, request_id=req_id)
    params = obj.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ProtocolError("'params' must be an object", code=INVALID_PARAMS, request_id=req_id)
    return Request(method=method, params=params, id=req_id)


def encode_response(request_id: Any, result: Any) -> str:
    """Encode a successful response frame (newline-terminated)."""
    return json.dumps({"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}) + "\n"


def encode_error(request_id: Any, code: int, message: str, data: Any = None) -> str:
    """Encode an error response frame (newline-terminated)."""
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return json.dumps({"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": err}) + "\n"


def encode_notification(method: str, params: dict[str, Any]) -> str:
    """Encode a notification frame — no ``id`` (newline-terminated)."""
    return json.dumps({"jsonrpc": JSONRPC_VERSION, "method": method, "params": params}) + "\n"


__all__ = [
    "JSONRPC_VERSION",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "ENGINE_ERROR",
    "SERVER_CLOSING",
    "ProtocolError",
    "Request",
    "parse_request",
    "encode_response",
    "encode_error",
    "encode_notification",
]
