# TyO3 Graph and Architecture Review

**Review target:** `tyo3_v002.zip`  
**Review focus:** semantic graph correctness, architecture, Rust/Python boundary, DTO design, packaging, test strategy, and maintainability  
**Chosen DTO remediation strategy:** **Option B — Rust DTOs serialize with serde into Python values**

---

## 1. Executive summary

The current TyO3 implementation is a serious prototype. It is not just scaffolding. It contains:

- a public `TyO3Session` facade;
- a Rust-backed `RustProject` implementation;
- a PyO3 extension module;
- Pydantic models for diagnostics, symbols, navigation, tokens, and hierarchy data;
- a `CodeGraph` layer built on `rustworkx`;
- tests and fixtures for native bridge behavior, graph construction, graph queries, updates, property-based invariants, semantic tokens, and coordinate conversion.

That is a strong start.

However, the graph layer is not yet semantically reliable enough for a serious code intelligence library. Several issues are not cosmetic; they can produce incorrect graph edges, unstable symbol identities, incorrect dependency answers, or navigation offsets that point into the wrong source line.

The most important immediate work is **not** to add more features. The first goal should be to make the semantic core deterministic, path-stable, contract-tested, and simple enough to reason about.

The highest-priority findings are:

1. `CodeGraph` resolves references before the enclosing-symbol range cache exists. This causes references to attach to module nodes instead of the actual enclosing function, class, or method.
2. Cross-file reference resolution is file-order-dependent. A project-local symbol can be misclassified as an external stub depending on which file is indexed first.
3. `CodeGraph.update_file()` does not preserve or recreate incoming references from other files.
4. Override detection contains unreachable code, so `OVERRIDES` edges are effectively not produced.
5. Rust coordinate conversion accepts columns beyond the current line and can resolve them into later lines.
6. Dependency graph algorithms operate over a heterogeneous graph without filtering edge semantics, so “dependency” queries currently include structural containment edges.
7. The native DTO bridge is stringly typed and should be replaced with serde-based Python value serialization.
8. Packaging and CI include brittle extension-copying logic, generated build artifacts, inconsistent dev dependency declarations, and tests inside the package source tree.

---

## 2. Validation performed during review

The source archive was extracted and inspected directly.

Python syntax validation passed:

```bash
python -m compileall -q src/tyo3
```

A pytest subset could not start in this sandbox because `rustworkx` is not installed:

```text
ModuleNotFoundError: No module named 'rustworkx'
```

Rust build and native-extension validation could not be run in this sandbox because `cargo` and `maturin` are unavailable. This means the review is primarily static analysis plus Python syntax validation, not a full runtime verification.

---

## 3. Architectural context

TyO3 appears to be aiming for this architecture:

```text
ty/Ruff internals
      ↓
Rust semantic engine
      ↓
PyO3 native boundary
      ↓
Python wrapper
      ↓
Pydantic public models
      ↓
CodeGraph / SCIP-style code intelligence layer
```

That is the right high-level direction. The design gives TyO3 access to the semantic accuracy and performance of ty while exposing a Pythonic API.

The implementation currently blurs several layers:

- The Rust DTO layer exposes many PyO3 classes directly.
- The Python wrapper recursively introspects those classes using custom `__fields__` metadata.
- The graph builder performs construction, indexing, reference resolution, inheritance resolution, diagnostics collection, external stub creation, and graph algorithms in one class.
- Symbol identity is partly derived from absolute or tool-returned file paths rather than a single normalized project-relative path policy.

The recommended direction is to keep each layer much stricter:

```text
Rust core modules
  Own ty/Ruff interaction and convert ty/Ruff values to Rust DTO structs.

Rust PyO3 module
  Exposes one primary project handle and returns plain Python values created via serde.

Python native wrapper
  Maps exceptions, validates plain dict/list data into Pydantic models, normalizes paths.

Python graph builder
  Builds a deterministic multi-pass graph from already-normalized session results.

Python graph object
  Stores immutable graph state and provides query methods over explicitly filtered edge kinds.
```

---

## 4. Severity summary

| Severity | Area | Finding | Immediate action |
|---|---|---|---|
| Critical | Graph correctness | References are resolved before `_file_node_ranges` is populated | Replace single-file indexing with a multi-pass builder |
| Critical | Graph correctness | Project-local references can become external stubs depending on file order | Precompute project file set and defer unresolved project-local edges |
| Critical | Incremental graph | `update_file()` loses incoming references from other files | Remove, mark experimental, or rebuild affected reverse dependencies |
| Critical | Inheritance graph | Override detection has unreachable code | Fix loop and add exact override tests |
| Critical | Rust coordinates | Columns past the current line can be accepted | Slice only the current line before column conversion |
| High | Graph semantics | Dependency algorithms treat all outgoing graph neighbors as dependencies | Filter by semantic edge kinds |
| High | Import graph | `IMPORTS` exists but import occurrences are stored as `REFERENCES` | Model imports explicitly |
| High | Native boundary | DTO conversion relies on string tags and introspection | Replace with serde serialization into Python dict/list values |
| High | Paths | Public and internal path representations are inconsistent | Define and enforce project-relative public paths |
| Medium | Models | Pydantic models are mutable/permissive in many places | Introduce a strict shared base model |
| Medium | CI | Native extension copy is CPython/Linux-specific | Use `maturin develop` in CI |
| Medium | Packaging | Build artifacts are included in the archive | Add `.gitignore`; remove `dist` and `target` directories |

---

## 5. What is good and worth preserving

Before focusing on defects, it is important to preserve the strongest parts of the implementation.

### 5.1 The top-level session API is the right shape

The public `TyO3Session` idea is good. A user should be able to open a project and ask for:

- diagnostics;
- document symbols;
- workspace symbols;
- go-to-definition results;
- references;
- hover information;
- semantic tokens;
- type hierarchy;
- file-level name occurrences.

This is the right public abstraction. Users should not need to know about the native engine, ty/Ruff internals, or PyO3 DTO details.

### 5.2 Batch occurrence collection is directionally correct

The graph layer uses `session.file_occurrences(file_str)` to resolve many references with one Rust call per file. That is the right performance direction. Earlier designs that call `goto_definition` once per token are usually too slow and too chatty across the Python/Rust boundary.

The correct design is:

```text
one Rust call per file
  returns all semantically meaningful occurrences
  includes target identity where possible
  Python graph builder consumes the whole batch
```

Keep this direction. Improve its correctness and target identity, not the general idea.

### 5.3 Typed native exceptions are good

The Rust module creates Python exception types such as `ProjectClosedError`, `PathResolutionError`, `PositionError`, and `AnalysisError`. This is much better than parsing string messages. Keep this.

The improvement needed is consistency: all line/column APIs should map native position failures to the same Python `PositionError` path.

### 5.4 Pydantic public models are the right user-facing contract

The public Python API should return Pydantic models or collections of Pydantic models. That gives callers validation, explicit schema, stable serialization, and good editor ergonomics.

The improvement needed is strictness and path consistency.

