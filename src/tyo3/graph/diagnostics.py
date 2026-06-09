"""Diagnostics collection and lookup for the CodeGraph projection."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tyo3.graph._helpers import _normalize_result_path, _ranges_overlap, _to_relative
from tyo3.graph.models import GraphBuildFailure, GraphBuildReport
from tyo3.models.analysis import Diagnostic

if TYPE_CHECKING:
    from tyo3.session import TyO3Session

logger = logging.getLogger(__name__)


class _DiagnosticsMixin:
    """Diagnostics methods for :class:`CodeGraph`."""

    def refresh_diagnostics(self, source: Any, *, root: Path | None = None) -> None:
        """Read-only diagnostics refresh: one ``source.check()``, distributed
        per file (the 4.2(b) diagnostics re-homing).

        ``check()`` is a pure **read** — it never advances head — so refreshing
        diagnostics here does not violate "reads don't write". Called by the
        graph *builders* (``build`` / the session & snapshot graph construction),
        **never** by ``apply_code_delta`` (the applier stays pure). Diagnostics
        are not part of the code delta, so they are refreshed from the analysis
        ``check()`` surface separately.
        """
        resolved_root = root if root is not None else self._root
        try:
            native_paths = [str(p) for p in source.files()]
        except Exception:
            # No enumerable project files (e.g. a bare snapshot) ⇒ empty set.
            native_paths = []
        project_files = {_to_relative(resolved_root, p) for p in native_paths} if resolved_root is not None else set()
        self._diagnostics.clear()
        self._collect_all_diagnostics(source, root=resolved_root, project_files=project_files)

    def _collect_all_diagnostics(
        self,
        session: TyO3Session,
        *,
        report: GraphBuildReport | None = None,
        root: Path | None = None,
        project_files: set[str] | None = None,
    ) -> None:
        """Collect diagnostics with a single ``check()`` call, distribute per-file.

        Replaces the old per-file ``check_file()`` approach (N FFI
        calls) with a single project-wide check (1 FFI call).

        Normalizes Rust-returned absolute paths to project-relative
        graph paths when *root* and *project_files* are provided.
        """
        try:
            result = session.check()
        except Exception as e:
            logger.warning("Failed to run project check, skipping diagnostics: %s", e)
            if report is not None:
                report.failures.append(
                    GraphBuildFailure(
                        file="<project>",
                        phase="diagnostics",
                        error_type=type(e).__name__,
                        message=str(e),
                    )
                )
            return
        for diagnostic in result.diagnostics:
            if diagnostic.file:
                norm_file = (
                    _normalize_result_path(root, diagnostic.file, project_files)
                    if root is not None and project_files is not None
                    else diagnostic.file
                )
                self._diagnostics.setdefault(norm_file, []).append(diagnostic)

    def diagnostics_for_file(self, path: str) -> list[Diagnostic]:
        """All diagnostics for a file."""
        return self._diagnostics.get(path, [])

    def diagnostics_for_symbol(self, durable_id: str) -> list[Diagnostic]:
        """Diagnostics whose range overlaps this symbol's definition."""
        node = self.symbol(durable_id)
        if node is None:
            return []
        file_diags = self._diagnostics.get(node.file, [])
        return [d for d in file_diags if d.range and _ranges_overlap(d.range, node.range)]

    def all_diagnostics(self) -> list[Diagnostic]:
        """All diagnostics across all files."""
        return [d for diags in self._diagnostics.values() for d in diags]
