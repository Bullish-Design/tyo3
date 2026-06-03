# TyO3 Graph Improvements — Implementation Guide

**Source:** `COMBINED_GRAPH_IMPROVEMENTS_REVIEW.md`
**Target files:** `graph.py`, `dependency.py`, `export.py`, `rust_project.py`, `models/`

This guide is organized into 5 phases (A–E), ordered by dependency and priority. Complete each phase fully before moving to the next. Within each phase, complete steps in order — later steps may depend on earlier ones.

**Before you start:** Read every target file end-to-end. Run `devenv shell -- tests` to confirm the test suite passes on the current code. All changes must keep tests green.

---

## Phase A — Correctness Fixes (P0)

These fix silent bugs. Do them first because later optimizations depend on correct secondary indexes.

---

### A1. Add `_rebuild_indexes()` method to `CodeGraph`

**File:** `src/tyo3/graph/graph.py`
**Why:** `update_file()` has an index invalidation bug. RustworkX uses swap-and-pop on `remove_node()`, which silently corrupts `_id_to_index`, `_file_to_nodes`, and `_file_to_edges`. The `sorted(reverse=True)` trick and `try/except: pass` only mask the problem. We need a safe rebuild mechanism before fixing `update_file()`.

**What to do:**

1. Add a new private method `_rebuild_indexes()` to `CodeGraph`. Place it after `_add_edge()` (after line ~606):

```python
def _rebuild_indexes(self) -> None:
    """Reconstruct all secondary indexes from the graph's current state.

    Call this after any bulk node/edge removal (e.g. remove_nodes_from)
    since RustworkX's swap-and-pop invalidates stored indices.
    """
    self._id_to_index.clear()
    self._file_to_nodes.clear()
    self._file_to_edges.clear()
    # Use defaultdict behavior — _file_to_nodes is already a defaultdict(list)
    for idx in self._graph.node_indices():
        node: SymbolNode = self._graph[idx]
        self._id_to_index[node.symbol_id] = idx
        self._file_to_nodes[node.file].append(idx)
    for edge_idx in self._graph.edge_indices():
        data = self._graph.get_edge_data_by_index(edge_idx)
        if data is not None and hasattr(data, 'file') and data.file is not None:
            self._file_to_edges[data.file].append(edge_idx)
```

2. **Do not** change `_file_to_nodes` or `_file_to_edges` initialization in `__init__` — they're already `defaultdict(list)`.

**Testing:** This method is exercised through A2. No standalone test needed, but you can add one that builds a small graph, manually removes nodes, calls `_rebuild_indexes()`, and asserts the indexes match the graph state.

---

### A2. Fix `update_file()` — use `remove_nodes_from()` + `_rebuild_indexes()`

**File:** `src/tyo3/graph/graph.py`, method `update_file()` (lines ~918–953)
**Why:** The current per-node removal loop with `try/except: pass` silently corrupts indexes. Additionally, incoming cross-file edges are not cleaned up, causing duplicate edges after re-indexing.

**What to do:**

Replace the body of `update_file()` with:

```python
def update_file(self, session: TyO3Session, path: str) -> None:
    old_node_indices = list(self._file_to_nodes.get(path, []))
    if old_node_indices:
        # remove_nodes_from handles index compaction internally,
        # automatically removes incident edges, and ignores invalid indices.
        self._graph.remove_nodes_from(old_node_indices)
        # Rebuild all secondary indexes since swap-and-pop invalidates them.
        self._rebuild_indexes()
    self._diagnostics.pop(path, None)
    self._index_file(session, path)
```

**What this removes:**
- The `sorted(old_indices, reverse=True)` loop
- The `try/except: pass` blocks for both node and edge removal
- Manual cleanup of `_id_to_index`, `_file_to_nodes`, `_file_to_edges`
- The separate edge removal loop (incident edges are auto-removed by `remove_nodes_from`)

**Key behavior change:** `remove_nodes_from()` is a single Rust call that handles all index shifting internally. `_rebuild_indexes()` then reconstructs the Python-side indexes from scratch, guaranteeing consistency.

**Testing:** The existing `update_file` tests should still pass. Additionally, write a test that:
1. Builds a graph with files A and B where B references symbols in A
2. Calls `update_file()` on A
3. Asserts that `_id_to_index` contains no stale entries for A's old symbols
4. Asserts that cross-file edges from B to A's old symbols are gone
5. Asserts that new symbols from A are correctly indexed

---

### A3. Fix `import_cycles()` O(M×N) module mapping

