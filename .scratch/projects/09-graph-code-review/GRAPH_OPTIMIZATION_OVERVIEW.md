# TyO3 Code Graph — RustworkX Optimization Overview

**Date:** 2026-06-03
**Context:** Post-Phase-3 refactoring analysis — after `_edges_of_kind` was switched from `predecessor_indices`/`neighbors` + `get_all_edge_data` to RustworkX's `in_edges`/`out_edges`
**Scope:** Every `self._graph.*` call site in `graph.py`, `dependency.py`, `export.py` mapped against RustworkX's available API to identify further optimization opportunities

---

## Executive Summary

RustworkX offers a rich API beyond what we currently use. After auditing all 28 call sites across the codebase, **two high-impact optimizations** stand out: `subgraph_with_nodemap` (eliminates O(E) edge scan in `subgraph_for_file`) and `remove_nodes_from` (eliminates manual index management in `update_file`). A third mechanical consistency improvement (`out_edges` in `_resolve_inheritance`) follows the same pattern we just applied to `_edges_of_kind`. Several future-facing APIs (`has_path`, `topological_sort`, `simple_cycles`) are available when the corresponding features are implemented.

---

## 1. Current RustworkX Usage Audit

### 1.1 All call sites in `graph.py`

| Line | Method | Context |
|------|--------|---------|
| 533 | `neighbors()` | `_resolve_inheritance` BFS — get successors of current node |
| 534 | `get_all_edge_data()` | `_resolve_inheritance` BFS — filter INHERITS edges |
| 591 | `add_node()` | `_add_node` |
| 604 | `add_edge()` | `_add_edge` |
| 653 | `in_edges()` | `_edges_of_kind` — incoming edge iteration (**optimized in Phase 3**) |
| 657 | `out_edges()` | `_edges_of_kind` — outgoing edge iteration (**optimized in Phase 3**) |
| 671 | `num_nodes()` | `node_count` property |
| 675 | `num_edges()` | `edge_count` property |
| 692 | `node_indices()` | `symbols_of_kind` — iterate all nodes, filter by kind |
| 751 | `neighbors()` | `dependencies` — get direct successors |
| 761 | `predecessor_indices()` | `dependents` — get direct predecessors |
| 794 | `node_indices()` | `import_cycles` — collect MODULE nodes |
| 806 | `node_indices()` | `import_cycles` — O(M×N) module mapping (Phase 4 target) |
| 815 | `edge_indices()` | `import_cycles` — iterate all edges for inter-module deps |
| 817 | `get_edge_data_by_index()` | `import_cycles` — edge data with try/except |
| 822 | `get_edge_endpoints_by_index()` | `import_cycles` — edge source/target |
| 897 | `neighbors()` | `subgraph_for_file` — collect successor neighbours |
| 899 | `predecessor_indices()` | `subgraph_for_file` — collect predecessor neighbours |
| 908 | `edge_indices()` | `subgraph_for_file` — O(E) scan to copy edges |
| 909 | `get_edge_endpoints_by_index()` | `subgraph_for_file` |
| 911 | `get_edge_data_by_index()` | `subgraph_for_file` |
| 933 | `remove_node()` | `update_file` — loop with sorted(reverse=True) and try/except |
| 944 | `remove_edge_from_index()` | `update_file` — loop with sorted(reverse=True) and try/except |
| 962 | `edge_indices()` | `coupling_between` — O(E) scan (Phase 4 target) |
| 964 | `get_edge_data_by_index()` | `coupling_between` |
| 963 | `get_edge_endpoints_by_index()` | `coupling_between` |
| 977 | `node_indices()` | `external_symbols` — filter by `.external` attribute |

### 1.2 Call sites in `dependency.py`

