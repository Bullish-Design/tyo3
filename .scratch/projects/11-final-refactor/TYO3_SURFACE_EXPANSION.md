# TyO3 — `ty_ide` Surface Expansion Guide

**Audience:** an engineer adding the *remaining* `ty_ide` capabilities to TyO3.
**Goal:** wrap the **entire public `ty_ide` API** behind the same clean,
Pydantic-validated `TyO3Session` surface — turning TyO3 from "the subset of ty
we needed" into "the complete programmable ty IDE engine."

> **Prerequisite:** ideally land the `FINAL_REFACTORING_GUIDE.md` first
> (especially Phase 2/3: the `py: Python<'py>` + `allow_threads` method shape, and
> Phase 6: single `TyO3Session` class). Every skeleton below assumes the
> **post-refactor** method shape. If you expand *before* refactoring, use the
> current `unsafe { Python::assume_attached() }` shape and add methods to **both**
> `RustProject` and `TyO3Session`.

The pinned engine is **astral-sh/ruff @ `3cb09eba…` (ty v0.0.40)**. All signatures
below were read from that exact revision. Anything marked **VERIFY** means "read
the struct/enum in the pinned source before writing the DTO" — field lists change
between ty versions and must not be guessed.

---

## 1. Inventory: what's wrapped vs. what's missing

`ty_ide`'s public entry points (`crates/ty_ide/src/lib.rs`):

| `ty_ide` function | Status | Signature (pinned) | Returns |
|---|---|---|---|
| `goto_definition` / `goto_declaration` / `goto_type_definition` | ✅ wrapped | `(db, file, offset)` | `Option<RangedValue<NavigationTargets>>` |
| `find_references` | ✅ wrapped | `(db, file, offset, include_decl)` | `Option<Vec<ReferenceTarget>>` |
| `hover` | ✅ wrapped | `(db, file, offset)` | `Option<RangedValue<…>>` |
| `document_symbols` | ✅ wrapped | `(db, file)` | `FlatSymbols` |
| `workspace_symbols` | ✅ wrapped | `(db, query)` | `Vec<WorkspaceSymbolInfo>` |
| `semantic_tokens` | ✅ wrapped (full) | `(db, file, Option<range>)` | `SemanticTokens` |
| `prepare_type_hierarchy` / `…_supertypes` / `…_subtypes` | ✅ wrapped | `(db, file, offset)` | `Option<…>` / `Vec<TypeHierarchyItem>` |
| **`rename` / `can_rename`** | ❌ **missing** | `(db, file, offset, new_name)` / `(db, file, offset)` | `Option<Vec<ReferenceTarget>>` / `Option<TextRange>` |
| **`completion`** | ❌ **missing** | `(db, &CompletionSettings, file, offset)` | `Vec<Completion<'db>>` |
| **`signature_help`** | ❌ **missing** | `(db, file, offset)` | `Option<SignatureHelpInfo<'_>>` |
| **`document_highlights`** | ❌ **missing** | `(db, file, offset)` | `Option<Vec<ReferenceTarget>>` |
| **`inlay_hints`** | ❌ **missing** | `(db, file, range, &InlayHintSettings)` | `Vec<InlayHint>` |
| **`hints`** | ❌ **missing** | `(db, file)` | `Vec<Hint>` |
| **`folding_ranges`** | ❌ **missing** | `(db, file, Option<range>)` | `Vec<FoldingRange>` |
| **`selection_range`** | ❌ **missing** | `(db, file, offset)` | `Vec<TextRange>` |
| **`code_actions`** | ❌ **missing** | `(db, file, diagnostic_range, diagnostic_id)` | `Vec<QuickFix>` |
| **`all_symbols`** | ❌ **missing** | `(db, importing_from, &QueryPattern)` | `Vec<AllSymbolInfo<'db>>` |
| `semantic_tokens` **range** variant | ⚠️ partial | the `Option<range>` arg is currently always `None` | extend existing method |

Also unwrapped enums worth exposing: `references::ReferencesMode` (controls
find-references scope).

