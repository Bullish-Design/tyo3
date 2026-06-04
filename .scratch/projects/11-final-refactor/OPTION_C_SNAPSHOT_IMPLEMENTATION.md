# TyO3 — Option C: Concurrent Snapshot Implementation Guide

**Audience:** an engineer (intern) executing the concurrency refactor end-to-end.
**Goal:** make TyO3 async/LSP-friendly by (1) releasing the GIL during all ty
analysis and (2) adding an explicit, revision-pinned, thread-shareable read-only
`Snapshot` handle.
**Prerequisite reading:** `PYO3_CONCURRENCY_ANALYSIS.md` (same folder). It proves
`ProjectDatabase` is `Send + Clone + !Sync` and why the clone-into-GIL-release pattern
is necessary and sufficient. This guide assumes those facts.

> **This guide is backed by a working slice.** Before it was written, the `check`
> method was implemented end-to-end on both `TyProject` and `TySnapshot`, compiled,
> and validated (GIL release proven against a control). Every Rust code block below
> is either copied from that working slice or follows its exact shape. Where the slice
> exists in the tree, the guide points you at it as the canonical reference.

This guide is **prescriptive**. Follow the phases in order. Each phase is
independently committable and independently green.

> ## 🔴 GOLDEN RULE — run EVERYTHING through the devenv shell
> This project only works inside the Nix devenv. **Every** build, test, cargo,
> maturin, ruff, or python invocation must be prefixed with `devenv shell -- …`
> (or run from inside an interactive `devenv shell`). If you run `cargo`, `pytest`,
> `python`, `maturin`, or `ruff` directly, you will get wrong toolchains, missing
> deps, or a stale environment — and waste hours chasing phantom failures.
>
> - ✅ `devenv shell -- check-rust`, `devenv shell -- build`, `devenv shell -- tests`
> - ✅ `devenv shell -- pytest src/tyo3/tests/test_concurrency.py -v`
> - ❌ `cargo check`, `pytest …`, `python …`, `maturin develop` (bare — wrong env)
>
> The **only** commands safe to run outside devenv are read-only repo greps
> (`grep`, `git status`, `ls`) used by the verification steps — they don't touch the
> toolchain. Anything that *executes* code goes through devenv.
>
> Every command block in this guide already follows this rule. **Do not "simplify"
> by dropping the `devenv shell -- ` prefix.** See §3 for the full entrypoint list
> and the fast edit loop.

---

## 0. Decisions locked (do not re-litigate mid-implementation)

| # | Decision | Choice | Consequence |
|---|---|---|---|
| 1 | Do session read methods release the GIL? | **YES (1b)** | Session and snapshot read methods share one shape: clone-under-lock + GIL release. `session.check()` is non-blocking; an async facade becomes trivial later. |
| 2 | Allow `snapshot.snapshot()`? | **NO (terminal)** | `snapshot()` lives only on `TyO3Session`. Clean two-level model: Session (mutable root) → Snapshot (immutable leaf). Additive to relax later. |
| 3 | Naming | `Snapshot` (Python) / `TySnapshot` (native) | Mirrors `TyO3Session` / `TyProject`. |
| 4 | Snapshot read parity | **Full** | Snapshot exposes every read method the session does (incl. `files`, `workspace_symbols`). No `reload`. |

### The final model
- **`TyO3Session`** — mutable, **latest-revision**, **non-blocking** reads
  (clone + GIL release); owns `reload`/`close`/`snapshot`. The everyday object; safe to
  `await` via a thread pool.
- **`Snapshot`** — immutable, **pinned-revision**, non-blocking reads; `close` only.
  Use it when one logical operation issues many reads that must all see **one
  consistent revision** and stay **isolated from `reload`** (build a `CodeGraph`,
  service a multi-part LSP request).

### The load-bearing invariant
> **All mutation swaps the canonical database; nothing mutates it in place.**
> `reload` already does this (`project.rs`, `ProjectDatabase::use_defaults`). This is
> what keeps every outstanding clone — session-read clones *and* snapshots — isolated
> and free of `salsa::Cancelled`. If you ever add an incremental-edit API, it must build
> a new db and swap, **not** call salsa setters on the live db.

---

## 1. PyO3 0.28 API reference — READ THIS FIRST

The pinned version is **`pyo3 0.28.3`** (`rust/Cargo.lock`). The GIL APIs were renamed
in recent releases; older blog posts and Stack Overflow answers use the *old* names.
**Use the names in this table — the old ones do not exist in 0.28.**

| Concept | ✅ 0.28 name | ❌ Old name (does NOT compile) | Where |
|---|---|---|---|
| Release the GIL around Rust work | `py.detach(\|\| { … })` | `py.allow_threads(…)` | `pyo3/src/marker.rs:558` |
| Acquire the GIL | `Python::attach(\|py\| { … })` | `Python::with_gil(…)` | `pyo3/src/marker.rs:410` |
| Fallible acquire | `Python::try_attach(…)` | — | `marker.rs` |

### 1.1 `py.detach` — the exact signature and bound
```rust
pub fn detach<T, F>(self, f: F) -> T
where
    F: Ungil + FnOnce() -> T,
    T: Ungil,                       // ← the RETURN value must also be Ungil
{ … }
```
Both the **closure** and its **return type** must be `Ungil`. The closure runs with the
GIL released; PyO3 re-acquires the GIL before `detach` returns (even if the closure
panics).

### 1.2 What `Ungil` actually means
On the stable toolchain PyO3 defines (`pyo3/src/marker.rs:188`):
```rust
unsafe impl<T: Send> Ungil for T {}
```
**So for our purposes `Ungil` ≡ `Send`.** A value can cross the `detach` boundary iff it
is `Send`. Concretely:

- ✅ **`Ungil` (Send):** `ProjectDatabase`, `TyProjectState`, all our `*Dto` structs,
  `String`, `u32`, `File` (salsa id), `PyErr` (`err/mod.rs:48`), `Py<T>`
  (`instance.rs:1453`).
- ❌ **NOT `Ungil`:** `Python<'_>`, `Bound<'_, T>` / `&PyAny`, `PyRef`/`PyRefMut`, raw
  `ffi::*`. **You must never capture any of these in the `detach` closure.**

Practical rule for this refactor: **build all Python objects (`pythonize`, `py.None()`,
`Bound`) _outside_ `detach`. Inside `detach`, touch only Rust data and return a `Dto`
(or `Result<Dto, AnalysisError>`).**

### 1.3 `#[pyclass]` requires `Send`, NOT `Sync` — and why we still need a `Mutex`
This is the subtlest and most important point in the whole refactor. **PyO3 will happily
compile an unsound class. The compiler does not protect you here. You protect yourself
with the `Mutex`.**

PyO3's `#[pyclass]` only requires the inner type to be **`Send`** (it then uses a no-op
thread checker; `pyo3/src/impl_/pyclass.rs:200-206`). It does **not** require `Sync`.

Now consider what happens with GIL release on a *shared* object:

1. Thread A calls `snap.check()`. PyO3 hands it `&self`. Inside, `py.detach(…)` releases
   the GIL.
2. Thread B (holding the same `snap`) acquires the freed GIL and *also* calls
   `snap.check()`. PyO3 hands it `&self` to the **same object**.
3. Now two threads hold `&PySnapshot` simultaneously. Sharing `&T` across threads is
   sound only if `T: Sync`.