---

# Part I — Critical graph correctness issues

---

## 6. Reference resolution happens before the enclosing-symbol range cache exists

### Where this appears

In `src/tyo3/graph/graph.py`, `CodeGraph.build()` indexes files one at a time:

```python
files = session.files()

for file_path in files:
    file_str = str(file_path)
    graph._index_file(session, file_str)
```

Inside `_index_file()`, references are resolved at lines 133-135:

```python
# 2. Build reference edges using the batch occurrence API
#    (Phase 5: single Rust call per file, O(1) FFI instead of O(tokens))
self._resolve_references_via_occurrences(session, file_str)
```

But `_file_node_ranges[file_str]` is only populated later at lines 147-158:

```python
# 5. Pre-materialize range cache for _find_enclosing_symbol (B1)
self._file_node_ranges[file_str] = sorted(
    [...]
)
```

`_resolve_references_via_occurrences()` calls `_find_enclosing_symbol()`:

```python
enclosing_id = self._find_enclosing_symbol(file_str, occ.range)
```

`_find_enclosing_symbol()` checks the cache:

```python
cached = self._file_node_ranges.get(file_str, [])
```

If the cache is empty, it falls back to the module node:

```python
module_id = f"{file_str}::<module>"
if module_id in self._id_to_index:
    return module_id
```

### Why this is serious

This means many reference edges are attached to the file’s module node instead of the function, method, or class that actually contains the reference.

For example, consider this code:

```python
from models import User

def make_user() -> User:
    return User(name="Ada")
```

The desired graph edge is approximately:

```text
app.py::make_user  --REFERENCES-->  models.py::User
```

The current build order can produce:

```text
app.py::<module>  --REFERENCES-->  models.py::User
```

This destroys one of the most valuable properties of a code intelligence graph: answering “which function uses this symbol?”

### Required fix

Replace the current single-file construction with a true multi-pass graph builder.

Recommended phases:

```text
Phase 0: Collect project files
  - Call session.files().
  - Normalize every file path.
  - Store project_files as a set.

Phase 1: Collect document symbols
  - For every file, call session.document_symbols(file).
  - Store the raw symbols in memory.
  - Do not create reference edges yet.

Phase 2: Materialize nodes
  - Create module nodes.
  - Create symbol nodes.
  - Add all nodes to rustworkx.
  - Build id_to_index and file_to_nodes.

Phase 3: Build structural indexes
  - Build name indexes.
  - Build range indexes.
  - Build parent/child containment edges.

Phase 4: Resolve occurrences/references
  - For every file, call session.file_occurrences(file).
  - Find the enclosing symbol using the already-populated range index.
  - Resolve target nodes using the already-populated symbol index.
  - Defer unresolved project-local references.

Phase 5: Resolve inheritance and overrides
  - Resolve class supertypes.
  - Resolve ancestor method tables.
  - Add INHERITS and OVERRIDES edges.

Phase 6: Attach diagnostics
  - Call check_file or use already-collected diagnostics.
```

### Implementation task

Create `CodeGraphBuilder` and move graph construction out of `CodeGraph`.

Suggested module split:

```text
src/tyo3/graph/
  graph.py          # CodeGraph container and query API
  builder.py        # CodeGraphBuilder multi-pass construction
  indexes.py        # SymbolIndex, RangeIndex, FileIndex
  resolver.py       # ReferenceResolver, ExternalSymbolResolver
  models.py         # SymbolNode, EdgeData, EdgeKind
  algorithms.py     # cycles, centrality, reachability
  export.py         # JSON/DOT/SCIP exporters
```

The builder should own mutable construction state. `CodeGraph` should be closer to an immutable query object.

---

## 7. Project-local references are file-order-dependent

### Where this appears

In `_resolve_references_via_occurrences()`, when the target symbol is not already known, the code calls `_ensure_target_node_simple()`:

```python
if target_sid not in self._id_to_index:
    found = self._find_symbol_in_file(target_file, target_name)
    if found:
        target_sid = found
    else:
        target_sid_ensured = self._ensure_target_node_simple(
            target_file, target_sid, target_name
        )
```

`_ensure_target_node_simple()` determines whether the target is project-local by checking whether the target file already has nodes:

```python
# If it's already in project files, skip — it'll be picked up later
if target_file in self._file_to_nodes:
    return None

# It's external — create a stub node
package = self._infer_package(target_file)
ext_sid = f"{package}::{target_name}" if package else target_sid
```

### Why this is serious

`self._file_to_nodes` only contains files already indexed. It is not the same thing as “files in the project.”

Suppose the project has:

```text
app.py
models.py
```

If `session.files()` returns `app.py` first, then while indexing `app.py`, `models.py` has not yet been indexed. A reference from `app.py` to `models.py::User` may be misclassified as an external stub.

If `session.files()` returns `models.py` first, the same reference may resolve correctly.

A graph builder must not produce different semantic graphs based on file enumeration order.

### Required fix

At the beginning of graph construction, compute:

```python
project_files: set[PurePosixPath]
```

Use that set to classify target files:

```python
if target_file in project_files:
    # It is project-local. Do not create an external stub.
    # If the specific symbol is not known yet, defer the edge.
else:
    # It is external. Create or reuse an external symbol stub.
```

### Recommended deferred edge model

During reference resolution, unresolved edges should be captured as explicit data:

```python
class PendingReference(BaseModel):
    source_symbol_id: str
    target_file: PurePosixPath
    target_name: str
    target_qualified_name: str | None
    occurrence_range: Range
    role: ReferenceRole
```

After all symbols are materialized, the builder tries to resolve pending references. If a project-local reference still cannot be resolved, the builder should record a warning or unresolved edge; it should not silently create an external stub.

---

## 8. `update_file()` loses incoming references

### Where this appears

`src/tyo3/graph/graph.py` lines 1075-1093:

```python
def update_file(self, session: TyO3Session, path: str) -> None:
    """Re-index a single file and update the graph in-place.

    Removes all nodes and edges associated with *path*, then re-runs
    :meth:`_index_file` to pick up changes. Incoming edges from
    other files that reference symbols defined in *path* are
    naturally recreated during re-indexing because
    :meth:`_resolve_references_via_occurrences` calls
    ``goto_definition`` / ``file_occurrences`` again.
    """
    old_node_indices = list(self._file_to_nodes.get(path, []))
    if old_node_indices:
        self._graph.remove_nodes_from(old_node_indices)
        self._rebuild_indexes()
    self._diagnostics.pop(path, None)
    self._index_file(session, path)
```

### Why this is serious

The comment is incorrect.

Re-indexing file `models.py` recreates references that originate inside `models.py`. It does not recreate references from `app.py` to `models.py`.

Before update:

```text
app.py::make_user  --REFERENCES-->  models.py::User
```

After removing all `models.py` nodes, this edge is removed. Re-indexing only `models.py` cannot recreate the edge, because the source occurrence lives in `app.py`.

### Required fix

There are three acceptable options.

#### Option A: conservative rebuild

