# TyO3 Refactor Guide

**Based on:** INITIAL_CODE_REVIEW.md (2026-06-01)
**Goal:** Transform tyo3 into a best-in-class, clean, elegant codebase
**Approach:** 7 sequential phases, each leaving the codebase in a working state

---

## How to Use This Guide

- Work through phases in order — each phase builds on the previous one.
- Each phase has a **verification step** at the end. Do not proceed until it passes.
- Run `test-quick` after every file change to catch regressions early.
- Run the full `test` suite at the end of each phase.
- Commit after each phase with a message like `refactor: phase N — <summary>`.

---

## Phase 1: Dead Code Removal and Trivial Fixes

**Goal:** Remove noise, fix obvious bugs, establish a clean baseline.
**Risk:** Low — deletions and one-line fixes only.
**Estimated scope:** ~10 files touched, net negative lines.

### Step 1.1: Delete Placeholder Modules

Remove the two empty placeholder files and their module declarations.

**Delete files:**
- `rust/src/dto/tokens.rs`
- `rust/src/dto/hierarchy.rs`

**Edit `rust/src/dto/mod.rs`:** Remove these two lines:
```rust
mod tokens;
mod hierarchy;
```

### Step 1.2: Delete Unused `errors.rs` Types

The file `rust/src/errors.rs` defines `PathError`, `PositionError`, and `ProjectError` — none are used anywhere. They will be replaced with proper PyO3 exceptions in Phase 3.

**Delete file:**
- `rust/src/errors.rs`

**Edit `rust/src/lib.rs`:** Remove this line:
```rust
mod errors;
```

### Step 1.3: Delete Unused `convert_hover()` Function

In `rust/src/convert/hover.rs`, the `convert_hover()` function (lines 15-33) is dead code. Only `convert_hover_markdown()` is called.

**Edit `rust/src/convert/hover.rs`:** Delete the entire `convert_hover()` function and its doc comment (lines 6-33). Keep only `convert_hover_markdown()`. Also remove the unused import `HoverContentKindDto` from the `use` statement at line 4 if it's only used by the deleted function.

### Step 1.4: Fix Double `.to_string()`

**Edit `rust/src/convert/symbols.rs` line 25:**
```rust
// BEFORE:
kind: kind.to_string().to_string(),
// AFTER:
kind: kind.to_string(),
```

### Step 1.5: Fix Redundant `format!`

**Edit `rust/src/project.rs` around line 436:**
```rust
// BEFORE:
let rendered = format!(
    "{}",
    hover_value.display(&state.db, ty_ide::MarkupKind::Markdown)
);
// AFTER:
let rendered = hover_value.display(&state.db, ty_ide::MarkupKind::Markdown).to_string();
```

### Step 1.6: Fix Broken `_get_all_files()`

**Edit `src/tyo3/services/project_service.py`:** Replace the `_get_all_files` method entirely. The current implementation accesses a non-existent `files` attribute on `TyProject` and uses a side-effect-in-ternary anti-pattern. Since the non-Rust path is only used for stubs in testing, simplify:

```python
def _get_all_files(self) -> list[ProjectFile]:
    """Return all files across all projects. Stub-mode only."""
    return []
```

This makes the broken behavior explicit. The real file discovery always goes through Rust via `list_files()`.

### Step 1.7: Fix Redundant `hasattr` Check

**Edit `src/tyo3/services/analysis_service.py` line 112:**
```python
# BEFORE:
if d.file is not None and hasattr(d, 'file')
# AFTER:
if d.file is not None
```

### Verification

```bash
# Rust compiles cleanly:
cd rust && cargo build 2>&1 | tail -5

# Python tests still pass:
test-quick
```

---

## Phase 2: Type Safety — Enums and DTOs

**Goal:** Make every type boundary honest. No more stringly-typed values.
**Risk:** Medium — touches models, DTOs, and conversion code. Many files affected but changes are mechanical.
**Estimated scope:** ~15 files touched.

### Step 2.1: Convert All Python Fake Enums to `StrEnum`

Every `str`-subclass "enum" must become a real `enum.StrEnum`. The pattern to follow is `HoverContentKind` in `models/navigation.py` — it's the only one done correctly.

**File `src/tyo3/models/core.py` — Replace `ProjectStatus`:**
```python
# BEFORE:
class ProjectStatus(str):
    CLOSED = "closed"
    OPEN = "open"
    ERROR = "error"

# AFTER:
from enum import StrEnum

class ProjectStatus(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    ERROR = "error"
```

**Do the same for all of these** (each in their respective file):

| Class | File | Members |
|-------|------|---------|
| `ProjectStatus` | `models/core.py` | CLOSED, OPEN, ERROR |
| `FileCategory` | `models/core.py` | FIRST_PARTY, VENDORED, STUB, DEPENDENCY |
| `DiagnosticSeverity` | `models/analysis.py` | FATAL, ERROR, WARNING, INFORMATION, HINT |
| `SymbolKind` | `models/symbols.py` | MODULE, CLASS_, FUNCTION, METHOD, CONSTRUCTOR, VARIABLE, CONSTANT, FIELD, PARAMETER, PROPERTY, TYPE_PARAMETER, IMPORT_, UNKNOWN |
| `ReferenceKind` | `models/navigation.py` | READ, WRITE, OTHER |
| `SemanticTokenType` | `models/advanced.py` | NAMESPACE, CLASS_, PARAMETER, SELF_PARAMETER, CLS_PARAMETER, VARIABLE, PROPERTY, FUNCTION, METHOD, KEYWORD, STRING, NUMBER, DECORATOR, BUILTIN_CONSTANT, TYPE_PARAMETER |
| `SemanticTokenModifier` | `models/advanced.py` | DEFINITION, READONLY, ASYNC_, DOCUMENTATION |

