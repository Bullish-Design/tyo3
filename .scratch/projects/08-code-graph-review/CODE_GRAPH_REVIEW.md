# TyO3 Code Graph — Deep Architecture Review

**Date:** 2026-06-03
**Scope:** Full codebase review — Rust backend, Python bridge, graph module, models, tests
**Goal:** Identify every issue standing between the current code and the cleanest, most correct, most elegant architecture possible.

---

## 1. Critical Correctness Bugs

### 1.1 `update_file()` corrupts secondary indexes via RustworkX index instability

**Location:** `src/tyo3/graph/graph.py:892-927`

**The problem:** RustworkX's `PyDiGraph.remove_node(idx)` invalidates the relationship between node indices and their stored data. When a node is removed, RustworkX may reuse that index for the next `add_node()` call. All secondary indexes (`_id_to_index`, `_file_to_nodes`, `_file_to_edges`) for *other* files continue to store the old indices, which now point to wrong nodes or to nothing.

**Concrete scenario:**
```
Initial state:
  _id_to_index = {"a.py::foo": 0, "b.py::bar": 1, "a.py::baz": 2}
  _file_to_nodes = {"a.py": [0, 2], "b.py": [1]}

After update_file("a.py"):
  1. remove_node(0), remove_node(2) — removes foo and baz
  2. re-index a.py — add_node() returns new indices (possibly 0, 2 again, or 3, 4)
  3. BUT _file_to_nodes["b.py"] still says [1]
     — if index 1 was compacted or reused, graph[1] is now wrong
```

The `_file_to_edges` problem is even worse: `remove_edge_from_index()` may shift edge indices, invalidating all stored edge indices for every file.

**Impact:** After any `update_file()` call, every subsequent query (`symbols_in_file`, `children`, `parent`, `references_to`, etc.) may return wrong results or raise `IndexError`. This is a data corruption bug.

**Recommended fix — option A (rebuild indexes):**
After any mutation, rebuild all secondary indexes from scratch:
```python
def _rebuild_indexes(self) -> None:
    self._id_to_index.clear()
    self._file_to_nodes.clear()
    self._file_to_edges.clear()
    for idx in self._graph.node_indices():
        node = self._graph[idx]
        self._id_to_index[node.symbol_id] = idx
        self._file_to_nodes[node.file].append(idx)
    for edge_idx in self._graph.edge_indices():
        data = self._graph.get_edge_data_by_index(edge_idx)
        if data and data.file:
            self._file_to_edges[data.file].append(edge_idx)
```
Cost: O(nodes + edges) per update. Acceptable for incremental single-file updates.

**Recommended fix — option B (tombstone pattern):**
Never call `remove_node()`. Instead, mark nodes as "dead" and filter them out in queries. This preserves all indices. Re-compact the graph only on full rebuild.

---

### 1.2 Qualified name mismatch between `document_symbols` and `file_occurrences`

**Location:** `rust/src/convert/occurrences.rs:116`, `rust/src/project.rs:126-148`

**The problem:** The Rust `file_occurrences` API resolves each name token to a definition and returns `definition.name(db)` — which is the **short name** (e.g., `"User"`). Meanwhile, `document_symbols` builds qualified names by walking the symbol hierarchy (e.g., `"models.User"` for a class `User` inside module `models`).

On the Python side, `graph.py:231` constructs the target symbol ID as:
```python
target_sid = f"{target_file}::{target_name}"
# Produces: "/path/to/models.py::User"
```

But the node was created (in `_add_symbol_node`) with:
```python
sid = symbol_id_from_symbol(file_str, symbol)
# Produces: "/path/to/models.py::models.User" (if qualified_name = "models.User")
```

These don't match. The occurrence creates an edge targeting `models.py::User`, but the node's symbol_id is `models.py::models.User`. The edge target doesn't exist in `_id_to_index`, so `_ensure_target_node_simple` is called, which either creates a spurious stub or returns `None` (line 281: "If it's already in project files, skip").