| Line | Method | Context |
|------|--------|---------|
| 51 | `node_indices()` | `symbols_of_kind` — filter nodes |
| 57 | `node_indices()` | `all_symbols` — iterate all nodes |
| 70 | `node_indices()` | `save()` — serialize nodes |
| 75 | `edge_indices()` | `save()` — serialize edges as `(src_idx, tgt_idx)` pairs |
| 76 | `get_edge_endpoints_by_index()` | `save()` |
| 107 | `add_node()` | `load()` — reconstruct nodes |
| 112 | `add_edge()` | `load()` — reconstruct edges from raw indices (fragile — Phase 7) |

### 1.3 Call sites in `export.py`

| Line | Method | Context |
|------|--------|---------|
| 31 | `node_indices()` | `to_dot()` — iterate nodes |
| 46 | `edge_indices()` | `to_dot()` — iterate edges |
| 50 | `get_edge_endpoints_by_index()` | `to_dot()` |
| 53 | `get_edge_data_by_index()` | `to_dot()` |
| 141 | `node_indices()` | `to_json()` — iterate nodes |
| 145 | `edge_indices()` | `to_json()` — iterate edges |
| 146 | `get_edge_endpoints_by_index()` | `to_json()` |
| 147 | `get_edge_data_by_index()` | `to_json()` |

---

## 2. High-Impact Optimizations

### 2.1 `subgraph_for_file` → `subgraph_with_nodemap`

**Phase:** 4/7 (both performance and polish)
**Impact:** Eliminates O(E) full-graph edge scan per subgraph extraction call

#### Current implementation (`graph.py:900–912`)

```python
sub = rx.PyDiGraph()
old_to_new: dict[int, int] = {}
for idx in included:
    new_idx = sub.add_node(self._graph[idx])
    old_to_new[idx] = new_idx

for edge_idx in self._graph.edge_indices():       # O(E) — scans ALL edges
    src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
    if src in old_to_new and tgt in old_to_new:
        data = self._graph.get_edge_data_by_index(edge_idx)
        sub.add_edge(old_to_new[src], old_to_new[tgt], data)

return sub
```

This does two things inefficiently:
1. **Manual node copy**: iterates `included` set, calls `add_node()` for each
2. **O(E) edge scan**: iterates every edge in the graph, checks if both endpoints are in `old_to_new`, then copies matching edges

For a graph with 200,000 edges, every `subgraph_for_file()` call scans all 200,000 edges even though the subgraph typically has 20–200 nodes and a few hundred edges.

#### Optimized implementation

```python
sub, old_to_new = self._graph.subgraph_with_nodemap(list(included))
return sub
```

`subgraph_with_nodemap(list_of_indices)` is a single Rust call that:
- Copies all listed nodes into a new graph
- Preserves all edges between those nodes
- Returns `(subgraph, NodeMap)` where `NodeMap` maps old indices → new indices
- Silent on invalid indices (nodes not in the graph are simply skipped)

**Replaces 13 lines with 2.** The node expansion logic (core + neighbours) stays the same — only the graph copy is replaced.

#### Complete replacement context

```python
def subgraph_for_file(self, file_path: str) -> rx.PyDiGraph:
    """Extract a subgraph containing all symbols defined in a file
    and their immediate reference neighbours (within the project)."""
    core_indices = set(self._file_to_nodes.get(file_path, []))
    if not core_indices:
        return rx.PyDiGraph()

    # Include immediate neighbours (referenced symbols and referrers)
    included = set(core_indices)
    for ci in core_indices:
        for succ in self._graph.neighbors(ci):
            included.add(succ)
        for pred in self._graph.predecessor_indices(ci):
            included.add(pred)

    # Single Rust call replaces manual node copy + O(E) edge scan
    sub, _ = self._graph.subgraph_with_nodemap(list(included))
    return sub
```

If the `old_to_new` mapping is ever needed downstream (e.g., to resolve indices back to the original graph), use `subgraph_with_nodemap` and keep the map:
```python
sub, node_map = self._graph.subgraph_with_nodemap(list(included))
```

---

### 2.2 `update_file` → `remove_nodes_from`

**Phase:** 5 (fix `update_file` index corruption)
**Impact:** Eliminates manual reverse-sort + try/except loop, single Rust FFI call

