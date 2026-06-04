# TyO3 — FINAL Refactoring Guide

**Audience:** an engineer (intern) executing the refactor end-to-end.
**Goal:** bring TyO3 to its intended final state — *the cleanest, simplest, most
elegant version of this architecture* — by removing every gap between what the
code **is** and what it **claims to be**.

This guide is **prescriptive**. Follow the phases in order. Each phase is
independently committable, independently testable, and ordered so the diff stays
bisectable (pure deletions first, behavior-preserving refactors next, the two
larger judgment-call refactors last).

> **None of this changes the architecture.** ty/Ruff → Rust PyO3 → `pythonize`
> → dict → Pydantic → `CodeGraph` is correct and stays. We are removing dead
> weight, eliminating `unsafe`, unlocking GIL release, and collapsing one
> duplicated layer.

---

## Phase 0 — Ground rules & baseline (do this first)

### 0.1 Environment
**Every command runs inside the devenv shell.** Never call `pytest`, `cargo`,
`maturin`, or `ruff` directly.

```bash
devenv shell          # enter once, stay in it
```

Key scripts (defined in `devenv.nix`): `build`, `build-release`, `tests`,
`test-quick`, `test-rust`, `test-property`, `test-ci`, `clean`, `status`,
`check-so`.

### 0.2 Branch & baseline
```bash
git checkout -b chore/final-refactor
devenv shell -- build          # compile the native extension (debug)
devenv shell -- tests          # capture the GREEN baseline — must pass before you start
```
Record the pass count (expected ~390). **If the baseline is red, stop and report.**

### 0.3 Working rules
- One phase = one (or a few) focused commit(s). Use the commit messages given.
- After **every** phase: `devenv shell -- build && devenv shell -- tests` must be green.
- After any Rust change, rebuild (`build`) before running Python tests — stale
  `.so` files cause confusing failures. Use `devenv shell -- check-so` if unsure.
- Run `devenv shell -- ruff check src` and `ruff format src` before each commit.
- Do **not** combine phases. If a phase fights you, finish/commit the prior one.

---

## Phase 1 — Delete dead code (pure subtraction, zero behavior change)

Every item below was verified unused via grep. This phase only deletes.

### 1.1 Delete the entire `tyo3-derive` crate
The `PyFields` derive macro and the `__fields__` machinery were the **old**
pyclass-based conversion strategy. Commit `fcec2e4` replaced it with
`pythonize`. The crate is not a dependency of `rust/Cargo.toml` and nothing
references `PyFields` / `__fields__` / `_to_python`.

```bash
git rm -r rust/tyo3-derive
```
Then confirm nothing referenced it:
```bash
grep -rn "tyo3-derive\|tyo3_derive\|PyFields\|__fields__\|_to_python" rust/ src/ \
  --include="*.toml" --include="*.rs" --include="*.py"   # expect: no matches
```

### 1.2 Delete dead Rust convenience wrappers
In `rust/src/coordinates.rs`, only the `_with_index` variants are ever called.
Delete the two wrappers that compute a `LineIndex` internally:

- `pub fn position_to_offset(...)` (the non-`_with_index` one, ~lines 10–16)
- `pub fn range_to_dto(...)` (the non-`_with_index` one, ~lines 126–132)

Keep `position_to_offset_with_index`, `range_to_dto_with_index`,
`char_len_to_byte_offset`, and the `#[cfg(test)]` module.

### 1.3 Delete dead Python graph code
- `src/tyo3/graph/graph.py`: delete the method `_resolve_references_for_symbol`
  (~lines 429–473). It is superseded by `_resolve_references_via_occurrences`
  and is never called.
- `src/tyo3/graph/identity.py`: delete `make_external_id` and
  `qualified_name_from_symbol_id` (both unused). Keep `make_symbol_id`,
  `symbol_id_from_symbol`, `file_from_symbol_id`.

### 1.4 Remove the never-raised native `AnalysisError`
`check()`/`check_file()` return diagnostics; they never raise an analysis error,
so the native `AnalysisError` exception never fires.

- `rust/src/lib.rs`: delete the `create_exception!(... AnalysisError ...)` line
  (~line 11) and its `m.add("AnalysisError", ...)` registration (~line 31).
- `src/tyo3/rust_project.py`: delete the `_NativeAnalysisError` import block and
  the dummy-class fallback for it, and remove every
  `except _NativeAnalysisError as e: raise AnalysisError(...)` clause (in
  `check` and `check_file`). Leave the generic `except Exception → InternalTyError`.
