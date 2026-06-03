# TyO3 Graph Review V2

Date: 2026-06-03

Scope: this review focuses on the TyO3 library as a whole, with a deeper emphasis on the `CodeGraph` subsystem because that is where most of the semantic-correctness risk currently lives.

## Executive Summary

TyO3 is a Python semantic-intelligence library backed by Astral's `ty` and Ruff internals. It exposes a Python API for project file discovery, diagnostics, symbols, navigation, references, hover information, semantic tokens, type hierarchy, and graph-based code analysis.

The architecture is promising and already has a serious amount of test coverage. The basic Rust-to-Python bridge is deliberate, and the Python API is coherent. The major concern is that the graph layer currently looks more reliable than it is. It can pass existing tests while still producing incomplete or misleading dependency, inheritance, and impact-analysis results.

The most important issues are:

1. Override edges are currently dead code due to unreachable indentation.
2. `CodeGraph.build()` indexes files sequentially and can turn in-project cross-file references into permanent external stubs.
3. `update_file()` removes incoming references but does not rebuild them.
4. Rust coordinate validation can accept columns beyond the current line.
5. Public error mapping is inconsistent across similar API methods.
6. Static and CI quality gates are not aligned with the documented development workflow.

The practical recommendation is to treat the session API as usable, but to treat graph outputs as provisional until the graph construction and invalidation model is fixed.

## What TyO3 Is Trying To Accomplish

TyO3 is not just another Python AST parser. Its goal is to expose semantic code intelligence for Python projects by wrapping `ty`, the fast Python type checker from Astral, through a PyO3 extension.

At a high level, TyO3 wants to answer questions like:

- What files belong to this Python project?
- What type-checking diagnostics exist?
- What symbols are defined in this file or workspace?
- Where is this symbol defined?
- Where is this symbol referenced?
- What hover/type information is available at this cursor position?
- What classes inherit from this class?
- What symbols, files, and modules depend on each other?
- What parts of the codebase become risky if one symbol changes?

That is a meaningful and useful product surface. It is especially valuable for developer tools, refactoring assistants, code-review automation, impact analysis, and agent workflows.

## High-Level Architecture

There are three important layers.

### 1. Rust Native Backend

The Rust side lives under `rust/src`.

Important files:

- `rust/src/project.rs`
- `rust/src/files.rs`
- `rust/src/coordinates.rs`
- `rust/src/convert/*`
- `rust/src/dto/*`

The Rust backend:

- Opens a `ty_project::ProjectDatabase`.
- Resolves files to Ruff `File` handles.
- Calls `ty_ide` APIs for symbols, navigation, hover, references, type hierarchy, semantic tokens, and occurrences.
- Converts ty/Ruff data into PyO3 DTO objects.
- Defines native exception classes so Python can catch structured Rust errors.

### 2. Python Validation Wrapper

The Python wrapper lives mostly in:

- `src/tyo3/rust_project.py`
- `src/tyo3/session.py`
- `src/tyo3/models/*`

`RustProject` wraps the native `TyProject`, converts PyO3 DTOs into plain Python dictionaries/lists/strings, then validates them into Pydantic models.

`TyO3Session` is the public user-facing API. It mostly delegates to `RustProject`, while adding simple position prevalidation for some methods.

### 3. Code Graph Layer

The graph layer lives in:

- `src/tyo3/graph/graph.py`
- `src/tyo3/graph/models.py`
- `src/tyo3/graph/identity.py`
- `src/tyo3/graph/dependency.py`
- `src/tyo3/graph/export.py`

This layer builds a `rustworkx.PyDiGraph` where:

- Nodes are symbols, modules, or external stubs.
- Edges represent relationships such as `DEFINES`, `CONTAINS`, `REFERENCES`, `INHERITS`, and `OVERRIDES`.
- Secondary indexes map symbol IDs and files back to rustworkx node indexes.
- Diagnostics are stored separately, keyed by file.

This graph layer is where the library moves from "semantic wrapper" to "code intelligence platform." It is also where the most severe correctness issues currently are.

## Mental Model For The Graph

A simplified project:

```python
# models.py
class User:
    def save(self) -> None:
        pass

# app.py
from models import User

def run() -> None:
    user = User()
    user.save()
```

