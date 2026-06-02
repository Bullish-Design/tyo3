"""Interactive CLI for exploring a TyO3-indexed project."""

from __future__ import annotations

import readline  # noqa: F401 — enables line editing in input()
import shlex
from collections.abc import Callable

from tyo3 import TyO3Session

# ── Colour helpers ───────────────────────────────────────────────────────────

_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"
_RED = "\033[31m"


def bold(s: str) -> str:
    return f"{_BOLD}{s}{_RESET}"


def dim(s: str) -> str:
    return f"{_DIM}{s}{_RESET}"


def green(s: str) -> str:
    return f"{_GREEN}{s}{_RESET}"


def yellow(s: str) -> str:
    return f"{_YELLOW}{s}{_RESET}"


def cyan(s: str) -> str:
    return f"{_CYAN}{s}{_RESET}"


def red(s: str) -> str:
    return f"{_RED}{s}{_RESET}"


# ── Commands ─────────────────────────────────────────────────────────────────

type CommandFn = Callable[[TyO3Session, list[str]], None]


def _cmd_files(session: TyO3Session, args: list[str]) -> None:
    """List all project files."""
    files = session.files()
    print(f"\n{bold(f'{len(files)} files')} in project:\n")
    for f in sorted(files):
        print(f"  {cyan(f)}")


def _cmd_symbols(session: TyO3Session, args: list[str]) -> None:
    """Show symbols in a file. Usage: symbols <path>"""
    if not args:
        print(red("Usage: symbols <path>"))
        return
    path = args[0]
    try:
        symbols = session.document_symbols(path)
    except Exception as e:
        print(red(f"Error: {e}"))
        return

    print(f"\n{bold(f'{len(symbols)} symbols')} in {cyan(path)}:\n")
    for s in symbols:
        kind = dim(f"[{s.kind}]")
        loc = f"{s.location.range.start.line}:{s.location.range.start.column}"
        container = f" {dim('in')} {yellow(s.container_name)}" if s.container_name else ""
        print(f"  {bold(s.name)} {kind} {dim(loc)}{container}")


def _cmd_search(session: TyO3Session, args: list[str]) -> None:
    """Search for symbols across the project. Usage: search <query>"""
    if not args:
        print(red("Usage: search <query>"))
        return
    query = " ".join(args)
    symbols = session.workspace_symbols(query)
    print(f"\n{bold(f'{len(symbols)} results')} for {yellow(query)}:\n")
    for s in symbols:
        kind = dim(f"[{s.kind}]")
        path = cyan(s.location.path)
        loc = f"{s.location.range.start.line}:{s.location.range.start.column}"
        qname = dim(f"({s.qualified_name})") if s.qualified_name else ""
        print(f"  {bold(s.name)} {kind} {qname}")
        print(f"    {path}:{loc}")


def _cmd_definition(session: TyO3Session, args: list[str]) -> None:
    """Go to definition. Usage: def <path> <line> <col>"""
    if len(args) < 3:
        print(red("Usage: def <path> <line> <col>"))
        return
    try:
        path, line, col = args[0], int(args[1]), int(args[2])
        targets = session.goto_definition(path, line, col)
    except ValueError:
        print(red("Line and column must be integers."))
        return
    except Exception as e:
        print(red(f"Error: {e}"))
        return

    print(f"\n{bold(f'{len(targets)} definition(s)')} found:\n")
    for t in targets:
        loc = f"{t.range.start.line}:{t.range.start.column}"
        sym_info = f" → {bold(t.symbol.name)} [{t.symbol.kind}]" if t.symbol else ""
        print(f"  {cyan(t.path)}:{loc}{sym_info}")


def _cmd_references(session: TyO3Session, args: list[str]) -> None:
    """Find all references. Usage: refs <path> <line> <col>"""
    if len(args) < 3:
        print(red("Usage: refs <path> <line> <col>"))
        return
    try:
        path, line, col = args[0], int(args[1]), int(args[2])
        refs = session.find_references(path, line, col)
    except ValueError:
        print(red("Line and column must be integers."))
        return
    except Exception as e:
        print(red(f"Error: {e}"))
        return

    print(f"\n{bold(f'{len(refs)} reference(s)')} found:\n")
    for r in refs:
        kind = dim(f"[{r.kind}]")
        loc = f"{r.range.start.line}:{r.range.start.column}"
        print(f"  {cyan(r.path)}:{loc} {kind}")