- **Keep** the Python `AnalysisError` class in `src/tyo3/exceptions.py` — it is
  documented public API and may be raised by future code.

### 1.5 Verify & commit
```bash
devenv shell -- build
devenv shell -- tests
devenv shell -- ruff check src
```
> If any test imported a now-deleted symbol, update that test to not import it
> (there should be none for 1.1–1.4; `test_native_bridge.py` was already removed
> in `fcec2e4`).

```
git add -A && git commit -m "chore(phase1): delete dead code (tyo3-derive crate, unused fns, dead native exception)"
```

---

## Phase 2 — Remove all `unsafe` from the PyO3 layer (safety)

**Problem:** every method in `rust/src/project.rs` ends with
`let py = unsafe { Python::assume_attached() };`. This hand-asserts an invariant
PyO3 already guarantees, and it forecloses GIL release (Phase 3). Both prior
reviews (`02-tyo3-backend-wiring` §M-03, `10-graph-review-2` §11.2) specified the
`py: Python<'_>` parameter instead; the `assume_attached` form was an undocumented
deviation. We are reverting to the reviewed design.

### 2.1 The transformation (apply to every `#[pymethods]` fn that returns a Py object)
For each of: `check`, `check_file`, `document_symbols`, `workspace_symbols`,
`goto_definition`, `goto_declaration`, `goto_type_definition`, `find_references`,
`semantic_tokens`, `file_occurrences`, `type_hierarchy`, `hover`:

1. Add `py: Python<'py>` as the **first parameter after `&self`** and add the
   `<'py>` lifetime to the method.
2. Change the return type from `PyResult<Py<PyAny>>` to `PyResult<Bound<'py, PyAny>>`.
3. Delete the `let py = unsafe { Python::assume_attached() };` line.
4. Drop the trailing `.map(|bound| bound.unbind())` — return the `Bound` directly.

**Example — `check` before:**
```rust
fn check(&self) -> PyResult<Py<PyAny>> {
    let guard = lock_state(&self.inner, "check")?;
    let state = guard.as_ref().unwrap();
    let result = state.db.check();
    let diagnostics = convert::diagnostics::convert_diagnostics(&state.db, &result);
    let check_result = dto::CheckResultDto { diagnostics, files_checked: None, elapsed_ms: None };
    let py = unsafe { Python::assume_attached() };
    pythonize(py, &check_result)
        .map_err(|e| PyRuntimeError::new_err(e.to_string()))
        .map(|bound| bound.unbind())
}
```
**After:**
```rust
fn check<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
    let guard = lock_state(&self.inner, "check")?;
    let state = guard.as_ref().unwrap();
    let result = state.db.check();
    let diagnostics = convert::diagnostics::convert_diagnostics(&state.db, &result);
    let check_result = dto::CheckResultDto { diagnostics, files_checked: None, elapsed_ms: None };
    pythonize(py, &check_result).map_err(|e| PyRuntimeError::new_err(e.to_string()))
}
```

### 2.2 Handle the two methods that return `py.None()`
`type_hierarchy` and `hover` return `py.None().into_any()` on the empty branch.
With a `Bound` return type, return `Ok(py.None().bind(py).clone())` — or simpler,
restructure to return `Ok(py.None().into_bound(py))`. Verify the exact API by
compiling; the goal is a `Bound<'py, PyAny>` representing `None`.

### 2.3 Thread `py` through the shared helper
`navigate_to_targets` (used by the three `goto_*` methods) also calls
`assume_attached`. Add `py: Python<'py>` to its signature, return
`PyResult<Bound<'py, PyAny>>`, delete the `unsafe` line, and pass `py` from each
`goto_*` caller.

### 2.4 Confirm zero `unsafe` remain
```bash
grep -rn "unsafe\|assume_attached" rust/src    # expect: no matches
devenv shell -- build
devenv shell -- tests
```
```
git add -A && git commit -m "refactor(phase2): take py: Python<'_> param, remove all unsafe from PyO3 layer"
```

---

## Phase 3 — Release the GIL during heavy work (concurrency)

**Problem:** today the GIL is held for the entire duration of `check()` (which
scans the whole project) and every other analysis call. For a library pitched at
async/LSP/embedded use, this freezes all other Python threads. This is exactly
the `02-...§M-03` finding; Phase 2 unblocked the fix.