### Step 2.2: Update Model Fields to Use Enum Types

After converting enums, update model fields that currently use `str` to use the actual enum type:

**`models/core.py`:**
```python
class TyProject(BaseModel):
    status: ProjectStatus  # was: str

class ProjectFile(BaseModel):
    file_category: FileCategory  # was: str
```

**`models/analysis.py`:**
```python
class Diagnostic(BaseModel):
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR  # was: str = "error"
```

**`models/symbols.py`:**
```python
class Symbol(BaseModel):
    kind: SymbolKind  # was: str
```

**`models/navigation.py`:**
```python
class Reference(BaseModel):
    kind: ReferenceKind  # was: str
```

**`models/advanced.py`:**
```python
class SemanticToken(BaseModel):
    token_type: SemanticTokenType  # was: str
    modifiers: set[SemanticTokenModifier] = Field(default_factory=set)  # was: set[str]
```

### Step 2.3: Convert Rust DTO String Fields to Serde Enums

Follow the existing `HoverContentKindDto` pattern for the other stringly-typed fields.

**Edit `rust/src/dto/diagnostics.rs`:**
```rust
use crate::dto::RangeDto;

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SeverityDto {
    Fatal,
    Error,
    Warning,
    Information,
    Hint,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct DiagnosticDto {
    pub file: Option<String>,
    pub range: Option<RangeDto>,
    pub severity: SeverityDto,     // was: String
    pub code: Option<String>,
    pub message: String,
    pub details: Vec<String>,
}
```

**Edit `rust/src/dto/symbols.rs`:**
```rust
use crate::dto::{FileRangeDto, RangeDto};

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SymbolKindDto {
    Module,
    Class,
    Function,
    Method,
    Constructor,
    Variable,
    Constant,
    Field,
    Parameter,
    Property,
    TypeParameter,
    Import,
    Unknown,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SymbolDto {
    pub name: String,
    pub qualified_name: Option<String>,
    pub kind: SymbolKindDto,       // was: String
    pub location: FileRangeDto,
    pub selection_range: Option<RangeDto>,
    pub container_name: Option<String>,
    pub deprecated: bool,
}
```

**Edit `rust/src/dto/navigation.rs`:**
```rust
use crate::dto::{RangeDto, SymbolDto};

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReferenceKindDto {
    Read,
    Write,
    Other,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct DefinitionTargetDto {
    pub path: String,
    pub range: RangeDto,
    pub selection_range: Option<RangeDto>,
    pub symbol: Option<SymbolDto>,
    pub module_name: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ReferenceDto {
    pub path: String,
    pub range: RangeDto,
    pub kind: ReferenceKindDto,    // was: String
}
```

### Step 2.4: Update Rust Conversion Functions

**Edit `rust/src/convert/diagnostics.rs`:** Change `severity_to_string` to return `SeverityDto`:
```rust
use crate::dto::{DiagnosticDto, SeverityDto};

fn severity_to_dto(severity: ruff_db::diagnostic::Severity) -> SeverityDto {
    match severity {
        ruff_db::diagnostic::Severity::Fatal => SeverityDto::Fatal,
        ruff_db::diagnostic::Severity::Error => SeverityDto::Error,
        ruff_db::diagnostic::Severity::Warning => SeverityDto::Warning,
        ruff_db::diagnostic::Severity::Info => SeverityDto::Information,
    }
}
```

Update `convert_diagnostics` to call `severity_to_dto` instead of `severity_to_string`.

**Edit `rust/src/convert/symbols.rs`:** Change the `kind` parameter to accept `&ty_ide::SymbolKind` and return `SymbolKindDto`:
```rust
fn symbol_kind_to_dto(kind: &ty_ide::SymbolKind) -> SymbolKindDto {
    // Map each ty_ide::SymbolKind variant to the corresponding SymbolKindDto variant.
    // Inspect ty_ide::SymbolKind's variants via its Display impl or source code.
    // Fallback: SymbolKindDto::Unknown
}
```

**Edit `rust/src/convert/navigation.rs`:** Change `convert_reference_kind` to return `ReferenceKindDto`:
```rust
fn convert_reference_kind(kind: ty_ide::ReferenceKind) -> ReferenceKindDto {
    match kind {
        ty_ide::ReferenceKind::Read => ReferenceKindDto::Read,
        ty_ide::ReferenceKind::Write => ReferenceKindDto::Write,
        ty_ide::ReferenceKind::Other => ReferenceKindDto::Other,
    }
}
```

### Step 2.5: Update Python Parsing to Use Enum Constructors

In `rust_project.py`, where JSON is parsed into models, update to construct enum values:

```python
# BEFORE:
severity=d.get("severity", "error"),
kind=s.get("kind", "unknown"),

# AFTER:
severity=DiagnosticSeverity(d.get("severity", "error")),
kind=SymbolKind(s.get("kind", "unknown")),
```

This ensures invalid values from Rust are caught immediately at the Python boundary.

### Verification

```bash
cd rust && cargo build 2>&1 | tail -5
test-quick
# Also verify enum validation works:
python -c "from tyo3.models.core import ProjectStatus; ProjectStatus('banana')"
# Should raise ValueError
```


---

## Phase 3: Typed Exceptions Across the PyO3 Boundary

**Goal:** Replace fragile string-matching error handling with structured exception types.
**Risk:** Medium — changes the Rust-Python error contract. Tests that assert on exception types may need updates.
**Estimated scope:** ~4 files touched.

### Step 3.1: Define PyO3 Custom Exceptions in Rust

**Edit `rust/src/lib.rs`:** Add custom exception definitions using `pyo3::create_exception!`. These become real Python exception classes importable from `tyo3._native_impl`:

```rust
use pyo3::prelude::*;
use pyo3::create_exception;

// Define Python exception hierarchy rooted at PyRuntimeError
create_exception!(tyo3._native_impl, ProjectClosedError, pyo3::exceptions::PyRuntimeError);
create_exception!(tyo3._native_impl, PathResolutionError, pyo3::exceptions::PyRuntimeError);
create_exception!(tyo3._native_impl, PositionError, pyo3::exceptions::PyRuntimeError);
create_exception!(tyo3._native_impl, AnalysisError, pyo3::exceptions::PyRuntimeError);

mod convert;
mod dto;
mod project;
mod coordinates;
mod files;

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

### Step 3.2: Use Typed Exceptions in `project.rs`

Replace every `PyRuntimeError::new_err(...)` with the appropriate typed exception.

**Pattern — in `lock_state()`:**
```rust
// BEFORE:
if guard.is_none() {
    return Err(PyRuntimeError::new_err(format!(
        "Project is closed — cannot call {}()", op_name
    )));
}

// AFTER:
use crate::ProjectClosedError;
if guard.is_none() {
    return Err(ProjectClosedError::new_err(format!(
        "Project is closed — cannot call {}()", op_name
    )));
}
```

**Pattern — in `resolve_file_and_source()` and anywhere paths are resolved:**
```rust
// BEFORE:
.map_err(|e| PyRuntimeError::new_err(e))?;

// AFTER:
use crate::PathResolutionError;
.map_err(|e| PathResolutionError::new_err(e))?;
```

**Pattern — in coordinate conversion errors:**
```rust
// BEFORE:
let offset = coordinates::position_to_offset(&source_str, &pos)
    .map_err(|e| PyRuntimeError::new_err(e))?;

// AFTER:
use crate::PositionError;
let offset = coordinates::position_to_offset(&source_str, &pos)
    .map_err(|e| PositionError::new_err(e))?;
```

**Pattern — in serialization failures:**
```rust
// Keep PyRuntimeError for truly unexpected internal errors like serialization failures.
// These indicate bugs, not user errors.
```

Go through every method in `project.rs` and classify each error site:
- Project closed → `ProjectClosedError`
- Path resolution → `PathResolutionError`  
- Position/offset → `PositionError`
- Serialization/internal → `PyRuntimeError` (keep as-is)

### Step 3.3: Simplify Python Error Handling in `rust_project.py`

Now that Rust raises typed exceptions, Python can catch them directly. Replace every string-matching `except` block.

First, import the native exceptions:

```python
try:
    from tyo3._native_impl import (
        ProjectClosedError as _NativeClosedError,
        PathResolutionError as _NativePathError,
        PositionError as _NativePositionError,
        AnalysisError as _NativeAnalysisError,
    )
except ImportError:
    # When native extension isn't built, define dummy classes that never match
    class _NativeClosedError(Exception): pass    # type: ignore[no-redef]
    class _NativePathError(Exception): pass      # type: ignore[no-redef]
    class _NativePositionError(Exception): pass   # type: ignore[no-redef]
    class _NativeAnalysisError(Exception): pass   # type: ignore[no-redef]
```

Then replace every error-handling block. Example for `files()`:

```python
# BEFORE:
def files(self) -> list[str]:
    try:
        return self._inner.files()
    except Exception as e:
        if "closed" in str(e).lower():
            raise ProjectClosedError(str(e)) from e
        raise

# AFTER:
def files(self) -> list[str]:
    try:
        return self._inner.files()
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e
```

Apply this pattern to **every method** in `RustProject`:
- `_NativeClosedError` → `ProjectClosedError`
- `_NativePathError` → `PathResolutionError`
- `_NativePositionError` → `PositionError`
- `_NativeAnalysisError` → `AnalysisError`

Each method should have at most 2-3 specific `except` clauses instead of one `except Exception` with string matching.

### Verification

```bash
cd rust && cargo build 2>&1 | tail -5
test-quick
# Verify exception types propagate correctly:
# (requires native extension built)
build
python -c "
from tyo3.rust_project import RustProject
rp = RustProject('fixtures/simple_package')
rp.close()
try:
    rp.files()
except Exception as e:
    print(type(e).__name__)  # Should print: ProjectClosedError
"
```


---

## Phase 4: Rust Backend Cleanup

**Goal:** Eliminate duplication, fix performance issues, and tighten the Rust code.
**Risk:** Low-Medium — internal refactors that don't change the external API.
**Estimated scope:** ~5 Rust files touched.

### Step 4.1: Remove Unnecessary `Arc`

**Edit `rust/src/project.rs`:**

```rust
// BEFORE:
pub struct PyTyProject {
    inner: Arc<Mutex<Option<TyProjectState>>>,
}

// In open():
inner: Arc::new(Mutex::new(Some(TyProjectState { db, root: system_root }))),

// AFTER:
pub struct PyTyProject {
    inner: Mutex<Option<TyProjectState>>,
}