def _cmd_hover(session: TyO3Session, args: list[str]) -> None:
    """Show hover info. Usage: hover <path> <line> <col>"""
    if len(args) < 3:
        print(red("Usage: hover <path> <line> <col>"))
        return
    try:
        path, line, col = args[0], int(args[1]), int(args[2])
        result = session.hover(path, line, col)
    except ValueError:
        print(red("Line and column must be integers."))
        return
    except Exception as e:
        print(red(f"Error: {e}"))
        return

    if result is None:
        print(dim("\nNo hover information available.\n"))
        return

    print()
    for c in result.contents:
        label = bold(f"[{c.kind}]")
        print(f"  {label} {c.value}")
    print()


def _cmd_check(session: TyO3Session, args: list[str]) -> None:
    """Run the type checker on the project."""
    print(f"\n{dim('Type-checking project...')}\n")
    try:
        result = session.check()
    except Exception as e:
        print(red(f"Error: {e}"))
        return

    files = f"{result.files_checked} files" if result.files_checked else "unknown files"
    elapsed = f"{result.elapsed_ms}ms" if result.elapsed_ms else "?"
    print(f"  Checked {bold(files)} in {bold(elapsed)}")
    errors = [d for d in result.diagnostics if d.severity in ("error", "fatal")]
    warns = [d for d in result.diagnostics if d.severity == "warning"]
    hints = [d for d in result.diagnostics if d.severity in ("information", "hint")]

    if errors:
        print(f"  {red(f'{len(errors)} errors')}")
    if warns:
        print(f"  {yellow(f'{len(warns)} warnings')}")
    if hints:
        print(f"  {dim(f'{len(hints)} hints')}")

    if not result.diagnostics:
        print(f"  {green('No diagnostics — clean!')}")
        print()
        return

    print()
    for d in result.diagnostics:
        severity_colour = {"error": red, "fatal": red, "warning": yellow}.get(d.severity, dim)
        prefix = severity_colour(f"[{d.severity.upper()}]")
        code = dim(f"({d.code})") if d.code else ""
        loc_str = ""
        if d.range:
            loc_str = dim(f" {d.range.start.line}:{d.range.start.column}")
        print(f"  {prefix}{loc_str} {d.message} {code}")
    print()


def _cmd_info(session: TyO3Session, args: list[str]) -> None:
    """Show project information."""
    root = session.root
    files = session.files()
    py_files = [f for f in files if f.suffix == ".py"]
    print()
    print(f"  Root:        {bold(root)}")
    print(f"  Total files: {bold(str(len(files)))}")
    print(f"  Python files:{bold(str(len(py_files)))}")
    print()


def _cmd_help(session: TyO3Session, args: list[str]) -> None:
    """Show available commands."""
    print(f"""
{bold("TyO3 Demo Explorer")} — available commands:

  {cyan("files")}                  List all project files
  {cyan("symbols <path>")}         Show symbols in a file
  {cyan("search <query>")}         Search for symbols across the project
  {cyan("def <path> <ln> <col>")}  Go to definition at position
  {cyan("refs <path> <ln> <col>")} Find all references at position
  {cyan("hover <path> <ln> <col>")} Show hover/type info at position
  {cyan("check")}                  Run the full type checker
  {cyan("info")}                   Show project summary
  {cyan("help")}                   Show this help
  {cyan("exit")}, {cyan("quit")}            Exit the explorer
""")


# ── Command dispatch ─────────────────────────────────────────────────────────

_COMMANDS: dict[str, CommandFn] = {
    "files": _cmd_files,
    "ls": _cmd_files,
    "symbols": _cmd_symbols,
    "sym": _cmd_symbols,
    "search": _cmd_search,
    "find": _cmd_search,
    "def": _cmd_definition,
    "definition": _cmd_definition,
    "goto": _cmd_definition,
    "refs": _cmd_references,
    "references": _cmd_references,
    "hover": _cmd_hover,
    "type": _cmd_hover,
    "check": _cmd_check,
    "info": _cmd_info,
    "help": _cmd_help,
    "?": _cmd_help,
}


def run_interactive(session: TyO3Session, repo_name: str) -> None:
    """Run the interactive command loop."""
    print(f"\n{bold('TyO3 Demo Explorer')}")
    print(f"Project: {cyan(repo_name)}  |  Root: {dim(str(session.root))}")
    print(f"Files: {bold(str(len(session.files())))}  |  Type {cyan('help')} for commands\n")

    while True:
        try:
            raw = input(f"{green('tyo3')}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw:
            continue

        parts = shlex.split(raw)
        cmd_name = parts[0].lower()
        args = parts[1:]

        if cmd_name in ("exit", "quit", "q"):
            break

        handler = _COMMANDS.get(cmd_name)
        if handler is None:
            print(red(f"Unknown command: {cmd_name}  (type 'help' for commands)"))
            continue

        try:
            handler(session, args)
        except Exception as e:
            print(red(f"Unexpected error: {e}"))

    print(f"\n{dim('Session closed.')}")
