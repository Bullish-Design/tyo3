# TyO3 Review 2 Refactoring Guide

Date: 2026-06-03

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

The target state after this guide:

- `CodeGraph.build()` is deterministic and multi-pass.
- References attach to the innermost enclosing symbol, not the module fallback.
- Project-local targets are never externalized because of file ordering.
- `OVERRIDES` edges exist for simple inheritance.
- `update_file()` is correct, even if initially implemented as a full rebuild.
- Coordinates past the current line raise `PositionError`.
- Dependency queries filter dependency edge kinds explicitly.
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
devenv shell
build
pytest src/tyo3/tests/ -q
ruff check .
ty check src
```

At the time of this review, `ruff check .` and `ty check src` are expected to
fail. Do not treat that as permission to ignore new failures. Each phase below
defines the minimum validation for that phase.

## Phase 0: Add Focused Regression Tests

Purpose: capture the bugs before changing the implementation. Some of these
tests will fail at first. That is expected.

### 0.1 Add a Graph Helper Module for Tests

Create a helper in `src/tyo3/tests/test_graph_semantics.py` or add helper
functions to an existing graph test file.

Use helpers like this:

```python
from tyo3.graph import CodeGraph, EdgeKind
from tyo3.graph.models import SymbolNode
from tyo3.models.symbols import SymbolKind


def find_one(graph: CodeGraph, *, file_suffix: str, name: str, kind: SymbolKind) -> SymbolNode:
    matches = [
        node
        for node in graph.symbols_of_kind(kind)
        if node.file.endswith(file_suffix) and node.name == name and not node.external
    ]
    assert len(matches) == 1, (
        f"Expected exactly one {kind} named {name!r} in {file_suffix}, "
        f"got {[m.symbol_id for m in matches]}"
    )
    return matches[0]


def edges_of_kind(graph: CodeGraph, kind: EdgeKind) -> list[tuple[str, str]]:
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
  absolute. Later, after path normalization, update these to exact relative
  paths.

### 0.2 Test References Attach to Functions, Not Modules

Use the existing `fixtures/graph_test`:

```python
from pathlib import Path

from tyo3.graph import EdgeKind
from tyo3.models.symbols import SymbolKind
from tyo3.tests.conftest import get_graph, needs_native


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

    base_save = find_one(
        graph,
        file_suffix="models.py",
        name="save",
        kind=SymbolKind.METHOD,
    )
    user_save_candidates = [
        node for node in graph.symbols_of_kind(SymbolKind.METHOD)
        if node.file.endswith("models.py")
        and node.name == "save"
        and ".User." in node.symbol_id
    ]
    assert len(user_save_candidates) == 1
    user_save = user_save_candidates[0]

    override_edges = edges_of_kind(graph, EdgeKind.OVERRIDES)
    assert (user_save.symbol_id, base_save.symbol_id) in override_edges
```

If this helper finds both `Base.save` and `User.save`, refine selection by
`qualified_name`:

```python
node.qualified_name == "Base.save"
node.qualified_name == "User.save"
```

### 0.4 Test Dependency Queries Exclude Structural Edges

```python
from tyo3.graph import CodeGraph, EdgeData, EdgeKind, SymbolNode
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
    graph._add_edge(module.symbol_id, function.symbol_id, EdgeData(kind=EdgeKind.DEFINES), "a.py")

    assert graph.children(module.symbol_id) == [function]
    assert graph.dependencies(module.symbol_id) == set()
```

This test should fail before Phase 5.

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

Run:

```bash
PYTHONPATH=src pytest src/tyo3/tests/test_graph_semantics.py -q
PYTHONPATH=src pytest src/tyo3/tests/test_coordinate_conversion.py -q
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

Run:

```bash
cargo test --manifest-path rust/Cargo.toml coordinates
build
PYTHONPATH=src pytest src/tyo3/tests/test_coordinate_conversion.py -q
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

In `src/tyo3/rust_project.py`, change:

```python
except _NativePositionError as e:
    raise PositionError(str(e)) from e
except Exception as e:
    raise InternalTyError(f"Unexpected error in find_references(): {e}") from e
```

to:

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

Use the same mapping:

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

### 2.4 Add Tests

Add native tests:

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
```

Also test negative values:

```python
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

Run:

```bash
PYTHONPATH=src pytest src/tyo3/tests/test_exceptions.py src/tyo3/tests/test_coordinate_conversion.py -q
```

Definition of done:

- Bad paths raise `PathResolutionError` consistently.
- Bad positions raise `PositionError` consistently.
- Unexpected native failures still raise `InternalTyError`.

## Phase 3: Rebuild `CodeGraph.build()` as Multi-Pass

Purpose: make graph construction deterministic and make references attach to
real enclosing symbols.

This is the most important graph phase.

### 3.1 Understand the Current Bug

Current build flow:

```python
for file_path in session.files():
    graph._index_file(session, str(file_path))
```

`_index_file()` does all of this for one file:

1. Add module and symbol nodes.
2. Add containment edges.
3. Resolve references.
4. Collect diagnostics.
5. Resolve inheritance.
6. Build range cache.

Reference resolution calls `_find_enclosing_symbol()`, but the range cache is
not built until after reference resolution. Therefore the lookup falls back to
the module node.

The fix is not just moving one line. Cross-file target nodes also need to exist
before reference resolution.

### 3.2 Add Construction State

In `CodeGraph.__init__()`, add:

```python
self._project_files: set[str] = set()
self._symbols_by_file: dict[str, list[Symbol]] = {}
self._pending_references: list[PendingReference] = []
```

Add a dataclass near the top of `graph.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PendingReference:
    source_symbol_id: str
    target_file: str
    target_name: str
    target_qualified_name: str | None
    file: str
    range: Range
    role: ReferenceRole
```

Explanation:

- `_project_files` means "files in this project", not "files already indexed".
- `_symbols_by_file` lets later phases reuse the symbols collected in the first
  pass.
- `_pending_references` records references that point to project files but
  cannot yet be resolved to a known symbol. Do not create external stubs for
  these.

### 3.3 Split `_index_file()` into Smaller Methods

Do this as a mechanical refactor. Keep behavior the same at first.

Create these private methods:

```python
def _collect_symbols_for_file(self, session: TyO3Session, file_str: str) -> list[Symbol] | None:
    try:
        return session.document_symbols(file_str)
    except Exception:
        logger.warning("Failed to get symbols for %s, skipping", file_str)
        return None


def _materialize_file_nodes(self, file_str: str, symbols: list[Symbol]) -> None:
    ...


def _add_containment_edges_for_file(self, file_str: str, symbols: list[Symbol]) -> None:
    ...


def _build_range_cache_for_file(self, file_str: str) -> None:
    ...


def _collect_diagnostics_for_file(self, session: TyO3Session, file_str: str) -> None:
    ...


def _resolve_inheritance_for_file(self, session: TyO3Session, file_str: str) -> None:
    symbols = self._symbols_by_file.get(file_str, [])
    self._resolve_inheritance(session, file_str, symbols)
```

Move existing code from `_index_file()` into these methods. Do not change logic
while moving code except where necessary to use parameters.

After this, `_index_file()` can temporarily call the new methods in the old
order. Run tests before proceeding if the refactor is large.

### 3.4 Implement Multi-Pass `build()`

Replace `CodeGraph.build()` with:

```python
@classmethod
def build(cls, session: TyO3Session) -> CodeGraph:
    graph = cls()
    files = [str(file_path) for file_path in session.files()]
    graph._project_files = set(files)

    # Pass 1: collect symbols
    for file_str in files:
        symbols = graph._collect_symbols_for_file(session, file_str)
        if symbols is None:
            continue
        graph._symbols_by_file[file_str] = symbols

    # Pass 2: materialize all local nodes
    for file_str, symbols in graph._symbols_by_file.items():
        graph._materialize_file_nodes(file_str, symbols)

    # Pass 3: structural edges and range caches
    for file_str, symbols in graph._symbols_by_file.items():
        graph._add_containment_edges_for_file(file_str, symbols)
        graph._build_range_cache_for_file(file_str)

    # Pass 4: semantic references
    for file_str in graph._symbols_by_file:
        graph._resolve_references_via_occurrences(session, file_str)

    graph._resolve_pending_references()

    # Pass 5: inheritance and overrides
    for file_str in graph._symbols_by_file:
        graph._resolve_inheritance_for_file(session, file_str)

    # Pass 6: diagnostics
    for file_str in graph._symbols_by_file:
        graph._collect_diagnostics_for_file(session, file_str)

    return graph
```

Explanation:

- All project nodes exist before any reference is resolved.
- All range caches exist before `_find_enclosing_symbol()` is called.
- External stubs can now be limited to files not in `_project_files`.
- Diagnostics are last because they do not affect graph topology.

### 3.5 Fix External Target Classification

Update `_ensure_target_node_simple()`:

```python
def _ensure_target_node_simple(
    self,
    target_file: str,
    target_sid: str,
    target_name: str,
) -> str | None:
    if target_file in self._project_files:
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

Important: use `_project_files`, not `_file_to_nodes`.

### 3.6 Add Pending Reference Recording

In `_resolve_references_via_occurrences()`, when a target cannot be resolved
and `target_file in self._project_files`, append a `PendingReference`:

```python
if target_sid not in self._id_to_index:
    found = self._find_symbol_in_file(target_file, target_name)
    if found:
        target_sid = found
    elif target_file in self._project_files:
        enclosing_id = self._find_enclosing_symbol(file_str, occ.range)
        if enclosing_id is not None:
            self._pending_references.append(
                PendingReference(
                    source_symbol_id=enclosing_id,
                    target_file=target_file,
                    target_name=target_name,
                    target_qualified_name=occ.target_qualified_name,
                    file=file_str,
                    range=occ.range,
                    role=occ.role,
                )
            )
        continue
    else:
        ...
```

Then implement:

```python
def _resolve_pending_references(self) -> None:
    remaining: list[PendingReference] = []
    for pending in self._pending_references:
        if pending.target_qualified_name:
            target_sid = f"{pending.target_file}::{pending.target_qualified_name}"
        else:
            target_sid = f"{pending.target_file}::{pending.target_name}"

        if target_sid not in self._id_to_index:
            found = self._find_symbol_in_file(pending.target_file, pending.target_name)
            if found is None:
                remaining.append(pending)
                continue
            target_sid = found

        if pending.source_symbol_id == target_sid:
            continue

        self._add_edge(
            pending.source_symbol_id,
            target_sid,
            EdgeData(
                kind=EdgeKind.REFERENCES,
                file=pending.file,
                range=pending.range,
                role=pending.role,
            ),
            pending.file,
        )

    if remaining:
        logger.warning("Unresolved project-local references: %d", len(remaining))
    self._pending_references = remaining
```

In a correct multi-pass build, most legitimate project-local references should
resolve immediately and never become pending. The pending list is a safety net.

### 3.7 Preserve the Old `_index_file()` Carefully

Once `build()` is multi-pass, `_index_file()` should not be used for normal
full graph construction. It can remain for `update_file()` only until Phase 4.

Update its docstring to make this clear:

```python
def _index_file(...):
    """Legacy single-file index path used only by old incremental update code."""
```

After Phase 4, you may remove `_index_file()` or make it call the split helpers
for a known affected file set.

### Phase 3 Validation

Run:

```bash
PYTHONPATH=src pytest src/tyo3/tests/test_graph_semantics.py -q
PYTHONPATH=src pytest src/tyo3/tests/test_graph_build.py src/tyo3/tests/test_graph_queries.py -q
```

Manual sanity check:

```bash
python - <<'PY'
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
- No project-local symbol becomes external just because its file was processed
  later.

