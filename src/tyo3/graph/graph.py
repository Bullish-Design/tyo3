"""CodeGraph — the semantic code intelligence graph."""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import rustworkx as rx

from tyo3.graph.dependency import DependencyGraph
from tyo3.graph.identity import file_from_durable_id, make_module_durable_id
from tyo3.graph.models import (
    EdgeData,
    EdgeDiff,
    EdgeKind,
    GraphBuildFailure,
    GraphBuildReport,
    GraphDiff,
    SymbolNode,
)
from tyo3.models.analysis import Diagnostic, Range
from tyo3.models.navigation import ReferenceRole
from tyo3.models.symbols import SymbolKind

if TYPE_CHECKING:
    from tyo3.session import TyO3Session

logger = logging.getLogger(__name__)


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


DEPENDENCY_EDGE_KINDS: frozenset[EdgeKind] = frozenset(
    {
        EdgeKind.REFERENCES,
        EdgeKind.IMPORTS,
        EdgeKind.INHERITS,
        EdgeKind.OVERRIDES,
        EdgeKind.TYPE_OF,
        EdgeKind.RETURNS,
        EdgeKind.INSTANTIATES,
    }
)


class CodeGraph:
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

    # ── Construction ──────────────────────────────────────────

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
            native_paths = []
        project_files = (
            {_to_relative(resolved_root, p) for p in native_paths} if resolved_root is not None else set()
        )
        self._diagnostics.clear()
        self._collect_all_diagnostics(
            source, root=resolved_root, project_files=project_files
        )

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

    # ── Pass helpers ──────────────────────────────────────────

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

    # ── Parent resolution for containment ─────────────────────

    # ── Reference resolution ──────────────────────────────────

    def _importers_of(self, files: set[str]) -> set[str]:
        """Graph paths of project files that import any file in *files*.

        Reads the reverse-dependency index built from IMPORTS edges. Used by
        apply_delta to find inbound cross-file edges that must be revalidated when
        *files* (the dirty set) change. Excludes the dirty files themselves — a file's
        own out-edges are rebuilt by re-indexing it, not by inbound revalidation.
        """
        importers: set[str] = set()
        for f in files:
            importers |= self._file_importers.get(f, set())
        return importers - files

    # ── Inheritance resolution (two-pass, §6.4) ───────────────

    # ── Internal graph mutation ───────────────────────────────

    def _add_node(self, node: SymbolNode) -> int:
        """Add a SymbolNode to the graph and update indexes."""
        self._assert_mutable()
        if node.durable_id in self._id_to_index:
            return self._id_to_index[node.durable_id]
        idx = self._graph.add_node(node)
        self._id_to_index[node.durable_id] = idx
        self._file_to_nodes[node.file].append(idx)
        self._semantic_subgraph_cache.clear()
        return idx

    def _add_edge(
        self,
        source_id: str,
        target_id: str,
        data: EdgeData,
        file: str,
    ) -> int | None:
        """Add an edge between two symbols by their IDs."""
        self._assert_mutable()
        src_idx = self._id_to_index.get(source_id)
        tgt_idx = self._id_to_index.get(target_id)
        if src_idx is None or tgt_idx is None:
            return None
        edge_idx = self._graph.add_edge(src_idx, tgt_idx, data)
        self._file_to_edges[file].append(edge_idx)
        self._semantic_subgraph_cache.clear()
        return edge_idx

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

    @staticmethod
    def _range_key(range_: Range | None) -> tuple[int, int, int, int] | None:
        if range_ is None:
            return None
        return (
            range_.start.line,
            range_.start.column,
            range_.end.line,
            range_.end.column,
        )

    def _edge_map(self) -> dict[tuple[Any, ...], EdgeDiff]:
        edges: dict[tuple[Any, ...], EdgeDiff] = {}
        counts: dict[tuple[Any, ...], int] = defaultdict(int)
        for edge_idx in self._graph.edge_indices():
            data = self._graph.get_edge_data_by_index(edge_idx)
            src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
            source_id = self._graph[src].durable_id
            target_id = self._graph[tgt].durable_id
            base_key = (
                source_id,
                target_id,
                data.kind,
                data.file,
                self._range_key(data.range),
                data.role,
            )
            ordinal = counts[base_key]
            counts[base_key] += 1
            key = (*base_key, ordinal)
            edges[key] = EdgeDiff(source_id, target_id, data)
        return edges

    def diff(self, before: CodeGraph) -> GraphDiff:
        """Return the structural graph delta from *before* to ``self``."""
        before_nodes = {before._graph[idx].durable_id: before._graph[idx] for idx in before._graph.node_indices()}
        after_nodes = {self._graph[idx].durable_id: self._graph[idx] for idx in self._graph.node_indices()}

        before_edges = before._edge_map()
        after_edges = self._edge_map()

        return GraphDiff(
            added_nodes=[after_nodes[did] for did in sorted(after_nodes.keys() - before_nodes.keys())],
            removed_nodes=[before_nodes[did] for did in sorted(before_nodes.keys() - after_nodes.keys())],
            added_edges=[after_edges[key] for key in sorted(after_edges.keys() - before_edges.keys(), key=repr)],
            removed_edges=[before_edges[key] for key in sorted(before_edges.keys() - after_edges.keys(), key=repr)],
        )

    # ── Incremental update helpers (Phase 6) ──────────────────

    def _edges_of_kind(
        self,
        durable_id: str,
        kinds: set[EdgeKind],
        *,
        incoming: bool = False,
    ) -> list[tuple[int, EdgeData]]:
        """Return all edges matching *kinds* for a node.

        When *incoming* is False (default), returns outgoing edges as
        ``(target_index, edge_data)`` pairs.
        When *incoming* is True, returns incoming edges as
        ``(source_index, edge_data)`` pairs.

        Uses RustworkX's ``in_edges`` / ``out_edges`` which return
        all parallel edges in a single efficient Rust-dide call.
        """
        idx = self._id_to_index.get(durable_id)
        if idx is None:
            return []
        result: list[tuple[int, EdgeData]] = []
        if incoming:
            for src, _tgt, data in self._graph.in_edges(idx):
                if data.kind in kinds:
                    result.append((src, data))
        else:
            for _src, tgt, data in self._graph.out_edges(idx):
                if data.kind in kinds:
                    result.append((tgt, data))
        return result

    # ── Properties ────────────────────────────────────────────

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

    # ── Symbol queries ────────────────────────────────────────

    def symbol(self, durable_id: str) -> SymbolNode | None:
        """Look up a symbol by its canonical ID."""
        idx = self._id_to_index.get(durable_id)
        return self._graph[idx] if idx is not None else None

    def symbols_in_file(self, path: str) -> list[SymbolNode]:
        """All symbols defined in a file."""
        return [self._graph[i] for i in self._file_to_nodes.get(path, [])]

    def symbols_of_kind(self, kind: SymbolKind) -> list[SymbolNode]:
        """All symbols of a given kind."""
        result: list[SymbolNode] = []
        for i in self._graph.node_indices():
            node = self._graph[i]
            if node.kind == kind:
                result.append(node)
        return result

    # ── Reference queries ─────────────────────────────────────

    def references_to(self, durable_id: str) -> list[EdgeData]:
        """All incoming REFERENCES edges to a symbol."""
        return [data for _, data in self._edges_of_kind(durable_id, {EdgeKind.REFERENCES}, incoming=True)]

    def references_from(self, durable_id: str) -> list[tuple[SymbolNode, EdgeData]]:
        """All outgoing REFERENCES edges from a symbol."""
        return [(self._graph[tgt_idx], data) for tgt_idx, data in self._edges_of_kind(durable_id, {EdgeKind.REFERENCES})]

    # ── Structural queries ────────────────────────────────────

    def children(self, durable_id: str) -> list[SymbolNode]:
        """Direct children (outgoing DEFINES/CONTAINS edges)."""
        seen: set[int] = set()
        result: list[SymbolNode] = []
        for tgt_idx, _ in self._edges_of_kind(durable_id, {EdgeKind.DEFINES, EdgeKind.CONTAINS}):
            if tgt_idx not in seen:
                seen.add(tgt_idx)
                result.append(self._graph[tgt_idx])
        return result

    def parent(self, durable_id: str) -> SymbolNode | None:
        """Enclosing symbol (incoming DEFINES/CONTAINS edge)."""
        edges = self._edges_of_kind(
            durable_id,
            {EdgeKind.DEFINES, EdgeKind.CONTAINS},
            incoming=True,
        )
        if edges:
            return self._graph[edges[0][0]]
        return None

    def module_for(self, durable_id: str) -> SymbolNode | None:
        """The MODULE node for this symbol's file."""
        # Look up the node to find its file (more robust than parsing the id)
        node = self.symbol(durable_id)
        if node is not None:
            module_id = make_module_durable_id(node.file)
            return self.symbol(module_id)
        # Fallback: try extracting file from the id text
        file = file_from_durable_id(durable_id)
        module_id = make_module_durable_id(file)
        return self.symbol(module_id)

    # ── Dependency analysis ───────────────────────────────────

    def _semantic_subgraph(self, kinds: frozenset[EdgeKind]) -> rx.PyDiGraph:
        """Build a subgraph containing only edges of the given kinds.

        Node indices are preserved (same as the main graph) so callers can
        use ``self._id_to_index`` for lookups.

        Results are memoized per *kinds* set and invalidated on any graph
        mutation (node/edge addition, index rebuild, full rebuild).
        """
        cached = self._semantic_subgraph_cache.get(kinds)
        if cached is not None:
            return cached
        sub = self._graph.copy()
        to_remove = [
            sub.get_edge_endpoints_by_index(edge_idx)
            for edge_idx in sub.edge_indices()
            if sub.get_edge_data_by_index(edge_idx).kind not in kinds
        ]
        sub.remove_edges_from(to_remove)
        self._semantic_subgraph_cache[kinds] = sub
        return sub

    def dependencies(
        self,
        durable_id: str,
        *,
        kinds: frozenset[EdgeKind] | None = None,
    ) -> set[str]:
        """All symbols this one directly depends on (semantic edges only)."""
        edge_kinds = DEPENDENCY_EDGE_KINDS if kinds is None else kinds
        return {self._graph[tgt_idx].durable_id for tgt_idx, _data in self._edges_of_kind(durable_id, edge_kinds)}

    def dependents(
        self,
        durable_id: str,
        *,
        kinds: frozenset[EdgeKind] | None = None,
    ) -> set[str]:
        """All symbols that directly depend on this one (semantic edges only)."""
        edge_kinds = DEPENDENCY_EDGE_KINDS if kinds is None else kinds
        return {
            self._graph[src_idx].durable_id
            for src_idx, _data in self._edges_of_kind(durable_id, edge_kinds, incoming=True)
        }

    def transitive_dependencies(
        self,
        durable_id: str,
        *,
        kinds: frozenset[EdgeKind] | None = None,
    ) -> set[str]:
        """All symbols reachable via semantic edges from this one."""
        edge_kinds = DEPENDENCY_EDGE_KINDS if kinds is None else kinds
        filtered = self._semantic_subgraph(edge_kinds)
        idx = self._id_to_index.get(durable_id)
        if idx is None:
            return set()
        # filtered uses the same indices as the main graph
        reachable = rx.descendants(filtered, idx)
        return {self._graph[i].durable_id for i in reachable}

    def transitive_dependents(
        self,
        durable_id: str,
        *,
        kinds: frozenset[EdgeKind] | None = None,
    ) -> set[str]:
        """All symbols that transitively depend on this one (semantic edges only)."""
        edge_kinds = DEPENDENCY_EDGE_KINDS if kinds is None else kinds
        filtered = self._semantic_subgraph(edge_kinds)
        idx = self._id_to_index.get(durable_id)
        if idx is None:
            return set()
        # filtered uses the same indices as the main graph
        reachable = rx.ancestors(filtered, idx)
        return {self._graph[i].durable_id for i in reachable}

    # ── Graph algorithms ──────────────────────────────────────

    def _build_module_graph(self) -> tuple[rx.PyDiGraph, dict[str, int]]:
        """Build a module-level graph from IMPORTS edges.

        Returns ``(module_graph, did_to_index)`` where *module_graph*
        nodes are module ``durable_id`` strings and *did_to_index*
        maps ``durable_id`` → node index in the module graph.
        """
        module_indices = [i for i in self._graph.node_indices() if self._graph[i].kind == SymbolKind.MODULE]
        if len(module_indices) < 2:
            return rx.PyDiGraph(), {}

        mod_graph = rx.PyDiGraph()
        did_to_midx: dict[str, int] = {}
        for i in module_indices:
            did = self._graph[i].durable_id
            midx = mod_graph.add_node(did)
            did_to_midx[did] = midx

        # Map every node to its module for aggregation
        node_to_module: dict[int, str] = {}
        for mi in module_indices:
            module_did = self._graph[mi].durable_id
            file = file_from_durable_id(module_did)
            for ni in self._file_to_nodes.get(file, []):
                node_to_module[ni] = module_did

        for edge_idx in self._graph.edge_indices():
            data = self._graph.get_edge_data_by_index(edge_idx)
            if getattr(data, "kind", None) != EdgeKind.IMPORTS:
                continue
            src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
            src_mod = node_to_module.get(src)
            tgt_mod = node_to_module.get(tgt)
            if src_mod and tgt_mod and src_mod != tgt_mod:
                mi_src = did_to_midx.get(src_mod)
                mi_tgt = did_to_midx.get(tgt_mod)
                if mi_src is not None and mi_tgt is not None:
                    mod_graph.add_edge(mi_src, mi_tgt, None)

        return mod_graph, did_to_midx

    def import_cycles(self) -> list[list[str]]:
        """Detect circular import chains in the module dependency graph.

        Builds a module-level dependency graph from IMPORTS edges only.
        Two modules are adjacent if one imports the other.

        Returns a list of cycles, where each cycle is a list of
        module durable_ids in order.
        """
        mod_graph, _did_to_midx = self._build_module_graph()
        if mod_graph.num_nodes() < 2:
            return []

        # Build adjacency dict from the module graph
        adj: dict[str, set[str]] = {mod_graph[i]: set() for i in mod_graph.node_indices()}

        for edge_idx in mod_graph.edge_indices():
            src, tgt = mod_graph.get_edge_endpoints_by_index(edge_idx)
            adj[mod_graph[src]].add(mod_graph[tgt])

        # DFS-based cycle detection
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {did: WHITE for did in adj}
        cycles: list[list[str]] = []

        def dfs(u: str, stack: list[str], stack_set: set[str]) -> None:
            color[u] = GRAY
            stack.append(u)
            stack_set.add(u)
            for v in adj.get(u, set()):
                if color.get(v) == GRAY:
                    # Found a cycle: extract from v's position to end
                    cycle_start = stack.index(v)
                    cycles.append(list(stack[cycle_start:]))
                elif color.get(v) == WHITE:
                    dfs(v, stack, stack_set)
            stack.pop()
            stack_set.discard(u)
            color[u] = BLACK

        for did in adj:
            if color[did] == WHITE:
                dfs(did, [], set())

        return cycles

    def hub_symbols(self, top_n: int = 10) -> list[tuple[str, float]]:
        """Find "hub" symbols using betweenness centrality.

        Hub symbols are those that bridge disconnected parts of
        the graph — they sit on many shortest paths between
        other symbols. High centrality often indicates a symbol
        that would cause widespread breakage if changed.

        Computes betweenness centrality over the full directed
        graph and returns the top *top_n* (durable_id, score)
        pairs, sorted descending by score.
        """
        try:
            # rustworkx betweenness_centrality returns a dict-like
            # mapping node_index -> float
            centrality = rx.betweenness_centrality(self._graph)
        except Exception:
            return []

        scored = [(self._graph[i].durable_id, score) for i, score in centrality.items() if score > 0.0]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_n]

    def import_cycle_groups(self) -> list[set[str]]:
        """Groups of mutually-dependent modules (strongly connected components).

        Unlike :meth:`import_cycles`, which enumerates every cycle path,
        this returns connected components in the module dependency graph —
        each group contains all modules that are reachable from each other
        through import edges.

        Returns a list of sets of module durable_ids (one set per SCC
        with more than one module).  Singles (files with no cross-file
        deps) are excluded.
        """
        mod_graph, _did_to_midx = self._build_module_graph()
        if mod_graph.num_nodes() < 2:
            return []

        sccs = rx.strongly_connected_components(mod_graph)
        return [{mod_graph[i] for i in scc} for scc in sccs if len(scc) > 1]

    def is_reachable(self, from_did: str, to_did: str) -> bool:
        """Check if there is a directed path from one symbol to another.

        Uses RustworkX's ``has_path`` which performs BFS/DFS to determine
        connectivity.
        """
        src = self._id_to_index.get(from_did)
        tgt = self._id_to_index.get(to_did)
        if src is None or tgt is None:
            return False
        return rx.has_path(self._graph, src, tgt, as_undirected=False)

    @property
    def has_cycles(self) -> bool:
        """Quick check for any cycles in the graph.

        Returns True if the graph contains at least one directed cycle.
        Uses RustworkX's ``is_directed_acyclic_graph``.
        """
        return not rx.is_directed_acyclic_graph(self._graph)

    def topological_order(self) -> list[str]:
        """Return symbol IDs in dependency order.

        Returns an empty list if the graph has cycles.
        Uses RustworkX's ``topological_sort``.
        """
        try:
            order = rx.topological_sort(self._graph)
        except rx.DAGHasCycle:
            return []
        return [self._graph[i].durable_id for i in order]

    def subgraph_for_file(self, file_path: str) -> rx.PyDiGraph:
        """Extract a subgraph containing all symbols defined in a file
        and their immediate reference neighbours (within the project).

        Uses ``subgraph_with_nodemap`` (B2) for a single Rust call that
        copies nodes and all edges between included nodes, replacing the
        O(E_total) edge scan with Rust-dide filtering.

        Returns a new PyDiGraph that can be queried, exported, or
        visualized independently of the main graph.
        """
        core_indices = set(self._file_to_nodes.get(file_path, []))
        if not core_indices:
            return rx.PyDiGraph()

        # Include immediate neighbours (1-hop)
        included = set(core_indices)
        for ci in core_indices:
            for succ in self._graph.neighbors(ci):
                included.add(succ)
            for pred in self._graph.predecessor_indices(ci):
                included.add(pred)

        # Single Rust call: copies nodes and all edges between included nodes
        sub, _node_map = self._graph.subgraph_with_nodemap(sorted(included))
        return sub

    # ── Incremental updates ───────────────────────────────────

    # ── Pure native-delta applier (Phase 2 / Phase 4) ─────────

    @staticmethod
    def _coerce_range(payload: Any) -> Range | None:
        """Validate a pythonized range dict (or ``None``) into a ``Range``."""
        if payload is None:
            return None
        if isinstance(payload, Range):
            return payload
        return Range.model_validate(payload)

    def _node_from_code_delta(self, n: dict[str, Any]) -> SymbolNode:
        """Build a ``SymbolNode`` from one ``CodeNodeDto`` dict."""
        return SymbolNode(
            durable_id=n["durable_id"],
            name=n["name"],
            qualified_name=n["qualified_name"],
            kind=SymbolKind(n["kind"]),
            file=n["file"],
            range=self._coerce_range(n["range"]),
            selection_range=self._coerce_range(n.get("name_range")),
            content_hash=n.get("content_hash"),
            content_hashes=dict(n.get("content_hashes") or {}),
            external=bool(n.get("external", False)),
            package=n.get("package"),
        )

    def apply_code_delta(self, code_delta: Any) -> None:
        """Apply a native ``CodeDelta`` to this graph — a **pure** function of the
        graph and the delta.

        Makes **no FFI calls** and touches **no session or snapshot**: it consumes
        only the delta's node/edge upserts, removals, and moves. This is the
        applier the parity oracle exercises in Phase 2 and that becomes the sole
        graph-update path at the Phase 4 cutover.

        *code_delta* is a pythonized ``CodeDeltaDto`` dict (the shape the native
        ``full_code_delta()`` / commit delta produce).

        **Revision-gating.** An incremental delta whose ``revision`` does not
        advance the graph (``<= self._revision``) is a stale/duplicate and is a
        **no-op** — publication is the commit tail (§5.3), so in-order is the
        rule and gating only makes the out-of-order case safe rather than
        corrupting the graph. A ``rescan`` delta is an *authoritative wholesale
        replacement* (it carries every node/edge — i.e. it is a full delta) and
        is therefore never gated out; it clears the graph and applies the
        complete set.
        """
        self._assert_mutable()
        d = code_delta if isinstance(code_delta, dict) else dict(code_delta)

        revision = d.get("revision")
        rescan = bool(d.get("rescan"))

        # Revision-gate: a stale/duplicate *incremental* delta is a no-op. A
        # rescan/full delta is authoritative and is applied regardless.
        if (
            not rescan
            and revision is not None
            and self._revision is not None
            and revision <= self._revision
        ):
            return

        # A rescan/full delta is the complete set — clear the graph and apply it
        # wholesale. (``nodes_upserted`` + ``edges_added`` carry every node/edge;
        # this is what ``full_code_delta()`` emits.) On a fresh graph this is a
        # no-op clear; on a populated one it replaces it.
        if rescan:
            self._graph = rx.PyDiGraph()
            self._id_to_index.clear()
            self._file_to_nodes.clear()
            self._file_to_edges.clear()
            self._file_importers.clear()
            self._semantic_subgraph_cache.clear()

        # 1. Removals first (so a remove+re-add of the same id is well-defined).
        removed_ids = list(d.get("nodes_removed") or [])
        if removed_ids:
            doomed = [self._id_to_index[i] for i in removed_ids if i in self._id_to_index]
            if doomed:
                self._graph.remove_nodes_from(doomed)
                self._rebuild_indexes()

        # 2. Node upserts. Re-emit (same id, new payload) replaces in place;
        #    new ids are added.
        for n in d.get("nodes_upserted") or []:
            node = self._node_from_code_delta(n)
            existing = self._id_to_index.get(node.durable_id)
            if existing is None:
                self._add_node(node)
            else:
                old = self._graph[existing]
                self._graph[existing] = node
                if old.file != node.file:
                    if existing in self._file_to_nodes.get(old.file, []):
                        self._file_to_nodes[old.file].remove(existing)
                    self._file_to_nodes[node.file].append(existing)
                self._semantic_subgraph_cache.clear()

        # 3. Moves: same id, unchanged body, new location only.
        for m in d.get("nodes_moved") or []:
            idx = self._id_to_index.get(m["durable_id"])
            if idx is None:
                continue
            node = self._graph[idx]
            updated = node.model_copy(
                update={
                    "file": m["file"],
                    "range": self._coerce_range(m["range"]),
                    "selection_range": self._coerce_range(m.get("name_range")),
                }
            )
            self._graph[idx] = updated
            if node.file != updated.file:
                if idx in self._file_to_nodes.get(node.file, []):
                    self._file_to_nodes[node.file].remove(idx)
                self._file_to_nodes[updated.file].append(idx)

        # 4. Edge removals then additions.
        for e in d.get("edges_removed") or []:
            self._remove_code_edge(e)
        for e in d.get("edges_added") or []:
            self._add_code_edge(e)

        self._semantic_subgraph_cache.clear()
        if revision is not None:
            self._revision = revision

    def _add_code_edge(self, e: dict[str, Any]) -> None:
        """Add one ``CodeEdgeDto`` as an ``EdgeData`` edge."""
        data = EdgeData(
            kind=EdgeKind(e["kind"]),
            file=e.get("file"),
            range=self._coerce_range(e.get("range")),
            role=ReferenceRole(e["role"]) if e.get("role") else None,
        )
        self._add_edge(e["source_id"], e["destination_id"], data, e.get("file") or "")
        # Maintain the file-level reverse-dependency index from IMPORTS edges
        # (project→project only), mirroring _add_import_edge.
        if data.kind == EdgeKind.IMPORTS:
            src = self.symbol(e["source_id"])
            tgt = self.symbol(e["destination_id"])
            if (
                src is not None
                and tgt is not None
                and src.file != "<external>"
                and tgt.file != "<external>"
                and src.file != tgt.file
            ):
                self._file_importers[tgt.file].add(src.file)

    def _remove_code_edge(self, e: dict[str, Any]) -> None:
        """Remove the first matching ``CodeEdgeDto`` edge.

        Symmetrically prunes the file-level reverse-dependency index
        ``_file_importers`` when the removed edge was the *last* IMPORTS edge
        between two project files (mirrors the maintenance in ``_add_code_edge``).
        """
        src_idx = self._id_to_index.get(e["source_id"])
        tgt_idx = self._id_to_index.get(e["destination_id"])
        if src_idx is None or tgt_idx is None:
            return
        kind = EdgeKind(e["kind"])
        want_range = self._coerce_range(e.get("range"))
        want_role = ReferenceRole(e["role"]) if e.get("role") else None
        src_node = self._graph[src_idx]
        tgt_node = self._graph[tgt_idx]
        for ei in self._graph.incident_edges(src_idx):
            s2, t2 = self._graph.get_edge_endpoints_by_index(ei)
            if s2 != src_idx or t2 != tgt_idx:
                continue
            data = self._graph.get_edge_data_by_index(ei)
            if (
                data.kind == kind
                and data.file == e.get("file")
                and data.range == want_range
                and data.role == want_role
            ):
                # Remove THIS specific (possibly parallel) edge by index —
                # ``remove_edge(src, tgt)`` would drop an arbitrary parallel edge
                # between the endpoints, corrupting multi-import/multi-ref pairs.
                self._graph.remove_edge_from_index(ei)
                self._semantic_subgraph_cache.clear()
                # Prune the reverse-dep index iff this was the last IMPORTS edge
                # connecting src.file → tgt.file (parallel imports may remain).
                if (
                    kind == EdgeKind.IMPORTS
                    and src_node.file != "<external>"
                    and tgt_node.file != "<external>"
                    and src_node.file != tgt_node.file
                    and not self._has_import_edge_between(src_node.file, tgt_node.file)
                ):
                    self._file_importers.get(tgt_node.file, set()).discard(src_node.file)
                return

    def _has_import_edge_between(self, source_file: str, target_file: str) -> bool:
        """True iff any IMPORTS edge still connects a node in *source_file* to a
        node in *target_file* (used to decide reverse-dep pruning)."""
        for src_idx in self._file_to_nodes.get(source_file, []):
            for _s, tgt, data in self._graph.out_edges(src_idx):
                if data.kind == EdgeKind.IMPORTS and self._graph[tgt].file == target_file:
                    return True
        return False

    # ── Previously implemented algorithms ─────────────────────

    def coupling_between(self, file_a: str, file_b: str) -> int:
        """Count REFERENCES edges between two files.

        Uses targeted ``out_edges()`` calls (B3) instead of scanning
        all edges in the graph, reducing the operation from O(E_total)
        to O(E_file_a + E_file_b).
        """
        nodes_a = set(self._file_to_nodes.get(file_a, []))
        nodes_b = set(self._file_to_nodes.get(file_b, []))
        if not nodes_a or not nodes_b:
            return 0
        count = 0
        # Count REFERENCES edges from A -> B
        for idx in nodes_a:
            for _src, tgt, data in self._graph.out_edges(idx):
                if data.kind == EdgeKind.REFERENCES and tgt in nodes_b:
                    count += 1
        # Count REFERENCES edges from B -> A
        for idx in nodes_b:
            for _src, tgt, data in self._graph.out_edges(idx):
                if data.kind == EdgeKind.REFERENCES and tgt in nodes_a:
                    count += 1
        return count

    # ── External symbol resolution ───────────────────────────

    def external_symbols(self) -> list[SymbolNode]:
        """All stub nodes for external (dependency) symbols."""
        result: list[SymbolNode] = []
        for i in self._graph.node_indices():
            node = self._graph[i]
            if node.external:
                result.append(node)
        return result

    def external_symbols_by_package(self) -> dict[str, list[SymbolNode]]:
        """Group external symbol stub nodes by package."""
        result: dict[str, list[SymbolNode]] = {}
        for node in self.external_symbols():
            pkg = node.package or "unknown"
            if pkg not in result:
                result[pkg] = []
            result[pkg].append(node)
        return result

    def resolve_external(self, durable_id: str) -> SymbolNode | None:
        """Resolve an external symbol by loading its dependency graph.

        If the symbol is external and its dependency graph is cached
        on disk, loads it and returns the fully-resolved node.
        Otherwise returns the stub node as-is.
        """
        node = self.symbol(durable_id)
        if node is None or not node.external or not node.package:
            return node

        if node.package not in self._dependency_cache:
            # Try loading from disk cache
            dep = DependencyGraph.load(node.package, "unknown")
            if dep is not None:
                self._dependency_cache[node.package] = dep

        dep = self._dependency_cache.get(node.package)
        if dep is None:
            return node  # Return the stub

        resolved = dep.lookup(durable_id)
        return resolved if resolved is not None else node

    # ── Diagnostics ───────────────────────────────────────────

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


def _ranges_overlap(a: Range, b: Range) -> bool:
    """Check if two ranges overlap."""
    a_start = (a.start.line, a.start.column)
    a_end = (a.end.line, a.end.column)
    b_start = (b.start.line, b.start.column)
    b_end = (b.end.line, b.end.column)
    return a_start <= b_end and b_start <= a_end