`ProjectDatabase` is **`!Sync`** (its `zalsa_local` uses `RefCell`/`UnsafeCell`). So a
pyclass that stored a bare `ProjectDatabase` would be **unsound** the instant two threads
share it across a GIL release — and PyO3 would have compiled it without complaint
(because it's `Send`).

**The fix — and it's mandatory:** store the db behind a `std::sync::Mutex`.
`Mutex<T>: Sync` whenever `T: Send`, so `Mutex<Option<TyProjectState>>` is `Sync` and the
class is sound. Each method locks, **clones** the db (cheap), drops the lock, then runs
the heavy work on the owned clone with the GIL released. The lock is held for microseconds
(the clone); the multi-second analysis runs lock-free *and* GIL-free, so two threads
sharing one snapshot serialize only on the clone, then run ty in parallel.

> The existing `PyTyProject` already uses `Mutex<Option<TyProjectState>>`
> (`project.rs:41-44`). **Mirror that pattern for `PySnapshot`.** The `Mutex` is not just
> for `close()`/`Option` — it is load-bearing for thread-safety once the GIL is released.

### 1.4 `#[pyclass(frozen)]` — recommended for the snapshot
A *frozen* pyclass (`#[pyclass(frozen)]`, `pyo3/src/pyclass.rs:28`) forbids `&mut self`
borrows; all access is `&self`. Our snapshot is exactly this (all reads are `&self`,
mutation is via the `Mutex`). Marking `PySnapshot` (and optionally `PyTyProject`) as
`frozen`:
- documents immutability at the type level,
- lets `Py::get()` access `&self` without a GIL-bound borrow check,
- is the PyO3-recommended shape for objects used without the GIL.

It does **not** remove the `Mutex` requirement (the db is still `!Sync`). Use both.

### 1.5 Returning `None` to Python (for `hover` / `type_hierarchy`)
With a `Bound` return type, the "no result" branch returns a bound `None`. The codebase
already does this (`project.rs`, hover/type_hierarchy):
```rust
return Ok(py.None().bind(py).clone());   // Bound<'py, PyAny> representing None
```
Keep that idiom for consistency. Do this **after** `detach` returns (it needs `py`).

---

## 2. The proven reference implementation (the `check` slice)

The `check` method is **already implemented** on both classes in the working tree. Study
it before doing anything else — the other eleven methods are this exact shape with a
different `compute_*` body. Reference locations:

- `rust/src/project.rs`: `clone_locked_state`, `compute_check`, `PyTyProject::check`,
  `PyTyProject::snapshot`, the `PySnapshot` class.
- `rust/src/lib.rs`: `m.add_class::<project::PySnapshot>()?;`
- `src/tyo3/session.py`: `TyO3Session.snapshot`, the `Snapshot` class.
- `src/tyo3/_native_impl.pyi`: `TyProject.snapshot`, `class TySnapshot`.

### 2.1 The shared helper (already in the tree)
```rust
/// Lock, clone the frozen project state (a cheap salsa snapshot), then drop the
/// lock. The returned owned `TyProjectState` is `Send`/`Ungil`, so it can drive
/// GIL-released analysis inside `py.detach(...)`.
fn clone_locked_state(
    inner: &Mutex<Option<TyProjectState>>,
    op_name: &str,
) -> PyResult<TyProjectState> {
    let guard = lock_state(inner, op_name)?;
    let s = guard.as_ref().unwrap();
    Ok(TyProjectState {
        db: s.db.clone(),       // ProjectDatabase: Clone — Arc bump + fresh zalsa_local
        root: s.root.clone(),
    })
}                                // ← lock released here, BEFORE the heavy work
```

### 2.2 The GIL-free core + the method wrapper (already in the tree)
```rust
/// GIL-free core of `check`: runs the project type-check and builds the DTO.
/// Touches no Python state, so it is safe to call inside `py.detach(...)`.
fn compute_check(state: &TyProjectState) -> dto::CheckResultDto {
    let result = state.db.check();
    let diagnostics = convert::diagnostics::convert_diagnostics(&state.db, &result);
    dto::CheckResultDto { diagnostics, files_checked: None, elapsed_ms: None }
}

// On PyTyProject:
fn check<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
    let state = clone_locked_state(&self.inner, "check")?;       // brief lock
    let check_result = py.detach(move || compute_check(&state)); // GIL + lock released
    pythonize(py, &check_result)                                 // GIL back: build PyObject
        .map_err(|e| PyRuntimeError::new_err(e.to_string()))
}
```
`compute_check` is infallible (a project check never errors), so it returns the DTO
directly. Methods that resolve a path/position return `Result<Dto, AnalysisError>` (next
section).

### 2.3 The snapshot class + `snapshot()` (already in the tree)
```rust
// On PyTyProject:
fn snapshot(&self) -> PyResult<PySnapshot> {
    let state = clone_locked_state(&self.inner, "snapshot")?;
    Ok(PySnapshot { inner: Mutex::new(Some(state)) })
}

#[pyclass(name = "TySnapshot", module = "tyo3._native_impl")]
pub struct PySnapshot {
    inner: Mutex<Option<TyProjectState>>,
}

#[pymethods]
impl PySnapshot {
    fn check<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "check")?;
        let check_result = py.detach(move || compute_check(&state));
        pythonize(py, &check_result).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    fn close(&self) -> PyResult<()> {
        let mut guard = self.inner.lock()
            .map_err(|e| PyRuntimeError::new_err(format!("Lock poisoned: {}", e)))?;
        *guard = None;
        Ok(())
    }
}
```
> **Apply §1.4 while you're here:** add `frozen` to both pyclass attributes, i.e.
> `#[pyclass(name = "TySnapshot", module = "tyo3._native_impl", frozen)]` and likewise on
> `PyTyProject`. (The slice currently omits `frozen`; add it as part of Phase 3 and
> confirm the build stays green — all methods are already `&self`.)

### 2.4 Measured result (so you know what "working" looks like)
On `fixtures/demo_repos` (37 files), N=4, validated via a throwaway script run through the
devenv shell:

```
control: pure-Python CPU (GIL held)   serial 2118 ms  parallel 2027 ms   speedup x1.05
treatment: Rust check() (GIL freed)   serial 37582 ms parallel 30840 ms  speedup x1.22
```

**Interpretation (important — set your expectations correctly):**
- The **control** (pure-Python CPU work) shows **no** speedup (x1.05) — the GIL serializes
  it. This proves the measurement harness is honest.
- The **treatment** shows a **real** speedup (x1.22 ≫ control) — the GIL is genuinely
  released; threads overlap.
- The speedup is **modest, and that's expected**, for two reasons: (a) ty's `check()`
  **already parallelizes internally with rayon**, so four concurrent checks compete for the
  same cores each check is already using; and (b) the Python-side `pythonize` +
  Pydantic `model_validate` of hundreds of diagnostics is GIL-bound. **A low absolute
  speedup is not a bug.** The headline win is that a `check()` on one thread no longer
  *freezes* other threads / the event loop — not that N checks get N× faster. Lighter,
  non-rayon cursor calls (`hover`, `goto_*`) returning small payloads will show larger
  cross-thread speedups.

---

## 3. Environment, entrypoints, and the FAST edit loop

**Use the `devenv.nix` script entrypoints for everything (the Golden Rule). Never call
`maturin`, `cargo`, `pytest`, `python`, `ruff`, or `rm` on build artifacts directly — go
through a script or `devenv shell -- …`.**

### 3.1 Entrypoint cheat-sheet
Existing scripts plus the **fast-loop scripts added for this refactor** (defined in
`devenv.nix`). Measured timings are from this machine.

| Task | Command | Cost | When |
|---|---|---|---|
| **Type-check Rust only** (no build/link) | `devenv shell -- check-rust` | **~1 s** incr. | **The edit loop.** Catches every `Ungil`/`Send`/borrow error. |
| Rust lint (CI gate) | `devenv shell -- clippy` | ~secs | before each commit |
| Build native ext (debug) | `devenv shell -- build` | ~17 s incr. | when you need to *run* Python |
| Rebuild, removing stale `.so` first | `devenv shell -- rebuild` | ~17 s | if a new method "isn't there" (§3.3) |
| Remove all artifacts (`cargo clean` + `.so`) | `devenv shell -- clean` | slow | only when truly stuck |
| Run ONE test file/expr | `devenv shell -- pytest <path> -v` | secs–min | per-feature checks |
| Run an ad-hoc script | `devenv shell -- pyrun <file.py>` | — | validation scripts |
| Full suite (+coverage) | `devenv shell -- tests` | ~3.5 min | end of a phase |
| Rust integration + snapshots | `devenv shell -- test-rust` | ~min | after Rust changes |
| Property tests | `devenv shell -- test-property` | ~min | final gate |
| Lint / format | `devenv shell -- ruff check src` / `devenv shell -- ruff format src` | secs | before commit |

> The `pytest` and `pyrun` scripts forward arguments, e.g.
> `devenv shell -- pytest src/tyo3/tests/test_concurrency.py::test_parallel -v`.
> If you add more focused scripts (e.g. a `test-snapshot` that runs only the snapshot
> tests), put them in `devenv.nix` next to the others — that's encouraged, see §3.2.

### 3.2 The recommended inner loop (do NOT full-build on every edit)
A full `build` is ~17× slower than `check-rust` and writes a 284 MB `.so`. Use this loop:

1. Edit Rust.
2. `devenv shell -- check-rust` — ~1 s. Repeat 1–2 until it compiles cleanly.
3. Only when you want to actually *run* the code: `devenv shell -- build` (or `rebuild`).
4. `devenv shell -- pytest <the one relevant test> -v`.
5. End of phase: `devenv shell -- tests && devenv shell -- test-rust`.

**Add your own scripts when a check is slow or repetitive.** You have permission to extend
`devenv.nix`. For example, a snapshot-only test script:
```nix
  scripts.test-snapshot.exec = ''
    cd "$DEVENV_ROOT"
    PYTHONPATH=src python -m pytest src/tyo3/tests/test_concurrency.py -v --no-cov 2>&1
  '';
```
Anything you'd otherwise type more than twice belongs in a script. New scripts are picked up
the next time you run `devenv shell -- <name>`.

Do **not** use short command timeouts — `build` is ~17 s incremental (minutes cold) and the
full suite is ~3.5 min. Allow 5 min (10 min for `tests`).

### 3.3 ⚠️ GOTCHA: stale `.so` shadowing (you WILL hit this)
The wheel builds as **abi3** → maturin writes `src/tyo3/_native_impl.abi3.so`. If an older
build left `src/tyo3/_native_impl.cpython-313-x86_64-linux-gnu.so` next to it, **CPython
imports the more-specific `cpython-313` file first**, shadowing your fresh `abi3.so`. The
symptom is maddening: a **green build**, but at runtime

```
AttributeError: 'tyo3._native_impl.TyProject' object has no attribute 'snapshot'
```

— you're running stale code. **Whenever you add or rename a native method/class and it
"isn't there" at runtime, run `devenv shell -- clean && devenv shell -- build`.** When in
doubt, clean. To diagnose, check which file actually loaded:
```bash
devenv shell -- bash -c 'PYTHONPATH=src python -c "import tyo3._native_impl as n; print(n.__file__); print(hasattr(n, \"TySnapshot\"))"'
```
There should be exactly one `_native_impl*.so` in `src/tyo3/`.

### 3.4 Running ad-hoc validation
Use the `pyrun` entrypoint (added for this refactor) — it sets `PYTHONPATH=src` and runs
inside devenv:
```bash
devenv shell -- pyrun .scratch/my_validation.py
```

---

## 4. Phase 1 — Extract a GIL-free analysis core (pure refactor, behavior-preserving)

**Why first:** every read method today interleaves *locking*, *analysis*, and
*`pythonize`*. To release the GIL we need the analysis middle as functions that take
`&TyProjectState`, touch **no Python**, and return serializable DTOs. This phase
introduces them and rewires the existing methods to call them — **still GIL-held, no
behavior change.** It's the big mechanical diff; isolating it keeps the GIL-release phase
tiny. (`check` is already done as the slice; this phase does the other ten + `files`.)

### 4.1 Add a GIL-free error type
Analysis code must not construct/raise a `PyErr` against a custom exception type while the
GIL is released. (`PyErr` is technically `Ungil`, but constructing one for our custom
exceptions reads Python type state — keep it out of the closure.) Use a plain Rust error,
converted to `PyErr` only after the GIL is back.

**Put it directly in `rust/src/project.rs`** (top, after the imports). Don't create a new
module — it would need wiring into `lib.rs` and extra `use` lines for no benefit. The
exception types `PathResolutionError`/`PositionError` are already in scope there (imported
at `project.rs:9`), so `into_pyerr` compiles as-is.
```rust
/// Analysis-layer error, free of any Python state so it can be produced inside
/// `py.detach(...)`. Converted to a concrete PyErr by the method wrapper.
/// Both variants wrap a `String` so they can be used directly as `map_err` fns,
/// e.g. `resolve_file(...).map_err(AnalysisError::Path)?`.
enum AnalysisError {
    Path(String),
    Position(String),
}

impl AnalysisError {
    fn into_pyerr(self) -> PyErr {
        match self {
            AnalysisError::Path(s) => PathResolutionError::new_err(s),
            AnalysisError::Position(s) => PositionError::new_err(s),
        }
    }
}
```
(No `pub` needed — everything that uses it lives in `project.rs`.)

### 4.2 Convert the shared helpers
- `resolve_file_and_source` (`project.rs:67`): return
  `Result<(File, String), AnalysisError>`; map the resolve error to `AnalysisError::Path`.
- `navigate_to_targets` (`project.rs:88`): becomes a pure core function
  `compute_navigate(state, path, line, column, navigate_fn) -> Result<Vec<dto::NavigationTargetDto>, AnalysisError>`.
  Drop its `py`, `lock_state`, and `pythonize`; map the position error to
  `AnalysisError::Position`. **Note the `navigate_fn: fn(&dyn Db, File, TextSize) -> …`
  parameter is a plain function pointer — function pointers are `Send`/`Ungil`, so they
  cross the `detach` boundary fine.**

### 4.3 Extract one `compute_*` per read method
Each is `fn(state: &TyProjectState, ...args) -> Result<Dto, AnalysisError>` (or a bare
`Dto` when the method can't fail), lifted **verbatim** from the current method body
(everything between `lock_state` and `pythonize`). DTOs (`rust/src/dto/`) and converters
(`rust/src/convert/`) are unchanged.

**Exact return types** (verified against `rust/src/convert/*` and `rust/src/dto/*` — use
these precisely; a wrong DTO name won't compile):

| New core fn | Lifted from (project.rs) | Returns |
|---|---|---|
| `compute_files` | `files` (201) | `Vec<String>` |
| `compute_check` ✅ done | `check` (218) | `dto::CheckResultDto` |
| `compute_check_file` | `check_file` (240) | `Result<dto::CheckResultDto, AnalysisError>` |
| `compute_document_symbols` | `document_symbols` (277) | `Result<Vec<dto::SymbolDto>, AnalysisError>` |
| `compute_workspace_symbols` | `workspace_symbols` (310) | `Vec<dto::SymbolDto>` |
| `compute_navigate` (goto ×3) | `navigate_to_targets` (88) | `Result<Vec<dto::DefinitionTargetDto>, AnalysisError>` |
| `compute_find_references` | `find_references` (420) | `Result<Vec<dto::ReferenceDto>, AnalysisError>` |
| `compute_semantic_tokens` | `semantic_tokens` (458) | `Result<Vec<dto::SemanticTokenDto>, AnalysisError>` |
| `compute_file_occurrences` | `file_occurrences` (487) | `Result<Vec<dto::NameOccurrenceDto>, AnalysisError>` |
| `compute_type_hierarchy` | `type_hierarchy` (508) | `Result<Option<dto::TypeHierarchyDto>, AnalysisError>` |
| `compute_hover` | `hover` (565) | `Result<Option<dto::HoverDto>, AnalysisError>` |

Notes:
- `compute_files` and `compute_workspace_symbols` are **infallible** (no path/position
  resolution) — return the bare value, not a `Result`.
- `compute_type_hierarchy` / `compute_hover` return `Ok(None)` where the methods currently
  return `py.None()`. The `None → py.None()` mapping moves into the wrapper (§4.4).
- `File` (a salsa interned id) stays inside the core; it never crosses the FFI boundary.

### 4.3.1 The mechanical transformation (the ONLY two error edits you make)
Lifting a body is verbatim **except** for the inline `PyErr` construction, which becomes an
`AnalysisError`. There are exactly two such sites and two replacements:

| Old (in the current method body) | New (in `compute_*`) |
|---|---|
| `.map_err(\|e\| PathResolutionError::new_err(e))?` | `.map_err(AnalysisError::Path)?` |
| `.map_err(\|e\| PositionError::new_err(e))?` | `.map_err(AnalysisError::Position)?` |

This works because `file_resolver::resolve_file` returns `Result<File, String>` and
`position_to_offset_with_index` returns `Result<_, String>` — and our `AnalysisError`
variants are tuple-structs over `String`, so `AnalysisError::Path` / `::Position` are
usable directly as the `map_err` function. Everything else in the body is copied unchanged.

### 4.3.2 Reference implementations (copy these shapes exactly)
The shared helper first — note the two error-edit sites:
```rust
fn resolve_file_and_source(
    state: &TyProjectState,
    path: &str,
) -> Result<(File, String), AnalysisError> {
    let file = file_resolver::resolve_file(&state.db, state.root.as_std_path(), path)
        .map_err(AnalysisError::Path)?;                 // was: PathResolutionError::new_err(e)
    let src = source_text(&state.db, file);
    Ok((file, src.as_str().to_string()))
}
```
A **fallible, no-position** core (`document_symbols`):
```rust
fn compute_document_symbols(
    state: &TyProjectState,
    path: &str,
) -> Result<Vec<dto::SymbolDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let flat_symbols = ty_ide::document_symbols(&state.db, file);
    let hierarchical = flat_symbols.to_hierarchical();
    let file_path = file.path(&state.db).as_str().to_string();
    let line_index = ruff_source_file::LineIndex::from_source_text(&source_str);

    let mut symbols: Vec<dto::SymbolDto> = Vec::new();
    for (id, info) in hierarchical.iter() {
        convert::symbols::collect_symbols_recursive(
            &hierarchical, id, &info, &source_str, &line_index, &file_path, None, &mut symbols,
        );
    }
    Ok(symbols)
}
```
A **fallible, position-resolving** core (`find_references`) — shows the `Position` edit:
```rust
fn compute_find_references(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
    include_declaration: bool,
) -> Result<Vec<dto::ReferenceDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(&source_str, &line_index, line, column)
        .map_err(AnalysisError::Position)?;             // was: PositionError::new_err(e)
    let refs = match ty_ide::find_references(&state.db, file, offset, include_declaration) {
        Some(refs) => convert::navigation::convert_references(&state.db, &refs),
        None => Vec::new(),
    };
    Ok(refs)
}
```
The **function-pointer** core (`compute_navigate`, shared by all three `goto_*`). This is
the trickiest one — note the exact `navigate_fn` type and that `&state.db` coerces to
`&dyn Db`:
```rust
fn compute_navigate(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
    navigate_fn: fn(
        &dyn Db,
        File,
        ruff_text_size::TextSize,
    ) -> Option<ty_ide::RangedValue<ty_ide::NavigationTargets>>,
) -> Result<Vec<dto::DefinitionTargetDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(&source_str, &line_index, line, column)
        .map_err(AnalysisError::Position)?;
    let targets = match navigate_fn(&state.db, file, offset) {
        Some(t) => convert::navigation::convert_navigation_targets(&state.db, &t),
        None => Vec::new(),
    };
    Ok(targets)
}
```
An **`Option`-returning** core (`hover`) — returns `Ok(None)`, never `py.None()`:
```rust
fn compute_hover(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
) -> Result<Option<dto::HoverDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(&source_str, &line_index, line, column)
        .map_err(AnalysisError::Position)?;
    match ty_ide::hover(&state.db, file, offset) {
        None => Ok(None),
        Some(hover_value) => {
            let file_range = hover_value.file_range();
            let file_path = file.path(&state.db).as_str().to_string();
            let rendered = hover_value
                .display(&state.db, ty_ide::MarkupKind::Markdown)
                .to_string();
            let dto = convert::hover::convert_hover_markdown_with_index(
                &source_str, &line_index, file_path, file_range, rendered,
            );
            Ok(Some(dto))
        }
    }
}
```
The remaining cores (`compute_check_file`, `compute_workspace_symbols`,
`compute_semantic_tokens`, `compute_file_occurrences`, `compute_type_hierarchy`,
`compute_files`) follow one of these five shapes exactly — copy the matching method body and
apply the §4.3.1 edits.
- All `source_text` / `LineIndex` / `ty_ide::*` / `convert::*` calls live in the core —
  none touch Python.

### 4.4 Rewire the existing `PyTyProject` methods (still GIL-held this phase)
Each becomes: lock → call core → map error → `pythonize`. Fallible example (`document_symbols`):
```rust
fn document_symbols<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
    let guard = lock_state(&self.inner, "document_symbols")?;
    let dtos = compute_document_symbols(guard.as_ref().unwrap(), path)
        .map_err(AnalysisError::into_pyerr)?;
    pythonize(py, &dtos).map_err(|e| PyRuntimeError::new_err(e.to_string()))
}
```
`Option` example (`hover`):
```rust
fn hover<'py>(&self, py: Python<'py>, path: &str, line: u32, column: u32)
    -> PyResult<Bound<'py, PyAny>>
{
    let guard = lock_state(&self.inner, "hover")?;
    match compute_hover(guard.as_ref().unwrap(), path, line, column)
        .map_err(AnalysisError::into_pyerr)?
    {
        None => Ok(py.None().bind(py).clone()),
        Some(dto) => pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string())),
    }
}
```
`reload` (166) and `close` (188) are unchanged — they mutate the canonical state under the
lock and stay GIL-held.

### 4.5 ✅ Verify nothing was missed (run these greps — all must pass)
Read-only greps; safe to run outside devenv. Each is a "did I convert everything?" gate.

```bash
# (a) Every compute_* core function exists (11 of them; goto_* share compute_navigate):
for f in files check check_file document_symbols workspace_symbols navigate \
         find_references semantic_tokens file_occurrences type_hierarchy hover; do
  grep -q "fn compute_$f\b" rust/src/project.rs && echo "ok  compute_$f" || echo "MISSING compute_$f"
done
# Expect: 11 'ok', zero 'MISSING'.

# (b) Inline PyErr construction was pulled OUT of method bodies into AnalysisError.
#     After extraction these should appear ONLY inside `into_pyerr` — one each:
grep -c "PathResolutionError::new_err" rust/src/project.rs   # expect: 1
grep -c "PositionError::new_err"       rust/src/project.rs   # expect: 1

# (c) No analysis API is still called directly inside a #[pymethods] body.
#     ty_ide:: / convert:: must live only in compute_*/helpers, never after a
#     `lock_state(&self.inner …)` in a method. Quick proxy — these must be ZERO:
grep -n "ty_ide::\|convert::" rust/src/project.rs | grep -v "fn compute_\|resolve_file_and_source" | sed -n '1,40p'
#     Eyeball: every hit should be inside a compute_* fn. (Phase 1 leaves methods thin.)

# (d) AnalysisError is wired:
grep -q "enum AnalysisError" rust/src/project.rs && grep -q "fn into_pyerr" rust/src/project.rs \
  && echo "ok AnalysisError" || echo "MISSING AnalysisError"
```

### 4.6 Build, test & commit
```bash
devenv shell -- check-rust          # ~1s — confirm it type-checks first
devenv shell -- build && devenv shell -- test-rust && devenv shell -- tests
devenv shell -- ruff check src
```
> Behavior is identical to baseline; `test-rust` (snapshot suite) guards against any
> conversion drift.

```
git commit -am "refactor(concurrency): extract GIL-free compute_* core + AnalysisError"
```

---

## 5. Phase 2 — Release the GIL on session reads (Decision 1b)

Flip every session read method to the **clone-under-lock + `detach`** shape (the slice's
`check` is the template). Because Phase 1 did the heavy lifting, this is small.

### 5.1 Shape (apply to all session read methods)
```rust
fn document_symbols<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
    let state = clone_locked_state(&self.inner, "document_symbols")?;   // brief lock
    let path = path.to_owned();                                         // own args into closure
    let dtos = py.detach(move || compute_document_symbols(&state, &path))  // GIL + lock released
        .map_err(AnalysisError::into_pyerr)?;
    pythonize(py, &dtos).map_err(|e| PyRuntimeError::new_err(e.to_string()))
}
```
- **Own every argument into the closure** (`path.to_owned()`, `query.to_owned()`); a
  borrow of a `&str` argument is fine to move *if* it outlives the closure, but owning is
  simpler and avoids lifetime puzzles. `line`/`column` are `Copy`.
- `Option` methods match on `Ok(None)/Ok(Some(dto))` after the closure as in §4.4.

The three **`goto_*`** wrappers are the only ones with a twist (they pass a function
pointer). Here is the exact `goto_definition` wrapper — `goto_declaration` and
`goto_type_definition` are identical but for the op-name string and the
`ty_ide::goto_*` function passed in:
```rust
fn goto_definition<'py>(&self, py: Python<'py>, path: &str, line: u32, column: u32)
    -> PyResult<Bound<'py, PyAny>>
{
    let state = clone_locked_state(&self.inner, "goto_definition")?;
    let path = path.to_owned();
    let dtos = py.detach(move || {
        compute_navigate(&state, &path, line, column, ty_ide::goto_definition)
    })
    .map_err(AnalysisError::into_pyerr)?;
    pythonize(py, &dtos).map_err(|e| PyRuntimeError::new_err(e.to_string()))
}
```
> The fn item `ty_ide::goto_definition` coerces to the `fn(&dyn Db, File, TextSize) -> …`
> pointer that `compute_navigate` expects; function pointers are `Copy` + `Send`, so moving
> one into the closure is fine.

Apply the §5.1 shape to: `files`, `check_file`, `document_symbols`, `workspace_symbols`,
the three `goto_*`, `find_references`, `semantic_tokens`, `file_occurrences`,
`type_hierarchy`, `hover`. (`check` is already done.)

### 5.2 ⚠️ Compile-gate the `Ungil` bound — already proven for `check`, re-confirm per method
`check` already compiles, so the pattern is sound. If a *new* method fails to build with a
trait-bound error mentioning `Ungil`/`Send`, you captured a non-`Send` value in the
closure — almost always a `Bound`/`&PyAny`/`Python` or a borrow of `&self`. Fix the
capture (own the data; move only Rust values). **Never reach for `unsafe`.** Type-check
after every couple of methods with the fast loop:
```bash
devenv shell -- check-rust      # ~1s, catches Ungil/Send errors without a full build
```

### 5.3 Correct the stale NOTE
Replace the misleading comment at `project.rs:26-33` (it claims `!Send + !Sync` and that
GIL release is impossible — both wrong) with:
```rust
/// CONCURRENCY: `ProjectDatabase` (salsa 0.26) is `Send + Clone` but `!Sync`
/// — its `salsa::Storage` holds a per-thread `ZalsaLocal` (`RefCell`/`UnsafeCell`).
/// You cannot share `&db` across threads, but you CAN move an owned clone, which is
/// how ty itself parallelizes (`ty_project::Project::check` clones the db per rayon
/// worker). Read methods take a `db.clone()` snapshot via `clone_locked_state` and
/// run inside `py.detach(move || …)` to release the GIL. Because `#[pyclass]` only
/// requires `Send` (not `Sync`), the `Mutex` below is what makes concurrent `&self`
/// access sound once the GIL is released. The canonical db is only ever swapped
/// (never mutated in place) — see `reload` — so outstanding clones stay isolated.
```

### 5.4 ✅ Verify every read method was converted (run these greps — all must pass)
```bash
# (a) Every session read method now releases the GIL. One detach CALL per read method.
#     Match `py.detach(move` so doc-comment mentions of `py.detach(...)` don't inflate it.
#     After Phase 2 there are 13 reads on PyTyProject (incl. `check`):
grep -c "py.detach(move" rust/src/project.rs   # expect: 13  (becomes 26 after Phase 3)

# (b) Every read method clones via the helper instead of holding the lock over analysis.
#     `lock_state(` should now appear ONLY in: its own definition, clone_locked_state,
#     reload, and close — NEVER inside a read method. List them and eyeball:
grep -n "lock_state(" rust/src/project.rs
#     Expect ~4 hits: fn lock_state, clone_locked_state, reload, close. If a read
#     method name shows up here, it was not converted.

# (c) Every read method (and snapshot()) takes a snapshot. clone_locked_state calls:
grep -c "clone_locked_state(" rust/src/project.rs   # expect: 14 (13 reads + snapshot)

# (d) The stale concurrency claims and the OLD PyO3 API are gone everywhere:
grep -rn "allow_threads\|assume_attached\|with_gil\|!Send + !Sync\|!Send" rust/src
#     Expect: NO output. (If the NOTE still says !Send, you skipped §5.3.)
```

### 5.5 Build, test & commit
```bash
devenv shell -- check-rust                        # ~1s gate
devenv shell -- clean && devenv shell -- build    # clean avoids stale-.so confusion
devenv shell -- test-rust && devenv shell -- tests
```
```
git commit -am "perf(concurrency): release GIL on session reads via clone + py.detach"
```

---

## 6. Phase 3 — Add the native `TySnapshot` pyclass (Rust)

`PySnapshot::check` + `close` + `PyTyProject::snapshot` already exist (the slice). This
phase fills in the **remaining read methods** on `PySnapshot` and adds `frozen`.

### 6.1 Fill in the read methods
For each read method, the `PySnapshot` body is **identical** to the matching session method
from Phase 2 — same `clone_locked_state` + `detach(compute_*)` + `pythonize`, just on
`&self.inner` of the snapshot. Example:
```rust
fn hover<'py>(&self, py: Python<'py>, path: &str, line: u32, column: u32)
    -> PyResult<Bound<'py, PyAny>>
{
    let state = clone_locked_state(&self.inner, "hover")?;
    let path = path.to_owned();
    match py.detach(move || compute_hover(&state, &path, line, column))
        .map_err(AnalysisError::into_pyerr)?
    {
        None => Ok(py.None().bind(py).clone()),
        Some(dto) => pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string())),
    }
}
```

> **Duplication note (and how to avoid it):** PyO3 `#[pymethods]` cannot be shared between
> two pyclasses via a trait, so the ~12 thin read wrappers are duplicated between
> `PyTyProject` and `PySnapshot`. Each body is ~4 lines delegating to the *same*
> `clone_locked_state` + `compute_*` + `pythonize`, so there is **no logic duplication** —
> only boilerplate. The Cargo feature `multiple-pymethods` is already enabled
> (`rust/Cargo.toml`), so if you prefer, a single `macro_rules!` can generate each pair of
> wrappers from `(method_name, compute_fn, args, returns_option)`. **Plain duplication is
> more greppable; a macro is less code. Pick one and be consistent. Do not duplicate any
> _analysis_ logic — that all lives in `compute_*`.**