For now, make `update_file()` rebuild the entire graph:

```python
def update_file(self, session: TyO3Session, path: str) -> CodeGraph:
    return CodeGraph.build(session)
```

This is simple and correct. It may be slower, but correctness is more important at this stage.

#### Option B: mark as experimental and unsafe

If the method remains, rename it or document it clearly:

```python
def update_file_experimental_unsafe(...):
    ...
```

Do not let users assume it preserves whole-project graph invariants.

#### Option C: implement real affected-file reindexing

Maintain a reverse file dependency index:

```python
file_references_to: dict[PurePosixPath, set[PurePosixPath]]
```

When `models.py` changes, re-index:

- `models.py` itself;
- every file with outgoing references/imports to `models.py`;
- possibly files affected by inheritance or re-export chains.

This is the best long-term design, but it is more complex. For the next milestone, Option A is acceptable.

---

## 9. Override detection has unreachable code

### Where this appears

`src/tyo3/graph/graph.py` lines 557-573:

```python
ancestor_methods: dict[str, str] = {}  # name -> symbol_id
visited: set[str] = set()
queue: deque[str] = deque([sid])
while queue:
    current_sid = queue.popleft()
    for _src, succ_idx, edge_data in self._graph.out_edges(self._id_to_index[current_sid]):
        if edge_data.kind != EdgeKind.INHERITS:
            continue
            parent_sid = self._graph[succ_idx].symbol_id
            if parent_sid not in visited:
                visited.add(parent_sid)
                queue.append(parent_sid)
            # Collect this parent's methods
            for c in self.children(parent_sid):
                if c.kind in METHOD_KINDS and c.name not in ancestor_methods:
                    ancestor_methods[c.name] = c.symbol_id
```

Everything after `continue` is unreachable.

### Current behavior

`ancestor_methods` remains empty. Therefore this block almost never adds an override edge:

```python
for method in child_methods:
    if method.name in ancestor_methods:
        method_sid = symbol_id_from_symbol(file_str, method)
        parent_method_sid = ancestor_methods[method.name]
        edge = EdgeData(kind=EdgeKind.OVERRIDES)
        self._add_edge(method_sid, parent_method_sid, edge, file_str)
```

### Required fix

The intended logic is:

```python
for _src, succ_idx, edge_data in self._graph.out_edges(self._id_to_index[current_sid]):
    if edge_data.kind != EdgeKind.INHERITS:
        continue

    parent_sid = self._graph[succ_idx].symbol_id
    if parent_sid not in visited:
        visited.add(parent_sid)
        queue.append(parent_sid)

    for child in self.children(parent_sid):
        if child.kind in METHOD_KINDS and child.name not in ancestor_methods:
            ancestor_methods[child.name] = child.symbol_id
```

### Required test

Add a small fixture:

```python
class Base:
    def save(self) -> None:
        pass

class User(Base):
    def save(self) -> None:
        pass
```

Expected edge:

```text
User.save  --OVERRIDES-->  Base.save
```

Test exact source and target symbol IDs. Do not merely assert that the graph has at least one edge.

---

## 10. Rust coordinate conversion can point into the wrong line

### Where this appears

`rust/src/coordinates.rs` lines 48-65:

```rust
let line_start = line_index.line_start(OneIndexed::from_zero_indexed(line_idx), source);

// Convert column (Unicode codepoints) to byte offset
let line_start_usize: usize = line_start.to_usize();
let line_text = &source[line_start_usize..];
let byte_col = char_len_to_byte_offset(line_text, col);

// Validate column does not exceed the line
if usize::from(byte_col) > line_text.len() {
    return Err(format!(
        "Column {} exceeds line {} length ({} bytes)",
        column,
        line,
        line_text.len()
    ));
}

Ok(line_start + byte_col)
```

### Why this is serious

`line_text` is not the current line. It is the rest of the file starting at the current line.

That means a column beyond the end of the current line may still be accepted if the rest of the file is long enough. The resulting byte offset can point into the next line or later.

Example:

```text
x = 1
y = 2
```

A request for line 1, column 8 should be invalid if the first line has fewer than 7 visible characters. The current implementation can accept it because `line_text` includes `\ny = 2`.

### Required fix

Slice only the current line:

```rust
let line_start_usize: usize = line_start.to_usize();
let rest = &source[line_start_usize..];
let line_len = rest.find('\n').unwrap_or(rest.len());
let line_text = &rest[..line_len];

let byte_col = char_len_to_byte_offset(line_text, col);

if byte_col.to_usize() > line_text.len() {
    return Err(format!(
        "Column {} exceeds line {} length ({} bytes)",
        column,
        line,
        line_text.len()
    ));
}

Ok(line_start + byte_col)
```

The implementation also needs to decide how to handle `\r\n`. Usually, source line text should exclude `\n` and preferably exclude `\r` from the valid column range.

### Required tests

Add Rust unit tests and Python integration tests for:

- column exactly at end of line;
- column one past end of line;
- empty line;
- final line with no trailing newline;
- CRLF line endings;
- ASCII identifiers;
- Unicode BMP characters;
- non-BMP characters;
- combining marks;
- negative line or column values passed from Python;
- very large line or column values.

### Coordinate policy note

The code comments mention “Unicode codepoints” and `PositionEncoding::Utf32`. That should be documented as the official TyO3 public coordinate policy.

The public policy should answer:

- Are lines 1-based or 0-based? Current design appears 1-based.
- Are columns 1-based or 0-based? Current design appears 1-based.
- Are columns UTF-8 bytes, UTF-16 code units, UTF-32 code points, or grapheme clusters? Current design appears UTF-32/codepoint-oriented.

For editor interoperability, this matters.

---

# Part II — Graph model and API design issues

---

## 11. `CodeGraph` is too large and owns too many responsibilities

`src/tyo3/graph/graph.py` is over one thousand lines and currently handles:

- project graph construction;
- module node creation;
- symbol node creation;
- containment edge creation;
- occurrence/reference resolution;
- fallback token-based resolution;
- external stub creation;
- inheritance resolution;
- override resolution;
- diagnostics collection;
- cache rebuilding;
- incremental update;
- dependency queries;
- graph algorithms;
- centrality analysis;
- file neighborhood extraction;
- coupling analysis.

This is a classic god class. The problem is not just size. The problem is that construction-time invariants, query-time invariants, and mutation-time invariants are interleaved.

### Required direction

Split graph code into separate responsibilities.

Recommended shape:

```text
src/tyo3/graph/
  builder.py
    CodeGraphBuilder
    PendingReference
    BuildWarnings

  graph.py
    CodeGraph
    Read-only graph query API

  indexes.py
    SymbolIndex
    RangeIndex
    FileIndex
    EdgeIndex

  resolver.py
    ReferenceResolver
    ExternalSymbolResolver
    InheritanceResolver

  algorithms.py
    import_cycles()
    dependency_cycles()
    centrality()
    transitive_closure()

  models.py
    SymbolNode
    EdgeData
    EdgeKind

  identity.py
    canonical_symbol_id()
    canonical_file_id()
    parse_symbol_id()

  export.py
    JSON/DOT/SCIP exporters
```

