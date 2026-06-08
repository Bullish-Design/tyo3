"""Path-normalisation and range helpers shared by the CodeGraph projection."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from tyo3.models.analysis import Range


def _to_relative(root: Path, path: str) -> str:
    """Convert an absolute path to a project-relative POSIX string.

    *root* must already be resolved (call ``root.resolve()`` once
    before passing it).

    If *path* is not under *root* (e.g. an external path), returns it
    unchanged so external references stay explicitly external.
    """
    try:
        return str(PurePosixPath(Path(path).resolve().relative_to(root)))
    except ValueError:
        return path  # External path — return as-is


def _normalize_result_path(root: Path, path: str, project_files: set[str]) -> str:
    """Normalize a Rust-returned path to match graph-path format.

    Rust APIs return absolute paths.  When the path refers to a
    project file, convert it to a project-relative graph path.
    Otherwise leave it as-is (external).
    """
    candidate = _to_relative(root, path)
    if candidate in project_files:
        return candidate
    return path

def _ranges_overlap(a: Range, b: Range) -> bool:
    """Check if two ranges overlap."""
    a_start = (a.start.line, a.start.column)
    a_end = (a.end.line, a.end.column)
    b_start = (b.start.line, b.start.column)
    b_end = (b.end.line, b.end.column)
    return a_start <= b_end and b_start <= a_end