A good graph should contain nodes like:

- `models.py::<module>`
- `models.py::User`
- `models.py::User.save`
- `app.py::<module>`
- `app.py::run`

It should contain structural edges like:

- `models.py::<module>` defines `models.py::User`
- `models.py::User` contains `models.py::User.save`
- `app.py::<module>` defines `app.py::run`

It should contain reference edges like:

- `app.py::run` references `models.py::User`
- `app.py::run` references `models.py::User.save`

Downstream graph features depend on those edges being correct. If references are missing, duplicated, pointed at stubs, or mixed with containment edges, then queries like "what depends on this symbol?" become unreliable.

## Verification Performed

Commands run during review:

```bash
pytest -q
```

Result: failed outside devenv because `pydantic` was missing from the ambient shell.

```bash
devenv shell -- pytest -q
```

Result: passed. The test suite completed and reported 87% total Python coverage.

```bash
devenv shell -- ruff check .
```

Result: failed with 29 diagnostics. These include import-order issues, unused imports, blind exception assertions, unused local variables, and `zip()` without `strict=`.

```bash
devenv shell -- ty check src
```

Result: failed with 73 diagnostics. Many are in tests or around native-extension typing, but it still means the static type gate is not clean.

```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml
```

Result: failed at link time due to missing Python C-API symbols such as `PyList_New`, `_Py_Dealloc`, and `PyGILState_Ensure`.

```bash
devenv shell -- cargo test --manifest-path rust/tyo3-derive/Cargo.toml
```

Result: failed with the same class of Python C-API link errors.

```bash
devenv shell -- cargo build --manifest-path rust/Cargo.toml --release
```

Result: did not complete within the review window after several minutes of no output. Treat as inconclusive for this review.

Important interpretation: passing Python tests are valuable, but they do not prove graph semantic correctness. Several current tests are no-crash smoke tests rather than behavior tests.

## Review Findings

### Finding 1: `OVERRIDES` Edges Are Dead Code

Severity: high

Location:

- `src/tyo3/graph/graph.py:563`
- `src/tyo3/graph/graph.py:566`

Relevant code shape:

```python
for _src, succ_idx, edge_data in self._graph.out_edges(self._id_to_index[current_sid]):
    if edge_data.kind != EdgeKind.INHERITS:
        continue
        parent_sid = self._graph[succ_idx].symbol_id
        ...
```

The `parent_sid = ...` block is indented under an `if` branch that immediately executes `continue`.

That means:

- If the edge is not `INHERITS`, the loop continues before collecting anything.
- If the edge is `INHERITS`, the `if` body is skipped entirely.
- Therefore the ancestor traversal never runs.
- Therefore `ancestor_methods` remains empty.
- Therefore `OVERRIDES` edges are never created.

Why this matters:

`OVERRIDES` edges are important for impact analysis. If a parent class method changes, we often want to know which subclass methods override it. This is a common refactoring and review question.

Concrete example:

```python
class Base:
    def save(self) -> None:
        pass

class User(Base):
    def save(self) -> None:
        pass
```

Expected graph relationship:

- `User.save` overrides `Base.save`

Current behavior:

- No override edge is produced.

Suggested fix:

Move the parent traversal outside the `if` branch.

```python
for _src, succ_idx, edge_data in self._graph.out_edges(self._id_to_index[current_sid]):
    if edge_data.kind != EdgeKind.INHERITS:
        continue

    parent_sid = self._graph[succ_idx].symbol_id
    if parent_sid not in visited:
        visited.add(parent_sid)
        queue.append(parent_sid)

    for c in self.children(parent_sid):
        if c.kind in METHOD_KINDS and c.name not in ancestor_methods:
            ancestor_methods[c.name] = c.symbol_id
```

Required test:

Build a fixture with a base class and subclass overriding a method. Assert that `references_from` or direct edge inspection finds an `EdgeKind.OVERRIDES` edge from the child method to the parent method.

### Finding 2: Sequential Graph Build Can Turn Local Symbols Into External Stubs

Severity: high

Location:

- `src/tyo3/graph/graph.py:288`
- `src/tyo3/graph/graph.py:333`

