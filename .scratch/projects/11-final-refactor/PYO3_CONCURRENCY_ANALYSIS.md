# TyO3 — PyO3 Concurrency Analysis: GIL Release for Async / LSP

**Status:** analysis + design options (no code changed by this document)
**Audience:** TyO3 maintainers deciding how to make the library async/LSP-friendly
**Bottom line:** The current code holds the Python GIL for the entire duration of
every ty analysis call, and a comment in `rust/src/project.rs` claims this is
*unavoidable*. **That claim is wrong.** ty's database is `Send` (just not `Sync`),
and ty itself releases work onto threads by **cloning** it. We can do the same and
release the GIL. This document proves it from the pinned dependency source and lays
out the implementation options.

---

## 0. TL;DR

- **What we have:** every analysis method (`check`, `document_symbols`, `goto_*`,
  `hover`, …) runs with the GIL held start-to-finish. While ty works, **no other
  Python thread runs** — including an `asyncio` event loop. That makes the library
  hostile to the async/LSP use case it's meant for.
- **The stated reason** (`rust/src/project.rs:28`): *"`ProjectDatabase` (Salsa 0.26)
  is `!Send + !Sync` … so we cannot wrap heavy analysis calls in `py.detach(...)`."*
- **The real situation:** `ProjectDatabase` is **`Send + Clone`, but `!Sync`**. The
  failed Phase 3 attempt tried to *borrow* `&db` into the GIL-release closure. `&T`
  is `Send` only if `T: Sync`, so that can't compile. But you don't need to borrow —
  you **move an owned clone** in. `ProjectDatabase: Send`, so that compiles, and
  ty's own parallel checker does exactly this.
- **The fix:** clone the db under a short lock, then `py.allow_threads(move || …)`
  over the owned clone. ~1 day, mechanical, compiler-verified.
- **The async surface:** with the GIL actually released, an `AsyncTyO3Session` that
  dispatches to a thread pool (`asyncio.to_thread`) gives real `await session.check()`
  without blocking the event loop.

---

## 1. Background — three facts you need

### 1.1 The GIL and native extensions
CPython's Global Interpreter Lock allows only one thread to execute Python at a
time. When Python calls into a native extension, the extension **holds the GIL by
default** for the whole call. If that call is pure-native CPU work touching no
Python objects (which ty analysis is), holding the GIL needlessly blocks every
other Python thread in the process.

### 1.2 PyO3's escape hatch: `allow_threads` / `detach`
PyO3 lets you release the GIL around a pure-native section:

```rust
let result = py.allow_threads(|| heavy_pure_rust_work());   // GIL released here
// (PyO3 0.28 also exposes this as `py.detach(|| …)`)
```

While the closure runs, other Python threads proceed; PyO3 re-acquires the GIL
before returning.

### 1.3 The `Ungil` bound (this is the crux)
`allow_threads` requires its closure to satisfy PyO3's `Ungil` bound. In practice
this means **everything captured across the boundary must be `Send`** — you're
conceptually letting that data live while another thread might touch Python.

The decisive Rust rule:

> `&T: Send` **iff** `T: Sync`.   `T: Send` (owned) is a *separate, weaker* property.

So whether you can release the GIL depends entirely on *how* you capture the
database:

| Capture | Requires | `ProjectDatabase` satisfies? |
|---|---|---|
| `&db` (borrow) | `&ProjectDatabase: Send` ⟺ `ProjectDatabase: Sync` | ❌ no (`!Sync`) |
| `db` (owned clone, `move`) | `ProjectDatabase: Send` | ✅ **yes** |

The Phase 3 attempt used the first row. The fix is the second row.

---

## 2. Dependency code walkthrough — proving `Send + Clone, !Sync`

All paths below are the pinned revisions this project builds against:

- ty / ruff: `astral-sh/ruff` rev `3cb09eba689ebb49e799131092121928cc789c18`
  (declared in `rust/Cargo.toml:22-30`, ty tag `0.0.40`)
- salsa: `0.26.2` from crates.io (`rust/Cargo.lock:1669-1672`)

Local checkout roots (for grepping on this machine):

- ty: `~/.cargo/git/checkouts/ruff-b18f69e2b025fac7/3cb09eb/`
- salsa: `~/.cargo/registry/src/index.crates.io-*/salsa-0.26.2/`

### 2.1 `ProjectDatabase` is `Clone`

`crates/ty_project/src/db.rs:35-58`:

```rust
#[derive(Clone)]                                   // ← line 35
pub struct ProjectDatabase {
    project: Option<Project>,                       // salsa interned handle
    files: Files,                                   // salsa interned handle
    system: Arc<dyn System + Send + Sync + RefUnwindSafe>,   // line 51 — Send + Sync
    storage: salsa::Storage<ProjectDatabase>,       // line 57 — the interesting field
}
```

Two things to note immediately:
- The struct **derives `Clone`**. A clone is the salsa "snapshot": cheap, shares the
  underlying storage.
- `system` is explicitly `Arc<dyn … + Send + Sync>`. The only field whose
  thread-safety is in question is `storage`.

### 2.2 `salsa::Storage` — `Send` but `!Sync`, by construction

`salsa-0.26.2/src/storage.rs:83-89`:

```rust
pub struct Storage<Db> {
    handle: StorageHandle<Db>,     // the shareable, Arc-backed core
    zalsa_local: ZalsaLocal,       // ← "Per-thread state"
}
```

`Storage::clone` (`storage.rs:251-258`) is what makes the snapshot model work — it
clones the shared `handle` but gives the clone a **fresh, empty** per-thread local:

```rust
impl<Db: Database> Clone for Storage<Db> {
    fn clone(&self) -> Self {
        Self {
            handle: self.handle.clone(),     // Arc bump — shared storage
            zalsa_local: ZalsaLocal::new(),  // fresh per-thread state
        }
    }
}
```

The salsa authors even document that the per-thread local is what costs you `Sync`.
`Storage::into_zalsa_handle` (`storage.rs:131-134`):

```rust
/// Convert this instance of [`Storage`] into a [`StorageHandle`].
///
/// This will discard the local state of this [`Storage`], thereby returning a value
/// that is both [`Sync`] and [`std::panic::UnwindSafe`].
```

i.e. *drop `zalsa_local` and you get back something `Sync`* → `zalsa_local` is the
thing that makes `Storage` **not** `Sync`.

### 2.3 Why `zalsa_local` is `Send` but `!Sync`

`salsa-0.26.2/src/zalsa_local.rs:34-46`:

```rust
pub struct ZalsaLocal {
    query_stack: RefCell<QueryStack>,                               // line 39
    most_recent_pages: UnsafeCell<FxHashMap<IngredientIndex, PageIndex>>, // line 43
    cancelled: CancellationToken,
}
```

`RefCell<T>` and `UnsafeCell<T>` are **`Send` (when `T: Send`) but never `Sync`**.
That's the textbook interior-mutability profile: you may *move* the cell to another
thread, you may not *share a reference* to it across threads.

**Therefore, propagating up the type:**

| Type | `Send` | `Sync` | Reason |
|---|---|---|---|
| `RefCell` / `UnsafeCell` (interior fields) | ✅ | ❌ | interior mutability |
| `ZalsaLocal` | ✅ | ❌ | contains the above |
| `salsa::Storage<Db>` | ✅ | ❌ | contains `ZalsaLocal` |
| `ProjectDatabase` | ✅ | ❌ | contains `Storage`; `system` is `Send + Sync` |

**Conclusion: `ProjectDatabase: Send + Clone + !Sync`.** The comment in
`project.rs` is right that it's `!Sync`, and wrong that it's `!Send`.

### 2.4 The clincher: ty *itself* releases work onto threads by cloning

You don't have to take the type analysis on faith — ty's own type checker does the
exact pattern we want. `crates/ty_project/src/lib.rs:332-372`,
`Project::check`:

```rust
pub(crate) fn check(self, db: &ProjectDatabase, reporter: &mut dyn ProgressReporter) {
    ...
    {
        let db = db.clone();                 // line 359 — owned clone
        rayon::scope(move |scope| {          // line 362 — move owned db into scope
            for file in &files {
                let db = db.clone();         // line 364 — one owned clone per worker
                db.unwind_if_revision_cancelled();
                scope.spawn(move |_| {       // line 369 — move clone onto worker thread
                    // … type-check `file` using this thread's `db` …
                });
            }
        });
    }
}
```

ty parallelizes across files by **cloning the database and moving owned clones onto
rayon worker threads.** If `ProjectDatabase` were `!Send`, this would not compile.
The whole salsa concurrency story is "snapshot (clone) per thread", *not* "share one
db by reference". That is precisely the capability `py.allow_threads` needs.

---

## 3. Why the Phase 3 attempt failed (and the one-line conceptual fix)

The intended Phase 3 transformation (from the refactoring guide) wrapped the heavy
section while still **borrowing** the db out of the locked state:

```rust
// FAILS to compile: closure captures `&state.db`  (a &ProjectDatabase)
let check_result = py.allow_threads(|| {
    let result = state.db.check();                                   // borrow
    let diagnostics = convert::diagnostics::convert_diagnostics(&state.db, &result);
    dto::CheckResultDto { diagnostics, .. }
});
```

`&ProjectDatabase: Send` requires `ProjectDatabase: Sync` — which is false — so the
`Ungil` bound is unsatisfied and the build errors. The engineer correctly observed
the failure, but mis-attributed it to `!Send` and concluded GIL release was
impossible (`project.rs:28-33`). The actual cause is the **borrow**; the fix is to
**own**.

---

## 4. The fix — clone-into-`allow_threads`

### 4.1 Pattern (using `check` as the worked example)

Current `rust/src/project.rs:218-238` (`check`, GIL held the whole time):

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

Fixed (GIL released during ty work):

```rust
fn check<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
    // 1. Take a cheap salsa snapshot under a SHORT lock, then drop the lock.
    let db = {
        let guard = lock_state(&self.inner, "check")?;
        guard.as_ref().unwrap().db.clone()        // ProjectDatabase: Clone
    };  // ← lock released here

    // 2. GIL RELEASED: pure-Rust analysis runs while other Python threads proceed.
    let check_result = py.allow_threads(move || {  // move the OWNED clone in
        let result = db.check();
        let diagnostics = convert::diagnostics::convert_diagnostics(&db, &result);
        dto::CheckResultDto { diagnostics, files_checked: None, elapsed_ms: None }
    });

    // 3. Re-acquired GIL: build the Python object.
    pythonize(py, &check_result).map_err(|e| PyRuntimeError::new_err(e.to_string()))
}
```

Three deltas vs. the failed attempt:
1. `.clone()` the db (owned) instead of holding `&state.db`.
2. `move` it into the closure.
3. Drop the `MutexGuard` *before* the closure, so the per-session lock isn't held
   while analysis runs (otherwise concurrent calls on the same session would
   serialize on the mutex even though the GIL is free).

The closure now captures only `db: ProjectDatabase` (`Send`) and produces
`CheckResultDto` (plain `serde` data — `Send`). `Ungil` is satisfied; it compiles.

### 4.2 Methods to convert (read-only — safe to snapshot)

All of these are pure reads and follow the identical pattern. Line numbers in
`rust/src/project.rs`:

| Method | Line | Notes |
|---|---|---|
| `check` | 218 | whole-project scan — biggest win |
| `check_file` | 240 | |
| `document_symbols` | 277 | |
| `workspace_symbols` | 310 | |
| `goto_definition` | 357 | via shared helper `navigate_to_targets` (line 88) |
| `goto_declaration` | 378 | via `navigate_to_targets` |
| `goto_type_definition` | 399 | via `navigate_to_targets` |
| `find_references` | 420 | |
| `semantic_tokens` | 458 | |
| `file_occurrences` | 487 | |
| `type_hierarchy` | 508 | returns `None` branch — keep `Bound` None handling |
| `hover` | 565 | returns `None` branch |

For the three `goto_*` methods, thread the owned `db` into the shared
`navigate_to_targets` helper (`project.rs:88`) and run its body inside the closure;
clone once in the caller, move into the helper call.

### 4.3 What stays GIL-held: the write/mutation path

Snapshots are for **reads**. Mutations need exclusive ownership of the *canonical*
db and must remain serialized:

- `reload` (`project.rs:166-181`) rebuilds the database and swaps it into the locked
  `Option` under the mutex. Keep it lock-held; do **not** snapshot. (It's CPU-bound
  db construction, not a ty query — releasing the GIL buys little and the swap must
  be exclusive.)
- `close` (`project.rs:188`) just drops state; trivial.
- Any future incremental editing API (`set_*` on salsa inputs) requires `&mut db`
  and will **cancel in-flight queries** (see §6). Route all writes through the
  canonical db under the lock; never mutate a snapshot.

This gives the clean model: **many concurrent snapshot reads, serialized writes.**

---

## 5. Options for the async / LSP story

Layered. **A is the foundation and should happen regardless.** B is what most
async/LSP consumers actually want. C–E are for heavier concurrency needs.

### Option A — Release the GIL via owned snapshot *(the real Phase 3)*
Apply §4.1 to every read method in §4.2.
- **Effort:** ~1 day; mechanical, compiler-verified.
- **Gain:** the interpreter no longer freezes during analysis. Hard prerequisite for
  everything else.