// In open():
inner: Mutex::new(Some(TyProjectState { db, root: system_root })),
```

Remove the `use std::sync::Arc;` import. Update `lock_state` if needed (it takes `&Mutex<...>` already, so it should work as-is).

### Step 4.2: Deduplicate Navigation Methods

The three `goto_*` methods and `find_references` share identical boilerplate. Extract a shared helper.

**Add to `project.rs` in the internal helpers section:**

```rust
/// Shared implementation for goto_definition, goto_declaration, goto_type_definition.
fn navigate_to_targets(
    inner: &Mutex<Option<TyProjectState>>,
    op_name: &str,
    path: &str,
    line: u32,
    column: u32,
    navigate_fn: fn(&dyn ty_project::Db, File, ruff_text_size::TextSize) -> Option<ty_ide::NavigationTargets>,
) -> PyResult<String> {
    let guard = lock_state(inner, op_name)?;
    let state = guard.as_ref().unwrap();

    let (file, source_str) = resolve_file_and_source(state, path)?;

    let pos = dto::PositionDto { line, column };
    let offset = coordinates::position_to_offset(&source_str, &pos)
        .map_err(|e| crate::PositionError::new_err(e))?;

    let result = navigate_fn(&state.db, file, offset);

    let targets = match result {
        Some(targets) => convert::navigation::convert_navigation_targets(&state.db, &targets),
        None => Vec::new(),
    };

    serde_json::to_string(&targets)
        .map_err(|e| PyRuntimeError::new_err(format!("Serialisation failed: {}", e)))
}
```

**Then simplify each navigation method:**

```rust
fn goto_definition(&self, path: &str, line: u32, column: u32) -> PyResult<String> {
    navigate_to_targets(&self.inner, "goto_definition", path, line, column, ty_ide::goto_definition)
}

fn goto_declaration(&self, path: &str, line: u32, column: u32) -> PyResult<String> {
    navigate_to_targets(&self.inner, "goto_declaration", path, line, column, ty_ide::goto_declaration)
}

fn goto_type_definition(&self, path: &str, line: u32, column: u32) -> PyResult<String> {
    navigate_to_targets(&self.inner, "goto_type_definition", path, line, column, ty_ide::goto_type_definition)
}
```

**Note:** The function pointer approach requires that all three `ty_ide` functions have the exact same signature. Verify this. If they differ slightly, use a closure or a generic parameter instead.

### Step 4.3: Precompute `LineIndex` Per File

Currently `coordinates::range_to_dto()` recomputes `LineIndex::from_source_text(source)` on every call. For `document_symbols` with 50 symbols, that's 50 redundant full-file scans.

**Edit `rust/src/coordinates.rs`:**

Add a new function that takes a precomputed `LineIndex`:

```rust
/// Convert a ruff TextRange to a RangeDto using a precomputed LineIndex.
pub fn range_to_dto_with_index(
    source: &str,
    line_index: &LineIndex,
    range: ruff_text_size::TextRange,
) -> RangeDto {
    let start_loc = line_index.source_location(range.start(), source, PositionEncoding::Utf32);
    let end_loc = line_index.source_location(range.end(), source, PositionEncoding::Utf32);

    RangeDto {
        start: PositionDto {
            line: start_loc.line.get() as u32,
            column: (start_loc.character_offset.to_zero_indexed() + 1) as u32,
        },
        end: PositionDto {
            line: end_loc.line.get() as u32,
            column: (end_loc.character_offset.to_zero_indexed() + 1) as u32,
        },
    }
}

/// Convenience: compute LineIndex and convert. Use when only one range is needed.
pub fn range_to_dto(source: &str, range: ruff_text_size::TextRange) -> RangeDto {
    let line_index = LineIndex::from_source_text(source);
    range_to_dto_with_index(source, &line_index, range)
}
```

**Update callers that convert multiple ranges for the same file** to precompute:

In `convert/symbols.rs`:
```rust
pub fn convert_symbol(
    source: &str,
    line_index: &LineIndex,  // NEW parameter
    file_path: &str,
    // ... rest unchanged
) -> SymbolDto {
    let name_range_dto = coordinates::range_to_dto_with_index(source, line_index, name_range);
    let full_range_dto = coordinates::range_to_dto_with_index(source, line_index, full_range);
    // ...
}
```

In `project.rs` `document_symbols()`, compute the index once:
```rust
let line_index = ruff_source_file::LineIndex::from_source_text(&source_str);
// Then pass &line_index to convert_symbol calls
```

Similarly update `convert/navigation.rs` — `convert_navigation_target` and `convert_reference` each recompute the line index via `range_to_dto`. If converting multiple targets for the same file, precompute.

### Step 4.4: Make `document_symbols` Hierarchy Recursive

**Edit `rust/src/project.rs`** — the `document_symbols` method currently only traverses two levels. Replace with a recursive helper:

```rust
fn collect_symbols_recursive(
    hierarchical: &ty_ide::HierarchicalSymbols,
    id: ty_ide::SymbolId,
    info: &ty_ide::SymbolInfo,
    source: &str,
    line_index: &LineIndex,
    file_path: &str,
    parent_name: Option<&str>,
    symbols: &mut Vec<dto::SymbolDto>,
) {
    let qualified = match parent_name {
        Some(p) => Some(format!("{}.{}", p, info.name)),
        None => None,
    };

    let sym = convert::symbols::convert_symbol(
        source, line_index, file_path,
        &info.name, &info.kind, info.deprecated,
        info.name_range, info.full_range,
        parent_name, qualified.clone(),
    );
    symbols.push(sym);

    let own_name = match &qualified {
        Some(q) => q.as_str(),
        None => &info.name,
    };

    for (child_id, child_info) in hierarchical.children(id) {
        collect_symbols_recursive(
            hierarchical, child_id, child_info,
            source, line_index, file_path,
            Some(own_name), symbols,
        );
    }
}
```

Then in `document_symbols`:
```rust
for (id, info) in hierarchical.iter() {
    collect_symbols_recursive(
        &hierarchical, id, info,
        &source_str, &line_index, &file_path,
        None, &mut symbols,
    );
}
```

### Verification

```bash
cd rust && cargo build 2>&1 | tail -5
build
test-rust
```


---

## Phase 5: Python Layer — Deduplicate and Add Resource Management

**Goal:** Eliminate copy-paste in `rust_project.py`, add context manager support, modernize type annotations.
**Risk:** Low — internal refactoring, no API changes except adding `__enter__`/`__exit__`.
**Estimated scope:** ~6 Python files touched.

### Step 5.1: Extract `_parse_symbol()` Helper in `rust_project.py`

`document_symbols()` and `workspace_symbols()` have identical JSON-to-Symbol construction code. Extract it.

**Add this helper to `rust_project.py`:**

```python
def _parse_symbol(self, s: dict) -> Symbol:
    """Parse a SymbolDto JSON dict into a Symbol model."""
    location_data = s["location"]
    loc = ModelFileRange(
        path=_string_path_to_typath(location_data["path"]),
        range=_json_range_to_model(location_data["range"]),
    )
    sel_range = (
        _json_range_to_model(s["selection_range"])
        if s.get("selection_range")
        else None
    )
    return Symbol(
        project=self._project_model,
        name=s["name"],
        qualified_name=s.get("qualified_name"),
        kind=SymbolKind(s.get("kind", "unknown")),
        location=loc,
        selection_range=sel_range,
        container_name=s.get("container_name"),
        deprecated=s.get("deprecated", False),
    )