### What `CodeGraph` should become

`CodeGraph` should mostly hold:

```python
class CodeGraph(BaseModel):
    graph: rx.PyDiGraph
    id_to_index: dict[str, int]
    file_to_nodes: dict[PurePosixPath, list[int]]
    diagnostics: dict[PurePosixPath, list[Diagnostic]]
```

It should provide query helpers, but not perform construction work.

---

## 12. Dependency queries use the wrong edge universe

### Where this appears

`CodeGraph.dependencies()` currently returns all outgoing neighbors:

```python
def dependencies(self, symbol_id: str) -> set[str]:
    idx = self._id_to_index.get(symbol_id)
    if idx is None:
        return set()
    return {
        self._graph[succ].symbol_id
        for succ in self._graph.neighbors(idx)
    }
```

`dependents()`, `transitive_dependencies()`, and `transitive_dependents()` have similar issues because they operate over the whole heterogeneous graph.

### Why this is wrong

The graph contains different edge types:

- `DEFINES`
- `CONTAINS`
- `REFERENCES`
- `IMPORTS`
- `INHERITS`
- `OVERRIDES`

A class does not “depend on” a method merely because it contains it. A module does not “depend on” a function merely because it defines it.

Structural edges and semantic dependency edges must be queried differently.

### Required fix

Dependency queries should filter edge kinds explicitly:

```python
DEFAULT_DEPENDENCY_EDGE_KINDS = {
    EdgeKind.REFERENCES,
    EdgeKind.IMPORTS,
    EdgeKind.INHERITS,
}

def dependencies(
    self,
    symbol_id: str,
    *,
    kinds: set[EdgeKind] = DEFAULT_DEPENDENCY_EDGE_KINDS,
) -> set[str]:
    ...
```

Also provide explicit structural APIs:

```python
children(symbol_id)
parent(symbol_id)
descendants(symbol_id)
ancestors(symbol_id)
```

Do not call structural containment a dependency.

---

## 13. Import edges are declared but not meaningfully modeled

`EdgeKind.IMPORTS` exists, and cycle detection refers to import/reference edges, but import occurrences are added as `REFERENCES` edges:

```python
edge = EdgeData(
    kind=EdgeKind.REFERENCES,
    file=file_str,
    range=occ.range,
    role=role,
)
```

If `role == ReferenceRole.IMPORT`, that should become either:

```text
module A --IMPORTS--> module/package B
```

or both:

```text
function/class/module scope --REFERENCES--> imported symbol
module A --IMPORTS--> module/package B
```

### Recommended policy

Use separate edge meanings:

- `REFERENCES`: symbol-level usage of another symbol.
- `IMPORTS`: module-level dependency on another module/package.
- `INHERITS`: class-level inheritance relation.
- `OVERRIDES`: method-level override relation.
- `DEFINES` / `CONTAINS`: structural ownership.

For import cycle detection, use only module-level `IMPORTS` edges. Do not mix in arbitrary `REFERENCES` edges unless the method is explicitly called `reference_cycles()`.

---

## 14. Symbol identity needs a stricter policy

The current graph uses symbol IDs like:

```text
{file_str}::{qualified_name}
{file_str}::<module>
{package}::{target_name}
```

There are several issues:

1. `file_str` may be absolute or relative depending on where it came from.
2. External symbols use a different naming policy from project symbols.
3. A fallback may use short name when a qualified name is unavailable.
4. Duplicate names in nested scopes can collide if the qualified name is incomplete.
5. Stable cache keys should not depend on a developer’s local filesystem path.

### Recommended public identity policy

Use project-relative POSIX paths for all first-party code:

```text
project://src/package/module.py::Class.method
```

or simpler:

```text
src/package/module.py::Class.method
```

Use a separate explicit scheme for external symbols:

```text
external://pydantic/BaseModel
stdlib://typing/Protocol
```

Do not mix raw absolute paths into stable graph IDs.

### Recommended internal model

```python
class SymbolId(BaseModel):
    scheme: Literal["project", "external", "stdlib"]
    file: PurePosixPath | None = None
    package: str | None = None
    qualified_name: str
```

This does not have to be the public object immediately, but the parsing/formatting logic should live in `identity.py`, not be scattered through graph construction.

---

## 15. External stubs should be deliberate, not fallback noise

The graph creates external stub nodes when it cannot resolve a target locally. This is useful, but it must be carefully controlled.

A stub should mean:

> “The target is outside the project, and we intentionally represent it as an external dependency.”

A stub should not mean:

> “The builder had not indexed that file yet,” or “The symbol-name heuristic failed.”

### Recommended fields for external stubs

```python
class ExternalSymbolNode(SymbolNode):
    package: str
    version: str | None = None
    source: Literal["stdlib", "site-packages", "typeshed", "unknown"]
    resolution_confidence: Literal["exact", "package", "heuristic"]
```

If the graph uses a single `SymbolNode`, add optional fields:

```python
is_external: bool = False
external_package: str | None = None
external_source: str | None = None
resolution_confidence: str | None = None
```

This helps downstream users decide whether an edge is reliable.

---

# Part III — Rust/Python DTO boundary

---

## 16. Current DTO bridge is too stringly typed

### Current state

The Rust module registers many DTO classes in `rust/src/lib.rs`:

```rust
m.add_class::<dto::PositionDto>()?;
m.add_class::<dto::RangeDto>()?;
m.add_class::<dto::FileRangeDto>()?;
m.add_class::<dto::SymbolKindDto>()?;
m.add_class::<dto::SymbolDto>()?;
...
```

The Rust DTO structs derive both serde and a custom `PyFields` macro:

```rust
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PyFields)]
pub struct CheckResultDto {
    pub diagnostics: Vec<DiagnosticDto>,
    pub files_checked: Option<u32>,
    pub elapsed_ms: Option<u64>,
}
```

Python recursively converts native objects using custom `__fields__` metadata in `src/tyo3/rust_project.py`:

```python
fields = getattr(type(obj), "__fields__", None)
if fields is not None:
    return _convert_struct(obj, fields)
```

Field tags are strings:

```text
"str"
"int"
"float"
"bool"
"obj"
"opt:<tag>"
"list:<tag>"
```

There is also a native enum registry:

```rust
m.add("_TYO3_ENUM_TYPES", vec![...])?;
```

Python uses it to decide whether to convert a native enum by calling `str(obj)`.

### Why this should be replaced

This design creates a parallel schema language separate from Rust types and Pydantic types. Every new DTO requires coordination across:

- Rust struct fields;
- Rust PyO3 class registration;
- custom derive output;
- string field tags;
- enum registry;
- Python recursive converter;
- Pydantic model validation.

That is too much machinery for internal transport.

The public API is Pythonic/Pydantic. The Rust DTO classes do not need to be public user-facing objects. They should be internal Rust structs serialized into ordinary Python dict/list/scalar values.