- **Risk:** low. If any single method's closure fails `Ungil` (it shouldn't — all
  captures are `Send`), leave that one GIL-held and note it. Per-method, not global.

### Option B — Async facade over a thread pool *(the actual "async API")*
With A in place, add an `AsyncTyO3Session` (pure Python) whose methods do:

```python
async def check(self) -> CheckResult:
    return await asyncio.to_thread(self._sync.check)   # GIL freed inside → loop runs
```

- **Effort:** ~1–2 days, pure Python; thin wrapper over the sync class.
- **Gain:** idiomatic `await session.check()` that doesn't block the event loop —
  the LSP/async headline.
- **Note:** worthless *without* A (the worker thread would just re-grab the GIL and
  serialize). With A, real overlap.

### Option C — Explicit snapshot handle for caller-driven concurrency
Expose `session.snapshot()` returning a lightweight read-only handle wrapping a
cloned db (a second pyclass). Power users fan out many concurrent reads (per request
/ per file) with no shared-session lock contention. Mirrors ty's internal model
(§2.4) directly.
- **Effort:** medium (new pyclass + lifetime/close semantics).
- **Gain:** true parallel reads across threads off one logical project; clean
  read(snapshot)/write(session) split.
- **Best when:** building an actual LSP server that issues many concurrent queries.

### Option D — Dedicated worker thread / actor
Keep one owned `ProjectDatabase` on a Rust thread; Python submits requests over a
channel and receives results (futures). Classic single-writer LSP architecture;
serializes writes through one owner and gives natural cancellation/backpressure.
- **Effort:** high (protocol, lifecycle, cancellation plumbing).
- **Verdict:** likely overkill — salsa's clone model (A/C) already gives cheap
  snapshots. Reach for it only if A+C prove insufficient.

### Option E — Process pool
One `TyO3Session` per process. Maximum isolation, zero GIL/lock concerns.
- **Cost:** each worker holds a full `ProjectDatabase` (memory) + IPC result
  serialization.
- **Verdict:** fallback only; in-process clones (A/C) are strictly better for an LSP.

### Recommendation
1. **Do Option A now** and fix the `project.rs` NOTE (§7) — it's steering readers to
   the wrong conclusion.
2. **Ship Option B** as the async surface (small, high value).
3. **Add Option C** when the real LSP server needs concurrent read fan-out.

---

## 6. Caveats to design around

### 6.1 Reads scale via clones; writes don't
Concurrent reads off snapshots are fine and intended. Writes (`reload`, future
`set_*`) need exclusive access. Keep all writes on the canonical db under the mutex.

### 6.2 salsa cancellation on mutation
When the canonical db is mutated while snapshots have queries in flight, salsa
**cancels** those queries by raising `salsa::Cancelled` and unwinding (see
`unwind_if_revision_cancelled` at `ty_project/src/lib.rs:366`, and the
`salsa::Cancelled` handling around `lib.rs:802-807`). For TyO3 this means: if you add
live-editing, a read racing a reload may abort mid-flight. Handle by either
(a) serializing reload against reads, or (b) catching cancellation and retrying on a
fresh snapshot. Today `reload` swaps the whole db under the lock, so a read that
already cloned an old snapshot simply finishes against the old revision — acceptable.

### 6.3 Clone cost
A `ProjectDatabase::clone` is a `StorageHandle` `Arc` bump plus a fresh empty
`ZalsaLocal` (`storage.rs:251`); the interned `project`/`files` handles and the
`Arc<dyn System>` are shared, not deep-copied. ty clones per-file inside rayon
(`lib.rs:364`), so it is designed to be cheap. One clone per TyO3 call is negligible.

### 6.4 The per-session `Mutex`
State lives behind `Mutex<Option<TyProjectState>>` (`project.rs`, `lock_state` at
~line 49). The §4.1 pattern clones under the lock then **drops the guard** before
analysis, so two threads sharing one session still overlap (they serialize only for
the microsecond of the clone). If you'd rather allow concurrent reads to also share
without cloning, a `RwLock` is an option — but the clone model is simpler and matches
ty, so prefer it.

### 6.5 `Ungil` / closure hygiene
Inside the closure, capture only `Send` data: the owned `db`, owned strings (e.g.
`path.to_owned()`), and produce owned DTOs. Do **not** capture the `MutexGuard`, any
`&self`, or any `Bound`/`Py` Python handle — build Python objects (`pythonize`)
*after* the closure returns, with the GIL re-acquired.