Current build flow:

1. `CodeGraph.build(session)` loops over files.
2. `_index_file()` adds symbols for one file.
3. `_index_file()` immediately resolves references for that file.
4. If a reference points to a file that has not been indexed yet, `_file_to_nodes` does not contain that target file.
5. `_ensure_target_node_simple()` treats the target as external and creates a stub.

Problematic code:

```python
if target_file in self._file_to_nodes:
    return None

package = self._infer_package(target_file)
ext_sid = f"{package}::{target_name}" if package else target_sid
self._add_stub_node(...)
```

This logic confuses "not indexed yet" with "external."

Why this matters:

Suppose the project has two files:

```python
# app.py
from models import User

def run() -> User:
    return User()
```

```python
# models.py
class User:
    pass
```

If `app.py` is indexed before `models.py`, then `User` may become an external stub. Later, when `models.py` is indexed, the real `models.py::User` node may not be created correctly because the symbol ID already exists. The graph can end up with:

- An external stub for a local symbol.
- Missing containment edges for the real symbol.
- Incorrect dependency queries.
- Incorrect external-symbol reports.

Suggested fix:

Use a two-pass graph build.

Pass 1:

- Enumerate all files.
- Add module nodes.
- Add all document symbol nodes.
- Build containment edges.
- Record the complete set of project file paths.

Pass 2:

- Resolve references.
- Resolve inheritance.
- Add diagnostics.

This guarantees that when resolving a target, the graph can distinguish:

- Target is a known project file and known symbol.
- Target is a known project file but unresolved symbol.
- Target is genuinely external.

Suggested data structure:

```python
self._project_files: set[str] = {str(path) for path in session.files()}
```

Then use `_project_files`, not `_file_to_nodes`, to decide externality.

Required test:

Create a fixture where file ordering makes a user of a symbol appear before the provider file. Assert that the target node is not external and belongs to the provider file.

### Finding 3: `update_file()` Drops Incoming References

Severity: high

Location:

- `src/tyo3/graph/graph.py:1075`
- `src/tyo3/graph/graph.py:1085`

Current behavior:

```python
old_node_indices = list(self._file_to_nodes.get(path, []))
if old_node_indices:
    self._graph.remove_nodes_from(old_node_indices)
    self._rebuild_indexes()
self._diagnostics.pop(path, None)
self._index_file(session, path)
```

The docstring says incoming edges from other files are "naturally recreated." That is not true. When nodes for `path` are removed, rustworkx removes incident edges. That includes references from other files into symbols defined in `path`.

Then `_index_file(session, path)` only re-indexes the changed file. It does not re-index the files that used to reference it.

Concrete example:

```python
# models.py
class User:
    pass
```

```python
# app.py
from models import User

def run() -> User:
    return User()
```

If `models.py` is updated:

1. `models.py::User` is removed.
2. Reference edges from `app.py::run` to `models.py::User` are removed.
3. `models.py` is re-indexed.
4. `app.py` is not re-indexed.
5. References from `app.py` are not restored.

Suggested fixes:

Option A: simple and correct

- Rebuild the entire graph for now.
- This is safest until there is a robust invalidation model.

Option B: targeted invalidation

- Track reverse file dependencies.
- When file `X` is updated, re-index `X` and every file that references symbols in `X`.

Option C: split node/edge invalidation

- Keep nodes.
- Remove only edges associated with the changed file and affected referring files.
- Re-resolve references for affected files.

Required test:

1. Build a graph for two files where `app.py` references `models.py::User`.
2. Assert the reference exists.
3. Call `update_file(session, models_path)`.
4. Assert the reference still exists.

Current tests only verify that symbols are present after update. They do not verify cross-file reference preservation.

### Finding 4: Column Validation Can Accept Positions Beyond The Current Line

Severity: high

Location:

- `rust/src/coordinates.rs:51`
- `rust/src/coordinates.rs:56`

Current code:

```rust
let line_start_usize: usize = line_start.to_usize();
let line_text = &source[line_start_usize..];
let byte_col = char_len_to_byte_offset(line_text, col);

if usize::from(byte_col) > line_text.len() {
    return Err(...);
}
```