```

**Then simplify both methods:**

```python
def document_symbols(self, path: str | StdPath) -> list[Symbol]:
    try:
        raw_json: str = self._inner.document_symbols(str(path))
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e
    except _NativePathError as e:
        raise PathResolutionError(str(e)) from e

    return [self._parse_symbol(s) for s in json.loads(raw_json)]

def workspace_symbols(self, query: str) -> list[Symbol]:
    try:
        raw_json: str = self._inner.workspace_symbols(query)
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e

    return [self._parse_symbol(s) for s in json.loads(raw_json)]
```

### Step 5.2: Extract `_parse_definition_target()` Helper

The `_goto()` method has inline DefinitionTarget construction with nested Symbol construction. Extract it:

```python
def _parse_definition_target(self, t: dict) -> DefinitionTarget:
    """Parse a DefinitionTargetDto JSON dict into a DefinitionTarget model."""
    sel_range = (
        _json_range_to_model(t["selection_range"])
        if t.get("selection_range")
        else None
    )
    symbol = self._parse_symbol(t["symbol"]) if t.get("symbol") else None

    return DefinitionTarget(
        project=self._project_model,
        path=_string_path_to_typath(t["path"]),
        range=_json_range_to_model(t["range"]),
        selection_range=sel_range,
        symbol=symbol,
        module_name=t.get("module_name"),
    )
```

**Simplify `_goto()`:**

```python
def _goto(self, method: str, path: str | StdPath, line: int, column: int) -> list[DefinitionTarget]:
    try:
        raw_json: str = getattr(self._inner, method)(str(path), line, column)
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e
    except _NativePositionError as e:
        raise PositionError(str(e)) from e
    except _NativePathError as e:
        raise PathResolutionError(str(e)) from e

    return [self._parse_definition_target(t) for t in json.loads(raw_json)]
```

### Step 5.3: Add Context Manager to `RustProject`

**Add to the `RustProject` class:**

```python
def __enter__(self) -> RustProject:
    return self

def __exit__(self, exc_type, exc_val, exc_tb) -> None:
    self.close()

def __del__(self) -> None:
    # Best-effort cleanup if user forgets to close.
    # Don't raise from __del__.
    try:
        self.close()
    except Exception:
        pass
```

### Step 5.4: Modernize Type Annotations

Throughout all Python files, replace `Optional[X]` with `X | None`:

```python
# BEFORE:
from typing import Optional
def foo(self) -> Optional[HoverResult]:

# AFTER:
def foo(self) -> HoverResult | None:
```

Files to update:
- `rust_project.py`
- `models/core.py`
- `models/analysis.py`
- `models/symbols.py`
- `models/navigation.py`
- `models/advanced.py`
- All service files

Remove `from typing import Optional` imports where no longer needed.

### Step 5.5: Change `Diagnostic.details` from `set` to `list`

The DTO sends `Vec<String>` (ordered), but the model stores `set[str]` (unordered, deduplicated). This loses information for no reason.

**Edit `models/analysis.py`:**
```python
# BEFORE:
details: set[str] = Field(default_factory=set)

# AFTER:
details: list[str] = Field(default_factory=list)
```

**Edit `rust_project.py`** — in `check()`:
```python
# BEFORE:
details=set(d.get("details", [])),

# AFTER:
details=d.get("details", []),
```

**Edit `analysis_service.py`** — in `check_project()` and `check_file()`:
```python
# BEFORE:
details=set(d.details) if d.details else set(),

# AFTER:
details=list(d.details) if d.details else [],
```

### Verification

```bash
test-quick
# Verify context manager works:
python -c "
from tyo3.rust_project import RustProject
with RustProject('fixtures/simple_package') as rp:
    print(len(rp.files()), 'files')
print('closed cleanly')
"
```


---

## Phase 6: Collapse the Service Layer

**Goal:** Replace four parallel services with a single coherent `TyO3Session` class. This is the largest and most impactful refactor.
**Risk:** High — restructures the public Python API. Tests will need significant updates.
**Estimated scope:** ~10 files touched, significant test rewrites.

### Design

The current architecture has four services that each independently:
- Maintain their own `_rust_projects: dict[str, object]` registry
- Have their own `_path_to_str()` helper (copied 3x)
- Have their own `_get_rust_project()` / `set_rust_project()` (copied 4x)
- Wrap `RustProject` with thin pass-through plus precondition checks

Replace all of this with a single `TyO3Session` class that:
- Owns one `RustProject` instance
- Exposes all operations (check, symbols, navigation, hover)
- Centralizes precondition validation
- Manages its own lifecycle (open/close/reload)
- Implements context manager protocol

### Step 6.1: Create `src/tyo3/session.py`

This is the new primary public API. Create it as a new file:

```python
"""TyO3Session — unified project session API.

Replaces the separate ProjectService, AnalysisService, SymbolService,
and NavigationService with a single coherent interface.
"""

