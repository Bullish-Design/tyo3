# TyO3 — `ty_ide` Surface Expansion Guide

**Audience:** an engineer adding the *remaining* `ty_ide` capabilities to TyO3.
**Goal:** wrap the **entire public `ty_ide` API** behind the current
Pydantic-validated TyO3 surface: `TyO3Session` for latest-revision reads and
`Snapshot` for revision-pinned reads.

This guide assumes the concurrent snapshot architecture from
`OPTION_C_SNAPSHOT_IMPLEMENTATION.md` has landed:

- Rust read methods release the GIL with `py.detach(...)`.
- Rust read methods clone the current `TyProjectState` with `clone_locked_state`
  before analysis, then drop the lock.
- Analysis logic lives in GIL-free `compute_*` helpers that touch no Python state.
- Native read parity is required: `PyTyProject` and `PySnapshot` expose the same
  read methods.
- Python read methods live once on `_ReadOps`, shared by `TyO3Session` and
  `Snapshot`.

The pinned engine is **astral-sh/ruff @ `3cb09eba...` (ty v0.0.40)**. All
signatures below were read from that exact revision. Anything marked **VERIFY**
means "read the struct/enum in the pinned source before writing the DTO" — field
lists change between ty versions and must not be guessed.

> ## Golden rule — run executable commands through devenv
>
> This project only works inside the Nix devenv. Every build, test, cargo,
> maturin, ruff, or Python invocation must be prefixed with
> `devenv shell -- ...` or run from inside an interactive `devenv shell`.
>
> Read-only greps and file inspection are fine outside devenv.

---

## 1. Inventory: what's wrapped vs. what's missing

`ty_ide`'s public entry points (`crates/ty_ide/src/lib.rs`):

| `ty_ide` function | Status | Signature (pinned) | Returns |
|---|---|---|---|
| `goto_definition` / `goto_declaration` / `goto_type_definition` | wrapped | `(db, file, offset)` | `Option<RangedValue<NavigationTargets>>` |
| `find_references` | wrapped | `(db, file, offset, include_decl)` | `Option<Vec<ReferenceTarget>>` |
| `hover` | wrapped | `(db, file, offset)` | `Option<RangedValue<...>>` |
| `document_symbols` | wrapped | `(db, file)` | `FlatSymbols` |
| `workspace_symbols` | wrapped | `(db, query)` | `Vec<WorkspaceSymbolInfo>` |
| `semantic_tokens` | wrapped, full-file only | `(db, file, Option<range>)` | `SemanticTokens` |
| `prepare_type_hierarchy` / `..._supertypes` / `..._subtypes` | wrapped | `(db, file, offset)` | `Option<...>` / `Vec<TypeHierarchyItem>` |
| **`rename` / `can_rename`** | missing | `(db, file, offset, new_name)` / `(db, file, offset)` | `Option<Vec<ReferenceTarget>>` / `Option<TextRange>` |
| **`completion`** | missing | `(db, &CompletionSettings, file, offset)` | `Vec<Completion<'db>>` |
| **`signature_help`** | missing | `(db, file, offset)` | `Option<SignatureHelpInfo<'_>>` |
| **`document_highlights`** | missing | `(db, file, offset)` | `Option<Vec<ReferenceTarget>>` |
| **`inlay_hints`** | missing | `(db, file, range, &InlayHintSettings)` | `Vec<InlayHint>` |
| **`hints`** | missing | `(db, file)` | `Vec<Hint>` |
| **`folding_ranges`** | missing | `(db, file, Option<range>)` | `Vec<FoldingRange>` |
| **`selection_range`** | missing | `(db, file, offset)` | `Vec<TextRange>` |
| **`code_actions`** | missing | `(db, file, diagnostic_range, diagnostic_id)` | `Vec<QuickFix>` |
| **`all_symbols`** | missing | `(db, importing_from, &QueryPattern)` | `Vec<AllSymbolInfo<'db>>` |
| `semantic_tokens` range variant | partial | the `Option<range>` arg is currently always `None` | extend existing method |

Also unwrapped enums worth exposing: `references::ReferencesMode` if the pinned
`find_references` entry point accepts it.

**Recommended rollout order (value x ease):**

