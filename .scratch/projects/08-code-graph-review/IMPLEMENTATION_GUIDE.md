# TyO3 Code Graph — Refactoring Implementation Guide

**Date:** 2026-06-03
**Based on:** CODE_GRAPH_REVIEW.md (same directory)
**Scope:** Step-by-step implementation plan to address all review findings

---

## Overview

This guide is organized into 7 phases, ordered by dependency and priority. Each phase can be completed and tested independently. Phases 1-3 fix critical correctness bugs. Phases 4-5 address performance. Phases 6-7 handle architecture and polish.

### Phase Map

| Phase | Focus | Review Items | Files Changed |
|-------|-------|-------------|---------------|
| 1 | Unify enums, fix SymbolKind aliases, trivial fixes | 3.1, 3.4, 5.3, 5.4, 5.2 | 7 Python, 1 Rust |
| 2 | Fix qualified name mismatch (Rust + Python) | 1.2, 4.2, 4.3 | 2 Rust, 2 Python |
| 3 | Fix multi-edge queries + shared edge primitive | 1.3, 7.1 | 1 Python |
| 4 | Performance: enclosing symbol index, scoped lookups | 2.1, 2.2, 2.3, 2.4, 2.5 | 1 Python |
| 5 | Fix `update_file` index corruption | 1.1 | 1 Python |
| 6 | Structured build warnings, remove bare excepts | 5.1 | 2 Python |
| 7 | Export typing, DependencyGraph robustness, tests | 3.3, 7.3, 6.1-6.4 | 4 Python |

---

## Phase 1: Enum Unification, SymbolKind, and Trivial Fixes

**Goal:** Clean up the type system foundation before touching construction or query logic. These are safe, mechanical changes that touch many files but have low risk.

### Step 1.1 — Unify `OccurrenceRole` and `ReferenceRole` into a single enum

The Rust DTO `ReferenceRoleDto.__str__()` returns capitalized values (`"Read"`, `"Write"`, etc.). The Python side has two enums with different casing. We'll make the Rust side return lowercase, then eliminate `OccurrenceRole` in favor of `ReferenceRole`.

**File: `rust/src/dto/occurrences.rs`** — Change `__str__` to return lowercase:

```rust
fn __str__(&self) -> &'static str {
    match self {
        ReferenceRoleDto::Read => "read",
        ReferenceRoleDto::Write => "write",
        ReferenceRoleDto::Import => "import",
        ReferenceRoleDto::Definition => "definition",
        ReferenceRoleDto::Other => "other",
    }
}
```

**File: `src/tyo3/models/navigation.py`** — Remove `OccurrenceRole`. Update `NameOccurrence` to use `ReferenceRole` from graph models:

```python
# Remove the OccurrenceRole class entirely.
# Update NameOccurrence:
from tyo3.graph.models import ReferenceRole

class NameOccurrence(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    range: Range
    target_file: str | None = None
    target_name: str | None = None
    role: ReferenceRole  # was OccurrenceRole
```

**Wait** — this creates a circular import: `navigation.py` → `graph.models` → `analysis.py`. The `ReferenceRole` enum should live in a shared location. Move it to `models/analysis.py` or create `models/enums.py`:

Better approach: Move `ReferenceRole` from `graph/models.py` to `models/navigation.py` (where `OccurrenceRole` currently lives), since it's a navigation-domain concept. Then `graph/models.py` imports it from there.

**Concrete steps:**

1. **`rust/src/dto/occurrences.rs`**: Change `ReferenceRoleDto.__str__` to return lowercase strings.

2. **`src/tyo3/models/navigation.py`**:
   - Remove `OccurrenceRole` class.
   - Add `ReferenceRole` (moved from `graph/models.py`, same lowercase values):
     ```python
     class ReferenceRole(StrEnum):
         READ = "read"
         WRITE = "write"
         IMPORT = "import"
         DEFINITION = "definition"
         OTHER = "other"
     ```
   - Change `NameOccurrence.role` type from `OccurrenceRole` to `ReferenceRole`.
   - Update `__all__` to export `ReferenceRole` instead of `OccurrenceRole`.

3. **`src/tyo3/graph/models.py`**: Remove `ReferenceRole` class. Import from `tyo3.models.navigation`:
   ```python
   from tyo3.models.navigation import ReferenceRole
   ```

4. **`src/tyo3/graph/graph.py`**:
   - Remove the `OccurrenceRole` import.
   - Remove the `role_map` dict in `_resolve_references_via_occurrences` (lines 252-258). Since both now use `ReferenceRole` with the same values, just use `occ.role` directly:
     ```python
     role = occ.role  # Already a ReferenceRole
     ```

5. **`src/tyo3/models/__init__.py`**: Replace `OccurrenceRole` with `ReferenceRole` in imports and `__all__`.