---

## 17. Chosen remediation: Option B — Rust DTOs serialize with serde into Python values

### Target state

Rust keeps strongly typed DTO structs and enums:

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DefinitionTargetDto {
    pub path: String,
    pub range: RangeDto,
    pub selection_range: Option<RangeDto>,
    pub symbol: Option<SymbolDto>,
    pub module_name: Option<String>,
}
```

But these DTOs are no longer exposed as PyO3 classes. Instead, each native method serializes the return value into plain Python values:

```text
Rust DTO struct/list
  ↓ serde
Python dict/list/str/int/bool/None
  ↓ Pydantic validation
TyO3 public model
```

The Python wrapper receives a normal dict or list:

```python
raw = self._inner.goto_definition(path, line, column)
return [DefinitionTarget.model_validate(item) for item in raw]
```

No `__fields__`, no string tags, no fallback `dir()` introspection, no native DTO class registry.

### Why this option is best for TyO3

It preserves strong Rust-side typing while simplifying the Python boundary.

Benefits:

- Rust DTOs remain compile-time checked.
- Python receives ordinary serializable data.
- Pydantic remains the public schema authority.
- PyO3 module exports fewer classes.
- Adding a DTO field requires fewer changes.
- Tests become simpler and more meaningful.
- The native boundary becomes easier to document.

### Rust dependency strategy

The current `rust/Cargo.toml` already includes serde:

```toml
serde = { version = "1", features = ["derive"] }
```

Add one of these conversion strategies:

#### Preferred: use a serde-to-Python converter crate

Use a crate compatible with the current PyO3 version that converts `Serialize` values into Python objects. The commonly used pattern is a helper such as:

```rust
fn to_py<T: serde::Serialize>(py: Python<'_>, value: &T) -> PyResult<Py<PyAny>> {
    pythonize::pythonize(py, value).map(|bound| bound.unbind())
}
```

The exact crate version must be chosen to match the active PyO3 version.

#### Fallback: serialize through `serde_json::Value`

If dependency compatibility is a concern, implement a small local converter:

```rust
fn json_value_to_py(py: Python<'_>, value: serde_json::Value) -> PyResult<Py<PyAny>> {
    match value {
        serde_json::Value::Null => Ok(py.None()),
        serde_json::Value::Bool(v) => Ok(v.into_pyobject(py)?.unbind().into()),
        serde_json::Value::Number(v) => {
            if let Some(i) = v.as_i64() {
                Ok(i.into_pyobject(py)?.unbind().into())
            } else if let Some(u) = v.as_u64() {
                Ok(u.into_pyobject(py)?.unbind().into())
            } else if let Some(f) = v.as_f64() {
                Ok(f.into_pyobject(py)?.unbind().into())
            } else {
                Err(pyo3::exceptions::PyValueError::new_err("invalid JSON number"))
            }
        }
        serde_json::Value::String(v) => Ok(v.into_pyobject(py)?.unbind().into()),
        serde_json::Value::Array(values) => {
            let list = pyo3::types::PyList::empty(py);
            for item in values {
                list.append(json_value_to_py(py, item)?)?;
            }
            Ok(list.unbind().into())
        }
        serde_json::Value::Object(map) => {
            let dict = pyo3::types::PyDict::new(py);
            for (k, v) in map {
                dict.set_item(k, json_value_to_py(py, v)?)?;
            }
            Ok(dict.unbind().into())
        }
    }
}

fn to_py<T: serde::Serialize>(py: Python<'_>, value: &T) -> PyResult<Py<PyAny>> {
    let value = serde_json::to_value(value)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    json_value_to_py(py, value)
}
```

This fallback is slightly less direct but still removes the custom `__fields__` system.

### Enum serialization policy

Use serde renames so Rust emits exactly the strings expected by Pydantic.

For example:

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SeverityDto {
    Fatal,
    Error,
    Warning,
    Information,
}
```

If the Python model expects values like `"class"`, `"function"`, `"read"`, and `"write"`, Rust must serialize those exact strings.

For names that conflict with Rust keywords or style, use explicit renames:

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum SymbolKindDto {
    #[serde(rename = "class")]
    Class,
    #[serde(rename = "function")]
    Function,
    #[serde(rename = "method")]
    Method,
}
```

### New PyO3 method shape

Current methods likely return native DTO classes or lists of them. The new pattern should be:

```rust
#[pymethods]
impl PyTyProject {
    fn document_symbols<'py>(
        &self,
        py: Python<'py>,
        path: &str,
    ) -> PyResult<Py<PyAny>> {
        let symbols: Vec<SymbolDto> = self.inner_document_symbols(path)?;
        to_py(py, &symbols)
    }

    fn check<'py>(&self, py: Python<'py>) -> PyResult<Py<PyAny>> {
        let result: CheckResultDto = self.inner_check()?;
        to_py(py, &result)
    }
}
```

For expensive analysis, combine this with GIL release:

```rust
fn check<'py>(&self, py: Python<'py>) -> PyResult<Py<PyAny>> {
    let result = py.allow_threads(|| self.inner_check())?;
    to_py(py, &result)
}
```

Do not create Python objects while the GIL is released. Compute Rust DTOs without the GIL, then serialize to Python values after reacquiring it.

### Python wrapper after the change

`src/tyo3/rust_project.py` can delete most of the recursive conversion machinery:

Remove:

- `_build_enum_cache()`;
- `_is_native_enum()`;
- `_to_python()`;
- `_convert_struct()`;
- `_convert_by_tag()`;
- `_convert_struct_fallback()`;
- all assumptions about `__fields__`.

Replace with small validation helpers:

```python
def _validate_model[T](model: type[T], raw: Any) -> T:
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        raise NativeDataValidationError(str(e)) from e


def _validate_list[T](model: type[T], raw: Any) -> list[T]:
    if not isinstance(raw, list):
        raise NativeDataValidationError(f"Expected list, got {type(raw).__name__}")
    return [_validate_model(model, item) for item in raw]
```

Example method:

```python
def document_symbols(self, path: str | Path) -> list[Symbol]:
    self._ensure_open()
    try:
        raw = self._inner.document_symbols(str(path))
    except _NativePathResolutionError as e:
        raise PathResolutionError(str(e)) from e
    except _NativeAnalysisError as e:
        raise AnalysisError(str(e)) from e
    return _validate_list(Symbol, raw)
```

### Tests to remove or rewrite

The current native bridge tests assert that DTO classes expose `__fields__`. Those tests should be removed or rewritten.

Replace them with tests that assert:

1. Native methods return built-in Python containers.
2. No native DTO classes are exported except the project handle and exceptions.
3. Returned dicts validate into Pydantic models.
4. Enum values are serialized as expected strings.
5. Unknown or malformed native payloads fail with a Python-side validation error.

Example:

```python
def test_document_symbols_returns_plain_python(session):
    raw = session._project._inner.document_symbols("main.py")
    assert isinstance(raw, list)
    assert all(isinstance(item, dict) for item in raw)
    assert Symbol.model_validate(raw[0])