1. `document_highlights` — simplest; reuses existing `ReferenceDto` and `Reference`.
2. `rename` / `can_rename` — flagship feature; also `Vec<ReferenceTarget>`.
3. `selection_range`, `folding_ranges` — simple range-only outputs.
4. `signature_help`, `completion` — high value, richer DTOs + settings + `'db`.
5. `inlay_hints`, `hints` — settings + display rendering.
6. `code_actions`, `all_symbols` — diagnostic pairing / `QueryPattern`.
7. `semantic_tokens` range overload — small extension to an existing method.

---

## 2. Current architecture: the shape every feature must follow

The old recipe was "add one native method, wrap it on `TyO3Session`." That is now
insufficient. A complete feature touches the Rust analysis core, both native
read handles, the native stub, and the shared Python read base.

### 2.1 Required touch points

Most features use this pattern:

```text
1. rust/src/dto/<feature>.rs        — serde DTOs/enums, registered in dto/mod.rs
2. rust/src/convert/<feature>.rs    — owned conversion from ty values, registered in convert/mod.rs
3. rust/src/project.rs              — compute_<feature>(...) GIL-free analysis core
4. rust/src/project.rs              — PyTyProject method using clone_locked_state + py.detach
5. rust/src/project.rs              — matching PySnapshot method with read parity
6. src/tyo3/_native_impl.pyi        — method on TyProject and TySnapshot
7. src/tyo3/models/<area>.py        — Pydantic models, exported from models/__init__.py
8. src/tyo3/session.py              — one public method on _ReadOps
9. src/tyo3/tests/...               — focused unit/snapshot/concurrency coverage as needed
10. README.md                       — public API table/docs update
```

Some simple features skip DTO or converter files by reusing existing DTOs. They
still require both native classes, the stub, `_ReadOps`, tests, and docs.

### 2.2 Rust method shape

All heavy ty work must happen in a `compute_*` helper:

```rust
fn compute_feature(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
) -> Result<dto::FeatureDto, AnalysisError> {
    let (file, source) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source);
    let offset = coordinates::position_to_offset_with_index(&source, &line_index, line, column)
        .map_err(AnalysisError::Position)?;

    let native = ty_ide::feature(&state.db, file, offset);
    Ok(convert::feature::convert_feature(&state.db, &source, &line_index, native))
}
```

Then both native classes use the same thin wrapper shape:

```rust
fn feature<'py>(
    &self,
    py: Python<'py>,
    path: &str,
    line: u32,
    column: u32,
) -> PyResult<Bound<'py, PyAny>> {
    let state = clone_locked_state(&self.inner, "feature")?;
    let path = path.to_owned();
    let dto = py.detach(move || compute_feature(&state, &path, line, column))
        .map_err(AnalysisError::into_pyerr)?;
    pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
}
```

Rules:

- Own all borrowed arguments before the `detach` closure (`path.to_owned()`,
  `query.to_owned()`, `new_name.to_owned()`).
- Do not capture `Python<'_>`, `Bound<'_, T>`, `&PyAny`, `&self`, or any other
  non-`Send` Python value in the `detach` closure.
- Build Python objects only after `detach` returns: `pythonize`, `py.None()`,
  and custom Python exceptions stay outside the closure.
- `AnalysisError` is the only path/position error type produced inside
  `compute_*`; convert it to `PyErr` in the wrapper with
  `AnalysisError::into_pyerr`.
- `#[pyclass(..., frozen)]` stays on both `PyTyProject` and `PySnapshot`.
- Keep `Mutex<Option<TyProjectState>>`; it is the soundness mechanism because
  `ProjectDatabase` is `Send + Clone + !Sync`.

### 2.3 Option-returning method shape

`Option` natives return `None` to Python, but `py.None()` is created after
`detach`:

```rust
fn feature<'py>(&self, py: Python<'py>, path: &str, line: u32, column: u32)
    -> PyResult<Bound<'py, PyAny>>
{
    let state = clone_locked_state(&self.inner, "feature")?;
    let path = path.to_owned();
    match py.detach(move || compute_feature(&state, &path, line, column))
        .map_err(AnalysisError::into_pyerr)?
    {
        None => Ok(py.None().bind(py).clone()),
        Some(dto) => pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string())),
    }
}
```

### 2.4 Python adapter shape

Add the public method to `_ReadOps`, not directly to `TyO3Session` or `Snapshot`.
Both classes inherit it.

