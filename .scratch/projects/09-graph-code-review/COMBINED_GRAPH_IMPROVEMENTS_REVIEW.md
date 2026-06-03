# TyO3 Combined Graph & Library Improvements Review

**Date:** 2026-06-03
**Scope:** All RustworkX call sites across `graph.py`, `dependency.py`, `export.py`, plus the Python bridge layer (`rust_project.py`) and domain model concerns.
**Sources:** Initial audit (GRAPH_CODE_REVIEW.md, GRAPH_OPTIMIZATION_OVERVIEW.md), independent code review, and RustworkX API verification against our installed rustworkx 0.16+.

---

## Table of Contents

1. [RustworkX Usage Audit](#1-rustworkx-usage-audit)
2. [Correctness Bugs (P0)](#2-correctness-bugs-p0)
3. [Performance Hotspots (P1)](#3-performance-hotspots-p1)
4. [RustworkX API Optimizations (P2)](#4-rustworkx-api-optimizations-p2)
5. [Library-Wide Improvements (P3)](#5-library-wide-improvements-p3)
6. [Export & Serialization Issues (P4)](#6-export--serialization-issues-p4)
7. [Future-Facing APIs](#7-future-facing-apis)
8. [Prioritized Implementation Plan](#8-prioritized-implementation-plan)
9. [Appendix: RustworkX API Reference](#9-appendix-rustworkx-api-reference)

---

## 1. RustworkX Usage Audit

All `self._graph.*` call sites across the codebase, mapped against RustworkX's available API. Line numbers reference the current state of the code.

### 1.1 `graph.py` — 28 call sites

| Line | Method | API Call | Context |
|------|--------|----------|---------|
| 533 | `_resolve_inheritance` | `neighbors()` | BFS — get successors of current node |
| 534 | `_resolve_inheritance` | `get_all_edge_data()` | BFS — filter INHERITS edges per neighbour |
| 591 | `_add_node` | `add_node()` | Single node insertion |
| 604 | `_add_edge` | `add_edge()` | Single edge insertion |
| 653 | `_edges_of_kind` | `in_edges()` | Incoming edge iteration (Phase 3 optimization) |
| 657 | `_edges_of_kind` | `out_edges()` | Outgoing edge iteration (Phase 3 optimization) |
| 671 | `node_count` | `num_nodes()` | Property |
| 675 | `edge_count` | `num_edges()` | Property |
| 692 | `symbols_of_kind` | `node_indices()` | Full graph scan, filter by kind |
| 751 | `dependencies` | `neighbors()` | Direct successors |
| 761 | `dependents` | `predecessor_indices()` | Direct predecessors |
| 794 | `import_cycles` | `node_indices()` | Collect MODULE nodes |
| 806 | `import_cycles` | `node_indices()` | **O(M×N) nested loop** — module mapping |
| 815 | `import_cycles` | `edge_indices()` | Full edge scan for inter-module deps |
| 817 | `import_cycles` | `get_edge_data_by_index()` | Per-edge data lookup with try/except |
| 822 | `import_cycles` | `get_edge_endpoints_by_index()` | Per-edge endpoint lookup |
| 897 | `subgraph_for_file` | `neighbors()` | Collect successor neighbours |
| 899 | `subgraph_for_file` | `predecessor_indices()` | Collect predecessor neighbours |
| 908 | `subgraph_for_file` | `edge_indices()` | **O(E) full graph scan** to copy edges |
| 909 | `subgraph_for_file` | `get_edge_endpoints_by_index()` | Per-edge |
| 911 | `subgraph_for_file` | `get_edge_data_by_index()` | Per-edge |
| 933 | `update_file` | `remove_node()` | **Loop with sorted(reverse=True) + try/except** |
| 944 | `update_file` | `remove_edge_from_index()` | Loop with sorted(reverse=True) + try/except |
| 962 | `coupling_between` | `edge_indices()` | **O(E) full graph scan** |
| 963 | `coupling_between` | `get_edge_endpoints_by_index()` | Per-edge |
| 964 | `coupling_between` | `get_edge_data_by_index()` | Per-edge |
| 977 | `external_symbols` | `node_indices()` | Full scan, filter by `.external` |

### 1.2 `dependency.py` — 7 call sites

| Line | Method | API Call | Context |
|------|--------|----------|---------|
| 50-52 | `symbols_of_kind` | `node_indices()` + `graph[i]` ×2 | Double lookup per node |
| 57 | `all_symbols` | `node_indices()` | Iterate all nodes |
| 70-71 | `save` | `node_indices()` + `graph[idx]` | Serialize nodes |
| 75-76 | `save` | `edge_indices()` + `get_edge_endpoints_by_index()` | Serialize edges — **drops EdgeData** |
| 107 | `load` | `add_node()` | Reconstruct nodes |
| 111-112 | `load` | `add_edge()` | Reconstruct edges — **fragile index assumption** |

### 1.3 `export.py` — 8 call sites

| Line | Method | API Call | Context |
|------|--------|----------|---------|
| 31 | `to_dot` | `node_indices()` | Iterate nodes |
| 46 | `to_dot` | `edge_indices()` | Iterate edges |
| 50 | `to_dot` | `get_edge_endpoints_by_index()` | Per-edge |
| 53 | `to_dot` | `get_edge_data_by_index()` | Per-edge |
| 141 | `to_json` | `node_indices()` | Iterate nodes |
| 145-147 | `to_json` | `edge_indices()` + endpoints + data | Standard edge iteration |

---

## 2. Correctness Bugs (P0)

These are silent data corruption or logic errors that must be fixed before any optimization work.

### 2.1 `update_file()` — Index Invalidation Bug

**Location:** `graph.py:918-953`

**Current code:**
```python
for idx in sorted(old_indices, reverse=True):
    try:
        self._graph.remove_node(idx)
    except Exception:
        pass
```

**The bug:** RustworkX's `remove_node(idx)` uses **swap-and-pop** internally. Removing node 5 from a 10-node graph moves node 9 into slot 5. This corrupts:

1. **`_id_to_index`** — the symbol formerly at index 9 still maps to 9, but is now at 5.
2. **`_file_to_nodes`** for other files — they still reference index 9, which is now invalid or points to a different node.
3. **`_file_to_edges`** — edge indices may reference edges that were implicitly removed when their incident nodes were deleted.

The `sorted(reverse=True)` mitigates one case (removing highest indices first avoids shifting lower ones), but doesn't prevent corruption when multiple indices are removed and the swapped-in nodes belong to other files.

The `try/except: pass` silently swallows the resulting errors, making the corruption invisible.

**Additionally:** incoming cross-file edges are not cleaned up. If file B has a REFERENCES edge pointing into file A, and file A is re-indexed, the old edge from B persists alongside the newly-created edge — resulting in duplicate edges.

**Fix:**
```python
def update_file(self, session: TyO3Session, path: str) -> None:
    old_indices = list(self._file_to_nodes.get(path, []))
    if old_indices:
        self._graph.remove_nodes_from(old_indices)
        self._rebuild_indexes()
    self._diagnostics.pop(path, None)
    self._index_file(session, path)

def _rebuild_indexes(self) -> None:
    """Reconstruct all secondary indexes from the graph's current state."""
    self._id_to_index.clear()
    self._file_to_nodes.clear()
    self._file_to_edges.clear()
    for idx in self._graph.node_indices():
        node = self._graph[idx]
        self._id_to_index[node.symbol_id] = idx
        self._file_to_nodes[node.file].append(idx)
    for edge_idx in self._graph.edge_indices():
        data = self._graph.get_edge_data_by_index(edge_idx)
        if data is not None and data.file is not None:
            self._file_to_edges[data.file].append(edge_idx)
```

`remove_nodes_from(index_list)`:
- Accepts any iterable of node indices — no sorting required
- Handles index compaction internally in a single Rust call
- Silently ignores indices not in the graph
- Automatically removes incident edges

**Verified:** `remove_nodes_from` is available and works as described in our rustworkx 0.16+.

### 2.2 `import_cycles()` — O(M×N) Module Mapping

**Location:** `graph.py:806-808`

**Current code:**
```python
for mi in module_indices:
    module_sid = self._graph[mi].symbol_id
    file = file_from_symbol_id(module_sid)
    for ni in self._graph.node_indices():  # O(N) per module!
        if self._graph[ni].file == file:
            node_to_module[ni] = module_sid
```

For M modules and N total nodes, this is O(M × N) graph lookups. The `_file_to_nodes` index already provides the exact mapping needed.

**Fix:**
```python
for mi in module_indices:
    module_sid = self._graph[mi].symbol_id
    file = file_from_symbol_id(module_sid)
    for ni in self._file_to_nodes.get(file, []):
        node_to_module[ni] = module_sid
```

This makes it O(N) total — each node is visited exactly once. Trivial fix, major impact at scale.

### 2.3 `_find_enclosing_symbol()` — Incorrect Size Comparison Heuristic

**Location:** `graph.py:572`

**Current code:**
```python
size = (nr.end.line - nr.start.line) * 10000 + (nr.end.column - nr.start.column)
```

This scalar approximation produces incorrect results when:
- A symbol spans more than 10,000 columns (possible in generated code)
- `end.column < start.column` on the same line (the column difference goes negative, but the line difference dominates — subtly wrong)

**Fix:** Use tuple comparison:
```python
size = (nr.end.line - nr.start.line, nr.end.column - nr.start.column)
```

Python's lexicographic tuple comparison is the semantically correct approach and has zero overhead.

### 2.4 `dependency.py save()` — Edge Data Silently Dropped

**Location:** `dependency.py:75-77`

**Current code:**
```python
for edge_idx in self.graph.edge_indices():
    src, tgt = self.graph.get_edge_endpoints_by_index(edge_idx)
    edges.append({"src": src, "tgt": tgt})
```

Edge data (`EdgeData` — kind, file, range, role) is not serialized. When `load()` reconstructs the graph, it passes `None` as edge data. Any downstream query that checks `edge.kind` on a loaded `DependencyGraph` will fail.

**Fix:** Serialize edge data alongside endpoints:
```python
for edge_idx in self.graph.edge_indices():
    src, tgt = self.graph.get_edge_endpoints_by_index(edge_idx)
    raw = self.graph.get_edge_data_by_index(edge_idx)
    edge_dict = {"src": src, "tgt": tgt}
    if raw is not None:
        edge_dict["data"] = {"kind": raw.kind.value}
        if raw.file:
            edge_dict["data"]["file"] = raw.file
    edges.append(edge_dict)
```

### 2.5 `dependency.py load()` — Fragile Index Assumption

**Location:** `dependency.py:110-112`

**Current code:**
```python
if edge_data["src"] < graph.num_nodes() and edge_data["tgt"] < graph.num_nodes():
    graph.add_edge(edge_data["src"], edge_data["tgt"], None)
```

This assumes `add_node()` assigns indices 0, 1, 2... sequentially, and that the serialized `src`/`tgt` values map directly to those indices. This is true for a fresh `PyDiGraph`, but it's an implicit coupling to RustworkX internals.

**Fix:** Save symbol_ids on edges instead of raw indices, and resolve via `id_to_index` during load:
```python
# In save():
edges.append({"src_id": self.graph[src].symbol_id,
              "tgt_id": self.graph[tgt].symbol_id})

# In load():
src_idx = id_to_index.get(edge_data["src_id"])
tgt_idx = id_to_index.get(edge_data["tgt_id"])
if src_idx is not None and tgt_idx is not None:
    graph.add_edge(src_idx, tgt_idx, edge_data_obj)
```

### 2.6 `export.py to_dot()` — `max_nodes` Guard Broken After Deletions

**Location:** `export.py:51`

**Current code:**
```python
if max_nodes is not None and (src >= max_nodes or tgt >= max_nodes):
    continue
```

This uses node index magnitude as a proxy for "is this node within the first N." After node deletions, RustworkX indices are non-contiguous (e.g., indices might be [0, 1, 3, 7, 12] after removals). Index 7 doesn't mean "the 7th node." This silently excludes valid low-count nodes with high indices and includes edges to already-skipped nodes.

**Fix:** Track which node indices were actually emitted, then filter edges against that set:
```python
emitted: set[int] = set()
count = 0
for idx in g.node_indices():
    if max_nodes is not None and count >= max_nodes:
        break
    emitted.add(idx)
    count += 1
    # ... emit node ...

for edge_idx in g.edge_indices():
    src, tgt = g.get_edge_endpoints_by_index(edge_idx)
    if src not in emitted or tgt not in emitted:
        continue
    # ... emit edge ...
```

Or better: use `subgraph_with_nodemap` to pre-filter and pass the result to `to_dot()`.

---

## 3. Performance Hotspots (P1)

These are the methods where algorithmic complexity causes real build-time or query-time slowdowns, ordered by estimated impact.

### 3.1 `_find_enclosing_symbol()` — O(nodes_per_file) per Occurrence

**Location:** `graph.py:553-583`

**This is the single largest build-time bottleneck and was overlooked in the initial optimization audit.**

```python
def _find_enclosing_symbol(self, file_str: str, range: Range) -> str | None:
    node_indices = self._file_to_nodes.get(file_str, [])
    best_id: str | None = None
    best_size: int = sys.maxsize
    for idx in node_indices:                    # O(nodes in file)
        node: SymbolNode = self._graph[idx]     # FFI call per node
        if node.kind == SymbolKind.MODULE:
            continue
        nr = node.range
        if (...contains check...):
            size = (nr.end.line - nr.start.line) * 10000 + ...
            if size < best_size:
                best_size = size
                best_id = node.symbol_id
```

This is called **once per occurrence** during `_resolve_references_via_occurrences()`. For a file with 200 symbols and 500 name occurrences, that's **100,000 `self._graph[idx]` FFI calls** per file. Over a project with 100 files, this easily becomes the dominant cost of `build()`.

**Fix — Pre-sort + binary search:**

During `_index_file()`, after all nodes for the file are added, build a sorted structure:

```python
# In _index_file(), after all nodes added:
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

Then `_find_enclosing_symbol` iterates this pre-materialized list (no FFI per call) and uses early termination. The cache is built once per file, amortized across all occurrences.

For even better performance, an **interval tree** (e.g., `intervaltree` package, or a simple sorted list with bisect) would reduce lookup from O(N) to O(log N + matches), but even the pre-materialized list eliminates the FFI-per-node cost.

### 3.2 `_find_symbol_in_file()` — O(all_symbols) Fallback Scan

**Location:** `graph.py:158-161`

```python
prefix = f"{file_path}::{name}@"
for sid in self._id_to_index:    # ALL symbols across ALL files
    if sid.startswith(prefix):
        return sid
```

This linear scan over the entire `_id_to_index` dictionary is triggered when the exact `file::name` lookup fails and a `name@line` format is suspected. It runs inside the occurrence-processing loop — for each unresolved name, every symbol ID in the project is checked.

**Fix:** Add a secondary index for the `name@line` pattern:
```python
# In _add_node():
if "@" in node.symbol_id:
    # Index by (file, name_prefix) for fast name@line lookups
    parts = node.symbol_id.split("::", 1)
    if len(parts) == 2:
        name_part = parts[1].split("@", 1)[0]
        self._name_to_sids[(parts[0], name_part)] = node.symbol_id
```

Then `_find_symbol_in_file` checks `self._name_to_sids.get((file_path, name))` — O(1) instead of O(N).

### 3.3 `subgraph_for_file()` — O(E_total) Edge Scan

**Location:** `graph.py:908-912`

```python
for edge_idx in self._graph.edge_indices():       # ALL edges in graph
    src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
    if src in old_to_new and tgt in old_to_new:
        data = self._graph.get_edge_data_by_index(edge_idx)
        sub.add_edge(old_to_new[src], old_to_new[tgt], data)
```

For a graph with 200,000 edges, every `subgraph_for_file()` call scans all 200,000 even though the subgraph typically has 20–200 nodes and a few hundred edges.

**Fix — Single Rust call:**
```python
def subgraph_for_file(self, file_path: str) -> rx.PyDiGraph:
    core_indices = set(self._file_to_nodes.get(file_path, []))
    if not core_indices:
        return rx.PyDiGraph()

    included = set(core_indices)
    for ci in core_indices:
        for succ in self._graph.neighbors(ci):
            included.add(succ)
        for pred in self._graph.predecessor_indices(ci):
            included.add(pred)

    sub, _ = self._graph.subgraph_with_nodemap(list(included))
    return sub
```

`subgraph_with_nodemap(node_list)` copies nodes and all edges between them in a single Rust call. Returns `(subgraph, NodeMap)` where NodeMap maps old → new indices. Replaces 13 lines with 2.

**Verified:** Available and functional in our rustworkx 0.16+.

### 3.4 `coupling_between()` — O(E_total) Scan

**Location:** `graph.py:957-969`

Iterates every edge in the entire graph to count references between two specific files.

**Fix — Targeted iteration:**
```python
def coupling_between(self, file_a: str, file_b: str) -> int:
    nodes_a = set(self._file_to_nodes.get(file_a, []))
    nodes_b = set(self._file_to_nodes.get(file_b, []))
    if not nodes_a or not nodes_b:
        return 0
    count = 0
    for idx in nodes_a:
        for _, tgt, data in self._graph.out_edges(idx):
            if data.kind == EdgeKind.REFERENCES and tgt in nodes_b:
                count += 1
    for idx in nodes_b:
        for _, tgt, data in self._graph.out_edges(idx):
            if data.kind == EdgeKind.REFERENCES and tgt in nodes_a:
                count += 1
    return count
```

Complexity drops from O(E_total) to O(degree(file_a) + degree(file_b)).

---

## 4. RustworkX API Optimizations (P2)

These are call-site improvements where a better RustworkX API exists. Individually small, collectively worthwhile.

### 4.1 `_resolve_inheritance()` BFS → `out_edges()`

**Location:** `graph.py:532-536`

**Current pattern:**
```python
for succ_idx in self._graph.neighbors(current_idx):
    for edge_data in self._graph.get_all_edge_data(current_idx, succ_idx):
        if edge_data.kind != EdgeKind.INHERITS:
            continue
```

This is the same `neighbors()` + `get_all_edge_data()` pattern that Phase 3 replaced in `_edges_of_kind`. Two FFI calls per neighbour.

**Fix — Consistent with Phase 3:**
```python
for _src, succ_idx, edge_data in self._graph.out_edges(current_idx):
    if edge_data.kind != EdgeKind.INHERITS:
        continue
```

Single Rust call, no per-neighbour `get_all_edge_data()`. Low impact but eliminates the last inconsistency.

### 4.2 `symbols_of_kind()` / `external_symbols()` — Double Lookup

**Location:** `graph.py:691-693`, `graph.py:975-978`

**Current pattern:**
```python
return [
    self._graph[i]              # lookup 1
    for i in self._graph.node_indices()
    if self._graph[i].kind == kind   # lookup 2 (same node!)
]
```

Two `self._graph[i]` FFI calls per node — once for the filter, once for the return value.

**Fix — Bind once:**
```python
def symbols_of_kind(self, kind: SymbolKind) -> list[SymbolNode]:
    result = []
    for i in self._graph.node_indices():
        node = self._graph[i]
        if node.kind == kind:
            result.append(node)
    return result
```

Or use `filter_nodes()`:
```python
def symbols_of_kind(self, kind: SymbolKind) -> list[SymbolNode]:
    return [self._graph[i] for i in self._graph.filter_nodes(lambda n: n.kind == kind)]
```

The same pattern applies to `external_symbols()`, `dependency.py:symbols_of_kind()`, and the MODULE node collection in `import_cycles()` (line 793-796). Each site has the same double-lookup.

### 4.3 Batch Node/Edge Construction

**Current:** `_index_file()` adds nodes and edges one at a time via `_add_node()` and `_add_edge()`, each making a separate Rust FFI call.

**Opportunity:** Collect all nodes for a file, batch-add with `add_nodes_from()`, then batch-add edges with `add_edges_from()`:

```python
new_nodes = [module_node] + [self._make_symbol_node(file_str, sym) for sym in symbols]
indices = self._graph.add_nodes_from(new_nodes)
for node, idx in zip(new_nodes, indices):
    self._id_to_index[node.symbol_id] = idx
    self._file_to_nodes[node.file].append(idx)
```

**Impact:** Reduces Python→Rust FFI overhead from O(symbols_per_file) calls to O(1) per file for nodes and O(1) for edges. Meaningful for large files with many symbols.

**Caveat:** Requires refactoring `_add_node()` since it currently handles deduplication (line 589: `if node.symbol_id in self._id_to_index`). Batch insertion would need pre-filtering.

### 4.4 Constructor Pre-allocation Hints

**Current:** `rx.PyDiGraph()` with no size hints.

```python
# During build(), after counting files:
file_count = len(files)
graph._graph = rx.PyDiGraph(
    node_count_hint=file_count * 20,     # ~20 symbols per file
    edge_count_hint=file_count * 100,    # ~100 edges per file
)
```

Reduces memory reallocations during graph construction. Zero-risk change.

### 4.5 `to_dot()` → `graph.to_dot()` Built-in

**Location:** `export.py:16-61`

The current 45-line manual DOT generation can be replaced with RustworkX's built-in:

```python
def to_dot(graph: Any, *, max_nodes: int | None = None) -> str:
    g = graph.graph
    if max_nodes is not None:
        # Pre-filter using subgraph
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

**Benefits:**
- Correct DOT escaping guaranteed (the current manual escaping on line 39 only handles `"`, misses `\n`, `{`, `}`, `<`, `>`)
- Fixes the `max_nodes` index-magnitude bug (Section 2.6)
- ~30 lines removed

**Verified:** `to_dot()` is available and accepts `node_attr`, `edge_attr`, `graph_attr` callables.

---

## 5. Library-Wide Improvements (P3)

These affect the broader TyO3 library beyond graph code.

### 5.1 `_to_python()` — `dir()` Introspection on the FFI Hot Path

**Location:** `rust_project.py:129-159`

```python
def _to_python(obj: Any) -> Any:
    # ...
    result: dict[str, Any] = {}
    for name in dir(obj):          # enumerates ALL attributes
        if name.startswith("_"):
            continue
        try:
            val = getattr(obj, name)
            if not callable(val):
                result[name] = _to_python(val)
        except Exception:
            pass                   # silent failure
    return result
```

**Problems:**
1. **Performance:** `dir()` returns all attributes including inherited/class ones. For every PyO3 struct crossing the FFI boundary, this does full attribute enumeration → `getattr()` → `callable()` check → recursive conversion. This is the hot path for *every* API call.
2. **Silent error swallowing:** `except Exception: pass` means any attribute access failure produces an empty dict with no indication.
3. **Fragility:** Non-dunder methods added to PyO3 structs would be incorrectly included as "fields."

**Recommendation:** Add a `__fields__` class attribute to each Rust DTO via `#[classattr]`:
```rust
#[classattr]
fn __fields__() -> Vec<&'static str> {
    vec!["line", "column"]
}
```

Then `_to_python` iterates only `obj.__fields__` — O(fields) instead of O(dir()), and failures are explicit.

**Alternative (Python-side, no Rust changes):** Maintain a field registry:
```python
_FIELD_MAP: dict[type, list[str]] = {
    _native.PositionDto: ["line", "column"],
    _native.RangeDto: ["start", "end"],
    # ...
}
```

This is less elegant but requires no Rust rebuild.

### 5.2 `_build_enum_cache()` — Naming Convention Coupling

**Location:** `rust_project.py:100-120`

Detection of PyO3 enum types relies on suffix conventions (`Kind`, `Type`, `Modifier`, `Role`) plus one hardcoded name (`NativeSeverity`). A new DTO enum that doesn't follow these conventions silently breaks — the enum variant gets converted via `dir()` introspection instead of `str()`, producing a dict instead of a string, which Pydantic then rejects.

**Recommendation:** Have the Rust module expose a `_TYO3_ENUM_TYPES` list attribute:
```rust
#[pymodule]
fn _native_impl(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // ... existing registrations ...
    m.add("_TYO3_ENUM_TYPES", vec![
        m.getattr("NativeSymbolKind")?,
        m.getattr("NativeSeverity")?,
        // ...
    ])?;
    Ok(())
}
```

Then `_build_enum_cache` uses `_native._TYO3_ENUM_TYPES` directly — no name matching, no silent failures.

### 5.3 `ReferenceKind` vs `ReferenceRole` Confusion

Two overlapping enums exist:
- **`ReferenceKind`** (`navigation.py:19-24`): `READ`, `WRITE`, `OTHER` — from `find_references()` API
- **`ReferenceRole`** (`navigation.py:27-34`): `READ`, `WRITE`, `IMPORT`, `DEFINITION`, `OTHER` — from `file_occurrences()` API

They share values (`READ`, `WRITE`, `OTHER`) but come from different sources and have different semantics. The graph code manually maps between them in `_resolve_references_for_symbol` (line 200-205). This is functional but the naming similarity is a maintenance trap.

**Recommendation:** Add a docstring to each enum explicitly noting the source API and the distinction, and add a `# NOTE:` at the mapping site.

### 5.4 Unused / Speculative Models

**`TyProject`** (`models/core.py:49-72`) and **`ProjectFile`** (`models/core.py:74-81`) are fully defined and tested but never instantiated in production code. **`TyProjectConfig`** and **`BackendInfo`** (`models/_spec.py`) are explicitly marked as "spec-anticipation models."

These add maintenance burden and test surface without delivering value. They should be removed or gated behind a `_draft` namespace to signal they're not part of the active API.

### 5.5 Thread Safety Note

`RustProject._closed` is a plain bool. Under free-threaded Python 3.13+, there's a TOCTOU race between `_check_open()` and the actual Rust call. The Rust-side `Mutex` provides real protection, but the Python flag could give false negatives under concurrent access. Worth documenting even if not immediately actionable.

---

## 6. Export & Serialization Issues (P4)

### 6.1 `to_json()` Docstring Inaccuracy

**Location:** `export.py:134`

The docstring says "Edge data is converted to a dict via `dataclasses.asdict`" but the implementation uses manual dict construction via `_edge_to_dict()`. Minor, but the discrepancy could mislead contributors.

### 6.2 `rx.node_link_json()` — Not Recommended

RustworkX offers `node_link_json()` for standard D3-style JSON output. However, our `to_json()` uses `model_dump(mode="json")` for nodes and `_edge_to_dict()` for edges — giving full control over the schema. Switching to `node_link_json()` would lose this control for marginal code reduction. **Keep the current approach** but fix the docstring.

---

## 7. Future-Facing APIs

These RustworkX APIs are available in our installed version but don't map to current functionality. They become relevant as new query methods are added.

### 7.1 `rx.strongly_connected_components(graph)` — Circular Dependency Clusters

Returns maximal sets of mutually-reachable nodes using Tarjan's algorithm in O(V+E). More useful than `simple_cycles` for the common question "which modules are in cycles" without enumerating every cycle path (which can be exponential in pathological cases).

```python
def import_cycle_groups(self) -> list[set[str]]:
    """Groups of mutually-dependent modules."""
    # Build module-level graph, then:
    sccs = rx.strongly_connected_components(mod_graph)
    return [
        {mod_graph[i] for i in scc}
        for scc in sccs if len(scc) > 1
    ]
```

**Verdict:** Worth adding as a separate method alongside `import_cycles()`. Different question, different answer format.

### 7.2 `rx.has_path(graph, src, tgt)` — Targeted Reachability

Currently `transitive_dependencies` uses `rx.descendants()` which computes ALL reachable nodes. For a simple "does A depend on B?" query, `has_path` stops at first hit.

```python
def is_reachable(self, from_sid: str, to_sid: str) -> bool:
    src = self._id_to_index.get(from_sid)
    tgt = self._id_to_index.get(to_sid)
    if src is None or tgt is None:
        return False
    return rx.has_path(self._graph, src, tgt)
```

### 7.3 `rx.is_directed_acyclic_graph(graph)` — Quick Cyclicity Check

Fast pre-check before expensive cycle enumeration:
```python
@property
def has_cycles(self) -> bool:
    return not rx.is_directed_acyclic_graph(self._graph)
```

### 7.4 `rx.topological_sort(graph)` — Dependency Ordering

```python
def topological_order(self) -> list[str]:
    try:
        order = rx.topological_sort(self._graph)
    except rx.DAGHasCycle:
        return []
    return [self._graph[i].symbol_id for i in order]
```

### 7.5 `rx.transitive_reduction(graph)` — Simplified Dependency Visualization

Removes edges implied by transitive paths. Useful for clean dependency visualizations.

### 7.6 `rx.simple_cycles(graph)` — Full Cycle Enumeration

Available but **not recommended** as a replacement for the current `import_cycles()` DFS. The current hand-rolled DFS operates directly on a `dict[str, set[str]]` adjacency structure — no need to build a temporary `PyDiGraph`. The real bottleneck is the O(M×N) module mapping (Section 2.2), not the DFS itself. Fix the mapping; leave the DFS.

If full cycle enumeration is ever needed on a heavily cyclic graph, `simple_cycles` would be appropriate. But for typical well-structured Python projects with few or no import cycles, the DFS cost is negligible.

### 7.7 `rx.edge_subgraph(graph, edge_indices)` — Edge-Filtered Subgraphs

Could extract just REFERENCES edges into a lightweight graph for coupling analysis. Relevant if coupling queries become a hot path.

---

## 8. Prioritized Implementation Plan

### P0 — Must Fix (Correctness)

| # | Fix | Location | Effort | Notes |
|---|-----|----------|--------|-------|
| 1 | `update_file()` → `remove_nodes_from` + `_rebuild_indexes()` | `graph.py:918-953` | Medium | Silent data corruption bug. Requires adding `_rebuild_indexes()` method. |
| 2 | `import_cycles()` O(M×N) → use `_file_to_nodes` | `graph.py:806-808` | Trivial | 3-line change. |
| 3 | `_find_enclosing_symbol()` size heuristic → tuple comparison | `graph.py:572` | Trivial | 1-line change. |
| 4 | `dependency.py save()` → serialize edge data | `dependency.py:75-77` | Small | Prevents silent data loss on round-trip. |
| 5 | `dependency.py load()` → symbol_id-based edge reconstruction | `dependency.py:110-112` | Small | Eliminates implicit index assumption. |
| 6 | `export.py to_dot()` → fix `max_nodes` guard | `export.py:51` | Small | Track emitted set, or use subgraph pre-filter. |

### P1 — Must Improve (Performance)

| # | Fix | Location | Effort | Impact |
|---|-----|----------|--------|--------|
| 7 | `_find_enclosing_symbol()` → pre-materialized range cache | `graph.py:553-583` | Medium | Biggest single build-time win. Eliminates O(N_file) FFI calls per occurrence. |
| 8 | `subgraph_for_file()` → `subgraph_with_nodemap` | `graph.py:902-912` | Small | O(E_total) → O(subgraph). 13 lines → 2. |
| 9 | `coupling_between()` → targeted `out_edges()` | `graph.py:957-969` | Small | O(E_total) → O(degree). |
| 10 | `_find_symbol_in_file()` → secondary index for `name@line` | `graph.py:158-161` | Small | O(all_symbols) → O(1) per lookup. |

### P2 — Should Improve (Code Quality / Consistency)

| # | Fix | Location | Effort | Impact |
|---|-----|----------|--------|--------|
| 11 | `_resolve_inheritance()` BFS → `out_edges()` | `graph.py:532-536` | Trivial | Consistency with Phase 3. |
| 12 | Double `self._graph[i]` lookups → bind once | Multiple sites | Trivial | Minor FFI reduction. |
| 13 | `to_dot()` → use `graph.to_dot()` built-in | `export.py:16-61` | Medium | Correct escaping, ~30 lines removed. |
| 14 | Batch `add_nodes_from()` / `add_edges_from()` | `graph.py:_index_file` | Medium | Fewer FFI calls during build. |
| 15 | Constructor pre-allocation hints | `graph.py:29` | Trivial | Fewer memory reallocations. |

### P3 — Should Improve (Library-Wide)

| # | Fix | Location | Effort | Impact |
|---|-----|----------|--------|--------|
| 16 | `_to_python()` → explicit field lists | `rust_project.py:129-159` | Medium-Large | Improves every API call. Requires Rust-side or Python-side registry. |
| 17 | `_build_enum_cache()` → explicit enum type list | `rust_project.py:100-120` | Medium | Eliminates naming convention fragility. |
| 18 | Remove unused models (`TyProject`, `ProjectFile`, etc.) | `models/` | Small | Reduce maintenance burden. |
| 19 | Add `to_json()` docstring fix | `export.py:134` | Trivial | Accuracy. |

### P4 — Nice to Have (Future Features)

| # | Feature | Effort | Notes |
|---|---------|--------|-------|
| 20 | `import_cycle_groups()` via `rx.strongly_connected_components()` | Small | New method, separate from `import_cycles()`. |
| 21 | `is_reachable()` via `rx.has_path()` | Small | Targeted reachability query. |
| 22 | `has_cycles` property via `rx.is_directed_acyclic_graph()` | Trivial | Quick cyclicity check. |
| 23 | `topological_order()` via `rx.topological_sort()` | Small | Dependency ordering. |
| 24 | Build-phase profiling hooks / `build_warnings` counter | Medium | Measure before further optimization. |
| 25 | Split `test_graph.py` (~54KB) into focused test files | Medium | Maintainability. |

### Recommended Sequencing

**Phase A (correctness):** Items 1-6. Do all P0 fixes first. Item 1 (`update_file` + `_rebuild_indexes`) is the foundation — other changes may invalidate secondary indexes, and having `_rebuild_indexes` as a safety net is essential.

**Phase B (build performance):** Items 7-10. These improve `build()` time. Item 7 (`_find_enclosing_symbol` cache) is the biggest win and should be profiled before and after to validate. Item 8 (`subgraph_with_nodemap`) is the cleanest single change.

**Phase C (consistency):** Items 11-15. These are low-risk, low-effort improvements that follow naturally from Phase A/B work. Can be done incrementally.

**Phase D (library):** Items 16-19. The `_to_python` fix (item 16) has the broadest impact but requires the most coordination (Rust-side changes or a maintained Python-side registry). Schedule when there's a natural Rust rebuild cycle.

**Phase E (features):** Items 20-25. Add when the corresponding query methods are needed by consumers.

---

## 9. Appendix: RustworkX API Reference

### Verified Available (rustworkx 0.16+)

All APIs listed below have been verified against our installed version.

#### Edge Iteration

| API | Returns | Use Case |
|-----|---------|----------|
| `in_edges(idx)` | `WeightedEdgeList[(src, tgt, data)]` | Incoming edges with data |
| `out_edges(idx)` | `WeightedEdgeList[(src, tgt, data)]` | Outgoing edges with data |
| `edge_indices()` | `EdgeIndices` | All edge indices in graph |
| `get_all_edge_data(src, tgt)` | `list[data]` | All parallel edges between pair |
| `get_edge_data_by_index(idx)` | `data` | Single edge by index |

#### Node Iteration

| API | Returns | Use Case |
|-----|---------|----------|
| `node_indices()` | `NodeIndices` | All node indices |
| `filter_nodes(predicate)` | `NodeIndices` | Filtered node indices |
| `neighbors(idx)` | `NodeIndices` | Successor indices |
| `predecessor_indices(idx)` | `NodeIndices` | Predecessor indices |

#### Graph Manipulation

| API | Behaviour |
|-----|-----------|
| `add_nodes_from(list)` | Batch add; returns list of new indices |
| `add_edges_from(list)` | Batch add `(src, tgt, data)` triples |
| `remove_nodes_from(indices)` | Bulk remove; handles index shifts; ignores invalid |
| `remove_edge_from_index(idx)` | Single edge by index |
| `subgraph_with_nodemap(nodes)` | Returns `(subgraph, old→new map)`; preserves internal edges |

#### Algorithms (`rx.*`)

| API | Returns | Notes |
|-----|---------|-------|
| `descendants(g, idx)` | `NodeIndices` | All reachable nodes |
| `ancestors(g, idx)` | `NodeIndices` | All nodes that can reach this one |
| `has_path(g, src, tgt)` | `bool` | Reachability check (early termination) |
| `is_directed_acyclic_graph(g)` | `bool` | Cyclicity check |
| `simple_cycles(g)` | `SimpleCycleIter` | All simple cycles (can be exponential) |
| `topological_sort(g)` | `NodeIndices` | Raises `DAGHasCycle` if cyclic |
| `strongly_connected_components(g)` | `list[NodeIndices]` | Tarjan's algorithm, O(V+E) |
| `betweenness_centrality(g)` | `dict[idx→float]` | Already used by `hub_symbols()` |
| `transitive_reduction(g)` | `PyDiGraph` | Remove redundant transitive edges |
| `node_link_json(g, ...)` | `dict` | Standard D3 node-link format |
| `edge_subgraph(g, edges)` | `PyDiGraph` | Subgraph from edge index list |

### Considered and Rejected

| API | Why Not |
|-----|---------|
| `find_successors_by_edge(idx, fn)` | Adds Python callback overhead per edge. `out_edges()` + Python filter is equivalent and doesn't obscure the filtering logic. Also deduplicates (returns unique nodes), losing parallel edge information. |
| `nodes()` | Returns node data only (no indices). We need indices for lookups and edge construction. `node_indices()` is the correct API. |
| `remove_edges_from(pairs)` | Takes `(src, tgt)` node pairs, not edge indices. Removes only ONE edge per pair. Raises on missing. Wrong shape for our `_file_to_edges` (which stores edge indices). |
| `node_link_json()` for `to_json()` | Generic D3 format doesn't match our schema. We need `model_dump(mode="json")` control. |