### 3.1 Pattern
Wrap **only the ty/Salsa computation** (no Python objects) in
`py.allow_threads(|| ...)`. Build DTOs and call `pythonize` *after* the closure
returns (those need the GIL / are cheap).

```rust
fn check<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
    let guard = lock_state(&self.inner, "check")?;
    let state = guard.as_ref().unwrap();

    // Heavy, pure-Rust work runs with the GIL released.
    let check_result = py.allow_threads(|| {
        let result = state.db.check();
        let diagnostics = convert::diagnostics::convert_diagnostics(&state.db, &result);
        dto::CheckResultDto { diagnostics, files_checked: None, elapsed_ms: None }
    });

    pythonize(py, &check_result).map_err(|e| PyRuntimeError::new_err(e.to_string()))
}
```

Apply the same shape to the genuinely heavy methods: `check`, `check_file`,
`document_symbols`, `workspace_symbols`, the `goto_*`/`find_references` family
(inside `navigate_to_targets`), `semantic_tokens`, `file_occurrences`,
`type_hierarchy`, `hover`. The `source_text` / `LineIndex` / DTO-conversion work
can all go inside the closure — none of it touches Python.

### 3.2 ⚠️ Compile-gated — this is a *verify*, not an assumption
`allow_threads` requires the closure and its captures to satisfy PyO3's `Ungil`
bound (effectively `Send`/`Sync` for what crosses the boundary). `ProjectDatabase`
(Salsa) is expected to be `Send + Sync`, but **prove it by compiling**:

```bash
devenv shell -- build
```
- If it compiles: great, run `devenv shell -- tests` and commit.
- If a method fails the `Ungil`/`Send` bound: **do not force it.** Revert
  `allow_threads` for *that method only* (leave it as the Phase-2 safe version)
  and add a `// NOTE: <type> is not Ungil; GIL held here.` comment. The safety win
  from Phase 2 stands regardless.

### 3.3 Commit
```
git add -A && git commit -m "perf(phase3): release GIL via py.allow_threads during ty analysis"
```

---

## Phase 4 — Rust layering & honesty (small, behavior-preserving)

### 4.1 Move symbol recursion into the convert layer
`collect_symbols_recursive` currently lives in `project.rs` but is pure
DTO-construction logic — every other converter lives in `convert/`. Move the
function into `rust/src/convert/symbols.rs` (make it `pub`), update
`document_symbols` in `project.rs` to call `convert::symbols::collect_symbols_recursive(...)`,
and adjust imports. No logic change.

### 4.2 `Diagnostic.details` — wire it or drop it (decide, don't leave it lying)
`convert/diagnostics.rs` hardcodes `details: vec![]` while the doc comment claims
"and sub-diagnostics". Pick one:

- **Preferred (wire it):** populate `details` from the diagnostic's
  sub-diagnostics. **Verify the ty API first** — inspect `ruff_db::diagnostic::Diagnostic`
  for a sub-diagnostic accessor (likely `sub_diagnostics()` yielding items with a
  `.message()`/concatenated message). If it exists:
  ```rust
  details: d.sub_diagnostics().iter().map(|s| s.concise_message().to_string()).collect(),
  ```
  (use the real method names you find). Apply in both `convert_diagnostics` and
  `convert_diagnostic_refs`.
- **Fallback (drop it - only if no clean API exists):** if no clean API exists, remove `details` from
  `DiagnosticDto` (`rust/src/dto/diagnostics.rs`) **and** from the Pydantic
  `Diagnostic` model (`src/tyo3/models/analysis.py`), and fix the doc comment.

> Whichever you choose, the rule is: no field that silently always-empty.

### 4.3 (Optional) Unify the two coordinate paths — *investigate only*
`position_to_offset_with_index` hand-counts codepoints while
`range_to_dto_with_index` uses ruff's `PositionEncoding::Utf32`. If
`ruff_source_file::LineIndex` exposes an offset-from-(line, column) API in this
pinned revision, replace `char_len_to_byte_offset` with it so both directions
share ruff's implementation. **If no clean inverse API exists, skip this** — do
not hand-roll a second mechanism. The existing tests in `coordinates.rs`
(`handles_unicode_codepoints`, `handles_crlf`, etc.) must still pass.

### 4.4 Verify & commit
```bash
devenv shell -- build && devenv shell -- test-rust && devenv shell -- tests
```
```
git add -A && git commit -m "refactor(phase4): move symbol recursion to convert/, resolve diagnostic.details, (opt) unify coordinates"
```