6. **`src/tyo3/graph/__init__.py`**: Verify `ReferenceRole` is still exported (it's re-exported from `graph.models` which now re-exports from `navigation`).

7. **`src/tyo3/tests/test_graph.py`**: Update any `OccurrenceRole` references to `ReferenceRole`.

8. **`src/tyo3/tests/test_file_occurrences.py`**: Update `OccurrenceRole` references.

### Step 1.2 — Fix `SymbolKind` aliases

**File: `src/tyo3/models/symbols.py`**:

Replace:
```python
class SymbolKind(StrEnum):
    MODULE = "module"
    CLASS_ = "class_"
    ...
    IMPORT_ = "import_"
    UNKNOWN = "unknown"

SymbolKind.CLASS = SymbolKind.CLASS_   # type: ignore[attr-defined]
SymbolKind.IMPORT = SymbolKind.IMPORT_  # type: ignore[attr-defined]
```

With:
```python
class SymbolKind(StrEnum):
    MODULE = "module"
    CLASS = "class_"       # member name CLASS, value "class_"
    FUNCTION = "function"
    METHOD = "method"
    CONSTRUCTOR = "constructor"
    VARIABLE = "variable"
    CONSTANT = "constant"
    FIELD = "field"
    PARAMETER = "parameter"
    PROPERTY = "property"
    TYPE_PARAMETER = "type_parameter"
    IMPORT = "import_"     # member name IMPORT, value "import_"
    UNKNOWN = "unknown"
```

Remove the two dynamic alias lines and the `# type: ignore` comments.

**Ripple effects** — grep for `SymbolKind.CLASS_` and `SymbolKind.IMPORT_` across the codebase and change to `SymbolKind.CLASS` / `SymbolKind.IMPORT`:
- `graph.py:452` — `SymbolKind.CLASS_` → `SymbolKind.CLASS`
- `graph.py` anywhere else referencing `CLASS_` or `IMPORT_`
- `test_graph.py` — any `SymbolKind.CLASS` references (already correct)
- `models/advanced.py:31` — `SemanticTokenType.CLASS_` if it follows same pattern (check separately)

**Note on Rust side:** The Rust `SymbolKindDto` has a `Class` variant whose `__str__` returns `"class_"`. This matches the StrEnum value `"class_"`, so no Rust change needed. The Rust side is decoupled from the Python member name.

### Step 1.3 — Trivial fixes

**File: `src/tyo3/graph/graph.py`**:

1. Replace `float("inf")` with `sys.maxsize` (line 541):
   ```python
   import sys
   # ...
   best_size: int = sys.maxsize
   ```

2. Move inline import to module level (line 313):
   ```python
   # At top of file, add:
   from tyo3.models.advanced import SemanticTokenType, SemanticTokenModifier
   # Remove the inline import from _resolve_references_via_tokens
   ```

**File: `src/tyo3/graph/export.py`**:

1. Remove duplicate `"module"` key (line 93): Delete the second `"module": "#1f77b4"` entry.

2. Move inline import to module level (line 159):
   ```python
   # At top of file (already imported: from tyo3.graph.models import EdgeData, EdgeKind, SymbolNode)
   # Add:
   from tyo3.models.analysis import Range
   # Remove the inline import from _edge_to_dict
   ```

### Step 1.4 — Test

Run the full test suite after Phase 1. All existing tests should pass (values unchanged, only names/locations shifted).

```bash
pytest src/tyo3/tests/ -x -v
```

---

## Phase 2: Fix Qualified Name Mismatch (Critical Bug)

**Goal:** Make `file_occurrences` return qualified names that match the symbol IDs produced by `document_symbols`, so REFERENCES edges actually connect.

### Step 2.1 — Investigate `ty_python_semantic` APIs for qualified name resolution

Before writing code, we need to understand what APIs are available. The key question: given a `Definition` from `definition_for_name`, can we extract a dotted qualified name?

**Research task (Rust):** In `rust/src/convert/occurrences.rs`, the current code calls `definition.name(db)`. Investigate the `Definition` type from `ty_python_semantic::types::ide_support` for:
- A `qualified_name(db)` method
- Access to the scope chain or containing class
- The binding's `DefinitionKind` to determine nesting

Check ty_python_semantic source: look at `Definition` trait/struct methods, `BindingWithContext`, or `SemanticModel::scope_chain`.

### Step 2.2 — Extend `NameOccurrenceDto` with `target_qualified_name`

**File: `rust/src/dto/occurrences.rs`** — Add a field:

```rust
pub struct NameOccurrenceDto {
    #[pyo3(get)]
    pub range: RangeDto,
    #[pyo3(get)]
    pub target_file: Option<String>,
    #[pyo3(get)]
    pub target_name: Option<String>,
    #[pyo3(get)]
    pub target_qualified_name: Option<String>,  // NEW
    #[pyo3(get)]
    pub role: ReferenceRoleDto,
}
```

### Step 2.3 — Build qualified name in `resolve_definition`

**File: `rust/src/convert/occurrences.rs`** — Extend `resolve_definition` to return `(Option<String>, Option<String>, Option<String>)` — (file, short_name, qualified_name):

The approach depends on what §2.1 reveals. Two strategies:

**Strategy A — If `Definition` has scope chain access:**
```rust
fn resolve_definition(
    db: &dyn Db,
    model: &SemanticModel<'_>,
    name: &ruff_python_ast::ExprName,
) -> (Option<String>, Option<String>, Option<String>) {
    let def = definition_for_name(model, name, ImportAliasResolution::ResolveAliases);
    match def {
        Some(definition) => {
            let file = definition.file(db);
            let path = file.path(db).as_str().to_string();
            let short_name = definition.name(db);
            let qualified = build_qualified_name(db, &definition);
            (Some(path), short_name, qualified)
        }
        None => (None, None, None),
    }
}
```

**Strategy B — If scope chain is unavailable, match against document_symbols:**
Walk the file's symbol tree (same as `document_symbols`) and find the symbol whose `name_range` contains the definition's range. This is more expensive but guaranteed to produce the same qualified name.

**Strategy C — Simplest fallback:**
On the Python side, instead of constructing `target_sid = f"{target_file}::{target_name}"` directly, use `_find_symbol_in_file(target_file, target_name)` to look up the actual node. This avoids the name mismatch entirely by searching by short name within the file's known nodes.

### Step 2.4 — Update Python model

**File: `src/tyo3/models/navigation.py`** — Add `target_qualified_name` to `NameOccurrence`:

```python
class NameOccurrence(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    range: Range
    target_file: str | None = None
    target_name: str | None = None
    target_qualified_name: str | None = None  # NEW
    role: ReferenceRole
```

### Step 2.5 — Update graph construction to use qualified name

**File: `src/tyo3/graph/graph.py`** — In `_resolve_references_via_occurrences`, change the target_sid construction:

```python
for occ in occurrences:
    if occ.target_file is None or occ.target_name is None:
        continue

    target_file = occ.target_file

    # Prefer qualified name for symbol ID construction
    if occ.target_qualified_name:
        target_sid = f"{target_file}::{occ.target_qualified_name}"
    else:
        target_sid = f"{target_file}::{occ.target_name}"

    # If still no match, try flexible lookup by short name
    if target_sid not in self._id_to_index:
        found = self._find_symbol_in_file(target_file, occ.target_name)
        if found:
            target_sid = found

    # ... rest of the method
```

### Step 2.6 — Test

Create a fixture with nested symbols (a class with methods) and verify that REFERENCES edges connect to the correct method nodes, not just top-level symbols.

```python
# fixtures/graph_test/nested.py (or extend existing fixture)
class Outer:
    def inner_method(self):
        pass

def caller():
    o = Outer()
    o.inner_method()  # This reference should resolve to Outer.inner_method
```

Write an integration test:
```python
def test_references_to_nested_symbols(self):
    graph = CodeGraph.build(session)
    # Find the inner_method node
    method_nodes = [n for n in graph.symbols_of_kind(SymbolKind.METHOD)
                    if n.name == "inner_method"]
    assert len(method_nodes) == 1
    refs = graph.references_to(method_nodes[0].symbol_id)
    assert len(refs) > 0  # The call in caller() should create a reference
```

---

## Phase 3: Fix Multi-Edge Queries + Shared Edge Primitive

**Goal:** All query methods correctly handle parallel edges (multiple edges between the same node pair). Extract a shared edge-filtering primitive to eliminate duplication.

### Step 3.1 — Add the `_edges_of_kind` primitive

**File: `src/tyo3/graph/graph.py`** — Add this method to `CodeGraph`, in the "Internal graph mutation" section (after `_add_stub_node`, before the Properties section):

```python
def _edges_of_kind(
    self,
    symbol_id: str,
    kinds: set[EdgeKind],
    *,
    incoming: bool = False,
) -> list[tuple[int, EdgeData]]:
    """Return all edges matching *kinds* for a node.

    When *incoming* is False (default), returns outgoing edges as
    ``(target_index, edge_data)`` pairs.
    When *incoming* is True, returns incoming edges as
    ``(source_index, edge_data)`` pairs.

    Correctly handles parallel edges (multiple edges between the
    same node pair).
    """
    idx = self._id_to_index.get(symbol_id)
    if idx is None:
        return []
    result: list[tuple[int, EdgeData]] = []
    for edge_idx in self._graph.edge_indices():
        src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
        if incoming and tgt == idx:
            data = self._graph.get_edge_data_by_index(edge_idx)
            if data.kind in kinds:
                result.append((src, data))
        elif not incoming and src == idx:
            data = self._graph.get_edge_data_by_index(edge_idx)
            if data.kind in kinds:
                result.append((tgt, data))
    return result
```

**Performance note:** This iterates all edges, which is O(E). For Phase 4, we can optimize using `_file_to_edges` or RustworkX's incident edge APIs. For now, correctness first.

**Alternative (better performance):** Use RustworkX's `in_edges` / `out_edges` if available, or iterate only the node's incident edges:

```python
def _edges_of_kind(
    self,
    symbol_id: str,
    kinds: set[EdgeKind],
    *,
    incoming: bool = False,
) -> list[tuple[int, EdgeData]]:
    idx = self._id_to_index.get(symbol_id)
    if idx is None:
        return []
    result: list[tuple[int, EdgeData]] = []
    if incoming:
        # Iterate all edges ending at this node
        for pred_idx in self._graph.predecessor_indices(idx):
            for data in self._graph.get_all_edge_data(pred_idx, idx):
                if data.kind in kinds:
                    result.append((pred_idx, data))
    else:
        # Iterate all edges starting from this node
        for succ_idx in self._graph.neighbors(idx):
            for data in self._graph.get_all_edge_data(idx, succ_idx):
                if data.kind in kinds:
                    result.append((succ_idx, data))
    return result
```

This uses `get_all_edge_data(src, tgt)` instead of `get_edge_data(src, tgt)`, which is the key fix for parallel edges.

### Step 3.2 — Rewrite query methods using the primitive

**`references_to`:**
```python
def references_to(self, symbol_id: str) -> list[EdgeData]:
    return [data for _, data in self._edges_of_kind(
        symbol_id, {EdgeKind.REFERENCES}, incoming=True
    )]
```

**`references_from`:**
```python
def references_from(self, symbol_id: str) -> list[tuple[SymbolNode, EdgeData]]:
    return [
        (self._graph[tgt_idx], data)
        for tgt_idx, data in self._edges_of_kind(
            symbol_id, {EdgeKind.REFERENCES}
        )
    ]
```

**`children`:**
```python
def children(self, symbol_id: str) -> list[SymbolNode]:
    seen: set[int] = set()
    result: list[SymbolNode] = []
    for tgt_idx, _ in self._edges_of_kind(
        symbol_id, {EdgeKind.DEFINES, EdgeKind.CONTAINS}
    ):
        if tgt_idx not in seen:
            seen.add(tgt_idx)
            result.append(self._graph[tgt_idx])
    return result
```

**`parent`:**
```python
def parent(self, symbol_id: str) -> SymbolNode | None:
    edges = self._edges_of_kind(
        symbol_id, {EdgeKind.DEFINES, EdgeKind.CONTAINS}, incoming=True
    )
    if edges:
        return self._graph[edges[0][0]]
    return None
```

### Step 3.3 — Fix `_resolve_inheritance` BFS edge check

In the BFS loop (line 512-518), replace:
```python
edge_data = self._graph.get_edge_data(self._id_to_index[current_sid], succ_idx)
```
With:
```python
for edge_data in self._graph.get_all_edge_data(self._id_to_index[current_sid], succ_idx):
    if edge_data.kind == EdgeKind.INHERITS:
        # process parent
        break
```

### Step 3.4 — Remove bare `try/except` from query methods

Now that `_edges_of_kind` handles edge access cleanly, the `try/except` wrappers in `references_to`, `references_from`, `children`, `parent` are unnecessary. Remove them.

### Step 3.5 — Test parallel edges

Add to `test_graph.py`:

```python
class TestParallelEdges:
    """Verify that multiple edges between the same node pair are handled."""

    def test_references_to_returns_all_parallel_edges(self) -> None:
        graph = CodeGraph()
        a = SymbolNode(
            symbol_id="a.py::caller",
            name="caller", qualified_name="caller",
            kind=SymbolKind.FUNCTION, file="a.py",
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1}, "end": {"line": 10, "column": 1}}
            ),
        )
        b = SymbolNode(
            symbol_id="b.py::target",
            name="target", qualified_name="target",
            kind=SymbolKind.FUNCTION, file="b.py",
            range=Range.model_validate(
                {"start": {"line": 1, "column": 1}, "end": {"line": 5, "column": 1}}
            ),
        )
        graph._add_node(a)
        graph._add_node(b)

        # Three references from caller to target at different locations
        for line in [3, 5, 7]:
            edge = EdgeData(
                kind=EdgeKind.REFERENCES,
                file="a.py",
                range=Range.model_validate(
                    {"start": {"line": line, "column": 5},
                     "end": {"line": line, "column": 11}}
                ),
                role=ReferenceRole.READ,
            )
            graph._add_edge("a.py::caller", "b.py::target", edge, "a.py")

        refs = graph.references_to("b.py::target")
        assert len(refs) == 3, f"Expected 3 references, got {len(refs)}"

        from_refs = graph.references_from("a.py::caller")
        assert len(from_refs) == 3
```

---

## Phase 4: Performance Optimizations

**Goal:** Eliminate the O(n²) and O(n×m) hotspots identified in the review.

### Step 4.1 — Enclosing symbol index (`_FileSymbolIndex`)

**File: `src/tyo3/graph/graph.py`** — Add a new class and integrate it:

```python
import bisect

class _FileSymbolIndex:
    """Sorted index of symbol ranges for O(log n) enclosing-symbol lookup.

    Symbols are stored as (start_line, start_col, end_line, end_col, symbol_id)
    sorted by start position. To find the enclosing symbol for a point, we
    binary-search for the rightmost start <= point, then scan backwards
    through candidates to find the tightest containing range.
    """

    __slots__ = ("_entries", "_sorted")

    def __init__(self) -> None:
        self._entries: list[tuple[int, int, int, int, str]] = []
        self._sorted = False

    def add(self, node: SymbolNode) -> None:
        if node.kind == SymbolKind.MODULE:
            return  # Module nodes are the fallback, not indexed
        r = node.range
        self._entries.append((
            r.start.line, r.start.column,
            r.end.line, r.end.column,
            node.symbol_id,
        ))
        self._sorted = False

    def find_enclosing(self, line: int, col: int) -> str | None:
        if not self._entries:
            return None
        if not self._sorted:
            self._entries.sort()
            self._sorted = True

        # Find rightmost entry whose start <= (line, col)
        # Entries are sorted by (start_line, start_col, ...)
        pos = bisect.bisect_right(self._entries, (line, col + 1)) - 1

        best_id: str | None = None
        best_size = sys.maxsize

        # Scan backwards from pos — all candidates must start <= point
        for i in range(pos, -1, -1):
            sl, sc, el, ec, sid = self._entries[i]
            # If start is already past our point, stop
            if (sl, sc) > (line, col):
                continue
            # Check containment: end must be >= point
            if (el, ec) >= (line, col):
                size = (el - sl) * 10000 + (ec - sc)
                if size < best_size:
                    best_size = size
                    best_id = sid
            # Optimization: if this entry's end_line is well before
            # our line, earlier entries can't contain us either
            if el < line:
                break

        return best_id
```

**Integrate into `CodeGraph.__init__`:**
```python
self._file_symbol_indexes: dict[str, _FileSymbolIndex] = defaultdict(_FileSymbolIndex)
```

**Update `_add_node`** to also register with the index:
```python
def _add_node(self, node: SymbolNode) -> int:
    if node.symbol_id in self._id_to_index:
        return self._id_to_index[node.symbol_id]
    idx = self._graph.add_node(node)
    self._id_to_index[node.symbol_id] = idx
    self._file_to_nodes[node.file].append(idx)
    self._file_symbol_indexes[node.file].add(node)
    return idx
```

**Rewrite `_find_enclosing_symbol`:**
```python
def _find_enclosing_symbol(self, file_str: str, range: Range) -> str | None:
    index = self._file_symbol_indexes.get(file_str)
    if index is not None:
        result = index.find_enclosing(range.start.line, range.start.column)
        if result is not None:
            return result
    # Fallback to module node
    module_id = f"{file_str}::<module>"
    if module_id in self._id_to_index:
        return module_id
    return None
```

### Step 4.2 — Scope `_find_symbol_in_file` to file nodes

**File: `src/tyo3/graph/graph.py`** — Replace the fallback scan:

```python
def _find_symbol_in_file(self, file_path: str, name: str) -> str | None:
    exact = f"{file_path}::{name}"
    if exact in self._id_to_index:
        return exact
    # Scoped scan: only check nodes in this file
    prefix = f"{file_path}::{name}@"
    for idx in self._file_to_nodes.get(file_path, []):
        sid = self._graph[idx].symbol_id
        if sid.startswith(prefix):
            return sid
    return None
```

### Step 4.3 — Fix `import_cycles` module mapping to use `_file_to_nodes`

Replace lines 776-782:

```python
node_to_module: dict[int, str] = {}
for mi in module_indices:
    node: SymbolNode = self._graph[mi]
    module_sid = node.symbol_id
    file = node.file
    for ni in self._file_to_nodes.get(file, []):
        node_to_module[ni] = module_sid
```

### Step 4.4 — Fix `coupling_between` to use `_file_to_edges`

Replace the method body:

```python
def coupling_between(self, file_a: str, file_b: str) -> int:
    nodes_a = set(self._file_to_nodes.get(file_a, []))
    nodes_b = set(self._file_to_nodes.get(file_b, []))
    count = 0
    # Edges originating from file_a targeting file_b
    for edge_idx in self._file_to_edges.get(file_a, []):
        src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
        data = self._graph.get_edge_data_by_index(edge_idx)
        if data.kind == EdgeKind.REFERENCES and tgt in nodes_b:
            count += 1
    # Edges originating from file_b targeting file_a
    for edge_idx in self._file_to_edges.get(file_b, []):
        src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
        data = self._graph.get_edge_data_by_index(edge_idx)
        if data.kind == EdgeKind.REFERENCES and tgt in nodes_a:
            count += 1
    return count
```

### Step 4.5 — Replace `list.pop(0)` with `deque.popleft()`

In `_resolve_inheritance` (around line 509):

```python
from collections import deque

# Replace:
queue: list[str] = [sid]
while queue:
    current_sid = queue.pop(0)

# With:
queue: deque[str] = deque([sid])
while queue:
    current_sid = queue.popleft()
```

Add `deque` to the imports at the top of `graph.py`.

### Step 4.6 — Test performance

No functional changes — all existing tests should still pass. Optionally add a benchmark:

```python
@needs_native
def test_build_performance_graph_test(self):
    """Ensure graph build completes in reasonable time."""
    import time
    from tyo3.session import TyO3Session

    with TyO3Session(fixture_path("graph_test")) as session:
        start = time.monotonic()
        graph = CodeGraph.build(session)
        elapsed = time.monotonic() - start
        assert elapsed < 30.0, f"Graph build took {elapsed:.1f}s"
```

---

## Phase 5: Fix `update_file` Index Corruption

**Goal:** Make incremental updates correct by rebuilding all secondary indexes after mutation.

### Step 5.1 — Add `_rebuild_indexes` method

**File: `src/tyo3/graph/graph.py`** — Add to the "Internal graph mutation" section:

```python
def _rebuild_indexes(self) -> None:
    """Rebuild all secondary indexes from the graph's current state.

    Called after any mutation that adds or removes nodes/edges
    (e.g., ``update_file``).  O(nodes + edges).
    """
    self._id_to_index.clear()
    self._file_to_nodes.clear()
    self._file_to_edges.clear()
    self._file_symbol_indexes.clear()

    for idx in self._graph.node_indices():
        node: SymbolNode = self._graph[idx]
        self._id_to_index[node.symbol_id] = idx
        self._file_to_nodes[node.file].append(idx)
        self._file_symbol_indexes[node.file].add(node)

    for edge_idx in self._graph.edge_indices():
        data: EdgeData = self._graph.get_edge_data_by_index(edge_idx)
        if data.file:
            self._file_to_edges[data.file].append(edge_idx)
```

### Step 5.2 — Rewrite `update_file` to use `_rebuild_indexes`

```python
def update_file(self, session: TyO3Session, path: str) -> None:
    """Re-index a single file and update the graph in-place."""
    # 1. Collect node indices for this file
    old_indices = list(self._file_to_nodes.get(path, []))

    # 2. Remove nodes (removes incident edges automatically in RustworkX)
    for idx in sorted(old_indices, reverse=True):
        self._graph.remove_node(idx)

    # 3. Remove diagnostics
    self._diagnostics.pop(path, None)

    # 4. Rebuild indexes from scratch (handles index shifts)
    self._rebuild_indexes()

    # 5. Re-index the file
    self._index_file(session, path)

    # 6. Rebuild again after re-indexing
    self._rebuild_indexes()
```

**Note:** We call `_rebuild_indexes` twice — once after removal (so `_index_file` sees correct indexes for existing nodes from other files) and once after re-indexing (to incorporate the new nodes). This is O(2 × (N + E)) which is acceptable for single-file updates.

**Alternative (slightly more efficient):** Only rebuild once, after the full update cycle. But `_index_file` itself calls `_find_symbol_in_file`, `_find_enclosing_symbol`, etc. which rely on indexes being correct. So we need correct indexes before re-indexing.

### Step 5.3 — Test

The existing `TestUpdateFile` integration tests should now be more reliable. Add a specific regression test:

```python
def test_update_does_not_corrupt_other_file_lookups(self) -> None:
    """After updating file A, symbols in file B are still queryable."""
    from tyo3.session import TyO3Session

    with TyO3Session(fixture_path("graph_test")) as session:
        graph = CodeGraph.build(session)
        files = [str(f) for f in session.files()]
        assert len(files) >= 2

        # Record all symbols from file[1]
        symbols_before = {
            n.symbol_id for n in graph.symbols_in_file(files[1])
        }

        # Update file[0]
        graph.update_file(session, files[0])

        # Verify file[1] symbols are still correctly accessible
        for sid in symbols_before:
            node = graph.symbol(sid)
            assert node is not None, f"Lost symbol {sid} after updating {files[0]}"
```

---

## Phase 6: Structured Build Warnings

**Goal:** Replace bare `except Exception` with structured error collection during construction. Remove bare excepts from query methods entirely.

### Step 6.1 — Define `BuildWarning` dataclass

**File: `src/tyo3/graph/models.py`** — Add:

```python
@dataclass(frozen=True, slots=True)
class BuildWarning:
    """A non-fatal error encountered during graph construction."""
    file: str
    phase: str
    error: str
```

### Step 6.2 — Add warning collection to `CodeGraph`

**File: `src/tyo3/graph/graph.py`**:

In `__init__`:
```python
from tyo3.graph.models import BuildWarning
# ...
self._build_warnings: list[BuildWarning] = []
```

Add property:
```python
@property
def build_warnings(self) -> list[BuildWarning]:
    """Non-fatal errors encountered during graph construction."""
    return list(self._build_warnings)
```

### Step 6.3 — Replace bare excepts in construction methods

For each `except Exception` in construction code, log the warning AND collect it:

**`_index_file` — document_symbols failure (line 62-66):**
```python
try:
    symbols = session.document_symbols(file_str)
except Exception as e:
    self._build_warnings.append(
        BuildWarning(file=file_str, phase="symbols", error=str(e))
    )
    logger.warning("Failed to get symbols for %s: %s", file_str, e)
    return
```

**`_index_file` — check_file failure (line 91-95):**
```python
try:
    result = session.check_file(file_str)
    self._diagnostics[file_str] = result.diagnostics
except Exception as e:
    self._build_warnings.append(
        BuildWarning(file=file_str, phase="diagnostics", error=str(e))
    )
    logger.warning("Failed to check %s: %s", file_str, e)
```

Apply the same pattern to:
- `_resolve_references_via_occurrences` — `file_occurrences` failure
- `_resolve_references_via_tokens` — `semantic_tokens` failure
- `_resolve_references_via_tokens` — `goto_definition` failure (inner loop)
- `_resolve_inheritance` — `type_hierarchy` failure

### Step 6.4 — Remove bare excepts from query methods

In `references_to`, `references_from`, `children`, `parent` — these now use `_edges_of_kind` which doesn't need try/except. Any `IndexError` from corrupt state should propagate immediately, not be swallowed.

### Step 6.5 — Export `BuildWarning`

**File: `src/tyo3/graph/__init__.py`** — Add `BuildWarning` to exports:
```python
from tyo3.graph.models import BuildWarning, EdgeData, EdgeKind, ReferenceRole, SymbolNode
```

### Step 6.6 — Test

```python
def test_build_warnings_accessible(self) -> None:
    graph = CodeGraph()
    assert graph.build_warnings == []

@needs_native
def test_build_collects_warnings(self) -> None:
    from tyo3.session import TyO3Session
    with TyO3Session(fixture_path("graph_test")) as session:
        graph = CodeGraph.build(session)
        # Warnings are a list (possibly empty if all files succeeded)
        assert isinstance(graph.build_warnings, list)
```

---

## Phase 7: Export Typing, DependencyGraph Robustness, and Test Improvements

### Step 7.1 — Fix `export.py` typing

**File: `src/tyo3/graph/export.py`**:

Replace `Any` with proper types using `TYPE_CHECKING`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from tyo3.graph.graph import CodeGraph

from tyo3.graph.models import EdgeData, EdgeKind, SymbolNode


def to_dot(graph: CodeGraph, *, max_nodes: int | None = None) -> str:
    ...

def to_json(graph: CodeGraph) -> dict[str, Any]:
    ...
```

The `from __future__ import annotations` (already present) ensures that `CodeGraph` is evaluated lazily, so the `TYPE_CHECKING` import doesn't cause circular import at runtime.

### Step 7.2 — Fix `DependencyGraph` save/load to use symbol_id keys

**File: `src/tyo3/graph/dependency.py`**:

**In `save()`** — Store edge endpoints as symbol IDs:
```python
edges = []
for edge_idx in self.graph.edge_indices():
    src, tgt = self.graph.get_edge_endpoints_by_index(edge_idx)
    edges.append({
        "src_id": self.graph[src].symbol_id,
        "tgt_id": self.graph[tgt].symbol_id,
    })
```

**In `load()`** — Resolve edges via `id_to_index`:
```python
for edge_data in data["edges"]:
    src_idx = id_to_index.get(edge_data.get("src_id"))
    tgt_idx = id_to_index.get(edge_data.get("tgt_id"))
    if src_idx is not None and tgt_idx is not None:
        graph.add_edge(src_idx, tgt_idx, None)
    # Backwards compatibility: fall back to raw indices
    elif "src" in edge_data and "tgt" in edge_data:
        src = edge_data["src"]
        tgt = edge_data["tgt"]
        if src < graph.num_nodes() and tgt < graph.num_nodes():
            graph.add_edge(src, tgt, None)
```

### Step 7.3 — Strengthen integration tests

Create a dedicated precise-assertion fixture:

**File: `fixtures/graph_precise/models.py`**:
```python
class Base:
    def save(self):
        pass

class User(Base):
    def save(self):
        pass

    def greet(self, name: str) -> str:
        return f"Hello, {name}"

MAX_USERS = 100
```

**File: `fixtures/graph_precise/app.py`**:
```python
from models import User, MAX_USERS

def main():
    u = User()
    u.save()
    u.greet("world")
    limit = MAX_USERS
```

Add precise tests:

```python
@needs_native
class TestPreciseGraphStructure:
    """Tests with exact assertions against a known fixture."""

    @pytest.fixture
    def graph(self):
        from tyo3.session import TyO3Session
        with TyO3Session(fixture_path("graph_precise")) as session:
            yield CodeGraph.build(session)

    def test_module_nodes_exist(self, graph):
        modules = graph.symbols_of_kind(SymbolKind.MODULE)
        module_names = {m.name for m in modules}
        assert "models" in module_names
        assert "app" in module_names

    def test_class_hierarchy(self, graph):
        classes = [c for c in graph.symbols_of_kind(SymbolKind.CLASS) if not c.external]
        class_names = {c.name for c in classes}
        assert "Base" in class_names
        assert "User" in class_names

    def test_containment(self, graph):
        # User class should contain save and greet methods
        user_nodes = [c for c in graph.symbols_of_kind(SymbolKind.CLASS)
                      if c.name == "User" and not c.external]
        assert len(user_nodes) == 1
        kids = graph.children(user_nodes[0].symbol_id)
        kid_names = {k.name for k in kids}
        assert "save" in kid_names
        assert "greet" in kid_names

    def test_edge_count_positive(self, graph):
        assert graph.edge_count > 0, "Graph should have edges"

    def test_references_exist(self, graph):
        # app.py::main should reference User
        user_nodes = [c for c in graph.symbols_of_kind(SymbolKind.CLASS)
                      if c.name == "User" and not c.external]
        if user_nodes:
            refs = graph.references_to(user_nodes[0].symbol_id)
            assert len(refs) > 0, "User should have at least one reference from app.py"
```

### Step 7.4 — Add multi-edge test (covered in Phase 3, Step 3.5)

### Step 7.5 — Add deep inheritance test

```python
class TestDeepInheritance:
    def test_overrides_through_chain(self) -> None:
        """A.method overriding C.method through B should be detected."""
        graph = CodeGraph()

        # Create three classes: C -> B -> A (A inherits B inherits C)
        for name, file in [("C", "c.py"), ("B", "b.py"), ("A", "a.py")]:
            mod_sid = f"{file}::<module>"
            graph._add_node(SymbolNode(
                symbol_id=mod_sid, name=file[0], qualified_name="<module>",
                kind=SymbolKind.MODULE, file=file,
                range=Range.model_validate(
                    {"start": {"line": 1, "column": 1}, "end": {"line": 20, "column": 1}}
                ),
            ))
            cls_sid = f"{file}::{name}"
            graph._add_node(SymbolNode(
                symbol_id=cls_sid, name=name, qualified_name=name,
                kind=SymbolKind.CLASS, file=file,
                range=Range.model_validate(
                    {"start": {"line": 1, "column": 1}, "end": {"line": 10, "column": 1}}
                ),
            ))
            method_sid = f"{file}::{name}.do_thing"
            graph._add_node(SymbolNode(
                symbol_id=method_sid, name="do_thing",
                qualified_name=f"{name}.do_thing",
                kind=SymbolKind.METHOD, file=file,
                range=Range.model_validate(
                    {"start": {"line": 3, "column": 5}, "end": {"line": 5, "column": 1}}
                ),
            ))
            # CONTAINS edge: class -> method
            graph._add_edge(cls_sid, method_sid,
                           EdgeData(kind=EdgeKind.CONTAINS), file)

        # INHERITS: A -> B, B -> C
        graph._add_edge("a.py::A", "b.py::B",
                        EdgeData(kind=EdgeKind.INHERITS), "a.py")
        graph._add_edge("b.py::B", "c.py::C",
                        EdgeData(kind=EdgeKind.INHERITS), "b.py")

        # Verify transitive dependencies
        deps = graph.transitive_dependencies("a.py::A")
        assert "b.py::B" in deps
        assert "c.py::C" in deps
```

---

## Verification Checklist

After all phases are complete, verify:

- [ ] `pytest src/tyo3/tests/ -x -v` — all tests pass
- [ ] `maturin develop` compiles without warnings
- [ ] `ReferenceRole` is the single role enum (no `OccurrenceRole`)
- [ ] `SymbolKind.CLASS` works without `type: ignore`
- [ ] `references_to()` returns all parallel edges (multi-edge test)
- [ ] `update_file()` does not corrupt indexes (regression test)
- [ ] `graph.build_warnings` is accessible after construction
- [ ] `to_dot()` and `to_json()` accept typed `CodeGraph` parameter
- [ ] `DependencyGraph` save/load uses symbol_id keys
- [ ] No bare `except Exception` in query methods
- [ ] No `float("inf")`, no duplicate dict keys, no inline imports
- [ ] Integration tests assert specific symbol names and edge targets