**Recommended rollout order (value × ease):**
1. `document_highlights` ← simplest; reuses existing `ReferenceDto`. *Do this one
   first as the worked example.*
2. `rename` / `can_rename` ← flagship feature; also `Vec<ReferenceTarget>`.
3. `selection_range`, `folding_ranges` ← simple, range-only outputs.
4. `signature_help`, `completion` ← high value, richer DTOs + settings + `'db`.
5. `inlay_hints`, `hints` ← settings + display rendering.
6. `code_actions`, `all_symbols` ← need diagnostic pairing / `QueryPattern`.
7. `semantic_tokens` range overload ← tiny extension.

---

## 2. The repeatable recipe (six touch points)

Every feature follows the **exact same six-file pattern** the codebase already
uses. Learn it once on `document_highlights` (§3), then apply mechanically.

```
1. rust/src/dto/<feature>.rs        — serde DTO struct(s)/enum(s)   + register in dto/mod.rs
2. rust/src/convert/<feature>.rs    — pub fn convert_…(db, …)->Dto  + register in convert/mod.rs
3. rust/src/project.rs              — #[pymethods] fn …(py, path, …) -> Bound<'py, PyAny>
4. src/tyo3/models/<area>.py        — Pydantic model (from_attributes) + export in models/__init__.py
5. src/tyo3/session.py              — public TyO3Session method (validate + map exceptions)
6. src/tyo3/tests/…                 — fixture + unit test + (optional) snapshot
```

### Conventions to copy (non-negotiable, for boundary consistency)
- **DTO enums** use `#[derive(Serialize, Deserialize)]` + `#[serde(rename_all = "snake_case")]`.
  For values colliding with Python keywords, rename to the trailing-underscore form
  the Python StrEnums use (`class_`, `import_`, `async_`) — see `dto/symbols.rs`.
- **Owning the `'db` lifetime:** `ty_ide` returns borrow-tied values
  (`Completion<'db>`, `AllSymbolInfo<'db>`, `SignatureHelpInfo<'_>`). Extract every
  field into an **owned** DTO *inside* the converter while `db` is borrowed; never
  return a `'db` value across the PyO3 boundary.
- **Coordinates:** always go through `coordinates::range_to_dto_with_index(source, &line_index, range)`
  and `coordinates::position_to_offset_with_index(...)`. Build the `LineIndex` once
  per file and reuse (see `resolve_file_and_source` + the per-file cache pattern in
  `convert/navigation.rs::convert_references`).
- **Positions are 1-based** on the Python side; the Rust layer converts. Reject
  invalid positions with `PositionError` (already handled by
  `position_to_offset_with_index`).
- **Method shape (post-refactor):**
  ```rust
  fn feature<'py>(&self, py: Python<'py>, path: &str, line: u32, column: u32)
      -> PyResult<Bound<'py, PyAny>> {
      let guard = lock_state(&self.inner, "feature")?;
      let state = guard.as_ref().unwrap();
      let (file, source) = resolve_file_and_source(state, path)?;
      let line_index = LineIndex::from_source_text(&source);
      let offset = coordinates::position_to_offset_with_index(&source, &line_index, line, column)
          .map_err(PositionError::new_err)?;
      let dto = py.allow_threads(|| {
          let result = ty_ide::feature(&state.db, file, offset);
          convert::feature::convert_feature(&state.db, &source, &line_index, result)
      });
      pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
  }
  ```
- **Python adapter shape:** mirror an existing method (e.g. `find_references`) for
  the `try/except` exception mapping; return `Model.model_validate(...)` (or a list
  comprehension of it). For `Option`-returning natives, return `None` when native
  returns `None`.

---

## 3. Worked example: `document_highlights` (do this end-to-end first)

Returns `Option<Vec<ReferenceTarget>>` — the **same type** `find_references`
already converts, so you reuse `convert::navigation::convert_references` and the
existing `ReferenceDto`/`Reference` model. This proves the pipeline with minimal
new types.

