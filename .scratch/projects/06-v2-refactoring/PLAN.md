# Plan: PyO3 Native Object Migration

## Context

TyO3 wraps the ty type checker via PyO3, providing a Pydantic-validated Python API for SCIP codegraph intelligence. The current Rust-Python boundary serializes everything as JSON (`serde_json::to_string` -> `json.loads` -> manual dict traversal -> Pydantic construction). This triple-conversion is the performance bottleneck for real-time SCIP updates.

**Goal:** Replace JSON serialization with native PyO3 objects at the boundary, then use Pydantic v2's `from_attributes=True` to construct Pydantic models directly from native objects. Pydantic models remain the public API — PyO3 objects are fast internal transport.

**Architecture after migration:**
```
Rust (ty) ──#[pyclass] objects──> RustProject ──model_validate()──> Pydantic models (public API)
```

## Prerequisites (do these first, in order)

### Step 1: Fix selection_range vs range semantics (Issue #7)

The range mapping is inverted from LSP convention in two places.

**File: `rust/src/convert/navigation.rs:20-27`**
- Currently: `range` <- `focus_range()`, `selection_range` <- `full_range()`
- Fix: swap them. `range` <- `full_range()`, `selection_range` <- `focus_range()`

**File: `rust/src/convert/symbols.rs:41-52`**
- Currently: `location.range` <- `name_range`, `selection_range` <- `full_range`
- Fix: swap them. `location.range` <- `full_range`, `selection_range` <- `name_range`

**Update affected tests** in `test_rust_integration.py`, `test_rust_snapshots.py` — snapshot assertions for range values will shift.

### Step 2: Fix diagnostics missing file/range (Issue #2)

**File: `rust/src/convert/diagnostics.rs`**
- Currently hardcodes `file: None, range: None`
- Investigate `ruff_db::diagnostic::Diagnostic` annotation API at pinned commit `3cb09eba6`
- Extract file path and text range from annotations
- If annotation API is not accessible, try per-file checking via `ty_ide` as alternative
- Update `DiagnosticDto` fields accordingly

**File: `src/tyo3/rust_project.py:170-184`** — Python side already handles non-None file/range, just needs the data.

## Migration (after prerequisites)

### Step 3: Add `#[pyclass]` to all Rust DTOs

All DTOs in `rust/src/dto/` get `#[pyclass(frozen)]` with `#[pyo3(get)]` on fields.

**3a: Leaf types** — `rust/src/dto/coordinates.rs`
- `PositionDto` -> `#[pyclass(name = "NativePosition", frozen)]` with `#[pyo3(get)]` on `line`, `column`
- `RangeDto` -> `#[pyclass(name = "NativeRange", frozen)]`
- `FileRangeDto` -> `#[pyclass(name = "NativeFileRange", frozen)]`
- Add `__repr__`, `__eq__`, `__hash__` via `#[pymethods]`
- Use `Native` prefix to avoid name collisions with Pydantic models at the Python import level

**3b: Enums** — PyO3 0.23 supports `#[pyclass]` on C-like enums
- `SymbolKindDto` in `dto/symbols.rs`
- `SeverityDto` in `dto/diagnostics.rs`
- `ReferenceKindDto` in `dto/navigation.rs`
- `HoverContentKindDto` in `dto/hover.rs`
- Add `__str__` returning the snake_case string for display/comparison with Pydantic StrEnums

**3c: Composite structs** — `SymbolDto`, `DiagnosticDto`, `DefinitionTargetDto`, `ReferenceDto`, `HoverContentDto`, `HoverDto`, `CheckResultDto`
- All get `#[pyclass(frozen)]` with `#[pyo3(get)]`
- `Vec<T>` fields (e.g., `CheckResultDto.diagnostics`) work automatically — PyO3 converts to Python list

**3d: Register in module** — `rust/src/lib.rs`
- Add `m.add_class::<dto::PositionDto>()?;` etc. for every type and enum

**Keep serde derives** alongside pyclass — serde is still useful for testing/debugging and costs nothing if unused at runtime.

### Step 4: Change Rust return types from `String` to native objects

**File: `rust/src/project.rs`** — Every method changes:

| Method | Before | After |
|--------|--------|-------|
| `check()` | `PyResult<String>` | `PyResult<CheckResultDto>` |
| `document_symbols()` | `PyResult<String>` | `PyResult<Vec<SymbolDto>>` |
| `workspace_symbols()` | `PyResult<String>` | `PyResult<Vec<SymbolDto>>` |
| `goto_definition()` | `PyResult<String>` | `PyResult<Vec<DefinitionTargetDto>>` |
| `goto_declaration()` | `PyResult<String>` | `PyResult<Vec<DefinitionTargetDto>>` |
| `goto_type_definition()` | `PyResult<String>` | `PyResult<Vec<DefinitionTargetDto>>` |
| `find_references()` | `PyResult<String>` | `PyResult<Vec<ReferenceDto>>` |
| `hover()` | `PyResult<Option<String>>` | `PyResult<Option<HoverDto>>` |

Delete all `serde_json::to_string()` calls in these methods.

### Step 5: Add `from_attributes=True` to Pydantic models

**Files: `src/tyo3/models/analysis.py`, `navigation.py`, `symbols.py`, `advanced.py`**

Add `model_config = ConfigDict(from_attributes=True)` to every model that will be constructed from native objects: `Position`, `Range`, `FileRange`, `CheckResult`, `Diagnostic`, `Symbol`, `DefinitionTarget`, `Reference`, `HoverContent`, `HoverResult`.

This lets `Position.model_validate(native_position_obj)` read `.line`, `.column` directly from the PyO3 object's `#[pyo3(get)]` attributes.

### Step 6: Rewrite `RustProject` to use `model_validate` instead of JSON

**File: `src/tyo3/rust_project.py`**

Replace JSON parsing with direct Pydantic validation:

```python
# Before
raw_json: str = self._inner.document_symbols(str(path))
return [self._parse_symbol(s) for s in json.loads(raw_json)]

# After
native_symbols = self._inner.document_symbols(str(path))
return [Symbol.model_validate(s) for s in native_symbols]
```

**Delete:** `import json`, `_string_path_to_typath()`, `_json_position_to_model()`, `_json_range_to_model()`, `_parse_symbol()`, `_parse_definition_target()`, and all manual dict field extraction.

**Keep:** Exception wrapping (catching `_NativeClosedError` etc. and re-raising as Python exceptions).

### Step 7: Update tests

**Tests that need changes:**
- `test_rust_integration.py` — If enum comparisons use string values (`s.kind == "function"`), update to use `SymbolKind.FUNCTION` or compare via `str(s.kind)`
- `test_rust_snapshots.py` — The `symbol_to_dict()` helper accesses `.location.range.start.line` etc. which still works via Pydantic models. Any `model_fields` introspection tests need updating.
- `test_check_file.py` — Update if `Diagnostic.file` type changes from `ProjectFile | None` to `str | None`

**Tests that should pass unchanged:**
- `test_models_*.py` — These test Pydantic models in isolation
- `test_rust_performance.py` — Times operations, return types still work
- `test_property_based.py` — Uses session-level API
- `test_exceptions.py`, `test_invariants.py` — No boundary changes

**New tests to add:**
- Verify native objects have correct `__repr__`, `__eq__`, `__hash__`
- Verify `model_validate(native_obj)` roundtrip works for every model
- Verify frozen native objects reject mutation

### Step 8: Clean up models

- Evaluate unused spec models in `core.py` (`TyProject`, `ProjectFile`, `BackendInfo`, `TyProjectConfig`) — if they don't serve the SCIP layer, remove or move to a `_spec` module
- Update `Diagnostic.file` type to match what the Rust backend actually provides (once issue #2 is resolved)
- Update `models/__init__.py` `__all__` to reflect reality

## Verification

1. `build` — Rust extension compiles with all new `#[pyclass]` types
2. `test-rust` — Integration tests pass with native objects
3. `test-quick` — Pydantic model unit tests still pass
4. `test-property` — Property-based tests pass
5. `test-perf` — Performance benchmarks show improvement (especially for large symbol sets)
6. `check-so` — Quick smoke test of the native extension
7. Manual: `python -c "from tyo3 import TyO3Session; ..."` — verify public API works end-to-end
