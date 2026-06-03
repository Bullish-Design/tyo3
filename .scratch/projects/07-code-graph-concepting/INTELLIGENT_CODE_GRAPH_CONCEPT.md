# Intelligent Code Graph Concept

**Parent project:** TyO3
**Depends on:** TyO3 v0.1 cursor APIs, ty 0.0.40
**Date:** 2026-06-02

## One-line concept

A RustworkX-backed directed property graph that models every symbol, reference, and relationship in a Python project as a first-class queryable structure, built on top of TyO3's semantic engine.

## Purpose

TyO3 v0.1 provides cursor-style code intelligence: "tell me about the symbol at this position." The intelligent code graph provides project-style code intelligence: "tell me about everything and how it connects."

The cursor APIs answer questions one at a time. The graph answers questions about the whole codebase at once:

- What does this function depend on, transitively?
- What breaks if I rename this class?
- Which modules form a dependency cycle?
- What's the full inheritance tree for this hierarchy?
- How tightly coupled are these two packages?
- What's the public API surface of this module?

These questions cannot be answered efficiently by repeated cursor calls. They require a materialized graph structure that captures the full semantic topology of the project.

## Design principles

1. **The graph is the data model.** Symbols are nodes, relationships are edges. The data structure matches the conceptual model — a codebase *is* a graph of interconnected definitions.

2. **Rust does the heavy lifting.** AST walking, name resolution, and type inference stay in Rust. Python receives batch results and assembles the graph.

3. **Pydantic for nodes, dataclasses for edges.** Node payloads extend existing TyO3 models (consistency, serialization). Edge payloads are lightweight frozen dataclasses (construction speed in tight loops).

4. **Diagnostics are separate.** The graph models structure ("what exists and how it connects"). Diagnostics model problems ("what's wrong"). They live in a parallel collection, not on the graph.

5. **External symbols get stub nodes.** References to stdlib, dependencies, and third-party code point to real nodes in the graph. Full dependency graphs are cached separately and resolved lazily.

6. **File-scoped batch construction.** The graph is built one file at a time. This matches ty's analysis model, enables incremental updates, and keeps memory pressure predictable.

---

## Architecture

### Where this sits in TyO3

```text
Consumer code / Scippy / analysis tools
    |
    v
CodeGraph                  <-- NEW: this concept
    |
    v
TyO3Session (cursor APIs) + new batch APIs
    |
    v
RustProject (Python bridge)
    |
    v
PyTyProject (Rust/PyO3 facade)
    |
    v
ty_ide / ty_python_semantic / ProjectDatabase
```

The `CodeGraph` is a Python-side structure that:
- Calls TyO3 session/bridge APIs to get raw data
- Assembles that data into a RustworkX directed graph
- Provides query methods over the graph
- Manages caching and incremental updates

It does not replace the cursor APIs. It builds on them.

### Why RustworkX

RustworkX is a Rust-backed graph library for Python (PyO3-based, same tech as TyO3). Compared to NetworkX:

- **3-100x faster** on graph operations (traversal, pathfinding, component analysis)
- **Lower memory** — Rust-backed adjacency storage vs. Python dict-of-dict-of-dict
- **Stable integer indices** — nodes/edges referenced by `int`, not by data object identity
- **No hashability requirement** — node/edge payloads can be any Python object (including Pydantic models)
- **Built-in algorithms** — topological sort, cycle detection, transitive closure, connected components, shortest paths all run in Rust
- **Filtered traversal** — `filter_nodes()`, `filter_edges()`, `find_successor_node_by_edge()` execute in Rust

The index-based access model is well-suited for a code graph: symbols get stable integer handles, and the payload (Pydantic model or dataclass) is stored separately from the topology.

---

## Graph Data Model

### Node payload: `SymbolNode`

Every node in the graph represents a named symbol — a module, class, function, method, variable, constant, import, or parameter. Nodes use Pydantic models for consistency with TyO3's existing model layer and for serialization support (caching dependency graphs to disk).