```

### PyO3 module export after the change

`rust/src/lib.rs` should become much smaller:

```rust
#[pymodule]
#[pyo3(name = "_native_impl")]
fn native_impl(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<project::PyTyProject>()?;

    m.add("ProjectClosedError", m.py().get_type::<ProjectClosedError>())?;
    m.add("PathResolutionError", m.py().get_type::<PathResolutionError>())?;
    m.add("PositionError", m.py().get_type::<PositionError>())?;
    m.add("AnalysisError", m.py().get_type::<AnalysisError>())?;

    Ok(())
}
```

No DTO classes. No enum registry.

---

## 18. Native target identity is too weak for graph construction

### Where this appears

`rust/src/convert/navigation.rs` returns `DefinitionTargetDto` with:

```rust
symbol: None,
module_name: None,
```

### Why this matters

The graph builder needs stable symbol identity. If navigation targets do not include symbol or module identity, the Python graph layer has to reconstruct identity heuristically from:

- file path;
- target name;
- target range;
- target qualified name if available;
- local symbol indexes.

This is fragile.

### Required direction

Improve Rust-side conversion to return as much identity as ty can provide.

A `DefinitionTargetDto` should ideally include:

```rust
pub struct DefinitionTargetDto {
    pub path: String,
    pub range: RangeDto,
    pub selection_range: Option<RangeDto>,
    pub symbol: Option<SymbolDto>,
    pub module_name: Option<String>,
}
```

If ty does not expose the symbol directly, create a deliberate Rust resolver or document the limitation explicitly. Do not leave this as accidental `None` forever while the Python graph layer guesses.

---

## 19. Rust diagnostics conversion can panic

### Where this appears

`rust/src/convert/diagnostics.rs` uses:

```rust
let file = span.expect_ty_file();
```

in both `extract_file_and_range()` and `diagnostic_matches_file()`.

### Why this matters

The comment says diagnostics from the ty checker should carry ty file spans. That may be true today, but this is a library boundary. A panic from an internal assumption can crash the native extension path and produce a bad Python user experience.

### Required fix

Convert this into recoverable behavior.

Possible shape:

```rust
let Some(file) = span.ty_file() else {
    return (None, None);
};
```

Use whatever non-panicking API is available in the Ruff/ty span type. If no such API exists, isolate the assumption and return a native `AnalysisError` rather than panicking.

---

## 20. Expensive Rust operations should release the GIL

Operations such as these are likely CPU-heavy:

- `check()`;
- `check_file()`;
- `document_symbols()` on large files;
- `workspace_symbols()`;
- `goto_definition()`;
- `find_references()`;
- `file_occurrences()`;
- `type_hierarchy()`.

PyO3 methods hold the Python GIL by default. If these methods run while holding the GIL, they block other Python threads unnecessarily.

### Required pattern

Compute Rust DTOs inside `allow_threads`, then serialize after reacquiring the GIL:

```rust
fn file_occurrences<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Py<PyAny>> {
    let occurrences = py.allow_threads(|| self.inner_file_occurrences(path))?;
    to_py(py, &occurrences)
}
```

Do not touch Python objects inside the `allow_threads` closure.

---

# Part IV — Pydantic model and path design

---

## 21. Public path semantics are inconsistent

Current models use a mixture of:

- `PurePosixPath`;
- `str`;
- graph node `file: str`;
- Rust-returned paths;
- README examples that appear project-relative.

Examples:

- `FileRange.path: PurePosixPath`
- `Diagnostic.file: str | None`
- `DefinitionTarget.path: PurePosixPath`
- `Reference.path: PurePosixPath`
- `SymbolNode.file: str`

### Required policy

Adopt this rule:

> Public TyO3 Python models should expose first-party project files as normalized project-relative `PurePosixPath` values.

The native layer may use absolute paths internally. The public API should not.

### Why project-relative paths are better

They are:

- reproducible across machines;
- stable in snapshots;
- stable in caches;
- compatible with SCIP-style symbol IDs;
- easier to diff in tests;
- safer to expose in logs and user-facing output.

### Implementation direction

`RustProject` should own the root path and normalize every native-returned path before Pydantic validation or immediately after validation.

Better:

```python
class RustProject:
    root: Path

    def _to_project_path(self, raw: str | Path) -> PurePosixPath:
        abs_path = Path(raw).resolve()
        rel = abs_path.relative_to(self.root)
        return PurePosixPath(rel.as_posix())
```

For paths outside the project, use an explicit external representation. Do not pretend an external site-packages path is a project-relative path.

---

## 22. Models should use a shared strict base

Many Pydantic models currently set only:

```python
model_config = ConfigDict(from_attributes=True)
```

For a semantic library, it is better to be strict by default.

Recommended base:

```python
from pydantic import BaseModel, ConfigDict

class TyO3Model(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_assignment=True,
    )
```

Then public models inherit from `TyO3Model`:

```python
class Position(TyO3Model):
    line: int
    column: int
```

If some models need `from_attributes=True` for transition compatibility, add it deliberately:

```python
class NativeValidatedModel(TyO3Model):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        from_attributes=True,
    )
```

### Why strict models matter

Strict models catch native serialization drift immediately. With serde-based DTOs, Rust emits dicts. If Rust adds, removes, or renames a field unexpectedly, Python should fail loudly in tests rather than silently accepting ambiguous data.

---

## 23. Use `default_factory` for collection defaults

`src/tyo3/models/navigation.py` includes:

```python
supertypes: list[TypeHierarchyItem] = []
subtypes: list[TypeHierarchyItem] = []
```

Use:

```python
supertypes: list[TypeHierarchyItem] = Field(default_factory=list)
subtypes: list[TypeHierarchyItem] = Field(default_factory=list)
```

Even though Pydantic v2 handles mutable defaults more safely than plain dataclasses, `default_factory` is the clearer and more idiomatic contract.

---

# Part V — Dependency graph cache issues

---

## 24. `DependencyGraph.symbols_of_kind()` likely compares different types

`src/tyo3/graph/dependency.py` has:

```python
def symbols_of_kind(self, kind: str) -> list[SymbolNode]:
    result = []
    for idx in self.graph.node_indices():
        node: SymbolNode = self.graph[idx]
        if node.kind == kind:
            result.append(node)
    return result
```

`node.kind` is a `SymbolKind`, not a plain string. Passing `"function"` will not match unless coercion happens elsewhere.

Fix:

```python
def symbols_of_kind(self, kind: SymbolKind | str) -> list[SymbolNode]:
    expected = kind if isinstance(kind, SymbolKind) else SymbolKind(kind)
    return [
        self.graph[idx]
        for idx in self.graph.node_indices()
        if self.graph[idx].kind == expected
    ]
