# V2 Refactoring Guide — Remaining Issues

Post-migration cleanup from CODE_REVIEW_V2. The native-object migration (Steps 1–8 in PLAN.md) is complete. This guide covers the remaining significant and minor issues.

---

## Significant Issues

### Issue #1: Push `check_file()` filter into Rust

**Problem:** `session.py:66-88` runs a full project check via `self._rp.check()`, deserializes every diagnostic across the boundary, then filters in Python. For real-time SCIP, this is wasteful — only one file's diagnostics are needed.

**Note:** ty's `state.db.check()` is project-wide with no file-scoped variant. We can't avoid the full Salsa check, but we *can* filter in Rust before converting DTOs, avoiding unnecessary DTO construction and Python deserialization.

#### Step 1a: Add `check_file()` to Rust

**File: `rust/src/project.rs`**

Add a new method to `PyTyProject`:

```rust
/// Run the type checker and return diagnostics for a single file.
fn check_file(&self, path: &str) -> PyResult<dto::CheckResultDto> {
    let guard = lock_state(&self.inner, "check_file")?;
    let state = guard.as_ref().unwrap();

    // Resolve the target file path for comparison
    let (file, _) = resolve_file_and_source(state, path)?;
    let target_path = file.path(&state.db).as_str().to_string();

    // Full project check (Salsa-cached if unchanged)
    let all_diagnostics = state.db.check();

    // Filter diagnostics in Rust — only convert matching ones
    let matching: Vec<_> = all_diagnostics
        .iter()
        .filter(|d| {
            // Check if this diagnostic's annotation file matches target
            convert::diagnostics::diagnostic_matches_file(&state.db, d, &target_path)
        })
        .collect();

    let diagnostics = convert::diagnostics::convert_diagnostic_refs(&state.db, &matching);

    Ok(dto::CheckResultDto {
        diagnostics,
        files_checked: Some(1),
        elapsed_ms: None,
    })
}
```

#### Step 1b: Add helper to `convert/diagnostics.rs`

**File: `rust/src/convert/diagnostics.rs`**

Add a file-matching predicate and a variant that takes `&[&Diagnostic]`:

```rust
/// Check if a diagnostic's primary annotation is for the given file path.
pub fn diagnostic_matches_file(db: &dyn Db, d: &Diagnostic, target_path: &str) -> bool {
    // Use the same annotation inspection logic as extract_file_and_range,
    // but only check the file path without computing the range DTO.
    // Return true if the annotation's file path matches target_path.
    // ... (extract from annotations iterator, compare paths)
}

/// Convert a slice of diagnostic references (used by check_file).
pub fn convert_diagnostic_refs(
    db: &dyn Db,
    diagnostics: &[&Diagnostic],
) -> Vec<DiagnosticDto> {
    diagnostics.iter().map(|d| {
        let (file, range) = extract_file_and_range(db, d);
        DiagnosticDto {
            file,
            range,
            severity: severity_to_dto(d.severity()),
            code: diagnostic_id_to_code(d.id()),
            message: d.primary_message().to_string(),
            details: vec![],
        }
    }).collect()
}
```

#### Step 1c: Add Python bridge method

**File: `src/tyo3/rust_project.py`**

Add `check_file()` to `RustProject`, mirroring the existing `check()` pattern:

```python
def check_file(self, path: str | StdPath) -> CheckResult:
    """Run the type-checker and return diagnostics for a single file."""
    try:
        native_result = self._inner.check_file(str(path))
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e
    except _NativeAnalysisError as e:
        raise AnalysisError(str(e)) from e
    except Exception as e:
        raise InternalTyError(f"Unexpected error in check_file(): {e}") from e

    return CheckResult.model_validate(_to_python(native_result))
```

#### Step 1d: Simplify `session.py`

**File: `src/tyo3/session.py:66-88`**

Replace the Python-side filtering with:

```python
def check_file(self, path: str | StdPath) -> CheckResult:
    """Run the type checker and return diagnostics for a single file."""
    return self._rp.check_file(path)
```

#### Step 1e: Update tests

**File: `src/tyo3/tests/test_check_file.py`**

Existing tests should still pass (they test the public API). Add a test confirming that `check_file()` returns only diagnostics for the target file and not others (if the test fixture has multi-file diagnostics).

---

### Issue #3: Check `_closed` flag on Python-side operations

**Problem:** `rust_project.py:168` defines `self._closed` but never checks it in any public method. Every call crosses the Rust boundary just to get a `ProjectClosedError` from the mutex guard.

#### Step 3a: Add a guard method

**File: `src/tyo3/rust_project.py`**

Add a private guard method:

```python
def _check_open(self) -> None:
    """Raise ProjectClosedError if this project has been closed."""
    if self._closed:
        raise ProjectClosedError("Project is closed")
```