**Impact:** Most reference edges from the batch occurrence API silently fail to connect. The graph has nodes and containment edges but is largely missing its REFERENCES edges — the most important edges for dependency analysis, impact analysis, and coupling metrics.

**Recommended fix (Rust side):**
In `occurrences.rs:resolve_definition`, return the qualified name instead of (or in addition to) the short name. This requires walking the definition's scope chain to build the dotted path, matching what `collect_symbols_recursive` produces for `document_symbols`.

Alternatively, the DTO could include both `target_name` (short) and `target_qualified_name` (dotted), letting Python choose the right one for identity construction.

---

### 1.3 Multi-edge queries only return one edge per node pair

**Location:** `src/tyo3/graph/graph.py:648-662, 664-677, 681-694, 696-708`

**The problem:** The concept document explicitly states: "If `main()` references `User` three times, there are three separate edges from `main` to `User`, each with a different range." RustworkX supports parallel edges (multiple edges between the same node pair). However, the query methods use `get_edge_data(src, tgt)` which returns only **one** edge between a given source and target.

**Affected methods:**
- `references_to()` — undercounts references
- `references_from()` — undercounts references
- `children()` — could miss children if multiple containment edges exist (unlikely but possible)
- `parent()` — not affected (only needs one)
- The BFS in `_resolve_inheritance` (line 514) — `get_edge_data()` only checks one edge

**Impact:** `references_to("models.py::User")` might return 5 edges when the real answer is 50. `coupling_between()` is correct (it iterates all edge indices), but the per-symbol reference queries are wrong. This defeats the coupling-strength-by-edge-count design from the concept doc.

**Recommended fix:**
Use `get_all_edge_data(src, tgt)` which returns a list of all edges between two nodes, or iterate edge indices directly:
```python
def references_to(self, symbol_id: str) -> list[EdgeData]:
    idx = self._id_to_index.get(symbol_id)
    if idx is None:
        return []
    result = []
    for edge_idx in self._graph.incident_edge_index_map(idx, all_edges=False).values():
        # incident_edge_index_map with all_edges=False gives incoming edges
        data = self._graph.get_edge_data_by_index(edge_idx)
        if data.kind == EdgeKind.REFERENCES:
            result.append(data)
    return result
```
Or more simply, use `in_edges(idx)` to get all incoming (source, target, data) triples.

---

## 2. Performance Bottlenecks

### 2.1 `_find_enclosing_symbol` — O(n) per occurrence, O(n*m) per file

**Location:** `src/tyo3/graph/graph.py:534-564`

**The problem:** For every occurrence (name token) in a file, this method linearly scans all node indices belonging to that file to find the innermost symbol whose range contains the occurrence. A file with 500 symbols and 2000 occurrences performs 1,000,000 range-containment checks. Across 200 files, that's 200M checks.

The range-containment check itself (lines 549-552) involves 4 tuple comparisons per candidate:
```python
if (
    (nr.start.line, nr.start.column) <= (range.start.line, range.start.column)
    and (nr.end.line, nr.end.column) >= (range.end.line, range.end.column)
):
```

**Recommended fix:**
Build a sorted list of (start_position, end_position, symbol_id) per file during the node-adding phase. Use binary search to find candidates:

```python
import bisect

class _FileSymbolIndex:
    """Sorted index of symbol ranges for fast enclosing-symbol lookup."""

    def __init__(self) -> None:
        self._entries: list[tuple[tuple[int,int], tuple[int,int], str]] = []
        self._sorted = False

    def add(self, start: tuple[int,int], end: tuple[int,int], sid: str) -> None:
        self._entries.append((start, end, sid))
        self._sorted = False

    def find_enclosing(self, point: tuple[int,int]) -> str | None:
        if not self._sorted:
            self._entries.sort()
            self._sorted = True
        # Binary search for candidates, then pick tightest
        ...
```