**File:** `src/tyo3/graph/graph.py`, method `import_cycles()` (lines ~806–808)
**Why:** The inner loop iterates ALL graph nodes per module to find which nodes belong to each module's file. The `_file_to_nodes` index already provides this mapping.

**What to do:**

Find this block (approximately lines 806–808):

```python
for mi in module_indices:
    module_sid = self._graph[mi].symbol_id
    file = file_from_symbol_id(module_sid)
    for ni in self._graph.node_indices():  # <-- THIS IS THE BUG
        if self._graph[ni].file == file:
            node_to_module[ni] = module_sid
```

Replace the inner loop:

```python
for mi in module_indices:
    module_sid = self._graph[mi].symbol_id
    file = file_from_symbol_id(module_sid)
    for ni in self._file_to_nodes.get(file, []):
        node_to_module[ni] = module_sid
```

**Testing:** Existing `import_cycles` tests should pass unchanged. The output is identical; only performance differs.

---

### A4. Fix `_find_enclosing_symbol()` size comparison heuristic

**File:** `src/tyo3/graph/graph.py`, method `_find_enclosing_symbol()` (line ~572)
**Why:** The scalar `(end_line - start_line) * 10000 + (end_col - start_col)` produces wrong results when `end_col < start_col` on multi-line ranges, or with very long lines.

**What to do:**

Find line ~572:

```python
size = (nr.end.line - nr.start.line) * 10000 + (nr.end.column - nr.start.column)
```

Replace with:

```python
size = (nr.end.line - nr.start.line, nr.end.column - nr.start.column)
```

Also update the `best_size` initialization (line ~569) from `sys.maxsize` to a tuple:

```python
best_size: tuple[int, int] | int = (sys.maxsize, sys.maxsize)
```

Actually, simpler — just change the type:

```python
best_size = (sys.maxsize, sys.maxsize)
```

Python's lexicographic tuple comparison handles this correctly: `(1, 5) < (2, 3)` is True because line difference dominates.

**Testing:** Existing tests should pass. Optionally add a unit test with a multi-line symbol where `end_col < start_col` to verify correct behavior.

---

### A5. Fix `dependency.py save()` — serialize edge data

**File:** `src/tyo3/graph/dependency.py`, method `save()` (lines ~75–77)
**Why:** Edge data (kind, file, range, role) is silently dropped during serialization. Round-tripping through save/load produces a graph with `None` edge data, breaking any downstream query that checks `edge.kind`.

**What to do:**

Find the edge serialization block in `save()`:

```python
for edge_idx in self.graph.edge_indices():
    src, tgt = self.graph.get_edge_endpoints_by_index(edge_idx)
    edges.append({"src": src, "tgt": tgt})
```

Replace with:

```python
for edge_idx in self.graph.edge_indices():
    src, tgt = self.graph.get_edge_endpoints_by_index(edge_idx)
    raw = self.graph.get_edge_data_by_index(edge_idx)
    edge_dict: dict[str, Any] = {"src": src, "tgt": tgt}
    if raw is not None:
        edge_data_dict: dict[str, str] = {"kind": raw.kind.value}
        if raw.file:
            edge_data_dict["file"] = raw.file
        if raw.role:
            edge_data_dict["role"] = raw.role.value
        edge_dict["data"] = edge_data_dict
    edges.append(edge_dict)
```

Add `from typing import Any` to the imports if not already present.

**Then update `load()`** (see A6).

---

### A6. Fix `dependency.py load()` — use symbol_id-based edge reconstruction

**File:** `src/tyo3/graph/dependency.py`, method `load()` (lines ~107–112)
**Why:** The current code assumes `add_node()` assigns indices 0, 1, 2... and uses raw index values from the serialized data. This is an implicit coupling to RustworkX internals. Also, now that save() includes edge data, load() must reconstruct it.

**What to do:**

**In `save()`**, change edge serialization to use symbol_ids instead of raw indices:

```python
for edge_idx in self.graph.edge_indices():
    src, tgt = self.graph.get_edge_endpoints_by_index(edge_idx)
    raw = self.graph.get_edge_data_by_index(edge_idx)
    edge_dict: dict[str, Any] = {
        "src_id": self.graph[src].symbol_id,
        "tgt_id": self.graph[tgt].symbol_id,
    }
    if raw is not None:
        edge_data_dict: dict[str, str] = {"kind": raw.kind.value}
        if raw.file:
            edge_data_dict["file"] = raw.file
        if raw.role:
            edge_data_dict["role"] = raw.role.value
        edge_dict["data"] = edge_data_dict
    edges.append(edge_dict)
```

