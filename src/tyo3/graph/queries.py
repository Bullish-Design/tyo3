"""Read queries over the CodeGraph projection."""

from __future__ import annotations

import rustworkx as rx

from tyo3.graph.dependency import DependencyGraph
from tyo3.graph.identity import file_from_durable_id, make_module_durable_id
from tyo3.graph.models import EdgeData, EdgeKind, SymbolNode
from tyo3.models.symbols import SymbolKind

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


class _QueriesMixin:
    """Read-query methods for :class:`CodeGraph`."""

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

    def _has_import_edge_between(self, source_file: str, target_file: str) -> bool:
        """True iff any IMPORTS edge still connects a node in *source_file* to a
        node in *target_file* (used to decide reverse-dep pruning)."""
        for src_idx in self._file_to_nodes.get(source_file, []):
            for _s, tgt, data in self._graph.out_edges(src_idx):
                if data.kind == EdgeKind.IMPORTS and self._graph[tgt].file == target_file:
                    return True
        return False

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