## Phase 4: Make `update_file()` Correct

Purpose: stop silently dropping incoming references.

### 4.1 Use the Conservative Correct Implementation

Do not implement a complex incremental invalidation model yet. Replace
`update_file()` with full rebuild semantics while preserving the current
mutating API:

```python
def update_file(self, session: TyO3Session, path: str) -> None:
    """Refresh graph state after a file changes.

    This conservative implementation rebuilds the full graph to preserve
    cross-file references and inheritance relationships. A future incremental
    version must reindex the changed file plus every file that references it.
    """
    fresh = CodeGraph.build(session)
    self._graph = fresh._graph
    self._id_to_index = fresh._id_to_index
    self._file_to_nodes = fresh._file_to_nodes
    self._file_to_edges = fresh._file_to_edges
    self._file_node_ranges = fresh._file_node_ranges
    self._name_prefix_index = fresh._name_prefix_index
    self._diagnostics = fresh._diagnostics
    self._dependency_cache = fresh._dependency_cache
    self._project_files = fresh._project_files
    self._symbols_by_file = fresh._symbols_by_file
    self._pending_references = fresh._pending_references
```

Explanation:

- Rebuilding is slower but correct.
- The method keeps returning `None`, so callers do not need to change.
- The `path` argument remains for API compatibility and future incremental
  implementations.

### 4.2 Add a Regression Test

```python
@needs_native
def test_update_file_preserves_incoming_references() -> None:
    session = get_session("graph_test")
    graph = CodeGraph.build(session)

    user = find_one(graph, file_suffix="models.py", name="User", kind=SymbolKind.CLASS)
    refs_before = graph.references_to(user.symbol_id)
    assert refs_before

    models_path = next(str(path) for path in session.files() if str(path).endswith("models.py"))
    graph.update_file(session, models_path)

    user_after = find_one(graph, file_suffix="models.py", name="User", kind=SymbolKind.CLASS)
    refs_after = graph.references_to(user_after.symbol_id)
    assert refs_after
```

### Phase 4 Validation

Run:

```bash
PYTHONPATH=src pytest src/tyo3/tests/test_graph_update.py src/tyo3/tests/test_graph_semantics.py -q
```

Definition of done:

- Incoming references still exist after updating the target file.
- The `update_file()` docstring no longer claims impossible behavior.

## Phase 5: Fix Overrides and Dependency Edge Semantics

Purpose: make graph relationships mean what their names say.

### 5.1 Fix `OVERRIDES` Traversal

Current code has this shape:

```python
if edge_data.kind != EdgeKind.INHERITS:
    continue
    parent_sid = ...
```

Everything after `continue` is unreachable.

Replace the loop with:

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

Explanation:

- `INHERITS` edges go from child class to parent class.
- BFS walks parent classes.
- `children(parent_sid)` returns methods/classes structurally contained by the
  parent.
- If a child class has a method with the same name as an ancestor method, add
  `child_method --OVERRIDES--> ancestor_method`.

### 5.2 Define Dependency Edge Kinds

Near the top of `graph.py`, add:

```python
DEFAULT_DEPENDENCY_EDGE_KINDS = {
    EdgeKind.REFERENCES,
    EdgeKind.IMPORTS,
    EdgeKind.INHERITS,
    EdgeKind.OVERRIDES,
    EdgeKind.TYPE_OF,
    EdgeKind.RETURNS,
    EdgeKind.INSTANTIATES,
}
```

Then update:

```python
def dependencies(
    self,
    symbol_id: str,
    *,
    kinds: set[EdgeKind] | None = None,
) -> set[str]:
    edge_kinds = DEFAULT_DEPENDENCY_EDGE_KINDS if kinds is None else kinds
    return {
        self._graph[tgt_idx].symbol_id
        for tgt_idx, _data in self._edges_of_kind(symbol_id, edge_kinds)
    }
```

Update `dependents()` similarly:

```python
def dependents(
    self,
    symbol_id: str,
    *,
    kinds: set[EdgeKind] | None = None,
) -> set[str]:
    edge_kinds = DEFAULT_DEPENDENCY_EDGE_KINDS if kinds is None else kinds
    return {
        self._graph[src_idx].symbol_id
        for src_idx, _data in self._edges_of_kind(symbol_id, edge_kinds, incoming=True)
    }
```

For transitive dependencies, do not use `rx.descendants()` on the full graph.
Implement a small filtered DFS:

```python
def transitive_dependencies(
    self,
    symbol_id: str,
    *,
    kinds: set[EdgeKind] | None = None,
) -> set[str]:
    edge_kinds = DEFAULT_DEPENDENCY_EDGE_KINDS if kinds is None else kinds
    seen: set[str] = set()
    stack = list(self.dependencies(symbol_id, kinds=edge_kinds))

    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(self.dependencies(current, kinds=edge_kinds) - seen)

    return seen
```

Mirror this for `transitive_dependents()`.

### 5.3 Add Tests

Use the Phase 0 override and dependency tests. Add:

```python
def test_dependencies_include_references() -> None:
    graph = CodeGraph()
    # Build two function nodes and one REFERENCES edge.
    # Assert dependencies(source) == {target}.
```

### Phase 5 Validation

Run:

```bash
PYTHONPATH=src pytest src/tyo3/tests/test_graph_semantics.py src/tyo3/tests/test_graph_queries.py -q
```

Definition of done:

- `User.save --OVERRIDES--> Base.save` exists.
- `dependencies()` excludes `DEFINES` and `CONTAINS`.
- `dependencies()` includes real semantic references.

## Phase 6: Model Imports Deliberately

Purpose: avoid using arbitrary symbol references as a proxy for import cycles.

This phase can happen after the multi-pass graph is correct.

### 6.1 Current Behavior

`EdgeKind.IMPORTS` exists, but `_resolve_references_via_occurrences()` always
adds `EdgeKind.REFERENCES`, even when `occ.role == ReferenceRole.IMPORT`.

Import cycle functions then aggregate `IMPORTS` and `REFERENCES`, which means
non-import symbol references can influence import-cycle answers.

### 6.2 Add Module-Level `IMPORTS` Edges

When resolving an occurrence:

```python
if role == ReferenceRole.IMPORT:
    self._add_import_edge(file_str, target_file, occ.range)
```

Implement:

```python
def _module_id_for_file(self, file_str: str) -> str:
    return f"{file_str}::<module>"


def _add_import_edge(self, source_file: str, target_file: str, range: Range) -> None:
    source_module = self._module_id_for_file(source_file)
    target_module = self._module_id_for_file(target_file)

    if source_module not in self._id_to_index:
        return

    if target_module not in self._id_to_index:
        # External import. Use or create a package-level external stub.
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
        EdgeData(kind=EdgeKind.IMPORTS, file=source_file, range=range, role=ReferenceRole.IMPORT),
        source_file,
    )
```

Still keep symbol-level `REFERENCES` edges for imported names if useful. The
key is that import-cycle detection should use `IMPORTS`.

### 6.3 Update Import Cycle Algorithms

In `import_cycles()` and `import_cycle_groups()`, change:

```python
dep_kinds = {EdgeKind.IMPORTS, EdgeKind.REFERENCES}
```

to:

```python
dep_kinds = {EdgeKind.IMPORTS}
```

If the old behavior is still useful, add a separate method later called
`module_reference_cycles()`.

### 6.4 Add Tests

Use `fixtures/circular_imports`:

```python
@needs_native
def test_circular_imports_detected_from_import_edges() -> None:
    graph = get_graph("circular_imports")
    cycles = graph.import_cycles()
    assert cycles
    assert any("module_a.py::<module>" in set(cycle) for cycle in cycles)
    assert any("module_b.py::<module>" in set(cycle) for cycle in cycles)
```