### 6.2 Add `frozen` to both classes
```rust
#[pyclass(name = "TyProject",  module = "tyo3._native_impl", frozen)]
#[pyclass(name = "TySnapshot", module = "tyo3._native_impl", frozen)]
```
All methods on both classes are already `&self`; confirm the build stays green.

> **`frozen` does NOT conflict with `close()`/`reload()`.** `frozen` only forbids
> `&mut self` / `PyRefMut` borrows — i.e. Python can't get a mutable borrow of the object.
> It says nothing about *interior* mutability. Our mutation goes through
> `self.inner.lock()` (a `Mutex`, which is interior mutability over `&self`), so
> `*guard = None` in `close()` and the db-swap in `reload()` keep working unchanged. If you
> ever see `error: cannot borrow ... as mutable` after adding `frozen`, it means a method
> took `&mut self` — change it to `&self` + `Mutex`, don't remove `frozen`.

### 6.3 `snapshot()` is terminal
`PyTyProject::snapshot()` exists. Do **not** add `snapshot()` to `PySnapshot`
(Decision 2).

### 6.4 Registration is done
`m.add_class::<project::PySnapshot>()?;` is already in `rust/src/lib.rs`.

### 6.5 ✅ Verify the snapshot surface matches the session (run these greps)
```bash
# (a) Every read method is defined on BOTH classes — each name must appear exactly
#     TWICE in project.rs (once in PyTyProject, once in PySnapshot). `\b` stops
#     `check` from matching `check_file`.
for m in files check check_file document_symbols workspace_symbols \
         goto_definition goto_declaration goto_type_definition find_references \
         semantic_tokens file_occurrences type_hierarchy hover; do
  n=$(grep -c "fn $m\b" rust/src/project.rs); echo "$n  $m"; done
#     Expect: every count == 2. A '1' means you forgot that method on PySnapshot.

# (b) detach CALLS now appear once per read method on each class: 13 + 13 = 26.
grep -c "py.detach(move" rust/src/project.rs      # expect: 26

# (c) Both pyclasses are frozen:
grep -n "#\[pyclass" rust/src/project.rs          # expect: 2 lines, BOTH containing `frozen`
grep -c "frozen" rust/src/project.rs              # expect: >= 2

# (d) snapshot() is terminal — defined once (on PyTyProject), NOT on PySnapshot:
grep -c "fn snapshot\b" rust/src/project.rs       # expect: 1

# (e) Class registered exactly once:
grep -c "add_class::<project::PySnapshot>" rust/src/lib.rs   # expect: 1
```