from __future__ import annotations

from pathlib import Path as StdPath

from tyo3.exceptions import (
    AnalysisError,
    PathResolutionError,
    PositionError,
    ProjectClosedError,
    ProjectOpenError,
)
from tyo3.models.analysis import CheckResult, Diagnostic, DiagnosticSeverity
from tyo3.models.core import FileCategory, Path, ProjectFile, TyProject
from tyo3.models.navigation import DefinitionTarget, HoverResult, Reference
from tyo3.models.symbols import Symbol
from tyo3.rust_project import RustProject


class TyO3Session:
    """A live session with the ty semantic engine for a single project root.

    Usage::

        with TyO3Session("/path/to/project") as session:
            result = session.check()
            symbols = session.document_symbols("src/main.py")
            definitions = session.goto_definition("src/main.py", 10, 5)
    """

    def __init__(self, root: str | StdPath) -> None:
        self._rp = RustProject(root)
        self._root = self._rp.root

    def __enter__(self) -> TyO3Session:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # ── Lifecycle ────────────────────────────────────────────

    def reload(self) -> None:
        """Reload the project, clearing cached state."""
        self._rp.reload()

    def close(self) -> None:
        """Close the session and free Rust-side resources."""
        self._rp.close()

    @property
    def root(self) -> StdPath:
        return self._root

    # ── Files ────────────────────────────────────────────────

    def files(self) -> list[str]:
        """Return all file paths known to the project."""
        return self._rp.files()

    # ── Analysis ─────────────────────────────────────────────

    def check(self) -> CheckResult:
        """Run the type checker on the entire project."""
        return self._rp.check()

    def check_file(self, path: str | StdPath) -> CheckResult:
        """Run the type checker and filter to a single file.

        Note: Currently runs a full project check. File-level
        filtering depends on Rust diagnostics including file info.
        """
        result = self._rp.check()
        path_str = str(path)
        # Filter diagnostics to the requested file when file info is available
        filtered = [d for d in result.diagnostics if d.file is not None]
        return CheckResult(
            diagnostics=filtered,
            files_checked=1,
            elapsed_ms=result.elapsed_ms,
        )

    # ── Symbols ──────────────────────────────────────────────

    def document_symbols(self, path: str | StdPath) -> list[Symbol]:
        """Return symbols defined in the given file."""
        return self._rp.document_symbols(path)

    def workspace_symbols(self, query: str) -> list[Symbol]:
        """Search for symbols matching query across the project."""
        if len(query) < 1:
            return []
        return self._rp.workspace_symbols(query)

    # ── Navigation ───────────────────────────────────────────

    def goto_definition(
        self, path: str | StdPath, line: int, column: int
    ) -> list[DefinitionTarget]:
        """Navigate to the definition of the symbol at (line, column)."""
        self._validate_position(line, column)
        return self._rp.goto_definition(path, line, column)

    def goto_declaration(
        self, path: str | StdPath, line: int, column: int
    ) -> list[DefinitionTarget]:
        """Navigate to the declaration of the symbol at (line, column)."""
        self._validate_position(line, column)
        return self._rp.goto_declaration(path, line, column)

    def goto_type_definition(
        self, path: str | StdPath, line: int, column: int
    ) -> list[DefinitionTarget]:
        """Navigate to the type definition of the symbol at (line, column)."""
        self._validate_position(line, column)
        return self._rp.goto_type_definition(path, line, column)

    def find_references(
        self,
        path: str | StdPath,
        line: int,
        column: int,
        include_declaration: bool = True,
    ) -> list[Reference]:
        """Find all references to the symbol at (line, column)."""
        self._validate_position(line, column)
        return self._rp.find_references(path, line, column, include_declaration)

    def hover(
        self, path: str | StdPath, line: int, column: int
    ) -> HoverResult | None:
        """Get hover information for the symbol at (line, column)."""
        self._validate_position(line, column)
        return self._rp.hover(path, line, column)

    # ── Validation ───────────────────────────────────────────

    @staticmethod
    def _validate_position(line: int, column: int) -> None:
        if line < 1 or column < 1:
            raise PositionError("Position must be 1-based (line >= 1, column >= 1)")
```

### Step 6.2: Update `__init__.py` to Export `TyO3Session`

**Edit `src/tyo3/__init__.py`:**
```python
"""TyO3: Python semantic engine powered by ty/Ruff."""

from __future__ import annotations

__version__ = "0.1.0"

from tyo3.session import TyO3Session

__all__ = ["TyO3Session"]

# Also try to import native extension for availability detection
try:
    from tyo3._native_impl import TyProject as _NativeTyProject  # noqa: F401
    _HAS_NATIVE = True
except ImportError:
    _HAS_NATIVE = False
```

### Step 6.3: Deprecate (Don't Delete) the Old Services

Don't delete the services immediately — existing tests depend on them. Instead:

1. Add a deprecation comment at the top of each service file:
   ```python
   # DEPRECATED: Use tyo3.TyO3Session instead. This module will be removed in v0.2.
   ```

2. Update the test suite incrementally. Write new tests against `TyO3Session` first, then remove old service tests in a follow-up.

### Step 6.4: Remove `project` Field from Result Models

This is the right time to decouple result models from `TyProject`. Every `Diagnostic`, `Symbol`, `DefinitionTarget`, and `Reference` currently carries a full `project: TyProject` — this is unnecessary weight.

**Edit each model to remove the `project` field:**

`models/analysis.py`:
```python
class Diagnostic(BaseModel):
    # project: TyProject  ← REMOVE
    file: ProjectFile | None = None
    range: Range | None = None
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR
    code: str | None = None
    message: str
    details: list[str] = Field(default_factory=list)