Add a unit test showing a plain `REFERENCES` edge does not create an import
cycle.

### Phase 6 Validation

Run:

```bash
PYTHONPATH=src pytest src/tyo3/tests/test_graph_cycles.py src/tyo3/tests/test_file_occurrences.py -q
```

Definition of done:

- Import cycles are based on `IMPORTS`.
- Plain cross-file references do not create import cycles.

## Phase 7: Introduce Build Reports Instead of Silent Degradation

Purpose: callers need to know whether a graph is complete.

Current graph construction catches broad exceptions and logs warnings. That
can return a graph missing whole files or edge classes.

### 7.1 Add Build Failure Models

In `src/tyo3/graph/models.py` or a new `src/tyo3/graph/build.py`:

```python
from typing import Literal

from pydantic import BaseModel, Field


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

### 7.2 Add `build_with_report()`

Keep `CodeGraph.build(session)` for compatibility. Add:

```python
@classmethod
def build_with_report(
    cls,
    session: TyO3Session,
    *,
    strict: bool = False,
) -> tuple[CodeGraph, GraphBuildReport]:
    ...
```

Implementation notes:

- Use the same multi-pass builder.
- On each caught exception, append a `GraphBuildFailure`.
- If `strict=True`, raise after recording the failure.
- `build()` can call `build_with_report(strict=False)` and return only the
  graph.

### 7.3 Add Tests

Use a fake session object whose `document_symbols()` raises for one file.
Assert:

- `build_with_report()` returns a report with one failure.
- `report.complete is False`.
- `strict=True` raises.

### Phase 7 Validation

Run:

```bash
PYTHONPATH=src pytest src/tyo3/tests/test_graph_build.py -q
```

Definition of done:

- Graph partial failures are inspectable.
- Strict build mode exists.
- Existing callers using `CodeGraph.build()` still work.

## Phase 8: Stabilize Symbol Identity and Paths

Purpose: make graph IDs portable and snapshot-friendly.

This is important, but do it after graph semantics are correct. Otherwise, path
normalization will make failing tests harder to interpret.

### 8.1 Decide the Public Path Policy

Recommended policy:

- First-party public paths are project-relative `PurePosixPath`.
- Internal Rust can use absolute paths.
- External paths use an explicit external or stdlib scheme.
- Stable graph IDs must not include `/home/...` absolute paths.

Example IDs:

```text
app.py::<module>
app.py::create_user
models.py::User
external://pydantic::BaseModel
stdlib://builtins::int
```

### 8.2 Add Path Normalization Helpers

Create `src/tyo3/paths.py`:

```python
from pathlib import Path, PurePosixPath


def to_project_relative(root: Path, path: str | Path) -> PurePosixPath:
    absolute = Path(path).resolve()
    relative = absolute.relative_to(root.resolve())
    return PurePosixPath(relative.as_posix())


def normalize_first_party_path(root: Path, path: str | Path) -> str:
    return str(to_project_relative(root, path))