---

## Phase 5 — Python correctness, performance & honesty (low risk)

All in the Python layer. Each is small and independently verifiable.

### 5.1 `workspace_symbols` empty-query guard
`src/tyo3/session.py`: replace `if len(query) < 1:` with `if not query:`.

### 5.2 Remove stale doc references
- `src/tyo3/rust_project.py` module docstring: delete the line referencing
  `RUST_BACKEND_IMPLEMENTATION.md §6.1` (that file isn't in the repo).
- `src/tyo3/exceptions.py` module docstring: delete "Derived from
  RUST_BACKEND_IMPLEMENTATION.md §6.3".
- `src/tyo3/models/navigation.py`: in `HoverContentKind` docstring, delete
  "Maps to `tyo3.rust_backend.HoverContentKindDto`" (no such module).

### 5.3 Stop dropping exception detail in graph build
In `src/tyo3/graph/graph.py`, several `except Exception as e:` handlers log a
message that omits `e`. Add the error to the log (or `exc_info=True`) in:
`_collect_symbols_for_file`, `_collect_all_diagnostics`,
`_resolve_references_via_occurrences`, `_resolve_references_via_tokens`. Example:
```python
logger.warning("Failed to get symbols for %s, skipping: %s", file_str, e)
```

### 5.4 Hoist repeated `root.resolve()`
`_to_relative(root, path)` calls `root.resolve()` on every invocation (once per
path, per pass). In `CodeGraph.build`, compute `root_resolved = root.resolve()`
once and thread it in, or cache via a local. Keep the external-path `ValueError`
fallback behavior identical.

### 5.5 Cache the semantic subgraph for transitive queries
`transitive_dependencies` / `transitive_dependents` each call
`_semantic_subgraph(...)`, which does a full `self._graph.copy()` + edge filter
**per call**. Memoize per `kinds` set and invalidate on mutation:

- Add `self._semantic_subgraph_cache: dict[frozenset[EdgeKind], rx.PyDiGraph] = {}`
  in `__init__`.
- In `_semantic_subgraph`, return the cached copy if present, else build & store.
- Clear the cache (`self._semantic_subgraph_cache.clear()`) anywhere the graph
  mutates after build: `_add_edge`, `_add_node`, `_rebuild_indexes`, and
  `update_file`. (Simplest correct approach: clear in `_add_node`/`_add_edge`.)

> If you're unsure about invalidation correctness, **skip 5.5** rather than risk
> stale results — it's a perf nicety, not a correctness fix.

### 5.6 `update_file` name honesty
`update_file` does a full rebuild, not an incremental update. Rename it to
`rebuild` and update its docstring to state it rebuilds the whole graph. Grep for
callers (`grep -rn "update_file" src/`) and update them + any test. If you prefer
zero API churn, instead keep the name but rewrite the docstring to say plainly
"Currently performs a full rebuild." **Pick one; don't leave the mismatch.**

### 5.7 Verify & commit
```bash
devenv shell -- tests && devenv shell -- ruff check src && devenv shell -- ruff format src
```
```
git add -A && git commit -m "refactor(phase5): py correctness/perf/doc cleanups"
```

---

## Phase 6 — Collapse the `RustProject` / `TyO3Session` duplication  ⚠️ DECISION

**Problem:** `TyO3Session` is a near-1:1 delegating wrapper over `RustProject`.
Every public method is declared twice; `TyO3Session` adds essentially one line of
value (the empty-query guard). Two public classes with identical surfaces is a
standing maintenance tax.

**Constraint discovered during review:** `RustProject` is imported **directly by
~12 test files** (`test_rust_*`, `test_native_objects`, `test_coordinate_conversion`,
`test_semantic_tokens`, `test_type_hierarchy`, `test_file_occurrences`,
`test_check_file`, `test_exceptions`, `test_property_based`, `conftest.py`, …).
So this is mechanical but broad.

### Decision: choose ONE
> **Recommended — Option A (one public class).** Best end state. Fold `RustProject`
> into `TyO3Session`, delete `rust_project.py`, migrate tests. Mechanical, ~12 files.
>
> **Option B (formalize two layers).** Lower churn. Keep `RustProject` as the
> documented *internal native adapter* (exception mapping + validation) and
> `TyO3Session` as the public ergonomic layer; just document the split and stop
> there. Choose this only if test-migration time is unavailable.

If unsure, **ask the maintainer which option** before executing. The steps below
are for **Option A**.

### 6.A.1 Merge the implementation
1. Move the *body* of every `RustProject` method into the matching `TyO3Session`
   method (the validation + native call + exception mapping). `TyO3Session` keeps
   its public signatures (`str | StdPath` params). Effectively, `TyO3Session`
   gains: the `_native` import block, the `_NativeClosedError`/`_NativePathError`/
   `_NativePositionError` typed catches, `_check_open`, the `__del__`
   `ResourceWarning`, and `_closed`/`_inner`/`_root` state.
2. Preserve every behavior: idempotent `close()`, `reload()` guard,
   `find_references(..., include_declaration=True)` default, the
   `semantic_tokens` per-token `file` injection, `workspace_symbols` empty guard.
3. Delete `src/tyo3/rust_project.py`.
4. Remove `from tyo3.rust_project import RustProject` from `session.py`.

### 6.A.2 Migrate tests (mechanical)
For each test file using `RustProject`:
- Replace `from tyo3.rust_project import RustProject` → `from tyo3 import TyO3Session`.
- Replace `RustProject(` → `TyO3Session(`.
- Special cases:
  - `test_exceptions.py` constructs `object.__new__(RustProject)` and pokes
    `_closed`/`_inner`/`__del__` and asserts the `"RustProject was not closed"`
    warning text. Update to `TyO3Session` internals and the new warning text
    (change the warning string in the merged `__del__` to say `TyO3Session`).
  - `conftest.py` `shared_project` cache builds `RustProject(...)` — switch to
    `TyO3Session(...)`.
  - Any test asserting `RustProject`-specific repr/name.

### 6.A.3 Verify & commit
```bash
devenv shell -- build && devenv shell -- tests
grep -rn "RustProject\|rust_project" src/      # expect: no matches
```
```
git add -A && git commit -m "refactor(phase6): collapse RustProject into TyO3Session (single public class)"
```

---

## Phase 7 — Remove the spec-anticipation models  ⚠️ DECISION

**Problem:** `models/core.py` and `models/_spec.py` define `TyProject`,
`ProjectFile`, `TyProjectConfig`, `BackendInfo` ("no backend yet"). **Nothing in
the runtime uses them** — only tests do. Worse, the Pydantic `TyProject` collides
in name with the Rust `TyProject` pyclass and `PyTyProject`. They make the test
suite green while testing nothing the library does.

**Constraint:** the `conftest.py` fixtures `open_project`, `closed_project`,
`error_project`, `*_file`, and the `symbol`/`reference`/`definition_target`
fixtures are built from `TyProject`/`ProjectFile`, and are consumed by
`test_models_symbols.py`, `test_models_navigation.py`, `test_invariants.py`.

### Decision: choose ONE
> **Recommended — Option A (delete).** Removes the dead models, the name
> collision, and the tests that only exist to test them. Cleanest end state.
>
> **Option B (quarantine).** If you want to preserve the spec shapes, move
> `core.py` spec entities + `_spec.py` into a clearly-marked, **non-exported**
> `src/tyo3/_spec_models.py` and drop them from `tyo3.models.__all__` and from
> `models/__init__.py` re-exports. Keep the runtime enums that are actually used.

Steps below are for **Option A**.

### 7.A.1 Identify the genuinely-used enums first
`ProjectStatus`, `FileCategory`, `CoordinateMode` live in `core.py`. Confirm
they're used only by the spec models / tests:
```bash
grep -rn "ProjectStatus\|FileCategory\|CoordinateMode" src/ | grep -v "tests/\|core.py\|__init__.py"
```
- If **no runtime hits** (expected): they go away with `core.py`.
- If any runtime code uses one, **keep that enum** (move it to wherever it's used,
  or leave a minimal `core.py` with just the surviving enum).

### 7.A.2 Delete the models
- Delete `src/tyo3/models/_spec.py`.
- Delete `src/tyo3/models/core.py` (or trim to surviving enums per 7.A.1).
- `src/tyo3/models/__init__.py`: remove the `_spec` import line, the
  `from tyo3.models.core import (...)` block, and the corresponding `__all__`
  entries (`ProjectStatus`, `FileCategory`, `CoordinateMode` — unless one
  survived).
- `src/tyo3/models/advanced.py`: the docstring references `ProjectFile`; update it
  to stop referencing the deleted model.

### 7.A.3 Rebuild the conftest fixtures without spec models
In `conftest.py`, the `symbol`, `definition_target`, `reference` fixtures only
need real `Symbol` / `DefinitionTarget` / `Reference` instances — rebuild them
**without** a `TyProject`/`ProjectFile` argument (construct the models directly
from `FileRange`/`Range`). Delete `open_project`, `closed_project`,
`error_project`, `*_file`, and `project_config` fixtures and their imports.

### 7.A.4 Delete the tests that only test the deleted models
- Delete `src/tyo3/tests/test_models_core.py` (entirely about the spec models).
- `src/tyo3/tests/test_invariants.py`: this file asserts spec-model invariants
  (e.g. "every ProjectFile's project is open"). Those invariants describe deleted
  models — delete the file, or rewrite only invariants that apply to surviving
  models. Default: **delete it.**
- `test_native_objects.py` imported a spec model name — adjust its imports if so
  (it mainly tests the pythonize boundary; keep those tests, fix imports only).
- `test_models_symbols.py` / `test_models_navigation.py`: keep them; they should
  now use the rebuilt fixtures from 7.A.3.

### 7.A.5 Verify & commit
```bash
devenv shell -- tests
grep -rn "TyProjectConfig\|BackendInfo\|ProjectFile\|models.core" src/   # expect: none (or only surviving enum)
```
```
git add -A && git commit -m "refactor(phase7): remove unused spec-anticipation models and their tests"
```

---

## Phase 8 — Consolidate dependency declarations

`pyproject.toml` declares dev dependencies **twice with conflicting versions**:
`[project.optional-dependencies].dev` (`pytest>=7.0`, `pytest-cov>=4.1`) **and**
`[dependency-groups].dev` (`pytest>=9.0.3`, `pytest-cov>=7.1.0`).

1. Keep the PEP 735 `[dependency-groups].dev` as the single source of truth.
2. Move `ty>=0.0.40` and `ruff>=0.5.0` into `[dependency-groups].dev`.
3. Delete the `[project.optional-dependencies]` table entirely.
4. Reconcile versions (use the higher pins: `pytest>=9`, `pytest-cov>=7`,
   plus `hypothesis`).
5. Confirm `devenv.nix` / CI don't reference the `[project.optional-dependencies]`
   `dev` extra (`grep -rn "optional-dependencies\|\[dev\]\|extra" devenv.nix .github/`);
   update if they do.

```bash
devenv shell -- tests        # ensure the dev toolchain still resolves under devenv
```
```
git add -A && git commit -m "build(phase8): consolidate dev deps into [dependency-groups]"
```

---

## Phase 9 — Final verification & polish

### 9.1 Full gates
```bash
devenv shell -- clean
devenv shell -- build
devenv shell -- tests           # full suite + coverage, must be green
devenv shell -- test-rust       # rust integration + snapshots
devenv shell -- test-property   # hypothesis
devenv shell -- ruff check src
devenv shell -- ruff format --check src
```
If the project type-checks itself: `devenv shell -- ty check` (or the configured
script) should be clean too.

### 9.2 Final dead-reference sweep
```bash
grep -rn "assume_attached\|unsafe" rust/src
grep -rn "PyFields\|__fields__\|_to_python\|tyo3-derive" rust/ src/
grep -rn "RustProject\|rust_project" src/                 # none if Phase 6 = Option A
grep -rn "TyProjectConfig\|BackendInfo\|models.core" src/ # none if Phase 7 = Option A
grep -rn "RUST_BACKEND_IMPLEMENTATION\|rust_backend" src/
```
All should return nothing (modulo enums you intentionally kept).

### 9.3 README sync
Re-read `README.md` against the final API. In particular:
- If Phase 6 = Option A, the README's API section is already `TyO3Session`-centric
  (good) — just confirm no `RustProject` mention crept in.
- Confirm the exception table still matches `exceptions.py`.

### 9.4 Update project memory
Per the repo's memory conventions, note the final-state decisions (one public
class `TyO3Session`; no `tyo3-derive`; spec models removed; GIL released via
`allow_threads`) so future sessions don't re-discover them.

