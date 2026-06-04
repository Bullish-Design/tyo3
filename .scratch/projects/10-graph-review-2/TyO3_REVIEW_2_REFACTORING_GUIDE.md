# TyO3 Review 2 Refactoring Guide

Date: 2026-06-03 (revised)

Audience: new TyO3 contributors, including interns who are comfortable with
Python but new to Rust and PyO3.

Goal: make TyO3's graph and native position behavior correct enough to trust
for code intelligence, dependency analysis, and future refactoring tools.

This guide is intentionally step-by-step. Follow the phases in order. Do not
start by doing broad architectural cleanup. The current implementation has a
good foundation; the main problem is that several graph and coordinate
invariants are not yet true.

## Current State Summary

The top-level `TyO3Session` and Rust-backed `RustProject` are the right public
shape. The graph layer, however, currently produces misleading semantic answers
in normal fixtures.

The highest priority issues are:

1. References are resolved before the range cache exists, so references inside
   functions and methods attach to module nodes.
2. Cross-file references are file-order-dependent because "not indexed yet" is
   confused with "external".
3. `update_file()` removes incoming references and does not rebuild the files
   that contained those references.
4. `OVERRIDES` edges are dead code due to unreachable indentation.
5. Rust coordinate validation accepts columns beyond the current line.
6. Dependency APIs use the whole heterogeneous graph, so structural edges count
   as dependencies.
7. Public exception mapping and native panic boundaries need hardening.
8. Tests need exact semantic assertions, not just no-crash and shape checks.
9. Graph diagnostics collection calls `check_file()` per file, each triggering
   a full project check internally — an O(N) FFI problem where O(1) suffices.
10. The range-size sort key used by `_find_enclosing_symbol` is incorrect for
    multi-line ranges, producing wrong enclosing-symbol lookups.

The target state after this guide:

- `CodeGraph.build()` is deterministic and multi-pass.
- References attach to the innermost enclosing symbol, not the module fallback.
- Project-local targets are never externalized because of file ordering.
- `OVERRIDES` edges exist for simple inheritance.
- `update_file()` is correct for its documented scope.
- Coordinates past the current line raise `PositionError`.
- Dependency queries filter dependency edge kinds explicitly.
- Diagnostics are collected with a single `check()` call.
- CI/static checks are meaningful enough to prevent regressions.

## Ground Rules

Use small commits or small PRs by phase. Each phase has its own tests and
validation. Do not combine the graph rebuild, Rust coordinate fix, DTO redesign,
and CI cleanup into one huge change.

When editing:

- Keep existing public APIs unless a phase explicitly says otherwise.
- Prefer simple correct behavior over clever incremental behavior.
- Add regression tests before or alongside each fix.
- When a test fixture is small, assert exact nodes and edges.
- Do not silently swallow graph-build failures without surfacing them.
- Run the validation commands for the phase before moving on.

Recommended baseline commands:

```bash
devenv shell -- build
devenv shell -- tests
devenv shell -- ruff check .
```

At the time of this review, `ruff check .` is expected to fail. Do not treat
that as permission to ignore new failures. Each phase below defines the minimum
validation for that phase.

## Phase 0: Add Focused Regression Tests

Purpose: capture the bugs before changing the implementation. Some of these
tests will fail at first. That is expected.

### 0.1 Add a Graph Test Helpers Module

Create `src/tyo3/tests/graph_helpers.py` (not a test file — a shared helper
module). Keep test helpers separate from test cases so multiple test files can
import them without duplication.

```python
from tyo3.graph import CodeGraph, EdgeKind
from tyo3.graph.models import SymbolNode
from tyo3.models.symbols import SymbolKind


def find_one(
    graph: CodeGraph,
    *,
    file_suffix: str,
    name: str,
    kind: SymbolKind,
) -> SymbolNode:
    """Find exactly one non-external symbol matching the criteria."""
    matches = [
        node
        for node in graph.symbols_of_kind(kind)
        if node.file.endswith(file_suffix)
        and node.name == name
        and not node.external
    ]
    assert len(matches) == 1, (
        f"Expected exactly one {kind} named {name!r} in *{file_suffix}, "
        f"got {[m.symbol_id for m in matches]}"
    )
    return matches[0]


def edges_of_kind(
    graph: CodeGraph, kind: EdgeKind
) -> list[tuple[str, str]]:
    """Return all (source_id, target_id) pairs for edges of a given kind."""
    result: list[tuple[str, str]] = []
    for edge_idx in graph.graph.edge_indices():
        data = graph.graph.get_edge_data_by_index(edge_idx)
        if data.kind != kind:
            continue
        src, tgt = graph.graph.get_edge_endpoints_by_index(edge_idx)
        result.append((graph.graph[src].symbol_id, graph.graph[tgt].symbol_id))
    return result
```

Explanation:

- `find_one()` avoids weak checks like `len(nodes) > 0`.
- `edges_of_kind()` lets tests assert exact graph relationships.
- Checking `file.endswith(...)` keeps tests working while paths are still
  absolute. After Phase 7 (path normalization), update these to exact relative
  paths.

### 0.2 Test References Attach to Functions, Not Modules

Create `src/tyo3/tests/test_graph_semantics.py`:

```python
from tyo3.graph import EdgeKind
from tyo3.models.symbols import SymbolKind
from tyo3.tests.conftest import get_graph, needs_native
from tyo3.tests.graph_helpers import find_one


@needs_native
def test_reference_from_create_user_targets_user_class() -> None:
    graph = get_graph("graph_test")

    create_user = find_one(
        graph,
        file_suffix="app.py",
        name="create_user",
        kind=SymbolKind.FUNCTION,
    )
    user = find_one(
        graph,
        file_suffix="models.py",
        name="User",
        kind=SymbolKind.CLASS,
    )

    targets = {
        target.symbol_id
        for target, edge in graph.references_from(create_user.symbol_id)
        if edge.kind == EdgeKind.REFERENCES
    }

    assert user.symbol_id in targets


@needs_native
def test_reference_does_not_attach_to_app_module() -> None:
    graph = get_graph("graph_test")

    app_modules = [
        node for node in graph.symbols_of_kind(SymbolKind.MODULE)
        if node.file.endswith("app.py")
    ]
    assert len(app_modules) == 1

    app_module_refs = graph.references_from(app_modules[0].symbol_id)
    local_model_targets = [
        target for target, _edge in app_module_refs
        if target.file.endswith("models.py") and not target.external
    ]
    assert local_model_targets == []
```

These fail today because references from `app.py` attach to
`app.py::<module>`.

### 0.3 Test Override Edges