---

## 7. Corrected source comment

Replace the misleading NOTE at `rust/src/project.rs:26-33`:

```rust
/// NOTE: `ProjectDatabase` (Salsa 0.26) is `!Send + !Sync` … so we cannot wrap
/// heavy analysis calls in `py.detach(|| ...)` to release the GIL. All Rust
/// analysis therefore runs with the GIL held.
```

with the accurate version:

```rust
/// CONCURRENCY: `ProjectDatabase` (salsa 0.26) is `Send + Clone` but `!Sync`
/// — its `salsa::Storage` holds a per-thread `ZalsaLocal` (`RefCell`/`UnsafeCell`).
/// You therefore cannot share `&db` across threads, but you CAN move an owned
/// clone, which is how ty itself parallelizes (`ty_project::Project::check`
/// clones the db per rayon worker). Read methods take a `db.clone()` snapshot
/// and run inside `py.allow_threads(move || …)` to release the GIL; the
/// canonical db is mutated only under the lock (see `reload`).
```

---

## 8. Validation

`src/tyo3/tests/test_concurrency.py` currently only proves *no deadlock* — it passes
even with the GIL held. After Option A, add a test that proves **overlap**:

- Spin up N threads, each running `check()` (or `document_symbols`) on a non-trivial
  fixture.
- Assert wall-clock is closer to `max(tᵢ)` than to `Σ tᵢ` (e.g. `< 0.6 * Σ`).
- With the GIL held this fails (serialized); with Option A it passes.

Keep it tolerant of CI jitter (generous margin, warm-up run) so it's a signal, not a
flake.

---

## 9. Reference index

### TyO3 source
| What | Location |
|---|---|
| Misleading `!Send + !Sync` NOTE | `rust/src/project.rs:26-33` |
| `lock_state` helper (the per-session mutex) | `rust/src/project.rs:~49` |
| `reload` (write path — keep GIL-held) | `rust/src/project.rs:166-181` |
| `check` (worked example) | `rust/src/project.rs:218-238` |
| Read methods to convert | `project.rs` 240, 277, 310, 357, 378, 399, 420, 458, 487, 508, 565 |
| `navigate_to_targets` shared `goto_*` helper | `rust/src/project.rs:88` |
| Phase-2 signatures (`py: Python<'py>`, `Bound`) | already in place, all methods |
| Phase-3 "documentation only" commit | `8696d36` |
| Dependency pins (ty rev, features) | `rust/Cargo.toml:12, 22-30` |
| salsa version pin | `rust/Cargo.lock:1669-1672` (`salsa 0.26.2`) |
| Concurrency test (currently no-deadlock only) | `src/tyo3/tests/test_concurrency.py` |

### Dependency source (rev `3cb09eb` / salsa `0.26.2`)
| What | Location |
|---|---|
| `ProjectDatabase` struct (`#[derive(Clone)]`, `storage` field) | `ty_project/src/db.rs:35-58` |
| `Project::check` — clone-per-rayon-worker pattern | `ty_project/src/lib.rs:332-372` (clones at 359, 364; `move` scope at 362; `spawn` at 369) |
| `salsa::Cancelled` handling | `ty_project/src/lib.rs:~802-807` |
| `salsa::Storage` struct (`handle` + `zalsa_local`) | `salsa-0.26.2/src/storage.rs:83-89` |
| `Storage::clone` (shares handle, fresh local) | `salsa-0.26.2/src/storage.rs:251-258` |
| `into_zalsa_handle` doc ("discard local → Sync") | `salsa-0.26.2/src/storage.rs:131-134` |
| `ZalsaLocal` (`RefCell`/`UnsafeCell` → `!Sync`) | `salsa-0.26.2/src/zalsa_local.rs:34-46` |

### PyO3
| What | Where |
|---|---|
| `Python::allow_threads` / `detach` (GIL release) | PyO3 0.28 (`pyo3 = "0.28"`, `rust/Cargo.toml:12`) |
| `Ungil` bound (captures must be `Send`) | PyO3 docs — `allow_threads` signature |

---

## Appendix — the one rule to remember

> **`&T: Send` requires `T: Sync`. Owned `T: Send` does not.**
> `ProjectDatabase` is `Send` but not `Sync`, so don't *borrow* it across the GIL
> boundary — *move a clone*. That single distinction is the entire difference between
> "GIL release is impossible" (the current comment) and "GIL release is one `.clone()`
> away" (reality).
