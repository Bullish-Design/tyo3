# TyO3 Code Review V2

## Overview

~4,450 lines Python + ~1,030 lines Rust. A PyO3-bridged wrapper around ty (Astral's type checker) exposing type checking, symbol discovery, navigation, and hover as a Pydantic-validated Python API. Well-structured, well-tested, and clearly designed with SCIP codegraph intelligence as the trajectory. This is genuinely impressive work for a v0.1.

---

## Architecture Assessment

**What works well:**
- Clean three-layer architecture: `TyO3Session` (user API) -> `RustProject` (Python bridge) -> `PyTyProject` (Rust/PyO3)
- The Rust DTO layer (`dto/`) separated from conversion logic (`convert/`) is a solid pattern that will scale
- Exception hierarchy maps Rust errors to typed Python exceptions cleanly
- The `Mutex<Option<TyProjectState>>` pattern in Rust is correct for lifecycle management
- Comprehensive test pyramid: unit, integration, snapshot, property-based, performance

---

## Critical Issues

### 1. `check_file()` runs a full project check every time

**Location:** `src/tyo3/session.py:73-87`

`check_file()` calls `self._rp.check()` (full project check), then filters in Python. For the SCIP real-time use case, this is a dealbreaker. Every keystroke-triggered file check re-checks the entire project.

**Fix:** Push the file filter into Rust. ty's Salsa caching helps on repeated calls, but the Python-side filtering still deserializes all diagnostics just to throw most away. At minimum, consider caching `check()` results on `RustProject` so repeated `check_file()` calls don't re-serialize.

### 2. Diagnostics lose file/range from Rust

**Location:** `rust/src/convert/diagnostics.rs:15-16`

`file` and `range` are hardcoded to `None`:
```rust
file: None,
range: None,
```

The comment says "File/range information is attached to annotations, not the diagnostic itself" -- but ruff_db `Diagnostic` does carry annotations with spans. You're discarding the most critical information for a code editor: *where* the error is. This renders diagnostics nearly useless for SCIP integration since you can't map them to file locations.

### 3. `_closed` flag not checked on operations in `RustProject`

**Location:** `src/tyo3/rust_project.py:156`

`RustProject` tracks `self._closed` but never checks it in `files()`, `check()`, `document_symbols()`, etc. It relies entirely on the Rust side's `lock_state()` check. While this works, it means:
- A Python call to a closed project still pays the JSON serialization overhead attempt before failing
- The Python `_closed` flag and the Rust `Option<None>` can diverge (e.g., if `close()` raises on the Rust side)

### 4. `reload()` has a race condition in Rust

**Location:** `rust/src/project.rs:200-221`

`reload()` drops the guard, sets state to `None`, then re-acquires the lock:
```rust
*guard = None;
drop(guard);
// ...window where another thread sees None...
let mut guard = self.inner.lock()...
*guard = Some(TyProjectState { db, root });
```
Between `drop(guard)` and re-acquisition, any concurrent caller sees a closed project. The README says "not thread-safe" but the Mutex implies it should be. Either remove the Mutex (since it's single-threaded) or fix the reload to hold the lock throughout.

---

## Significant Issues

### 5. `navigate_to_targets` swallows the `RangedValue` wrapper

**Location:** `rust/src/project.rs:99-106`

The `RangedValue<NavigationTargets>` from ty_ide gives you the source range that was navigated *from* (useful for highlighting). You unwrap it and discard the outer range. For SCIP, you'll want this.

### 6. Redundant `LineIndex` computation in `position_to_offset`

**Location:** `rust/src/coordinates.rs:16`

`position_to_offset` always builds a fresh `LineIndex`. But callers like `navigate_to_targets` already have the source text. In `hover()`, `find_references()`, and the three `goto_*` methods, you compute the LineIndex here, then potentially compute it *again* in `convert_navigation_target` for the same file. Pass the LineIndex in.

### 7. Semantic mismatch: `selection_range` vs `full_range` in navigation

**Location:** `rust/src/convert/navigation.rs:22-27`

```rust
range: coordinates::range_to_dto_with_index(..., target.focus_range()),
selection_range: Some(coordinates::range_to_dto_with_index(..., target.full_range())),
```
LSP convention is `range = full_range` and `selectionRange = name_range` (the focused/highlighted part). You have it backwards -- `range` is the focus range and `selection_range` is the full range. The Python-side `DefinitionTarget` model doesn't document which is which, so consumers will misinterpret this.

### 8. `Diagnostic.file` is typed as `ProjectFile | None` but always `None` from Rust

**Location:** `src/tyo3/models/analysis.py:84`

The `Diagnostic` model accepts `file: ProjectFile | None`, where `ProjectFile` requires a full `TyProject` embedded object (with `root`, `status`, `opened_at`, etc.). This is never populated from the Rust backend. The model is over-specified for what the backend actually provides, and the `ProjectFile` -> `TyProject` chain creates a deep nesting that doesn't serve the wire format.

### 9. Unused models cluttering the API surface

**Location:** `src/tyo3/models/core.py`

`TyProject`, `ProjectFile`, `TyProjectConfig`, `BackendInfo`, `CoordinateMode`, `FileCategory`, and `ProjectStatus` are defined. Of these, only `ProjectFile` is used (in `Diagnostic.file` and `SemanticToken.file`) -- and even those are always `None` from the Rust backend. `TyProjectConfig` and `BackendInfo` aren't referenced anywhere except `conftest.py` tests. These are spec-driven models that don't yet have a backend implementation.

### 10. `models/__init__.py` uses `from X import *` with no `__all__` guards

**Location:** `src/tyo3/models/__init__.py:3-7`

Wildcard re-exports from 5 submodules. Each submodule does define `__all__`, so this is technically controlled, but the `models/__init__.py` `__all__` list (line 9-39) includes `"TyProject"` which shadows the meaning (it's the Pydantic model, not the Rust class). Confusing for users who `from tyo3.models import TyProject` thinking they get the session object.

---

## Minor Issues

### 11. `workspace_symbols` minimum query length differs between layers

**Location:** `src/tyo3/session.py:97-98`

`TyO3Session.workspace_symbols()` returns `[]` for `len(query) < 1` (empty string). But `RustProject.workspace_symbols()` has no such guard. And ty_ide itself may have its own minimum. The validation should be consistent and documented.

### 12. `_string_path_to_typath` is a one-liner that could be inlined

**Location:** `src/tyo3/rust_project.py:98-100`

```python
def _string_path_to_typath(path_str: str) -> PurePosixPath:
    return PurePosixPath(path_str)
```
This adds indirection with no value. It was presumably left over from when a custom `Path` model existed. Same for `_json_position_to_model` and `_json_range_to_model` -- these could use Pydantic's own `model_validate` on the dicts directly.

### 13. `SymbolKind.CLASS_` and `IMPORT_` use trailing underscores

**Location:** `src/tyo3/models/symbols.py:21,31`

`CLASS_ = "class_"`, `IMPORT_ = "import_"`. The serde rename in Rust (`#[serde(rename = "class_")]`) preserves the underscore in JSON. Python consumers must use `SymbolKind.CLASS_` rather than a more natural name. Consider aliasing or renaming the JSON wire format.

### 14. Demo CLI `cyan(f)` and `cyan(t.path)` pass `PurePosixPath` to string formatter

**Location:** `src/tyo3/demo/cli.py:56,89,115`

`cyan()` expects a `str`, but `f`, `s.location.path`, and `t.path` are `PurePosixPath`. This works because f-strings call `__str__()`, but it means the colour function signatures are lying about their types.

### 15. `close()` behavior inconsistency

**Location:** `src/tyo3/rust_project.py:372` vs `rust/src/project.rs:233`

Python `RustProject.close()` is idempotent (line 372: `if self._closed: return`), but Rust `PyTyProject.close()` raises `ProjectClosedError` on double-close. The Python wrapper's `_closed` flag masks the Rust error, which is fine, but means the Rust `close()` error path is dead code that can never be reached through the Python API.

### 16. `__del__` ResourceWarning may fire during interpreter shutdown

**Location:** `src/tyo3/rust_project.py:383-394`

The `__del__` method calls `self.close()` which calls into Rust. During interpreter shutdown, the Rust module may already be unloaded, causing a segfault or silent crash. The `except Exception: pass` catches Python exceptions but not native crashes.

### 17. `devenv.nix` build script copies wrong filename

**Location:** `devenv.nix`

The build script copies `lib_native_impl.so` but the rename to `_native_impl.cpython-313-x86_64-linux-gnu.so` is brittle -- it hardcodes the Python version and platform triple. Using `maturin develop` would handle this automatically.

### 18. Stale `.so` files in source tree

**Location:** `src/tyo3/`

The source tree contains three `.so` files:
```
_native_impl.cpython-313-x86_64-linux-gnu.so
rust_backend.cpython-313-x86_64-linux-gnu.so
tyo3.cpython-313-x86_64-linux-gnu.so
```
Only `_native_impl` is the current one. `rust_backend` and `tyo3` are leftovers from previous naming. These should be in `.gitignore`.

---

## Design Considerations for SCIP

### 19. JSON serialization boundary is the bottleneck

Every Rust -> Python call goes through `serde_json::to_string` -> `json.loads` -> Pydantic model construction. For real-time SCIP updates on every keystroke, this triple-conversion will dominate latency. Consider:
- Returning PyO3 native objects directly (with `#[pyclass]` on the DTOs) instead of JSON
- Or at minimum, using `model_validate(data)` directly on the parsed JSON instead of manual field-by-field construction in `_parse_symbol`, `_parse_definition_target`, etc.

### 20. No incremental/file-scoped analysis

The current architecture is project-global: `check()` checks everything, `reload()` drops everything. For SCIP real-time updates, you'll need:
- File-level invalidation (ty's Salsa DB supports this)
- Streaming/delta diagnostics (only changed files)
- An event/callback mechanism for file watchers

### 21. No SCIP output format support yet

The models are LSP-shaped (which is correct as an intermediate step), but SCIP needs different representations: `Occurrence`, `SymbolInformation`, `Document` with relationships. Planning the SCIP emission layer early will inform whether the current model hierarchy is the right intermediate form.

---

## Testing Assessment

**Strengths:**
- 13 test files, ~2,700 lines of tests (>60% of Python LOC is tests)
- Property-based tests with Hypothesis -- excellent for coordinate conversion edge cases
- Performance benchmarks with explicit thresholds
- Snapshot tests for regression-catching
- Good fixture variety (simple, classes, imports, unicode, standalone, diagnostic_targets)

**Gaps:**
- No tests for the demo CLI (`demo/cli.py`, `demo/runner.py`)
- No tests for `TyO3Session` as a unit (tests go through either pure models or `RustProject` directly)
- The `backends` fixture in `conftest.py:113` is declared but has no implementation (`...`)
- Property-based tests don't cover `check()` or `check_file()`

---

## Summary

| Category | Verdict |
|----------|---------|
| Architecture | Strong foundation; clean separation of concerns |
| Correctness | Critical: diagnostics lose location data; `selection_range` semantics are swapped |
| Performance readiness | JSON serialization boundary will not scale to real-time SCIP |
| API design | Clean, Pythonic, well-documented; some dead models from spec |
| Test coverage | Excellent breadth; missing session-level and demo tests |
| Rust code quality | Solid; proper error handling, good use of ty_ide APIs |
| Production readiness | Pre-alpha; core path (diagnostics with locations) is broken |

**Top 3 priorities:**
1. Extract file/range from diagnostic annotations (issue #2) -- without this, the library's primary value prop doesn't work
2. Fix `selection_range` vs `range` semantics (issue #7) -- silent correctness bug
3. Plan the JSON -> native PyO3 object migration (issue #19) -- architectural decision that shapes everything downstream