```

Add tests for:

- absolute path inside root
- relative path inside root
- path outside root raises `ValueError`

### 8.3 Apply to Graph IDs

In `CodeGraph.build()`, use:

```python
root = session.root
files = [normalize_first_party_path(root, file_path) for file_path in session.files()]
```

But keep a mapping from public graph path to native absolute path:

```python
native_file_by_graph_file: dict[str, str]
```

Use native paths when calling `session.document_symbols()`,
`session.file_occurrences()`, and `session.check_file()`. Use normalized graph
paths for `SymbolNode.file` and symbol IDs.

This is a careful change. It touches every place where a Rust result path is
matched against a graph path. Build helper functions rather than scattering
`Path.resolve()` calls.

### Phase 8 Validation

Run:

```bash
PYTHONPATH=src pytest src/tyo3/tests/test_graph_semantics.py src/tyo3/tests/test_graph_export.py -q
```

Add a test that copies `fixtures/graph_test` to a temporary directory and
builds a graph there. The sorted node IDs should match the original graph.

Definition of done:

- Graph IDs no longer include absolute first-party paths.
- Same fixture in two absolute directories produces the same first-party IDs.
- External symbols remain explicitly external.

## Phase 9: Harden Native Diagnostics and Navigation Identity

Purpose: reduce native boundary fragility and reduce Python-side guessing.

### 9.1 Avoid Panics in Diagnostic Conversion

Current `rust/src/convert/diagnostics.rs` uses:

```rust
let file = span.expect_ty_file();
```

This can panic if a diagnostic span is not a ty file span.

Find whether the span type has a non-panicking method. Search the Rust docs or
local crate source:

```bash
rg "fn .*ty_file|expect_ty_file|struct Span|enum Span" rust ~/.cargo/git/checkouts
```

Use the non-panicking API if available:

```rust
let Some(file) = span.ty_file() else {
    return (None, None);
};
```

If no non-panicking API exists, isolate the assumption and add a comment. Do not
let arbitrary panics cross the PyO3 boundary if avoidable.

### 9.2 Improve Navigation Target Identity

Current `rust/src/convert/navigation.rs` returns:

```rust
symbol: None,
module_name: None,
```

This forces the Python graph to reconstruct identity from file, range, and
names. The batch occurrence API already returns better identity for graph
construction, so this is less urgent, but navigation should eventually return
symbol details when ty exposes them.

Task:

- Investigate `ty_ide::NavigationTarget`.
- If it exposes symbol name/kind/module information, populate `SymbolDto`.
- If it does not, document the limitation in code and tests.

### Phase 9 Validation

Run:

```bash
build
PYTHONPATH=src pytest src/tyo3/tests/test_check_file.py src/tyo3/tests/test_rust_integration.py -q
```

Definition of done:

- Diagnostics conversion does not use avoidable panicking APIs.
- Navigation identity limitations are explicit.

## Phase 10: Clean Dependency Cache Serialization

Purpose: fix smaller correctness issues in cached dependency graphs.

### 10.1 Fix `symbols_of_kind()`

Current code compares `node.kind` (`SymbolKind`) to a `str`.

Change:

```python
def symbols_of_kind(self, kind: SymbolKind | str) -> list[SymbolNode]:
    expected = kind if isinstance(kind, SymbolKind) else SymbolKind(kind)
    return [
        self.graph[idx]
        for idx in self.graph.node_indices()
        if self.graph[idx].kind == expected
    ]