```

`models/symbols.py`:
```python
class Symbol(BaseModel):
    # project: TyProject  ← REMOVE
    name: str
    qualified_name: str | None = None
    kind: SymbolKind
    location: FileRange
    selection_range: Range | None = None
    container_name: str | None = None
    deprecated: bool = False
```

`models/navigation.py`:
```python
class DefinitionTarget(BaseModel):
    # project: TyProject  ← REMOVE
    path: Path
    range: Range
    selection_range: Range | None = None
    symbol: Symbol | None = None
    module_name: str | None = None

class Reference(BaseModel):
    # project: TyProject  ← REMOVE
    path: Path
    range: Range
    kind: ReferenceKind
```

**Then update `rust_project.py`** — remove all `project=self._project_model` arguments from model constructors. Also remove `self._project_model` from `__init__` if nothing else uses it.

**Update old service code** that sets `d.project = project` / `s.project = project` — these assignments become unnecessary.

**Update test fixtures in `conftest.py`** — remove `project=...` from model construction in fixtures.

### Verification

```bash
test-quick
# Verify the new API works end-to-end:
build
python -c "
from tyo3 import TyO3Session
with TyO3Session('fixtures/simple_package') as s:
    files = s.files()
    result = s.check()
    print(f'{len(files)} files, {len(result.diagnostics)} diagnostics')
"
```


---

## Phase 7: Path Model and Final Polish

**Goal:** Replace the custom `Path` model, clean up remaining rough edges, ensure everything is pristine.
**Risk:** Medium — `Path` is used pervasively, so this is a wide-but-shallow change.
**Estimated scope:** ~12 files touched.

### Step 7.1: Replace Custom `Path` with `pathlib.PurePosixPath`

The custom `Path(components=list[str])` model adds no value and forces awkward conversions everywhere. Replace it with `pathlib.PurePosixPath` throughout.

**Delete the `Path` class from `models/core.py`.**

**Update all references.** The key change pattern:

```python
# BEFORE (creating a Path from a string):
Path(components=list(StdPath(f).parts))

# AFTER:
PurePosixPath(f)

# BEFORE (converting Path to string — the _path_to_str helper):
components = path.components
if components and components[0] == "/":
    return "/" + "/".join(components[1:])
return "/".join(components)

# AFTER:
str(path)
```

**Files that import/use `Path` from models (update all):**
- `models/core.py` — `TyProjectConfig.config_path`, `TyProjectConfig.extra_search_paths`, `TyProject.root`
- `models/analysis.py` — `FileRange.path`
- `models/navigation.py` — `DefinitionTarget.path`, `Reference.path`
- `models/__init__.py` — remove `Path` from exports
- `rust_project.py` — `_string_path_to_typath` helper becomes unnecessary, delete it
- All service files — delete `_path_to_str()` helpers (all three copies)

**For Pydantic compatibility with `PurePosixPath`:**

Pydantic v2 supports `pathlib.Path` natively. For `PurePosixPath`, you may need a custom type annotation:

```python
from typing import Annotated
from pydantic import BeforeValidator

PosixPath = Annotated[PurePosixPath, BeforeValidator(lambda v: PurePosixPath(v) if isinstance(v, str) else v)]
```

Or simply use `pathlib.Path` (which Pydantic handles natively) if platform portability isn't a concern.

### Step 7.2: Clean Up `TyProjectConfig` and `TyProject`

With `Path` replaced, simplify the config model:

```python
from pathlib import Path

class TyProjectConfig(BaseModel):
    python_version: str | None = None
    config_path: Path | None = None
    extra_search_paths: set[Path] = Field(default_factory=set)
    respect_gitignore: bool = True
    force_exclude: bool = False
    check_all_files: bool = True

class TyProject(BaseModel):
    root: Path
    status: ProjectStatus
    coordinate_mode: str = "python"
    python_version: str | None = None
    config_path: Path | None = None
    extra_search_paths: set[Path] = Field(default_factory=set)
    respect_gitignore: bool = True
    force_exclude: bool = False
    check_all_files: bool = True
    opened_at: datetime
    last_reloaded_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.status == ProjectStatus.OPEN

    @property
    def has_error(self) -> bool:
        return self.status == ProjectStatus.ERROR
```

No more `model_config = {"arbitrary_types_allowed": True}` — Pydantic handles `pathlib.Path` natively.

### Step 7.3: Remove the `_path_to_str()` Helpers

With `pathlib.Path` everywhere, the triplicated `_path_to_str()` helpers are unnecessary. Delete them from:
- `services/analysis_service.py`
- `services/symbol_service.py`
- `services/navigation_service.py`

Callers now just use `str(path)`.

### Step 7.4: Update `models/__init__.py` Exports

Remove `Path` from `__all__` since it's no longer a custom type. Update any remaining exports to match the refactored models (no `project` field, real enum types, etc.).

### Step 7.5: Final Code Quality Pass

Walk through every file and check for:

1. **Unused imports** — Run `ruff check --select F401` to find them.
2. **Remaining `Optional[X]`** — Search and replace with `X | None`.
3. **Remaining `from typing import Optional`** — Remove if no longer used.
4. **Inconsistent string quotes** — Should be double quotes per `pyproject.toml`.
5. **Line length violations** — Max 120 per `pyproject.toml`.

```bash
cd "$DEVENV_ROOT"
PYTHONPATH=src ruff check src/tyo3/ --fix
PYTHONPATH=src ruff format src/tyo3/
```

### Step 7.6: Update Test Fixtures

The test `conftest.py` creates model instances with the old API (`Path(components=...)`, `project=...`). Update all fixtures to match the refactored models:

```python
# BEFORE:
@pytest.fixture
def sample_path():
    return Path(components=["/", "home", "user", "project"])