#### Current implementation (`graph.py:933–939`)

```python
# 1. Remove all nodes defined in this file
old_indices = list(self._file_to_nodes.get(path, []))
old_ids = [self._graph[i].symbol_id for i in old_indices]
for idx in sorted(old_indices, reverse=True):
    try:
        self._graph.remove_node(idx)
    except Exception:
        pass
for sid in old_ids:
    self._id_to_index.pop(sid, None)
self._file_to_nodes.pop(path, None)
```

This has three problems:
1. **`sorted(reverse=True)`** is needed because `remove_node(idx)` shifts indices of nodes with higher indices. Removing `[2, 5]` must happen as 5 first, then 2.
2. **`try/except`** catches `IndexError` if a node index is somehow invalid, silently swallowing the corruption
3. **Per-node FFI call** — each `remove_node()` is a separate Rust FFI call, and each one removes incident edges and compacts the internal adjacency lists

#### Optimized implementation

```python
old_indices = list(self._file_to_nodes.get(path, []))
old_ids = [self._graph[i].symbol_id for i in old_indices]

# Single Rust call — handles index shifts internally, ignores invalid indices
self._graph.remove_nodes_from(old_indices)

for sid in old_ids:
    self._id_to_index.pop(sid, None)
self._file_to_nodes.pop(path, None)
```

`remove_nodes_from(index_list)`:
- Accepts **any iterable** of node indices — no sorting required (handles index compaction internally)
- Silently **ignores indices not in the graph** (no `try/except` needed)
- Removes **incident edges automatically**
- Single FFI call regardless of how many nodes are removed

#### Edge removal counterpart

For edges, the situation is more nuanced. RustworkX offers `remove_edges_from()` but it takes `(src, tgt)` **node-index pairs**, not edge indices:

```python
# remove_edges_from takes (src_idx, tgt_idx) tuples
# AND only removes ONE edge per pair (not all parallel edges)
# AND raises NoEdgeBetweenNodes if no edge exists
g.remove_edges_from([(0, 1), (0, 2)])
```

**This is not suitable for bulk edge removal by edge index.** Our `_file_to_edges` stores edge indices, not (src, tgt) pairs. Two options:

1. **Keep the current loop** but remove the `sorted(reverse=True)` — RustworkX edge indices are stable under edge removal from the same method (unlike node removal which shifts indices). This needs verification.

2. **Phase 5 solution**: replace the entire `update_file` with `_rebuild_indexes()` (per the implementation guide). After the rebuild, secondary indexes are reconstructed from scratch, making edge-index-level mutation unnecessary.

**Recommendation:** Don't micro-optimize the edge removal loop. The Phase 5 `_rebuild_indexes()` approach (calling `_rebuild_indexes` after `remove_nodes_from`) eliminates the need for edge-index manipulation entirely — edges are reconstructed from the graph's current state.

---

## 3. Mechanical Consistency Improvements

### 3.1 `_resolve_inheritance` BFS → `out_edges`

**Impact:** Low (the current approach is already efficient enough, but inconsistent with Phase 3)

#### Current implementation (`graph.py:532–535`)

```python
current_idx = self._id_to_index[current_sid]
for succ_idx in self._graph.neighbors(current_idx):
    for edge_data in self._graph.get_all_edge_data(current_idx, succ_idx):
        if edge_data.kind != EdgeKind.INHERITS:
            continue
```

This is the **same pattern** that `_edges_of_kind` used before Phase 3:
1. Get neighbour indices via `neighbors()`
2. For each neighbour, call `get_all_edge_data()` to get edge payloads
3. Filter by `EdgeKind`

Phase 3 replaced this in `_edges_of_kind` with `in_edges`/`out_edges`. The `_resolve_inheritance` BFS is the remaining site using the old pattern.

#### Optimized implementation

```python
current_idx = self._id_to_index[current_sid]
for _src, succ_idx, edge_data in self._graph.out_edges(current_idx):
    if edge_data.kind != EdgeKind.INHERITS:
        continue
```