`line_text` is not the current line. It is the entire remainder of the file from the start of the current line to EOF.

Why this matters:

If a user asks for line 1, column 500 in a file where line 1 is only 20 characters long but the file has more than 500 characters total, this validation can accept the position. The computed byte offset may land on a later line.

That violates the public API contract that positions are line/column coordinates.

Suggested fix:

Slice only the requested line.

Pseudocode:

```rust
let line_start = ...;
let line_end = if line_idx + 1 < total_lines {
    line_index.line_start(OneIndexed::from_zero_indexed(line_idx + 1), source)
} else {
    TextSize::try_from(source.len()).unwrap()
};

let line_text = &source[line_start.to_usize()..line_end.to_usize()];
let byte_col = char_len_to_byte_offset(line_text, col);

if byte_col.to_usize() > line_text.trim_end_matches(['\n', '\r']).len() {
    return Err(...);
}
```

Be careful about whether columns at line endings are allowed. Decide and test the policy explicitly.

Required tests:

- Valid line, column past line end should raise `PositionError`.
- Valid line, column at last character should work.
- Valid line, column at end-of-line should behave according to documented policy.
- Unicode identifiers should still work.

### Finding 5: Public Exception Mapping Is Inconsistent

Severity: medium

Location:

- `src/tyo3/rust_project.py:367`
- `src/tyo3/rust_project.py:430`

`find_references()` catches:

```python
except _NativeClosedError
except _NativePositionError
except Exception
```

It does not catch `_NativePathError` or `OverflowError`.

`hover()` catches:

```python
except _NativeClosedError
except _NativePositionError
except Exception
```

It also does not catch `_NativePathError` or `OverflowError`.

Why this matters:

The README says public exceptions include:

- `PathResolutionError`
- `PositionError`
- `AnalysisError`
- `InternalTyError`

Users should be able to rely on bad file paths becoming `PathResolutionError` and bad positions becoming `PositionError`. If some methods wrap those in `InternalTyError`, callers must special-case methods, which weakens the API.

Suggested fix:

Apply a consistent exception mapping policy to every method.

For every method that resolves a file path:

```python
except _NativePathError as e:
    raise PathResolutionError(str(e)) from e
```

For every method that accepts line/column:

```python
except _NativePositionError as e:
    raise PositionError(str(e)) from e
except OverflowError as e:
    raise PositionError(str(e)) from e
```

Required tests:

For each public method:

- Closed project raises `ProjectClosedError`.
- Invalid path raises `PathResolutionError`.
- Zero position raises `PositionError`.
- Negative position raises `PositionError`.
- Too-large line raises `PositionError`.
- Too-large column raises `PositionError`.

### Finding 6: File Resolution Does Not Explicitly Enforce Root Containment

Severity: medium

Location:

- `rust/src/files.rs:20`
- `rust/src/files.rs:29`

Current code canonicalizes the input path and then calls Ruff's `system_path_to_file`.

```rust
let canonical = absolute.canonicalize()?;
let system_path = SystemPath::from_std_path(&canonical)?;
files::system_path_to_file(db, system_path)
```

There is no explicit:

```rust
if !canonical.starts_with(root) {
    return Err(...)
}
```

Why this matters:

The public API describes paths as project-relative or within-project paths. A semantic engine should not accidentally expose analysis for arbitrary files outside the project root. Even if Ruff currently rejects these, TyO3 should enforce its own contract.

Suggested fix:

Add a root containment check after canonicalization.

```rust
if !canonical.starts_with(root) {
    return Err(format!(
        "Path '{}' is outside project root '{}'",
        path_str,
        root.display()
    ));
}
```

Required tests:

- `../outside.py` raises `PathResolutionError`.
- Absolute path outside root raises `PathResolutionError`.
- Symlink escaping the project root is rejected after canonicalization.

### Finding 7: `dependencies()` Mixes Structural Edges With Semantic Dependencies

Severity: medium

Location:

- `src/tyo3/graph/graph.py:810`
- `src/tyo3/graph/graph.py:815`

Current code:

```python
return {
    self._graph[succ].symbol_id
    for succ in self._graph.neighbors(idx)
}
```