# AFTER:
@pytest.fixture
def sample_path():
    return PurePosixPath("/home/user/project")
```

Update every fixture that constructs `Diagnostic`, `Symbol`, `DefinitionTarget`, `Reference`, etc. to remove the `project=...` argument.

### Verification (Final)

```bash
# Full lint pass:
PYTHONPATH=src ruff check src/tyo3/
PYTHONPATH=src ruff format --check src/tyo3/

# Full test suite (all categories):
build
test

# Smoke test the public API:
python -c "
from tyo3 import TyO3Session
with TyO3Session('fixtures/simple_package') as s:
    files = s.files()
    result = s.check()
    if files:
        syms = s.document_symbols(files[0])
        print(f'{len(files)} files, {len(result.diagnostics)} diags, {len(syms)} symbols')
    s.reload()
    print('reload OK')
print('session closed cleanly')
"
```


---

## Appendix A: File Change Summary

A quick reference of which files are touched in each phase.

| Phase | Files Modified | Files Created | Files Deleted |
|-------|---------------|---------------|---------------|
| 1 | `dto/mod.rs`, `lib.rs`, `convert/hover.rs`, `convert/symbols.rs`, `project.rs`, `project_service.py`, `analysis_service.py` | — | `dto/tokens.rs`, `dto/hierarchy.rs`, `errors.rs` |
| 2 | `models/core.py`, `models/analysis.py`, `models/symbols.py`, `models/navigation.py`, `models/advanced.py`, `dto/diagnostics.rs`, `dto/symbols.rs`, `dto/navigation.rs`, `convert/diagnostics.rs`, `convert/symbols.rs`, `convert/navigation.rs`, `rust_project.py` | — | — |
| 3 | `lib.rs`, `project.rs`, `rust_project.py` | — | — |
| 4 | `project.rs`, `coordinates.rs`, `convert/symbols.rs`, `convert/navigation.rs` | — | — |
| 5 | `rust_project.py`, `models/analysis.py`, `analysis_service.py`, all model files (annotation modernization) | — | — |
| 6 | `__init__.py`, `rust_project.py`, `models/analysis.py`, `models/symbols.py`, `models/navigation.py`, `conftest.py`, old service files (deprecation comments) | `session.py` | — |
| 7 | `models/core.py`, `models/analysis.py`, `models/navigation.py`, `models/__init__.py`, `rust_project.py`, all service files, `conftest.py` | — | — |

---

## Appendix B: Architecture Before and After

### Before (Current)

```
User Code
  │
  ├─→ ProjectService    ─→ RustProject ─→ PyTyProject (Rust)
  ├─→ AnalysisService   ─→ RustProject ─→ PyTyProject (Rust)
  ├─→ SymbolService     ─→ RustProject ─→ PyTyProject (Rust)
  └─→ NavigationService ─→ RustProject ─→ PyTyProject (Rust)
       (4 separate registries, 4 copies of helper code)
```

### After (Refactored)

```
User Code
  │
  └─→ TyO3Session ─→ RustProject ─→ PyTyProject (Rust)
      (single owner, single lifecycle, all operations)
```

### Data Flow Before

```
ty_ide result
  → convert::* (Rust → DTO struct)
  → serde_json::to_string (DTO → JSON string)
  → json.loads (JSON → Python dict)
  → manual field extraction (dict → model kwargs)
  → Pydantic(project=..., **kwargs) (kwargs → model with project baggage)
```

### Data Flow After

```
ty_ide result
  → convert::* (Rust → DTO struct, with typed enums)
  → serde_json::to_string (DTO → JSON string)
  → json.loads (JSON → Python dict)
  → Pydantic model_validate or _parse_* helpers (dict → lightweight model)
```

The JSON-to-string step remains in this refactor (replacing it with `pythonize` is a good follow-up but out of scope for this pass). The key improvements are: typed enums at every boundary, no `project` baggage on result models, single entry point, no duplication.

---

## Appendix C: What NOT to Change

These parts of the codebase are solid and should be preserved as-is:

1. **`devenv.nix`** — The build scripts and dev environment are excellent. Don't touch them except to update test paths if test files move.

2. **`coordinates.rs` core logic** — The 1-based to byte-offset conversion is correct and well-tested. Only add the `range_to_dto_with_index` optimization; don't restructure the algorithms.

3. **`Cargo.toml`** — Dependency pinning and release profile are fine.

4. **Test fixture projects** (`fixtures/`) — These sample projects are well-designed for testing.

5. **The `_goto()` pattern in `rust_project.py`** — This deduplication approach is correct. Extend it, don't remove it.

6. **`pyproject.toml`** — Build configuration, linting rules, and tool settings are all sensible.

---

## Appendix D: Commit Strategy

One commit per phase, with descriptive messages:

```
refactor: phase 1 — remove dead code and fix trivial bugs
refactor: phase 2 — convert to real StrEnums and typed DTOs
refactor: phase 3 — typed PyO3 exceptions replace string matching
refactor: phase 4 — deduplicate Rust backend, optimize LineIndex
refactor: phase 5 — deduplicate Python wrapper, add context manager
refactor: phase 6 — introduce TyO3Session, deprecate service layer
refactor: phase 7 — replace custom Path model, final polish
```

Each commit should leave the codebase in a fully working state with all tests passing. If a phase is too large for a single commit, split it into sub-commits (e.g., `phase 2a — Python enums`, `phase 2b — Rust DTO enums`).

---

*Guide written against commit `8df14bc` — tracks with INITIAL_CODE_REVIEW.md*
