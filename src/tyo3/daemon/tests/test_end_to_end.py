"""End-to-end daemon test — spawn the process, connect a socket, full flow.

Spawns ``python -m tyo3.daemon`` as a real subprocess, connects a raw unix-socket
JSON-RPC client, and drives the whole reactive path:

    ping → open → decorate → sync_buffer → receive `delta` notification
         → receive `refinement` (precision=method) → entity_at → author → decorate

Asserts the push notification actually arrives over the wire and is id-level.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any

import pytest

import tyo3
from tyo3.daemon.tests.conftest import needs_native

pytestmark = needs_native

_SRC = str(Path(tyo3.__file__).resolve().parent.parent)


class _SocketClient:
    """A minimal newline-delimited JSON-RPC client with a reader thread.

    Responses are matched by id; notifications (no id) land on a queue.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._reader = sock.makefile("r", encoding="utf-8", newline="\n")
        self._writer = sock.makefile("w", encoding="utf-8", newline="\n")
        self._id = 0
        self._responses: dict[int, Any] = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self.notifications: Queue = Queue()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        try:
            for line in self._reader:
                if not line.strip():
                    continue
                obj = json.loads(line)
                if "id" in obj and obj["id"] is not None:
                    with self._cond:
                        self._responses[obj["id"]] = obj
                        self._cond.notify_all()
                elif "method" in obj:
                    self.notifications.put(obj)
        except (OSError, ValueError):
            pass

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 10.0) -> Any:
        with self._lock:
            self._id += 1
            req_id = self._id
        frame = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        self._writer.write(frame + "\n")
        self._writer.flush()
        deadline = time.monotonic() + timeout
        with self._cond:
            while req_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"no response to {method} within {timeout}s")
                self._cond.wait(timeout=remaining)
            resp = self._responses.pop(req_id)
        if "error" in resp:
            raise RuntimeError(f"rpc error for {method}: {resp['error']}")
        return resp["result"]

    def wait_notification(self, method: str, predicate, *, timeout: float = 10.0) -> dict[str, Any]:
        """Block until a notification of *method* satisfying *predicate* arrives."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no '{method}' notification within {timeout}s")
            try:
                note = self.notifications.get(timeout=remaining)
            except Empty:
                continue
            if note.get("method") == method and predicate(note["params"]):
                return note

    def close(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


def _spawn_daemon(root: Path, socket_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tyo3.daemon",
            "--root",
            str(root),
            "--socket",
            str(socket_path),
            "--print-socket",
            "--log-level",
            "WARNING",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc


def _connect(socket_path: Path, *, timeout: float = 30.0) -> socket.socket:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_path.exists():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(socket_path))
                return s
            except OSError:
                pass
        time.sleep(0.1)
    raise TimeoutError(f"daemon socket never appeared at {socket_path}")


@pytest.fixture
def daemon(shop_project: Path, tmp_path: Path):
    socket_path = tmp_path / "tyo3.sock"
    proc = _spawn_daemon(shop_project, socket_path)
    client = None
    try:
        sock = _connect(socket_path)
        client = _SocketClient(sock)
        yield client
    finally:
        if client is not None:
            client.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


CATALOG_PRICE_250 = (
    "class Item:\n"
    "    def price(self) -> int:\n"
    "        return 250\n"
    "\n"
    "    def label(self) -> str:\n"
    '        return "item"\n'
)


def test_full_flow_over_socket(daemon: _SocketClient):
    # ── ping / open ──────────────────────────────────────────────────────
    health = daemon.request("ping")
    assert health["ok"] is True
    assert "engine_version" in health

    opened = daemon.request("open", {"root": "."})
    assert opened["revision"] >= 1
    assert any(f.endswith("store.py") for f in opened["files"])

    # ── resolve checkout via decorate, then entity_at ────────────────────
    deco = daemon.request("decorate", {"path": "store.py"})
    checkout = next(d for d in deco if d["name"] == "checkout")
    checkout_id = checkout["durable_id"]
    pos = checkout["range"]["start"]
    card = daemon.request("entity_at", {"path": "store.py", "line": pos["line"], "col": pos["column"]})
    assert card["durable_id"] == checkout_id
    assert card["qualified_name"] == "checkout"

    # ── sync_buffer (edit Item.price) → delta over the wire ──────────────
    price_id = next(d["durable_id"] for d in daemon.request("decorate", {"path": "catalog.py"}) if d["name"] == "price")
    delta = daemon.request("sync_buffer", {"path": "catalog.py", "text": CATALOG_PRICE_250})
    edit_rev = delta["revision"]
    assert price_id in delta["changed_ids"]
    assert checkout_id in set(delta["affected_ids"]), "the importer is in the affected closure"

    # The bus pump must push a `delta` notification for the same revision.
    note = daemon.wait_notification("delta", lambda p: p["revision"] == edit_rev, timeout=10.0)
    assert price_id in set(note["params"]["changed_ids"])
    assert checkout_id in set(note["params"]["affected_ids"]), "notification is id-level"

    # precision=method ⇒ an async refinement should arrive for the same rev.
    ref = daemon.wait_notification("refinement", lambda p: p["revision"] == edit_rev, timeout=15.0)
    assert checkout_id in set(ref["params"]["narrowed"]), "the price user survives narrowing"

    # ── author a note → authored + decorate reflect it ───────────────────
    daemon.request("author", {"layer": "intent", "durable_id": checkout_id, "value": {"note": "money shot"}})
    got = daemon.request("authored", {"layer": "intent", "durable_id": checkout_id})
    assert got["value"] == {"note": "money shot"}
    deco2 = daemon.request("decorate", {"path": "store.py"})
    entry = next(d for d in deco2 if d["durable_id"] == checkout_id)
    assert entry["note"] == "money shot"


def test_autostop_exits_when_last_client_leaves(shop_project: Path, tmp_path: Path):
    socket_path = tmp_path / "auto.sock"
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tyo3.daemon",
            "--root",
            str(shop_project),
            "--socket",
            str(socket_path),
            "--print-socket",
            "--autostop",
            "--log-level",
            "WARNING",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        sock = _connect(socket_path)
        client = _SocketClient(sock)
        assert client.request("ping")["ok"] is True
        client.close()  # last client leaves → daemon should exit
        proc.wait(timeout=15)
        assert proc.returncode == 0
        # Socket file is cleaned up on exit.
        assert not socket_path.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
