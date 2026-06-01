# TyO3 Known Limitations

> **Version:** v0.2.0
> **Last updated:** 2026-06-01 (Phase 3 complete)
> **Related:** `RUST_BACKEND_IMPLEMENTATION.md` (v0.2.0), `progress.md`

This document tracks known limitations, upstream API gaps, and design trade-offs discovered during the TyO3 Rust backend wiring. Each limitation includes severity, the affected Allium spec rule(s), and the path to resolution.

---

## 1. `all_symbols` Not Callable (Blocker)

**Severity:** Blocker for `tyo3-symbols.allium` → `SearchAllSymbols`

**Root cause:** `ty_ide::all_symbols()` requires a `QueryPattern` argument, but `QueryPattern` is defined in a private module (`mod symbols`) and is **not re-exported** from the `ty_ide` crate. External crates cannot name or construct a `QueryPattern`.

**Affected spec rules:**
- `tyo3-symbols.allium` → `SearchAllSymbols`

**Upstream locations:**
- `QueryPattern` defined: `crates/ty_ide/src/symbols.rs:26` (`pub struct QueryPattern`)
- `all_symbols` exported: `crates/ty_ide/src/lib.rs` → `pub use all_symbols::{AllSymbolInfo, all_symbols};`
- `QueryPattern` NOT exported: missing from `pub use symbols::{...}` line in lib.rs

**Workaround:** Not possible from outside the crate. The Python `all_symbols()` stub returns empty results.

**Resolution paths:**
1. **Upstream fix (preferred):** Add `QueryPattern` to the `pub use symbols::{...}` re-export line in `crates/ty_ide/src/lib.rs`. This is a one-line change upstream.
2. **Fork:** Fork the Ruff repo at the pinned commit, apply the re-export, and point Cargo.toml at the fork.
3. **Vendored copy:** Vendor the `symbols.rs` module and build `QueryPattern` from vendored types.

---

## 2. Hover Content Not Structured (Non-Blocker)

**Severity:** Non-blocker — functionally equivalent, less structured

**Root cause:** `ty_ide::hover()` returns `Option<RangedValue<Hover<'_>>>` where both `Hover` and `HoverContent` are defined in a private module (`mod hover`) and are **not re-exported** from `ty_ide`. While the `hover()` function is callable and returns a usable value, the inner types cannot be named, pattern-matched, or have their public methods called from outside the crate.

**Affected spec rules:**
- `tyo3-navigation.allium` → `GetHover`

**Current behaviour:** The entire hover is rendered as a single Markdown string via `Hover::display(db, MarkupKind::Markdown)` and returned as one `HoverContentDto` of kind `markdown`. Users get all the information (signature, type, docstring) but as a flat formatted string rather than structured per-content-item data.

**What is lost:**
- Per-content-item kind discrimination (signature vs type vs docstring vs typed_dict_key)
- The Python model `HoverContent.kind` field always shows `"markdown"`
- Cannot programmatically extract only the type or only the docstring

**Upstream locations:**
- `Hover` / `HoverContent` defined: `crates/ty_ide/src/hover.rs`
- `hover` function exported: `crates/ty_ide/src/lib.rs` → `pub use hover::hover;`
- `Hover` / `HoverContent` NOT exported: no corresponding `pub use hover::{...}`

**Resolution paths:**
1. **Upstream fix (preferred):** Add `Hover`, `HoverContent` to `pub use hover::{...}` in lib.rs.
2. **Accept for v0.1:** Markdown rendering is functionally adequate. Move to v0.2+.
3. **Debug parsing hack:** Since `HoverContent` derives `Debug`, parse variant names from `format!("{:?}", content)`. Fragile and not recommended.

---

## 3. No GIL Release During Rust Operations (Non-Blocker)

**Severity:** Non-blocker for v0.1 — acceptable for initial release

**Root cause:** The Salsa incremental computation framework used by `ProjectDatabase` is inherently single-threaded. Internally, it uses `RefCell<salsa::active_query::QueryStack>` and `UnsafeCell<HashMap<...>>`, making the entire database `!Send + !Sync`. This means a `&TyProjectState` (which contains `ProjectDatabase`) cannot be sent across thread boundaries, and `py.allow_threads()` cannot be used.