#### Step 3b: Add guard calls to all public methods

**File: `src/tyo3/rust_project.py`**

Add `self._check_open()` as the first line of: `files()`, `check()`, `check_file()` (new), `document_symbols()`, `workspace_symbols()`, `goto_definition()` / `goto_declaration()` / `goto_type_definition()` (via `_goto()`), `find_references()`, `hover()`, `reload()`.

This is a one-liner addition to each method, before the `try:` block.

#### Step 3c: Update tests

Add a test in `test_invariants.py` or a new test file confirming that calling any method after `close()` raises `ProjectClosedError` immediately (without hitting Rust).

---

### Issue #4: Fix `reload()` race condition

**Problem:** `project.rs:199-220` drops the mutex guard at line 205, creates a new database, then re-acquires the lock at line 215. Between drop and re-acquire, concurrent callers see `None` (closed state).

#### Step 4a: Hold the lock throughout

**File: `rust/src/project.rs`**

Rewrite `reload()` to never release the guard:

```rust
fn reload(&self) -> PyResult<()> {
    let mut guard = lock_state(&self.inner, "reload")?;
    let root = guard.as_ref().unwrap().root.clone();

    // Create the new database while still holding the lock
    let system = OsSystem::new(root.clone());
    let metadata = ProjectMetadata::new(
        ruff_python_ast::name::Name::new("tyo3-project"),
        root.clone(),
    );
    let db = ProjectDatabase::use_defaults(metadata, system);

    // Atomically swap — old database drops when guard's previous value drops
    *guard = Some(TyProjectState { db, root });
    Ok(())
}
```

The old `TyProjectState` (and its `ProjectDatabase`) is dropped when `*guard = Some(...)` overwrites it. No window where another thread sees `None`.

**Note:** Creating a `ProjectDatabase` while holding the mutex is fine — it's a CPU-bound operation with no lock contention risk. The Mutex exists to protect the `Option<TyProjectState>`, not to guard database construction time.

---

### Issue #5: Preserve `RangedValue` wrapper from navigation

**Problem:** `project.rs:78-109` — `navigate_to_targets()` receives `Option<RangedValue<NavigationTargets>>` from ty_ide but discards the outer range (the source range that was navigated *from*). This is useful for SCIP source highlighting.

#### Step 5a: Add source range to `DefinitionTargetDto`

This is a design decision. Two options:

**Option A — Add `source_range` to the return type:**

Extend the navigation response to include the source range. Add a wrapper DTO:

```rust
// dto/navigation.rs
#[pyclass(name = "NativeNavigationResult", frozen, module = "tyo3._native_impl")]
pub struct NavigationResultDto {
    #[pyo3(get)]
    pub source_range: Option<RangeDto>,  // where navigation started
    #[pyo3(get)]
    pub targets: Vec<DefinitionTargetDto>,
}
```

Update `project.rs` `navigate_to_targets()` to return `NavigationResultDto` instead of `Vec<DefinitionTargetDto>`.

Update Python-side: `RustProject._goto()` returns a richer object; `session.py` `goto_*` methods can either return the new type or unpack `.targets` for backward compat.

**Option B — Defer until SCIP integration needs it.**

The source range is only useful when building SCIP `Occurrence` records. If SCIP integration is not imminent, document the limitation and move on. The change is backward-incompatible at the Python API level.

**Recommendation:** Option B (defer). Mark with a `# TODO(scip): preserve RangedValue source_range` comment in `project.rs:101`.

---

### Issue #6: Eliminate redundant `LineIndex` computation

**Problem:** `coordinates.rs:16` builds a fresh `LineIndex` on every `position_to_offset()` call. In `workspace_symbols()` (`project.rs:318-323`), a new `LineIndex` is computed *per symbol* inside a loop, even when multiple symbols come from the same file.

#### Step 6a: Accept `LineIndex` parameter in `position_to_offset`

**File: `rust/src/coordinates.rs`**

Add an overload that accepts a pre-computed `LineIndex`:

```rust
pub fn position_to_offset_with_index(
    source: &str,
    line_index: &LineIndex,
    line: u32,
    column: u32,
) -> Result<TextSize, String> {
    // Same logic as position_to_offset but skip LineIndex construction
    // ...
}
```

Keep the existing `position_to_offset()` as a convenience wrapper.

#### Step 6b: Cache `LineIndex` per file in `workspace_symbols`

**File: `rust/src/project.rs`** — `workspace_symbols()` method (lines 311-345)

Use a `HashMap<FileId, (String, LineIndex)>` to cache per-file:

```rust
fn workspace_symbols(&self, query: &str) -> PyResult<Vec<dto::SymbolDto>> {
    let guard = lock_state(&self.inner, "workspace_symbols")?;
    let state = guard.as_ref().unwrap();
    let results = ty_ide::workspace_symbols(&state.db, query);

    let mut file_cache: HashMap<_, _> = HashMap::new();
    let mut symbols = Vec::with_capacity(results.len());

    for ws_info in &results {
        let (source_str, line_index) = file_cache
            .entry(ws_info.file)
            .or_insert_with(|| {
                let src = source_text(&state.db, ws_info.file).as_str().to_string();
                let idx = LineIndex::from_source_text(&src);
                (src, idx)
            });

        let file_path = ws_info.file.path(&state.db).as_str().to_string();
        symbols.push(convert::symbols::convert_workspace_symbol(
            source_str, line_index, &file_path, ws_info,
        ));
    }

    Ok(symbols)
}
```

#### Step 6c: Pass `LineIndex` through navigation callers

**File: `rust/src/project.rs`** — `navigate_to_targets()` (line 96)

In `navigate_to_targets`, compute `LineIndex` once for the source file and pass it to `position_to_offset_with_index`. The `convert_navigation_targets` call already receives the source text, so computing `LineIndex` there and passing it through is straightforward.

Similar for `find_references()` (line 420) and `hover()` (line 459).

---

## Minor Issues

### Issue #10: Explicit imports in `models/__init__.py`

**Problem:** Wildcard `from X import *` makes it unclear what's exported. `TyProject` name is ambiguous (Pydantic model vs session object).

#### Fix

**File: `src/tyo3/models/__init__.py`**

Replace wildcard imports with explicit imports:

```python
from tyo3.models.analysis import (
    CheckResult,
    Diagnostic,
    FileRange,
    Position,
    Range,
)
from tyo3.models.navigation import (
    DefinitionTarget,
    HoverContent,
    HoverResult,
    Reference,
    ReferenceKind,
)
from tyo3.models.symbols import Symbol, SymbolKind
from tyo3.models.core import (
    CoordinateMode,
    FileCategory,
    ProjectFile,
    ProjectStatus,
    TyProject,
)
from tyo3.models.advanced import (
    SemanticToken,
    SemanticTokenModifier,
    SemanticTokenType,
)
from tyo3.models._spec import BackendInfo, TyProjectConfig
```

Keep `__all__` as-is (it already enumerates everything).

---

### Issue #11: Consistent `workspace_symbols` query validation

**Problem:** `session.py:98` returns `[]` for empty queries, but `RustProject` and Rust have no guard.

#### Fix

**File: `src/tyo3/rust_project.py`** — `workspace_symbols()`

Add the same guard:

```python
def workspace_symbols(self, query: str) -> list[Symbol]:
    if not query:
        return []
    # ... existing code
```

This is defense-in-depth — the session layer already guards, but the bridge layer should too for direct callers.

---

### Issue #13: `SymbolKind.CLASS_` and `IMPORT_` trailing underscores

**Problem:** Users must write `SymbolKind.CLASS_` instead of `SymbolKind.CLASS` because `class` is a Python keyword.

#### Fix

**File: `src/tyo3/models/symbols.py`**

Add aliases as class attributes after the enum definition:

```python
class SymbolKind(StrEnum):
    MODULE = "module"
    CLASS_ = "class_"
    FUNCTION = "function"
    # ...
    IMPORT_ = "import_"

# Aliases for convenience (avoid trailing underscore for non-keyword contexts)
SymbolKind.CLASS = SymbolKind.CLASS_   # type: ignore[attr-defined]
SymbolKind.IMPORT = SymbolKind.IMPORT_  # type: ignore[attr-defined]
```

**Alternative (cleaner):** Use `__init_subclass__` or a custom `_generate_next_value_` to map `CLASS` -> `"class_"` wire format. But the alias approach is simpler and doesn't change the wire format.

Document both names in the class docstring.

---

### Issue #14: Demo CLI `cyan()` type annotation

**Problem:** `cli.py:38` declares `cyan(s: str)` but callers pass `PurePosixPath` objects (lines 56, 89, 115, 137).

#### Fix

**File: `src/tyo3/demo/cli.py`**

Change all colour helper signatures from `str` to `object`:

```python
def cyan(s: object) -> str:
    return f"{_CYAN}{s}{_RESET}"
```

This is correct — f-strings call `__str__()` on any object. Apply the same change to `bold()`, `dim()`, `green()`, `yellow()`, `red()`.

---

### Issue #15: Align `close()` idempotency between Python and Rust

**Problem:** Python `close()` is a no-op on second call (`rust_project.py:322`). Rust `close()` raises `ProjectClosedError` (`project.rs:232`). The Python guard masks the Rust error, making the Rust error path dead code.

#### Fix

**File: `rust/src/project.rs`** — `close()` method (lines 226-238)