### 9.5 Open the PR
```
git push -u origin chore/final-refactor
```
PR body should summarize: net lines removed, `unsafe` count 0, one public class,
GIL released, dead crate gone.

---

## Phase 10 — Shipping & health (make it a real, installable, healthy package)

The previous phases make the library *clean*. This phase makes it *shippable and
maintainable*. These items are independent of Phases 1–9 and can be done in any
order, but several are quick high-value wins — do **10.1–10.5 first**.

### 10.1 Add the `py.typed` marker (⚠️ correctness bug — do this first)
TyO3 ships a fully-typed Pydantic API, but **without this file no downstream type
checker (including ty itself) sees any of it** — consumers get `Any`.

```bash
touch src/tyo3/py.typed
```
Ensure maturin includes package data. In `pyproject.toml` confirm the `tyo3`
package ships non-`.py` files (with `python-source = "src"` maturin includes
package files, but verify the built wheel contains `tyo3/py.typed`):
```bash
devenv shell -- build-wheel
python -m zipfile -l dist/*.whl | grep py.typed     # expect a hit
```
If it's missing from the wheel, add an explicit include under `[tool.maturin]`
(e.g. `include = ["src/tyo3/py.typed"]`) and rebuild.

### 10.2 Add a `LICENSE` file
`pyproject.toml` and `README.md` both declare MIT, but there is no `LICENSE`
file. Add the standard MIT text with the correct author and year:
```
LICENSE   (MIT, "Copyright (c) 2026 Bullish Design")
```
Confirm `pyproject.toml`'s `license = { text = "MIT" }` matches (or switch to
`license = { file = "LICENSE" }`).