`out_edges(idx)` returns a `WeightedEdgeList` — an iterable of `(src, tgt, data)` triples for all outgoing edges including parallel edges. Single Rust call, no `neighbors()` + per-neighbor `get_all_edge_data()` step.

**Why this matters:** Consistency. Every edge-iteration site in the codebase should use the same pattern. After this change, the pattern "iterate edges of a node filtering by kind" is:
- `_edges_of_kind()` — uses `in_edges`/`out_edges` (Phase 3)
- `_resolve_inheritance` BFS — uses `out_edges` (this change)
- All other edge iteration is index-based (`edge_indices()` + `get_edge_data_by_index()`)

---

## 4. Future-Facing APIs (Available but Not Yet Needed)

These are RustworkX APIs that are available but don't directly map to current functionality. They become relevant as new query methods are added.

### 4.1 `rx.simple_cycles(graph)`

**Relevance:** `import_cycles()` currently uses a hand-rolled DFS (~30 lines) for cycle detection in the module-level adjacency graph.

```python
# Current: hand-rolled DFS with WHITE/GRAY/BLACK state machine
WHITE, GRAY, BLACK = 0, 1, 2
color: dict[str, int] = {sid: WHITE for sid in adj}
def dfs(u, stack, stack_set):
    color[u] = GRAY
    stack.append(u)
    stack_set.add(u)
    for v in adj.get(u, set()):
        if color.get(v) == GRAY:
            cycle_start = stack.index(v)
            cycles.append(list(stack[cycle_start:]))
        elif color.get(v) == WHITE:
            dfs(v, stack, stack_set)
    stack.pop()
    stack_set.discard(u)
    color[u] = BLACK
```

`rx.simple_cycles(graph)` returns a `SimpleCycleIter` — an iterator of cycles expressed as `NodeIndices` (lists of integer node indices). To use it, we'd build a temporary `rx.PyDiGraph` from the module-level adjacency:

```python
# Build module-level graph
mod_graph = rx.PyDiGraph()
mod_graph.add_nodes_from(list(adj.keys()))
name_to_idx = {name: i for i, name in enumerate(adj.keys())}
for src_name, tgt_names in adj.items():
    for tgt_name in tgt_names:
        mod_graph.add_edge(name_to_idx[src_name], name_to_idx[tgt_name], None)

# Delegate cycle detection to RustworkX
for cycle_indices in rx.simple_cycles(mod_graph):
    cycle_names = [mod_graph[i] for i in cycle_indices]
    cycles.append(cycle_names)
```

**Verdict: Not worth it yet.** Two reasons:
1. The hand-rolled DFS operates directly on our `dict[str, set[str]]` adjacency — no need to build a temporary `PyDiGraph`
2. The real bottleneck in `import_cycles` is the O(M×N) `node_to_module` mapping (Phase 4), not the DFS
3. In the common case (few or no cycles in well-structured projects), the DFS cost is negligible

If we ever need to enumerate ALL simple cycles in a heavily cyclic graph, `simple_cycles` would be the right tool.

### 4.2 `rx.has_path(graph, src, tgt)`

**Relevance:** Targeted reachability queries. Currently, `transitive_dependencies` uses `rx.descendants(graph, idx)` which computes ALL reachable nodes. If you only need to answer "does A transitively depend on B?", `has_path` is more efficient:

```python
# Instead of:
all_deps = rx.descendants(self._graph, idx_a)
if idx_b in all_deps: ...

# Use:
if rx.has_path(self._graph, idx_a, idx_b): ...
```

`has_path` typically uses BFS/DFS that stops at first hit, rather than exploring the entire reachable set.

**Relevance:** A future `is_reachable(self, from_sid: str, to_sid: str) -> bool` query method could use this directly.

### 4.3 `rx.is_directed_acyclic_graph(graph)`

**Relevance:** Fast cyclicity check. Rather than enumerating cycles (expensive), just answer "are there any cycles?"

