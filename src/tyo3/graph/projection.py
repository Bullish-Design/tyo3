"""CodeGraph — the semantic code intelligence projection.

The class is composed from four mixins (applier, queries, diff, diagnostics);
all instance state is initialised here in ``__init__``. It is a pure projection
of ``full_code_delta()`` — not a transaction participant.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

import rustworkx as rx

from tyo3.graph.applier import _ApplierMixin
from tyo3.graph.dependency import DependencyGraph
from tyo3.graph.diagnostics import _DiagnosticsMixin
from tyo3.graph.diff import _DiffMixin
from tyo3.graph.models import EdgeKind, GraphBuildReport, SymbolNode
from tyo3.graph.queries import _QueriesMixin
from tyo3.models.analysis import Diagnostic

if TYPE_CHECKING:
    from tyo3.session import TyO3Session


class CodeGraph(_ApplierMixin, _QueriesMixin, _DiffMixin, _DiagnosticsMixin):
    """A semantic code intelligence graph for a Python project."""

    def __init__(self) -> None:
        self._graph: rx.PyDiGraph = rx.PyDiGraph()

        # Secondary indexes
        self._id_to_index: dict[str, int] = {}
        self._file_to_nodes: dict[str, list[int]] = defaultdict(list)
        self._file_to_edges: dict[str, list[int]] = defaultdict(list)

        # Reverse-dependency index: target_file -> {source_files that import it}.
        # Maintained from the code delta's IMPORTS edges (add/remove) and rebuilt
        # authoritatively in _rebuild_indexes. Read by the dependency queries and
        # Phase 6 bus file-interest matching.
        self._file_importers: dict[str, set[str]] = defaultdict(set)

        # The resolved project root. Set by build() / the session & snapshot graph
        # builders; `source` may be a Snapshot (no .root). None until set.
        self._root: Path | None = None

        # Diagnostics (separate from the graph; refreshed read-only via
        # refresh_diagnostics, never by the applier).
        self._diagnostics: dict[str, list[Diagnostic]] = {}

        # Dependency graph cache for external packages
        self._dependency_cache: dict[str, DependencyGraph] = {}

        # Memoized semantic subgraphs keyed by filtered edge-kinds
        #   frozenset[EdgeKind] -> rx.PyDiGraph
        self._semantic_subgraph_cache: dict[frozenset[EdgeKind], rx.PyDiGraph] = {}

        # MVCC graph state. HEAD graphs are mutable and advance via
        # apply_code_delta; pinned graphs are immutable copies tied to one revision.
        self._revision: int | None = None
        self._frozen = False

    @classmethod
    def build(
        cls,
        source,
        *,
        report: GraphBuildReport | None = None,
        root: Path | None = None,
    ) -> CodeGraph:
        """Build a code graph from a TyO3 session or Snapshot via the **native
        code delta** — a pure projection, not a read-surface walk (Phase 4).

        Applies *source*'s full (``rescan``) native ``code_delta`` to a fresh
        ``CodeGraph`` (structural state), then performs a single read-only
        diagnostics refresh (``source.check()`` — a pure read that never advances
        head). No identity priming, no ``sync_all``, no per-file FFI resolution:
        resolution now happens in Rust and arrives as the delta.

        *root* overrides the project root when *source* is a Snapshot (which has
        no ``.root`` attribute); defaults to ``source.root`` for a session.

        Raises :class:`GraphBuildFailure` if required identity is missing (a
        defensive case that should not arise after Phase 1/3 reconciliation) —
        it never repairs via a write.
        """
        graph = cls()
        if root is not None:
            root_resolved = root
        elif hasattr(source, "root"):
            root_resolved = source.root.resolve()
        else:
            root_resolved = getattr(source, "_root", None)
        graph._root = root_resolved

        inner = getattr(source, "_inner", source)
        full_delta = inner.full_code_delta()
        graph.apply_code_delta(full_delta)
        graph.refresh_diagnostics(source, root=root_resolved)

        if report is not None:
            files = {n.file for n in (graph._graph[i] for i in graph._graph.node_indices())}
            report.files_total = len([f for f in files if f != "<external>"])
            report.files_indexed = report.files_total

        return graph

    @classmethod
    def build_with_report(
        cls,
        session: TyO3Session,
    ) -> tuple[CodeGraph, GraphBuildReport]:
        """Build a graph and return it alongdide a build report.

        Convenience wrapper around :meth:`build` that always returns
        a report, so callers can inspect failures without threading
        their own report instance.
        """
        report = GraphBuildReport()
        graph = cls.build(session, report=report)
        return graph, report

    def _rebuild_indexes(self) -> None:
        """Reconstruct all secondary indexes from the graph's current state.

        Call this after any bulk node/edge removal (e.g.
        ``remove_nodes_from``) since RustworkX's swap-and-pop
        invalidates stored indices.
        """
        self._id_to_index.clear()
        self._file_to_nodes.clear()
        self._file_to_edges.clear()
        self._file_importers.clear()
        self._semantic_subgraph_cache.clear()
        for idx in self._graph.node_indices():
            node: SymbolNode = self._graph[idx]
            self._id_to_index[node.durable_id] = idx
            self._file_to_nodes[node.file].append(idx)
        for edge_idx in self._graph.edge_indices():
            data = self._graph.get_edge_data_by_index(edge_idx)
            if data is not None and hasattr(data, "file") and data.file is not None:
                self._file_to_edges[data.file].append(edge_idx)
            if data is not None and data.kind == EdgeKind.IMPORTS:
                src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
                src_file = self._graph[src].file
                tgt_file = self._graph[tgt].file
                if tgt_file != "<external>" and src_file != "<external>" and tgt_file != src_file:
                    self._file_importers[tgt_file].add(src_file)

    def _assert_mutable(self) -> None:
        """Reject structural mutations on a revision-pinned graph."""
        if self._frozen:
            raise RuntimeError("Cannot mutate a revision-pinned CodeGraph")

    def _pin_at(self, revision: int) -> CodeGraph:
        """Return an immutable copy of this graph pinned at *revision*."""
        pinned = CodeGraph()
        pinned._graph = self._graph.copy()
        pinned._id_to_index = dict(self._id_to_index)
        pinned._file_to_nodes = defaultdict(list, {k: list(v) for k, v in self._file_to_nodes.items()})
        pinned._file_to_edges = defaultdict(list, {k: list(v) for k, v in self._file_to_edges.items()})
        pinned._file_importers = defaultdict(set, {k: set(v) for k, v in self._file_importers.items()})
        pinned._root = self._root
        pinned._diagnostics = {k: list(v) for k, v in self._diagnostics.items()}
        pinned._dependency_cache = dict(self._dependency_cache)
        pinned._semantic_subgraph_cache = {}
        pinned._revision = revision
        pinned._frozen = True
        return pinned

    @property
    def graph(self) -> rx.PyDiGraph:
        """The underlying RustworkX directed graph."""
        if self._frozen:
            return self._graph.copy()
        return self._graph

    @property
    def revision(self) -> int | None:
        """The application revision this graph describes, if known."""
        return self._revision

    @property
    def frozen(self) -> bool:
        """Whether this graph is pinned and structurally immutable."""
        return self._frozen

    @property
    def node_count(self) -> int:
        return self._graph.num_nodes()

    @property
    def edge_count(self) -> int:
        return self._graph.num_edges()