### 6.6 Build, test & commit
```bash
devenv shell -- check-rust
devenv shell -- clean && devenv shell -- build
devenv shell -- test-rust && devenv shell -- tests
```
```
git commit -am "feat(concurrency): full TySnapshot read surface (frozen pyclass)"
```

---

## 7. Phase 4 — Python layer: `_ReadOps` base + full `Snapshot`

The slice's `Snapshot` only wires `check`. Now give it full parity by factoring the read
wrappers into a shared base.

### 7.1 Factor read methods into `_ReadOps`
The read wrappers in `session.py` are identical for both native classes (they only touch
`self._inner`, the typed-exception catches, and Pydantic validation). Move them onto a base
both classes inherit.

> **Add `from typing import Any` to `session.py`'s imports** — the base annotates
> `_inner: Any` and `session.py` does not currently import `Any`. Without it you'll get a
> `NameError` at import time. (The module already has `from __future__ import annotations`,
> so the annotation itself is lazy, but `_inner: Any` as a *class-body* annotation is still
> evaluated — keep the import.)

```python
# session.py  (keep all existing imports; add: from typing import Any)
class _ReadOps:
    """Read-only analysis methods shared by TyO3Session and Snapshot.

    Subclasses must provide `self._inner` (native handle) and `self._closed`.
    """
    _inner: Any
    _closed: bool

    def _check_open(self) -> None:
        if self._closed:
            raise ProjectClosedError("Project is closed")

    # ── verbatim from the current TyO3Session bodies (session.py:121-344) ──
    def files(self): ...
    def check(self): ...
    def check_file(self, path): ...
    def document_symbols(self, path): ...
    def workspace_symbols(self, query): ...
    def goto_definition(self, path, line, column): ...
    def goto_declaration(self, path, line, column): ...
    def goto_type_definition(self, path, line, column): ...
    def _goto(self, method, path, line, column): ...
    def find_references(self, path, line, column, include_declaration=True): ...
    def semantic_tokens(self, path): ...
    def file_occurrences(self, path): ...
    def hover(self, path, line, column): ...
    def type_hierarchy(self, path, line, column): ...
```
Cut/paste the method *bodies* unchanged; only the class they live on changes.