```python
if not rx.is_directed_acyclic_graph(self._graph):
    # graph has at least one cycle
```

Could be used as a quick pre-check before expensive cycle enumeration, or as a standalone property on `CodeGraph`:

```python
@property
def has_cycles(self) -> bool:
    """True if the graph contains at least one directed cycle."""
    return not rx.is_directed_acyclic_graph(self._graph)
```

### 4.4 `rx.topological_sort(graph)` and `rx.lexicographical_topological_sort(graph)`

**Relevance:** Dependency ordering. The concept doc mentions `topological_order()` as a desired query. When implemented:

```python
def topological_order(self) -> list[str]:
    """Return symbol IDs in dependency order (dependencies first)."""
    try:
        order = rx.topological_sort(self._graph)
    except rx.DAGHasCycle:
        return []
    return [self._graph[i].symbol_id for i in order]
```

`topological_sort` raises `DAGHasCycle` if the graph has cycles, so it doubles as a cycle check.

### 4.5 `rx.strongly_connected_components(graph)`

**Relevance:** Finding tightly-coupled clusters. Strongly connected components are maximal sets of nodes where every node can reach every other node — this is a stricter definition than the `connected_components` mentioned in the concept doc, and directly identifies circular dependency clusters.

```python
def strongly_connected_groups(self) -> list[set[str]]:
    """Groups of mutually reachable symbols (circular dependency clusters)."""
    components = rx.strongly_connected_components(self._graph)
    return [
        {self._graph[i].symbol_id for i in comp}
        for comp in components
        if len(comp) > 1  # multi-node components = actual cycles
    ]
```

This could replace or complement the `import_cycles` approach — it finds all strongly connected clusters in O(V+E) using Tarjan's algorithm, without the module-level aggregation step.

### 4.6 `rx.transitive_reduction(graph)`

**Relevance:** Simplifying dependency graphs. Given a DAG, transitive reduction removes edges that are implied by transitive paths. For example, if A→B→C and A→C, the A→C edge is redundant and gets removed.

```python
def simplified_dependencies(self) -> rx.PyDiGraph:
    """Return the transitive reduction of the dependency graph."""
    return rx.transitive_reduction(self._graph)
```

Useful for visualizing "direct" vs "transitive" dependencies cleanly.

---

## 5. Non-Opportunities (Considered and Rejected)

### 5.1 `dependencies` / `dependents` → `find_successors_by_edge` / `find_predecessors_by_edge`

**Why not:** These methods take a filter callable (Python lambda per edge), adding overhead. They also deduplicate — return unique *nodes*, not individual *edges* — which loses parallel edge information. For `dependencies`/`dependents`, `neighbors()`/`predecessor_indices()` are already single pure-Rust calls returning indices. The current approach is optimal.

### 5.2 `symbols_of_kind` / `external_symbols` → `nodes()`

**Why not:** `nodes()` returns node data for ALL nodes as a flat list. We still need to filter by a node attribute (`.kind`, `.external`) on the Python side. `node_indices()` + index lookup is equivalent in performance. No gain.

### 5.3 `remove_edges_from` for edge removal in `update_file`

**Why not:** `remove_edges_from` takes `(src_idx, tgt_idx)` **node-index pairs** (not edge indices). It removes only ONE edge per pair (fails on parallel edges) and raises `NoEdgeBetweenNodes` if no edge exists. Our `_file_to_edges` stores edge indices. Using `remove_edges_from` would require:
1. Resolving each edge index to its `(src, tgt)` pair via `get_edge_endpoints_by_index`
2. Handling parallel edges (calling once per edge, not per pair)
3. Wrapping in `try/except`

This is more code AND slower than the current `remove_edge_from_index` loop. The Phase 5 `_rebuild_indexes()` approach eliminates edge-index manipulation entirely.

### 5.4 Export functions (`to_dot`, `to_json`) → `edges()` / `nodes()`

