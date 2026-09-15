"""End-to-end tests for the thin headless agent CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.daemon.conftest import needs_native
from tyo3.agent import AgentClient, DaemonUnavailable
from tyo3.agent.cli import app

pytestmark = needs_native

runner = CliRunner()


def _stop_client_daemon(client: AgentClient) -> None:
    process = client._process
    client.close()
    if process is not None and process.poll() is None:
        process.terminate()
        process.wait(timeout=10)


def _json_result(shop_project: Path, *args: str) -> dict:
    result = runner.invoke(app, [*args, "--root", str(shop_project), "--json"])
    assert result.exit_code == 0, result.exception or result.stdout
    assert "\x1b" not in result.stdout
    return json.loads(result.stdout)


def test_every_cli_verb_runs_against_real_daemon(shop_project: Path):
    client = AgentClient(shop_project)
    try:
        status = _json_result(shop_project, "status")
        assert status["root"] == str(shop_project.resolve())
        assert status["instance_id"]

        synced = _json_result(shop_project, "sync")
        assert synced["revision"] >= status["revision"]

        found = _json_result(shop_project, "find", "checkout")
        assert any(item["name"] == "checkout" for item in found["symbols"])
        durable_id = found["symbols"][0]["durable_id"]

        context = _json_result(shop_project, "context", "money.py:1:5")
        assert context["name"] == "usd"
        assert _json_result(shop_project, "context", "--id", durable_id) is not None

        impact = _json_result(shop_project, "impact", "money.py:1:5")
        assert impact["durable_id"]
        assert isinstance(impact["dependents"], list)

        diagnostics = _json_result(shop_project, "check", "money.py")
        assert isinstance(diagnostics["diagnostics"], list)

        changed = _json_result(shop_project, "changed", "--since", str(status["revision"]))
        assert changed["before_revision"] == status["revision"]

        noted = _json_result(shop_project, "note", "--id", durable_id, "--value", '{"source":"cli"}')
        assert noted["durable_id"] == durable_id
        notes = _json_result(shop_project, "notes", "--layer", "intent")
        assert durable_id in notes["ids"]
        stale = _json_result(shop_project, "notes", "--stale")
        assert "items" in stale
    finally:
        _stop_client_daemon(client)


def test_cli_usage_error_is_two_and_daemon_error_is_three(monkeypatch, shop_project: Path):
    usage = runner.invoke(app, ["context", "not-a-target", "--root", str(shop_project), "--json"])
    assert usage.exit_code == 2
    assert usage.stdout == ""

    def unavailable(_self: AgentClient) -> None:
        raise DaemonUnavailable("test daemon unavailable")

    monkeypatch.setattr(AgentClient, "_connect_or_start", unavailable)
    unavailable_result = runner.invoke(app, ["status", "--root", str(shop_project), "--json"])
    assert unavailable_result.exit_code == 3
    assert "test daemon unavailable" in unavailable_result.stderr
    assert unavailable_result.stdout == ""


def test_cli_help_lists_every_verb():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for verb in ("status", "sync", "find", "context", "impact", "check", "changed", "note", "notes"):
        assert verb in result.stdout