```

---

## 25. Cached `EdgeData.role` deserializes as a raw string

In `DependencyGraph.load()`:

```python
edge_obj = EdgeData(
    kind=EdgeKind(raw_edge["kind"]),
    file=raw_edge.get("file"),
    role=raw_edge.get("role"),
)
```

But `EdgeData.role` expects `ReferenceRole | None`, not `str | None`.

Fix:

```python
role = raw_edge.get("role")
edge_obj = EdgeData(
    kind=EdgeKind(raw_edge["kind"]),
    file=raw_edge.get("file"),
    role=ReferenceRole(role) if role is not None else None,
)
```

Also persist and restore edge `range` if it matters for cached graph fidelity.

---

# Part VI — Packaging, CI, and repository hygiene

---

## 26. Generated build artifacts are included

The archive includes:

```text
dist/
rust/target/
rust/tyo3-derive/target/
```

These should not be in source control or source archives.

Add a `.gitignore` similar to:

```gitignore
/target/
/rust/target/
/rust/tyo3-derive/target/
/dist/
/build/
/htmlcov/
/.coverage
/.pytest_cache/
/.ruff_cache/
/.mypy_cache/
/.ty/
/.venv/
*.so
*.pyd
*.dylib
*.egg-info/
__pycache__/
```

---

## 27. Add a root `LICENSE`

`pyproject.toml` declares:

```toml
license = { text = "MIT" }
```

But the archive does not appear to include a root `LICENSE` file. Add one.

---

## 28. Tests should not live inside `src/tyo3`

Tests currently live under:

```text
src/tyo3/tests/
```

Prefer:

```text
tests/
fixtures/
src/tyo3/
```

This avoids accidentally packaging tests and fixtures into production wheels. It also makes import behavior closer to how users import the installed package.

Update `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
```

---

## 29. CI hard-codes a CPython/Linux extension filename

Current CI does:

```bash
cd rust
cargo build --release
cp target/release/lib_native_impl.so ../src/tyo3/_native_impl.cpython-313-x86_64-linux-gnu.so
```

This is brittle. It assumes:

- Linux;
- CPython 3.13;
- x86_64;
- the exact extension suffix;
- manual copy rather than Python packaging metadata.

Use maturin instead:

```bash
uv sync --all-extras --dev
uv run maturin develop --release
uv run pytest
```

Recommended CI checks:

```bash
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test --all
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run maturin develop --release
uv run pytest
```

---

## 30. Dev dependencies are split inconsistently

`pyproject.toml` has both:

```toml
[project.optional-dependencies]
dev = [...]
```

and:

```toml
[dependency-groups]
dev = [...]
```

They contain different dependencies.

Pick one primary workflow. If the project uses `uv`, dependency groups are fine, but then CI and docs should consistently use:

```bash
uv sync --group dev
```

or equivalent.

Also ensure dev dependencies include everything needed for tests:

- `pytest`
- `pytest-cov`
- `hypothesis`
- `ruff`
- `ty`
- `maturin`
- any import-time dependencies such as `rustworkx`

---

## 31. Add `py.typed`

If TyO3 is intended to be a typed Python library, include:

```text
src/tyo3/py.typed
```

and configure package data if needed.

---

# Part VII — Testing strategy

---

## 32. Current tests are broad but not precise enough

The suite covers many areas, which is good. But many tests appear to assert shape rather than exact semantics.

Weak test patterns include:

```python
assert isinstance(result, list)
assert graph.edge_count >= 0
assert len(symbols) >= 1
```

These are useful smoke tests, but they do not protect the semantic graph.

For TyO3, tests should be contract tests. The library’s value is semantic precision.

### Better assertions

Use exact expected results for small fixtures:

```python
assert graph.references_from("app.py::make_user") == [
    ReferenceEdge(
        target="models.py::User",
        role=ReferenceRole.CALL,
        range=Range(...),
    )
]
```

or, if comparing model objects is awkward:

```python
edges = graph.references_from("app.py::make_user")
assert {(target.symbol_id, edge.role) for target, edge in edges} == {
    ("models.py::User", ReferenceRole.READ),
}
```

---

## 33. Required new tests

### 33.1 Reference attaches to function, not module

Fixture:

```python
# models.py
class User:
    pass

# app.py
from models import User

def make_user() -> User:
    return User()
```

Expected:

```text
app.py::make_user --REFERENCES--> models.py::User
```

Not:

```text
app.py::<module> --REFERENCES--> models.py::User
```

### 33.2 File order does not affect graph output

Build the same graph twice with mocked `session.files()` returning:

```python
["app.py", "models.py"]
```

and:

```python
["models.py", "app.py"]
```

The serialized graph should be identical.

### 33.3 Project-local unresolved target is not externalized

If a target file is in `project_files`, unresolved symbols should produce a pending/unresolved project-local reference warning, not an external stub.

### 33.4 Override edge is produced

Fixture:

```python
class Base:
    def save(self):
        pass

class User(Base):
    def save(self):
        pass
```

Expected:

```text
User.save --OVERRIDES--> Base.save
```

### 33.5 Coordinate validation rejects past-line columns

Rust and Python tests should prove that line 1, column beyond line 1 fails even if line 2 has enough bytes.

### 33.6 `update_file()` either rebuilds correctly or is removed

If retained, test:

1. Build graph with `app.py` referencing `models.py`.
2. Update `models.py`.
3. Confirm incoming references from `app.py` still exist.

### 33.7 Dependency queries ignore structural edges

Given:

```python
def f():
    pass
```

The module structurally defines `f`, but `dependencies(module)` should not include `f` unless the query explicitly includes structural edges.

### 33.8 Import cycles use import edges only

Create circular imports:

```python
# a.py
import b

# b.py
import a
```

Expected import cycle:

```text
a.py -> b.py -> a.py
```

Ensure this is based on `IMPORTS`, not accidental `REFERENCES` edges.

---

## 34. Snapshot testing policy

Graph snapshot tests are valuable, but only if symbol IDs are stable. Do not introduce large snapshots until path normalization is fixed.

Recommended snapshot content:

```json
{
  "nodes": [
    {"id": "app.py::<module>", "kind": "module"},
    {"id": "app.py::make_user", "kind": "function"},
    {"id": "models.py::User", "kind": "class"}
  ],
  "edges": [
    {"source": "app.py::<module>", "target": "app.py::make_user", "kind": "defines"},
    {"source": "app.py::make_user", "target": "models.py::User", "kind": "references"}
  ]
}
```

Sort nodes and edges before snapshotting.

---

# Part VIII — Recommended implementation plan

---

## 35. Phase 0 — Repository hygiene

Goal: make the project easier to build and review.

Tasks:

1. Add `.gitignore`.
2. Remove `dist/`, `rust/target/`, and `rust/tyo3-derive/target/` from source control.
3. Add `LICENSE`.
4. Add `src/tyo3/py.typed`.
5. Move tests to root `tests/`.
6. Ensure `uv sync --group dev` or the chosen equivalent installs all test dependencies.

Definition of done:

- Clean checkout contains no generated build outputs.
- Tests are outside the package source tree.
- CI does not manually copy extension files.

---

## 36. Phase 1 — Fix coordinate correctness

Goal: make position conversion safe and explicit.

Tasks:

1. Fix `position_to_offset_with_index()` to slice only the current line.
2. Decide and document CRLF behavior.
3. Add Rust unit tests.
4. Add Python integration tests for invalid positions.
5. Ensure all Python wrapper methods map native position failures to `PositionError`.

Definition of done:

- A column beyond the current line always fails.
- Negative Python line/column values produce `PositionError`, not `InternalTyError`.
- Unicode coordinate policy is documented.

---

## 37. Phase 2 — Replace stringly typed DTO conversion with serde

Goal: simplify the native boundary.

Tasks:

1. Keep Rust DTO structs/enums with `Serialize`/`Deserialize` derives.
2. Remove `#[pyclass]` from DTOs unless a DTO must truly be public.
3. Remove the custom `PyFields` derive from DTOs.
4. Remove `tyo3-derive` if no longer used.
5. Add a Rust `to_py()` helper based on serde-to-Python conversion.
6. Change PyO3 methods to return `Py<PyAny>` containing plain Python values.
7. Delete Python `__fields__` recursive conversion code.
8. Replace native bridge tests with serde/Pydantic validation tests.