**Impact:** All Rust operations hold the Python GIL. A long-running `check()` or `find_references()` call blocks the Python interpreter. For an LSP server use case (future), this means the server cannot process other requests while a type-check is in flight.

**Why the implementation guide recommended `allow_threads`:** The guide was written before the actual Salsa internals were inspected. It assumed `ProjectDatabase` was thread-safe.

**Resolution paths:**
1. **Accept for v0.1:** Type-checking is fast enough for interactive use. The GIL is released during Python-level I/O and network operations anyway.
2. **Snapshot-based parallelism (v0.2+):** Take a Salsa snapshot (`db.snapshot()`), release the GIL, run the query on the snapshot in a background thread. Snapshots are read-only and may be `Send` depending on the Salsa version.
3. **Upstream Salsa change:** Salsa 3.0+ may support `Send + Sync` storage. Track salsa releases.

---

## 4. Module Naming: `_native_impl` Submodule (Resolved)

**Severity:** Resolved — design decision documented for reference

**Original plan (v0.1):** 
- PyO3 module: `rust_backend`
- maturin `module-name`: `tyo3.rust_backend`
- Python import: `from tyo3 import rust_backend`

**What worked in Phase 2:**
- PyO3 module: `tyo3`
- maturin `module-name`: `tyo3`
- Issues arose in Phase 3: the native `tyo3` module shadowed the Python `tyo3` package.

**What works in Phase 3:**
- PyO3 module: `_native_impl`
- Cargo lib name: `_native_impl`
- maturin `module-name`: `tyo3._native_impl`
- Python import: `from tyo3 import _native_impl` or `from tyo3._native_impl import TyProject`
- The compiled `.so` is manually copied to `src/tyo3/_native_impl.cpython-313-x86_64-linux-gnu.so`

**Why:** maturin's editable install (`maturin develop`) does not correctly handle mixed Python/Rust layouts where the Python source lives under `src/` and the native module should be a subpackage. It installs the native module at the top level of site-packages regardless of `python-source` setting. The workaround is to copy the `.so` directly into the Python package directory.

**Impact:**
- All tests require `PYTHONPATH=src` so Python finds `src/tyo3/` instead of any installed package.
- The native `.so` must be rebuilt and copied after Rust changes.
- The build step: `cd rust && cargo build && cp target/debug/lib_native_impl.so ../src/tyo3/_native_impl.cpython-313-x86_64-linux-gnu.so`

---

## 5. Error Types Partially Resolved (Phase 3)

**Severity:** Cosmetic — dead code warnings remain in Rust

**Affected types:** `PathError`, `PositionError`, `ProjectError` in `rust/src/errors.rs`

**Status:** Phase 3 implemented Python-side exception mapping (`tyo3/exceptions.py`) with `ProjectOpenError`, `ProjectClosedError`, `PathResolutionError`, `PositionError`, `AnalysisError`, `InternalTyError`. The `RustProject` wrapper maps Rust `PyRuntimeError` messages to these Python exception classes by inspecting error message strings. However, the Rust-side `PathError`/`PositionError`/`ProjectError` types remain unused — all Rust errors are still raised as `PyRuntimeError::new_err()`.

**Resolution:** A future improvement would be to convert Rust error types into specific PyO3 error classes (via `#[pyclass]` and `From` impls), allowing Python-side `except` clauses to catch specific errors without string inspection.

---

## 6. BackendInfoDto Unused (Deferred)

**Severity:** Cosmetic — dead code warning

**Status:** `BackendInfoDto` is defined in `rust/src/dto/mod.rs` but not constructed. The Python `ProjectService.query_backend_info()` hardcodes version constants and does not query Rust.

**Resolution:** If runtime backend version queries are desired (Phase 3+), add a `backend_info()` method to `PyTyProject` that constructs and returns a `BackendInfoDto`.

---

## 7. Maturin Mixed Layout Not Working (Phase 3)

