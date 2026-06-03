# TyO3 Deep Code Review

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture Analysis](#2-architecture-analysis)
3. [Rust Layer Review](#3-rust-layer-review)
4. [Python Bridge Review](#4-python-bridge-review)
5. [CodeGraph Review](#5-codegraph-review)
6. [Domain Model Review](#6-domain-model-review)
7. [Test Suite Review](#7-test-suite-review)
8. [RustworkX Optimization Opportunities](#8-rustworkx-optimization-opportunities)
9. [Prioritized Recommendations](#9-prioritized-recommendations)

---

## 1. Project Overview

### What TyO3 Is

TyO3 is a Python semantic analysis engine that wraps [ty](https://github.com/astral-sh/ty) (the Rust-based type checker from Astral, makers of Ruff) via PyO3 bindings, exposing a Pydantic-validated Python API for type checking, symbol discovery, code navigation, and semantic graph construction. It is essentially an LSP-like semantic engine packaged as a Python library.

### Technology Stack

- **Python 3.13** with Pydantic v2.12.5+
- **Rust** (via PyO3 0.23) with ty/Ruff semantic engine crates
- **maturin** for building the Python extension from Rust
- **rustworkx** 0.16+ for directed graph construction and algorithms
- **devenv** (Nix) for reproducible development environment

### Core Capabilities

| Capability | API Method | Backend |
|---|---|---|
| Type checking | `session.check()`, `check_file()` | ty's Salsa-cached checker |
| Symbol discovery | `document_symbols()`, `workspace_symbols()` | ty_ide |
| Goto definition/declaration/type | `goto_definition()`, etc. | ty_ide navigation |
| Find references | `find_references()` | ty_ide |
| Hover information | `hover()` | ty_ide, rendered as Markdown |
| Semantic tokens | `semantic_tokens()` | ty_ide |
| Batch name resolution | `file_occurrences()` | Custom Rust (Phase 5) |
| Type hierarchy | `type_hierarchy()` | ty_ide |
| Code graph construction | `CodeGraph.build(session)` | Python + rustworkx |

### File Inventory

| Layer | Directory | File Count | Purpose |
|---|---|---|---|
| Rust core | `rust/src/` | 21 files | PyO3 bindings, DTOs, conversions |
| Python API | `src/tyo3/` | 18 modules | Session, models, graph, demo CLI |
| Tests | `src/tyo3/tests/` | 20 files | Unit, integration, property, perf |
| Fixtures | `fixtures/` | ~15 files | Test projects for various scenarios |

---

## 2. Architecture Analysis

### Three-Layer Architecture

```
                        ┌─────────────────────────────┐
                        │     Python Public API        │
                        │  TyO3Session  /  CodeGraph   │
                        └──────────┬──────────────────┘
                                   │
                        ┌──────────▼──────────────────┐
                        │     Python Bridge            │
                        │  RustProject + _to_python()  │
                        │  Pydantic model_validate()   │
                        └──────────┬──────────────────┘
                                   │  PyO3 FFI
                        ┌──────────▼──────────────────┐
                        │     Rust Extension           │
                        │  PyTyProject + DTOs          │
                        │  ty_ide / ty_project / Salsa │
                        └─────────────────────────────┘
```

**Data flow for every API call:**
1. Python calls `TyO3Session.method()`
2. Session delegates to `RustProject.method()`
3. RustProject calls `self._inner.method()` (PyO3 FFI)
4. Rust resolves files, computes offsets, calls ty_ide
5. Rust converts ty types -> DTO structs, returns to Python
6. `_to_python()` converts PyO3 objects -> plain Python dicts via `dir()` introspection
7. `Pydantic.model_validate()` produces validated domain models
8. Domain models returned to caller

### Strengths of the Architecture

**Clean separation of concerns.** Each layer has a single responsibility: Rust handles performance-critical work and ty integration, the bridge handles type conversion and error mapping, and the Python API provides a clean user-facing interface.

**Defense in depth for errors.** Exceptions are validated at multiple layers: Python-side `_validate_position()` catches obvious mistakes early, Rust-side coordinate validation catches bounds errors, and the bridge maps every Rust exception to a typed Python exception.

**Salsa-cached type checking.** The Rust layer benefits from ty's incremental computation framework (Salsa). Repeated `check()` calls on unchanged code are essentially free after the first invocation.

**Idempotent resource management.** The `Mutex<Option<TyProjectState>>` pattern in Rust combined with the Python `_closed` flag and `__del__` finalizer ensures resources are freed exactly once, with clear error messages if the project is used after closing.

### Architectural Concerns

**The four-step conversion pipeline is expensive.** Every value crosses: Rust ty types -> Rust DTOs -> PyO3 native objects -> Python dicts (via `_to_python`) -> Pydantic models. The `_to_python` step using `dir()` introspection is the weakest link. See [Section 4](#4-python-bridge-review) for details.

**CodeGraph builds eagerly and sequentially.** `CodeGraph.build()` processes every file one at a time. For large projects, this is a bottleneck. The `update_file()` method exists for incremental updates but has an index invalidation bug. See [Section 5](#5-codegraph-review).

**The fallback chain obscures failures.** When `file_occurrences` fails, it falls back to token-based resolution, which silently skips failed tokens. The graph can be incomplete with no indication of what was missed. Consider adding a `build_warnings` counter or similar metric.

---

## 3. Rust Layer Review

### `project.rs` — The Core API Surface (622 lines)

This is the heart of the Rust extension. Every Python API call ultimately lands here.

**State management** uses `Mutex<Option<TyProjectState>>`:

```rust
struct TyProjectState {
    db: ProjectDatabase,    // Salsa database
    root: SystemPathBuf,
}

pub struct PyTyProject {
    inner: Mutex<Option<TyProjectState>>,
}
```

The `lock_state()` helper acquires the mutex and checks for `None` (closed project) in a single operation — clean and correct.

**`navigate_to_targets()`** (`project.rs:80-113`) is a well-designed shared implementation for goto_definition, goto_declaration, and goto_type_definition. It accepts a function pointer to the specific ty_ide navigation function, eliminating code duplication across three methods. This is the right abstraction level.

**`collect_symbols_recursive()`** (`project.rs:116-162`) handles the hierarchical-to-flat symbol conversion. It walks ty_ide's tree structure and builds qualified names by threading `parent_name` through recursion. The qualified name construction (`format!("{}.{}", parent, name)`) is clean.

**Performance considerations:**
- `workspace_symbols()` (`project.rs:345-386`) caches `(source_text, LineIndex)` per file in a `HashMap` to avoid recomputing the LineIndex for symbols in the same file — good optimization.
- `check_file()` (`project.rs:279-308`) runs a full project check (Salsa-cached), then filters diagnostics in Rust before constructing DTOs. This avoids sending unnecessary data across FFI.

**Issue: `workspace_symbols` container_name semantics** (`project.rs:375-379`). The `container_name` parameter receives `imported_from.module_name()` for workspace symbols, but in `document_symbols`, it means "enclosing class/function". The same field means different things in different contexts — this could confuse consumers of the Symbol model.

### `occurrences.rs` — The Batch Resolution Engine (178 lines)

This is the most sophisticated piece of Rust code in the project. It implements Phase 5's batch occurrence resolution, replacing O(n) FFI calls with a single Rust-side pass.

**Algorithm:**
1. Get semantic tokens for the file (all tokens, not just names)
2. Parse the module AST and create a SemanticModel
3. For each name-like token:
   - Find the covering AST node using `covering_node()`
   - Extract `ExprName` or `Identifier`
   - Classify the role (Definition, Import, or Read) from token modifiers
   - Resolve the definition using `definition_for_name()`
   - Build qualified name by walking the scope chain

**`build_qualified_name()`** (`occurrences.rs:137-177`) is the most complex helper. It walks upward through scope chains, collecting class and function names while skipping synthetic scopes (lambda, comprehensions). The logic for stopping at module scope and skipping `<`-prefixed names is correct, but there's a subtlety: deeply nested lambdas inside classes could produce incorrect qualified names if the lambda's scope is skipped but its parent class scope is collected.

**`name_ref_from_node()`** (`occurrences.rs:81-94`) handles both `ExprName` and `Identifier` AST nodes. For `Identifier`, it constructs a synthetic `ExprName` with `AtomicNodeIndex::NONE` — this works but is fragile if ty_ide ever validates node indices.

### `coordinates.rs` — Position Conversion (121 lines)

Handles the 1-based Python position to byte-offset conversion. Key details:

- Uses `PositionEncoding::Utf32` for LSP compatibility (Unicode codepoint counting)
- `char_len_to_byte_offset()` iterates UTF-8 chars to convert codepoint columns to byte offsets
- Validates line >= 1, column >= 1, line within file bounds, column within line length

**Concern:** A comment mentions future "CoordinateMode" support for 0-based indexing, but the current code always assumes 1-based. The `CoordinateMode` enum exists in the Python models but is never wired through to Rust.

### DTO Layer (`dto/` and `convert/`)

Clean, mechanical code. Each ty_ide type has a corresponding DTO with `#[pyclass(frozen)]` and `__repr__`/`__hash__`/`__eq__` implementations where needed. The conversion modules (`convert/`) are straightforward mappings.

**Notable:** `convert/hover.rs` renders the entire hover as Markdown because ty_ide doesn't publicly expose structured `Hover`/`HoverContent` types. This limits what the Python side can do with hover information (e.g., extracting just the type signature vs. the docstring).

---

## 4. Python Bridge Review

### `rust_project.py` — The FFI Bridge (465 lines)

This file is the most critical piece of Python code. Every API call crosses through it.

### CRITICAL: `_to_python()` Uses `dir()` Introspection

**Location:** `rust_project.py:129-159`

```python
def _to_python(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_to_python(item) for item in obj]
    if _is_native_enum(obj):
        return str(obj)

    # PyO3 frozen struct: convert to dict via #[pyo3(get)] fields
    result: dict[str, Any] = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            val = getattr(obj, name)
            if not callable(val):
                result[name] = _to_python(val)
        except Exception:
            pass
    return result
```

**Problems:**

1. **Performance:** `dir()` returns *all* attributes including inherited ones, class methods, etc. For every PyO3 struct crossing the boundary, this does a full attribute enumeration, `getattr()` on each, `callable()` check, and recursive conversion. This is the hot path for every API call.

2. **Silent error swallowing:** The bare `except Exception: pass` means any attribute access failure is silently ignored. If a PyO3 struct has a broken `__getattr__`, you get an empty dict with no indication of failure.

3. **Fragility:** If PyO3 adds methods to frozen structs (e.g., `__class__` changes, new dunder methods), the `name.startswith("_")` filter catches them, but non-dunder methods would be incorrectly included.

**Recommendation:** Define an explicit field registry. Either:
- Add a `__fields__` class attribute to each Rust DTO (`#[classattr]`)
- Or maintain a Python-side mapping: `{PositionDto: ["line", "column"], RangeDto: ["start", "end"], ...}`

This would make conversion O(fields) instead of O(dir()) per object, and make failures explicit.

### SIGNIFICANT: `_build_enum_cache()` Naming Convention Coupling

**Location:** `rust_project.py:100-120`

```python
def _build_enum_cache() -> None:
    for name in dir(_native):
        if name.endswith("Kind") or name.endswith("Type") \
           or name.endswith("Modifier") or name.endswith("Role") \
           or name == "NativeSeverity":
            obj = getattr(_native, name)
            if isinstance(obj, type):
                _ENUM_TYPES.add(obj)
```

The detection of PyO3 enum types relies on naming conventions. If a new DTO enum is added that doesn't end with "Kind", "Type", "Modifier", or "Role", it silently breaks — the enum variant gets converted via `dir()` introspection instead of `str()`, producing a dict instead of a string. Pydantic then rejects it.

**Recommendation:** Have the Rust module expose a `_TYO3_ENUM_TYPES` attribute listing all enum classes, or use a marker trait/base class.

### Error Mapping Pattern

The error mapping pattern is thorough and consistent across all methods:

```python
try:
    native_result = self._inner.some_method(...)
except _NativeClosedError as e:
    raise ProjectClosedError(str(e)) from e
except _NativePathError as e:
    raise PathResolutionError(str(e)) from e
except _NativePositionError as e:
    raise PositionError(str(e)) from e
except _NativeAnalysisError as e:
    raise AnalysisError(str(e)) from e
except OverflowError as e:
    raise PositionError(str(e)) from e
except Exception as e:
    raise InternalTyError(f"Unexpected error in method(): {e}") from e
```

The `OverflowError` catch (in `_goto` and `type_hierarchy`) is a nice touch — PyO3 raises `OverflowError` when a Python int is too large for Rust's `u32`. The catch-all `InternalTyError` with method name context is good for debugging.

**Minor issue:** Not all methods catch the same set of exceptions. For example, `find_references` doesn't catch `_NativePathError`, even though it takes a path parameter. If the Rust side ever raises a path error from `find_references`, it would be caught by the generic `Exception` handler and wrapped as `InternalTyError` instead of `PathResolutionError`.

### Resource Management

The `__del__` finalizer (`rust_project.py:449-464`) is well-implemented:

```python
def __del__(self) -> None:
    if getattr(self, "_inner", None) is None:
        return
    if not getattr(self, "_closed", True):
        warnings.warn(
            "RustProject was not closed explicitly.",
            ResourceWarning, stacklevel=2,
        )
        try:
            self.close()
        except Exception:
            pass
```

The `getattr(..., None)` guards against partially-constructed objects (e.g., if `__init__` raises). The `ResourceWarning` is the right severity level.

**Thread safety note:** `RustProject._closed` is a plain bool. With free-threaded Python 3.13+, there's a TOCTOU race between `_check_open()` and the actual Rust call. The Rust-side `Mutex` provides the real protection, but the Python flag could give false negatives under concurrent access.

---

## 5. CodeGraph Review

### `graph.py` — The Semantic Code Intelligence Graph (1,041 lines)

This is the largest and most complex Python file in the project. It builds a directed graph where symbols are nodes and relationships (references, containment, inheritance) are edges.

### CRITICAL: `update_file()` Index Invalidation Bug

**Location:** `graph.py:918-953`

```python
def update_file(self, session: TyO3Session, path: str) -> None:
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
    # ... edge removal has the same problem ...
    self._index_file(session, path)
```

**The bug:** When rustworkx's `PyDiGraph.remove_node(idx)` is called, it uses a **swap-and-pop** strategy internally. If you remove node at index 5 from a graph with 10 nodes, node 9 is moved to index 5 and the graph shrinks to 9 nodes. This means:

1. `_id_to_index` now has stale entries — the symbol that was at index 9 still maps to index 9, but it's now at index 5.
2. `_file_to_nodes` for other files still contains index 9, which is now invalid.
3. Sorting in reverse helps but doesn't fully fix it — if indices 5 and 9 both need removal, removing 9 first is fine, but then removing 5 swaps what was at index 8 into slot 5. If index 8 belonged to a different file, that file's `_file_to_nodes` is now wrong.

The `try/except: pass` masks the resulting errors, making the bug silent.

**Fix:** Use `self._graph.remove_nodes_from(old_indices)` which handles bulk removal atomically in Rust. Then rebuild all secondary indexes from scratch:

```python
def update_file(self, session: TyO3Session, path: str) -> None:
    old_indices = list(self._file_to_nodes.get(path, []))
    if old_indices:
        self._graph.remove_nodes_from(old_indices)
        self._rebuild_indexes()  # Reconstruct _id_to_index, _file_to_nodes, _file_to_edges
    self._diagnostics.pop(path, None)
    self._index_file(session, path)

def _rebuild_indexes(self) -> None:
    """Reconstruct all secondary indexes from the graph."""
    self._id_to_index.clear()
    self._file_to_nodes.clear()
    self._file_to_edges.clear()
    for idx in self._graph.node_indices():
        node = self._graph[idx]
        self._id_to_index[node.symbol_id] = idx
        self._file_to_nodes[node.file].append(idx)
    # Edge indexes would need rebuilding too, but edge indices
    # are also invalidated by node removal.
```

### SIGNIFICANT: `import_cycles()` Has O(N*M) Complexity

**Location:** `graph.py:802-808`

```python
for mi in module_indices:
    module_sid = self._graph[mi].symbol_id
    file = file_from_symbol_id(module_sid)
    for ni in self._graph.node_indices():  # O(N) per module!
        if self._graph[ni].file == file:
            node_to_module[ni] = module_sid
```

For M modules and N total nodes, this is O(M * N). The `_file_to_nodes` index already has this exact mapping and should be used instead:

```python
for mi in module_indices:
    module_sid = self._graph[mi].symbol_id
    file = file_from_symbol_id(module_sid)
    for ni in self._file_to_nodes.get(file, []):
        node_to_module[ni] = module_sid
```

This makes it O(N) total (each node visited once).

Beyond the performance fix, the entire 70-line DFS implementation (`graph.py:782-854`) can be replaced with rustworkx's built-in `rx.simple_cycles()` or `rx.strongly_connected_components()`. See [Section 8](#8-rustworkx-optimization-opportunities).

### SIGNIFICANT: `_find_enclosing_symbol()` Size Heuristic

**Location:** `graph.py:572`

```python
size = (nr.end.line - nr.start.line) * 10000 + (nr.end.column - nr.start.column)
```

This approximation computes a scalar "size" for range comparison. The multiplier of 10000 means:
- A symbol spanning 10001+ lines produces unexpected comparisons
- The column difference is treated as a fractional component, which works for most cases but is semantically wrong
- If `end.column < start.column` (same line, which shouldn't happen per validation), the math still works because the line difference dominates

**Recommendation:** Use tuple comparison for correctness:

```python
size = (nr.end.line - nr.start.line, nr.end.column - nr.start.column)
```

Then compare tuples directly. Python tuple comparison is lexicographic, which is the right behavior here.

### SIGNIFICANT: `coupling_between()` Is O(E)

**Location:** `graph.py:957-969`

Iterates *every edge in the entire graph* to count references between two files. The `_file_to_edges` index exists but isn't used. For a graph with 100K edges, this is unnecessarily expensive when you only care about two files.

**Fix:** Iterate only outgoing edges from nodes in the two files using `self._graph.out_edges(idx)`.

### `_resolve_references_via_occurrences()` — Phase 5 Batch API

**Location:** `graph.py:214-290`

This is the primary reference resolution path and it's well-designed. The fallback chain is:

1. **Batch occurrences** (preferred) — single Rust call per file
2. **Token-based** (fallback) — semantic tokens + per-token goto_definition
3. **Per-symbol** (legacy, unused in normal flow) — find_references per symbol

The occurrence-based resolver correctly handles:
- Missing target files (skips with `continue`)
- Qualified name mismatches (falls back to short-name lookup via `_find_symbol_in_file`)
- External symbols (creates stub nodes)
- Self-references at definition sites (skipped)

**Minor concern:** When `_find_symbol_in_file` does short-name matching (`graph.py:165-168`), it returns the *first* match by iteration order. If a file has two symbols with the same short name (e.g., a function `save` and a method `save` in different classes), it may return the wrong one. The qualified name path handles this correctly, but the fallback is lossy.

### `_resolve_inheritance()` — Type Hierarchy Edges

**Location:** `graph.py:465-551`

This method adds INHERITS and OVERRIDES edges. The OVERRIDES detection walks the INHERITS chain via BFS to collect ancestor methods, then matches by method name.

**Concern:** Method matching is name-based only (`graph.py:547`). If a child class has a method `save()` and an unrelated ancestor has a different method `save()` (e.g., through diamond inheritance), a spurious OVERRIDES edge is created. This is a fundamental limitation of name-based matching without type information, and is acceptable for a code intelligence tool, but should be documented.

### `_infer_package()` — External Package Heuristic

**Location:** `graph.py:443-463`

Splits on `site-packages/` to extract package names. Known limitations:
- Namespace packages (`google/cloud/storage/` -> returns "google")
- Conda environments (no `site-packages/` directory)
- Editable installs (not in site-packages)
- `_` vs `-` normalization (pip vs filesystem)

The fallback returns `None`, which becomes "unknown" — functional but limits `external_symbols_by_package()` usefulness.

---

## 6. Domain Model Review

### Pydantic Models — Well-Structured

The models in `src/tyo3/models/` are clean and well-designed:

- **`Position`** and **`Range`** (`analysis.py`) use `model_validator(mode="after")` to enforce 1-based positions and start-before-end ordering. The `ConfigDict(from_attributes=True)` enables construction from both dicts and attribute-bearing objects.

- **`SymbolNode`** (`graph/models.py`) uses `ConfigDict(frozen=True)` for immutability — correct since graph nodes shouldn't mutate after insertion.

- **`EdgeData`** (`graph/models.py`) uses `@dataclass(frozen=True, slots=True)` instead of Pydantic — a good choice for the edge payload since it's constructed thousands of times during graph building and dataclasses are faster than Pydantic models for simple data carriers.

### Unused / Speculative Models

**`TyProject`** (`models/core.py:49-72`) is a rich model with `opened_at`, `last_reloaded_at`, `python_version`, `config_path`, etc. Neither `TyO3Session` nor `RustProject` ever construct or return this model. The Rust side doesn't produce it.

**`ProjectFile`** (`models/core.py:74-81`) has the same issue — fully defined, tested, but never instantiated in production code.

**`TyProjectConfig`** and **`BackendInfo`** (`models/_spec.py`) are explicitly marked as "spec-anticipation models" but are re-exported from `core.py` for "backward compatibility" — backward compatibility with what? These models have never been part of a public API.

These models add maintenance burden and test surface without delivering value. They should either be removed or clearly gated behind a `_future` or `_draft` namespace.

### `ReferenceKind` vs `ReferenceRole` Confusion

Two overlapping enums exist:
- **`ReferenceKind`** (`navigation.py:19-24`): `READ`, `WRITE`, `OTHER` — from `find_references()` API
- **`ReferenceRole`** (`navigation.py:27-34`): `READ`, `WRITE`, `IMPORT`, `DEFINITION`, `OTHER` — from `file_occurrences()` API

These have overlapping values but different semantics and different sources. The `CodeGraph` uses `ReferenceRole` on edge data, and the `_resolve_references_for_symbol` method manually maps `ReferenceKind` to `ReferenceRole` (`graph.py:200-205`). This works but the naming similarity is confusing.

---

## 7. Test Suite Review

### Coverage Breadth

The test suite is comprehensive with 20 test files covering:

| Category | Files | Focus |
|---|---|---|
| Model validation | 5 files | Pydantic constraints, enum values, field presence |
| Integration | 3 files | Rust backend operations, snapshots, native objects |
| Graph | 2 files | CodeGraph construction, queries, algorithms |
| Features | 3 files | Semantic tokens, type hierarchy, file occurrences |
| Invariants | 2 files | Data invariants, exception hierarchy |
| Property-based | 1 file | Hypothesis strategies for fuzz testing |
| Performance | 1 file | Timing tests with regression thresholds |
| Coordinate handling | 1 file | Position validation, Unicode, edge cases |

### Strengths

**Session-scoped caching** (`conftest.py`): Expensive operations (RustProject, TyO3Session, CodeGraph) are cached at session scope with `@lru_cache`, avoiding rebuild costs across tests.

**Property-based testing** (`test_property_based.py`): Uses Hypothesis to generate arbitrary inputs and verify no-crash invariants, determinism, and structural properties. This catches edge cases that unit tests miss.

**Snapshot testing** (`test_rust_snapshots.py`): Captures symbol output for regression detection across ty version upgrades.

**`@needs_native` marker**: Tests that require the Rust extension gracefully skip when it's not built, allowing pure-Python model tests to run independently.

### Concerns

**`test_graph.py` is very large** (~54KB). It should be split into focused test files: `test_graph_construction.py`, `test_graph_queries.py`, `test_graph_algorithms.py`, etc.

**Performance tests use absolute timing thresholds** (`test_rust_performance.py`). These are inherently flaky on different hardware. Consider relative timing (operation A should be faster than operation B) or statistical approaches.

**No negative/adversarial tests for CodeGraph**. The graph tests use well-formed fixtures. Tests for malformed inputs (files that fail to parse, symbols with no ranges, circular containment) would strengthen confidence.

---

## 8. RustworkX Optimization Opportunities

The current CodeGraph implementation manually reimplements several algorithms that rustworkx provides natively in Rust. Below are the specific opportunities, ordered by impact.

### 8.1 Replace `import_cycles()` with `rx.simple_cycles()` or SCCs

**Current:** 70+ lines of manual DFS with coloring, stack management, adjacency list construction, plus the O(N*M) module mapping bug.

**Replacement with `rx.simple_cycles()`:**

```python
def import_cycles(self) -> list[list[str]]:
    mod_graph = rx.PyDiGraph()
    file_to_mod: dict[str, str] = {}
    mod_to_idx: dict[str, int] = {}

    for mi in self._graph.node_indices():
        node = self._graph[mi]
        if node.kind == SymbolKind.MODULE:
            mod_to_idx[node.symbol_id] = mod_graph.add_node(node.symbol_id)
            file_to_mod[node.file] = node.symbol_id

    # Build module adjacency from inter-file reference edges
    seen: set[tuple[str, str]] = set()
    dep_kinds = {EdgeKind.IMPORTS, EdgeKind.REFERENCES}
    for edge_idx in self._graph.edge_indices():
        data = self._graph.get_edge_data_by_index(edge_idx)
        if data.kind not in dep_kinds:
            continue
        src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
        src_mod = file_to_mod.get(self._graph[src].file)
        tgt_mod = file_to_mod.get(self._graph[tgt].file)
        if src_mod and tgt_mod and src_mod != tgt_mod:
            pair = (src_mod, tgt_mod)
            if pair not in seen:
                seen.add(pair)
                mod_graph.add_edge(mod_to_idx[src_mod], mod_to_idx[tgt_mod], None)

    raw_cycles = rx.simple_cycles(mod_graph)
    return [[mod_graph[i] for i in cycle] for cycle in raw_cycles]
```

**Alternative with SCCs** (often what you actually want for import cycles):

```python
def import_cycle_groups(self) -> list[list[str]]:
    """Return groups of mutually-dependent modules."""
    # ... build mod_graph as above ...
    sccs = rx.strongly_connected_components(mod_graph)
    return [
        [mod_graph[i] for i in scc]
        for scc in sccs if len(scc) > 1
    ]
```

SCCs are O(V+E) and tell you *which modules are in cycles* without enumerating every cycle path. `simple_cycles()` enumerates all distinct cycles, which can be exponential in pathological cases.

**Impact:** ~50 lines removed. O(N*M) bug fixed. Correctness guaranteed by Rust-native Tarjan/Johnson algorithm.

### 8.2 Replace `subgraph_for_file()` with `graph.subgraph()`

**Current** (`graph.py:883-914`): 30 lines manually creating a new graph, adding nodes one by one, iterating *all edges in the entire graph* to find matching ones, building an old-to-new index mapping.

**Replacement:**

```python
def subgraph_for_file(self, file_path: str) -> rx.PyDiGraph:
    core = set(self._file_to_nodes.get(file_path, []))
    if not core:
        return rx.PyDiGraph()
    included = set(core)
    for ci in core:
        included.update(self._graph.successor_indices(ci))
        included.update(self._graph.predecessor_indices(ci))
    return self._graph.subgraph(list(included))
```

The built-in `subgraph()` copies nodes and edges in Rust, handles re-indexing, and only processes edges within the included set — not the entire graph.

**Impact:** ~20 lines removed. O(E_total) -> O(V_sub + E_sub) performance improvement.

### 8.3 Replace Manual DOT Generation with `graph.to_dot()`

**Current** (`export.py:16-61`): 45 lines of manual string concatenation, custom escaping, index-based node filtering.

**Replacement:**

```python
def to_dot(graph: CodeGraph, *, max_nodes: int | None = None) -> str:
    g = graph.graph
    return g.to_dot(
        node_attr=lambda node: {
            "label": _dot_label(node),
            "color": _kind_color(node.kind.value),
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

The built-in handles all DOT escaping correctly and iterates nodes/edges in Rust. The `max_nodes` feature would need to be handled differently (e.g., pre-filtering with `subgraph()`), but the current index-based filtering is already broken for non-contiguous indices.

**Impact:** ~30 lines removed. Correct DOT escaping guaranteed. The `max_nodes` hack is removed (it was buggy anyway — it compared node indices against a count, which breaks when indices aren't contiguous after removals).

### 8.4 Fix `update_file()` with `remove_nodes_from()`

**Current:** Removes nodes one at a time in a reverse-sorted loop, causing index corruption.

**Replacement:**

```python
def update_file(self, session: TyO3Session, path: str) -> None:
    old_indices = list(self._file_to_nodes.get(path, []))
    if old_indices:
        self._graph.remove_nodes_from(old_indices)
        self._rebuild_indexes()
    self._diagnostics.pop(path, None)
    self._index_file(session, path)
```

`remove_nodes_from()` handles bulk removal atomically in Rust. The subsequent `_rebuild_indexes()` reconstructs all secondary indexes from the graph's current state, ensuring consistency.

**Impact:** Bug fix. ~10 lines simplified. Correctness guaranteed.

### 8.5 Use `add_nodes_from()` and `add_edges_from()` for Batch Construction

**Current:** `_index_file` adds nodes and edges one at a time via `_add_node()` and `_add_edge()`, each making a separate Rust call.

**Opportunity:** Collect all nodes for a file, batch-add them, then batch-add edges:

```python
# Batch add nodes
new_nodes = [module_node] + [self._make_symbol_node(file_str, sym) for sym in symbols]
indices = self._graph.add_nodes_from(new_nodes)
# Update secondary indexes
for node, idx in zip(new_nodes, indices):
    self._id_to_index[node.symbol_id] = idx
    self._file_to_nodes[node.file].append(idx)

# Batch add edges
edge_tuples = [(src_idx, tgt_idx, data) for ...]
self._graph.add_edges_from(edge_tuples)
```

**Impact:** Reduces Python-Rust FFI overhead from O(n) calls to O(1) per file. More significant for large files with many symbols.

### 8.6 Use `rx.node_link_json()` for JSON Export

**Current** (`export.py:127-153`): Manual iteration building dicts with `model_dump()` and `_edge_to_dict()`.

**Replacement:**

```python
def to_json(graph: CodeGraph) -> dict:
    return rx.node_link_json(
        graph.graph,
        node_attrs=lambda n: n.model_dump(mode="json"),
        edge_attrs=lambda e: _edge_to_dict(e),
    )
```

**Impact:** ~15 lines removed. Standard node-link JSON format for interoperability.

### 8.7 Use `filter_nodes()` for Kind Queries

**Current** (`graph.py:688-694`):

```python
def symbols_of_kind(self, kind: SymbolKind) -> list[SymbolNode]:
    return [
        self._graph[i]
        for i in self._graph.node_indices()
        if self._graph[i].kind == kind  # Two lookups per node
    ]
```

**Replacement:**

```python
def symbols_of_kind(self, kind: SymbolKind) -> list[SymbolNode]:
    return [self._graph[i] for i in self._graph.filter_nodes(lambda n: n.kind == kind)]
```

Same applies to `external_symbols()`:

```python
def external_symbols(self) -> list[SymbolNode]:
    return [self._graph[i] for i in self._graph.filter_nodes(lambda n: n.external)]
```

**Impact:** Minor per-call improvement. Eliminates redundant `self._graph[i]` double-lookups.

### 8.8 Use `find_successors_by_edge()` / `find_predecessors_by_edge()`

**Current** (`_edges_of_kind`, `graph.py:631-660`): Manual loop over `in_edges()`/`out_edges()` with kind filtering.

**Replacement** for the common children/parent queries:

```python
def children(self, symbol_id: str) -> list[SymbolNode]:
    idx = self._id_to_index.get(symbol_id)
    if idx is None:
        return []
    return list(self._graph.find_successors_by_edge(
        idx, lambda e: e.kind in {EdgeKind.DEFINES, EdgeKind.CONTAINS}
    ))
```

**Impact:** Minor. Slightly cleaner code, same algorithmic complexity.

### 8.9 Use Constructor Hints for Pre-allocation

**Current:** `rx.PyDiGraph()` with no size hints.

**Improvement:**

```python
def __init__(self) -> None:
    self._graph: rx.PyDiGraph = rx.PyDiGraph(
        node_count_hint=1000,
        edge_count_hint=5000,
    )
```

Or better, compute hints from the file count during `build()`:

```python
file_count = len(files)
graph._graph = rx.PyDiGraph(
    node_count_hint=file_count * 20,    # ~20 symbols per file
    edge_count_hint=file_count * 100,   # ~100 edges per file
)
```

**Impact:** Reduces memory reallocations during graph construction.

### 8.10 Use `coupling_between()` with Targeted Iteration

**Current** (`graph.py:957-969`): Scans all edges O(E).

**Replacement:**

```python
def coupling_between(self, file_a: str, file_b: str) -> int:
    nodes_a = set(self._file_to_nodes.get(file_a, []))
    nodes_b = set(self._file_to_nodes.get(file_b, []))
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

**Impact:** O(E_total) -> O(degree(file_a) + degree(file_b)).

### Summary Table

| Optimization | Lines Saved | Performance Gain | Bug Fixed |
|---|---|---|---|
| 8.1 `simple_cycles()` / SCCs | ~50 | O(N*M) -> O(V+E) | Yes |
| 8.2 `graph.subgraph()` | ~20 | O(E_total) -> O(subgraph) | No |
| 8.3 `graph.to_dot()` | ~30 | Minor | Escaping bugs |
| 8.4 `remove_nodes_from()` | ~10 | Minor | Yes (critical) |
| 8.5 Batch `add_nodes_from()` | ~5 | Fewer FFI calls | No |
| 8.6 `node_link_json()` | ~15 | Minor | No |
| 8.7 `filter_nodes()` | ~3 each | Minor | No |
| 8.8 `find_successors_by_edge()` | ~5 | Minor | No |
| 8.9 Constructor hints | 0 | Memory allocation | No |
| 8.10 Targeted `coupling_between` | ~5 | O(E) -> O(degree) | No |

---

## 9. Prioritized Recommendations

### P0 — Must Fix (Correctness Bugs)

1. **Fix `update_file()` index invalidation** (Section 8.4). Use `remove_nodes_from()` and rebuild secondary indexes. This is a silent data corruption bug.

2. **Fix `import_cycles()` O(N*M) complexity** (Section 8.1). Use `_file_to_nodes` index at minimum; replace with `rx.simple_cycles()` or `rx.strongly_connected_components()` for full fix.

### P1 — Should Fix (Performance / Reliability)

3. **Replace `_to_python()` `dir()` introspection** (Section 4). This is the FFI hot path. Use explicit field lists or Rust-side `__fields__` attributes. Would improve every API call.

4. **Fix `_build_enum_cache()` naming convention coupling** (Section 4). Expose enum types from the Rust module explicitly.

5. **Replace `subgraph_for_file()` manual implementation** (Section 8.2). The current O(E) edge scan is unnecessary when `graph.subgraph()` exists.

6. **Fix `_find_enclosing_symbol()` size heuristic** (Section 5). Use tuple comparison instead of the `* 10000` approximation.

### P2 — Should Improve (Code Quality / Simplification)

7. **Replace manual DOT generation** (Section 8.3). Use `graph.to_dot()` for correctness and simplicity.

8. **Replace manual JSON export** (Section 8.6). Use `rx.node_link_json()`.

9. **Use batch construction APIs** (Section 8.5). `add_nodes_from()` / `add_edges_from()` for graph building.

10. **Fix `coupling_between()` O(E) scan** (Section 8.10). Use targeted `out_edges()` iteration.

11. **Add `_rebuild_indexes()` method** (Section 8.4). Centralize index reconstruction for use after any graph mutation.

### P3 — Nice to Have (Polish)

12. **Use `filter_nodes()` for kind queries** (Section 8.7).
13. **Use constructor hints** (Section 8.9).
14. **Remove unused models** (`TyProject`, `ProjectFile`, `BackendInfo`, `TyProjectConfig`).
15. **Split `test_graph.py`** into focused test files.
16. **Add `find_references` missing `PathResolutionError` catch** in `RustProject`.
17. **Add graph build completeness metrics** (warning count, skipped files count) to `CodeGraph`.
18. **Document `container_name` semantic difference** between `document_symbols` and `workspace_symbols`.