**Why not:** `edges()` returns just edge data (no `(src, tgt)`), and `nodes()` returns just node data (no index). Export functions need both indices (to label nodes) and endpoints (to define edge connections). `edge_indices()` + `get_edge_endpoints_by_index()` + `get_edge_data_by_index()` is the correct pattern for full-graph serialization.

---

## 6. Priority-Ordered Implementation Plan

| # | Change | Method(s) affected | Lines changed | Phase | Priority |
|---|--------|-------------------|---------------|-------|----------|
| 1 | `subgraph_with_nodemap` | `subgraph_for_file` | 13 → 2 | 4/7 | **High** |
| 2 | `remove_nodes_from` | `update_file` | 6 → 1 | 5 | **High** |
| 3 | `out_edges` BFS | `_resolve_inheritance` | 3 → 3 | 3 (cleanup) | Low |
| 4 | `simple_cycles` DFS | `import_cycles` | ~30 → ~15 | Future | Low |
| 5 | `has_path` | New query method | N/A | Future | Feature |
| 6 | `is_directed_acyclic_graph` | New `has_cycles` property | N/A | Future | Feature |
| 7 | `topological_sort` | New `topological_order` method | N/A | Future | Feature |

### Recommended sequencing

Changes #1 and #2 should be done in their respective phases (4 and 5) as they naturally fit those refactoring steps. Change #3 is a 3-line consistency fix that can be done anytime. Changes #4–7 are feature additions that should be implemented when the corresponding query methods are needed.

---

## 7. RustworkX API Reference (Relevant Subset)

### Edge iteration

| API | Returns | Use case |
|-----|---------|----------|
| `in_edges(idx)` | `WeightedEdgeList[(src, tgt, data)]` | Iterate incoming edges with data |
| `out_edges(idx)` | `WeightedEdgeList[(src, tgt, data)]` | Iterate outgoing edges with data |
| `incident_edges(idx, all_edges=False)` | Edge indices list | Get edge indices only |
| `edge_indices()` | `EdgeIndices` | Iterate all edge indices in graph |
| `get_all_edge_data(src, tgt)` | `list[data]` | All parallel edges between pair |
| `get_edge_data_by_index(idx)` | `data` | Single edge by index |

### Node iteration

| API | Returns | Use case |
|-----|---------|----------|
| `node_indices()` | `NodeIndices` | Iterate all node indices |
| `nodes()` | `list[data]` | Iterate all node data (no indices) |
| `neighbors(idx)` | `NodeIndices` | Successor indices (outgoing) |
| `predecessor_indices(idx)` | `NodeIndices` | Predecessor indices (incoming) |

### Graph manipulation

| API | Behavior |
|-----|----------|
| `remove_nodes_from(indices)` | Batch remove; handles index shifts; ignores invalid |
| `remove_edges_from(pairs)` | Takes `(src, tgt)` pairs; one edge per pair; raises on missing |
| `remove_edge_from_index(idx)` | Single edge by index |
| `subgraph_with_nodemap(nodes)` | Returns `(subgraph, old→new map)`; preserves edges |

### Algorithms (module-level `rx.*`)

| API | Returns | Notes |
|-----|---------|-------|
| `descendants(g, idx)` | `NodeIndices` | All reachable nodes |
| `ancestors(g, idx)` | `NodeIndices` | All nodes that can reach this one |
| `has_path(g, src, tgt)` | `bool` | Reachability check |
| `is_directed_acyclic_graph(g)` | `bool` | Cyclicity check |
| `simple_cycles(g)` | `SimpleCycleIter[NodeIndices]` | All simple cycles |
| `topological_sort(g)` | `NodeIndices` | Raises `DAGHasCycle` if cyclic |
| `strongly_connected_components(g)` | `list[NodeIndices]` | Tarjan's algorithm |
| `betweenness_centrality(g)` | `dict[idx→float]` | Already in use (`hub_symbols`) |
| `transitive_reduction(g)` | `PyDiGraph` | Remove redundant transitive edges |