### 7.2 `TyO3Session(_ReadOps)`
Keeps `__init__`, `root`, `reload`, `close`, `__enter__/__exit__/__del__` (unchanged), and
the already-added `snapshot()` (which currently constructs the slice `Snapshot`; once
`Snapshot` is full-parity in §7.3, nothing else changes here).

### 7.3 `Snapshot(_ReadOps)` — replace the slice stub
Replace the slice's `check`-only `Snapshot` with the inheriting version:
```python
class Snapshot(_ReadOps):
    """Immutable, revision-pinned, thread-shareable read view of a project.

    Created via TyO3Session.snapshot(). Exposes every read method a session does,
    but no reload. All reads see the project exactly as it was when the snapshot was
    taken, regardless of later session reloads, and a single snapshot is safe to
    share across threads.
    """
    def __init__(self, native_snapshot: Any) -> None:
        self._inner = native_snapshot
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._inner.close()
        self._closed = True

    def __enter__(self) -> Snapshot:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __del__(self) -> None:
        if getattr(self, "_inner", None) is None:
            return
        if not getattr(self, "_closed", True):
            warnings.warn(
                "Snapshot was not closed explicitly. Use 'with session.snapshot()' "
                "or call snapshot.close().",
                ResourceWarning, stacklevel=2,
            )
            try:
                self.close()
            except Exception:
                pass
```