This returns all outgoing neighbors regardless of edge kind.

Why this matters:

Not every outgoing edge means "depends on." For example:

- A module `DEFINES` a function.
- A class `CONTAINS` a method.
- A function `REFERENCES` another function.
- A class `INHERITS` from another class.

Only some of those should be considered semantic dependencies. If containment edges are counted as dependencies, then topological ordering, centrality, impact analysis, and coupling become harder to interpret.

Suggested fix:

Define explicit dependency edge kinds:

```python
DEPENDENCY_KINDS = {
    EdgeKind.REFERENCES,
    EdgeKind.IMPORTS,
    EdgeKind.INHERITS,
    EdgeKind.OVERRIDES,
    EdgeKind.TYPE_OF,
    EdgeKind.RETURNS,
    EdgeKind.INSTANTIATES,
}
```

Then use `_edges_of_kind()`.

```python
return {
    self._graph[tgt_idx].symbol_id
    for tgt_idx, _data in self._edges_of_kind(symbol_id, DEPENDENCY_KINDS)
}
```

Also consider whether `OVERRIDES` should be treated as child-to-parent dependency, parent-to-child impact relationship, or both through separate query methods.

### Finding 8: Graph Construction Silently Degrades

Severity: medium

Location:

- `src/tyo3/graph/graph.py:72`
- `src/tyo3/graph/graph.py:138`

Current behavior:

```python
try:
    symbols = session.document_symbols(file_str)
except Exception:
    logger.warning("Failed to get symbols for %s, skipping", file_str)
    return
```

```python
try:
    result = session.check_file(file_str)
except Exception:
    logger.warning("Failed to check %s, skipping diagnostics", file_str)
```

Why this matters:

Silent degradation is dangerous for graph analysis. A caller can receive a graph that looks valid but is missing entire files, references, inheritance edges, or diagnostics.

Suggested fix:

Add a build report object.

Example:

```python
class GraphBuildReport(BaseModel):
    indexed_files: list[str]
    skipped_files: list[GraphBuildFailure]
    diagnostic_failures: list[GraphBuildFailure]
    reference_failures: list[GraphBuildFailure]
```

Expose either:

```python
graph = CodeGraph.build(session, strict=True)
```

or:

```python
graph, report = CodeGraph.build_with_report(session)
```

Strict mode should raise if a file cannot be indexed.

### Finding 9: Dependency Cache Does Not Round-Trip Edge Roles Correctly

Severity: medium

Location:

- `src/tyo3/graph/dependency.py:89`
- `src/tyo3/graph/dependency.py:135`
- `src/tyo3/graph/export.py:146`

Save writes:

```python
edge_data_dict["role"] = raw.role.value
```

Load reads:

```python
role=raw_edge.get("role")
```

But `EdgeData.role` is expected to be a `ReferenceRole`, not a string. Export later does:

```python
d["role"] = edge.role.value
```

If the edge was loaded from cache, `edge.role` may be a plain string, and `.value` will fail.

Suggested fix:

```python
from tyo3.models.navigation import ReferenceRole

role_raw = raw_edge.get("role")
role = ReferenceRole(role_raw) if role_raw is not None else None

edge_obj = EdgeData(
    kind=EdgeKind(raw_edge["kind"]),
    file=raw_edge.get("file"),
    role=role,
)
```

Required test:

1. Build a dependency graph with an edge containing `ReferenceRole.READ`.
2. Save it.
3. Load it.
4. Export to JSON.
5. Assert no exception and role equals `"read"`.

### Finding 10: CI Does Not Match The Documented Development Workflow

Severity: medium

Location:

- `.github/workflows/ci.yml:58`
- `.github/workflows/ci.yml:71`

The workflow installs devenv but then runs:

```yaml
cd rust
cargo build --release
cp target/release/lib_native_impl.so ../src/tyo3/_native_impl.cpython-313-x86_64-linux-gnu.so
```

and:

```yaml
uv pip install hypothesis
python -m pytest ...
```

Problems:

- It does not run through `devenv shell -- build` or `maturin develop`.
- It hardcodes a Linux CPython 3.13 extension filename.
- It only installs `hypothesis`, not all runtime/dev dependencies.
- It does not run `ruff check`.
- It does not run `ty check`.
- It does not catch the static-check failures found in review.