### 10.3 Delete the dead `json-schema` Cargo feature
`rust/Cargo.toml` declares `[features] json-schema = []` but no code references
`#[cfg(feature = "json-schema")]`. Verify and remove:
```bash
grep -rn "json-schema\|json_schema" rust/    # if only the [features] line, delete it
```

### 10.4 Add a type stub for the native module
`tyo3._native_impl` resolves to `Any`. Add `src/tyo3/_native_impl.pyi` declaring
the `TyProject` class methods (signatures mirroring `rust/src/project.rs`) and the
exception classes (`ProjectClosedError`, `PathResolutionError`, `PositionError`).
Mark return types as `Any` (they're pythonize'd dicts validated downstream).
Ensure it ships in the wheel (same check as 10.1).

### 10.5 Harden CI (the suite currently never exercises the Rust side)
`.github/workflows/ci.yml` gates only on `ruff → build-release → tests`. That
means the Rust `#[cfg(test)]` tests (the unicode/CRLF coordinate logic) and lint
never run in CI. Add gates:

- **`cargo fmt --check`** and **`cargo clippy -- -D warnings`** (clippy would have
  caught the `unsafe`/`assume_attached` smell; keep it as a permanent guard).
- **`devenv shell -- test-rust`** so the Rust unit + snapshot tests run.

