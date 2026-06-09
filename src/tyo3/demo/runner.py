"""Demo example runner for TyO3.

Accepts a GitHub repo URL, clones it to a fixture directory,
builds a semantic index, and launches an interactive CLI for
exploring the codebase with queries, navigation, and relationships.

Usage::

    python -m tyo3.demo.runner https://github.com/owner/repo
    python -m tyo3.demo.runner https://github.com/owner/repo --keep
    python -m tyo3.demo.runner https://github.com/owner/repo --branch main
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tyo3 import TyO3Session
from tyo3.demo.cli import run_interactive

# ── Repo URL parsing ────────────────────────────────────────────────────────

_GITHUB_URL_RE = re.compile(r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")


def parse_repo_url(url: str) -> tuple[str, str] | None:
    """Extract (owner, repo_name) from a GitHub URL.

    Returns ``None`` if the URL doesn't look like a GitHub repo.
    """
    m = _GITHUB_URL_RE.match(url.rstrip("/"))
    if m is None:
        return None
    return m.group("owner"), m.group("repo")


# ── Fixture path resolution ─────────────────────────────────────────────────


def resolve_fixture_dir() -> Path:
    """Resolve the demo fixtures directory, creating it if needed."""
    # Walk up from this file to find the project-root fixtures/ directory.
    # runner.py is at src/tyo3/demo/runner.py  →  repo root is ../../../
    candidate = Path(__file__).resolve().parent.parent.parent.parent / "fixtures" / "demo_repos"
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


# ── Clone helpers ────────────────────────────────────────────────────────────


def clone_repo(
    repo_url: str,
    target_dir: Path,
    *,
    branch: str | None = None,
    shallow: bool = True,
    timeout_s: int = 120,
) -> None:
    """Clone a GitHub repository to *target_dir*.

    Raises :class:`subprocess.CalledProcessError` if the clone fails.
    """
    cmd = ["git", "clone"]
    if shallow:
        cmd.append("--depth=1")
    if branch:
        cmd.extend(["--branch", branch, "--single-branch"])
    cmd.extend([repo_url, str(target_dir)])

    print(f"  Cloning {repo_url} ...")
    subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


# ── main ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    """Entry-point: parse args, clone, index, launch CLI.

    The ``tour`` subcommand (``tyo3-demo tour``) runs the guided walkthrough of
    the incremental engine on a synthetic project instead of cloning a repo.
    """
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "tour":
        from tyo3.demo.tour import main as tour_main

        tour_main(argv[1:])
        return

    parser = argparse.ArgumentParser(
        prog="tyo3-demo",
        description="Clone a GitHub repo, index it with TyO3, and explore interactively.",
    )
    parser.add_argument(
        "repo_url",
        help="GitHub repository URL (e.g. https://github.com/owner/repo)",
    )
    parser.add_argument(
        "--branch",
        "-b",
        default=None,
        help="Branch or tag to clone (default: repository default)",
    )
    parser.add_argument(
        "--keep",
        "-k",
        action="store_true",
        help="Keep the cloned repo after exiting (default: clean up)",
    )
    parser.add_argument(
        "--fixtures-dir",
        default=None,
        type=Path,
        help="Directory to clone repos into (default: fixtures/demo_repos/)",
    )
    parser.add_argument(
        "--no-shallow",
        action="store_true",
        help="Do a full clone instead of shallow (--depth=1)",
    )

    args = parser.parse_args(argv)

    # ── Validate URL ─────────────────────────────────────────────────
    parsed = parse_repo_url(args.repo_url)
    if parsed is None:
        print(f"Error: '{args.repo_url}' doesn't look like a GitHub repo URL.")
        print("       Expected: https://github.com/owner/repo")
        sys.exit(1)

    owner, repo = parsed
    repo_name = f"{owner}/{repo}"
    print(f"  Repo:  {repo_name}")
    print(f"  Branch: {args.branch or '(default)'}")

    # ── Resolve target directory ─────────────────────────────────────
    if args.fixtures_dir:
        fixtures_root = args.fixtures_dir.resolve()
    else:
        fixtures_root = resolve_fixture_dir()

    if args.keep:
        target_dir = fixtures_root / repo
    else:
        # Use a temp dir inside fixtures so it's on the same filesystem
        target_dir = Path(tempfile.mkdtemp(prefix=f"{repo}_", dir=fixtures_root))

    # ── Clone ────────────────────────────────────────────────────────
    try:
        clone_repo(
            args.repo_url,
            target_dir,
            branch=args.branch,
            shallow=not args.no_shallow,
        )
    except subprocess.CalledProcessError as e:
        print(f"\nClone failed (exit code {e.returncode}):")
        print(e.stderr)
        _cleanup(target_dir, keep=args.keep)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("\nClone timed out after 120s.")
        _cleanup(target_dir, keep=args.keep)
        sys.exit(1)

    print(f"  Cloned to {target_dir}")

    # ── Build index with TyO3 ────────────────────────────────────────
    print(f"\n  Building TyO3 index for {target_dir} ...")
    try:
        session = TyO3Session(str(target_dir))
    except Exception as e:
        print(f"\nFailed to open project with TyO3: {e}")
        print("Make sure the native extension is built (`maturin develop`).")
        _cleanup(target_dir, keep=args.keep)
        sys.exit(1)

    # ── Launch interactive CLI ───────────────────────────────────────
    try:
        run_interactive(session, repo_name)
    finally:
        session.close()
        _cleanup(target_dir, keep=args.keep)


def _cleanup(target_dir: Path, *, keep: bool) -> None:
    """Remove the cloned directory unless --keep was set."""
    if keep or not target_dir.exists():
        return
    try:
        shutil.rmtree(target_dir)
        print(f"\n  Cleaned up {target_dir}")
    except OSError:
        pass  # best-effort


if __name__ == "__main__":
    main()