```python
def feature(self, path: str | StdPath, line: int, column: int) -> Feature | None:
    self._check_open()
    try:
        native = self._inner.feature(str(path), line, column)
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e
    except _NativePositionError as e:
        raise PositionError(str(e)) from e
    except _NativePathError as e:
        raise PathResolutionError(str(e)) from e
    except OverflowError as e:
        raise PositionError(str(e)) from e
    except Exception as e:
        raise InternalTyError(f"Unexpected error in feature(): {e}") from e

    if native is None:
        return None
    return Feature.model_validate(native)
```

The native stub must declare the method on both classes:

```python
class TyProject:
    def feature(self, path: str, line: int, column: int) -> Any: ...

class TySnapshot:
    def feature(self, path: str, line: int, column: int) -> Any: ...
```

---

## 3. Conventions to copy

- **DTO enums** use `#[derive(Serialize, Deserialize)]` plus
  `#[serde(rename_all = "snake_case")]`. For values colliding with Python
  keywords, rename to the trailing-underscore form the Python StrEnums use
  (`class_`, `import_`, `async_`).
- **Owning `'db` values:** `ty_ide` returns borrow-tied values
  (`Completion<'db>`, `AllSymbolInfo<'db>`, `SignatureHelpInfo<'_>`). Extract
  every field into owned DTO data inside `compute_*`/converter code while `db`
  is borrowed. Never return a `'db` value across the PyO3 boundary.
- **Coordinates:** use `coordinates::range_to_dto_with_index(source, &line_index,
  range)` and `coordinates::position_to_offset_with_index(...)`. Build the
  `LineIndex` once per file and reuse it. For cross-file result sets, copy the
  per-file cache pattern from `convert/navigation.rs::convert_references` and
  `compute_workspace_symbols`.
- **Positions are 1-based** on the Python side; Rust converts them. Invalid
  positions become `PositionError`.
- **Settings structs have `Default`.** Start from `CompletionSettings::default()`
  or `InlayHintSettings::default()` and override only fields exposed as kwargs.
- **Do not serialize ty internals.** `Type<'db>`, `Name`, `ModuleName`, AST nodes,
  and edits with ty-owned text must become `String`/owned DTO fields before
  leaving Rust analysis code.
- **No Python inside `detach`.** The closure may touch only Rust data and must
  return a `Send`/`Ungil` value such as a DTO or `Result<Dto, AnalysisError>`.

---

## 4. Worked example: `document_highlights`

This is the first feature to implement because it reuses existing reference
conversion. Native returns `Option<Vec<ReferenceTarget>>`; Python should return
`list[Reference]`. Ty returns `None` when no symbol is highlightable; TyO3 should
surface that as an empty list, matching `find_references`.

### 4.1 Rust compute core

Add this near the other `compute_*` helpers in `rust/src/project.rs`:

```rust
fn compute_document_highlights(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
) -> Result<Vec<dto::ReferenceDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(&source_str, &line_index, line, column)
        .map_err(AnalysisError::Position)?;

    let refs = match ty_ide::document_highlights(&state.db, file, offset) {
        Some(targets) => convert::navigation::convert_references(&state.db, &targets),
        None => Vec::new(),
    };
    Ok(refs)
}
```

No new DTO or converter is needed.

### 4.2 Native wrappers on both read handles

Add the same method body to `impl PyTyProject` and `impl PySnapshot`:

```rust
fn document_highlights<'py>(
    &self,
    py: Python<'py>,
    path: &str,
    line: u32,
    column: u32,
) -> PyResult<Bound<'py, PyAny>> {
    let state = clone_locked_state(&self.inner, "document_highlights")?;
    let path = path.to_owned();
    let refs = py.detach(move || compute_document_highlights(&state, &path, line, column))
        .map_err(AnalysisError::into_pyerr)?;
    pythonize(py, &refs).map_err(|e| PyRuntimeError::new_err(e.to_string()))
}
```

### 4.3 Native stub

Add to both `TyProject` and `TySnapshot` in `src/tyo3/_native_impl.pyi`:

```python
def document_highlights(self, path: str, line: int, column: int) -> Any: ...
```

### 4.4 Python `_ReadOps`

Add one shared method in `src/tyo3/session.py`:

```python
def document_highlights(self, path: str | StdPath, line: int, column: int) -> list[Reference]:
    """Highlight all in-file occurrences of the symbol at *(line, column)*."""
    self._check_open()
    try:
        native_refs = self._inner.document_highlights(str(path), line, column)
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e
    except _NativePositionError as e:
        raise PositionError(str(e)) from e
    except _NativePathError as e:
        raise PathResolutionError(str(e)) from e
    except OverflowError as e:
        raise PositionError(str(e)) from e
    except Exception as e:
        raise InternalTyError(f"Unexpected error in document_highlights(): {e}") from e

    return [Reference.model_validate(r) for r in native_refs]
```

### 4.5 Tests and gates

- Add a fixture test mirroring `test_file_occurrences.py` or
  `test_references.py`.
- Assert that both `session.document_highlights(...)` and
  `session.snapshot().document_highlights(...)` return equivalent results.
- Add a closed-snapshot behavior test if the method is not covered by an
  existing parity test.

Run:

```bash
devenv shell -- check-rust
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/<new-test-file>.py -v
devenv shell -- test-rust
devenv shell -- tests
```

Suggested commit:

```bash
git commit -am "feat(surface): wrap ty_ide document_highlights"
```

---

## 5. Flagship: `rename` / `can_rename`

`rename` returns `Option<Vec<ReferenceTarget>>` — edit sites for replacing the
symbol with `new_name`. `can_rename` returns `Option<TextRange>` — the editable
range or `None`.

### 5.1 DTOs

`rust/src/dto/rename.rs`:

```rust
use crate::dto::RangeDto;

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct RenameEditDto {
    pub path: String,
    pub range: RangeDto,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct WorkspaceEditDto {
    pub new_name: String,
    pub edits: Vec<RenameEditDto>,
}
```

Register the module and re-export in `dto/mod.rs`.

### 5.2 Converter

`rust/src/convert/rename.rs` should reuse the per-file `(source, LineIndex)` cache
pattern from `convert_references`. Map each `ReferenceTarget` to a
`RenameEditDto` with the resolved file path and range. Return
`WorkspaceEditDto { new_name, edits }`.

### 5.3 Rust compute cores

- `compute_can_rename(state, path, line, column) -> Result<Option<RangeDto>, AnalysisError>`
- `compute_rename(state, path, line, column, new_name) -> Result<Option<WorkspaceEditDto>, AnalysisError>`

Own `new_name` before `detach` in the native wrappers.

### 5.4 Python models and methods

`src/tyo3/models/navigation.py`:

```python
class RenameEdit(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    path: PurePosixPath
    range: Range

class WorkspaceEdit(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    new_name: str
    edits: list[RenameEdit]
```

`_ReadOps`:

```python
def can_rename(self, path, line, column) -> Range | None: ...
def rename(self, path, line, column, new_name: str) -> WorkspaceEdit | None: ...
```

### 5.5 Tests

- Rename a symbol used across two fixture files and assert all edit ranges.
- Assert empty/invalid `new_name` returns `None` if ty returns `None`; do not
  invent a Python validation error unless the native API errors.
- Assert snapshot parity.

---

## 6. Per-feature specs

For each feature: verify the pinned `ty_ide` source first, build owned DTOs, add
`compute_*`, add both native wrappers, add `_ReadOps`, update stubs/docs/tests.

### 6.1 `selection_range` -> `selection_ranges(path, line, column)`

- Native: `selection_range(db, file, offset) -> Vec<TextRange>`.
- DTO: `Vec<RangeDto>`; no new DTO file needed.
- Python: `-> list[Range]`.
- Compute core converts each `TextRange` using the file's `LineIndex`.

### 6.2 `folding_ranges` -> `folding_ranges(path)`

- Native: `folding_ranges(db, file, range_filter: Option<TextRange>) -> Vec<FoldingRange>`.
- Start with whole-file only by passing `None`; add a range overload later.
- DTO: `FoldingRangeDto { range: RangeDto, kind: FoldingRangeKindDto }`.
- VERIFY:
  `grep -nE "pub (struct FoldingRange|enum FoldingRangeKind)" $TYIDE/src/folding_range.rs`
- Python: `FoldingRange` + `FoldingRangeKind(StrEnum)`.

### 6.3 `signature_help` -> `signature_help(path, line, column)`

- Native: `signature_help(db, file, offset) -> Option<SignatureHelpInfo<'_>>`.
- VERIFY:
  `sed -n '31,75p' $TYIDE/src/signature_help.rs`