### 3.1 Rust method (`rust/src/project.rs`)
```rust
/// Highlight all occurrences of the symbol at a position within its file.
fn document_highlights<'py>(
    &self, py: Python<'py>, path: &str, line: u32, column: u32,
) -> PyResult<Bound<'py, PyAny>> {
    let guard = lock_state(&self.inner, "document_highlights")?;
    let state = guard.as_ref().unwrap();
    let (file, source) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source);
    let offset = coordinates::position_to_offset_with_index(&source, &line_index, line, column)
        .map_err(PositionError::new_err)?;

    let refs = py.allow_threads(|| {
        match ty_ide::document_highlights(&state.db, file, offset) {
            Some(targets) => convert::navigation::convert_references(&state.db, &targets),
            None => Vec::new(),
        }
    });
    pythonize(py, &refs).map_err(|e| PyRuntimeError::new_err(e.to_string()))
}
```
No new DTO, no new converter — touch points 1 & 2 are skipped here.

### 3.2 Python adapter (`src/tyo3/session.py`)
```python
def document_highlights(self, path: str | StdPath, line: int, column: int) -> list[Reference]:
    """Highlight all in-file occurrences of the symbol at (line, column)."""
    # (post-refactor: body lives in TyO3Session; pre-refactor: delegate to RustProject)
    ...  # copy find_references' try/except mapping, call self._inner.document_highlights(...)
    return [Reference.model_validate(r) for r in native]
```

### 3.3 Test
Add `test_document_highlights.py` mirroring `test_file_occurrences.py`: open a
fixture, call `document_highlights` on a known symbol, assert the returned ranges
match the occurrences in that file.

### 3.4 Gate
`devenv shell -- build && devenv shell -- tests`. Commit:
`feat(surface): wrap ty_ide document_highlights`.

---

## 4. Flagship: `rename` / `can_rename`