This reduces per-occurrence lookup from O(n) to O(log n + k) where k is the number of overlapping ranges (typically 2-3: module, class, method).

### 2.2 `import_cycles` — O(modules × total_nodes) module mapping

**Location:** `src/tyo3/graph/graph.py:776-782`

**The problem:**
```python
for mi in module_indices:
    module_sid = self._graph[mi].symbol_id
    file = file_from_symbol_id(module_sid)
    for ni in self._graph.node_indices():    # <-- scans ALL nodes
        if self._graph[ni].file == file:
            node_to_module[ni] = module_sid
```

This is O(M × N) where M = number of modules and N = total nodes. For a 200-file project with 8000 nodes, that's 1.6M iterations just to build the mapping.

**Fix:** Use `_file_to_nodes` directly:
```python
node_to_module: dict[int, str] = {}
for mi in module_indices:
    module_sid = self._graph[mi].symbol_id
    file = self._graph[mi].file
    for ni in self._file_to_nodes.get(file, []):
        node_to_module[ni] = module_sid
```

This is O(N) total — each node visited exactly once.

### 2.3 `_find_symbol_in_file` — O(total_symbols) fallback scan

**Location:** `src/tyo3/graph/graph.py:145-159`

**The problem:** The fallback path (when exact match fails) iterates over *every key* in `_id_to_index`:
```python
prefix = f"{file_path}::{name}@"
for sid in self._id_to_index:
    if sid.startswith(prefix):
        return sid
```

For a 10,000-symbol graph, this is 10,000 string prefix checks per failed lookup. It's called during `_add_symbol_node` for every symbol that has a `container_name`.

**Fix:** Use `_file_to_nodes` to scope the search to only symbols in the same file:
```python
for idx in self._file_to_nodes.get(file_path, []):
    node = self._graph[idx]
    if node.symbol_id.startswith(prefix):
        return node.symbol_id
```

### 2.4 `coupling_between` — O(all_edges) per call

**Location:** `src/tyo3/graph/graph.py:931-943`

**The problem:** Iterates every edge in the graph to count cross-file references. For a graph with 200,000 edges, this is 200,000 iterations per call. If you're computing a coupling matrix between 50 files, that's 10M iterations.

**Fix:** Use `_file_to_edges` to scope to edges originating from the two files:
```python
def coupling_between(self, file_a: str, file_b: str) -> int:
    nodes_a = set(self._file_to_nodes.get(file_a, []))
    nodes_b = set(self._file_to_nodes.get(file_b, []))
    count = 0
    for edge_idx in self._file_to_edges.get(file_a, []):
        src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
        data = self._graph.get_edge_data_by_index(edge_idx)
        if data.kind == EdgeKind.REFERENCES and tgt in nodes_b:
            count += 1
    for edge_idx in self._file_to_edges.get(file_b, []):
        src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
        data = self._graph.get_edge_data_by_index(edge_idx)
        if data.kind == EdgeKind.REFERENCES and tgt in nodes_a:
            count += 1
    return count
```

### 2.5 `list.pop(0)` in BFS — O(n²) queue operations

**Location:** `src/tyo3/graph/graph.py:510`

**The problem:** `queue.pop(0)` on a Python list is O(n) because it shifts all remaining elements. In a BFS over the inheritance chain, this turns an O(V+E) algorithm into O(V²).

**Fix:** Use `collections.deque`:
```python
from collections import deque
queue: deque[str] = deque([sid])
while queue:
    current_sid = queue.popleft()  # O(1)
```

---

## 3. Architectural Design Issues

### 3.1 Dual enum problem: `OccurrenceRole` vs `ReferenceRole`

**Location:** `src/tyo3/models/navigation.py:27-37` and `src/tyo3/graph/models.py:52-59`

**The problem:** Two nearly identical enums exist for the same concept:

| `OccurrenceRole` (navigation.py) | `ReferenceRole` (graph/models.py) |
|---|---|
| `"Read"` | `"read"` |
| `"Write"` | `"write"` |
| `"Import"` | `"import"` |
| `"Definition"` | `"definition"` |
| `"Other"` | `"other"` |

The only difference is capitalization. The graph code (line 252-258) maintains a manual mapping between them:
```python
role_map = {
    OccurrenceRole.READ: ReferenceRole.READ,
    OccurrenceRole.WRITE: ReferenceRole.WRITE,
    ...
}
```

**Why this is bad:**
- Violates DRY — two enums for one concept
- The case mismatch (`"Read"` vs `"read"`) is a maintenance trap — adding a new role requires updating both enums and the mapping
- The `OccurrenceRole` values come from the Rust side (`ReferenceRoleDto.__str__` returns `"Read"`, `"Write"`, etc.), so the capitalized form is the "native" form. The lowercase form was chosen independently for the graph models.

**Recommended fix:** Either:
(a) Make `OccurrenceRole` the single source of truth and use it in `EdgeData`, eliminating `ReferenceRole` entirely. Change the Rust DTO to return lowercase if needed.
(b) Or make `ReferenceRole` use capitalized values to match `OccurrenceRole`, then make `EdgeData.role` use `OccurrenceRole` directly.

### 3.2 The `_to_python` conversion layer is a hidden performance tax

**Location:** `src/tyo3/rust_project.py:129-159`

**The problem:** Every value crossing the Rust→Python boundary goes through this recursive conversion:

```python
def _to_python(obj):
    # For each PyO3 frozen struct:
    for name in dir(obj):          # Lists ALL attributes including dunder methods
        if name.startswith("_"):
            continue
        val = getattr(obj, name)
        if not callable(val):
            result[name] = _to_python(val)  # Recursive
```

`dir()` is expensive — it walks the MRO, collects all attributes, and sorts them. For a `NameOccurrenceDto` with 4 fields, `dir()` returns ~30+ entries (including `__class__`, `__repr__`, etc.) that are all checked and filtered. For `file_occurrences()` returning 2000 items, that's 60,000+ `dir()` calls and 120,000+ `getattr()` calls just for the top-level objects, plus recursion into nested `RangeDto` and `PositionDto`.

The enum detection (`_is_native_enum`) is also fragile:
```python
if (
    name.endswith("Kind")
    or name.endswith("Type")
    or name.endswith("Modifier")
    or name.endswith("Role")
    or name == "NativeSeverity"
):
```
This heuristic will break if any future DTO class (not an enum) has a name ending in "Kind", "Type", "Modifier", or "Role".

**Recommended fix:** Add a `to_dict()` method to each Rust DTO via `#[pymethods]`:
```rust
#[pymethods]
impl NameOccurrenceDto {
    fn to_dict(&self, py: Python<'_>) -> PyResult<PyObject> {
        let dict = PyDict::new(py);
        dict.set_item("range", self.range.to_dict(py)?)?;
        dict.set_item("target_file", &self.target_file)?;
        dict.set_item("target_name", &self.target_name)?;
        dict.set_item("role", self.role.__str__())?;
        Ok(dict.into())
    }
}
```

Then in Python: `NameOccurrence.model_validate(o.to_dict())`. This eliminates all `dir()`/`getattr()` overhead. Even better, implement it as `__iter__` yielding key-value pairs so Pydantic can validate from a mapping protocol directly.

### 3.3 `export.py` functions accept `Any` instead of typed parameters

**Location:** `src/tyo3/graph/export.py:16, 128`

```python
def to_dot(graph: Any, ...) -> str:
def to_json(graph: Any) -> dict[str, Any]:
```

These should accept `CodeGraph` (or at minimum a `Protocol` that declares `.graph -> rx.PyDiGraph`). Using `Any` defeats static analysis and makes the API contract invisible to callers.

