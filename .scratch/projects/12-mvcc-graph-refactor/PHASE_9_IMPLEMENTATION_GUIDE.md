# Phase 9 Implementation Guide — Floating warm fast path (`session.latest`, cancel-retry)

> Audience: an engineer who has finished **Phase 1**
> (`PHASE_1_IMPLEMENTATION_GUIDE.md`, `ContentStore` + `OverlaySystem`), **Phase 2**
> (`PHASE_2_IMPLEMENTATION_GUIDE.md`, the HEAD db over the overlay via real
> discovery), **Phase 3** (`PHASE_3_IMPLEMENTATION_GUIDE.md`, the write path →
> `SyncResult`), **Phase 4** (`PHASE_4_IMPLEMENTATION_GUIDE.md`, independent MVCC
> snapshots), **Phase 5** (`PHASE_5_IMPLEMENTATION_GUIDE.md`, the concurrency
> proof), **Phase 6** (`PHASE_6_IMPLEMENTATION_GUIDE.md`, the HEAD graph
> `apply_delta`), **Phase 7** (`PHASE_7_IMPLEMENTATION_GUIDE.md`, pinned
> `Snapshot.graph()` + `graph.diff`), and **Phase 8**
> (`PHASE_8_IMPLEMENTATION_GUIDE.md`, the watcher as a change source), and is now
> implementing **Phase 9** of `MVCC_SUBSTRATE_ARCHITECTURE.md`.
>
> Goal of Phase 9: add the **floating warm read fast path** — the *only* place in
> the system where a read runs against the **live HEAD database** (sharing its
> `Zalsa`, reusing the writer's warm memos) rather than against an independent,
> revision-pinned snapshot. Because it shares HEAD's `Zalsa`, such a read **can be
> cancelled** by a concurrent `apply_changes` (architecture §0); Phase 9's whole job
> is to make that safe and invisible by **catching `salsa::Cancelled` and retrying**
> — exactly ty_server's `RETRY_ON_CANCELLATION` pattern (`server/api.rs:354`). The
> result is a read surface that always returns the *latest* HEAD state, warm, and
> never surfaces a cancellation to the caller.
>
> This is the deliberate counterpart to the snapshot surface, "distinct by intent"
> (architecture §6, §12):
>
> | Surface | Storage | Consistency | Temperature | Cancellable | Phase |
> |---|---|---|---|---|---|
> | `session.snapshot()` / `Snapshot` | independent per-revision `Zalsa` | **pinned** at R | cold (warms per query) | **never** | 4 |
> | `session.latest` (this phase) | **shared HEAD `Zalsa`** | **floats** to newest | **warm** (writer's memos) | yes → caught + retried | 9 |
>
> Three things land together because they are one capability:
>
> 1. **A cancel-retry helper** — re-clone the HEAD db, run the read inside
>    `py.detach`, catch `salsa::Cancelled`, and retry on a fresh clone, bounded.
> 2. **A floating read surface** — a `PyHeadView` native class (or macro-generated
>    methods) exposing the same read vocabulary as `PySnapshot`, but sourced from a
>    live HEAD clone with the retry wrapper.
> 3. **`session.latest`** — a Python `LatestView(_ReadOps)` that dispatches the
>    existing read methods through `PyHeadView`, so `session.latest.check()` /
>    `.document_symbols(...)` / `.goto_definition(...)` are warm floating reads.
>
> **This is the last feature phase.** After Phase 9 only **Phase 10** (benchmarks)
> remains. Phase 9 is **additive**: it does **not** change `session.snapshot()`, the
> `Snapshot` surface, or the existing `TyO3Session` convenience reads (which remain
> backed by the cached head snapshot from Phase 4). It adds a *new, opt-in* warm
> surface beside them.
>
> When you finish: the project compiles (`devenv shell -- check-rust`,
> `devenv shell -- clippy`), the **entire existing suite still passes**
> (`devenv shell -- rebuild` then `devenv shell -- tests`), and new tests prove the
> load-bearing invariants — a floating read run against a hot writer **never**
> surfaces `salsa::Cancelled`; it always reflects the **latest** HEAD (sees an edit
> a held snapshot does not); and its results match a fresh pinned read of the same
> revision when HEAD is quiescent.

---

## 0. Mental model (read this first)

### 0.1 Two read surfaces, one HEAD — why both exist

The architecture's defining tension (§0): memo reuse ⟺ shared `Zalsa` ⟺ writes block
on readers. Phase 4 resolved it for the **pinned** surface by giving each snapshot
its *own* `Zalsa` (independent `ProjectDatabase` over a frozen overlay), so the
writer's `cancel_others` never sees a snapshot's clone and never blocks, and the
snapshot is never cancelled — at the cost of **cold** memos (each snapshot warms
lazily).

Phase 9 serves the *other* real access pattern: "just show me the latest, fast."
That means reading against the **live HEAD `Zalsa`**, where the writer already
populated memos (a `check()` right after an `edit()` is warm). The unavoidable price
(§0) is that a HEAD-sharing read **is** a clone of HEAD's storage, so:

- while it is alive, a concurrent `apply_changes` **blocks** in `cancel_others`
  (`storage.rs:160`) waiting for `clones == 1`; and
- when that writer sets the cancellation flag, the in-flight read **unwinds with
  `salsa::Cancelled`**.

Phase 9 makes this safe: the read is **cancellable and retried**, so the writer is
blocked only for the few microseconds it takes the read to notice the flag and
unwind (dropping its clone), and the caller never sees the cancellation — the read
re-runs on a fresh, now-warmer clone and returns. This is *exactly* what ty_server
does for its single logical client (`RETRY_ON_CANCELLATION`); Phase 9 brings it to
tyo3 as the explicit, documented "floating fast path" the architecture reserves for
this (§6, §7).

### 0.2 What "floating" and "warm" mean precisely

- **Floating**: each read re-clones the *current* HEAD db, so it reflects whatever
  revision HEAD is at *now* — including edits that landed after a held snapshot was
  taken. There is no pinning. `session.latest.check()` always describes the newest
  HEAD.
- **Warm**: the clone shares HEAD's `Zalsa`, so salsa memos the writer's
  `apply_changes` populated (and that prior floating reads populated) are reused. No
  cold rebuild per read. This is the entire point — it is the fast "glance at
  latest" path the cold snapshot surface cannot be.

Contrast the **existing** `TyO3Session` convenience reads (Phase 4): they dispatch
through a *cached head snapshot* (`_native()`, `session.py:610`) — an *independent*
db pinned at the last-read revision, invalidated on each write. Those are pinned and
isolated (never cancelled), but cold and not strictly "latest" until the next
invalidation. Phase 9 does **not** touch them; it adds `session.latest` beside them.

> **Why additive, not a repoint.** Architecture §6 sketches `session.check()` itself
> as the floating read, and §6.1 talks about collapsing read surfaces. But Phase 4
> deliberately made `TyO3Session`'s convenience reads independent (cached snapshot)
> so they are never cancellable, and Phase 5's whole proof rests on that
> (`PHASE_5_…`, §7.1: *"Phase 9 owns the floating warm read path… If a test clones
> HEAD and reads while writing, a cancellation is expected, not a Phase-5
> failure."*). Repointing `session.check()` to floating would change that contract
> and risk every existing test that calls `session.check()`. The clean, low-risk
> realisation of §6/§7/§12 is a **distinct** surface: `session.latest` (floating,
> warm) beside `session.snapshot()` (pinned, cold) — "distinct by intent" (§12).
> §"alternative" (below) documents repointing for an implementer who wants the
> literal §6 spelling; the recommended path here is additive.

### 0.3 The cancel-retry contract (the one mechanism Phase 9 owns)

Salsa signals cancellation by **unwinding** (panicking) with a `salsa::Cancelled`
payload; `salsa::Cancelled::catch(f)` runs `f`, catches *only* that unwind (resuming
any other panic), and returns `Result<R, Cancelled>`. ty checks the same thing via
`error.payload.downcast_ref::<salsa::Cancelled>()` after
`ruff_db::panic::catch_unwind` (`server/api.rs:354`).

The Phase 9 loop:

```text
read_head_with_retry(op, f):
  attempts = 0
  loop:
    state = lock(head) → head.read_clone() → unlock(head)     # fresh HEAD clone (shares Zalsa)
    match salsa::Cancelled::catch(|| f(&state)):               # run with GIL released
      Ok(value)      => return Ok(value)
      Err(Cancelled) =>
        drop(state)                                            # release the clone → unblock writer
        attempts += 1
        if attempts >= MAX_RETRIES: return Err(InternalTyError("read repeatedly cancelled"))
        continue                                               # re-clone (now warmer) and retry
```

Monotonic progress: each cancellation corresponds to a completed write that advanced
HEAD and warmed memos, so the retry is *more* likely to succeed and faster. In
practice 0–1 retries; the bound only guards a pathological never-quiescent writer.

### 0.4 The honest tradeoff: a floating read briefly blocks the writer

This is the cost the architecture states plainly (§7, §9): the floating read is *the
only place* a write can be momentarily delayed. While a floating read holds its HEAD
clone, a concurrent `apply_changes` waits in `cancel_others` until the read either
finishes or unwinds. Because the read *is* cancellable, that wait is bounded by
"time to notice the flag and unwind," not "time to finish the whole query." Snapshots
have **no** such effect (independent `Zalsa`). So:

- Use `session.snapshot()` when you need isolation/consistency and must never perturb
  the writer (the agent-facing MVCC surface).
- Use `session.latest` for quick warm "what's the current state" reads, accepting
  that under a hot writer they may retry and may briefly delay a write.

Document this on `session.latest`. It is a feature, not a bug: it is why the path is
warm.

### 0.5 GIL discipline (unchanged from the existing reads)

Writes hold the GIL while calling `apply_changes` (`edit` et al. do *not*
`py.detach` — `project.rs:1059`). Floating reads run their analysis inside
`py.detach` (GIL released), exactly like `PySnapshot`'s reads (`project.rs:1259`).
So: a write on thread A (GIL held) and a floating read on thread B (GIL released)
genuinely overlap; the read holds a HEAD clone; the write's `cancel_others` blocks
until the read unwinds. The cloning step (`lock(head) → read_clone()`) needs the
head **mutex** but not the GIL, so it sits *inside* the detached closure for the
retry loop. This is the same `clone_locked_state` the existing reads use
(`project.rs:145`), just called per-attempt.

---

## 1. Prerequisite check

Phase 9 assumes Phases 1–8 are merged/working. Run the baseline first — **always via
the devenv scripts** (`MEMORY.md`: never bare `cargo`/`maturin`/`pytest`; the devenv
shell carries the pinned ty/ruff git deps and `PYTHONPATH=src`):

```bash
devenv shell -- check-rust      # fast cargo check — your inner loop
devenv shell -- clippy          # -D warnings; the CI gate
devenv shell -- test-rust       # Rust-backed integration tests
devenv shell -- tests           # full Python suite (after a rebuild)
```

Phase 9 **touches Rust** (a new dependency, a shared head handle, a new pyclass), so
the loop is: edit → `devenv shell -- check-rust` → `devenv shell -- clippy` →
`devenv shell -- rebuild` (rebuild the `.so`) → `devenv shell -- pytest …` /
`devenv shell -- tests`.

You rely on, and will extend (Rust, `rust/src/project.rs` unless noted):

- `PyTyProject` (`project.rs:114`), `HeadState` (`project.rs:78`), and the
  `ReadCloneSource` trait + `HeadState::read_clone` (`project.rs:101`) — a HEAD
  `read_clone` produces a `TyProjectState { db: db.clone(), root }` whose db
  **shares HEAD's `Zalsa`**. That is the warm-but-cancellable clone Phase 9 reads
  through. (Phase 5 explicitly avoided using it; Phase 9 is its purpose.)
- `clone_locked_state` (`project.rs:145`) and `lock_state` (`project.rs:123`) — used
  per-attempt inside the retry loop.
- The `compute_*` analysis functions (`project.rs:188-720`, e.g. `compute_check`,
  `compute_document_symbols`, `compute_navigate`, …) — pure, GIL-free, take
  `&TyProjectState`, return serde DTOs. Phase 9 reuses them **verbatim**; the only
  new thing is the retry wrapper around them.
- `PySnapshot` (`project.rs:1231`) and its read methods (`project.rs:1239+`) — the
  exact method list `PyHeadView` mirrors. Phase 9's reads differ only in the state
  source (HEAD clone vs frozen) and the retry wrapper.

Python (`src/tyo3/session.py`):

- `_ReadOps` (`session.py:85`) — the shared read surface. `LatestView` subclasses it
  and overrides only `_native()` to return the `PyHeadView`.
- `TyO3Session` (`session.py:567`): `_inner`, `_check_open`, `close`. Phase 9 adds a
  `latest` property.
- `exceptions.py`: `InternalTyError` — raised if retries are exhausted.

Cargo (`rust/Cargo.toml`): Phase 9 adds the `salsa` crate (§2) so `salsa::Cancelled`
is in scope. It must be the **same version ty pins** or the unwind payload won't
downcast.

---

## 2. Cargo: add the `salsa` dependency at ty's pinned version (`rust/Cargo.toml`)

`salsa::Cancelled` is the unwind payload `ProjectDatabase`'s queries raise on
cancellation. To catch it, tyo3 must depend on the **same compiled `salsa` crate**
ty does — same version, same registry — so cargo unifies them into one crate and the
downcast/`catch` is type-compatible. The ruff workspace pins (additional
working-directory checkout, `Cargo.toml:162`):

```toml
salsa = { version = "0.26.1", default-features = false, features = [
    "compact_str", "macros", "salsa_unstable", "inventory",
] }
```

Add to `rust/Cargo.toml` `[dependencies]`:

```toml
# Phase 9: cancel-retry on the floating warm fast path. MUST match the version
# ty_project pins (ruff workspace Cargo.toml) so `salsa::Cancelled` unwind payloads
# from ProjectDatabase queries downcast in OUR crate too.
salsa = { version = "0.26.1", default-features = false, features = [
    "compact_str", "macros", "salsa_unstable", "inventory",
] }
```

> **Why match features.** Cargo unifies features for one crate version across the
> whole build graph, so declaring `salsa = "0.26.1"` is usually enough for the type
> to unify. Mirroring ty's features avoids any surprise where a feature gate changes
> a type's layout. Confirm with `devenv shell -- check-rust` — if `Cargo.lock`
> resolves a *different* salsa version, pin it exactly (`=0.26.1`) and re-run.
>
> **Fallback without adding salsa.** If adding the dep is undesirable, you can
> instead use `ruff_db::panic::catch_unwind` (ruff_db is already a dependency,
> `Cargo.toml:26`) and treat *any* caught panic as a retry candidate. That is less
> precise (it would also retry genuine bugs) and loses the `Cancelled`-only
> guarantee, so the explicit `salsa` dep is recommended. If you take the fallback,
> bound retries tightly and re-raise non-cancellation panics by inspecting the
> message — but prefer the dep.

---

## 3. Rust: shared head handle, the retry helper, and `PyHeadView`

### 3.1 Make the head handle shareable

`PyHeadView` needs to re-clone the *live* HEAD db on every attempt, so it must reach
the same head the `PyTyProject` mutates — not a copy. Change `PyTyProject.inner` from
`Mutex<…>` to `Arc<Mutex<…>>` so the handle can be shared:

```rust
#[pyclass(name = "TyProject", module = "tyo3._native_impl", frozen)]
pub struct PyTyProject {
    inner: Arc<Mutex<Option<HeadState>>>,
    // Phase 8 fields (pending, watcher) unchanged …
}
```

`lock_state(&self.inner, …)` keeps working unchanged: `&Arc<Mutex<T>>` deref-coerces
to `&Mutex<T>` at the call site, so existing call sites compile as-is. Update only
the constructors (`open` `project.rs:1006`, `reload` `project.rs:1030`) to wrap in
`Arc::new(Mutex::new(...))`, and any place that builds `PyTyProject { inner: … }`.

> If wrapping `inner` in `Arc` ripples awkwardly (e.g. `reload` replaces the inner
> `Option` — that still works: `*guard = Some(build_head(...))` mutates through the
> `Arc<Mutex>`), the alternative is to give `PyHeadView` a `Py<PyTyProject>`
> back-reference and add a private accessor. The `Arc<Mutex>` share is cleaner and
> GIL-free; prefer it.

### 3.2 The cancel-retry helper

One generic helper drives every floating read. It re-clones HEAD per attempt, runs
the analysis with the GIL released, and catches `salsa::Cancelled`.

```rust
use std::panic::AssertUnwindSafe;

/// Max attempts before giving up. Each cancellation corresponds to a completed
/// write, so in practice 0–1 retries; the bound only guards a pathological,
/// never-quiescent writer.
const MAX_HEAD_RETRIES: u32 = 50;

/// Run a read `f` against a fresh clone of the LIVE HEAD db, catching salsa
/// cancellation and retrying on a new clone. The clone shares HEAD's Zalsa, so the
/// read is warm but cancellable by a concurrent apply_changes (architecture §0/§7).
///
/// `f` is a pure, GIL-free analysis closure (a `compute_*` call). Runs inside
/// py.detach so a concurrent write (which holds the GIL) can proceed; on
/// cancellation the clone is dropped (unblocking the writer) and the read retries
/// on a now-warmer clone. Returns the analysis DTO, or an error if retries are
/// exhausted / the project is closed.
fn read_head_with_retry<T>(
    py: Python<'_>,
    inner: &Arc<Mutex<Option<HeadState>>>,
    op: &str,
    f: impl Fn(&TyProjectState) -> T + Send,
) -> PyResult<T>
where
    T: Send,
{
    py.detach(|| {
        let mut attempts: u32 = 0;
        loop {
            // Fresh HEAD clone each attempt (shares HEAD Zalsa). No GIL needed.
            let state = clone_locked_state(inner, op)?;
            match salsa::Cancelled::catch(AssertUnwindSafe(|| f(&state))) {
                Ok(value) => return Ok(value),
                Err(_cancelled) => {
                    drop(state); // release the clone so the writer's cancel_others can proceed
                    attempts += 1;
                    if attempts >= MAX_HEAD_RETRIES {
                        return Err(PyRuntimeError::new_err(format!(
                            "{op}() on session.latest was cancelled by concurrent writes \
                             {MAX_HEAD_RETRIES} times; HEAD never quiesced. Use \
                             session.snapshot() for a pinned read."
                        )));
                    }
                    // loop: re-clone (warmer) and retry
                }
            }
        }
    })
}
```

Notes:

- **`clone_locked_state` inside the closure.** It takes `&Arc<Mutex<…>>` (deref to
  `&Mutex`), locks briefly, `read_clone()`s, drops the lock — no GIL. Calling it
  per-attempt is the floating behaviour (re-reads the *current* HEAD). The `?`
  propagates `ProjectClosedError` if the project closed mid-loop.
- **`AssertUnwindSafe`.** `TyProjectState` holds a `ProjectDatabase` which is not
  `UnwindSafe`; the assert is sound because on a caught unwind we *drop* the state
  and re-clone — we never observe a half-mutated value (and a snapshot/HEAD read
  mutates nothing observable anyway). ty uses the same shape.
- **`Cancelled::catch` resumes other panics.** A genuine bug (not a cancellation)
  re-raises as a normal Rust panic, which PyO3 turns into a `PanicException` — it is
  **not** silently retried. Only cancellation is retried.
- **`Send` bounds.** `py.detach` requires the closure be `Send + Ungil`. `inner` is
  `&Arc<Mutex<Option<HeadState>>>` (Send+Sync since `HeadState: Send`), and `f` is
  `Send`. The `compute_*` functions are plain fns, trivially `Send` closures.

### 3.3 `PyHeadView` — the floating read surface

A new pyclass holding a *shared* head handle (not a clone — it must see live HEAD),
exposing the same read methods as `PySnapshot`, each routed through
`read_head_with_retry`.

```rust
/// A floating, warm view of the LIVE HEAD. Unlike PySnapshot (pinned, independent,
/// never cancelled), each read here clones the live HEAD db (sharing its Zalsa) and
/// retries on salsa cancellation. Reflects the newest HEAD revision; warm because it
/// reuses the writer's memos. The ONLY place a read can be cancelled (and the only
/// place a read can briefly delay a write) — architecture §7.
#[pyclass(name = "TyHeadView", module = "tyo3._native_impl", frozen)]
pub struct PyHeadView {
    inner: Arc<Mutex<Option<HeadState>>>,
}

#[pymethods]
impl PyHeadView {
    fn files(&self, py: Python<'_>) -> PyResult<Vec<String>> {
        read_head_with_retry(py, &self.inner, "files", |s| compute_files(s))
    }

    fn check<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let dto = read_head_with_retry(py, &self.inner, "check", |s| compute_check(s))?;
        pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    fn document_symbols<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_string();
        let dto = read_head_with_retry(py, &self.inner, "document_symbols", move |s| {
            compute_document_symbols(s, &path)
        })?;
        pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // … the remaining read methods (goto_definition, find_references, hover,
    //   file_occurrences, type_hierarchy, completions, semantic_tokens, etc.),
    //   each: capture args by value into a `move` closure, call the SAME compute_*
    //   PySnapshot uses, route through read_head_with_retry, pythonize the DTO.
}
```

> **Avoid the ~20-method copy-paste with a macro.** `PySnapshot` and `PyHeadView`
> have identical method signatures; only the body differs (frozen
> `clone_locked_state` + `py.detach` vs `read_head_with_retry`). Factor a
> `macro_rules!` that, given a method name, arg list, and `compute_*` call, emits the
> method body — and invoke it for both classes. If the macro balloons the PR, it is
> acceptable to hand-write `PyHeadView`'s methods by copying `PySnapshot`'s and
> swapping the wrapper; but a macro keeps the two surfaces from drifting. State your
> choice in the PR.
>
> **Error handling parity.** Each method must surface the same typed errors
> `PySnapshot` does (path/position errors from the analysis). Those come out of the
> `compute_*`/`pythonize` path identically; the retry wrapper only adds the
> `Cancelled` handling, so reuse `PySnapshot`'s error mapping verbatim.

### 3.4 `PyTyProject.head_view()`

```rust
#[pymethods]
impl PyTyProject {
    /// A floating, warm view of the live HEAD (architecture §6 "floating latest
    /// reads"). Each read reflects the newest revision, reuses the writer's memos,
    /// and is internally retried on salsa cancellation. For pinned, isolated reads
    /// use `snapshot()` instead.
    fn head_view(&self) -> PyResult<PyHeadView> {
        // Validate the project is open, then share the head handle.
        let _ = lock_state(&self.inner, "head_view")?;
        Ok(PyHeadView {
            inner: Arc::clone(&self.inner),
        })
    }
}
```

> `head_view()` shares the `Arc` — it does **not** clone the db. The db clone happens
> per-read inside `read_head_with_retry`, which is what makes the view *float* (each
> read re-clones the then-current HEAD). A `PyHeadView` is cheap and may be created
> per call or held; holding one pins nothing (it only holds the `Arc<Mutex>`), so it
> never blocks writes by existing.

---

## 4. Python: `LatestView` + `session.latest` (`src/tyo3/session.py`)

`LatestView` reuses the entire `_ReadOps` surface and only overrides where the read
dispatches — to the `PyHeadView`. No method bodies are duplicated.

```python
# ── LatestView — floating warm reads (Phase 9) ──────────────────────────────


class LatestView(_ReadOps):
    """A floating, warm read view of the live HEAD.

    Every read reflects the *newest* HEAD revision (including edits made after this
    view was obtained) and reuses the type-checker's warm memos, so it is faster
    than a cold snapshot for "what is the current state?" glances. Internally each
    read is retried if a concurrent write cancels it, so callers never see a
    cancellation.

    Tradeoff (architecture §7): because a floating read shares HEAD's storage, it
    can briefly delay a concurrent write (until the read notices the write and
    retries). For isolated, repeatable reads that never perturb the writer, use
    :meth:`TyO3Session.snapshot` instead — distinct by intent.
    """

    def __init__(self, native_head_view: Any) -> None:
        self._inner = native_head_view
        self._closed = False

    def _native(self) -> Any:
        self._check_open()
        return self._inner

    # No close()/revision: a LatestView pins nothing and has no fixed revision
    # (it floats). It is valid as long as the owning session is open.
```

Add the property to `TyO3Session`:

```python
@property
def latest(self) -> LatestView:
    """A floating, warm read view of the live HEAD (Phase 9).

    ``session.latest.check()`` reflects the newest revision, warm. Contrast
    ``session.snapshot()`` (pinned, cold, isolated). See :class:`LatestView`."""
    self._check_open()
    try:
        native_head_view = self._inner.head_view()
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e
    except Exception as e:
        raise InternalTyError(f"Unexpected error in latest: {e}") from e
    return LatestView(native_head_view)
```

Notes:

- **No `_invalidate_head_snap`.** `latest` is not cached and pins nothing; it always
  re-reads live HEAD. There is no cache to invalidate.
- **`_ReadOps` works unchanged.** Every read method already does
  `self._native().<method>(...)` and validates the DTO. `LatestView._native()`
  returns the `PyHeadView`, whose methods have the **same names and signatures** as
  the snapshot's — so `LatestView` gets the entire read surface for free.
- **Exception mapping** in `_ReadOps` already maps `_NativeClosedError`/path/position
  errors; the `PyHeadView` raises the same ones, so no `_ReadOps` change is needed.
  The retry-exhaustion `PyRuntimeError` falls through to the generic
  `InternalTyError` wrapper — acceptable (it is a rare pathological case).

### 4.1 (Documented alternative) repoint the convenience reads

If you want the literal §6 spelling where `session.check()` itself is the floating
read, override the convenience reads to dispatch through `latest`. The minimal,
non-duplicating way is to repoint `TyO3Session._native()`:

```python
# ALTERNATIVE — not the recommended default. Makes session.check() etc. floating.
def _native(self) -> Any:
    self._check_open()
    return self._inner.head_view()      # floating warm, cancel-retried
```

This deletes the Phase-4 cached-head-snapshot dispatch (`_head_snap`,
`_invalidate_head_snap`). **Only do this if** you re-audit every existing test that
calls `session.check()`/`document_symbols()`/… for an assumption of pinning or
non-cancellation, and you accept that convenience reads can now briefly delay
writes. Phase 5's stress tests explicitly treat HEAD-clone reads as Phase-9
territory, so re-validate them. The recommended path keeps the convenience reads as
they are and exposes floating reads only via the explicit `session.latest` — minimal
blast radius, "distinct by intent." State which you chose in the PR.

---

## 5. `.pyi` stubs and exports

- `src/tyo3/_native_impl.pyi`: add a `TyHeadView` class stub with the read-method
  signatures (mirror `TySnapshot`), and `def head_view(self) -> TyHeadView: ...` on
  `TyProject`.
- `src/tyo3/__init__.py`: export `LatestView` in `__all__` if `Snapshot` is exported
  there (keep the surface consistent). Otherwise the inline class suffices.
- No new model — floating reads return the **same** validated models (`CheckResult`,
  `Symbol`, `DefinitionTarget`, …) as the snapshot reads.

---

## 6. Tests (`src/tyo3/tests/test_floating_reads.py` + Rust)

The load-bearing invariants: (1) floating reads **never** surface `salsa::Cancelled`
under a hot writer; (2) they reflect the **latest** HEAD (see an edit a held snapshot
does not); (3) they **match** a pinned read when HEAD is quiescent; (4) holding a
`LatestView` pins nothing. Hang-prone concurrency goes in a **subprocess with an
external timeout**, per Phase 5 §2.2 (a blocked GIL-holding write can starve an
in-process timeout).

### 6.1 Rust: the retry mechanism is exercised (`rust/src/project.rs`)

Under `#[cfg(test)] mod phase9_floating_tests` (in `project.rs` for access to
`build_head`, `read_head_with_retry`, `compute_check`). The clean Rust proof reuses
the Phase 5 bounded-worker pattern: a writer thread hammers HEAD while a floating
read loops; assert no read errors and the writer completes.

```rust
#[cfg(test)]
mod phase9_floating_tests {
    use super::*;
    use std::io::Write;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{mpsc, Arc as StdArc, Mutex as StdMutex};
    use std::time::Duration;

    fn project(body: &str) -> (tempfile::TempDir, SystemPathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let mut f = std::fs::File::create(dir.path().join("a.py")).unwrap();
        f.write_all(body.as_bytes()).unwrap();
        let root = SystemPathBuf::from_path_buf(dir.path().canonicalize().unwrap()).unwrap();
        (dir, root)
    }

    #[test]
    fn floating_read_against_hot_writer_never_errors() {
        // NOTE: this drives read_head_with_retry's inner loop WITHOUT py.detach by
        // calling its core directly (factor the loop body into a helper if your
        // read_head_with_retry is py-bound). The point: cancellation is caught and
        // the read eventually returns Ok.
        let (_d, root) = project("x: int = 0\n");
        let inner: StdArc<StdMutex<Option<HeadState>>> =
            StdArc::new(StdMutex::new(Some(build_head(root.clone(), ContentStore::new()))));
        let a = root.join("a.py");

        let stop = StdArc::new(AtomicBool::new(false));
        let (err_tx, err_rx) = mpsc::channel::<String>();

        // Reader thread: floating reads in a loop.
        let reader = {
            let inner = StdArc::clone(&inner);
            let stop = StdArc::clone(&stop);
            std::thread::spawn(move || {
                while !stop.load(Ordering::Relaxed) {
                    // retry loop core (mirror read_head_with_retry without py.detach):
                    let mut attempts = 0u32;
                    loop {
                        let state = {
                            let g = inner.lock().unwrap();
                            g.as_ref().unwrap().read_clone()
                        };
                        match salsa::Cancelled::catch(std::panic::AssertUnwindSafe(|| {
                            compute_check(&state)
                        })) {
                            Ok(_) => break,
                            Err(_) => {
                                drop(state);
                                attempts += 1;
                                assert!(attempts < 1000, "cancelled far too many times");
                            }
                        }
                    }
                }
                let _ = err_tx;
            })
        };

        // Writer: hammer HEAD.
        let (done_tx, done_rx) = mpsc::channel();
        {
            let inner = StdArc::clone(&inner);
            std::thread::spawn(move || {
                for i in 1..=100 {
                    let mut g = inner.lock().unwrap();
                    let head = g.as_mut().unwrap();
                    let event = classify_overlay_edit(&head.system, &head.db, &a);
                    head.store.insert_text(a.clone(), format!("x: int = {i}\n"));
                    commit_head(head, std::slice::from_ref(&event), vec![], vec![a.as_str().to_string()], vec![], false);
                }
                let _ = done_tx.send(());
            });
        }

        done_rx
            .recv_timeout(Duration::from_secs(10))
            .expect("writer did not finish while floating reads ran");
        stop.store(true, Ordering::Relaxed);
        reader.join().unwrap();
        assert!(err_rx.try_iter().next().is_none());
    }
}
```

> If `read_head_with_retry` is hard to call without a `Python<'_>`, factor its loop
> body into a `pub(crate) fn read_head_loop<T>(inner, op, f) -> PyResult<T>` (no
> `py.detach`) and have `read_head_with_retry` be `py.detach(|| read_head_loop(...))`.
> The test then calls `read_head_loop` directly. Keep the GIL-release only in the
> thin py-bound wrapper.

### 6.2 Python: never-cancelled under a hot writer (subprocess) (`test_floating_reads.py`)

Reuse the Phase 5 subprocess helper (`_run_child` / `_assert_child_ok`,
`PHASE_5_…` §4.1). The external `timeout=` is the real deadlock guard.

```python
def test_latest_reads_survive_hot_writer_subprocess(tmp_path):
    result = _run_child(
        tmp_path,
        """
        import threading
        from tyo3 import TyO3Session

        open("a.py", "w").write("x: int = 0\\n")
        errors = []
        stop = threading.Event()

        with TyO3Session(".") as s:
            latest = s.latest

            def reader():
                try:
                    while not stop.is_set():
                        latest.check()                  # floating, warm, retried
                        latest.document_symbols("a.py")
                except BaseException as exc:             # a leaked salsa::Cancelled would land here
                    errors.append(repr(exc)); stop.set()

            threads = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
            for t in threads: t.start()

            for i in range(80):
                s.edit("a.py", f"x: int = {i}\\n" if i % 2 else "x: int = 'bad'\\n")

            stop.set()
            for t in threads: t.join(timeout=3)
            assert not errors, errors
        """,
        timeout=20.0,
    )
    _assert_child_ok(result)
```

### 6.3 Python: floating reflects latest; snapshot stays pinned

```python
def test_latest_reflects_newest_head(tmp_path):
    (tmp_path / "a.py").write_text("x: int = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        snap = s.snapshot()                       # pinned at r0
        try:
            r0_syms = {sym.name for sym in snap.document_symbols("a.py")}
            s.edit("a.py", "x: int = 1\ny: int = 2\n")     # HEAD advances
            latest_syms = {sym.name for sym in s.latest.document_symbols("a.py")}
            assert "y" in latest_syms                       # floating sees the edit
            assert "y" not in r0_syms                        # snapshot did not
            # the held snapshot is STILL pinned:
            assert {sym.name for sym in snap.document_symbols("a.py")} == r0_syms
        finally:
            snap.close()


def test_latest_matches_pinned_when_quiescent(tmp_path):
    (tmp_path / "a.py").write_text("x: int = 'bad'\n")    # a type error
    with TyO3Session(str(tmp_path)) as s:
        # No concurrent writes: floating and pinned describe the same revision.
        latest = s.latest.check()
        with s.snapshot() as snap:
            pinned = snap.check()
        assert len(latest.diagnostics) == len(pinned.diagnostics)


def test_latest_view_pins_nothing(tmp_path):
    # Holding a LatestView must not block edits (it pins no revision/clone).
    (tmp_path / "a.py").write_text("x = 0\n")
    with TyO3Session(str(tmp_path)) as s:
        view = s.latest
        view.check()                                 # warm it
        for i in range(20):
            s.edit("a.py", f"x = {i}\n")             # must not hang/deadlock
        assert "x" in {sym.name for sym in view.document_symbols("a.py")}
```

> Adapt `.diagnostics`/`.name` to the existing model accessors used elsewhere in the
> suite. The *assertions* are the contract: floating == latest, snapshot == pinned,
> view holds nothing.

---

## 7. Build, test, iterate (devenv)

Phase 9 changes Cargo, Rust, and Python — run it all through the devenv scripts
(never bare `cargo`/`maturin`/`pytest`; `MEMORY.md`):

```bash
# After editing Cargo.toml, confirm salsa resolves to ty's version:
devenv shell -- check-rust            # fast; flags a mismatched/duplicate salsa
devenv shell -- clippy                # -D warnings — CI gate

# Rust mechanism tests:
devenv shell -- test-rust

# Rebuild the native extension (removes the stale .so first), then run Python:
devenv shell -- rebuild
devenv shell -- pytest src/tyo3/tests/test_floating_reads.py -q -s
devenv shell -- tests                 # FULL suite — prove no regressions
```

If `check-rust` reports two `salsa` versions, pin tyo3's to `=0.26.1` and re-run; a
duplicate compiled salsa means the `Cancelled` downcast/`catch` will silently never
match (cancellations would escape). `devenv shell -- check-so` confirms the rebuilt
extension imports. A clean full-suite run proves `session.latest` is additive: the
snapshot surface (Phase 4/5), graph (Phase 6/7), and watcher (Phase 8) are unchanged.

---

## 8. Gotchas & decisions (read before you debug)

1. **`salsa` must be ty's exact version (§2).** A second compiled `salsa` crate
   means `salsa::Cancelled::catch` and the downcast see a *different* type than the
   one `ProjectDatabase` throws, so cancellations escape as uncaught panics and
   reads fail under a hot writer. `devenv shell -- check-rust` + `Cargo.lock` are the
   check; pin `=0.26.1` if needed.

2. **Re-clone HEAD *inside* the retry loop (§3.2).** The floating behaviour and the
   warm retry both depend on cloning the *current* HEAD on every attempt. Clone once
   outside the loop and you neither float nor benefit from the post-write warm memos —
   and you'd retry against the same cancelled state forever.

3. **Drop the clone before retrying (§0.4/§3.2).** On `Cancelled`, `drop(state)`
   *before* looping. The writer's `cancel_others` is waiting for `clones == 1`;
   holding the old clone across the next `clone_locked_state` keeps the count high
   and can livelock the writer. Drop, then re-clone.

4. **Run the read in `py.detach` (§0.5).** The analysis must release the GIL so a
   concurrent write (GIL-held) can run, signal cancellation, and let the read unwind.
   Cloning needs the head **mutex**, not the GIL, so it sits inside the detached
   closure. This mirrors `PySnapshot`'s reads.

5. **`AssertUnwindSafe` is required and sound (§3.2).** `ProjectDatabase` is not
   `UnwindSafe`; assert it because on a caught unwind we discard the clone and
   re-clone — no half-state is observed. ty does the same.

6. **Only cancellation is retried (§0.3).** `Cancelled::catch` resumes any other
   panic. Do **not** broaden the catch to all panics — that would mask real bugs as
   silent retries. (The `ruff_db::panic::catch_unwind` fallback in §2 loses this
   precision; prefer the salsa dep.)

7. **Bound the retries (§3.2).** A never-quiescent writer could spin the loop
   forever. `MAX_HEAD_RETRIES` then a clear error pointing the caller at
   `snapshot()`. In practice 0–1 retries; the bound is a safety net, not a normal
   path.

8. **`session.latest` is additive — do not repoint by default (§0.2/§4.1).** Keep
   the Phase-4 cached-head-snapshot convenience reads intact. Repointing
   `session.check()` to floating is an explicit, audited alternative, not the
   default; Phase 5's contract assumes convenience reads are not HEAD clones.

9. **`head_view()` shares the `Arc`; it clones no db (§3.4).** Creating or holding a
   `LatestView`/`PyHeadView` pins nothing and never blocks a write *by existing* —
   only an in-flight floating *read* can briefly delay a write. Lock this with
   `test_latest_view_pins_nothing`.

10. **Subprocess + external timeout for the hot-writer test (§6.2).** A blocked
    GIL-holding write starves in-process thread timeouts (Phase 5 §2.2). The
    `subprocess.run(timeout=…)` is the reliable kill switch; a hang there fails CI
    instead of freezing it.

11. **Keep `PyHeadView` and `PySnapshot` in lock-step (§3.3).** They expose the same
    read vocabulary. A macro prevents drift; if you hand-copy, add a test that every
    public `PySnapshot` read method exists on `PyHeadView` so a future read added to
    one is not forgotten on the other.

12. **Use the devenv scripts (§7).** `check-rust`/`clippy` inner loop, `rebuild`
    before Python, `test-rust`/`tests` gates. Bare tools miss the pinned ty/ruff/salsa
    deps and `PYTHONPATH=src` (`MEMORY.md`).

---

## 9. Explicitly OUT of scope for Phase 9

- **Benchmarks** — warm floating-read latency vs cold snapshot warm-up, retry rate
  under write load, memory — **Phase 10**. Phase 9 proves *correctness* (never
  cancelled, always latest, matches pinned when quiescent), not speed.
- **Repointing `session.check()` to floating as the default** — documented as an
  alternative (§4.1) but not the Phase 9 deliverable. The deliverable is the additive
  `session.latest`.
- **A floating graph view.** `session.graph` (Phase 6) is already the live, mutable
  HEAD graph — the warm "latest" graph counterpart to the cold pinned `snap.graph()`
  (Phase 7). Phase 9 does not add a graph surface; it composes with the existing one.
- **Changing the snapshot surface or the MVCC model.** `session.snapshot()` /
  `Snapshot` stay pinned, independent, never cancelled. Phase 9 adds a *second*
  surface; it does not weaken the first.
- **Async/await or callback delivery of reads.** Floating reads stay synchronous
  (GIL released during analysis, like every existing read).
- **Sharing memos across snapshots / forking salsa storage** — rejected by the
  architecture (§2.3); Phase 9's warmth comes from sharing the *live* HEAD Zalsa, not
  from any new storage trick.

If you find yourself writing timing assertions, adding a graph view to `latest`,
forking salsa, or making `Snapshot` cancellable, stop — you have left Phase 9.

---

## 10. Definition of Done

- [ ] `rust/Cargo.toml`: `salsa = "0.26.1"` (ty's pinned version + features) added;
      `devenv shell -- check-rust` confirms a single resolved salsa.
- [ ] `project.rs`: `PyTyProject.inner` is `Arc<Mutex<Option<HeadState>>>`;
      constructors (`open`, `reload`) updated; existing `lock_state` call sites
      unchanged (deref coercion).
- [ ] `project.rs`: `read_head_with_retry` (re-clone HEAD per attempt,
      `py.detach`, `salsa::Cancelled::catch`, bounded retry, drop-before-retry).
- [ ] `project.rs`: `PyHeadView` pyclass mirroring `PySnapshot`'s read surface,
      each method routed through `read_head_with_retry` and the same `compute_*`
      (macro-generated or hand-copied — stated in PR).
- [ ] `project.rs`: `PyTyProject.head_view() -> PyHeadView` (shares the `Arc`,
      clones no db).
- [ ] `session.py`: `LatestView(_ReadOps)` overriding only `_native()`; `latest`
      property on `TyO3Session`.
- [ ] `_native_impl.pyi`: `TyHeadView` stub + `head_view`; `__init__.py` export of
      `LatestView` if `Snapshot` is exported.
- [ ] Rust test: floating reads against a hot writer never error and the writer
      completes (bounded worker, channel timeout).
- [ ] Python tests (`test_floating_reads.py`): hot-writer subprocess (no leaked
      `Cancelled`); floating reflects newest HEAD while a held snapshot stays pinned;
      floating matches pinned when quiescent; holding a `LatestView` pins nothing.
- [ ] `devenv shell -- check-rust` / `devenv shell -- clippy` clean;
      `devenv shell -- test-rust` passes.
- [ ] `devenv shell -- rebuild` then `devenv shell -- tests` shows **no regressions**
      (Phase 4/5 snapshot/concurrency, Phase 6/7 graph, Phase 8 watcher unchanged).
- [ ] PR notes: salsa version pin; whether `inner` was `Arc`-wrapped or a
      `Py<PyTyProject>` back-reference used; macro vs hand-copied `PyHeadView`;
      whether convenience reads were repointed (alternative §4.1) or left additive;
      `MAX_HEAD_RETRIES` chosen.

---

## 11. How this closes the architecture (and seeds Phase 10)

Phase 9 completes the read model the architecture promised (§6, §7, §12): **two
surfaces, distinct by intent**, over one HEAD —

- `session.snapshot()` / `Snapshot` (Phase 4): pinned, independent, cold, **never
  cancelled** — the agent-facing MVCC surface, paired with `snap.graph()` (Phase 7)
  for a fully consistent revision view.
- `session.latest` (Phase 9): floating, shared-HEAD, **warm**, cancellable →
  internally retried — the "glance at the newest state" fast path, paired with the
  live `session.graph` (Phase 6) as its graph counterpart.

Every change source — agent `edit`, editor buffer, virtual buffer, and the disk
watcher (Phase 8) — moves HEAD through the single writer; both read surfaces observe
the result on their own terms. The cancel-retry confined to `session.latest` is the
*only* place a read can be cancelled or briefly delay a write (architecture §7),
exactly as designed.

- **Phase 10** benchmarks the honest costs the whole architecture stated (§9, §11):
  cold snapshot warm-up vs warm floating-read latency; the floating-read retry rate
  and writer-stall under load; single-file `apply_delta` vs full rebuild;
  copy-on-pin vs self-build; memory across K retained snapshots/pins. Phase 9 gives
  Phase 10 the warm/cold pair to measure against each other.

Keep the salsa version matched, the HEAD re-clone inside the retry loop, the clone
dropped before each retry, and the floating surface additive, and the substrate has
its complete, two-surface read model — ready to be measured, not extended.