`rename` returns `Option<Vec<ReferenceTarget>>` — every edit site for the symbol,
all replaced by `new_name`. `can_rename` returns `Option<TextRange>` (the editable
range, or `None` if the position isn't renameable).

### 4.1 DTO (`rust/src/dto/rename.rs`)
A rename is a workspace edit. Model it as new-name + edit sites:
```rust
use crate::dto::RangeDto;

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct RenameEditDto { pub path: String, pub range: RangeDto }

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct WorkspaceEditDto { pub new_name: String, pub edits: Vec<RenameEditDto> }
```
Register in `dto/mod.rs`.

### 4.2 Converter (`rust/src/convert/rename.rs`)
Reuse the per-file `(source, LineIndex)` cache pattern from
`convert_references`; map each `ReferenceTarget` → `RenameEditDto`. Wrap with the
`new_name` the caller passed.

### 4.3 Rust methods (`project.rs`)
- `can_rename(py, path, line, column) -> Bound<PyAny>` → pythonize `Option<RangeDto>`
  (return `py.None()` when `None`).
- `rename(py, path, line, column, new_name: &str) -> Bound<PyAny>` → build offset,
  call `ty_ide::rename(&db, file, offset, new_name)`, convert to `WorkspaceEditDto`,
  pythonize (or `None`).

### 4.4 Python (`models/navigation.py` + `session.py`)
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
```python
def can_rename(self, path, line, column) -> Range | None: ...
def rename(self, path, line, column, new_name: str) -> WorkspaceEdit | None: ...
```

### 4.5 Notes & tests
- ty refuses empty `new_name` (returns `None`) — surface as `None`, don't error.
- **Graph payoff:** with `WorkspaceEdit` you can build a project-wide
  rename-refactor on top of `CodeGraph`. Worth a follow-up example script.
- Test: rename a symbol used across two fixture files; assert edits cover both.

---

## 5. Per-feature specs (apply the recipe)

For each: the native call, the DTO you must build (**VERIFY fields against the
pinned source** with the listed grep), the Python model, and gotchas.

### 5.1 `selection_range`  →  `session.selection_ranges(path, line, column)`
- Native: `selection_range(db, file, offset) -> Vec<TextRange>` (expanding ranges
  outward from the cursor).
- DTO: just `Vec<RangeDto>` — no new DTO file needed; convert each `TextRange`.
- Python: `-> list[Range]`.

### 5.2 `folding_ranges`  →  `session.folding_ranges(path)`
- Native: `folding_ranges(db, file, range_filter: Option<TextRange>) -> Vec<FoldingRange>`.
  Pass `None` for the whole file (add an optional `range` param later if wanted).
- DTO (`dto/folding.rs`): `FoldingRangeDto { range: RangeDto, kind: FoldingRangeKindDto }`.
  **VERIFY:** `grep -nE "pub (struct FoldingRange|enum FoldingRangeKind)" $TYIDE/src/folding_range.rs`
  then enumerate `FoldingRangeKind` variants for the enum.
- Python (`models/advanced.py`): `FoldingRange` + `FoldingRangeKind(StrEnum)`.

### 5.3 `signature_help`  →  `session.signature_help(path, line, column)`
- Native: `signature_help(db, file, offset) -> Option<SignatureHelpInfo<'_>>`.
  Fields (pinned): `SignatureHelpInfo { signatures: …, active_signature: Option<usize> }`;
  `SignatureDetails { …, active_parameter: Option<usize> }`; `ParameterDetails { … }`.
  **VERIFY** all three structs:
  `sed -n '31,75p' $TYIDE/src/signature_help.rs`.
- DTO (`dto/signature.rs`): `SignatureHelpDto { signatures: Vec<SignatureDto>, active_signature: Option<u32> }`,
  `SignatureDto { label: String, documentation: Option<String>, parameters: Vec<ParameterDto>, active_parameter: Option<u32> }`,
  `ParameterDto { label: String, documentation: Option<String> }`.
- `'db` gotcha: `SignatureDetails<'db>` holds types — render labels/docs to
  `String` inside the converter.
- Python (`models/navigation.py`): mirror the DTOs; method returns
  `SignatureHelp | None`.

### 5.4 `completion`  →  `session.completions(path, line, column, *, auto_import=True)`
- Native: `completion(db, &CompletionSettings, file, offset) -> Vec<Completion<'db>>`.
  `CompletionSettings { auto_import: bool }` (`Default = { auto_import: true }`).
  `Completion<'db> { name: Name, qualified: Option<Name>, insert: Option<Name>, ty: Option<Type<'db>>, kind: Option<CompletionKind>, module_name: Option<&'db ModuleName>, import: Option<Edit>, … }`.
  **VERIFY** the full `Completion` struct and `CompletionKind` variants:
  `sed -n '261,300p' $TYIDE/src/completion.rs; sed -n '546,575p' $TYIDE/src/completion.rs`.
- DTO (`dto/completion.rs`): `CompletionDto { name, qualified_name: Option<String>, insert_text: Option<String>, type_: Option<String>, kind: Option<CompletionKindDto>, module_name: Option<String> }`.
  - Render `ty: Type<'db>` to a display string (use the type's display, as `hover`
    does). Don't try to serialize the `Type`.
  - `import: Option<Edit>` → optionally expose as an auto-import edit
    (`RangeDto` + text); start by ignoring it, add later.
- Python (`models/completion.py`, new): `Completion` + `CompletionKind(StrEnum)`.
  Method signature exposes `auto_import: bool = True` → build
  `CompletionSettings { auto_import }`.

### 5.5 `inlay_hints`  →  `session.inlay_hints(path, *, range=None)`
- Native: `inlay_hints(db, file, range: TextRange, &InlayHintSettings) -> Vec<InlayHint>`.
  `InlayHintSettings { variable_types: bool, call_argument_names: bool, … }` (has `Default`).
  Note: **`range` is required** — for "whole file" pass the file's full
  `TextRange` (`TextRange::up_to(text_len)`), or accept an optional Python range.
- DTO (`dto/inlay.rs`): `InlayHintDto { position: PositionDto, label: String, kind: Option<InlayHintKindDto> }`.
  - `InlayHint.label` is a structured `InlayHintLabel` (parts) — render to a flat
    `String` via its `display()`/`parts()` for v1. **VERIFY** `InlayHintKind` variants.
- Python (`models/advanced.py`): `InlayHint` + `InlayHintKind(StrEnum)`. Expose
  `variable_types`/`call_argument_names` kwargs mapping to the settings struct.

### 5.6 `hints`  →  `session.hints(path)`
- Native: `hints(db, file) -> Vec<Hint>`; `Hint` exposes `.message() -> String`
  and `HintKind`. **VERIFY** `HintKind` variants and whether `Hint` carries a
  range (`grep -nE "pub (struct Hint|enum HintKind|fn range)" $TYIDE/src/hints.rs`).
- DTO (`dto/hints.rs`): `HintDto { message: String, kind: HintKindDto, range: Option<RangeDto> }`.
- Python: `Hint` + `HintKind(StrEnum)`; method returns `list[Hint]`.

### 5.7 `code_actions`  →  `session.code_actions(path, range, code)`
- Native: `code_actions(db, file, diagnostic_range: TextRange, diagnostic_id: &str) -> Vec<QuickFix>`.
  It's **keyed off a diagnostic** — the natural caller passes a `Diagnostic`'s
  `range` and `code` from `check()`. `diagnostic_id` is the lint name.
  **VERIFY** `QuickFix` fields: `sed -n '14,20p' $TYIDE/src/code_action.rs` (expect
  a title + a set of text edits).
- DTO (`dto/code_action.rs`): `QuickFixDto { title: String, edits: Vec<TextEditDto> }`,
  `TextEditDto { path: String, range: RangeDto, new_text: String }`.
- Rust method: convert the Python `range` (1-based) → `TextRange` via the
  inverse-offset path on both endpoints. Returns `[]` if the `code` isn't a known
  lint (native already guards this).
- Python: `code_actions(self, path, range: Range, code: str) -> list[QuickFix]`.

### 5.8 `all_symbols`  →  `session.all_symbols(query, *, importing_from)`
- Native: `all_symbols(db, importing_from: File, query: &QueryPattern) -> Vec<AllSymbolInfo<'db>>`.
  Like `workspace_symbols` but importable-symbol-aware (used for auto-import). It
  returns immediately for empty/match-everything queries.
  `QueryPattern` lives in `ty_ide/src/symbols.rs` (already used internally by
  `workspace_symbols`). **VERIFY** its constructor:
  `grep -nE "impl QueryPattern|pub fn (new|from)" $TYIDE/src/symbols.rs`.
  `AllSymbolInfo` exposes `name_in_file()`, `qualified()`, `kind()`, `deprecated()`,
  `module()`.
- DTO: reuse `SymbolDto` (or a slim `AllSymbolDto { name, qualified_name, kind, module_name, deprecated }`).
- Rust method needs an `importing_from` file — resolve from a `path` arg (or
  default to the project root's `__init__`). Document the choice.
- Python: `all_symbols(self, query: str, *, importing_from: str | StdPath) -> list[Symbol]`.

### 5.9 `semantic_tokens` range overload (extend existing)
- The native call already takes `Option<TextRange>`; today TyO3 passes `None`.
  Add an optional `range: Range | None = None` to `session.semantic_tokens(path)`,
  convert to `TextRange` when provided, pass through. No new DTO.

### 5.10 (Optional) `references::ReferencesMode`
- Expose find-references scoping by adding a `mode` parameter to
  `find_references`. **VERIFY** `ReferencesMode` variants:
  `grep -nE "pub enum ReferencesMode" -A8 $TYIDE/src/references.rs`. Map a Python
  StrEnum → the Rust enum. Only do this if `find_references` actually accepts a
  mode in the pinned signature; otherwise skip.

---

## 6. Cross-cutting concerns (read once)

- **Settings structs have `Default`.** `CompletionSettings::default()` and
  `InlayHintSettings::default()` exist — construct from defaults and override only
  the fields you expose as kwargs. Don't hardcode bools.
- **Never serialize `'db` types** (`Type<'db>`, `Name`, `ModuleName`, AST nodes).
  Convert to `String`/owned DTO fields inside the converter while `db` is borrowed.
  `Name`/`ModuleName` → `.as_str().to_string()`; `Type<'db>` → its display string.
- **Settings/registries needing `db`:** `code_actions` uses `db.lint_registry()`;
  keep all such calls inside the `allow_threads` closure (they're pure Rust).
- **GIL release:** put the `ty_ide::*` call **and** the DTO conversion inside
  `py.allow_threads(|| …)`; only `pythonize` runs with the GIL (Phase 3 pattern).
  If a return type isn't `Ungil`/`Send`, drop `allow_threads` for that method only.
- **Exception mapping:** every Python adapter copies the `find_references`
  `try/except` ladder (`_NativeClosedError → ProjectClosedError`,
  `_NativePositionError/OverflowError → PositionError`,
  `_NativePathError → PathResolutionError`, else `InternalTyError`).
- **`Option` natives → `None`:** `hover`/`type_hierarchy` already model this;
  `rename`, `can_rename`, `signature_help`, `document_highlights` return `None`
  when the native returns `None`.

---

## 7. Discovery checklist (run before writing each feature's DTO)

```bash
TYIDE="/home/andrew/.cargo/git/checkouts/ruff-*/3cb09eb/crates/ty_ide"
# 1. entrypoint signature
grep -nE "^pub fn <feature>" $TYIDE/src/<feature>.rs
# 2. every public struct/enum the return type references
grep -nE "pub (struct|enum) " $TYIDE/src/<feature>.rs
# 3. field-by-field for each struct you'll mirror in a DTO
sed -n '<start>,<end>p' $TYIDE/src/<feature>.rs
# 4. any *Settings struct's Default impl
grep -nE "impl Default for .*Settings" -A6 $TYIDE/src/<feature>.rs
```
> If you can't find a field/variant in the pinned source, **it doesn't exist in
> this ty version** — don't invent it. Match the pinned API exactly so a future
> ty bump (see `FINAL_REFACTORING_GUIDE.md` §10.9) is a controlled diff.

---

## 8. Per-feature Definition of Done

A feature is complete when:
- DTO(s) in `rust/src/dto/`, registered in `dto/mod.rs`; serde renames match the
  Python StrEnum values exactly.
- Converter in `rust/src/convert/`, registered in `convert/mod.rs`; reuses the
  coordinate helpers and per-file `LineIndex` cache; returns owned DTOs.
- `#[pymethods]` method on `PyTyProject` using the `py: Python<'py>` +
  `allow_threads` shape; positions validated → `PositionError`.
- Pydantic model(s) in `src/tyo3/models/…` with `ConfigDict(from_attributes=True)`,
  exported from `models/__init__.py`.
- Public `TyO3Session` method with the standard exception mapping; `None`
  passthrough where the native returns `Option`.
- Test: a fixture + unit test asserting shape and coordinates; snapshot test in
  the `test_rust_snapshots.py` style for stable outputs.
- README API table updated; `devenv shell -- build && tests && test-rust` green;
  `ruff` + `clippy` clean.

## 9. Suggested PR slicing

Ship one feature per PR, in the §1 rollout order. Each PR is ~6 small files and
fully self-contained. Group the trivial range-only ones (`selection_range`,
`folding_ranges`, `semantic_tokens` range) into a single PR if you like. Save
`completion` + `all_symbols` (auto-import territory) for last — they're the
richest and benefit from the patterns established by the earlier features.

When complete, TyO3 exposes the **entire `ty_ide` surface** — go-to, references,
hover, symbols, semantic tokens, type hierarchy, **rename, completion, signature
help, highlights, inlay hints, folding, selection, code actions, and importable
symbol search** — every one of them Pydantic-typed and graph-queryable.