Suggested fix:

Use the same commands developers are expected to use:

```yaml
- name: Build extension
  run: devenv shell -- build-release

- name: Run tests
  run: devenv shell -- pytest -q

- name: Run lint
  run: devenv shell -- ruff check .
```

For `ty check`, either:

- Add it after cleaning up current diagnostics.
- Or document why it is not yet a required gate.

### Finding 11: Tests Include No-Op Assertions

Severity: low

Location:

- `src/tyo3/tests/test_coordinate_conversion.py:111`
- `src/tyo3/tests/test_coordinate_conversion.py:122`

Example:

```python
assert hover is None or hover is not None
```

This assertion is always true.

Why this matters:

No-op assertions make test output look stronger than it is. They also train readers to trust coverage numbers more than behavior.

Better options:

If the test is only checking no crash, write:

```python
self.rp.hover("main.py", 1, 1)
```

and name the test `test_hover_at_start_of_file_does_not_raise`.

If the test is checking behavior, assert concrete behavior:

```python
hover = self.rp.hover("main.py", 3, 5)
assert hover is not None
assert hover.contents
```

### Finding 12: Mutable Defaults Should Be Replaced With `Field(default_factory=list)`

Severity: low

Location:

- `src/tyo3/models/navigation.py:129`
- `src/tyo3/models/navigation.py:130`

Current code:

```python
supertypes: list[TypeHierarchyItem] = []
subtypes: list[TypeHierarchyItem] = []
```

Pydantic v2 generally handles mutable defaults more safely than plain dataclasses, but the style still creates confusion and is easy to copy into places where it is unsafe.

Suggested fix:

```python
from pydantic import Field

supertypes: list[TypeHierarchyItem] = Field(default_factory=list)
subtypes: list[TypeHierarchyItem] = Field(default_factory=list)
```

This is a small hygiene issue, but it is a good habit.

## Positive Notes

The review found several good decisions worth preserving.

### The Public API Is Coherent

`TyO3Session` is a good top-level abstraction. Users do not need to know about PyO3 DTOs, Rust databases, or conversion details. The API shape is understandable:

```python
with TyO3Session("/path/to/project") as session:
    result = session.check()
    symbols = session.document_symbols("src/main.py")
    targets = session.goto_definition("src/main.py", 10, 5)
```

### DTO Conversion Is More Robust Than Naive PyO3 Reflection

The custom `PyFields` derive macro exposes `__fields__` metadata for native DTO structs. Python then converts objects recursively based on field tags. This avoids fragile `dir()` introspection for normal paths.

This is good engineering. The warning fallback is still useful, but the primary path is explicit.

### The Native Exception Types Are The Right Direction

The Rust module defines native exception classes:

- `ProjectClosedError`
- `PathResolutionError`
- `PositionError`
- `AnalysisError`

That allows Python to catch typed native exceptions instead of string-matching error messages. The remaining work is to apply the mapping consistently.

### Batch File Occurrences Are The Right Performance Direction

`file_occurrences()` avoids one FFI call per token. That matters for large files. The graph layer's move from token-by-token goto calls toward batch occurrence resolution is the right direction.

### There Is Meaningful Test Investment

The project has a broad test suite:

- model tests
- native bridge tests
- Rust integration tests
- graph construction tests
- graph query tests
- graph export tests
- property-based tests
- performance-oriented tests

The issue is not lack of tests. The issue is that several tests assert no-crash or broad shape rather than semantic correctness.

## Suggested Fix Order

This is the recommended sequence for an intern or new contributor.

### Step 1: Fix The Dead Override Traversal

Why first:

- Smallest high-impact bug.
- Easy to understand.
- Easy to test.

Deliverables:

- Fix indentation.
- Add override fixture.
- Assert `OVERRIDES` edge exists.

### Step 2: Introduce Two-Pass Graph Build

Why second:

- Fixes the forward-reference external-stub bug.
- Creates a stronger foundation for reference and dependency correctness.

Deliverables:

- Store project file set.
- First pass: add files and symbols.
- Second pass: add references, inheritance, diagnostics.
- Add test where target file is indexed after referencing file.

### Step 3: Fix `update_file()` Semantics

Why third:

- Incremental updates are currently misleading.
- This is central for long-lived sessions and editor-like use cases.

Deliverables:

- Decide rebuild strategy.
- Add test preserving incoming references after update.
- Update docstring to match actual behavior.

### Step 4: Fix Coordinate Validation

Why fourth:

- This is a public API correctness issue.
- It can cause wrong navigation/hover results instead of clean errors.

Deliverables:

- Validate columns against current line only.
- Add edge-case tests.

### Step 5: Normalize Error Mapping

Why fifth:

- Makes the Python API predictable.
- Easy to test method-by-method.

Deliverables:

- Add helper for native exception mapping if useful.
- Add tests for each public method.

### Step 6: Clean Static Gates

Why sixth:

- Prevents future drift.
- Makes CI meaningful.

Deliverables:

- Fix `ruff check .`.
- Decide whether `ty check src` is a required gate.
- Update GitHub Actions to match devenv workflow.

## Example: What A Better Graph Build Could Look Like

This is intentionally pseudocode. It shows the shape, not a drop-in patch.

```python
@classmethod
def build(cls, session: TyO3Session) -> CodeGraph:
    graph = cls()
    files = [str(path) for path in session.files()]
    graph._project_files = set(files)

    for file_str in files:
        graph._index_symbols_only(session, file_str)

    graph._rebuild_range_caches()

    for file_str in files:
        graph._resolve_references_via_occurrences(session, file_str)

    for file_str in files:
        graph._resolve_inheritance_for_file(session, file_str)

    for file_str in files:
        graph._collect_diagnostics(session, file_str)

    return graph
```

The key point is that all local nodes exist before any references are resolved.

## Example: What A Build Report Could Look Like

```python
class GraphBuildFailure(BaseModel):
    file: str
    phase: Literal["symbols", "references", "diagnostics", "inheritance"]
    error_type: str
    message: str

class GraphBuildReport(BaseModel):
    indexed_files: list[str] = Field(default_factory=list)
    failures: list[GraphBuildFailure] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.failures
```

This gives callers a way to distinguish:

- "The graph is complete."
- "The graph is usable but missing diagnostics for two files."
- "The graph skipped an entire file."

That distinction matters for tools and agents.

## Example: Stronger Tests For Reference Correctness

A weak test:

```python
refs = graph.references_to(user.symbol_id)
assert isinstance(refs, list)
```

A stronger test:

```python
refs = graph.references_to(user.symbol_id)
assert len(refs) == 2
assert {ref.file for ref in refs} == {app_path}
assert all(ref.kind == EdgeKind.REFERENCES for ref in refs)
```

A stronger graph-level test:

```python
run = find_node(graph, name="run", file=app_path)
user = find_node(graph, name="User", file=models_path)

outgoing = graph.references_from(run.symbol_id)
targets = {node.symbol_id for node, _edge in outgoing}

assert user.symbol_id in targets
assert not user.external
```

This proves that the reference points to the real local symbol rather than an external stub.

## Practical Guidance

When working on TyO3 graph code, keep these rules in mind:

1. Do not trust node existence alone. A stub node can have the same symbol ID shape as a real node.
2. Always ask whether an edge is structural or semantic.
3. Avoid resolving references before all local symbols are known.
4. Treat silent `except Exception` paths as graph-completeness hazards.
5. For tests, assert exact semantic relationships where possible.
6. If a graph query will be used for impact analysis, false negatives are as dangerous as false positives.
7. Keep public API exceptions predictable across methods.
8. Prefer build reports over warnings when partial output is returned.

## Final Assessment

TyO3 has a solid foundation and a useful product direction. The session API and DTO bridge are viable. The graph layer, however, needs a correctness pass before it should be trusted for dependency analysis, impact analysis, inheritance analysis, or refactoring support.

The highest leverage work is to make graph construction deterministic and complete:

- Build local symbols first.
- Resolve references second.
- Handle incremental invalidation honestly.
- Add precise semantic tests.

Once those are fixed, the graph layer can become the most valuable part of the library.
