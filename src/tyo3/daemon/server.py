"""DaemonServer — unix-socket JSON-RPC server + client registry.

Binds a unix-domain socket, accepts clients, and runs one reader thread per
connection. Requests are dispatched through :class:`Handlers` (which funnels all
session work through the :class:`SessionActor`); responses are written back on
the same connection. The :class:`BusPump` broadcasts ``delta`` / ``refinement``
notifications to **every** connected client.

Per-connection writes (a response from a handler thread, a notification from the
pump thread) are serialised by a per-client send lock, so frames never interleave
on the wire.
"""

from __future__ import annotations

import hashlib
import logging
import os
import socket
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from tyo3.bus.interest import Interest
from tyo3.daemon.bus_pump import BusPump
from tyo3.daemon.handlers import Handlers
from tyo3.daemon.protocol import (
    ENGINE_ERROR,
    ProtocolError,
    encode_error,
    encode_notification,
    encode_response,
    parse_request,
)
from tyo3.daemon.session_actor import SessionActor
from tyo3.daemon.tracking import AffectedTracker

if TYPE_CHECKING:
    from tyo3.bus.delta import Delta

log = logging.getLogger("tyo3.daemon")


def default_socket_path(root: str | Path) -> Path:
    """Derive the per-project socket path.

    ``$XDG_RUNTIME_DIR/tyo3/<hash(root)>.sock`` when ``XDG_RUNTIME_DIR`` is set
    (the right place for ephemeral per-user sockets), else a stable temp-dir
    fallback. The hash keys on the resolved root so every editor pane on the
    same project finds the same daemon.
    """
    resolved = str(Path(root).resolve())
    digest = hashlib.sha1(resolved.encode()).hexdigest()[:16]
    base = os.environ.get("XDG_RUNTIME_DIR")
    parent = Path(base) / "tyo3" if base else Path(tempfile.gettempdir()) / "tyo3"
    parent.mkdir(parents=True, exist_ok=True)
    return parent / f"{digest}.sock"


class _Client:
    """One connected socket, with a serialising send lock."""

    def __init__(self, conn: socket.socket) -> None:
        self.conn = conn
        self._send_lock = threading.Lock()
        self.alive = True
        # Per-connection delta filter. Defaults to ALL so a client that never
        # sends ``subscribe`` keeps receiving every committed delta (back-compat
        # — the e2e harness and the Lua plugin rely on this) (AB7).
        self.interest: Interest = Interest.ALL

    def send(self, line: str) -> bool:
        """Write one framed line; return False if the peer is gone."""
        if not self.alive:
            return False
        data = line.encode("utf-8")
        with self._send_lock:
            try:
                self.conn.sendall(data)
                return True
            except OSError:
                self.alive = False
                return False

    def close(self) -> None:
        self.alive = False
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass


class DaemonServer:
    """The socket server owning the actor, handlers, and bus pump."""

    def __init__(
        self,
        root: str | Path,
        *,
        socket_path: str | Path | None = None,
        autostop: bool = False,
    ) -> None:
        self._root = str(Path(root).resolve())
        self._socket_path = Path(socket_path) if socket_path else default_socket_path(self._root)
        self._autostop = autostop

        self._actor = SessionActor(self._root)
        self._tracker = AffectedTracker()
        self._handlers = Handlers(self._actor, tracker=self._tracker)
        self._pump = BusPump(self._actor, self.broadcast, self.broadcast_delta, tracker=self._tracker)

        self._server_sock: socket.socket | None = None
        self._clients: set[_Client] = set()
        self._clients_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._handler_threads: list[threading.Thread] = []

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    # ── Broadcast (called by the pump thread) ──────────────────────

    def broadcast(self, line: str) -> None:
        """Send *line* to every connected client; reap dead ones.

        Used for **refinements** (broadcast-to-all, back-compat — a client that
        didn't receive a revision's delta simply ignores its refinement).
        """
        with self._clients_lock:
            clients = list(self._clients)
        dead = [c for c in clients if not c.send(line)]
        if dead:
            with self._clients_lock:
                for c in dead:
                    self._clients.discard(c)

    def broadcast_delta(self, delta: Delta) -> None:
        """Scope *delta* to each client's ``Interest`` and send its slice (AB7).

        Mirrors ``Bus.publish``'s exact match/scope semantics per connection:
        an ``ALL`` (or ``rescan``) client gets the delta unconditionally (even
        an empty one — so it can pin a snapshot at this revision); a scoped
        client gets a non-empty intersection only when its interest matches the
        delta's affected ids / files / touched layers.
        """
        with self._clients_lock:
            clients = list(self._clients)
        dead = []
        for c in clients:
            interest = c.interest
            if interest.all or delta.rescan:
                scoped = delta.scoped_to(interest)
            elif interest.matches(
                affected_ids=delta.affected,
                affected_files=delta.files,
                touched_layers=delta.layers,
            ):
                scoped = delta.scoped_to(interest)
                if scoped.is_empty():
                    continue
            else:
                continue
            line = encode_notification("delta", _delta_params(scoped))
            if not c.send(line):
                dead.append(c)
        if dead:
            with self._clients_lock:
                for c in dead:
                    self._clients.discard(c)

    # ── Lifecycle ──────────────────────────────────────────────────

    def serve_forever(self) -> None:
        """Open the session, bind the socket, and accept clients until shutdown.

        Blocks the calling thread on the accept loop. Raises if the session
        cannot be opened (surfaced from the actor).
        """
        log.info("opening session at %s", self._root)
        self._actor.start()  # opens + sync_all; raises on failure
        self._pump.start()
        self._bind()
        log.info("listening on %s", self._socket_path)
        try:
            self._accept_loop()
        finally:
            self.shutdown()

    def _bind(self) -> None:
        # A stale socket from a previous crashed daemon blocks bind — remove it.
        try:
            if self._socket_path.exists():
                self._socket_path.unlink()
        except OSError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(str(self._socket_path))
        sock.listen(16)
        sock.settimeout(0.5)  # so the accept loop can observe _shutdown
        self._server_sock = sock

    def _accept_loop(self) -> None:
        assert self._server_sock is not None
        while not self._shutdown.is_set():
            try:
                conn, _ = self._server_sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            client = _Client(conn)
            with self._clients_lock:
                self._clients.add(client)
            t = threading.Thread(target=self._serve_client, args=(client,), name="tyo3-client", daemon=True)
            self._handler_threads.append(t)
            t.start()

    def _serve_client(self, client: _Client) -> None:
        log.debug("client connected")
        try:
            reader = client.conn.makefile("r", encoding="utf-8", newline="\n")
            for line in reader:
                if self._shutdown.is_set():
                    break
                if not line.strip():
                    continue
                self._handle_line(client, line)
        except OSError:
            pass
        finally:
            self._drop_client(client)

    def _drop_client(self, client: _Client) -> None:
        client.close()
        with self._clients_lock:
            self._clients.discard(client)
            remaining = len(self._clients)
        log.debug("client disconnected (%d remaining)", remaining)
        if self._autostop and remaining == 0:
            log.info("last client disconnected; --autostop ⇒ shutting down")
            self._shutdown.set()
            # Nudge the accept loop out of its blocking accept.
            self._wake_accept()

    def _wake_accept(self) -> None:
        # Connect-and-close to break a blocking accept() if the timeout path
        # isn't taken; harmless if the server is already down.
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(str(self._socket_path))
        except OSError:
            pass

    def _handle_line(self, client: _Client, line: str) -> None:
        """Parse, dispatch, and reply to one request line."""
        try:
            req = parse_request(line)
        except ProtocolError as e:
            client.send(encode_error(e.request_id, e.code, e.message))
            return
        if req.is_notification:
            # Editor → daemon notifications are not part of this protocol; ignore.
            return
        # ``subscribe`` is handled at the server, not in ``Handlers``: Handlers is
        # a single shared instance dispatched for every connection and has no
        # per-connection identity, but ``subscribe`` must set *this* client's
        # interest (AB7). An empty ``subscribe {}`` ⇒ Interest() = matches
        # nothing (a client mutes itself); ``subscribe {"all": true}`` restores
        # ALL.
        if req.method == "subscribe":
            p = req.params or {}
            client.interest = Interest(
                files=frozenset(p.get("files", ())),
                ids=frozenset(p.get("ids", ())),
                layers=frozenset(p.get("layers", ())),
                all=bool(p.get("all", False)),
            )
            client.send(encode_response(req.id, {"ok": True}))
            return
        try:
            result = self._handlers.dispatch(req.method, req.params)
            client.send(encode_response(req.id, result))
        except ProtocolError as e:
            client.send(encode_error(req.id, e.code, e.message))
        except Exception as e:  # noqa: BLE001 — engine/handler error → typed error frame
            log.exception("handler error for method %s", req.method)
            client.send(encode_error(req.id, ENGINE_ERROR, f"{type(e).__name__}: {e}", data={"method": req.method}))

    def shutdown(self) -> None:
        """Stop the server: close clients, pump, actor, and remove the socket."""
        if self._shutdown.is_set() and self._server_sock is None:
            return
        self._shutdown.set()
        # Stop accepting.
        sock = self._server_sock
        self._server_sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        # Close clients.
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for c in clients:
            c.close()
        # Stop the pump, then the actor (closes the session).
        try:
            self._pump.stop()
        except Exception:
            log.exception("error stopping bus pump")
        try:
            self._actor.stop()
        except Exception:
            log.exception("error stopping session actor")
        # Remove the socket file.
        try:
            if self._socket_path.exists():
                self._socket_path.unlink()
        except OSError:
            pass
        log.info("daemon stopped")


def _delta_params(delta: Delta) -> dict:
    """Encode a (scoped) ``Delta`` into the ``delta`` notification params.

    Lives at the server because encoding is now per connection — each client's
    interest produces a different scoped delta (AB7). Previously built once in
    ``BusPump._emit_delta``.
    """
    return {
        "revision": delta.revision,
        "created_ids": sorted(delta.created),
        "changed_ids": sorted(delta.changed),
        "deleted_ids": sorted(delta.deleted),
        "moved_ids": sorted(delta.moved),
        "authored_ids": sorted(delta.authored),
        "affected_ids": sorted(delta.affected),
        "touched_files": sorted(delta.files),
        "rescan": delta.rescan,
    }


__all__ = ["DaemonServer", "default_socket_path"]