> **Note:** This replaces the version from A5. If you're implementing A5 and A6 together (recommended), just write the final version from A6 directly.

**In `load()`**, update edge reconstruction:

```python
# After building all nodes and id_to_index:
for edge_data in data.get("edges", []):
    src_idx = id_to_index.get(edge_data.get("src_id"))
    tgt_idx = id_to_index.get(edge_data.get("tgt_id"))
    if src_idx is None or tgt_idx is None:
        continue
    # Reconstruct EdgeData if present
    raw_edge = edge_data.get("data")
    if raw_edge is not None:
        from tyo3.graph.models import EdgeData, EdgeKind
        edge_obj = EdgeData(
            kind=EdgeKind(raw_edge["kind"]),
            file=raw_edge.get("file"),
            role=raw_edge.get("role"),
        )
    else:
        edge_obj = None
    graph.add_edge(src_idx, tgt_idx, edge_obj)
```

**Backward compatibility:** Old cached files use `"src"` and `"tgt"` keys with integer values. You can add a fallback:

```python
# Fallback for old format
if "src_id" not in edge_data and "src" in edge_data:
    src_val, tgt_val = edge_data["src"], edge_data["tgt"]
    if src_val < graph.num_nodes() and tgt_val < graph.num_nodes():
        graph.add_edge(src_val, tgt_val, None)
    continue
```

**Testing:** Delete any existing cache files in `~/.cache/tyo3/deps/` and re-run tests. Write a test that:
1. Creates a `DependencyGraph` with edge data
2. Calls `save()`
3. Calls `load()` and asserts edge data round-trips correctly

---

### A7. Fix `export.py to_dot()` — `max_nodes` guard

**File:** `src/tyo3/graph/export.py`, function `to_dot()` (line ~51)
**Why:** After node deletions, RustworkX indices are non-contiguous. Using `src >= max_nodes` as a filter incorrectly uses index magnitude as a proxy for "is this node within the first N." High-index but valid nodes get excluded; edges to skipped nodes get included.

**What to do:**

Refactor the node/edge emission in `to_dot()`:

1. Track which node indices were actually emitted:

```python
emitted: set[int] = set()
count = 0
for idx in g.node_indices():
    if max_nodes is not None and count >= max_nodes:
        break
    emitted.add(idx)
    count += 1
    node: SymbolNode = g[idx]
    # ... existing node DOT string generation ...
```

2. Filter edges against the emitted set:

```python
edge_count = 0
max_edges = max_nodes * 3 if max_nodes is not None else None
for edge_idx in g.edge_indices():
    if max_edges is not None and edge_count >= max_edges:
        break
    src, tgt = g.get_edge_endpoints_by_index(edge_idx)
    if src not in emitted or tgt not in emitted:
        continue
    edge_count += 1
    # ... existing edge DOT string generation ...
```

3. Remove the old `if max_nodes is not None and (src >= max_nodes or tgt >= max_nodes): continue` check.

**Testing:** Write a test that creates a graph, removes some nodes (creating non-contiguous indices), and verifies `to_dot(max_nodes=N)` produces valid DOT output with exactly N nodes.

---

## Phase B — Performance Fixes (P1)

These improve build-time and query-time performance. Phase A must be complete first (especially A1/A2, since `_rebuild_indexes` is the safety net).

---

### B1. Optimize `_find_enclosing_symbol()` — pre-materialized range cache

**File:** `src/tyo3/graph/graph.py`
**Why:** This is the single largest build-time bottleneck. Called once per name occurrence during `_resolve_references_via_occurrences()`. For a file with 200 symbols and 500 occurrences, it makes 100,000 FFI calls (`self._graph[idx]` per node per occurrence).

**What to do:**

1. Add a new instance variable in `__init__`:

```python
self._file_node_ranges: dict[str, list[tuple[int, int, int, int, str]]] = {}
```

Each entry is `(start_line, start_col, end_line, end_col, symbol_id)`, sorted by range size ascending (smallest first).

2. At the end of `_index_file()`, after all nodes for the file are added, build the cache:

```python
# Build pre-materialized range cache for _find_enclosing_symbol
self._file_node_ranges[file_str] = sorted(
    [
        (node.range.start.line, node.range.start.column,
         node.range.end.line, node.range.end.column,
         node.symbol_id)
        for idx in self._file_to_nodes[file_str]
        for node in [self._graph[idx]]
        if node.kind != SymbolKind.MODULE
    ],
    key=lambda t: (t[2] - t[0], t[3] - t[1])  # sort by range size ascending
)
```