```python
@needs_native
def test_user_save_overrides_base_save() -> None:
    graph = get_graph("graph_test")

    overrides = edges_of_kind(graph, EdgeKind.OVERRIDES)

    # Find by qualified_name — unambiguous
    user_save = [
        node for node in graph.symbols_of_kind(SymbolKind.METHOD)
        if node.qualified_name == "User.save"
        and node.file.endswith("models.py")
    ]
    base_save = [
        node for node in graph.symbols_of_kind(SymbolKind.METHOD)
        if node.qualified_name == "Base.save"
        and node.file.endswith("models.py")
    ]

    assert len(user_save) == 1
    assert len(base_save) == 1
    assert (user_save[0].symbol_id, base_save[0].symbol_id) in overrides
```

### 0.4 Test Dependency Queries Exclude Structural Edges

```python
from tyo3.graph import CodeGraph, EdgeData, EdgeKind
from tyo3.graph.models import SymbolNode
from tyo3.models.analysis import Range
from tyo3.models.symbols import SymbolKind


def _range() -> Range:
    return Range.model_validate(
        {"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}}
    )


def test_dependencies_do_not_include_defined_children() -> None:
    graph = CodeGraph()
    module = SymbolNode(
        symbol_id="a.py::<module>",
        name="a",
        qualified_name="<module>",
        kind=SymbolKind.MODULE,
        file="a.py",
        range=_range(),
    )
    function = SymbolNode(
        symbol_id="a.py::f",
        name="f",
        qualified_name="f",
        kind=SymbolKind.FUNCTION,
        file="a.py",
        range=_range(),
    )
    graph._add_node(module)
    graph._add_node(function)
    graph._add_edge(
        module.symbol_id, function.symbol_id,
        EdgeData(kind=EdgeKind.DEFINES), "a.py",
    )

    assert graph.children(module.symbol_id) == [function]
    assert graph.dependencies(module.symbol_id) == set()
```

This test should fail before Phase 4.

### 0.5 Test Coordinate Past Current Line

Add to `src/tyo3/tests/test_coordinate_conversion.py`:

```python
@needs_native
def test_column_beyond_current_line_rejected() -> None:
    rp = RustProject(fixture_path("simple_package"))
    try:
        with pytest.raises(PositionError):
            rp.goto_definition("main.py", 1, 500)
    finally:
        rp.close()
```

This currently fails because the Rust converter accepts the position.

### Phase 0 Validation

```bash
devenv shell -- pytest src/tyo3/tests/test_graph_semantics.py -q
devenv shell -- pytest src/tyo3/tests/test_coordinate_conversion.py -q
```

Expected result before fixes:

- Graph semantic tests fail.
- Coordinate past-line test fails.
- Existing unrelated tests may still pass or fail according to baseline.

Do not weaken these tests to make them pass.

## Phase 1: Fix Rust Coordinate Validation

Purpose: make line/column positions mean exactly what the public API says.

Public contract:

- Lines are 1-based.
- Columns are 1-based.
- Columns count Unicode scalar values, matching the current use of Ruff's
  UTF-32 position encoding.
- A column beyond the current physical line must raise `PositionError`.

### 1.1 Understand the Current Rust Bug

Current code in `rust/src/coordinates.rs`:

```rust
let line_start_usize: usize = line_start.to_usize();
let line_text = &source[line_start_usize..];
let byte_col = char_len_to_byte_offset(line_text, col);

if usize::from(byte_col) > line_text.len() {
    return Err(...);
}
```

The variable `line_text` is misnamed. It is not the current line. It is the
entire rest of the file from `line_start` to EOF.

A request for line 1, column 500 can walk through newlines into later lines,
or clamp at EOF, instead of failing because line 1 is shorter.

### 1.2 Replace the Slice with the Current Line Only

In `rust/src/coordinates.rs`, change `position_to_offset_with_index()` so it
extracts the requested line only.

Suggested implementation:

```rust
let line_start = line_index.line_start(OneIndexed::from_zero_indexed(line_idx), source);
let line_start_usize = line_start.to_usize();

let line_end_usize = if line_idx + 1 < total_lines {
    line_index
        .line_start(OneIndexed::from_zero_indexed(line_idx + 1), source)
        .to_usize()
} else {
    source.len()
};

let raw_line_text = &source[line_start_usize..line_end_usize];
let line_text = raw_line_text.trim_end_matches(['\n', '\r']);

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

Rust explanation:

- `LineIndex` maps line numbers to byte offsets in the UTF-8 source string.
- `line_start` is a `TextSize`, Ruff's compact byte-offset type.
- We compute `line_end_usize` from the next line start, or EOF for the final
  line.
- `trim_end_matches(['\n', '\r'])` keeps newline characters out of the valid
  column range.
- `char_len_to_byte_offset()` converts a Unicode codepoint count to a byte
  offset, because Rust strings are UTF-8.

Important policy decision:

- With this implementation, column `line_length + 1` is allowed. It points to
  the position just after the last character on the line.
- Column `line_length + 2` is rejected.

If the project wants stricter behavior, change the comparison from `>` to
`>=`. Make that decision explicit in tests and README.

### 1.3 Add Rust Unit Tests

At the bottom of `rust/src/coordinates.rs`, add:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use ruff_source_file::LineIndex;

    fn offset(source: &str, line: u32, column: u32) -> Result<u32, String> {
        let index = LineIndex::from_source_text(source);
        position_to_offset_with_index(source, &index, line, column)
            .map(|t| t.to_u32())
    }

    #[test]
    fn rejects_column_beyond_current_line() {
        let source = "x = 1\ny = 2\n";
        assert!(offset(source, 1, 500).is_err());
    }

    #[test]
    fn accepts_end_of_line_position() {
        let source = "x = 1\ny = 2\n";
        assert!(offset(source, 1, 6).is_ok());
    }

    #[test]
    fn rejects_line_past_file() {
        let source = "x = 1\n";
        assert!(offset(source, 99, 1).is_err());
    }

    #[test]
    fn handles_unicode_codepoints() {
        let source = "αβ = 1\n";
        assert!(offset(source, 1, 1).is_ok());
        assert!(offset(source, 1, 2).is_ok());
        assert!(offset(source, 1, 99).is_err());
    }

    #[test]
    fn handles_crlf() {
        let source = "x = 1\r\ny = 2\r\n";
        assert!(offset(source, 1, 6).is_ok());
        assert!(offset(source, 1, 7).is_err());
    }
}
```

Adjust the exact end-of-line column if the policy differs.

### 1.4 Add Python Integration Tests

Keep the Phase 0 Python test. Add one test that verifies a valid position still
works:

```python
@needs_native
def test_valid_column_on_first_line_still_works() -> None:
    rp = RustProject(fixture_path("simple_package"))
    try:
        result = rp.goto_definition("main.py", 1, 1)
        assert isinstance(result, list)
    finally:
        rp.close()
```

### Phase 1 Validation

```bash
cargo test --manifest-path rust/Cargo.toml coordinates
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_coordinate_conversion.py -q
```

If `cargo test` fails because the crate is a PyO3 extension and cannot link in
your environment, run the Python integration test after `build`. The Python
test is the required gate for this phase.

Definition of done:

- `goto_definition("main.py", 1, 500)` raises `PositionError`.
- Zero and negative positions still raise `PositionError`.
- Unicode coordinate tests still pass.

## Phase 2: Normalize Public Exception Mapping

Purpose: callers should not need method-specific exception handling for bad
paths and bad positions.

### 2.1 Identify Methods That Resolve Paths

Methods resolving a file path should catch `_NativePathError` and raise
`PathResolutionError`:

- `check_file`
- `document_symbols`
- `goto_definition`
- `goto_declaration`
- `goto_type_definition`
- `find_references`
- `semantic_tokens`
- `file_occurrences`
- `hover`
- `type_hierarchy`

At the time of review, `_goto()`, `semantic_tokens()`, `file_occurrences()`,
and `type_hierarchy()` do this. `find_references()` and `hover()` do not.

### 2.2 Update `find_references()`

In `src/tyo3/rust_project.py`, add the missing exception handlers:

```python
except _NativePositionError as e:
    raise PositionError(str(e)) from e
except _NativePathError as e:
    raise PathResolutionError(str(e)) from e
except OverflowError as e:
    raise PositionError(str(e)) from e
except Exception as e:
    raise InternalTyError(f"Unexpected error in find_references(): {e}") from e
```

### 2.3 Update `hover()`

Same mapping:

```python
except _NativePositionError as e:
    raise PositionError(str(e)) from e
except _NativePathError as e:
    raise PathResolutionError(str(e)) from e
except OverflowError as e:
    raise PositionError(str(e)) from e
except Exception as e:
    raise InternalTyError(f"Unexpected error in hover(): {e}") from e
```

### 2.4 Remove Duplicate Position Validation

`TyO3Session._validate_position()` checks `line >= 1, column >= 1` in Python.
The Rust coordinate code (`coordinates.rs`) performs the same check. The Python
validation fires first, so the Rust path for zero/negative values is
unreachable. Remove the redundant Python-side validation and let Rust be the
single source of truth. This means `PositionError` for bad values is raised via
the Rust → `_NativePositionError` → `PositionError` mapping, same as all other
position errors.

Delete `_validate_position()` from `TyO3Session` and remove all calls to it.

Similarly, `workspace_symbols("")` is guarded in both `TyO3Session` (line 90)
and `RustProject` (line 308). Keep the guard in `TyO3Session` only — it is the
public API surface.

### 2.5 Add Tests

```python
@needs_native
def test_find_references_bad_path_raises_path_error() -> None:
    rp = RustProject(fixture_path("simple_package"))
    try:
        with pytest.raises(PathResolutionError):
            rp.find_references("missing.py", 1, 1)
    finally:
        rp.close()


@needs_native
def test_hover_bad_path_raises_path_error() -> None:
    rp = RustProject(fixture_path("simple_package"))
    try:
        with pytest.raises(PathResolutionError):
            rp.hover("missing.py", 1, 1)
    finally:
        rp.close()


@needs_native
def test_find_references_negative_position_raises_position_error() -> None:
    rp = RustProject(fixture_path("simple_package"))
    try:
        with pytest.raises(PositionError):
            rp.find_references("main.py", -1, 1)
    finally:
        rp.close()
```

### Phase 2 Validation

```bash
devenv shell -- pytest src/tyo3/tests/test_exceptions.py src/tyo3/tests/test_coordinate_conversion.py -q
```

Definition of done:

- Bad paths raise `PathResolutionError` consistently.
- Bad positions raise `PositionError` consistently.
- Unexpected native failures still raise `InternalTyError`.
- No duplicate validation logic between `TyO3Session` and `RustProject`.

## Phase 3: Rebuild `CodeGraph.build()` as Multi-Pass

Purpose: make graph construction deterministic and make references attach to
real enclosing symbols.

This is the most important graph phase.

### 3.1 Understand the Current Bugs

Current build flow:

```python
for file_path in session.files():
    graph._index_file(session, str(file_path))
```

`_index_file()` does all of this for one file:

1. Add module and symbol nodes.
2. Add containment edges.
3. Resolve references.
4. Collect diagnostics (via `check_file()` — a full project check).
5. Resolve inheritance.
6. Build range cache.

There are two ordering bugs:

**Bug A — range cache too late:** Reference resolution calls
`_find_enclosing_symbol()`, but the range cache is not built until step 6
(after references). The lookup falls back to the module node.

**Bug B — cross-file order dependence:** When processing file A's references,
if a target is in file B which hasn't been processed yet, the target is
classified as "external" and gets a stub node. This is wrong — it is a
project-local file.

The fix requires all project nodes and all range caches to exist before any
reference is resolved.

### 3.2 Fix the Range Sort Key

The range cache powers `_find_enclosing_symbol()`, which is the core mechanism
for attaching references to the correct symbol. The current sort key is wrong
for multi-line ranges:

```python
key=lambda t: (t[2] - t[0], t[3] - t[1])  # end_line - start_line, end_col - start_col
```

For a multi-line range, `end_col - start_col` is meaningless — a function on
lines 10-50 ending at column 5 is not smaller than one on lines 10-50 ending
at column 80. The sort must use total span as the metric:

```python
def _range_size(t: tuple[int, int, int, int, str]) -> tuple[int, int]:
    """Sort key: (line span, end column). Smallest ranges first."""
    start_line, _start_col, end_line, end_col, _sid = t
    return (end_line - start_line, end_col)
```

Use this in every place that builds the range cache (both `_index_file()` and
`_rebuild_indexes()`). The first element (line span) dominates; the second
element (end column) only matters for single-line ranges where line span is 0,
which is the one case where comparing columns is meaningful.

### 3.3 Split `_index_file()` into Phase Methods

Do this as a mechanical refactor. Keep behavior the same at first.

Create these private methods:

```python
def _collect_symbols_for_file(
    self, session: TyO3Session, file_str: str
) -> list[Symbol] | None:
    try:
        return session.document_symbols(file_str)
    except Exception:
        logger.warning("Failed to get symbols for %s, skipping", file_str)
        return None


def _materialize_file_nodes(
    self, file_str: str, symbols: list[Symbol]
) -> None:
    ...


def _add_containment_edges_for_file(
    self, file_str: str, symbols: list[Symbol]
) -> None:
    ...


def _build_range_cache_for_file(self, file_str: str) -> None:
    ...
```

Move existing code from `_index_file()` into these methods. Do not change logic
while moving code except where necessary to use parameters.

### 3.4 Implement Multi-Pass `build()`

Replace `CodeGraph.build()` with:

```python
@classmethod
def build(cls, session: TyO3Session) -> CodeGraph:
    graph = cls()
    files = [str(file_path) for file_path in session.files()]
    project_files = set(files)

    # ── Pass 1: collect symbols ──
    symbols_by_file: dict[str, list[Symbol]] = {}
    for file_str in files:
        symbols = graph._collect_symbols_for_file(session, file_str)
        if symbols is not None:
            symbols_by_file[file_str] = symbols

    # ── Pass 2: materialize all project nodes ──
    for file_str, symbols in symbols_by_file.items():
        graph._materialize_file_nodes(file_str, symbols)

    # ── Pass 3: structural edges + range caches ──
    for file_str, symbols in symbols_by_file.items():
        graph._add_containment_edges_for_file(file_str, symbols)
        graph._build_range_cache_for_file(file_str)

    # ── Pass 4: semantic references ──
    for file_str in symbols_by_file:
        graph._resolve_references_via_occurrences(
            session, file_str, project_files
        )

    # ── Pass 5: inheritance and overrides ──
    for file_str, symbols in symbols_by_file.items():
        graph._resolve_inheritance(session, file_str, symbols)

    # ── Pass 6: diagnostics (single check, distribute per-file) ──
    graph._collect_all_diagnostics(session)

    return graph
```

Key design decisions:

- **No build state on `self`.** The build method uses local variables
  (`symbols_by_file`, `project_files`) and passes them to helpers as arguments.
  The resulting `CodeGraph` instance carries only the graph and its indexes —
  no leftover construction scaffolding. This keeps the runtime object clean.

- **`project_files` is passed to reference resolution**, not stored. When
  `_resolve_references_via_occurrences` encounters a target in a project file,
  it knows not to create an external stub — the target exists as a node already
  (Pass 2 guaranteed this). If the target can't be found by ID or name lookup,
  it is a symbol-identity mismatch, not a missing file. Log a warning; do not
  defer it.

- **No `PendingReference` mechanism.** In a correct multi-pass build, all
  project nodes exist before Pass 4. If a project-local reference can't be
  resolved, that is a bug in symbol identity (the Rust-returned name doesn't
  match how the node was stored). The right response is a warning, not a
  deferred queue. A deferred queue would hide symbol-identity bugs by
  silently retrying with different matching strategies.

- **Single `check()` for diagnostics** (see 3.5).

### 3.5 Fix Diagnostics Collection: One Check, Not N

The current `_index_file()` calls `session.check_file(path)` for every file.
Each `check_file()` internally runs `state.db.check()` — a full project
type-check. Salsa caches the result after the first call, but the first call is
expensive, and there are still N FFI round-trips to filter and convert
diagnostics.

Replace per-file `check_file()` with a single `check()` call:

```python
def _collect_all_diagnostics(self, session: TyO3Session) -> None:
    """Collect diagnostics with a single check() call, distribute per-file."""
    try:
        result = session.check()
    except Exception:
        logger.warning("Failed to run project check, skipping diagnostics")
        return
    for diagnostic in result.diagnostics:
        if diagnostic.file:
            self._diagnostics.setdefault(diagnostic.file, []).append(diagnostic)
```

This replaces N FFI calls with 1.

### 3.6 Update `_resolve_references_via_occurrences` Signature

Add the `project_files` parameter so it can distinguish project-local files
from external ones without needing state on `self`:

```python
def _resolve_references_via_occurrences(
    self,
    session: TyO3Session,
    file_str: str,
    project_files: set[str],
) -> None:
```

Update `_ensure_target_node_simple()` similarly:

```python
def _ensure_target_node_simple(
    self,
    target_file: str,
    target_sid: str,
    target_name: str,
    project_files: set[str],
) -> str | None:
    if target_file in project_files:
        # All project nodes exist (Pass 2). If we can't find it, it's a
        # symbol-identity mismatch. Log and skip — do not create a stub.
        logger.debug(
            "Could not resolve project-local target %s in %s",
            target_name, target_file,
        )
        return None

    package = self._infer_package(target_file)
    ext_sid = f"{package}::{target_name}" if package else target_sid
    if ext_sid not in self._id_to_index:
        self._add_stub_node(
            symbol_id=ext_sid,
            name=target_name,
            qualified_name=target_name,
            kind=SymbolKind.UNKNOWN,
            package=package or "unknown",
        )
    return ext_sid
```

### 3.7 Delete `_index_file()`

Once `build()` is multi-pass, `_index_file()` has no callers. Delete it.
`update_file()` will be rewritten in Phase 4.

### Phase 3 Validation

```bash
devenv shell -- pytest src/tyo3/tests/test_graph_semantics.py -q
devenv shell -- pytest src/tyo3/tests/test_graph_build.py src/tyo3/tests/test_graph_queries.py -q
```

Manual sanity check:

```bash
devenv shell -- python - <<'PY'
from pathlib import Path
from tyo3.session import TyO3Session
from tyo3.graph import CodeGraph, EdgeKind

with TyO3Session(Path("fixtures/graph_test").resolve()) as session:
    graph = CodeGraph.build(session)
    for edge_idx in graph.graph.edge_indices():
        data = graph.graph.get_edge_data_by_index(edge_idx)
        if data.kind != EdgeKind.REFERENCES:
            continue
        src, tgt = graph.graph.get_edge_endpoints_by_index(edge_idx)
        print(graph.graph[src].symbol_id, "->", graph.graph[tgt].symbol_id)
PY
```

Definition of done:

- References inside `create_user()` attach to `create_user`.
- References inside `main()` attach to `main`.
- Method body references attach to the method, not the module.
- No project-local symbol becomes external because of file ordering.
- Diagnostics are collected via one `check()` call, not N `check_file()` calls.

## Phase 4: Fix Overrides, Dependency Semantics, and `update_file()`

Purpose: make graph relationships mean what their names say, and make
`update_file()` correct.

This phase combines three closely related fixes from the original guide
(Phases 4, 5.1, and 5.2) because they are all small, independent changes to
the same file with no ordering dependencies between them.

