"""CodeGraph — the semantic code intelligence graph."""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable

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
from tyo3.models.analysis import CodeDelta, Diagnostic, EdgeDelta, Range, SymbolNodeDelta, SyncResult
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


def _range_size(t: tuple[int, int, int, int, str]) -> tuple[int, int]:
    """Sort key: (line span, end column).  Smallest ranges first.

    The first element (line span) dominates; the second element
    (end column) only matters for single-line ranges where line
    span is 0, which is the one case where comparing columns is
    meaningful.
    """
    start_line, _start_col, end_line, end_col, _did = t
    return (end_line - start_line, end_col)


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
        # Maintained by the CodeDelta applier (IMPORTS edges) and rebuilt
        # authoritatively in _rebuild_indexes. Mirrors the native CodeLayer
        # reverse-dep index for cross-file queries.
        self._file_importers: dict[str, set[str]] = defaultdict(set)

        # The resolved project root, set at build() time. Used to map paths to
        # project-relative graph paths for diagnostics normalisation; `source`
        # may be a Snapshot (no .root). None until build() runs.
        self._root: Path | None = None

        # Range cache by file (rebuilt in _rebuild_indexes).
        #   file_str -> list of (start_line, start_col, end_line, end_col, durable_id)
        #   sorted by range size ascending (smallest first)
        self._file_node_ranges: dict[str, list[tuple[int, int, int, int, str]]] = {}

        # Secondary index: (file, qualified_name) -> durable_id
        # Populated by the CodeDelta applier and used by _find_symbol_in_file
        # to resolve engine-returned names (which lack DurableIds) to graph nodes.
        self._name_to_id: dict[tuple[str, str], str] = {}

        # Legacy secondary index for old payloads that used a name suffix
        # before Gate 2 §5.6 disallowed location-derived graph keys.
        #   (file, name_prefix) -> durable_id
        self._name_prefix_index: dict[tuple[str, str], str] = {}

        # Diagnostics (separate from the structural graph). Type-check output is
        # NOT part of the CodeDelta, so it is collected lazily from the source on
        # first access — keeping the structural build/apply path free of any
        # session/snapshot read call (Gate 3N §6.3.1). `_diag_source` is the
        # session/snapshot the graph was built from; `_diag_loaded` gates the
        # one-time collection.
        self._diagnostics: dict[str, list[Diagnostic]] = {}
        self._diag_source: Any = None
        self._diag_loaded: bool = False

        # Dependency graph cache for external packages
        self._dependency_cache: dict[str, DependencyGraph] = {}

        # Memoized semantic subgraphs keyed by filtered edge-kinds
        #   frozenset[EdgeKind] -> rx.PyDiGraph
        self._semantic_subgraph_cache: dict[frozenset[EdgeKind], rx.PyDiGraph] = {}

        # MVCC graph state (Phase 7). HEAD graphs are mutable and advance via
        # apply_delta; pinned graphs are immutable copies tied to one revision.
        self._revision: int | None = None
        self._frozen = False

    # ── Construction ──────────────────────────────────────────

    @classmethod
    def build(
        cls,
        session,
        *,
        report: GraphBuildReport | None = None,
        root: Path | None = None,
    ) -> CodeGraph:
        """Build a complete code graph by applying a native ``CodeDelta`` (Gate 3N).

        The structural graph (nodes + typed edges) is produced authoritatively in
        Rust — the ``CodeLayer`` — and applied here purely via
        :meth:`apply_code_delta`. There are no read-surface passes and no
        ``session``/``snapshot`` read calls on this path: for a live session the
        delta describes the current HEAD layer; for a ``Snapshot`` it is computed
        over the snapshot's own frozen database at its pinned revision.

        Diagnostics (type-check output, not part of the ``CodeDelta``) are
        collected lazily from *source* on first access (see
        :meth:`diagnostics_for_file`).

        *root* overrides the project root when *source* is a ``Snapshot`` (which
        has no ``.root``). Defaults to ``source.root`` for a ``TyO3Session``. The
        optional *report* records the (now single-pass) file totals.
        """
        graph = cls()
        graph._root = root if root is not None else session.root.resolve()
        delta = CodeDelta.model_validate(session._inner.code_delta_full())
        graph.apply_code_delta(delta)
        # Diagnostics are loaded lazily; remember the source to check against.
        graph._diag_source = session
        graph._diag_loaded = False
        if report is not None:
            files = {
                graph._graph[i].file
                for i in graph._graph.node_indices()
                if graph._graph[i].kind == SymbolKind.MODULE
                and graph._graph[i].file != "<external>"
            }
            report.files_total = len(files)
            report.files_indexed = len(files)
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

    def _refresh_diagnostics(
        self,
        session,
        *,
        root: Path | None = None,
        project_files: set[str] | None = None,
    ) -> None:
        """Clear and recompute all diagnostics from one project-wide check().

        apply_delta uses this instead of touching only dirty files: a change in one
        file can add/remove diagnostics in importers and dependents, so a partial
        update would be incorrect. One check() over the pinned source is consistent
        with the structural update's revision.
        """
        self._diagnostics.clear()
        self._collect_all_diagnostics(session, root=root, project_files=project_files)

    def _find_symbol_in_file(self, file_path: str, name: str) -> str | None:
        """Find a durable_id for *name* in *file_path*.

        Uses the ``_name_to_id`` map (populated during materialisation) as
        the primary lookup — this maps both qualified_name and short name to
        the DurableId. Falls back to node-name scan for pre-existing nodes
        from earlier builds.
        """
        # Primary: name_to_id map (populated during materialisation).
        # An empty string signals a short-name collision — the caller
        # must use a qualified name to disambiguate.
        cached = self._name_to_id.get((file_path, name))
        if cached:
            return cached
        # Secondary: name_prefix_index (legacy, for @line ids)
        cached = self._name_prefix_index.get((file_path, name))
        if cached is not None:
            return cached
        # Fallback: search by short name within the file's nodes
        for idx in self._file_to_nodes.get(file_path, []):
            node: SymbolNode = self._graph[idx]
            if node.name == name or node.qualified_name == name:
                return node.durable_id
        return None

    # ── Internal graph mutation ───────────────────────────────

    def _add_node(self, node: SymbolNode) -> int:
        """Add a SymbolNode to the graph and update indexes."""
        self._assert_mutable()
        if node.durable_id in self._id_to_index:
            return self._id_to_index[node.durable_id]
        idx = self._graph.add_node(node)
        self._id_to_index[node.durable_id] = idx
        self._file_to_nodes[node.file].append(idx)
        # Name→id map for cross-reference resolution (collision-safe).
        if node.qualified_name:
            self._name_to_id[(node.file, node.qualified_name)] = node.durable_id
        short_key = (node.file, node.name)
        if short_key not in self._name_to_id:
            self._name_to_id[short_key] = node.durable_id
        else:
            self._name_to_id[short_key] = ""
        # Legacy lookup for payloads that predate Gate 2 §5.6.
        if "@" in node.durable_id:
            parts = node.durable_id.split("::", 1)
            if len(parts) == 2:
                name_part = parts[1].split("@", 1)[0]
                self._name_prefix_index[(parts[0], name_part)] = node.durable_id
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
        self._file_node_ranges.clear()
        self._name_to_id.clear()
        self._name_prefix_index.clear()
        self._file_importers.clear()
        self._semantic_subgraph_cache.clear()
        for idx in self._graph.node_indices():
            node: SymbolNode = self._graph[idx]
            self._id_to_index[node.durable_id] = idx
            self._file_to_nodes[node.file].append(idx)
            # Rebuild name→id map (collision-safe).
            if node.qualified_name:
                self._name_to_id[(node.file, node.qualified_name)] = node.durable_id
            short_key = (node.file, node.name)
            if short_key not in self._name_to_id:
                self._name_to_id[short_key] = node.durable_id
            else:
                self._name_to_id[short_key] = ""
            # Rebuild legacy suffix lookup for old payloads only.
            if "@" in node.durable_id:
                parts = node.durable_id.split("::", 1)
                if len(parts) == 2:
                    name_part = parts[1].split("@", 1)[0]
                    self._name_prefix_index[(parts[0], name_part)] = node.durable_id
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
        # Rebuild range cache (B1)
        for file_str in self._file_to_nodes:
            self._file_node_ranges[file_str] = sorted(
                [
                    (
                        node.range.start.line,
                        node.range.start.column,
                        node.range.end.line,
                        node.range.end.column,
                        node.durable_id,
                    )
                    for idx in self._file_to_nodes[file_str]
                    for node in [self._graph[idx]]
                    if node.kind != SymbolKind.MODULE
                ],
                key=_range_size,
            )

    def _assert_mutable(self) -> None:
        """Reject structural mutations on a revision-pinned graph."""
        if self._frozen:
            raise RuntimeError("Cannot mutate a revision-pinned CodeGraph")

    def _pin_at(self, revision: int) -> CodeGraph:
        """Return an immutable copy of this graph pinned at *revision*."""
        # Capture diagnostics at pin time so the frozen graph is self-consistent
        # at its revision and never re-checks a moving source.
        self._ensure_diagnostics()
        pinned = CodeGraph()
        pinned._graph = self._graph.copy()
        pinned._id_to_index = dict(self._id_to_index)
        pinned._file_to_nodes = defaultdict(list, {k: list(v) for k, v in self._file_to_nodes.items()})
        pinned._file_to_edges = defaultdict(list, {k: list(v) for k, v in self._file_to_edges.items()})
        pinned._file_importers = defaultdict(set, {k: set(v) for k, v in self._file_importers.items()})
        pinned._root = self._root
        pinned._file_node_ranges = {k: list(v) for k, v in self._file_node_ranges.items()}
        pinned._name_to_id = dict(self._name_to_id)
        pinned._name_prefix_index = dict(self._name_prefix_index)
        pinned._diagnostics = {k: list(v) for k, v in self._diagnostics.items()}
        pinned._diag_loaded = True  # captured above; a frozen graph never re-checks
        pinned._diag_source = None
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

    def rebuild(self, session: TyO3Session, path: str | None = None) -> None:
        """Re-apply the full native ``CodeDelta`` (Gate 3N rescan fallback).

        *path* is accepted for API compatibility but unused — the native
        producer always emits a coherent whole-graph delta, so a targeted
        rebuild is unnecessary.
        """
        self._assert_mutable()
        delta = CodeDelta.model_validate(session._inner.code_delta_full())
        self.apply_code_delta(delta)
        self._diag_source = session
        self._diag_loaded = False

    # ── Pure CodeDelta application (Gate 3N) ──────────────────

    def apply_code_delta(self, delta: CodeDelta) -> None:
        """Apply a native ``CodeDelta`` to the rustworkx replica — purely.

        A pure function of ``(self, delta)``: it reads nothing from the session
        or snapshot read surface. Order within an apply matches the delta's
        phased structure — removals, then upserts, then moves, then edge churn —
        so a node always exists before an edge references it (§6.3.2).

        ``rescan`` clears the replica and applies the delta as a full build.
        """
        self._assert_mutable()

        if delta.rescan:
            self._clear_all()

        # 1. Node removals — rustworkx drops incident edges automatically; a
        #    bulk removal invalidates stored indices, so rebuild them.
        if delta.nodes_removed:
            doomed = [
                self._id_to_index[i]
                for i in delta.nodes_removed
                if i in self._id_to_index
            ]
            if doomed:
                self._graph.remove_nodes_from(doomed)
                self._rebuild_indexes()

        # 2. Explicit edge removals (edges no longer holding whose nodes survive).
        for e in delta.edges_removed:
            self._remove_delta_edge(e)

        # 3. Node upserts — add new, update payload of existing (stable index).
        for nd in delta.nodes_upserted:
            self._upsert_delta_node(nd)

        # 4. Moves — location-only payload update; never re-add, never churn edges.
        for node_id, new_file, new_range in delta.nodes_moved:
            self._move_delta_node(node_id, new_file, new_range)

        # 5. Edge additions — both endpoints exist by now.
        for e in delta.edges_added:
            self._add_delta_edge(e)

        self._semantic_subgraph_cache.clear()
        self._revision = delta.revision

    def _clear_all(self) -> None:
        """Reset the graph and every secondary index (rescan / full replace)."""
        self._graph = rx.PyDiGraph()
        self._id_to_index.clear()
        self._file_to_nodes = defaultdict(list)
        self._file_to_edges = defaultdict(list)
        self._file_importers = defaultdict(set)
        self._file_node_ranges.clear()
        self._name_to_id.clear()
        self._name_prefix_index.clear()
        self._semantic_subgraph_cache.clear()

    @staticmethod
    def _node_from_delta(nd: SymbolNodeDelta) -> SymbolNode:
        """Reconstruct a ``SymbolNode`` payload from a delta node entry."""
        kind = SymbolKind(nd.kind)
        qn = nd.qualified_name
        leaf = qn.rsplit("::", 1)[-1].rsplit(".", 1)[-1] if qn else nd.durable_id
        external = nd.file == "<external>" or nd.durable_id.startswith("<external>")
        if external:
            name = leaf
            if "::" in nd.durable_id:
                package = nd.durable_id.split("::", 1)[0].removeprefix("<external>")
            elif "." in qn:
                package = qn.split(".", 1)[0]
            else:
                package = None
        elif kind == SymbolKind.MODULE:
            name = PurePosixPath(nd.file).stem if nd.file else qn
            external, package = False, None
        else:
            name = leaf
            external, package = False, None
        return SymbolNode(
            durable_id=nd.durable_id,
            name=name,
            qualified_name=qn,
            kind=kind,
            file=nd.file,
            range=nd.range,
            content_hash=nd.content_hash,
            external=external,
            package=package,
        )

    def _upsert_delta_node(self, nd: SymbolNodeDelta) -> None:
        """Add a new node or replace an existing node's payload in place."""
        node = self._node_from_delta(nd)
        idx = self._id_to_index.get(node.durable_id)
        if idx is None:
            self._add_node(node)
            return
        old: SymbolNode = self._graph[idx]
        if old.file != node.file:
            try:
                self._file_to_nodes[old.file].remove(idx)
            except ValueError:
                pass
            self._file_to_nodes[node.file].append(idx)
        self._graph[idx] = node
        if node.qualified_name:
            self._name_to_id[(node.file, node.qualified_name)] = node.durable_id
        self._semantic_subgraph_cache.clear()

    def _move_delta_node(self, node_id: str, new_file: str, new_range: Range) -> None:
        """Update a node's file/range only — no edge churn (§5.5.1 / §6.6)."""
        idx = self._id_to_index.get(node_id)
        if idx is None:
            return
        old: SymbolNode = self._graph[idx]
        moved = old.model_copy(update={"file": new_file, "range": new_range})
        if old.file != new_file:
            try:
                self._file_to_nodes[old.file].remove(idx)
            except ValueError:
                pass
            self._file_to_nodes[new_file].append(idx)
        self._graph[idx] = moved

    def _edge_kind_from_delta(self, kind_str: str, src_id: str) -> EdgeKind:
        """Map a delta edge kind string to an ``EdgeKind``.

        ``containment`` resolves to DEFINES from a module node and CONTAINS
        otherwise (the native producer emits a single ``containment`` kind).
        """
        if kind_str == "containment":
            src = self.symbol(src_id)
            if src is not None and src.kind == SymbolKind.MODULE:
                return EdgeKind.DEFINES
            return EdgeKind.CONTAINS
        return {
            "references": EdgeKind.REFERENCES,
            "imports": EdgeKind.IMPORTS,
            "inherits": EdgeKind.INHERITS,
            "overrides": EdgeKind.OVERRIDES,
        }[kind_str]

    def _add_delta_edge(self, e: EdgeDelta) -> None:
        """Add one typed edge from a delta entry, updating the reverse-dep index."""
        kind = self._edge_kind_from_delta(e.kind, e.src_id)
        src = self.symbol(e.src_id)
        if src is None or e.dst_id not in self._id_to_index:
            return
        role = ReferenceRole(e.role) if e.role else None
        # Edge payload (file/range) comes from the delta verbatim: reference and
        # import edges carry the occurrence location; structural edges carry
        # None. This makes the replica edge byte-equal to the read-surface build.
        data = EdgeData(kind=kind, file=e.file, range=e.range, role=role)
        self._add_edge(e.src_id, e.dst_id, data, e.file or src.file)
        if kind == EdgeKind.IMPORTS:
            tgt = self._graph[self._id_to_index[e.dst_id]]
            if (
                tgt.file != "<external>"
                and src.file != "<external>"
                and tgt.file != src.file
            ):
                self._file_importers[tgt.file].add(src.file)

    def _delta_edge_matches(self, data: EdgeData, e: EdgeDelta, kind: EdgeKind) -> bool:
        """Return whether an existing edge has the exact delta identity."""
        role = ReferenceRole(e.role) if e.role else None
        return (
            data.kind == kind
            and data.role == role
            and data.file == e.file
            and data.range == e.range
        )

    def _remove_delta_edge(self, e: EdgeDelta) -> None:
        """Remove edges matching the full edge identity between surviving nodes."""
        si = self._id_to_index.get(e.src_id)
        ti = self._id_to_index.get(e.dst_id)
        if si is None or ti is None:
            return
        kind = self._edge_kind_from_delta(e.kind, e.src_id)
        doomed = [
            ei
            for ei in self._graph.edge_indices()
            if self._graph.get_edge_endpoints_by_index(ei) == (si, ti)
            and self._delta_edge_matches(
                self._graph.get_edge_data_by_index(ei),
                e,
                kind,
            )
        ]
        for ei in doomed:
            self._graph.remove_edge_from_index(ei)
        if kind == EdgeKind.IMPORTS:
            src_file = self._graph[si].file
            tgt_file = self._graph[ti].file
            self._file_importers.get(tgt_file, set()).discard(src_file)
        self._semantic_subgraph_cache.clear()

    def apply_delta(
        self,
        source,
        delta: SyncResult,
        *,
        report: GraphBuildReport | None = None,
        sort_key: Callable[[list[str]], list[str]] | None = None,
    ) -> None:
        """Apply a write's native ``CodeDelta`` to the replica (Gate 3N).

        A thin wrapper over :meth:`apply_code_delta`: the structural delta is
        produced inside the native commit and carried on ``delta.code_delta``.
        *source* and *sort_key* are accepted for API compatibility — ordering and
        inbound revalidation are now handled in the native producer, so neither is
        needed here. Diagnostics may shift in importers on any edit, so they are
        re-collected lazily from *source* on next access.
        """
        self._assert_mutable()
        if delta.code_delta is not None:
            self.apply_code_delta(delta.code_delta)
        else:
            # A commit that carried no code_delta (e.g. a no-op) only advances
            # the revision marker.
            self._revision = delta.revision
        self._diag_source = source
        self._diag_loaded = False

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

    def _ensure_diagnostics(self) -> None:
        """Lazily collect diagnostics from the build source on first access.

        Type-check output is not part of the ``CodeDelta``, so it is kept off the
        structural build/apply path and gathered here on demand via a single
        ``check()`` over the source. A pinned graph captures its diagnostics at
        pin time (see :meth:`_pin_at`) and never re-checks.
        """
        if self._diag_loaded:
            return
        self._diag_loaded = True
        source = self._diag_source
        if source is None:
            return
        project_files = {
            self._graph[i].file
            for i in self._graph.node_indices()
            if self._graph[i].file and self._graph[i].file != "<external>"
        }
        self._collect_all_diagnostics(source, root=self._root, project_files=project_files)

    def diagnostics_for_file(self, path: str) -> list[Diagnostic]:
        """All diagnostics for a file."""
        self._ensure_diagnostics()
        return self._diagnostics.get(path, [])

    def diagnostics_for_symbol(self, durable_id: str) -> list[Diagnostic]:
        """Diagnostics whose range overlaps this symbol's definition."""
        self._ensure_diagnostics()
        node = self.symbol(durable_id)
        if node is None:
            return []
        file_diags = self._diagnostics.get(node.file, [])
        return [d for d in file_diags if d.range and _ranges_overlap(d.range, node.range)]

    def all_diagnostics(self) -> list[Diagnostic]:
        """All diagnostics across all files."""
        self._ensure_diagnostics()
        return [d for diags in self._diagnostics.values() for d in diags]


def _ranges_overlap(a: Range, b: Range) -> bool:
    """Check if two ranges overlap."""
    a_start = (a.start.line, a.start.column)
    a_end = (a.end.line, a.end.column)
    b_start = (b.start.line, b.start.column)
    b_end = (b.end.line, b.end.column)
    return a_start <= b_end and b_start <= a_end
