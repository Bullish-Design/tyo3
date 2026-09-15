"""Headless AgentClient tests: no Neovim, no overlay writes."""

from __future__ import annotations

import time
import threading
import json
import socket
from pathlib import Path

import pytest

from tests.daemon.conftest import needs_native
from tyo3.agent import AgentClient, DaemonUnavailable, EngineError, RequestTimeout
from tyo3.daemon.server import DaemonServer

pytestmark = needs_native


def _stop_client_daemon(client: AgentClient) -> None:
    """Stop only the daemon process this test started, after closing clients."""
    process = client._process
    client.close()
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except TimeoutError:
            process.kill()


def test_client_autostarts_and_second_client_joins(shop_project: Path):
    first = AgentClient(shop_project)
    second = None
    try:
        status = first.status()
        assert Path(status["root"]).resolve() == shop_project.resolve()
        assert status["session_id"]
        assert status["instance_id"] == first.instance_id
        assert status["revision"] >= 1
        assert status["protocol_version"] == 1

        second = AgentClient(shop_project)
        assert second.status()["instance_id"] == status["instance_id"]
    finally:
        if second is not None:
            second.close()
        _stop_client_daemon(first)


def test_client_connects_to_an_explicit_socket(shop_project: Path, tmp_path: Path):
    socket_path = tmp_path / "explicit.sock"
    server = DaemonServer(shop_project, socket_path=socket_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15.0
    while not socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    client = AgentClient(shop_project, socket=socket_path)
    try:
        assert client.socket_path == socket_path
        assert client.status()["root"] == str(shop_project.resolve())
    finally:
        client.close()
        server.shutdown()
        thread.join(timeout=10)


def test_client_sync_reindexes_agent_owned_files_without_writing_source(shop_project: Path):
    client = AgentClient(shop_project)
    try:
        before = client.status()
        source = shop_project / "agent_added.py"
        source.write_text("def agent_owned_function() -> str:\n    return 'owned'\n")
        source_bytes = source.read_bytes()

        delta = client.sync()
        assert delta["revision"] == before["revision"] + 1
        assert len(delta["created_ids"]) == 1
        assert any(item["qualified_name"] == "agent_owned_function" for item in client.find("agent_owned")["symbols"])
        assert source.read_bytes() == source_bytes
    finally:
        _stop_client_daemon(client)


def test_client_maps_engine_error_to_typed_exception(shop_project: Path):
    client = AgentClient(shop_project)
    try:
        with pytest.raises(EngineError) as exc_info:
            client.check("does-not-exist.py")
        assert type(exc_info.value) is EngineError
        assert exc_info.value.error_type == "PathResolutionError"
        assert exc_info.value.method == "check"
    finally:
        _stop_client_daemon(client)


def test_client_timeout_is_unknown_and_follow_up_status_reconciles(shop_project: Path):
    client = AgentClient(shop_project)
    try:
        before = client.status()["revision"]
        source = shop_project / "slow_sync.py"
        source.write_text("def slow_sync_entity() -> int:\n    return 1\n")
        with pytest.raises(RequestTimeout):
            client.sync(timeout=0.001)

        deadline = time.monotonic() + 15.0
        after = before
        while after == before and time.monotonic() < deadline:
            after = client.status()["revision"]
            if after == before:
                time.sleep(0.1)
        assert after == before + 1
        assert client._pending == set()
        assert client._responses == {}
        assert any(item["qualified_name"] == "slow_sync_entity" for item in client.find("slow_sync")["symbols"])
    finally:
        _stop_client_daemon(client)


def test_client_reports_transport_loss_before_request_timeout(shop_project: Path, tmp_path: Path):
    socket_path = tmp_path / "drop.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)

    def serve_stub() -> None:
        conn, _ = listener.accept()
        with conn:
            reader = conn.makefile("r", encoding="utf-8", newline="\n")
            for line in reader:
                request = json.loads(line)
                if request["method"] == "ping":
                    result = {"ok": True, "instance_id": "stub"}
                elif request["method"] == "open":
                    result = {"root": str(shop_project.resolve())}
                else:
                    return
                conn.sendall(
                    (json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\n").encode()
                )

    thread = threading.Thread(target=serve_stub, daemon=True)
    thread.start()
    client = AgentClient(shop_project, socket=socket_path, timeout=5.0)
    try:
        started = time.monotonic()
        with pytest.raises(DaemonUnavailable):
            client.check()
        assert time.monotonic() - started < 1.0
        assert client._pending == set()
        assert client._responses == {}
    finally:
        client.close()
        listener.close()
        thread.join(timeout=5)


def test_client_reconnect_converges_without_notification_replay(shop_project: Path):
    client = AgentClient(shop_project)
    process = client._process
    try:
        before = client.status()
        client.close()
        client.reconnect()
        after = client.status()
        assert after["root"] == before["root"]
        assert after["instance_id"] == before["instance_id"]
        assert after["revision"] == before["revision"]
        assert client.notifications.empty()
        assert client.find("checkout")["symbols"]
    finally:
        client.close()
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