3. Rewrite `_find_enclosing_symbol()` to use the cache:

```python
def _find_enclosing_symbol(self, file_str: str, range: Range) -> str | None:
    cached = self._file_node_ranges.get(file_str, [])
    for start_line, start_col, end_line, end_col, sid in cached:
        # Check containment: node range must fully contain the target range
        if (start_line, start_col) <= (range.start.line, range.start.column) and \
           (end_line, end_col) >= (range.end.line, range.end.column):
            return sid  # First match is smallest due to sort order
    # Fallback to module
    module_id = f"{file_str}::<module>"
    if module_id in self._id_to_index:
        return module_id
    return None
```

4. In `_rebuild_indexes()`, clear the cache:

```python
self._file_node_ranges.clear()
```

And after rebuilding nodes, rebuild the range cache for each file:

```python
for file_str in self._file_to_nodes:
    self._file_node_ranges[file_str] = sorted(
        [
            (node.range.start.line, node.range.start.column,
             node.range.end.line, node.range.end.column,
             node.symbol_id)
            for idx in self._file_to_nodes[file_str]
            for node in [self._graph[idx]]
            if node.kind != SymbolKind.MODULE
        ],
        key=lambda t: (t[2] - t[0], t[3] - t[1])
    )
```

**Why this works:** The cache is built once per file (O(N_file)), then each `_find_enclosing_symbol` call iterates pure Python tuples (no FFI). The sort-by-size-ascending means the first containment match is the smallest enclosing symbol, so we can return immediately.

**Testing:** Existing tests for reference resolution should pass unchanged. The optimization is transparent.

---

### B2. Optimize `subgraph_for_file()` — use `subgraph_with_nodemap`

**File:** `src/tyo3/graph/graph.py`, method `subgraph_for_file()` (lines ~883–914)
**Why:** Currently scans ALL edges in the graph (O(E_total)) to find edges between subgraph nodes. `subgraph_with_nodemap()` does this in a single Rust call.

**What to do:**

Replace the method body:

```python
def subgraph_for_file(self, file_path: str) -> rx.PyDiGraph:
    core_indices = set(self._file_to_nodes.get(file_path, []))
    if not core_indices:
        return rx.PyDiGraph()

    # Include immediate neighbors (1-hop)
    included = set(core_indices)
    for ci in core_indices:
        for succ in self._graph.neighbors(ci):
            included.add(succ)
        for pred in self._graph.predecessor_indices(ci):
            included.add(pred)

    # Single Rust call: copies nodes and all edges between included nodes
    sub, _node_map = self._graph.subgraph_with_nodemap(sorted(included))
    return sub
```

**What this removes:**
- The manual `old_to_new` dict construction
- The `rx.PyDiGraph()` manual construction
- The O(E_total) edge scan loop
- The per-edge `get_edge_endpoints_by_index()` and `get_edge_data_by_index()` calls