```python
from pydantic import BaseModel, ConfigDict
from tyo3.models.analysis import Range, FileRange
from tyo3.models.symbols import SymbolKind

class SymbolNode(BaseModel):
    """A symbol in the code graph.

    Extends the information from TyO3's Symbol model with
    graph-specific identity and documentation fields.
    """
    model_config = ConfigDict(frozen=True)

    symbol_id: str                      # canonical identity (see Identity Scheme)
    name: str                           # short name: "save"
    qualified_name: str                 # dotted path: "models.User.save"
    kind: SymbolKind                    # FUNCTION, CLASS, MODULE, etc.
    file: str                           # defining file: "src/models.py"
    range: Range                        # full definition range
    selection_range: Range | None       # name/focus range
    documentation: str | None = None    # from hover, if available
    signature: str | None = None        # type signature, if available
    external: bool = False              # True for stdlib/dependency symbols
    package: str | None = None          # "stdlib", "pydantic", etc. (if external)
```

### Edge payload: `EdgeData`

Every edge represents a directed relationship between two symbols. Edges use frozen dataclasses for minimal construction overhead — a typical project may have 50,000+ edges built in a tight indexing loop.

```python
from dataclasses import dataclass
from enum import StrEnum
from tyo3.models.analysis import Range

class EdgeKind(StrEnum):
    """Classification of a relationship between two symbols."""

    # Structural containment
    DEFINES = "defines"             # module -> top-level symbol
    CONTAINS = "contains"           # class -> method, function -> nested def

    # References
    REFERENCES = "references"       # enclosing symbol -> referenced symbol

    # Imports
    IMPORTS = "imports"             # module/symbol -> imported symbol

    # Type relationships
    INHERITS = "inherits"           # child class -> parent class
    OVERRIDES = "overrides"         # method -> overridden method in parent
    TYPE_OF = "type_of"             # variable/parameter -> its type's symbol
    RETURNS = "returns"             # function -> return type symbol
    INSTANTIATES = "instantiates"   # call site -> class being instantiated


class ReferenceRole(StrEnum):
    """How a symbol is used at a reference site."""

    READ = "read"
    WRITE = "write"
    IMPORT = "import"
    DEFINITION = "definition"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class EdgeData:
    """Payload stored on every graph edge."""

    kind: EdgeKind
    file: str | None = None         # source file where the relationship manifests
    range: Range | None = None      # source location of the reference/import
    role: ReferenceRole | None = None   # read/write/import (for REFERENCES edges)
```

### Graph topology

```python
import rustworkx as rx

class CodeGraph:
    """A semantic code intelligence graph for a Python project."""

    def __init__(self) -> None:
        self._graph: rx.PyDiGraph = rx.PyDiGraph()

        # ── Secondary indexes ──
        self._id_to_index: dict[str, int] = {}          # symbol_id -> node index
        self._file_to_nodes: dict[str, list[int]] = {}  # file path -> node indices
        self._file_to_edges: dict[str, list[int]] = {}  # file path -> edge indices

        # ── Diagnostics (separate from graph) ──
        self._diagnostics: dict[str, list[Diagnostic]] = {}  # file path -> diagnostics
```

### Node granularity

Nodes represent **definitions**, not occurrences. Every named symbol discovered by `document_symbols()` becomes a node. References to those symbols become edges.

| Entity | Is a node? | Example |
|---|---|---|
| Module (`foo.py`) | Yes (MODULE) | `src/models.py` |
| Class | Yes (CLASS) | `User` |
| Function | Yes (FUNCTION) | `process_data` |
| Method | Yes (METHOD) | `User.save` |
| Variable/constant | Yes (VARIABLE/CONSTANT) | `MAX_RETRIES` |
| Parameter | Yes (PARAMETER) | `self`, `name` |
| Import name | Yes (IMPORT) | `from os import path` |
| Reference occurrence | **No** — becomes an edge | `user.save()` at line 42 |
| Literal / expression | **No** — not in graph | `"hello"`, `42 + x` |

### Edge semantics

When `app.py:main()` references `models.User` at line 42, that becomes:

```
Edge: main -> User
Kind: REFERENCES
File: "src/app.py"
Range: Range(start=Position(42, 10), end=Position(42, 14))
Role: READ
```

If `main()` references `User` three times, there are three separate edges from `main` to `User`, each with a different range. This naturally encodes coupling strength (edge count between two symbols = coupling weight).

### Containment edges

Every file has a MODULE node. Structural containment is modeled as edges:

```
src/models.py (MODULE)
    --DEFINES--> User (CLASS)
        --CONTAINS--> __init__ (METHOD)
        --CONTAINS--> save (METHOD)
        --CONTAINS--> name (FIELD)
    --DEFINES--> MAX_RETRIES (CONSTANT)
```

This lets you use `rx.descendants(graph, module_node)` to get everything defined in a file, or `rx.ancestors(graph, method_node)` to walk up to the enclosing class and module.

### Inheritance edges

```
User (CLASS) --INHERITS--> BaseModel (CLASS, external)
Admin (CLASS) --INHERITS--> User (CLASS)
Admin.save (METHOD) --OVERRIDES--> User.save (METHOD)
```

Inheritance edges point from child to parent (dependency direction). Override edges point from the overriding method to the overridden method.

---

## Symbol Identity Scheme

Every symbol needs a canonical, unambiguous identifier. The scheme uses **file-anchored paths**:

```
<file_path>::<qualified_name>
```

Examples:

```
src/models.py::User
src/models.py::User.save
src/models.py::User.__init__
src/models.py::MAX_RETRIES
src/app.py::main
src/app.py::main.<local>.temp_var
```

### Why file-anchored

- **Unambiguous.** Two files can define a class named `Config` — the file path disambiguates.
- **Stable within a session.** File paths don't change during analysis.
- **Natural for TyO3.** File paths are already the primary key in the session API.
- **Handles locals.** Function-scoped variables get `<local>` segments: `app.py::main.<local>.x`.

### External symbols

External symbols use a package prefix instead of a file path:

```
stdlib::json.loads
stdlib::pathlib.Path
pydantic::BaseModel
pydantic::BaseModel.model_validate
requests::Session.get
```

The `package` field on `SymbolNode` records which package owns the symbol. The `symbol_id` format is the same — `<package>::<qualified_name>` — but the prefix is a package name rather than a file path.

### Identity construction

```python
def make_symbol_id(file: str, qualified_name: str) -> str:
    return f"{file}::{qualified_name}"

def make_external_id(package: str, qualified_name: str) -> str:
    return f"{package}::{qualified_name}"
```

The `qualified_name` comes from TyO3's `Symbol.qualified_name` field (populated from ty's symbol resolution). For symbols without a qualified name (rare — typically only compiler-generated), fall back to `<file>::<name>@<line>` for uniqueness.

---

## External Symbols and Dependency Graphs

### The problem

Your project code references symbols defined in stdlib, third-party packages, and stubs. Without nodes for these symbols, reference and inheritance edges have dangling targets. The graph is incomplete.

### The solution: stub nodes with lazy resolution

Every external reference creates a **stub node** in the project graph immediately:

```python
SymbolNode(
    symbol_id="pydantic::BaseModel",
    name="BaseModel",
    qualified_name="BaseModel",
    kind=SymbolKind.CLASS,
    file="<external>",
    range=...,              # zero range or omitted
    external=True,
    package="pydantic",
)
```

Stub nodes carry enough information to connect edges and answer basic queries ("what external symbols does this project use?"). They are cheap to create and don't require loading the dependency's source.

### Cached dependency graphs

For deeper analysis ("what methods does `BaseModel` expose?", "what's the inheritance chain through stdlib?"), full dependency graphs are built and cached separately:

```python
class DependencyGraph:
    """Cached code graph for an external package."""

    package: str                # "stdlib", "pydantic", "requests"
    version: str                # "3.13", "2.7.0"
    graph: rx.PyDiGraph         # full symbol graph
    _id_to_index: dict[str, int]

    @classmethod
    def build(cls, session: TyO3Session, package: str) -> DependencyGraph:
        """Index a dependency's stubs/source and build its graph."""
        ...

    def lookup(self, symbol_id: str) -> SymbolNode | None:
        """Find a symbol in this dependency graph."""
        ...
```

### Resolution model

```
Project CodeGraph
    |
    | edge target: "pydantic::BaseModel" (stub node)
    |
    v
DependencyGraph cache
    |
    | cache key: ("pydantic", "2.7.0")
    |
    v
Full pydantic graph (loaded lazily from disk or built on demand)
```

Dependency graphs are:

- **Built on demand** — first query that needs detail triggers construction.
- **Cached to disk** — stored at `~/.cache/tyo3/deps/<package>-<version>.graph`. Subsequent sessions load from cache instead of re-indexing.
- **Keyed by (package, version)** — upgrading a dependency invalidates its cache entry.
- **Separate from the project graph** — they are not merged into `_graph`. Cross-graph queries use a `resolve_external()` method.

### What this enables

| Query | How it works |
|---|---|
| "What external packages does this project use?" | Filter stub nodes by `external=True`, group by `package` |
| "What does my code use from pydantic?" | Find all edges targeting `pydantic::*` stub nodes |
| "What methods does `BaseModel` have?" | Lazily load pydantic's `DependencyGraph`, query CONTAINS edges |
| "Full inheritance chain including stdlib" | Traverse INHERITS edges; when hitting a stub, resolve into the dependency graph and continue |
| "Pre-warm analysis for common deps" | Ship pre-built graphs for stdlib, typing, collections.abc |

---

## Diagnostics

Diagnostics live outside the graph in a parallel collection keyed by file path. The graph models structure; diagnostics model problems.

```python
class CodeGraph:
    _diagnostics: dict[str, list[Diagnostic]]   # file -> diagnostics

    def diagnostics_for_file(self, path: str) -> list[Diagnostic]:
        return self._diagnostics.get(path, [])

    def diagnostics_for_symbol(self, symbol_id: str) -> list[Diagnostic]:
        """Find diagnostics whose range overlaps this symbol's definition."""
        node_idx = self._id_to_index.get(symbol_id)
        if node_idx is None:
            return []
        node: SymbolNode = self._graph[node_idx]
        file_diags = self._diagnostics.get(node.file, [])
        return [d for d in file_diags if _ranges_overlap(d.range, node.range)]

    def all_diagnostics(self) -> list[Diagnostic]:
        return [d for diags in self._diagnostics.values() for d in diags]
```

### Why separate

1. Many diagnostics don't map to any single symbol — expression-level type mismatches, syntax errors, unused imports. Forcing them onto nodes or edges loses information or creates awkward mappings.

2. The graph topology should be valid regardless of whether the code has errors. A class with a type error still has methods, still inherits, still gets referenced. Mixing structural and error information conflates two concerns.

3. TyO3 already has a clean `CheckResult` / `Diagnostic` model. The graph can consume it directly without conversion.

4. Incremental update is trivial — re-check a file, replace its diagnostic list. No graph topology changes needed.

### Future: optional graph links

If a use case emerges for "navigate from symbol to its errors," a `diagnostics_for_symbol()` convenience method (shown above) resolves the link on demand via file + range intersection. No need to store the link on the graph itself.

---

## Graph Construction

### Construction pipeline

For each file in the project:

```
1. session.document_symbols(file)   -> add SymbolNodes (MODULE, CLASS, FUNCTION, etc.)
2. batch file_occurrences(file)     -> add REFERENCES edges  (new Rust API — see below)
3. session.check_file(file)         -> populate _diagnostics
4. type_hierarchy per class         -> add INHERITS / OVERRIDES edges  (needs wiring)
5. resolve containment              -> add DEFINES / CONTAINS edges from symbol hierarchy
6. resolve imports                  -> add IMPORTS edges from import occurrences
```

Post-processing:

```
7. create stub nodes for unresolved external targets
8. build secondary indexes (_id_to_index, _file_to_nodes, _file_to_edges)
```

### What exists today vs. what's needed

| Step | TyO3 API | Status |
|---|---|---|
| 1. Document symbols | `session.document_symbols(path)` | Exists |
| 2. File occurrences | `session.file_occurrences(path)` | **Needs new batch Rust API** |
| 3. Diagnostics | `session.check_file(path)` | Exists |
| 4. Type hierarchy | `session.type_hierarchy(path, line, col)` | **Needs ty_ide wiring** |
| 5. Containment | Derived from symbol parent/child | Python-side logic, no new API |
| 6. Imports | Derived from occurrence data | Python-side logic, no new API |

### The batch occurrence API (the hard part)

The most critical missing piece is a Rust function that returns **all resolved name occurrences for a file** in a single call. Today, getting this information requires calling `goto_definition()` for every name token — O(tokens) FFI round trips.

Two implementation strategies:

**Strategy A: Semantic tokens + goto_definition per token**

```
semantic_tokens(file) -> all classified tokens with positions
    for each token:
        goto_definition(file, token.line, token.col) -> resolve symbol identity
```

Semantic tokens (ty_ide export, not yet wired) give you all token positions and classifications in one call. You then resolve each token's identity with `goto_definition`. This is O(tokens) cursor calls but avoids AST walking — it reuses existing APIs.

**Strategy B: Custom Rust batch walker**

```rust
fn file_occurrences(&self, path: &str) -> PyResult<Vec<OccurrenceDto>> {
    // Walk the semantic model for this file
    // For each name binding, resolve its target symbol
    // Return all occurrences in one batch
}
```

A Rust function using `ty_python_semantic` that walks the file's AST / semantic model and resolves every name binding in one pass. Returns all occurrences without per-token FFI round trips. Faster but requires deeper Rust implementation.

**Recommended approach:** Start with Strategy A. It can be built entirely with APIs that either exist or need straightforward wiring (semantic tokens). Profile the result. If O(tokens) cursor calls are too slow for target project sizes, invest in Strategy B. The graph construction logic on the Python side is identical either way — it receives a list of occurrences and builds edges.

---

## Query API

The `CodeGraph` exposes both direct graph access and convenience query methods.

### Graph-level queries

```python
class CodeGraph:
    # ── Direct access ──

    @property
    def graph(self) -> rx.PyDiGraph:
        """The underlying RustworkX graph. For advanced queries."""
        return self._graph

    @property
    def node_count(self) -> int:
        return self._graph.num_nodes()

    @property
    def edge_count(self) -> int:
        return self._graph.num_edges()

    # ── Symbol queries ──

    def symbol(self, symbol_id: str) -> SymbolNode | None:
        """Look up a symbol by its canonical ID."""
        idx = self._id_to_index.get(symbol_id)
        return self._graph[idx] if idx is not None else None

    def symbols_in_file(self, path: str) -> list[SymbolNode]:
        """All symbols defined in a file."""
        return [self._graph[i] for i in self._file_to_nodes.get(path, [])]

    def symbols_of_kind(self, kind: SymbolKind) -> list[SymbolNode]:
        """All symbols of a given kind (CLASS, FUNCTION, etc.)."""
        return [
            self._graph[i] for i in self._graph.node_indices()
            if self._graph[i].kind == kind
        ]

    # ── Reference queries ──

    def references_to(self, symbol_id: str) -> list[EdgeData]:
        """All references pointing to a symbol (incoming REFERENCES edges)."""
        ...

    def references_from(self, symbol_id: str) -> list[tuple[SymbolNode, EdgeData]]:
        """All symbols referenced by a given symbol (outgoing REFERENCES edges)."""
        ...

    def usages(self, symbol_id: str) -> list[FileRange]:
        """All locations where a symbol is used (convenience over references_to)."""
        ...

    # ── Relationship queries ──

    def superclasses(self, symbol_id: str) -> list[SymbolNode]:
        """Direct parent classes (outgoing INHERITS edges)."""
        ...

    def subclasses(self, symbol_id: str) -> list[SymbolNode]:
        """Direct child classes (incoming INHERITS edges)."""
        ...

    def inheritance_chain(self, symbol_id: str) -> list[SymbolNode]:
        """Full MRO-ordered superclass chain (transitive INHERITS)."""
        ...

    def overrides(self, symbol_id: str) -> SymbolNode | None:
        """The method this one overrides, if any."""
        ...

    def overridden_by(self, symbol_id: str) -> list[SymbolNode]:
        """Methods that override this one."""
        ...

    def imports_of(self, file: str) -> list[SymbolNode]:
        """All symbols imported by a file."""
        ...

    def importers_of(self, symbol_id: str) -> list[str]:
        """All files that import a symbol."""
        ...

    # ── Dependency analysis ──

    def dependencies(self, symbol_id: str) -> set[str]:
        """All symbols this one directly depends on (outgoing edges of any kind)."""
        ...

    def dependents(self, symbol_id: str) -> set[str]:
        """All symbols that depend on this one (incoming edges of any kind)."""
        ...

    def transitive_dependencies(self, symbol_id: str) -> set[str]:
        """All symbols reachable from this one (rx.descendants)."""
        ...

    def transitive_dependents(self, symbol_id: str) -> set[str]:
        """All symbols that transitively depend on this one (rx.ancestors)."""
        ...

    # ── Structural analysis ──

    def children(self, symbol_id: str) -> list[SymbolNode]:
        """Direct children (outgoing DEFINES/CONTAINS edges)."""
        ...

    def parent(self, symbol_id: str) -> SymbolNode | None:
        """Enclosing symbol (incoming DEFINES/CONTAINS edge)."""
        ...

    def module_for(self, symbol_id: str) -> SymbolNode | None:
        """The MODULE node for this symbol's file."""
        ...

    # ── Graph algorithms ──

    def import_cycles(self) -> list[list[str]]:
        """Detect circular import chains (cycles in the IMPORTS subgraph)."""
        ...

    def connected_components(self) -> list[set[str]]:
        """Find disconnected clusters of symbols."""
        ...

    def coupling_between(self, file_a: str, file_b: str) -> int:
        """Count reference edges between two files (coupling metric)."""
        ...

    def topological_order(self) -> list[str]:
        """Dependency-order traversal of all symbols."""
        ...
```