**Severity:** Developer experience — requires manual build step

**Root cause:** maturin's editable install (`maturin develop`) does not correctly handle mixed Python/Rust layouts when `python-source = "src"` and the native module should be a subpackage like `tyo3._native_impl`. The native `.so` is installed at the top level of site-packages instead of inside the `tyo3` package.

**Workaround:** The `.so` is manually copied from the build output to `src/tyo3/_native_impl.cpython-313-x86_64-linux-gnu.so`. Import works via `from tyo3 import _native_impl` when `PYTHONPATH=src`.

**Build command:**
```bash
cd rust && cargo build
cp target/debug/lib_native_impl.so ../src/tyo3/_native_impl.cpython-313-x86_64-linux-gnu.so
```

**Resolution paths:**
1. **Fix maturin config:** Investigate maturin 1.12.x mixed layout support more thoroughly.
2. **Switch to separate build:** Use `cargo build` + manual copy as the standard build flow.
3. **Use `maturin build` + pip install:** Create a wheel and install it (requires fixing the platform tag issue).

---

## 8. `use_rust=False` Default for Backward Compatibility (Phase 3)

**Severity:** Non-blocker — intentional design trade-off

**Status:** All service classes (`ProjectService`, `AnalysisService`, `SymbolService`, `NavigationService`) accept a `use_rust: bool = False` constructor parameter. When `False` (default), they use existing black-box stubs. When `True`, they delegate to `RustProject`.

**Why:** The existing test suite (75 tests) uses abstract `Path` models that don't correspond to real filesystem paths. Making `use_rust=True` the default would break all tests. The current design allows gradual migration.

**Resolution:** Phase 4 will add integration tests with real fixture projects and `use_rust=True`. At that point, the default can be flipped or the black-box stubs can be removed.

---

## 9. `PYTHONPATH=src` Required for Development (Phase 3)

**Severity:** Developer experience — documented workaround

**Status:** Running Python code that imports from `tyo3` requires `PYTHONPATH=src` (or equivalent) so that Python finds the `src/tyo3/` package. This is because `maturin develop` does not install the Python source as an editable package — it only installs the native extension.

**Resolution:** Add `pip install -e .` or configure maturin to properly handle the mixed layout. Until then, `PYTHONPATH=src` is the documented requirement.

---

## 10. Semantic Tokens and Type Hierarchy Deferred

**Severity:** Planned deferral — spec rules exist but no implementation

**Affected spec rules:**
- `tyo3-advanced.allium` → `GetSemanticTokens`
- `tyo3-advanced.allium` → `ExploreTypeHierarchy`

**Status:** DTO stubs exist in `rust/src/dto/tokens.rs` and `rust/src/dto/hierarchy.rs`. No converter or PyTyProject method exists. The implementation guide explicitly defers these to v0.2+.

**Upstream APIs available:**
- `ty_ide::semantic_tokens()` — exported, usable
- `ty_ide::prepare_type_hierarchy()` / `type_hierarchy_supertypes()` / `type_hierarchy_subtypes()` — exported, usable
- `ty_ide::TypeHierarchyItem` — re-exported, usable

---

## Summary

| # | Limitation | Severity | Phase to Resolve |
|---|---|---|---|
| 1 | `all_symbols` inaccessible | Blocker | Needs upstream re-export |
| 2 | Hover content flattened to Markdown | Non-blocker | v0.2+ or upstream fix |
| 3 | No GIL release (Salsa single-threaded) | Non-blocker | v0.2+ (snapshot approach) |
| 4 | Module naming: `_native_impl` submodule | Resolved | N/A |
| 5 | Error types partially resolved | Cosmetic | Phase 4+ |
| 6 | `BackendInfoDto` unused | Cosmetic | Phase 4+ |
| 7 | Maturin mixed layout not working | DevEx | Phase 4+ |
| 8 | `use_rust=False` default | Non-blocker | Phase 4 |
| 9 | `PYTHONPATH=src` required | DevEx | Phase 4+ |
| 10 | Semantic tokens / type hierarchy | Deferred | v0.2+ |