```

Import `SymbolKind`.

### 10.2 Fix `EdgeData.role` Deserialization

Current load code uses:

```python
role=raw_edge.get("role")
```

Change to:

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

### 10.3 Add Tests

Add a round-trip test with a role:

```python
def test_dependency_graph_edge_role_roundtrips(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("tyo3.graph.dependency.CACHE_DIR", tmp_path)
    # Build graph with an EdgeData(role=ReferenceRole.READ), save, load,
    # then export to JSON and assert role == "read".
```

### Phase 10 Validation

Run:

```bash
PYTHONPATH=src pytest src/tyo3/tests/test_graph_dependency.py src/tyo3/tests/test_graph_export.py -q
```

Definition of done:

- Loaded dependency graph roles are `ReferenceRole`, not raw strings.
- JSON export works after loading cached dependency graphs.

## Phase 11: Static Gates and CI

Purpose: prevent regressions after the correctness work lands.

### 11.1 Fix Ruff Diagnostics

Run:

```bash
ruff check . --fix
```

Then handle remaining warnings manually. Known categories from review:

- import ordering
- unused imports
- duplicate `import pytest`
- unused locals
- `zip()` without `strict=`
- blind `pytest.raises(Exception)`

For `zip(new_nodes, indices)`, use:

```python
for node, idx in zip(new_nodes, indices, strict=True):
    ...
```

### 11.2 Make `ty check src` a Later Gate

`ty check src` currently reports many diagnostics in tests and around the native
extension. Do not block graph correctness on making all tests statically clean.

Recommended sequence:

1. Make `ruff check .` clean.
2. Exclude or configure generated/native extension import patterns.
3. Gradually clean `ty check src`.
4. Add `ty check src` to CI only once it is green.

### 11.3 Update GitHub Actions

Current CI manually runs `cargo build --release` and copies a hardcoded
extension filename. Replace that with the same workflow developers use.

Suggested CI shape:

```yaml
- name: Build native extension
  run: devenv shell -- build-release

- name: Run tests
  run: devenv shell -- pytest src/tyo3/tests/ -q --tb=short

- name: Ruff
  run: devenv shell -- ruff check .
```

Add `ty check src` only after it is clean.

### Phase 11 Validation

Run:

```bash
ruff check .
build-release
PYTHONPATH=src pytest src/tyo3/tests/ -q
```

Definition of done:

- CI no longer hardcodes the CPython/Linux extension filename.
- CI uses maturin/devenv to build the extension.
- `ruff check .` passes locally and in CI.

## Phase 12: Optional DTO Boundary Simplification

Purpose: simplify maintenance of the Rust/Python transport layer.

This is not required before graph correctness. Do this only after the earlier
phases are stable.

### 12.1 Current Boundary

Rust DTOs are PyO3 classes with custom `PyFields` metadata. Python converts
them using string tags such as:

```text
str
obj
opt:obj
list:obj
```

This works, but every new DTO requires updates in multiple places:

- Rust struct
- PyO3 class registration
- `PyFields` derive
- enum registry
- Python converter tests
- Pydantic model

### 12.2 Target Boundary

Keep Rust DTO structs, but return plain Python dict/list/scalar values using
serde conversion:

```text
Rust DTO -> serde -> Python dict/list -> Pydantic model
```

This lets Python delete most of `_to_python()` and the native DTO classes.

### 12.3 Implementation Sketch

Add a Rust helper, either using a serde-to-Python crate compatible with PyO3
0.28 or a local `serde_json::Value` converter.

Native method shape:

```rust
fn document_symbols<'py>(
    &self,
    py: Python<'py>,
    path: &str,
) -> PyResult<Py<PyAny>> {
    let symbols = self.inner_document_symbols(path)?;
    to_py(py, &symbols)
}
```

Python wrapper shape:

```python
raw = self._inner.document_symbols(str(path))
return [Symbol.model_validate(item) for item in raw]
```

### 12.4 Validation

Rewrite native bridge tests:

- Native methods return `list`/`dict`, not native DTO objects.
- Pydantic validates returned data.
- Enum strings exactly match Python enum values.
- `_native_impl` exports `TyProject` and exception classes only.

## Final Definition of Done

The refactoring is complete when all of these are true:

- `CodeGraph.build()` is multi-pass and deterministic.
- References inside functions/methods attach to those functions/methods.
- Project-local references are never externalized due to file order.
- `update_file()` preserves cross-file references.
- `OVERRIDES` edges exist for `User.save -> Base.save`.
- `dependencies()` excludes structural edges by default.
- Import cycles use `IMPORTS`, not arbitrary `REFERENCES`.
- Columns beyond the current line raise `PositionError`.
- Public exception mapping is consistent for bad paths and bad positions.
- Dependency graph cache round-trips roles correctly.
- `ruff check .` passes.
- CI builds the native extension with maturin/devenv, not a hardcoded `.so`
  copy.
- Exact semantic graph tests exist and pass.

## Suggested PR Order

1. Regression tests for graph semantics and coordinate validation.
2. Rust coordinate validation fix.
3. Python exception mapping fix.
4. Multi-pass graph build.
5. Conservative `update_file()` rebuild.
6. Override edge fix and dependency edge filtering.
7. Import edge modeling.
8. Build report / strict mode.
9. Path normalization.
10. Dependency cache fixes.
11. Ruff and CI cleanup.
12. Optional DTO serde simplification.

Do not skip directly to DTO or package cleanup. The graph answers must become
true first.