The reason for `Any` is likely to avoid a circular import (export.py importing from graph.py which imports from models.py). This can be solved with:
- A `TYPE_CHECKING` guarded import
- Or by accepting `rx.PyDiGraph` directly and having `CodeGraph` pass `self.graph`

### 3.4 `SymbolKind.CLASS_` and the dynamic alias hack

**Location:** `src/tyo3/models/symbols.py:37-38`

```python
SymbolKind.CLASS = SymbolKind.CLASS_   # type: ignore[attr-defined]
SymbolKind.IMPORT = SymbolKind.IMPORT_  # type: ignore[attr-defined]
```

This works at runtime but:
- Requires `type: ignore` comments
- Breaks IDE autocompletion for `SymbolKind.CLASS`
- Breaks `SymbolKind.__members__` introspection (the alias doesn't appear)
- Forces `graph.py:452` to check both: `if symbol.kind not in (SymbolKind.CLASS, SymbolKind.CLASS_)`

**Better approach:** Use `CLASS` as the member name with a value that avoids the keyword conflict:
```python
class SymbolKind(StrEnum):
    CLASS = "class"      # StrEnum value is "class", member name is CLASS
    IMPORT = "import"    # StrEnum value is "import", member name is IMPORT
```

Python allows `CLASS` and `IMPORT` as identifiers — they're only reserved in lowercase. The trailing underscore is unnecessary.

---

## 4. Rust-Side Issues

### 4.1 `name_ref_from_node` clones entire AST nodes

**Location:** `rust/src/convert/occurrences.rs:77-89`

```rust
fn name_ref_from_node(node: AnyNodeRef<'_>) -> Option<ruff_python_ast::ExprName> {
    if let AnyNodeRef::ExprName(name) = node {
        return Some(name.clone());  // Clones the entire ExprName AST node
    }
    if let AnyNodeRef::Identifier(id) = node {
        return Some(ruff_python_ast::ExprName {
            node_index: ruff_python_ast::AtomicNodeIndex::NONE,
            range: id.range,
            id: id.id.clone(),  // Clones the identifier string
            ctx: ruff_python_ast::ExprContext::Load,
        });
    }
    None
}
```

The function constructs an owned `ExprName` just to pass it to `resolve_definition`, which only needs:
1. The `ExprName` reference for `definition_for_name(model, name, ...)`

The clone is necessary because `definition_for_name` takes `&ExprName`, but the `Identifier` branch constructs a new `ExprName` from scratch — including cloning the `id` string and setting a dummy `node_index`. This synthetic node may not behave identically to a real `ExprName` in all code paths.

**Recommendation:** If `definition_for_name` can accept either an `ExprName` ref or an `Identifier` ref, add an overload. Otherwise, extract just the data needed (name string + range) and use a simpler resolution path for identifiers.

### 4.2 `resolve_definition` returns short name, not qualified name

**Location:** `rust/src/convert/occurrences.rs:105-121`

This is the root cause of critical bug §1.2. The function returns:
```rust
let sym_name = definition.name(db);
(Some(path), sym_name)
```

`definition.name(db)` returns the short symbol name (e.g., `"User"`), not the qualified dotted path (e.g., `"models.User"` or `"User.__init__"`).

To build qualified names, you'd need to walk the definition's scope chain. The `ty_python_semantic` API may provide this through the `Definition` type's bindings. Investigate `definition.qualified_name()` or reconstruct from the scope tree.

### 4.3 `collect_symbols_recursive` qualified name construction

**Location:** `rust/src/project.rs:126-162`

The qualified name is built by prepending the parent name:
```rust
let qualified = match parent_name {
    Some(p) => Some(format!("{}.{}", p, info.name)),
    None => None,
};
```

For top-level symbols, `parent_name` is `None`, so `qualified` is `None`. This means top-level functions get `qualified_name = None` on the Rust side, and the Python side falls back to:
```python
qualified_name=symbol.qualified_name or symbol.name
```

So a top-level function `foo` in `models.py` gets symbol_id `models.py::foo` (from `symbol.name`), which is correct. But if the occurrence API also returns `"foo"` as the short name, they match — the bug from §1.2 only manifests for *nested* symbols (methods, inner classes, nested functions) where the qualified name diverges from the short name.

This means REFERENCES edges for top-level symbols likely work, but REFERENCES to methods like `User.save` or inner classes are broken.

---

## 5. Code Quality & Maintainability

### 5.1 Bare `except Exception` swallows errors silently

**Locations (non-exhaustive):**
- `graph.py:64` — `document_symbols` failure: logs warning, skips entire file
- `graph.py:92` — `check_file` failure: logs warning, skips diagnostics
- `graph.py:178-179` — `find_references` failure: silently returns
- `graph.py:308-310` — `semantic_tokens` failure: logs warning, returns
- `graph.py:336-338` — `goto_definition` failure: silently continues
- `graph.py:457-458` — `type_hierarchy` failure: silently continues
- `graph.py:513-515` — edge data retrieval: silently continues
- `graph.py:656-659` — `get_edge_data` in queries: silently continues

**The problem:** During graph construction, a single file failure silently skips that file. The caller of `CodeGraph.build()` has no way to know that 5 of 200 files failed to index. The graph looks complete but is missing data.

For query methods, catching exceptions on `get_edge_data` means corrupted graph state (e.g., from the §1.1 index bug) is silently masked instead of raising.

**Recommended fix:** Add a structured error collection:
```python
@dataclass
class BuildWarning:
    file: str
    phase: str  # "symbols", "occurrences", "diagnostics", "inheritance"
    error: str

class CodeGraph:
    def __init__(self) -> None:
        ...
        self._build_warnings: list[BuildWarning] = []
```

Log the warning AND collect it. Expose via `graph.build_warnings` so callers can inspect.

For query methods, don't catch exceptions — let them propagate. If the graph state is corrupt, the caller should know immediately rather than getting silently wrong results.

### 5.2 Inline imports inside method bodies

**Locations:**
- `graph.py:313` — `from tyo3.models.advanced import SemanticTokenType, SemanticTokenModifier`
- `export.py:159` — `from tyo3.models.analysis import Range`

These are likely to avoid circular imports, but they add import overhead on every call and make dependencies invisible at the module level. Use `TYPE_CHECKING` guards for type annotations and move runtime imports to the top.

### 5.3 `float("inf")` assigned to `int` variable

**Location:** `graph.py:541`

```python
best_size: int = float("inf")  # type: ignore[assignment]
```

Use `sys.maxsize` instead — it's an actual `int` and doesn't need a type ignore.

### 5.4 Duplicate dict key in `export.py`

**Location:** `export.py:80-93`

```python
colors: dict[str, str] = {
    "module": "#1f77b4",       # line 81
    ...
    "module": "#1f77b4",       # line 93 — duplicate key
}
```

The second `"module"` entry silently overwrites the first. Same value here, but indicates copy-paste error. Remove the duplicate.

---

## 6. Testing Gaps

### 6.1 Integration tests are assertion-light

Many integration tests assert only structural properties (`len > 0`, `isinstance(x, list)`) without checking specific expected values. Examples:

- `TestGraphConstruction.test_build_simple` (line 111): `assert graph.edge_count >= 0` — this assertion is vacuous (edge count is always >= 0)
- `TestGraphQueries.test_transitive_dependencies_returns_set` (line 197): asserts `isinstance(deps, set)` then breaks after one symbol — doesn't verify the deps are correct
- `TestUpdateFile.test_update_repopulates_file` (line 995): `assert len(after_symbols) >= len(before_symbols)` — should verify the specific symbols are back

**Recommendation:** For known fixtures, assert specific symbol names, specific edge counts, specific reference targets. Create a dedicated fixture with a known, controlled structure (e.g., 2 files, 3 classes, 5 functions with known cross-references) and write precise assertions.

### 6.2 No tests for multi-edge (parallel edge) scenarios

The concept document explicitly describes multiple edges between the same symbol pair: "If `main()` references `User` three times, there are three separate edges." No test verifies this, and as documented in §1.3, the query methods are broken for this case.

**Recommendation:** Add a unit test that manually constructs a graph with 3 REFERENCES edges from symbol A to symbol B (different ranges), then verifies `references_to(B)` returns all 3.

### 6.3 No test for the occurrence→reference pipeline end-to-end

There's no integration test that verifies the full path: `file_occurrences()` → occurrence processing → REFERENCES edge creation → correct edge target. Given the §1.2 qualified-name mismatch, this is the most important untested path.

### 6.4 `_resolve_inheritance` has no test for deep hierarchies

The BFS over the INHERITS chain (lines 507-525) isn't tested for multi-level inheritance (A inherits B inherits C, where A.method overrides C.method). The unit tests only test simple single-level cycles and self-imports.

---

## 7. Design Elegance Opportunities

### 7.1 Graph query methods should use a shared edge-filtering primitive

Many query methods (`references_to`, `references_from`, `children`, `parent`) follow the same pattern:
1. Get node index
2. Get predecessors or successors
3. For each neighbor, get edge data and filter by kind
4. Collect results

This should be factored into a shared primitive:
```python
def _edges_of_kind(
    self, node_id: str, kinds: set[EdgeKind], *, incoming: bool = False
) -> list[tuple[int, EdgeData]]:
    """Return all edges of the given kinds, incoming or outgoing."""
    idx = self._id_to_index.get(node_id)
    if idx is None:
        return []
    result = []
    if incoming:
        for edge_idx in self._graph.incident_edge_index_map(idx, all_edges=False).values():
            data = self._graph.get_edge_data_by_index(edge_idx)
            if data.kind in kinds:
                src, _ = self._graph.get_edge_endpoints_by_index(edge_idx)
                result.append((src, data))
    else:
        for edge_idx in self._graph.incident_edge_index_map(idx, all_edges=True).values():
            data = self._graph.get_edge_data_by_index(edge_idx)
            if data.kind in kinds:
                _, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
                result.append((tgt, data))
    return result
```

Then:
```python
def references_to(self, symbol_id: str) -> list[EdgeData]:
    return [data for _, data in self._edges_of_kind(symbol_id, {EdgeKind.REFERENCES}, incoming=True)]

def children(self, symbol_id: str) -> list[SymbolNode]:
    return [self._graph[idx] for idx, _ in self._edges_of_kind(
        symbol_id, {EdgeKind.DEFINES, EdgeKind.CONTAINS}
    )]
```

This eliminates the duplicated pattern, fixes the multi-edge bug (§1.3) in one place, and makes it easy to add new query methods.

### 7.2 Consider separating graph construction from graph querying

The `CodeGraph` class currently handles both construction (500+ lines of `_index_file`, `_resolve_*`, `_ensure_*`, `_infer_*`) and querying (300+ lines of `symbol`, `references_to`, `import_cycles`, etc.). These are different concerns with different stability profiles.

Consider a `CodeGraphBuilder` that handles construction and produces an immutable `CodeGraph`:
```python
class CodeGraphBuilder:
    """Builds a CodeGraph from a TyO3 session."""
    def __init__(self, session: TyO3Session) -> None: ...
    def build(self) -> CodeGraph: ...

class CodeGraph:
    """Immutable queryable code graph. Created by CodeGraphBuilder."""
    def __init__(self, graph, indexes, diagnostics, warnings) -> None: ...
    # Only query methods — no mutation
```

Benefits:
- Clear phase separation (construction vs. query)
- The query object can be truly immutable (no `_add_node` etc.)
- Construction logic can be tested independently
- Makes it obvious that `update_file` is a builder operation, not a query

### 7.3 The `DependencyGraph` save/load format is fragile

**Location:** `src/tyo3/graph/dependency.py:69-120`

The serialization saves raw RustworkX indices:
```python
nodes.append({"idx": idx, "data": node.model_dump(mode="json")})
edges.append({"src": src, "tgt": tgt})
```

On load, edges are connected using these saved indices:
```python
if edge_data["src"] < graph.num_nodes() and edge_data["tgt"] < graph.num_nodes():
    graph.add_edge(edge_data["src"], edge_data["tgt"], None)
```

But `graph.add_node()` returns sequential indices only if nodes are added in order. The `idx` check `< graph.num_nodes()` is a heuristic that happens to work if nodes are added sequentially from 0, but it's not guaranteed by RustworkX.

**Better approach:** Save edges as `(source_symbol_id, target_symbol_id)` pairs and resolve them via `id_to_index` on load:
```python
edges.append({"src_id": graph[src].symbol_id, "tgt_id": graph[tgt].symbol_id})
# On load:
src_idx = id_to_index.get(edge_data["src_id"])
tgt_idx = id_to_index.get(edge_data["tgt_id"])
if src_idx is not None and tgt_idx is not None:
    graph.add_edge(src_idx, tgt_idx, None)
```

---

## 8. Summary: Priority-Ordered Fix List

### P0 — Correctness (data corruption / silent wrong results)

| # | Issue | Location | Fix Complexity |
|---|---|---|---|
| 1.1 | `update_file` index corruption | graph.py:892 | Medium — rebuild indexes or use tombstones |
| 1.2 | Qualified name mismatch (occurrences) | occurrences.rs:116 | Medium — need qualified name from Rust |
| 1.3 | Multi-edge queries return single edge | graph.py:648+ | Easy — use `get_all_edge_data` or edge index iteration |

### P1 — Performance (blocking at scale)

| # | Issue | Location | Fix Complexity |
|---|---|---|---|
| 2.1 | O(n) enclosing symbol scan | graph.py:534 | Medium — build interval index |
| 2.2 | O(M×N) module mapping | graph.py:776 | Easy — use `_file_to_nodes` |
| 2.3 | O(N) symbol prefix scan | graph.py:145 | Easy — scope to file nodes |
| 2.4 | O(E) coupling query | graph.py:931 | Easy — use `_file_to_edges` |
| 2.5 | O(n²) BFS queue | graph.py:510 | Trivial — use `deque` |

### P2 — Architecture (maintainability, clarity)

| # | Issue | Location | Fix Complexity |
|---|---|---|---|
| 3.1 | Dual OccurrenceRole/ReferenceRole | navigation.py, graph/models.py | Easy — unify |
| 3.2 | `_to_python` introspection overhead | rust_project.py:129 | Medium — add Rust `to_dict` |
| 3.3 | `Any` typing in export functions | export.py:16 | Trivial |
| 3.4 | `SymbolKind.CLASS_` alias hack | symbols.py:37 | Easy — rename members |
| 5.1 | Swallowed exceptions | graph.py (many) | Easy — collect warnings |
| 7.1 | Duplicated edge-filter pattern | graph.py queries | Medium — extract primitive |

### P3 — Polish

| # | Issue | Location | Fix Complexity |
|---|---|---|---|
| 5.3 | `float("inf")` type hack | graph.py:541 | Trivial |
| 5.4 | Duplicate dict key | export.py:93 | Trivial |
| 5.2 | Inline imports | graph.py:313, export.py:159 | Trivial |
| 6.1 | Assertion-light tests | test_graph.py | Medium — write precise assertions |
| 6.2 | No multi-edge tests | test_graph.py | Easy — add test |
| 7.3 | DependencyGraph save/load fragility | dependency.py:69 | Easy — use symbol_id keys |