### 7.4 Exports & stub (exact edits)
`src/tyo3/__init__.py` — add the import next to the existing `TyO3Session` import and the
name to `__all__`:
```python
from tyo3.session import Snapshot, TyO3Session   # was: from tyo3.session import TyO3Session

__all__ = [
    "TyO3Session",
    "Snapshot",          # ← add
    # … existing entries …
]
```
`src/tyo3/_native_impl.pyi` — fill out `class TySnapshot` to full read parity (it currently
only has `check`/`close` from the slice). All return types are `Any` (pythonize'd dicts
validated downstream):
```python
class TySnapshot:
    """An immutable, revision-pinned read view of an open ty project database."""

    def files(self) -> list[str]: ...
    def check(self) -> Any: ...
    def check_file(self, path: str) -> Any: ...
    def document_symbols(self, path: str) -> Any: ...
    def workspace_symbols(self, query: str) -> Any: ...
    def goto_definition(self, path: str, line: int, column: int) -> Any: ...
    def goto_declaration(self, path: str, line: int, column: int) -> Any: ...
    def goto_type_definition(self, path: str, line: int, column: int) -> Any: ...
    def find_references(self, path: str, line: int, column: int, include_declaration: bool) -> Any: ...
    def semantic_tokens(self, path: str) -> Any: ...
    def file_occurrences(self, path: str) -> Any: ...
    def type_hierarchy(self, path: str, line: int, column: int) -> Any: ...
    def hover(self, path: str, line: int, column: int) -> Any: ...
    def close(self) -> None: ...
```