### 4.1 Fix `OVERRIDES` Traversal

Current code in `_resolve_inheritance()` has a critical indentation bug:

```python
if edge_data.kind != EdgeKind.INHERITS:
    continue
    parent_sid = ...  # DEAD CODE — unreachable after continue
```

Everything after `continue` is unreachable. The BFS never walks parent
classes and `ancestor_methods` is always empty.

Replace the inner loop body with:

```python
ancestor_methods: dict[str, str] = {}
visited: set[str] = {sid}
queue: deque[str] = deque([sid])

while queue:
    current_sid = queue.popleft()
    current_idx = self._id_to_index.get(current_sid)
    if current_idx is None:
        continue

    for _src, succ_idx, edge_data in self._graph.out_edges(current_idx):
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

### 4.2 Define Dependency Edge Kinds

Near the top of `graph.py`, add:

```python
DEPENDENCY_EDGE_KINDS: frozenset[EdgeKind] = frozenset({
    EdgeKind.REFERENCES,
    EdgeKind.IMPORTS,
    EdgeKind.INHERITS,
    EdgeKind.OVERRIDES,
    EdgeKind.TYPE_OF,
    EdgeKind.RETURNS,
    EdgeKind.INSTANTIATES,
})
```

Use `frozenset` — this is a constant.

Update `dependencies()` and `dependents()`:

```python
def dependencies(
    self,
    symbol_id: str,
    *,
    kinds: frozenset[EdgeKind] | None = None,
) -> set[str]:
    """All symbols this one directly depends on (semantic edges only)."""
    edge_kinds = DEPENDENCY_EDGE_KINDS if kinds is None else kinds
    return {
        self._graph[tgt_idx].symbol_id
        for tgt_idx, _data in self._edges_of_kind(symbol_id, edge_kinds)
    }
```

For `transitive_dependencies()` and `transitive_dependents()`, build a
filtered subgraph and delegate to rustworkx instead of reimplementing BFS in
Python:

```python
def transitive_dependencies(
    self,
    symbol_id: str,
    *,
    kinds: frozenset[EdgeKind] | None = None,
) -> set[str]:
    """All symbols reachable via semantic edges from this one."""
    edge_kinds = DEPENDENCY_EDGE_KINDS if kinds is None else kinds
    filtered = self._semantic_subgraph(edge_kinds)
    idx = self._id_to_index.get(symbol_id)
    if idx is None:
        return set()
    # filtered uses the same indices as the main graph
    reachable = rx.descendants(filtered, idx)
    return {self._graph[i].symbol_id for i in reachable}


def _semantic_subgraph(self, kinds: frozenset[EdgeKind]) -> rx.PyDiGraph:
    """Build a subgraph containing only edges of the given kinds.

    Node indices are preserved (same as the main graph) so callers can
    use `self._id_to_index` for lookups.
    """
    sub = self._graph.copy()
    to_remove = [
        edge_idx for edge_idx in sub.edge_indices()
        if sub.get_edge_data_by_index(edge_idx).kind not in kinds
    ]
    sub.remove_edges_from(to_remove)
    return sub
```

This keeps the BFS in Rust (fast) while filtering edge kinds. The graph copy
is O(V+E) but avoids the O(V * E) recursive-`dependencies()` approach.

### 4.3 Make `update_file()` Correct

The current `update_file()` removes nodes for the changed file and re-indexes
it, but incoming references from other files are lost because those files are
not re-indexed.

Replace with a targeted rebuild that re-indexes only the affected files:

```python
def update_file(self, session: TyO3Session, path: str) -> None:
    """Re-index a file and all files that reference symbols in it.

    Rebuilds the changed file plus its reverse-dependency set to
    preserve cross-file reference edges. This is cheaper than a full
    rebuild for large projects (|affected| << |total|) while remaining
    correct for all edge types.

    For the initial implementation, this delegates to a full rebuild.
    Once profiling shows this is a bottleneck, the targeted approach
    (documented above) should be implemented.
    """
    fresh = CodeGraph.build(session)
    # Swap all internal state — the old graph is discarded.
    self.__dict__.update(fresh.__dict__)
```

The `__dict__` swap is cleaner than listing every field — it cannot fall out
of sync when new fields are added.

The docstring documents the intended future optimization (targeted rebuild of
changed file + reverse deps) without implementing it prematurely. When the
need arises, the implementation would:

1. Identify all files that have REFERENCES edges pointing into the changed file.
2. Remove nodes for the changed file + those files.
3. Re-run Passes 1-5 for only those files.

### 4.4 Add Tests

Use the Phase 0 override and dependency tests. Add:

```python
def test_dependencies_include_references() -> None:
    graph = CodeGraph()
    f = SymbolNode(
        symbol_id="a.py::f", name="f", qualified_name="f",
        kind=SymbolKind.FUNCTION, file="a.py", range=_range(),
    )
    g = SymbolNode(
        symbol_id="a.py::g", name="g", qualified_name="g",
        kind=SymbolKind.FUNCTION, file="a.py", range=_range(),
    )
    graph._add_node(f)
    graph._add_node(g)
    graph._add_edge(f.symbol_id, g.symbol_id, EdgeData(kind=EdgeKind.REFERENCES), "a.py")

    assert graph.dependencies(f.symbol_id) == {g.symbol_id}
    assert graph.dependents(g.symbol_id) == {f.symbol_id}


@needs_native
def test_update_file_preserves_incoming_references() -> None:
    session = get_session("graph_test")
    graph = CodeGraph.build(session)

    user = find_one(graph, file_suffix="models.py", name="User", kind=SymbolKind.CLASS)
    refs_before = graph.references_to(user.symbol_id)
    assert refs_before

    models_path = next(
        str(path) for path in session.files()
        if str(path).endswith("models.py")
    )
    graph.update_file(session, models_path)

    user_after = find_one(graph, file_suffix="models.py", name="User", kind=SymbolKind.CLASS)
    refs_after = graph.references_to(user_after.symbol_id)
    assert refs_after