**Testing:** Existing `subgraph_for_file` tests should pass. The returned subgraph has the same nodes and edges, but indices may differ (the `_node_map` provides the mapping if needed, but callers typically don't depend on specific index values).

---

### B3. Optimize `coupling_between()` — targeted `out_edges()`

**File:** `src/tyo3/graph/graph.py`, method `coupling_between()` (lines ~957–969)
**Why:** Scans ALL edges in the graph to count references between two files. Only needs to check outgoing edges from nodes in those two files.

**What to do:**

Replace the method body:

```python
def coupling_between(self, file_a: str, file_b: str) -> int:
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
```

**Testing:** Existing `coupling_between` tests should pass unchanged.

---

### B4. Add secondary index for `_find_symbol_in_file()` name@line lookups

**File:** `src/tyo3/graph/graph.py`
**Why:** When the exact `file::name` lookup fails, the method falls back to a linear scan of ALL symbol IDs in `_id_to_index`. This runs inside the occurrence-processing loop.

**What to do:**

1. Add a new instance variable in `__init__`:

```python
# Secondary index: (file, name_prefix) -> symbol_id for name@line lookups
self._name_prefix_index: dict[tuple[str, str], str] = {}
```

2. In `_add_node()`, after updating `_id_to_index` and `_file_to_nodes`, add:

```python
# Index for fast name@line lookups
if "@" in node.symbol_id:
    parts = node.symbol_id.split("::", 1)
    if len(parts) == 2:
        name_part = parts[1].split("@", 1)[0]
        self._name_prefix_index[(parts[0], name_part)] = node.symbol_id
```

3. In `_find_symbol_in_file()`, replace the linear scan (the `prefix = f"{file_path}::{name}@"` block) with:

```python
# Fast name@line lookup via secondary index
cached = self._name_prefix_index.get((file_path, name))
if cached is not None:
    return cached
```

Keep the existing third-tier fallback (short-name scan of `_file_to_nodes[file_path]`) as a last resort.

4. In `_rebuild_indexes()`, clear and rebuild:

```python
self._name_prefix_index.clear()
# ... after the node index rebuild loop:
for idx in self._graph.node_indices():
    node = self._graph[idx]
    # ... existing _id_to_index and _file_to_nodes updates ...
    if "@" in node.symbol_id:
        parts = node.symbol_id.split("::", 1)
        if len(parts) == 2:
            name_part = parts[1].split("@", 1)[0]
            self._name_prefix_index[(parts[0], name_part)] = node.symbol_id
```

**Testing:** Existing tests should pass. The optimization is transparent to callers.

---

## Phase C — Code Quality & Consistency (P2)

Low-risk improvements that clean up inconsistencies and use better RustworkX APIs. Can be done incrementally.

---

### C1. Unify `_resolve_inheritance()` BFS to use `out_edges()`

**File:** `src/tyo3/graph/graph.py`, method `_resolve_inheritance()` (lines ~532–536)
**Why:** This is the last place using the old `neighbors()` + `get_all_edge_data()` pattern. Phase 3 already replaced all other sites with `out_edges()`.

**What to do:**

Find:

```python
for succ_idx in self._graph.neighbors(current_idx):
    for edge_data in self._graph.get_all_edge_data(current_idx, succ_idx):
        if edge_data.kind != EdgeKind.INHERITS:
            continue
```

Replace with:

```python
for _src, succ_idx, edge_data in self._graph.out_edges(current_idx):
    if edge_data.kind != EdgeKind.INHERITS:
        continue
```

**Testing:** Existing inheritance/override tests should pass.

---

### C2. Fix double `self._graph[i]` lookups — bind once

**Affected locations:**
- `symbols_of_kind()` (line ~691–694)
- `external_symbols()` (line ~975–978)
- `import_cycles()` module collection (line ~793–796)
- `dependency.py:symbols_of_kind()` (line ~50–52)

**Pattern to fix:**

```python
# BEFORE (two FFI calls per node):
return [
    self._graph[i]
    for i in self._graph.node_indices()
    if self._graph[i].kind == kind
]

# AFTER (one FFI call per node):
result = []
for i in self._graph.node_indices():
    node = self._graph[i]
    if node.kind == kind:
        result.append(node)
return result
```

Apply this pattern to each of the four locations. The `filter_nodes()` API is also an option:

```python
return [self._graph[i] for i in self._graph.filter_nodes(lambda n: n.kind == kind)]
```

But this still does two lookups (one inside `filter_nodes` via the lambda, one in the list comprehension). The explicit loop with `node = self._graph[i]` is clearer and has fewer FFI calls.

**For `dependency.py:symbols_of_kind()`:**

```python
# BEFORE:
return [
    self.graph[i]
    for i in self.graph.node_indices()
    if self.graph[i].kind == kind  # Note: compares against string, not SymbolKind
]

# AFTER:
result = []
for i in self.graph.node_indices():
    node = self.graph[i]
    if node.kind == kind:
        result.append(node)
return result
```

---

### C3. Replace manual `to_dot()` with `graph.to_dot()` built-in

**File:** `src/tyo3/graph/export.py`, function `to_dot()` (lines ~16–61)
**Why:** The manual DOT generation is 45 lines, has incomplete escaping (only handles `"`, misses `\n`, `{`, `}`, `<`, `>`), and has the `max_nodes` bug (fixed in A7 if done separately). RustworkX's `to_dot()` handles all of this correctly.

**What to do:**

Replace the `to_dot()` function:

```python
def to_dot(graph: Any, *, max_nodes: int | None = None) -> str:
    g = graph.graph
    if max_nodes is not None:
        indices = list(g.node_indices())[:max_nodes]
        g, _ = g.subgraph_with_nodemap(indices)

    return g.to_dot(
        node_attr=lambda node: {
            "label": _dot_label(node),
            "color": _kind_color(str(node.kind.value)),
            "style": "dashed" if node.external else "solid",
            "fontname": "monospace",
            "shape": "box",
        },
        edge_attr=lambda edge: {
            "label": "" if edge.kind == EdgeKind.REFERENCES else edge.kind.value,
            "color": _edge_style(edge.kind.value)[1],
            "style": _edge_style(edge.kind.value)[0],
        },
        graph_attr={"rankdir": "LR"},
    )
```

**Keep** the `_dot_label()`, `_kind_color()`, and `_edge_style()` helper functions — they're still needed by the callbacks.

**Remove** the `_escape_dot()` helper — no longer needed, `to_dot()` handles escaping.

**Note:** If you already implemented A7, this supersedes it (the `max_nodes` bug is fixed by the `subgraph_with_nodemap` pre-filter). If A7 was done first, this step replaces the A7 changes entirely.

**Testing:** Existing `to_dot` tests may need minor adjustments since the exact DOT formatting may differ slightly from the manual version (e.g., attribute ordering, quoting). Update test assertions to match the new output format. The graph structure should be identical.

---

### C4. Batch node/edge construction in `_index_file()`

**File:** `src/tyo3/graph/graph.py`, method `_index_file()` and `_add_node()`/`_add_edge()`
**Why:** Each node and edge addition is a separate FFI call. Batching reduces overhead from O(symbols_per_file) to O(1) per file.

**What to do:**

This is a medium-effort refactor. The challenge is that `_add_node()` handles deduplication (`if node.symbol_id in self._id_to_index`). Batch insertion requires pre-filtering.

1. In `_index_file()`, collect all new nodes before adding:

```python
# Collect nodes, filtering out duplicates
new_nodes = []
for sym in symbols:
    node = self._make_symbol_node(file_str, sym)
    if node.symbol_id not in self._id_to_index:
        new_nodes.append(node)

# Add module node if not duplicate
module_node = SymbolNode(...)
if module_node.symbol_id not in self._id_to_index:
    new_nodes.insert(0, module_node)

# Batch add
if new_nodes:
    indices = self._graph.add_nodes_from(new_nodes)
    for node, idx in zip(new_nodes, indices):
        self._id_to_index[node.symbol_id] = idx
        self._file_to_nodes[node.file].append(idx)
```

2. Similarly for edges, collect all edge tuples and use `add_edges_from()`:

```python
edge_triples = []  # list of (src_idx, tgt_idx, EdgeData)
# ... build edge_triples during reference/inheritance resolution ...
if edge_triples:
    edge_indices = self._graph.add_edges_from(edge_triples)
    for edge_idx, (_, _, data) in zip(edge_indices, edge_triples):
        if data.file is not None:
            self._file_to_edges[data.file].append(edge_idx)
```

**Caveat:** This requires restructuring `_index_file()` to separate node collection from edge collection. If this is too disruptive, it can be deferred. The performance gain is proportional to the number of symbols per file.

**Testing:** All existing tests should pass.

---

### C5. Add constructor pre-allocation hints

**File:** `src/tyo3/graph/graph.py`, method `build()` (classmethod) or `__init__()`
**Why:** Pre-allocating graph capacity reduces memory reallocations during construction.

**What to do:**

In `build()`, after determining the file count, hint the graph size:

```python
graph = cls()
file_count = len(files)
# Pre-allocate with rough estimates: ~20 symbols per file, ~100 edges per file
graph._graph = rx.PyDiGraph(
    node_count_hint=file_count * 20,
    edge_count_hint=file_count * 100,
)
```

**Note:** Check if `rx.PyDiGraph` accepts `node_count_hint` and `edge_count_hint` as constructor kwargs. If not, check for `reserve_node_capacity()` / `reserve_edge_capacity()` methods. If neither exists, skip this step — it's zero-risk but also zero-value if the API doesn't support it.

**Testing:** No behavioral change. Existing tests pass.

---

## Phase D — Library-Wide Improvements (P3)

These affect code outside the graph module. Do them when there's bandwidth.

---

### D1. Optimize `_to_python()` — replace `dir()` introspection

**File:** `src/tyo3/rust_project.py`, function `_to_python()` (lines ~129–159)
**Why:** `dir()` on every PyO3 struct crossing the FFI boundary enumerates ALL attributes (including inherited/class ones), then checks each with `getattr()` and `callable()`. This is the hot path for every API call. The `except Exception: pass` swallows errors silently.

**NOTE!!!** We want to go with Option 2 immediately. 

**Option 1 — Python-side field registry (no Rust changes):**

Add a module-level dict mapping each known PyO3 type to its field names:

```python
_FIELD_MAP: dict[type, tuple[str, ...]] = {}

def _register_fields() -> None:
    """Populate field map for known PyO3 DTO types."""
    if _native is None:
        return
    _FIELD_MAP.update({
        _native.PositionDto: ("line", "column"),
        _native.RangeDto: ("start", "end"),
        # ... add all PyO3 DTOs with their exact field names ...
    })
```

Then in `_to_python()`:

```python
fields = _FIELD_MAP.get(type(obj))
if fields is not None:
    return {name: _to_python(getattr(obj, name)) for name in fields}
# Fallback to dir() for unknown types
```

**Option 2 — Rust-side `__fields__` (requires Rust rebuild):**

Add `#[classattr]` to each PyO3 struct:

```rust
#[classattr]
fn __fields__() -> Vec<&'static str> {
    vec!["line", "column"]
}
```

Then `_to_python()` checks `obj.__fields__` first.

**Recommendation:** Start with Option 2 - the performance and cleanliness gains are worth the effort.

**Testing:** All API tests should pass. Add a test that verifies `_to_python()` on a known PyO3 struct returns the expected dict.

---

### D2. Fix `_build_enum_cache()` — use explicit enum type list

**File:** `src/tyo3/rust_project.py`, function `_build_enum_cache()` (lines ~100–120)
**Why:** Detection relies on suffix conventions (`Kind`, `Type`, `Modifier`, `Role`) plus one hardcoded name. New enum types that don't match these patterns silently break.

**Option 1 — Python-side explicit list:**

```python
_KNOWN_ENUM_NAMES = [
    "NativeSymbolKind",
    "NativeSeverity",
    "NativeReferenceKind",
    "NativeReferenceRole",
    "NativeSemanticTokenType",
    "NativeSemanticTokenModifier",
    # ... add all known enum names ...
]

def _build_enum_cache() -> None:
    global _ENUM_TYPES, _ENUM_TYPES_BUILT
    if _ENUM_TYPES_BUILT or _native is None:
        return
    for name in _KNOWN_ENUM_NAMES:
        obj = getattr(_native, name, None)
        if obj is not None and isinstance(obj, type):
            _ENUM_TYPES.add(obj)
    _ENUM_TYPES_BUILT = True
```

**Option 2 — Rust-side `_TYO3_ENUM_TYPES` list (requires Rust rebuild).**

**Recommendation:** Option 1 first. Audit all enum types in the `_native` module by running:

```python
[name for name in dir(_native) if isinstance(getattr(_native, name), type) and not name.startswith('_')]
```

**Testing:** Existing tests should pass. Add a test that verifies all expected enum types are in the cache.

---

### D3. Add docstring clarifications for `ReferenceKind` vs `ReferenceRole`

**File:** `src/tyo3/models/navigation.py` (lines 19–34)
**Why:** Two overlapping enums with shared values (`READ`, `WRITE`, `OTHER`) but different semantics. Maintenance trap.

**What to do:**

Add docstrings:

```python
class ReferenceKind(StrEnum):
    """Classification of a reference from find_references() API.

    This is the raw reference kind from the Rust backend. Only three variants.
    For graph edge classification, see ReferenceRole instead.
    """
    READ = "read"
    WRITE = "write"
    OTHER = "other"


class ReferenceRole(StrEnum):
    """Semantic role of a name occurrence from file_occurrences() API.

    Used on EdgeData.role in the code graph and on NameOccurrence.role.
    Superset of ReferenceKind — adds IMPORT and DEFINITION.
    """
    READ = "read"
    WRITE = "write"
    IMPORT = "import"
    DEFINITION = "definition"
    OTHER = "other"
```

Also add a `# NOTE:` at the mapping site in `graph.py` (wherever `ReferenceKind` is converted to `ReferenceRole`, around `_resolve_references_for_symbol`).

---

### D4. Remove unused/speculative models

**Files:**
- `src/tyo3/models/core.py` — `TyProject` (lines 49–67), `ProjectFile` (lines 74–80)
- `src/tyo3/models/_spec.py` — `TyProjectConfig`, `BackendInfo`

**Why:** These models are defined and tested but never instantiated in production code. They add maintenance burden.

**What to do:**

1. Search for usages first:

```bash
grep -r "TyProject\b" src/ --include="*.py" | grep -v "test_" | grep -v "__pycache__"
grep -r "ProjectFile\b" src/ --include="*.py" | grep -v "test_" | grep -v "__pycache__"
grep -r "TyProjectConfig\b" src/ --include="*.py" | grep -v "test_" | grep -v "__pycache__"
grep -r "BackendInfo\b" src/ --include="*.py" | grep -v "test_" | grep -v "__pycache__"
```

2. If they're truly unused in production code:
   - Remove the model classes
   - Remove their tests
   - Remove any re-exports from `__init__.py`
   - Remove the `from tyo3.models._spec import ...` in `core.py`

3. If `_spec.py` becomes empty, delete the file.

**Testing:** Run the full test suite after removal. Some tests that directly test these models will need to be removed too.

---

### D5. Fix `to_json()` docstring

**File:** `src/tyo3/graph/export.py` (line ~134)
**Why:** Docstring says "Edge data is converted to a dict via `dataclasses.asdict`" but the implementation uses `_edge_to_dict()`.

**What to do:**

Update the docstring to accurately describe the implementation:

```python
def to_json(graph: Any) -> dict[str, Any]:
    """Export the code graph as a JSON-serializable dict.

    Nodes are serialized via ``model_dump(mode="json")``.
    Edges are serialized via ``_edge_to_dict()`` which extracts
    kind, file, role, and range fields.
    """
```

---

## Phase E — Future Features (P4)

Add these when consumers need them. Each is self-contained.

---

### E1. Add `import_cycle_groups()` via `rx.strongly_connected_components()`

**File:** `src/tyo3/graph/graph.py`
**Why:** Answers "which modules are in cycles?" without enumerating every cycle path. More useful for the common case.

```python
def import_cycle_groups(self) -> list[set[str]]:
    """Groups of mutually-dependent modules (strongly connected components)."""
    # Build module-level subgraph (reuse logic from import_cycles)
    module_indices = [
        i for i in self._graph.node_indices()
        if self._graph[i].kind == SymbolKind.MODULE
    ]
    if not module_indices:
        return []

    # Build module-to-module adjacency graph
    mod_graph = rx.PyDiGraph()
    # ... (use same module mapping from import_cycles, but build a separate PyDiGraph)

    sccs = rx.strongly_connected_components(mod_graph)
    return [
        {mod_graph[i] for i in scc}
        for scc in sccs if len(scc) > 1
    ]
```

---

### E2. Add `is_reachable()` via `rx.has_path()`

**File:** `src/tyo3/graph/graph.py`

```python
def is_reachable(self, from_sid: str, to_sid: str) -> bool:
    """Check if there is a directed path from one symbol to another."""
    src = self._id_to_index.get(from_sid)
    tgt = self._id_to_index.get(to_sid)
    if src is None or tgt is None:
        return False
    return rx.has_path(self._graph, src, tgt)
```

---

### E3. Add `has_cycles` property via `rx.is_directed_acyclic_graph()`

**File:** `src/tyo3/graph/graph.py`

```python
@property
def has_cycles(self) -> bool:
    """Quick check for any cycles in the graph."""
    return not rx.is_directed_acyclic_graph(self._graph)
```

---

### E4. Add `topological_order()` via `rx.topological_sort()`

**File:** `src/tyo3/graph/graph.py`

```python
def topological_order(self) -> list[str]:
    """Return symbol IDs in dependency order. Empty list if graph has cycles."""
    try:
        order = rx.topological_sort(self._graph)
    except rx.DAGHasCycle:
        return []
    return [self._graph[i].symbol_id for i in order]
```

---

### E5. Split `test_graph.py` into focused test files

**File:** `src/tyo3/tests/test_graph.py` (~1291 lines)
**Why:** Large monolithic test file is hard to navigate and maintain.

**Suggested split:**
- `test_graph_build.py` — construction, indexing, `_add_node`, `_add_edge`
- `test_graph_queries.py` — `symbols_of_kind`, `dependencies`, `dependents`, etc.
- `test_graph_references.py` — reference resolution, `_find_enclosing_symbol`
- `test_graph_inheritance.py` — `_resolve_inheritance`, INHERITS/OVERRIDES edges
- `test_graph_cycles.py` — `import_cycles`, `import_cycle_groups`
- `test_graph_export.py` — `to_dot`, `to_json`
- `test_graph_dependency.py` — `DependencyGraph` save/load
- `test_graph_update.py` — `update_file`, incremental updates

Move shared fixtures to `conftest.py` if not already there.

---

## Quick Reference: File Changes by Phase

| Phase | Files Modified |
|-------|---------------|
| A (correctness) | `graph.py`, `dependency.py`, `export.py` |
| B (performance) | `graph.py` |
| C (consistency) | `graph.py`, `export.py` |
| D (library-wide) | `rust_project.py`, `navigation.py`, `models/core.py`, `models/_spec.py`, `export.py` |
| E (features) | `graph.py`, `tests/test_graph.py` |

## Running Tests

After each step:

```bash
devenv shell -- tests
```

For faster iteration during development:

```bash
devenv shell -- test-quick
```

For the full CI suite before merging:

```bash
devenv shell -- test-ci
```