### 7.5 ✅ Verify the Python layer (run these greps — all must pass)
```bash
# (a) Read methods live on _ReadOps and are NOT duplicated in TyO3Session/Snapshot.
#     Each read method must be defined exactly ONCE in session.py:
for m in files check check_file document_symbols workspace_symbols \
         goto_definition goto_declaration goto_type_definition find_references \
         semantic_tokens file_occurrences type_hierarchy hover; do
  n=$(grep -c "    def $m\b" src/tyo3/session.py); echo "$n  $m"; done
#     Expect: every count == 1. A '2' means you left a copy behind on a subclass.

# (b) Both classes inherit the base; Snapshot has no reload:
grep -n "class _ReadOps\|class TyO3Session(_ReadOps)\|class Snapshot(_ReadOps)" src/tyo3/session.py
grep -c "def reload" src/tyo3/session.py          # expect: 1 (TyO3Session only)

# (c) Snapshot is exported:
grep -n "Snapshot" src/tyo3/__init__.py           # expect: an import line AND an __all__ entry

# (d) The native stub declares every method on TySnapshot (parity with TyProject):
for m in check check_file document_symbols workspace_symbols goto_definition \
         goto_declaration goto_type_definition find_references semantic_tokens \
         file_occurrences type_hierarchy hover files close; do
  grep -q "def $m" src/tyo3/_native_impl.pyi && : || echo "MISSING in .pyi: $m"; done
#     Expect: no 'MISSING' output.

# (e) No lingering reference to the old slice-stub Snapshot (the check-only one):
grep -n "Slice: only" src/tyo3/session.py         # expect: no output (you replaced the stub)
```

### 7.6 Build, test & commit
```bash
devenv shell -- tests && devenv shell -- ruff check src && devenv shell -- ruff format src
```
```
git commit -am "feat(concurrency): Snapshot full read parity via _ReadOps base + exports"
```

---

## 8. Phase 5 — Tests, docs, memory