Definition of done:

- `_native_impl` exports `TyProject` and exception classes, not every DTO.
- Native method results are plain dict/list/scalar Python objects.
- Python validates every native result with Pydantic.
- There is no `__fields__` system.

---

## 38. Phase 3 — Introduce path normalization

Goal: make public results stable and portable.

Tasks:

1. Define public path policy: project-relative `PurePosixPath` for first-party files.
2. Add root-aware path normalization in `RustProject` or a dedicated path module.
3. Reject first-party API paths that escape the project root.
4. Represent external paths explicitly.
5. Update symbol ID generation to use normalized paths.
6. Update tests and snapshots.

Definition of done:

- Running the same project from two different absolute directories produces identical graph IDs.
- Public first-party paths are project-relative.
- External paths are not silently treated as project files.

---

## 39. Phase 4 — Rebuild `CodeGraph` as a multi-pass builder

Goal: make graph construction deterministic.

Tasks:

1. Create `CodeGraphBuilder`.
2. Add `project_files` set before indexing.
3. Collect all document symbols before resolving references.
4. Add all nodes before resolving references.
5. Build range indexes before resolving references.
6. Defer unresolved project-local references.
7. Create external stubs only for true external files.
8. Add exact semantic graph tests.

Definition of done:

- Graph output is independent of file order.
- References attach to innermost enclosing symbols.
- Project-local references do not become external stubs.

---

## 40. Phase 5 — Fix inheritance and edge semantics

Goal: make graph relationships meaningful.

Tasks:

1. Fix unreachable override detection code.
2. Add exact `OVERRIDES` tests.
3. Split `REFERENCES` and `IMPORTS` semantics.
4. Update dependency algorithms to filter edge kinds.
5. Add module-level import cycle tests.
6. Document each edge kind.

Definition of done:

- `dependencies()` does not include structural children by default.
- Import cycles are based on import edges.
- Override edges exist for simple inheritance fixtures.

---

## 41. Phase 6 — Decide incremental update strategy

Goal: avoid false correctness claims.

Tasks:

1. Either make `update_file()` rebuild the whole graph, or remove it from the public API.
2. If incremental update remains, maintain reverse dependency indexes.
3. Add tests for incoming references after update.
4. Document performance/correctness tradeoffs.

Definition of done:

- `update_file()` cannot silently drop incoming references.

---

# Part IX — Suggested architecture after remediation

---

## 42. Target Python package layout

```text
src/tyo3/
  __init__.py
  session.py
  exceptions.py
  py.typed

  native/
    __init__.py
    project.py          # RustProject wrapper
    validation.py       # Pydantic validation helpers
    errors.py           # native exception mapping
    paths.py            # project path normalization

  models/
    __init__.py
    base.py             # TyO3Model
    core.py
    analysis.py
    symbols.py
    navigation.py
    advanced.py

  graph/
    __init__.py
    builder.py
    graph.py
    indexes.py
    resolver.py
    models.py
    identity.py
    algorithms.py
    dependency.py
    export.py
```

---

## 43. Target Rust package layout

```text
rust/src/
  lib.rs                # PyO3 module registration only
  project.rs            # PyTyProject wrapper methods
  engine.rs             # pure Rust TyEngine / ProjectDatabase owner
  dto/
    mod.rs
    analysis.rs
    symbols.rs
    navigation.rs
    tokens.rs
    hierarchy.rs
    occurrences.rs
  convert/
    mod.rs
    diagnostics.rs
    symbols.rs
    navigation.rs
    tokens.rs
    hierarchy.rs
    occurrences.rs
  py_serde.rs           # serde -> Python value conversion helper
  paths.rs              # root and path resolution
  coordinates.rs        # position conversion
  errors.rs             # native error helpers
```

Key rule:

> `lib.rs` should not know about every DTO class. It should register the project handle and exceptions only.

---

# Part X — Checklist

Use this checklist while making changes.

## Correctness checklist

- [ ] A reference inside a function attaches to the function, not the module.
- [ ] Graph output is independent of file ordering.
- [ ] Project-local unresolved symbols are not externalized.
- [ ] External stubs are marked as external and include package/source metadata.
- [ ] `OVERRIDES` edges are produced for a simple inheritance fixture.
- [ ] `dependencies()` excludes `DEFINES` and `CONTAINS` by default.
- [ ] Import cycle detection uses `IMPORTS` edges.
- [ ] Coordinate conversion rejects columns beyond the current line.
- [ ] Public first-party paths are project-relative.
- [ ] Absolute paths do not appear in stable symbol IDs.

## Native boundary checklist

- [ ] Rust DTOs derive `Serialize` and `Deserialize`.
- [ ] Rust DTOs are not exported as PyO3 classes.
- [ ] PyO3 methods return plain Python values via serde conversion.
- [ ] Python no longer uses `__fields__` or string type tags.
- [ ] Native enum values serialize to strings accepted by Pydantic.
- [ ] Expensive Rust work uses `allow_threads`.
- [ ] Native panics are avoided at the boundary.

## Test checklist

- [ ] Exact graph fixtures exist for symbols and edges.
- [ ] Graph snapshots sort nodes and edges deterministically.
- [ ] Coordinate tests cover Unicode and invalid columns.
- [ ] DTO serialization tests verify plain Python dict/list output.
- [ ] CI runs Rust tests, Python tests, Ruff, ty, and maturin build.

---

## 44. Final guidance

The current implementation has the correct ambition and several good foundations. Do not throw it away. But do not build more features on top of the current graph builder until the semantic construction bugs are fixed.

The first mission should be:

1. fix coordinate conversion;
2. replace the native DTO bridge with serde-generated Python values;
3. introduce strict path normalization;
4. rebuild graph construction as a deterministic multi-pass builder;
5. add exact semantic tests that would have caught the current bugs.

After those changes, TyO3 can become a genuinely strong base for Python code intelligence, dependency analysis, and eventually SCIP/codegraph export.