- DTO:
  `SignatureHelpDto { signatures: Vec<SignatureDto>, active_signature: Option<u32> }`
  `SignatureDto { label: String, documentation: Option<String>, parameters: Vec<ParameterDto>, active_parameter: Option<u32> }`
  `ParameterDto { label: String, documentation: Option<String> }`
- `'db` gotcha: render labels/docs to `String` inside the converter.
- Python: `SignatureHelp | None`.

### 6.4 `completion` -> `completions(path, line, column, *, auto_import=True)`

- Native: `completion(db, &CompletionSettings, file, offset) -> Vec<Completion<'db>>`.
- Settings: start with `CompletionSettings::default()`, override `auto_import`.
- VERIFY:
  `sed -n '261,300p' $TYIDE/src/completion.rs`
  `sed -n '546,575p' $TYIDE/src/completion.rs`
- DTO:
  `CompletionDto { name, qualified_name: Option<String>, insert_text: Option<String>, type_: Option<String>, kind: Option<CompletionKindDto>, module_name: Option<String> }`
- Render `Type<'db>` to a display string. Do not serialize `Type`.
- `import: Option<Edit>` can be ignored for v1; expose auto-import edits later as
  `TextEditDto` if needed.
- Python: `Completion` + `CompletionKind(StrEnum)`.

### 6.5 `inlay_hints` -> `inlay_hints(path, *, range=None, ...)`

- Native: `inlay_hints(db, file, range: TextRange, &InlayHintSettings) -> Vec<InlayHint>`.
- The range is required natively. For whole file, pass `TextRange::up_to(text_len)`.
- Settings: start with `InlayHintSettings::default()`, override exposed kwargs
  such as `variable_types` and `call_argument_names`.
- DTO: `InlayHintDto { position: PositionDto, label: String, kind: Option<InlayHintKindDto> }`.
- `InlayHint.label` is structured; flatten to `String` for v1.
- VERIFY `InlayHintKind` variants in pinned source.
- Python: `InlayHint` + `InlayHintKind(StrEnum)`.

### 6.6 `hints` -> `hints(path)`

- Native: `hints(db, file) -> Vec<Hint>`.
- VERIFY:
  `grep -nE "pub (struct Hint|enum HintKind|fn range)" $TYIDE/src/hints.rs`
- DTO: `HintDto { message: String, kind: HintKindDto, range: Option<RangeDto> }`.
- Python: `Hint` + `HintKind(StrEnum)`.

### 6.7 `code_actions` -> `code_actions(path, range, code)`

- Native:
  `code_actions(db, file, diagnostic_range: TextRange, diagnostic_id: &str) -> Vec<QuickFix>`.
- The natural caller passes a `Diagnostic.range` and diagnostic `code` from
  `check()`.
- VERIFY:
  `sed -n '14,30p' $TYIDE/src/code_action.rs`
- DTO:
  `QuickFixDto { title: String, edits: Vec<TextEditDto> }`
  `TextEditDto { path: String, range: RangeDto, new_text: String }`
- Convert Python `Range` endpoints back to `TextRange` with coordinate helpers.
- Python: `list[QuickFix]`; return `[]` for unknown lint/code if native does.

### 6.8 `all_symbols` -> `all_symbols(query, *, importing_from)`

- Native:
  `all_symbols(db, importing_from: File, query: &QueryPattern) -> Vec<AllSymbolInfo<'db>>`.
- VERIFY:
  `grep -nE "impl QueryPattern|pub fn (new|from)" $TYIDE/src/symbols.rs`
- `AllSymbolInfo` exposes name, qualified name, kind, module/deprecation data.
- DTO: reuse `SymbolDto` if it has enough fields; otherwise add
  `AllSymbolDto { name, qualified_name, kind, module_name, deprecated }`.
- Resolve `importing_from` from a required Python path argument; do not silently
  guess the root file.
- Python: `all_symbols(self, query: str, *, importing_from: str | StdPath) -> list[Symbol]`.

### 6.9 `semantic_tokens` range overload

- Native already takes `Option<TextRange>`; `compute_semantic_tokens` currently
  passes `None`.
- Add optional `range: Range | None = None` to `_ReadOps.semantic_tokens`.
- Convert the Python range to `TextRange` in Rust and pass `Some(range)`.
- Update both native class methods and stubs to accept the optional range shape
  you choose. Prefer an explicit four-integer argument or a small DTO-compatible
  dict shape; avoid accepting arbitrary Python objects in Rust.