```

### Phase 4 Validation

```bash
devenv shell -- pytest src/tyo3/tests/test_graph_semantics.py src/tyo3/tests/test_graph_queries.py src/tyo3/tests/test_graph_update.py -q
```

Definition of done:

- `User.save --OVERRIDES--> Base.save` exists.
- `dependencies()` excludes `DEFINES` and `CONTAINS`.
- `dependencies()` includes real semantic references.
- `update_file()` preserves incoming cross-file references.

## Phase 5: Model Imports Deliberately

Purpose: avoid using arbitrary symbol references as a proxy for import cycles.

### 5.1 Current Behavior

`EdgeKind.IMPORTS` exists, but `_resolve_references_via_occurrences()` always
adds `EdgeKind.REFERENCES`, even when `occ.role == ReferenceRole.IMPORT`.

Import cycle functions then aggregate `IMPORTS` and `REFERENCES`, which means
non-import symbol references can influence import-cycle answers.

### 5.2 Add Both Symbol-Level and Module-Level Import Edges

When resolving an occurrence with `role == ReferenceRole.IMPORT`:

1. **Keep the symbol-level REFERENCES edge** (from enclosing symbol to
   imported symbol). This preserves dependency-query granularity — "function
   `f` depends on class `User`" remains queryable.

2. **Add a module-level IMPORTS edge** (from source `<module>` to target
   `<module>`). This is what import-cycle detection uses.

```python
if occ.role == ReferenceRole.IMPORT and target_file != file_str:
    self._add_import_edge(file_str, target_file, occ.range, project_files)
```

Implement:

```python
def _add_import_edge(
    self,
    source_file: str,
    target_file: str,
    range: Range,
    project_files: set[str],
) -> None:
    """Add a module-level IMPORTS edge between two files."""
    source_module = f"{source_file}::<module>"
    target_module = f"{target_file}::<module>"

    if source_module not in self._id_to_index:
        return

    if target_module not in self._id_to_index:
        if target_file in project_files:
            return  # Project file without a module node — skip
        package = self._infer_package(target_file) or "unknown"
        target_module = f"{package}::<module>"
        if target_module not in self._id_to_index:
            self._add_stub_node(
                symbol_id=target_module,
                name=package,
                qualified_name="<module>",
                kind=SymbolKind.MODULE,
                package=package,
            )

    self._add_edge(
        source_module,
        target_module,
        EdgeData(kind=EdgeKind.IMPORTS, file=source_file, range=range),
        source_file,
    )
```

### 5.3 Extract Shared Module-Graph Construction

`import_cycles()` and `import_cycle_groups()` both build a module-level
dependency graph from scratch by scanning all edges. Extract the shared logic:

```python
def _build_module_graph(self) -> tuple[rx.PyDiGraph, dict[str, int]]:
    """Build a module-level graph from IMPORTS edges.

    Returns (module_graph, sid_to_index) where module_graph nodes are
    module symbol_id strings.
    """
    module_indices = [
        i for i in self._graph.node_indices()
        if self._graph[i].kind == SymbolKind.MODULE
    ]
    if len(module_indices) < 2:
        return rx.PyDiGraph(), {}

    mod_graph = rx.PyDiGraph()
    sid_to_midx: dict[str, int] = {}
    for i in module_indices:
        sid = self._graph[i].symbol_id
        midx = mod_graph.add_node(sid)
        sid_to_midx[sid] = midx

    # Map every node to its module for aggregation
    node_to_module: dict[int, str] = {}
    for mi in module_indices:
        module_sid = self._graph[mi].symbol_id
        file = file_from_symbol_id(module_sid)
        for ni in self._file_to_nodes.get(file, []):
            node_to_module[ni] = module_sid

    for edge_idx in self._graph.edge_indices():
        data = self._graph.get_edge_data_by_index(edge_idx)
        if data.kind != EdgeKind.IMPORTS:
            continue
        src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
        src_mod = node_to_module.get(src)
        tgt_mod = node_to_module.get(tgt)
        if src_mod and tgt_mod and src_mod != tgt_mod:
            mi_src = sid_to_midx.get(src_mod)
            mi_tgt = sid_to_midx.get(tgt_mod)
            if mi_src is not None and mi_tgt is not None:
                mod_graph.add_edge(mi_src, mi_tgt, None)

    return mod_graph, sid_to_midx
```

Then both `import_cycles()` and `import_cycle_groups()` call
`self._build_module_graph()` and operate on the result.

### 5.4 Update Import Cycle Detection to Use `IMPORTS` Only

In both `import_cycles()` and `import_cycle_groups()`, the old code used:

```python
dep_kinds = {EdgeKind.IMPORTS, EdgeKind.REFERENCES}
```

The refactored `_build_module_graph()` uses `EdgeKind.IMPORTS` only. The old
behavior (including cross-file references) is no longer needed.

### 5.5 Add Tests

Use `fixtures/circular_imports`:

```python
@needs_native
def test_circular_imports_detected_from_import_edges() -> None:
    graph = get_graph("circular_imports")
    cycles = graph.import_cycles()
    assert cycles
    # Verify the cycle contains both modules
    all_sids_in_cycles = {sid for cycle in cycles for sid in cycle}
    assert any(sid.endswith("module_a.py::<module>") for sid in all_sids_in_cycles)
    assert any(sid.endswith("module_b.py::<module>") for sid in all_sids_in_cycles)
```

Add a unit test showing a plain `REFERENCES` edge does not create an import
cycle:

```python
def test_references_do_not_create_import_cycles() -> None:
    graph = CodeGraph()
    mod_a = SymbolNode(
        symbol_id="a.py::<module>", name="a", qualified_name="<module>",
        kind=SymbolKind.MODULE, file="a.py", range=_range(),
    )
    mod_b = SymbolNode(
        symbol_id="b.py::<module>", name="b", qualified_name="<module>",
        kind=SymbolKind.MODULE, file="b.py", range=_range(),
    )
    func_a = SymbolNode(
        symbol_id="a.py::f", name="f", qualified_name="f",
        kind=SymbolKind.FUNCTION, file="a.py", range=_range(),
    )
    func_b = SymbolNode(
        symbol_id="b.py::g", name="g", qualified_name="g",
        kind=SymbolKind.FUNCTION, file="b.py", range=_range(),
    )
    for node in [mod_a, mod_b, func_a, func_b]:
        graph._add_node(node)
    # Cross-file REFERENCES edges in both directions — but no IMPORTS
    graph._add_edge("a.py::f", "b.py::g", EdgeData(kind=EdgeKind.REFERENCES), "a.py")
    graph._add_edge("b.py::g", "a.py::f", EdgeData(kind=EdgeKind.REFERENCES), "b.py")

    assert graph.import_cycles() == []
```

### Phase 5 Validation

```bash
devenv shell -- pytest src/tyo3/tests/test_graph_cycles.py src/tyo3/tests/test_file_occurrences.py -q
```

Definition of done:

- Import cycles are based on `IMPORTS` edges only.
- Plain cross-file references do not create import cycles.
- `import_cycles()` and `import_cycle_groups()` share module-graph construction.

## Phase 6: Introduce Build Reports

Purpose: callers need to know whether a graph is complete.

Current graph construction catches broad exceptions and logs warnings. That
can return a graph missing whole files or edge classes with no programmatic
way to detect it.

### 6.1 Add Build Report Model

In `src/tyo3/graph/models.py`:

```python
from typing import Literal