### RustworkX algorithms available for free

| Algorithm | Code intelligence use |
|---|---|
| `rx.descendants(g, node)` | Transitive dependency analysis |
| `rx.ancestors(g, node)` | Impact analysis ("what depends on this?") |
| `rx.is_directed_acyclic_graph(g)` | Circular dependency detection |
| `rx.connected_components(g)` | Find isolated module clusters |
| `rx.topological_sort(g)` | Build order / dependency ordering |
| `rx.dijkstra_shortest_paths(g, src)` | Semantic distance between symbols |
| `rx.all_simple_paths(g, src, dst)` | All dependency chains between two symbols |
| `rx.transitivity(g)` | Graph density / coupling metrics |
| `rx.betweenness_centrality(g)` | Find "hub" symbols that bridge modules |
| `rx.weakly_connected_components(g)` | Find isolated subsystems |

---

## Incremental Updates

When a file changes, the graph must be updated without rebuilding from scratch.

### Update protocol

```python
def update_file(self, path: str) -> None:
    """Re-index a single file and update the graph."""

    # 1. Remove all nodes defined in this file
    old_node_ids = [self._graph[i].symbol_id for i in self._file_to_nodes.get(path, [])]
    for node_id in old_node_ids:
        idx = self._id_to_index.pop(node_id)
        self._graph.remove_node(idx)

    # 2. Remove all edges originating from this file
    for edge_idx in self._file_to_edges.get(path, []):
        self._graph.remove_edge_from_index(edge_idx)

    # 3. Re-index the file (same as initial construction)
    self._index_file(path)

    # 4. Edges from OTHER files pointing to symbols that were
    #    removed and re-added get reconnected via symbol_id
    #    (new node gets a new index, but same symbol_id)
    self._reconnect_incoming_edges(old_node_ids)
```

### Reconnection strategy

When a symbol is removed and re-added (e.g., `models.py::User` after editing `models.py`), it gets a new RustworkX node index but the same `symbol_id`. Incoming edges from other files (e.g., `app.py::main -> models.py::User`) still reference the old index.

Two approaches:

**A. Rebuild incoming edges.** For each re-added symbol, scan all edges in the graph for references to the old index and update them. O(edges) per updated file — potentially slow.

**B. Indirection through symbol_id.** Store `symbol_id` on edges rather than relying on the node index for identity. When querying, resolve `symbol_id -> current index` via `_id_to_index`. This makes edge targets stable across node index changes, at the cost of an extra dict lookup per edge traversal.

**Recommendation:** Start with full rebuild for changed files (remove all nodes/edges for the file, re-index). For v1, the full-file rebuild is fast enough — a single file's portion of the graph is small. Optimize to incremental reconnection only if profiling shows it's needed.

---

## Memory Profile

Back-of-envelope estimates for typical project sizes:

| Project size | Symbols (nodes) | References (edges) | Graph memory | Notes |
|---|---|---|---|---|
| Small (5k lines) | ~200 | ~2,000 | ~1 MB | Trivial |
| Medium (50k lines) | ~2,000 | ~50,000 | ~10 MB | Comfortable |
| Large (200k lines) | ~8,000 | ~200,000 | ~40 MB | Fine |
| Very large (500k+) | ~20,000+ | ~500,000+ | ~100 MB+ | Consider lazy loading |

RustworkX stores the graph topology (adjacency lists) in Rust memory — compact and cache-friendly. Node/edge payloads are Python objects stored as `PyObject` pointers. Using `frozen=True` on Pydantic models and `frozen=True, slots=True` on dataclasses keeps payloads lean.

The secondary indexes (`_id_to_index`, `_file_to_nodes`, `_file_to_edges`) add ~1-2 MB of dict overhead for a 50k-line project. This is negligible.

---

## New TyO3 APIs Required

### Must have (graph cannot be built without these)

**1. `semantic_tokens(path)` — Rust + Python wiring**

ty_ide already exports `semantic_tokens`. Needs:
- Rust: DTO conversion in `convert/tokens.rs`, new method on `PyTyProject`
- Python: bridge method on `RustProject`, session method on `TyO3Session`

Returns all classified tokens in a file — positions, token types (function, class, variable, parameter, keyword, etc.), and modifiers (definition, readonly, async). This is the backbone for identifying which tokens are name references that need occurrence resolution.

**2. `type_hierarchy(path, line, col)` — Rust + Python wiring**

ty_ide exports `prepare_type_hierarchy`, `type_hierarchy_supertypes`, `type_hierarchy_subtypes` as three separate functions. TyO3 should compose them into a single `type_hierarchy()` call that returns:

```python
class TypeHierarchyItem(BaseModel):
    name: str
    kind: SymbolKind
    location: FileRange
    supertypes: list[TypeHierarchyItem]
    subtypes: list[TypeHierarchyItem]
```

Needed for INHERITS and OVERRIDES edges.

### Should have (significant performance improvement)

**3. `file_occurrences(path)` — batch occurrence API**

A Rust function that returns all resolved name occurrences for a file in one call. Each occurrence includes the token's position, the resolved target symbol's qualified name and file, and the reference role (read/write/import/definition).

```python
class Occurrence(BaseModel):
    range: Range                    # where the name appears
    target_file: str                # where the referenced symbol is defined
    target_qualified_name: str      # qualified name of the referenced symbol
    role: ReferenceRole             # read, write, import, definition
```

This is the most impactful new API. Without it, graph construction falls back to Strategy A (semantic tokens + per-token goto_definition calls). With it, a single Rust call per file populates all REFERENCES edges.

Implementation approach: start without this API (use Strategy A). Build it when profiling shows the per-token cursor approach is the bottleneck.

### Nice to have (enriches the graph)

**4. `all_symbols(query, importing_from)` — broader symbol search**

ty_ide exports `all_symbols`. Useful for building a more complete symbol table including importable symbols from the environment. Lower priority than the above.

---

## Implementation Phases

### Phase 1: Foundation

- Add `rustworkx` dependency
- Define `SymbolNode`, `EdgeData`, `EdgeKind`, `ReferenceRole` types
- Build `CodeGraph` class with `_graph`, secondary indexes, and basic query methods
- Populate graph from existing APIs: `document_symbols()` for nodes, `find_references()` per symbol for edges
- Add `check_file()` integration for diagnostics

This produces a working but slow graph — O(symbols) cursor calls to populate edges. Validates the data model and query patterns before investing in batch APIs.

### Phase 2: Semantic tokens

- Wire `semantic_tokens()` end-to-end (Rust DTO + Python bridge)
- Use semantic tokens to identify all name tokens in a file
- Resolve each token with `goto_definition()` — still O(tokens) cursor calls, but semantic tokens provide the positions for free
- Populate REFERENCES edges from resolved tokens

This replaces the Phase 1 approach (per-symbol `find_references`) with a more complete per-file approach (every name token resolved). Coverage improves — catches references that `find_references` might miss.

### Phase 3: Type hierarchy and relationships

- Wire `type_hierarchy()` end-to-end (Rust composition of three ty_ide calls)
- Populate INHERITS edges from type hierarchy results
- Derive OVERRIDES edges from inheritance + method name matching
- Add IMPORTS edge resolution from import occurrences in semantic tokens