Add steps to the `test` job:
```yaml
      - name: Rust format
        run: devenv shell -- cargo fmt --manifest-path rust/Cargo.toml --check
      - name: Rust clippy
        run: devenv shell -- cargo clippy --manifest-path rust/Cargo.toml -- -D warnings
      - name: Rust tests
        run: devenv shell -- test-rust
```
> Verify the exact `cargo`/clippy invocation works inside the devenv shell first;
> if a `clippy` script exists in `devenv.nix`, prefer it. Fix any clippy findings
> the gate surfaces (there may be a few post-refactor) before turning on
> `-D warnings`.

### 10.6 Add a concurrency test (validates the Phase 3 GIL claim)
Phase 3 releases the GIL during analysis; prove the headline. Add
`src/tyo3/tests/test_concurrency.py`: open **separate** `TyO3Session` instances on
separate threads, run `check()`/`document_symbols()` concurrently, and assert all
return valid results with no deadlock/exception. (Per README, one session is not
shared across threads — the test uses one session per thread.)

### 10.7 Turn perf tests into a real guardrail
`test_rust_performance.py` only asserts `< 2s` on tiny fixtures (catches
catastrophes, misses drift). Add one benchmark that builds a `CodeGraph` over a
**larger** checkout (reuse the demo's clone path, or vendor a medium fixture) and
records timings. Keep it `@pytest.mark.benchmark`/opt-in so the default suite
stays fast, but make it runnable in a nightly CI job.

### 10.8 Build & publish wheels (the gap to `pip install tyo3`)
Today "install" means "build from source in devenv." To be installable:

1. **Enable abi3** so one wheel works across 3.13+: add `abi3-py313` to the PyO3
   features in `rust/Cargo.toml`
   (`pyo3 = { version = "0.28", features = ["extension-module", "abi3-py313"] }`)
   and verify the build still works.
2. **Add a `cibuildwheel` release workflow** (`.github/workflows/release.yml`)
   triggered on tag, building manylinux + macOS (arm64/x86_64) + Windows wheels
   and an sdist, then publishing to PyPI via trusted publishing (OIDC).
   `maturin-action` (`PyO3/maturin-action`) is the path of least resistance here.
3. Smoke-test: `pip install` the built wheel into a clean venv (no Rust toolchain)
   and run the README quick-start.

> This is the single biggest "make it a product" item and deserves its own PR.

### 10.9 Project meta files & dependency-pin policy
- Add **`CHANGELOG.md`** (Keep a Changelog format) and **`CONTRIBUTING.md`**
  (how to enter devenv, run gates, and — importantly — how to bump the pinned
  ty/Ruff git rev: it's a deliberate, tested operation, not a casual bump).
- Document the ty/Ruff pin strategy in `CONTRIBUTING.md`: the rev in
  `rust/Cargo.toml` is intentional; upgrading means bumping all eight ty/ruff
  deps to the same rev and re-running the full suite.

### 10.10 (Optional) Public-repo tidiness
`.scratch/projects/**` (internal review docs, including this guide) are currently
committed. For the public OSS face, consider adding `.scratch/` to `.gitignore`
so the repo shows the product, not the scaffolding. (CI already skips `.scratch`
for triggering.) Decide with the maintainer — keeping them is also a valid
"build in public" choice.

### 10.11 Verify & commit
```bash
devenv shell -- build && devenv shell -- tests && devenv shell -- test-rust
devenv shell -- ruff check src
```
Commit each sub-item logically, e.g.:
```
git commit -m "chore(phase10): add py.typed, LICENSE, drop dead cargo feature"
git commit -m "ci(phase10): gate on clippy, rustfmt, and rust tests"
git commit -m "build(phase10): abi3 wheels + cibuildwheel release workflow"
```

---

## Appendix A — Phase risk & ordering rationale

| Phase | Risk | Why here |
|------|------|----------|
| 1 Dead code | 🟢 none | Pure deletion; shrinks surface before anything else. |
| 2 Remove `unsafe` | 🟡 low | Mechanical, compiler-checked; unblocks Phase 3. |
| 3 `allow_threads` | 🟡 compile-gated | Needs Phase 2; may partially revert per-method. |
| 4 Rust layering | 🟢 low | Behavior-preserving moves + one field decision. |
| 5 Python cleanups | 🟢 low | Small, isolated. 5.5 is optional. |
| 6 Merge session | 🟠 medium | Broad (test migration). Decision-gated. |
| 7 Spec models | 🟠 medium | Touches conftest fixtures. Decision-gated. |
| 8 Deps | 🟢 low | Config only. |
| 9 Verify | — | Gates the whole thing. |
| 10 Shipping & health | 🟢→🟠 | Independent of 1–9. 10.1–10.5 quick wins; 10.8 (wheels) is its own PR. |

## Appendix B — "Definition of Done"

The refactor is complete when **all** are true:
- `grep -rn "unsafe" rust/src` → empty.
- `rust/tyo3-derive/` does not exist; no `PyFields`/`__fields__` references.
- Heavy ty calls run inside `py.allow_threads` (or carry a documented NOTE if a
  type wasn't `Ungil`).
- Exactly **one** public project class (`TyO3Session`) — if Option 6.A was taken.
- No "spec-anticipation" models in `tyo3.models` — if Option 7.A was taken.
- No field that is structurally always-empty (`Diagnostic.details` resolved).
- `pyproject.toml` declares dev deps once.
- Full suite green: `tests`, `test-rust`, `test-property`; `ruff` clean.
- README and project memory reflect the final state.
- `src/tyo3/py.typed` exists and ships in the wheel; `LICENSE` present.
- CI gates on `clippy`, `rustfmt`, and the Rust test suite.
- abi3 wheels build in a tagged release workflow and `pip install` works in a
  clean, Rust-free venv.

## Appendix C — If something goes wrong
- **Stale `.so`:** `devenv shell -- clean && devenv shell -- build`.
- **A phase won't go green:** revert that phase's commit (`git revert` / reset),
  leave the prior phases intact, and report the blocker. Phases are independent by
  design — a stuck Phase 6 or 7 does not block shipping Phases 1–5 & 8.
- **`allow_threads` bound errors (Phase 3):** revert `allow_threads` for that
  method only; keep the Phase-2 safe token. Do not add `unsafe` to work around it.
```

