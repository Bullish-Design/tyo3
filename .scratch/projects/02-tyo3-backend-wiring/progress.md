# TyO3 Backend Wiring — Progress

## Status: Phase 3 Complete ✅

### Completed

#### Prerequisites
- [x] `devenv.nix` configured with Rust, Python 3.13, maturin, env vars
- [x] `pyproject.toml` updated with maturin build backend

#### Phase 0 — Rust Feasibility Spike ✅
- [x] `rust/Cargo.toml` with pinned ty/Ruff git dependencies (commit `3cb09eba`)
- [x] `rust/Cargo.lock` generated
- [x] `rust/src/lib.rs` — PyO3 module entry (`_native_impl`)
- [x] `rust/src/project.rs` — PyTyProject class with `open()`, `files()`, `check()`, `document_symbols()`
- [x] `rust/src/coordinates.rs` — Position ↔ TextSize conversion (1-based)
- [x] `rust/src/files.rs` — Path → File handle resolution via `system_path_to_file`
- [x] `cargo check` passes (all deps resolve, builds successfully)

#### Phase 1 — Core DTO Layer ✅
- [x] **4.1 Position and Range DTOs** (`rust/src/dto/coordinates.rs`)
  - `PositionDto` (1-based line/column)
  - `RangeDto` (start + end)
  - `FileRangeDto` (path + range)
- [x] **4.2 Coordinate conversion** (`rust/src/coordinates.rs`)
  - `position_to_offset()` — Python Position → ruff TextSize
  - `range_to_dto()` — ruff TextRange → 1-based RangeDto
  - `char_len_to_byte_offset()` — Unicode codepoint → byte offset
- [x] **4.3 File resolution** (`rust/src/files.rs`)
  - `resolve_file()` — resolves absolute/relative paths to `ruff_db::files::File`
- [x] **4.4 Remaining DTOs**
  - `dto/diagnostics.rs` — `DiagnosticDto`
  - `dto/symbols.rs` — `SymbolDto`
  - `dto/navigation.rs` — `DefinitionTargetDto`, `ReferenceDto`
  - `dto/hover.rs` — `HoverContentKindDto`, `HoverContentDto`, `HoverDto`
  - `dto/mod.rs` — `BackendInfoDto`, `CheckResultDto`
  - `dto/tokens.rs` — placeholder (deferred to v0.2+)
  - `dto/hierarchy.rs` — placeholder (deferred to v0.2+)
- [x] **4.5 DTO Conversion Layer** (`rust/src/convert/`)
  - `convert/diagnostics.rs` — ruff_db `Diagnostic` → `DiagnosticDto`
  - `convert/symbols.rs` — ty_ide `SymbolInfo` parts → `SymbolDto`
  - `convert/navigation.rs` — `NavigationTarget` → `DefinitionTargetDto`, `ReferenceTarget` → `ReferenceDto`
  - `convert/hover.rs` — hover content items → `HoverDto` (with builder + markdown fallback)
  - `convert/mod.rs` — re-exports all converters (now `pub mod`)
- [x] **Error types** (`rust/src/errors.rs`)
  - `PathError`, `PositionError`, `ProjectError`

#### Phase 2 — TyProject Rust Facade ✅
- [x] **Refactored existing methods** to use `convert/` module consistently
  - `check()` now uses `convert::diagnostics::convert_diagnostics()`
  - `document_symbols()` now uses `convert::symbols::convert_symbol()`
- [x] **Lifecycle methods**
  - `reload()` — drops and re-creates ProjectDatabase with same root ✅
  - `close()` — drops ProjectDatabase, subsequent ops raise clear error ✅
- [x] **Position-based methods** wired with `position_to_offset()`
  - `goto_definition(path, line, col)` → `ty_ide::goto_definition` ✅
  - `goto_declaration(path, line, col)` → `ty_ide::goto_declaration` ✅
  - `goto_type_definition(path, line, col)` → `ty_ide::goto_type_definition` ✅
  - `find_references(path, line, col, include_decl)` → `ty_ide::find_references` ✅
  - `hover(path, line, col)` → `ty_ide::hover` (Markdown rendering) ✅
