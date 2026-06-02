"""Demo example runner for TyO3.

Clone a GitHub repo, build a semantic index, and explore it interactively.
"""

from __future__ import annotations


def run_demo(argv: list[str] | None = None) -> None:
    """Lazy-import entry point for the demo runner."""
    from tyo3.demo.runner import main

    main(argv)


__all__ = ["run_demo"]