### 6.10 Optional `references::ReferencesMode`

- VERIFY:
  `grep -nE "pub enum ReferencesMode" -A8 $TYIDE/src/references.rs`
- Only expose this if the pinned `find_references` signature accepts a mode.
- Map Python `StrEnum` values to Rust enum variants exactly.

---

## 7. Discovery checklist

Run before writing each feature's DTO:

```bash
TYIDE="/home/andrew/.cargo/git/checkouts/ruff-*/3cb09eb/crates/ty_ide"

# 1. Entrypoint signature
grep -nE "^pub fn <feature>" $TYIDE/src/<feature>.rs

# 2. Public structs/enums referenced by the return type
grep -nE "pub (struct|enum) " $TYIDE/src/<feature>.rs

# 3. Field-by-field details
sed -n '<start>,<end>p' $TYIDE/src/<feature>.rs

# 4. Settings defaults
grep -nE "impl Default for .*Settings" -A8 $TYIDE/src/<feature>.rs
```

If a field or variant is not in the pinned source, it does not exist in this ty
version. Match the pinned API exactly so a future ty bump is a controlled diff.

---

## 8. Verification checklist for every feature

Read-only greps:

```bash
# The compute core exists.
grep -n "fn compute_<feature>" rust/src/project.rs

# The native method exists on both read handles.
grep -n "fn <feature>" rust/src/project.rs
# Expect two method hits: one in impl PyTyProject, one in impl PySnapshot.

# The method uses py.detach, not allow_threads.
grep -n "allow_threads\\|assume_attached\\|with_gil" rust/src/project.rs
# Expect no hits for new code.

# Python read method lives on _ReadOps, not duplicated.
grep -n "class _ReadOps\\|def <feature>" src/tyo3/session.py

# Native stubs have parity.
grep -n "def <feature>" src/tyo3/_native_impl.pyi
# Expect two hits: TyProject and TySnapshot.
```

Executable gates:

```bash
devenv shell -- check-rust
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/<feature-test>.py -v
devenv shell -- test-rust
devenv shell -- tests
devenv shell -- ruff check src
```

Use `devenv shell -- rebuild` if a freshly added native method is missing at
runtime. If stale `_native_impl*.so` shadowing is suspected, follow the cleanup
guidance in `OPTION_C_SNAPSHOT_IMPLEMENTATION.md`.

---

## 9. Per-feature Definition of Done

A feature is complete when:

- DTOs in `rust/src/dto/` are registered in `dto/mod.rs`, unless existing DTOs
  are intentionally reused.
- Converters in `rust/src/convert/` are registered in `convert/mod.rs`, unless
  existing converters are intentionally reused.
- A GIL-free `compute_*` helper exists and returns owned DTO data or
  `Result<owned DTO, AnalysisError>`.
- `PyTyProject` exposes the method using `clone_locked_state` + owned args +
  `py.detach`.
- `PySnapshot` exposes the same read method with the same semantics. No
  `snapshot()` method is added to `PySnapshot`.
- `src/tyo3/_native_impl.pyi` declares the method on both native classes.
- Pydantic models exist with `ConfigDict(from_attributes=True)` and are exported
  from `models/__init__.py`.
- `_ReadOps` exposes the public Python method with the standard exception
  mapping. `TyO3Session` and `Snapshot` get the feature through inheritance.
- `None` is preserved for real optional results; "no hits" collections return
  empty lists.
- Tests cover session behavior, snapshot parity, path/position errors where
  relevant, and stable coordinates.
- README/API docs are updated.
- `devenv shell -- check-rust`, `build`, focused tests, `test-rust`, `tests`, and
  `ruff check src` are green.

---

## 10. Suggested PR slicing

Ship one feature per PR in the rollout order from section 1. Each PR should be
small and independently green. Group the trivial range-only features
(`selection_range`, `folding_ranges`, and the `semantic_tokens` range overload)
only if the shared range-input plumbing makes that cheaper.

Save `completion` and `all_symbols` for later. They involve borrow-tied ty data,
settings, importable-symbol semantics, and likely benefit from patterns established
by the simpler features.

When complete, TyO3 exposes the full programmable ty IDE engine across both read
surfaces: `TyO3Session` for latest project state and `Snapshot` for pinned,
thread-shareable, revision-consistent analysis.