- [x] **Search methods**
  - `workspace_symbols(query)` → `ty_ide::workspace_symbols` ✅
- [x] **maturin build** succeeds and Python import works

#### Phase 3 — PyO3 Python Bindings ✅
- [x] **`src/tyo3/rust_project.py`** — `RustProject` wrapper class
  - Wraps `tyo3._native_impl.TyProject` (the PyO3 class)
  - All methods return Pydantic-validated domain models
  - JSON parsing and Path model conversion
  - Graceful degradation when native extension not built
- [x] **`src/tyo3/exceptions.py`** — Exception hierarchy
  - `TyO3Error`, `ProjectOpenError`, `ProjectClosedError`
  - `PathResolutionError`, `PositionError`, `AnalysisError`, `InternalTyError`
  - Wired into `RustProject` methods with proper message extraction
- [x] **`src/tyo3/models/navigation.py`** — Hover models added
  - `HoverContentKind` (StrEnum: type, signature, docstring, typed_dict_key, markdown, plain_text)
  - `HoverContent` (BaseModel)
  - `HoverResult` (BaseModel with location + contents)
- [x] **`src/tyo3/models/__init__.py`** — Hover model exports
- [x] **`src/tyo3/__init__.py`** — Native extension import with graceful fallback
- [x] **Native extension packaging** — `.so` placed in `src/tyo3/_native_impl.cpython-*.so`
- [x] **Service layer** — Updated with optional `use_rust` parameter
  - `ProjectService`: stores `RustProject` instances, delegates file listing and lifecycle
  - `AnalysisService`: delegates `check()` to RustProject when `use_rust=True`
  - `SymbolService`: delegates `document_symbols()` and `workspace_symbols()` to RustProject when `use_rust=True`
  - `NavigationService`: delegates all navigation/hover operations to RustProject when `use_rust=True`
  - Default `use_rust=False` preserves backward compatibility with all existing tests

##### End-to-end test results (all passing)
| Test | Result |
|---|---|
| `files()` | ✅ 1 file found |
| `document_symbols()` | ✅ 5 symbols (greet, MyClass, __init__, get_val, result) |
| `workspace_symbols("greet")` | ✅ 1 result (Function: greet) |
| `goto_definition()` | ✅ 1 target found |
| `find_references()` | ✅ Returns results |
| `hover()` | ✅ Markdown with signature + docstring |
| `reload()` | ✅ Files still accessible after reload |
| `close()` | ✅ Subsequent ops raise `ProjectClosedError` |
| Existing test suite (75 tests) | ✅ All pass with `use_rust=False` (default) |

##### Build instructions
```bash
# Inside devenv shell:
cd rust && maturin develop
# Copy the .so into the Python package:
cp .devenv/state/venv/lib/python3.13/site-packages/_native_impl/_native_impl.cpython-*.so src/tyo3/
# Or use the convenience command:
cd rust && cargo build && cp target/debug/lib_native_impl.so ../src/tyo3/_native_impl.cpython-313-x86_64-linux-gnu.so
```

### Next (Phase 4 — Python Service Integration)
1. Create fixture projects under `fixtures/`
2. Add integration tests that exercise `use_rust=True` code paths
3. Snapshot tests for symbol/document_symbols output
4. Coordinate conversion edge case tests

### Future Phases
- **Phase 4**: Python service integration tests with real fixtures
- **Phase 5**: Full testing suite, CI integration, release build

### Known Limitations
- `all_symbols` not callable (QueryPattern not publicly exported from ty_ide)
- Hover content flattened to Markdown (Hover/HoverContent types not publicly re-exported)
- No GIL release during Rust operations (Salsa single-threaded)
- Native extension must be manually copied to `src/tyo3/` after build
- `PYTHONPATH=src` required when running tests with the native extension