### 8.1 ⚠️ FIRST: fix the export-inventory test (already failing on the slice)
Adding `TySnapshot` to the native module **breaks an existing over-specific test**:
`src/tyo3/tests/test_native_objects.py::TestPythonizeEdgeCases::test_native_module_exports_only_typroject_and_exceptions`
asserts the native module exports *only* `TyProject` + exceptions. Update its `expected`
set to include `"TySnapshot"`:
```python
expected = {
    "TyProject",
    "TySnapshot",        # ← add
    "ProjectClosedError",
    "PathResolutionError",
    "PositionError",
}
```
(Also update the docstring/name — it's no longer "only TyProject".) This is a correct test
change: the invariant it encoded ("one native class") is intentionally no longer true.

### 8.2 Concurrency / parallelism (the proof) — make it robust, not flaky
Strengthen `src/tyo3/tests/test_concurrency.py`. **Do not assert an absolute speedup
threshold** (it's machine- and load-dependent — see §2.4). Instead use the **control-vs-
treatment** method that the slice validated:
- Measure speedup of a **pure-Python CPU** function across N threads (GIL held → ≈1.0).
- Measure speedup of `check()` across N threads, **one cold session per thread**
  (separate sessions so salsa doesn't serve cached results).
- Assert the **Rust speedup meaningfully exceeds the Python-control speedup**, e.g.
  `rust_speedup > control_speedup * 1.15`, with a warm-up run and `N>=4`. This is stable
  because it's a *relative* comparison: if the GIL were still held, both would be ≈1.0.
- Add a second test sharing **one** snapshot across threads (validates the snapshot path
  and the `Mutex`-clone soundness) — assert all results are valid and equal, no deadlock.

> Why one session per thread for the timing test: a single shared session/snapshot returns
> salsa-memoized results after the first call, so subsequent calls have almost no work to
> parallelize and the timing signal vanishes. Cold separate sessions each do real work.

**Proven harness** (this is the exact control-vs-treatment logic validated on the slice;
adapt into a pytest test). It compares the Rust speedup against a Python-CPU control, so it
is stable across machines:
```python
import time
from concurrent.futures import ThreadPoolExecutor
from tyo3 import TyO3Session

FIXTURE = "fixtures/demo_repos"   # 37 files — enough work to time
N = 4

def _serial(make_call, n):
    calls = [make_call() for _ in range(n)]
    t0 = time.perf_counter()
    for c in calls:
        c()
    return time.perf_counter() - t0

def _parallel(make_call, n):
    calls = [make_call() for _ in range(n)]
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n) as ex:
        list(ex.map(lambda c: c(), calls))
    return time.perf_counter() - t0

def test_check_releases_the_gil():
    # Control: pure-Python CPU work — GIL held, so threads do NOT speed it up.
    def py_busy():
        return lambda: sum(i * i for i in range(3_000_000))
    py_busy()()  # warm
    control_speedup = _serial(py_busy, N) / _parallel(py_busy, N)

    # Treatment: each call is check() on its OWN cold session (real, uncached work).
    def make_check():
        s = TyO3Session(FIXTURE)
        return lambda: (s.check(), s.close())
    rust_speedup = _serial(make_check, N) / _parallel(make_check, N)

    # If the GIL were still held, rust_speedup would be ~control_speedup (~1.0).
    # Released, it is meaningfully higher. Relative comparison → stable on CI.
    assert rust_speedup > control_speedup * 1.15, (
        f"GIL not released? rust={rust_speedup:.2f} control={control_speedup:.2f}"
    )
```
> Measured on the slice: control ≈ x1.05, Rust ≈ x1.22 (§2.4). The `* 1.15` margin clears
> the control comfortably while tolerating jitter. If this ever flakes on a busy CI box,
> raise `N`, enlarge the fixture, or compare medians of 3 runs — **do not** switch to an
> absolute threshold.

### 8.3 Lifecycle
- `session.snapshot()` after `session.close()` raises `ProjectClosedError`.
- A read on a `Snapshot` after `snapshot.close()` raises `ProjectClosedError`.
- `Snapshot` works as a context manager; `__del__` emits `ResourceWarning` if not closed.
- `snapshot.snapshot` does **not** exist (Decision 2): `assert not hasattr(snap, "snapshot")`.

### 8.4 Isolation (the snapshot's reason to exist)
- Take a snapshot → modify a fixture file on disk → `session.reload()` → the **snapshot**
  still returns pre-edit results; a **new** snapshot reflects the edit; the **session**
  reflects the edit.

### 8.5 Equivalence
- Parametrized over fixtures: `snapshot.<read>(...)` equals `session.<read>(...)` at the
  same revision (no reload between). (The slice already showed `check` parity: 449 == 449.)

### 8.6 Rust unit test
In `project.rs` `#[cfg(test)]`: two threads, two clones of one db, concurrent `db.check()`
— no panic, identical diagnostics. (Confirms the salsa clone model directly.)

### 8.7 Docs
- `README.md`: add a "Concurrency" section — session reads are non-blocking; use
  `Snapshot` for revision-consistent multi-read operations and isolation from reload; a
  snapshot is safe to share across threads. Replace any "one session per thread" guidance.
  Add an async example: `await asyncio.to_thread(session.check)`.
- If `CONTRIBUTING.md` exists, document the swap-don't-mutate invariant (§0) and the
  stale-`.so` gotcha (§3.1).

### 8.8 Memory (repo memory conventions)
Record the final model so future sessions don't re-derive it:
- `ProjectDatabase` is `Send + Clone + !Sync`; GIL released via `clone + py.detach`
  (supersedes the old "Salsa is !Send" note).
- `#[pyclass]` requires `Send` not `Sync`; the `Mutex` is the soundness mechanism under
  GIL release.
- Two read surfaces: `TyO3Session` (latest, mutable) and `Snapshot` (pinned, immutable,
  terminal). Invariant: all mutation swaps the db.

### 8.9 Final gates
```bash
devenv shell -- clean && devenv shell -- build
devenv shell -- tests && devenv shell -- test-rust && devenv shell -- test-property
devenv shell -- ruff check src && devenv shell -- ruff format --check src
grep -rn "assume_attached\|allow_threads\|unsafe" rust/src     # expect: none
```
```
git commit -am "test+docs(concurrency): parallelism/isolation tests, README, memory"
```

---

## 9. Common errors & fixes (intern troubleshooting)

| Symptom | Cause | Fix |
|---|---|---|
| `AttributeError: 'TyProject' object has no attribute 'snapshot'` after a **green build** | Stale `cpython-313` `.so` shadowing the fresh `abi3.so` | `devenv shell -- clean && devenv shell -- build` (§3.1) |
| `error[E0277]: ... cannot be sent between threads safely` / mentions `Ungil` on a `detach` call | You captured a non-`Send` value (a `Bound`, `&PyAny`, `Python`, or a `&self` borrow) in the closure | Own the data before the closure; move only Rust values. Build `pythonize`/`None` **outside** `detach` (§1.2) |
| `error: ... allow_threads ... not found` / `with_gil ... not found` | Old API names | Use `py.detach(...)` / `Python::attach(...)` (§1) |
| Data race / weird panic only under threads | Stored a bare `!Sync` db in the pyclass instead of `Mutex<...>` | Wrap in `Mutex<Option<TyProjectState>>`; clone under the lock (§1.3) |
| Parallel speedup ≈ 1.0 in your test | Shared one session → salsa cache makes subsequent calls trivial; or you measured a rayon-internal-parallel `check` | Use one cold session per thread; compare against a Python-CPU control instead of an absolute threshold (§8.2) |
| `pythonize` borrow/lifetime error | Tried to `pythonize` inside `detach` (no `py` there) | Return the `Dto` from `detach`, then `pythonize(py, &dto)` after (§2.2) |

---

## 10. Definition of Done

- [ ] `compute_*` core functions exist; no analysis logic touches Python (`pythonize`,
      `py.None()`, and `PyErr` construction happen only in the thin method wrappers).
- [ ] Every session read method releases the GIL via `clone_locked_state` + `py.detach`
      (Decision 1b). `grep -n "detach" rust/src/project.rs` shows every read method.
- [ ] `TySnapshot` native class + `Snapshot` Python class exist with **full** read parity;
      both pyclasses are `frozen`; `snapshot()` exists only on `TyO3Session` (Decision 2).
- [ ] `Snapshot` is revision-pinned and provably isolated from `reload` (test 8.4).
- [ ] Parallelism test (8.2) compares Rust vs a Python-CPU control and passes.
- [ ] The export-inventory test (8.1) includes `TySnapshot`.
- [ ] The `project.rs` NOTE is corrected; it agrees with `PYO3_CONCURRENCY_ANALYSIS.md`.
- [ ] `_native_impl.pyi`, `tyo3.__all__`, and README reflect the new surface.
- [ ] `grep -rn "allow_threads\|assume_attached\|unsafe" rust/src` is empty.
- [ ] Full suite + rust + property green; ruff clean.

---

## 11. Risk & sequencing summary

| Phase | What | Risk |
|---|---|---|
| 1 | Extract `compute_*` + `AnalysisError` (big mechanical diff) | 🟡 churn; guarded by `test-rust` snapshots |
| 2 | Session reads → clone + `py.detach` (1b) | 🟢 pattern already proven by the `check` slice |
| 3 | Full `TySnapshot` read surface + `frozen` | 🟢 mirrors Phase 2 bodies |
| 4 | Python `_ReadOps` + full `Snapshot` + exports + stub | 🟢 mostly moves |
| 5 | Tests + docs + memory | 🟢 |

**Total ≈ 2.5–3 days.** The riskiest unknown (does `py.detach` over an owned db clone
satisfy `Ungil`?) is **already answered: yes** — `check` compiles and runs with proven GIL
release. The rest is disciplined repetition of that shape.

---

## Appendix A — PyO3 0.28 fact sheet (with source anchors)

| Fact | Anchor |
|---|---|
| `py.detach<T,F>(f) where F: Ungil + FnOnce()->T, T: Ungil` | `pyo3-0.28.3/src/marker.rs:558` |
| `Ungil` on stable ≡ `Send` (`unsafe impl<T: Send> Ungil for T`) | `marker.rs:188` |
| `!Ungil`: `Python`, `PyAny`/`Bound`, `PyRef`/`PyRefMut`, raw `ffi::*` | `marker.rs:272-292` |
| `Py<T>: Ungil`, `PyErr: Ungil` | `instance.rs:1453`, `err/mod.rs:48` |
| `Python::attach(\|py\| …)` (replaces `with_gil`) | `marker.rs:410` |
| `#[pyclass]` needs `Send` (NoopThreadChecker) or `unsendable` (runtime panic); **no `Sync` requirement** | `impl_/pyclass.rs:200-206` |
| `#[pyclass(frozen)]` — immutable, GIL-free `&self` access | `pyclass.rs:28` |
| `multiple-pymethods` feature (enables split/macro pymethods) | `rust/Cargo.toml` |

## Appendix B — file/line index (pre-change anchors)

| What | Location |
|---|---|
| `TyProjectState` + stale NOTE | `rust/src/project.rs:26-37` |
| `PyTyProject` pyclass | `rust/src/project.rs:41-44` |
| `lock_state` | `rust/src/project.rs:50-64` |
| `clone_locked_state` (slice) | `rust/src/project.rs` (after `lock_state`) |
| `compute_check` (slice) | `rust/src/project.rs` (after `clone_locked_state`) |
| `resolve_file_and_source` | `rust/src/project.rs:67-82` |
| `navigate_to_targets` → `compute_navigate` | `rust/src/project.rs:88-122` |
| `PyTyProject::check` (slice, GIL-released) / `snapshot` | `rust/src/project.rs` |
| Read methods to extract/convert | `project.rs` 201, 240, 277, 310, 357, 378, 399, 420, 458, 487, 508, 565 |
| `reload` (swap — keep GIL-held) / `close` | `rust/src/project.rs:166-196` |
| `PySnapshot` (slice: check + close) | end of `rust/src/project.rs` |
| Native module registration (slice: both classes) | `rust/src/lib.rs:20-33` |
| `TyO3Session` read methods to move to `_ReadOps` | `src/tyo3/session.py:121-344` |
| `TyO3Session.snapshot` + `Snapshot` (slice) | `src/tyo3/session.py` |
| Package exports | `src/tyo3/__init__.py:11-24` |
| Native stub (slice: `snapshot`, `TySnapshot`) | `src/tyo3/_native_impl.pyi` |
| Export-inventory test to fix | `src/tyo3/tests/test_native_objects.py:197` |
| Concurrency test to strengthen | `src/tyo3/tests/test_concurrency.py` |
| Proof of `Send + !Sync` + ty's clone-per-rayon-worker | `PYO3_CONCURRENCY_ANALYSIS.md` |