Make Rust `close()` idempotent too:

```rust
fn close(&self) -> PyResult<()> {
    let mut guard = self.inner.lock().map_err(|e| {
        PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
    })?;

    // Idempotent — closing an already-closed project is a no-op
    *guard = None;
    Ok(())
}
```

Setting `None` on an already-`None` guard is harmless. This aligns both layers and removes dead code.

---

### Issue #16: `__del__` safety during interpreter shutdown

**Problem:** `rust_project.py:333-344` calls `self.close()` in `__del__`, which calls into the Rust extension. During interpreter shutdown, the native module may be unloaded.

#### Fix

**File: `src/tyo3/rust_project.py`** — `__del__` method

Guard against shutdown more robustly:

```python
def __del__(self) -> None:
    if not getattr(self, "_closed", True):
        warnings.warn(
            "RustProject was not closed explicitly. "
            "Use 'with RustProject(...) as rp:' or call rp.close().",
            ResourceWarning,
            stacklevel=2,
        )
        try:
            self.close()
        except Exception:
            pass
```

The current code already has this pattern, but the `getattr` default of `True` is actually correct here — if `_closed` doesn't exist (failed `__init__`), we don't want to call `close()`. The real protection is the `except Exception: pass`.

**Additional hardening:** Import the native module reference at `__init__` time and check it's still valid:

```python
def __del__(self) -> None:
    # Guard against interpreter shutdown — if our module globals are gone,
    # the Rust extension is likely unloaded too.
    if self._inner is None:
        return
    if not getattr(self, "_closed", True):
        warnings.warn(...)
        try:
            self.close()
        except Exception:
            pass
```

---

### Issue #17: Brittle `.so` copy in `devenv.nix`

**Problem:** `devenv.nix:39,47` hardcodes `cpython-313-x86_64-linux-gnu.so` — breaks if Python version or platform changes.

#### Fix

**File: `devenv.nix`** — `build` and `build-release` scripts

Use a glob to find the output filename, or better, use `maturin develop`:

**Option A — Dynamic filename detection:**

```nix
scripts.build.exec = ''
  echo "═══ Building Rust extension (debug) ═══"
  cd "$DEVENV_ROOT/rust"
  cargo build 2>&1
  SUFFIX="$(python3 -c "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX'))")"
  cp target/debug/lib_native_impl.so "$DEVENV_ROOT/src/tyo3/_native_impl''${SUFFIX}"
  echo "═══ Build complete — .so copied to src/tyo3/ ═══"
'';
```

**Option B — Use `maturin develop` (recommended):**

```nix
scripts.build.exec = ''
  echo "═══ Building Rust extension (debug) ═══"
  cd "$DEVENV_ROOT"
  maturin develop 2>&1
  echo "═══ Build complete ═══"
'';

scripts.build-release.exec = ''
  echo "═══ Building Rust extension (release) ═══"
  cd "$DEVENV_ROOT"
  maturin develop --release 2>&1
  echo "═══ Release build complete ═══"
'';
```

`maturin develop` handles the filename, copies the `.so` to the right location, and respects the active Python version automatically.

**Note:** This requires a `pyproject.toml` or `Cargo.toml` with the maturin build system configured at the project root level that `maturin develop` can find. Verify this works before switching.

---

## Implementation Order

Recommended sequence, grouped by risk and dependency:

| Phase | Issues | Risk | Effort |
|-------|--------|------|--------|
| **1 — Quick wins** | #10, #11, #13, #14, #15 | None | 30 min |
| **2 — Safety fixes** | #3, #4, #16 | Low | 1 hr |
| **3 — Performance** | #1, #6 | Medium (Rust changes) | 2-3 hr |
| **4 — Build hygiene** | #17 | Low | 30 min |
| **5 — Deferred** | #5 | Deferred to SCIP | — |

### Phase 1 rationale
Pure Python changes, no Rust rebuild needed. Each is a 5-minute fix.

### Phase 2 rationale
#3 and #4 are correctness bugs. #16 is a safety issue. All are small changes but touch both Python and Rust.

### Phase 3 rationale
#1 is the highest-impact remaining performance issue. #6 is a natural companion (both optimize the Rust hot path). These require Rust changes + rebuild + testing.

### Phase 4 rationale
Build script change. Test by rebuilding from clean.

### Phase 5 rationale
#5 is only useful when SCIP output is being built. Adding it now creates API churn for no current consumer.

---

## Verification

After each phase, run:

```bash
build          # Rebuild Rust extension (if Rust changed)
test-rust      # Integration tests
test-quick     # Unit tests (no native needed)
check-so       # Smoke test
```

After Phase 3, also run:

```bash
test-perf      # Verify no performance regression (should improve)
test-property  # Property-based tests still pass
```