### Phase 4: External symbols and caching

- Create stub nodes for external symbol references
- Build `DependencyGraph` class
- Implement disk caching for dependency graphs
- Add lazy resolution (`resolve_external()`)
- Pre-build stdlib graph

### Phase 5: Batch occurrence API

- Profile Phase 2 performance on target project sizes
- If needed: build `file_occurrences()` Rust batch walker using `ty_python_semantic`
- Replace per-token cursor approach with single batch call per file
- This is the performance ceiling — one Rust call per file, all occurrences resolved in Rust

### Phase 6: Advanced queries and incremental updates

- Implement incremental `update_file()` for live editing workflows
- Add graph algorithm queries (import cycles, coupling metrics, centrality)
- Add subgraph extraction for focused analysis
- Add visualization export (DOT, JSON graph format)

---

## Open Design Areas

### Graph serialization and persistence

The graph should be cacheable to disk for fast reload. Options:

- **Pickle.** Fast, but fragile across Python/library versions. Fine for local cache, not for distribution.
- **JSON.** Portable, inspectable. Pydantic nodes serialize naturally. Edge dataclasses need a custom encoder. Graph topology needs separate representation (node list + edge list).
- **Custom binary format.** Compact, fast. More implementation work. Consider only if JSON is too slow/large.

Recommendation: JSON for the node/edge payload data, with the graph topology stored as an adjacency list. RustworkX doesn't have built-in serialization, so you'd export to `(nodes: list, edges: list[tuple[src, dst, data]])` and reconstruct.

### Parallel construction

File indexing is embarrassingly parallel — each file's symbols and occurrences can be computed independently. Options:

- **Sequential** (v1): simple, correct, sufficient for small-medium projects.
- **ThreadPoolExecutor**: ty's `ProjectDatabase` is behind a `Mutex`, so true parallel Rust calls serialize on the lock. But Python-side graph assembly can be parallelized.
- **Process-level parallelism**: each worker opens its own `TyO3Session`. Maximum parallelism but higher memory cost (each worker holds a `ProjectDatabase`).

Recommendation: Sequential for v1. Profile before parallelizing — Salsa caching means file N+1 is often faster than file N because shared type information is already computed.

### SCIP export (optional downstream)

If SCIP compatibility is ever needed, the code graph contains all the data required to emit a SCIP `Index`:

- `SymbolNode` -> SCIP `SymbolInformation` (map `symbol_id` to SCIP symbol string format)
- REFERENCES edges -> SCIP `Occurrence` records (with `SymbolRole` from `ReferenceRole`)
- INHERITS edges -> SCIP `Relationship` with `is_implementation = true`
- Diagnostics -> SCIP `Diagnostic` on occurrences
- `SemanticTokenType` -> SCIP `SyntaxKind`

This would be a thin mapping layer — a function that walks the `CodeGraph` and emits protobuf. It does not need to be built until there's a concrete Sourcegraph integration requirement.

---

## Example Usage

```python
from tyo3 import TyO3Session
from tyo3.graph import CodeGraph

# Build the graph
with TyO3Session("/path/to/project") as session:
    graph = CodeGraph.build(session)

# Basic queries
user = graph.symbol("src/models.py::User")
print(f"{user.name}: {user.kind}, {user.documentation}")

# Find all references
refs = graph.references_to("src/models.py::User")
print(f"User is referenced {len(refs)} times")

# Inheritance
parents = graph.superclasses("src/models.py::User")
children = graph.subclasses("src/models.py::User")

# Dependency analysis
deps = graph.transitive_dependencies("src/app.py::main")
print(f"main() transitively depends on {len(deps)} symbols")

# Impact analysis
impact = graph.transitive_dependents("src/models.py::User.save")
print(f"Changing save() could affect {len(impact)} symbols")

# Structural analysis
cycles = graph.import_cycles()
if cycles:
    print(f"Found {len(cycles)} circular import chains")

coupling = graph.coupling_between("src/models.py", "src/app.py")
print(f"Coupling between models and app: {coupling} references")

# Access the underlying RustworkX graph for advanced analysis
centrality = rx.betweenness_centrality(graph.graph)
hub_symbols = sorted(centrality.items(), key=lambda x: x[1], reverse=True)[:10]
```