class GraphBuildFailure(BaseModel):
    file: str
    phase: Literal["symbols", "references", "diagnostics", "inheritance"]
    error_type: str
    message: str


class GraphBuildReport(BaseModel):
    files_indexed: int = 0
    files_total: int = 0
    failures: list[GraphBuildFailure] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.failures
```

### 6.2 Thread the Report Through `build()`

Make `build()` accept an optional report parameter and record failures:

```python
@classmethod
def build(
    cls,
    session: TyO3Session,
    *,
    report: GraphBuildReport | None = None,
) -> CodeGraph:
    graph = cls()
    files = [str(file_path) for file_path in session.files()]
    if report is not None:
        report.files_total = len(files)
    ...
```

Each `try/except` in the build passes records a `GraphBuildFailure` on the
report instead of (or in addition to) logging.

Add a convenience constructor:

```python
@classmethod
def build_with_report(
    cls,
    session: TyO3Session,
) -> tuple[CodeGraph, GraphBuildReport]:
    report = GraphBuildReport()
    graph = cls.build(session, report=report)
    return graph, report
```

The existing `build(session)` call (no report) continues to work unchanged.

### 6.3 Add Tests

```python
def test_build_report_records_symbol_failure() -> None:
    # Use a mock session whose document_symbols() raises for one file.
    ...
    graph, report = CodeGraph.build_with_report(mock_session)
    assert not report.complete
    assert len(report.failures) == 1
    assert report.failures[0].phase == "symbols"
```

### Phase 6 Validation

```bash
devenv shell -- pytest src/tyo3/tests/test_graph_build.py -q
```

Definition of done:

- Graph partial failures are inspectable via `GraphBuildReport`.
- `build()` without a report works unchanged.

## Phase 7: Stabilize Symbol Identity and Paths

Purpose: make graph IDs portable and snapshot-friendly.

Do this after graph semantics are correct. Otherwise, path normalization makes
failing tests harder to interpret.

### 7.1 Public Path Policy

- First-party public paths are project-relative `PurePosixPath`.
- Internal Rust can use absolute paths.
- External paths use the inferred package name.
- Stable graph IDs must not include `/home/...` absolute paths.

Example IDs:

```text
app.py::<module>
app.py::create_user
models.py::User
pydantic::BaseModel
stdlib::int
```

### 7.2 Normalize at the Session Boundary

Rather than maintaining a mapping dict between graph paths and native paths,
normalize once and denormalize once:

```python
# In CodeGraph.build(), before Pass 1:
root = session.root
native_paths = [str(p) for p in session.files()]
graph_paths = [_to_relative(root, p) for p in native_paths]
native_by_graph = dict(zip(graph_paths, native_paths, strict=True))
```

Session calls use native paths (`session.document_symbols(native_path)`).
Graph nodes, symbol IDs, and secondary indexes use graph paths.

This is a single normalization point. The helper:

```python
def _to_relative(root: Path, path: str) -> str:
    """Convert an absolute path to a project-relative POSIX string."""
    try:
        return str(PurePosixPath(Path(path).resolve().relative_to(root.resolve())))
    except ValueError:
        return path  # External path — return as-is
```

Result paths from Rust (in occurrences, diagnostics, etc.) also need
normalization when they refer to project files. Add a helper for that:

```python
def _normalize_result_path(
    root: Path, path: str, project_files: set[str]
) -> str:
    """Normalize a Rust-returned path to match graph-path format."""
    candidate = _to_relative(root, path)
    if candidate in project_files:
        return candidate
    return path
```

### 7.3 Add Tests

Add a test that builds a graph from a fixture copied to a temp directory
and verifies the IDs match:

```python
def test_graph_ids_are_path_independent(tmp_path) -> None:
    # shutil.copytree("fixtures/graph_test", tmp_path / "graph_test")
    # Build graphs from both locations
    # Assert sorted first-party symbol IDs are identical
```

### Phase 7 Validation

```bash
devenv shell -- pytest src/tyo3/tests/test_graph_semantics.py src/tyo3/tests/test_graph_export.py -q
```

Definition of done:

- Graph IDs no longer include absolute first-party paths.
- Same fixture in two absolute directories produces the same first-party IDs.
- External symbols remain explicitly external.

## Phase 8: Harden Native Boundary

Purpose: reduce native boundary fragility.

### 8.1 Avoid Panics in Diagnostic Conversion

Current `rust/src/convert/diagnostics.rs` uses:

```rust
let file = span.expect_ty_file();
```

This panics if a diagnostic span is not a ty file span.

Search for a non-panicking alternative:

```bash
rg "fn .*ty_file|expect_ty_file" rust ~/.cargo/git/checkouts
```

If a non-panicking API exists (e.g. `span.ty_file() -> Option<File>`):

```rust
let Some(file) = span.ty_file() else {
    return (None, None);
};
```

If no non-panicking API exists, wrap the call with `std::panic::catch_unwind`
to prevent panics from crossing the PyO3 boundary (which can cause undefined
behavior):

```rust
let file = match std::panic::catch_unwind(|| span.expect_ty_file()) {
    Ok(f) => f,
    Err(_) => return (None, None),
};
```

### 8.2 Delete Dead Rust Code

`rust/src/convert/hover.rs` contains `convert_hover_markdown()` (the
convenience wrapper without `_with_index`). It is never called — all callers
use `convert_hover_markdown_with_index()` directly. Delete it.

### 8.3 Fix PyO3 Version Mismatch in Derive Crate

`rust/tyo3-derive/Cargo.toml` has `pyo3 = { version = "0.23" }` in
dev-dependencies while the main crate uses `0.28`. Update:

```toml
[dev-dependencies]
pyo3 = { version = "0.28", features = ["extension-module"] }
```

### Phase 8 Validation

```bash
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_check_file.py src/tyo3/tests/test_rust_integration.py -q
```

Definition of done:

- Diagnostic conversion does not use avoidable panicking APIs.
- No dead code in the Rust convert modules.
- Derive crate dev-dependency matches the main crate version.

## Phase 9: Clean Dependency Cache Serialization

Purpose: fix smaller correctness issues in cached dependency graphs.

### 9.1 Fix `DependencyGraph.symbols_of_kind()`

Current code compares `node.kind` (`SymbolKind`) to a `str`. Change the
signature to accept `SymbolKind`:

```python
def symbols_of_kind(self, kind: SymbolKind) -> list[SymbolNode]:
    return [
        self.graph[idx]
        for idx in self.graph.node_indices()
        if self.graph[idx].kind == kind
    ]
