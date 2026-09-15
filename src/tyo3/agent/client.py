"""Synchronous client for the headless TyO3 agent surface."""

from __future__ import annotations

import json
import os
import selectors
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from queue import Queue
from typing import Any

from tyo3.agent.errors import (
    AgentError,
    DaemonUnavailable,
    EngineError,
    RequestTimeout,
    RevisionEvicted,
    SessionClosed,
)
from tyo3.daemon.server import default_socket_path


class AgentClient:
    """Connect to and query the daemon serving one project root.

    Construction connects to an existing daemon or starts ``tyo3-daemon``.
    Agent-facing writes are intentionally limited to :meth:`sync`, which asks
    the daemon to reindex files the agent has already written on disk. This
    class has no source-text or overlay submission method.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        timeout: float = 30.0,
        startup_timeout: float = 60.0,
        socket: str | Path | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.timeout = timeout
        self.startup_timeout = startup_timeout
        self.socket_path = Path(socket) if socket is not None else default_socket_path(self.root)
        self.instance_id: str | None = None
        self.restarted = False
        self.notifications: Queue[dict[str, Any]] = Queue()

        self._sock: socket.socket | None = None
        self._reader: Any = None
        self._process: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._condition = threading.Condition()
        self._responses: dict[int, dict[str, Any]] = {}
        self._next_id = 0
        self._closed = threading.Event()

        try:
            self._connect_or_start()
            self._bootstrap()
        except AgentError:
            self.close()
            raise
        except Exception as exc:  # noqa: BLE001 - normalize connection failures
            self.close()
            raise DaemonUnavailable(f"could not connect to TyO3 daemon for {self.root}: {exc}") from exc

    def __enter__(self) -> AgentClient:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close this client connection, leaving the resident daemon running."""
        if self._closed.is_set():
            return
        self._closed.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        reader = self._reader
        self._reader = None
        if reader is not None:
            try:
                reader.close()
            except OSError:
                pass
        with self._condition:
            self._condition.notify_all()

    def reconnect(self) -> None:
        """Reconnect and bootstrap after a dropped connection."""
        self.close()
        self._closed.clear()
        self._responses.clear()
        self._connect_or_start()
        self._bootstrap()

    # ── Agent verbs ───────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """Return daemon, project, instance, and current revision status."""
        ping = self._request("ping", {})
        opened = self._request("open", {"root": str(self.root)})
        self._verify_open_root(opened)
        result = dict(opened)
        result.update({key: value for key, value in ping.items() if key not in result})
        result["ok"] = ping.get("ok", True)
        return result

    def sync(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Reindex the working tree after the agent writes files itself."""
        return self._request("reindex", {}, timeout=timeout)

    def find(self, query: str | None = None) -> dict[str, Any]:
        """Find workspace symbols, optionally filtered by a substring."""
        params = {} if query is None else {"query": query}
        return self._request("symbols", params)

    def context(self, params: dict[str, Any]) -> dict[str, Any] | None:
        """Read source, references, and authored context for a position."""
        durable_id = params.get("durable_id")
        if durable_id is not None:
            symbols = self._request("symbols", {})
            if not isinstance(symbols, dict):
                return None
            entry = next((item for item in symbols.get("symbols", []) if item.get("durable_id") == durable_id), None)
            if not isinstance(entry, dict):
                return None
            start = entry.get("range", {}).get("start", {})
            params = {"path": entry.get("path"), "line": start.get("line"), "col": start.get("column")}
        return self._request("context_pack", params)

    def impact(self, params: dict[str, Any]) -> dict[str, Any] | None:
        """Read the transitive semantic dependents of a position or durable ID."""
        return self._request("impact", params)

    def check(self, path: str | None = None) -> dict[str, Any]:
        """Run project or file diagnostics."""
        return self._request("check", {} if path is None else {"path": path})

    def changed(self, since: int, *, to: int | None = None) -> dict[str, Any]:
        """Return entity changes since a retained revision."""
        params: dict[str, Any] = {"from_rev": since}
        if to is not None:
            params["to_rev"] = to
        return self._request("diff", params)

    def note(self, layer: str, durable_id: str, value: Any) -> dict[str, Any]:
        """Record a durable note against an entity identity."""
        return self._request("author", {"layer": layer, "durable_id": durable_id, "value": value})

    def notes(self, layer: str = "intent", *, stale: bool = False) -> dict[str, Any]:
        """List layer notes, or review-state entries when ``stale`` is true."""
        if stale:
            return self._request("review_state", {"path": None})
        return self._request("layer_ids", {"layer": layer, "with_values": True})

    # Direct read helpers keep the client adapter-ready without exposing the
    # Neovim-only overlay routes.
    def entity_at(self, params: dict[str, Any]) -> dict[str, Any] | None:
        return self._request("entity_at", params)

    def symbols(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("symbols", params or {})

    def context_pack(self, params: dict[str, Any]) -> dict[str, Any] | None:
        return self._request("context_pack", params)

    def check_file(self, path: str) -> dict[str, Any]:
        return self._request("check", {"path": path})

    # ── Connection and framing ───────────────────────────────────

    def _connect_or_start(self) -> None:
        try:
            self._connect_socket(self.socket_path, timeout=min(self.timeout, 2.0))
            return
        except OSError:
            pass

        executable = shutil.which("tyo3-daemon")
        command = (
            [executable, "--root", str(self.root), "--socket", str(self.socket_path), "--print-socket"]
            if executable is not None
            else [
                sys.executable,
                "-m",
                "tyo3.daemon",
                "--root",
                str(self.root),
                "--socket",
                str(self.socket_path),
                "--print-socket",
            ]
        )
        env = dict(os.environ)
        source_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = source_root + os.pathsep + env.get("PYTHONPATH", "")
        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            raise DaemonUnavailable(f"could not start tyo3-daemon: {exc}") from exc

        deadline = time.monotonic() + self.startup_timeout
        announced = False
        stdout = self._process.stdout
        selector = selectors.DefaultSelector()
        if stdout is not None:
            selector.register(stdout, selectors.EVENT_READ)
        try:
            while time.monotonic() < deadline:
                if stdout is not None and not announced:
                    events = selector.select(timeout=min(0.25, max(0.0, deadline - time.monotonic())))
                    if events:
                        line = stdout.readline().strip()
                        if line:
                            self.socket_path = Path(line)
                            announced = True
                if announced:
                    try:
                        self._connect_socket(self.socket_path, timeout=0.5)
                        return
                    except OSError:
                        pass
                returncode = self._process.poll()
                if returncode is not None:
                    if returncode == 3:
                        # Another process won the root lock. The announced
                        # default path is still the one to connect to.
                        announced = True
                        try:
                            self._connect_socket(self.socket_path, timeout=0.5)
                            return
                        except OSError:
                            pass
                    else:
                        raise DaemonUnavailable(f"tyo3-daemon exited with status {returncode}")
                time.sleep(0.05)
        finally:
            selector.close()
        raise DaemonUnavailable(f"tyo3-daemon did not listen within {self.startup_timeout:g}s")

    def _connect_socket(self, path: Path, *, timeout: float) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect(str(path))
            sock.settimeout(None)
        except BaseException:
            sock.close()
            raise
        self._sock = sock
        self._reader = sock.makefile("r", encoding="utf-8", newline="\n")
        self._closed.clear()
        self._reader_thread = threading.Thread(target=self._read_loop, name="tyo3-agent-reader", daemon=True)
        self._reader_thread.start()

    def _read_loop(self) -> None:
        reader = self._reader
        if reader is None:
            return
        try:
            for line in reader:
                if not line.strip():
                    continue
                message = json.loads(line)
                if isinstance(message, dict) and message.get("id") is not None:
                    with self._condition:
                        self._responses[message["id"]] = message
                        self._condition.notify_all()
                elif isinstance(message, dict) and "method" in message:
                    self.notifications.put(message)
        except (OSError, ValueError, UnicodeError):
            pass
        finally:
            with self._condition:
                self._condition.notify_all()

    def _request(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> Any:
        if self._closed.is_set() or self._sock is None:
            raise DaemonUnavailable("TyO3 daemon connection is closed")
        wait_for = self.timeout if timeout is None else timeout
        with self._write_lock:
            self._next_id += 1
            request_id = self._next_id
            frame = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n"
            try:
                self._sock.sendall(frame.encode("utf-8"))
            except OSError as exc:
                raise DaemonUnavailable(f"lost connection while sending {method}: {exc}") from exc

        deadline = time.monotonic() + wait_for
        with self._condition:
            while request_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RequestTimeout(method, wait_for)
                if self._closed.is_set():
                    raise DaemonUnavailable(f"lost connection while waiting for {method}")
                self._condition.wait(timeout=remaining)
            response = self._responses.pop(request_id)

        if "error" in response:
            self._raise_rpc_error(method, response["error"])
        result = response.get("result")
        if isinstance(result, dict):
            self._observe_instance(result)
        return result

    def _bootstrap(self) -> None:
        ping = self._request("ping", {})
        if not isinstance(ping, dict):
            raise DaemonUnavailable("daemon ping returned a non-object result")
        opened = self._request("open", {"root": str(self.root)})
        if not isinstance(opened, dict):
            raise DaemonUnavailable("daemon open returned a non-object result")
        self._verify_open_root(opened)

    def _verify_open_root(self, opened: dict[str, Any]) -> None:
        actual = opened.get("root")
        if not isinstance(actual, str) or Path(actual).resolve() != self.root:
            raise AgentError(f"daemon serves {actual!r}, requested {str(self.root)!r}")

    def _observe_instance(self, result: dict[str, Any]) -> None:
        instance_id = result.get("instance_id")
        if not isinstance(instance_id, str):
            return
        if self.instance_id is not None and self.instance_id != instance_id:
            self.restarted = True
        self.instance_id = instance_id

    def _raise_rpc_error(self, method: str, error: Any) -> None:
        if not isinstance(error, dict):
            raise AgentError(f"invalid RPC error for {method}: {error!r}")
        message = str(error.get("message", "daemon request failed"))
        data = error.get("data")
        error_type = data.get("error_type") if isinstance(data, dict) else None
        error_type = error_type if isinstance(error_type, str) else "UnknownEngineError"
        kwargs = {
            "error_type": error_type,
            "method": method,
            "code": error.get("code") if isinstance(error.get("code"), int) else None,
            "data": data,
        }
        if error_type in {"RevisionEvictedError", "RevisionEvicted"}:
            raise RevisionEvicted(message, **kwargs)
        if error_type in {"ProjectClosedError", "SessionClosed"}:
            raise SessionClosed(message, **kwargs)
        raise EngineError(message, **kwargs)


__all__ = ["AgentClient"]
