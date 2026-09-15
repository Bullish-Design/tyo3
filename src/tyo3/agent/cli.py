"""The thin ``tyo3-agent`` command-line shell over :class:`AgentClient`.

The agent owns source-file writes. This module only parses command arguments,
calls one client method per command, and renders the result; it has no engine
or source-editing logic.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer

from tyo3.agent.client import AgentClient
from tyo3.agent.errors import (
    AgentError,
    DaemonUnavailable,
    EngineError,
    RequestTimeout,
    RevisionEvicted,
    SessionClosed,
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode=None,
    help="Headless project control for the TyO3 semantic daemon.",
)

_TARGET_RE = re.compile(r"^(.+):(\d+):(\d+)$")


@dataclass(frozen=True)
class _Options:
    root: Path
    json_output: bool
    socket: Path | None


def _discover_root() -> Path:
    """Find the nearest project marker, falling back to the current directory."""
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (
            (candidate / "pyproject.toml").is_file()
            or (candidate / ".tyo3").is_dir()
            or (candidate / ".git").exists()
        ):
            return candidate
    return current


@app.callback()
def _main(
    ctx: typer.Context,
    root: Annotated[Path | None, typer.Option("--root", help="Project root (default: discovered from cwd).")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Force JSON output.")] = False,
    socket: Annotated[Path | None, typer.Option("--socket", help="Daemon socket path.")] = None,
) -> None:
    """Headless project control for the TyO3 semantic daemon."""
    ctx.ensure_object(dict)
    ctx.obj["root"] = root.resolve() if root is not None else _discover_root()
    ctx.obj["json"] = json_output
    ctx.obj["socket"] = socket


def _options(ctx: typer.Context, root: Path | None, json_output: bool) -> _Options:
    inherited_root = ctx.ensure_object(dict).get("root", _discover_root())
    inherited_json = ctx.ensure_object(dict).get("json", False)
    inherited_socket = ctx.ensure_object(dict).get("socket")
    selected_root = root.resolve() if root is not None else inherited_root
    return _Options(root=selected_root, json_output=json_output or inherited_json, socket=inherited_socket)


def _emit(value: Any, options: _Options) -> None:
    if options.json_output or not sys.stdout.isatty():
        typer.echo(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            rendered = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, (dict, list)) else item
            typer.echo(f"{key}: {rendered}")
    elif isinstance(value, list):
        for item in value:
            typer.echo(json.dumps(item, ensure_ascii=False, sort_keys=True))
    else:
        typer.echo(value)


def _error_code(error: AgentError) -> int:
    if isinstance(error, DaemonUnavailable):
        return 3
    if isinstance(error, (RevisionEvicted, SessionClosed, RequestTimeout)):
        return 4
    if isinstance(error, EngineError):
        return 1
    return 1


def _call[T](options: _Options, action: Callable[[AgentClient], T]) -> None:
    try:
        with AgentClient(options.root, socket=options.socket) as client:
            result = action(client)
    except AgentError as error:
        typer.echo(f"tyo3-agent: {error}", err=True)
        raise typer.Exit(code=_error_code(error)) from error
    _emit(result, options)


def _target(target: str | None, durable_id: str | None) -> dict[str, Any]:
    if (target is None) == (durable_id is None):
        raise typer.BadParameter("provide exactly one TARGET or --id DURABLE_ID")
    if durable_id is not None:
        if not durable_id:
            raise typer.BadParameter("--id must not be empty")
        return {"durable_id": durable_id}
    assert target is not None
    match = _TARGET_RE.fullmatch(target)
    if match is None:
        raise typer.BadParameter("TARGET must be path:line:col")
    path, line, col = match.groups()
    if int(line) < 1 or int(col) < 1:
        raise typer.BadParameter("TARGET line and col must be positive")
    return {"path": path, "line": int(line), "col": int(col)}


def _decode_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


@app.command()
def status(
    ctx: typer.Context,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show daemon, project, instance, and revision status."""
    _call(_options(ctx, root, json_output), AgentClient.status)


@app.command()
def sync(
    ctx: typer.Context,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Reindex the working tree after the agent writes files itself."""
    _call(_options(ctx, root, json_output), AgentClient.sync)


@app.command("find")
def find_symbols(
    ctx: typer.Context,
    query: Annotated[str | None, typer.Argument(help="Case-insensitive symbol query.")] = None,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Find workspace symbols."""
    _call(_options(ctx, root, json_output), lambda client: client.find(query))


@app.command()
def context(
    ctx: typer.Context,
    target: Annotated[str | None, typer.Argument(metavar="TARGET", help="path:line:col")] = None,
    durable_id: Annotated[str | None, typer.Option("--id", help="Durable entity ID.")] = None,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Read source, references, and authored context for an entity."""
    params = _target(target, durable_id)
    _call(_options(ctx, root, json_output), lambda client: client.context(params))


@app.command()
def impact(
    ctx: typer.Context,
    target: Annotated[str | None, typer.Argument(metavar="TARGET", help="path:line:col")] = None,
    durable_id: Annotated[str | None, typer.Option("--id", help="Durable entity ID.")] = None,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the transitive dependents of an entity."""
    params = _target(target, durable_id)
    _call(_options(ctx, root, json_output), lambda client: client.impact(params))


@app.command()
def check(
    ctx: typer.Context,
    path: Annotated[str | None, typer.Argument(help="Optional project-relative file.")] = None,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run project or file diagnostics."""
    _call(_options(ctx, root, json_output), lambda client: client.check(path))


@app.command()
def changed(
    ctx: typer.Context,
    since: Annotated[int, typer.Option("--since", help="Retained revision to compare from.")],
    to: Annotated[int | None, typer.Option("--to", help="Optional retained revision to compare to.")] = None,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show entity changes since a retained revision."""
    _call(_options(ctx, root, json_output), lambda client: client.changed(since, to=to))


@app.command()
def note(
    ctx: typer.Context,
    value_argument: Annotated[str | None, typer.Argument(metavar="VALUE")] = None,
    durable_id: Annotated[str | None, typer.Option("--id", help="Durable entity ID.")] = None,
    value_option: Annotated[str | None, typer.Option("--value", help="JSON or plain-text note value.")] = None,
    layer: Annotated[str, typer.Option("--layer", help="Authored layer.")] = "intent",
    root: Annotated[Path | None, typer.Option("--root")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Record a durable note against an entity identity."""
    if durable_id is None:
        raise typer.BadParameter("--id DURABLE_ID is required", param_hint="--id")
    if value_argument is None and value_option is None:
        raise typer.BadParameter("provide VALUE or --value", param_hint="VALUE")
    if value_argument is not None and value_option is not None:
        raise typer.BadParameter("provide VALUE or --value, not both")
    raw_value = value_argument if value_argument is not None else value_option
    assert raw_value is not None
    _call(_options(ctx, root, json_output), lambda client: client.note(layer, durable_id, _decode_value(raw_value)))


@app.command()
def notes(
    ctx: typer.Context,
    layer: Annotated[str, typer.Option("--layer", help="Authored layer to list.")] = "intent",
    stale: Annotated[bool, typer.Option("--stale", help="Show notes requiring review.")] = False,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List durable notes, or notes requiring review."""
    _call(_options(ctx, root, json_output), lambda client: client.notes(layer, stale=stale))


if __name__ == "__main__":  # pragma: no cover - exercised through the script
    app()