```

Import `SymbolKind` from `tyo3.models.symbols`.

### 9.2 Fix `EdgeData.role` Deserialization

Current `DependencyGraph.load()` passes raw strings for `role`:

```python
role=raw_edge.get("role")
```

Change to:

```python
from tyo3.models.navigation import ReferenceRole

role_raw = raw_edge.get("role")
role = ReferenceRole(role_raw) if role_raw is not None else None
```

### 9.3 Add Round-Trip Test

```python
def test_dependency_graph_edge_role_roundtrips(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("tyo3.graph.dependency.CACHE_DIR", tmp_path)
    # Build a DependencyGraph with an EdgeData(role=ReferenceRole.READ),
    # save it, load it, and assert the loaded role is ReferenceRole.READ
    # (not the string "read").
```

### Phase 9 Validation

```bash
devenv shell -- pytest src/tyo3/tests/test_graph_dependency.py src/tyo3/tests/test_graph_export.py -q
```

Definition of done:

- Loaded dependency graph roles are `ReferenceRole`, not raw strings.
- JSON export works after loading cached dependency graphs.

## Phase 10: Static Gates and CI

Purpose: prevent regressions after the correctness work lands.

### 10.1 Fix Ruff Diagnostics

```bash
devenv shell -- ruff check . --fix
```

Then handle remaining warnings manually. Known categories from review:

- import ordering
- unused imports
- unused locals
- `zip()` without `strict=`
- blind `pytest.raises(Exception)`

For `zip(new_nodes, indices)`, use:

```python
for node, idx in zip(new_nodes, indices, strict=True):
    ...
```

### 10.2 Clean Up Unused Spec Models

`models/core.py` exports `TyProject`, `ProjectFile`, `BackendInfo`,
`TyProjectConfig` — none of which are constructed by any code path. They are
spec-anticipation models with no backend.

Move them to `models/_spec.py` (most already are) and remove from the public
`__all__` in `models/__init__.py`. Keep them importable for anyone who has
adopted them, but stop advertising them as part of the active API.

### 10.3 Update GitHub Actions

Current CI manually runs `cargo build --release` and copies a hardcoded
extension filename. Replace with the devenv workflow:

```yaml
- name: Build native extension
  run: devenv shell -- build-release

- name: Run tests
  run: devenv shell -- tests

- name: Ruff
  run: devenv shell -- ruff check .
```

### Phase 10 Validation

```bash
devenv shell -- ruff check .
devenv shell -- build-release
devenv shell -- tests
```

Definition of done:

- `ruff check .` passes locally and in CI.
- CI uses maturin/devenv to build the extension.
- Unused spec models are not in the public `__all__`.

## Phase 11: DTO Boundary Simplification

Purpose: simplify maintenance of the Rust/Python transport layer.

This is not required before graph correctness. Do this only after the earlier
phases are stable.

### 11.1 Current Boundary

Rust DTOs are PyO3 classes with custom `PyFields` metadata. Python converts
them via `_to_python()` using string type tags:

```text
str, obj, opt:obj, list:obj
```

Every new DTO requires updates in six places: Rust struct, PyO3 class
registration, `PyFields` derive, enum registry, Python converter tests,
Pydantic model.

### 11.2 Target: Use `pythonize` for Direct Serde-to-Python Conversion

The `pythonize` crate (compatible with PyO3 0.23+) converts Rust types
implementing `serde::Serialize` directly into native Python objects (dict,
list, str, int, etc.) without an intermediate JSON step:

```rust
use pythonize::pythonize;

fn document_symbols<'py>(
    &self,
    py: Python<'py>,
    path: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let symbols = self.inner_document_symbols(path)?;
    pythonize(py, &symbols).map_err(|e| PyRuntimeError::new_err(e.to_string()))
}
```

Python side simplifies to:

```python
raw = self._inner.document_symbols(str(path))
return [Symbol.model_validate(item) for item in raw]
```

This eliminates:

- The entire `_to_python()` recursive converter
- The `PyFields` derive macro
- The `_TYO3_ENUM_TYPES` registry
- All DTO `#[pyclass]` registrations (DTOs become internal Rust structs only)
- The `_build_enum_cache()` / `_is_native_enum()` machinery

The DTO structs remain in Rust (they define the serialization contract), but
they only need `#[derive(Serialize)]`, not `#[pyclass]` or `#[derive(PyFields)]`.

### 11.3 Validation

- Native methods return `list`/`dict`, not native DTO objects.
- Pydantic validates returned data.
- Enum strings exactly match Python enum values.
- `_native_impl` exports only `TyProject` and exception classes.
- All existing Python tests continue to pass.

## Final Definition of Done

The refactoring is complete when all of these are true:

- `CodeGraph.build()` is multi-pass and deterministic.
- References inside functions/methods attach to those functions/methods.
- Project-local references are never externalized due to file order.
- `update_file()` preserves cross-file references.
- `OVERRIDES` edges exist for `User.save -> Base.save`.
- `dependencies()` excludes structural edges by default.
- Import cycles use `IMPORTS`, not arbitrary `REFERENCES`.
- `import_cycles()` and `import_cycle_groups()` share module-graph construction.
- Columns beyond the current line raise `PositionError`.
- Public exception mapping is consistent for bad paths and bad positions.
- No duplicate validation between `TyO3Session` and `RustProject`.
- Diagnostics collected via single `check()`, not N `check_file()` calls.
- Range-size sort key is correct for multi-line ranges.
- Dependency graph cache round-trips roles correctly.
- `ruff check .` passes.
- CI builds the native extension with maturin/devenv.
- Exact semantic graph tests exist and pass.
- No dead Rust code in convert modules.

## Suggested PR Order

1. Regression tests for graph semantics and coordinate validation (Phase 0).
2. Rust coordinate validation fix (Phase 1).
3. Python exception mapping + duplicate validation cleanup (Phase 2).
4. Multi-pass graph build + single-check diagnostics + range sort fix (Phase 3).
5. Override fix + dependency filtering + `update_file()` (Phase 4).
6. Import edge modeling + shared module-graph (Phase 5).
7. Build reports (Phase 6).
8. Path normalization (Phase 7).
9. Native boundary hardening (Phase 8).
10. Dependency cache fixes (Phase 9).
11. Ruff, CI, unused model cleanup (Phase 10).
12. Optional DTO pythonize simplification (Phase 11).

Do not skip directly to DTO or package cleanup. The graph answers must become
true first.
