# Phase 5 — One native `commit()` with staging and rollback

> A step-by-step execution guide for **Phase 5** of
> `REFINED_IMPLEMENTATION_PLAN.md`. Read that plan's "Phase 5" section and §5.3 /
> §5.10 / §5.12 of `REFINED_IMPLEMENTATION_CONCEPT.md` once before starting — this
> guide assumes that vocabulary (the single-writer commit transaction, the
> in-lock step order, "no torn publish," the sidecar as a commit participant, the
> typed error model) and turns it into concrete edits against the code as it
> exists today.
>
> **Phase 5 depends on Phases 1–4.** Phase 1 made committed generations complete
> and reconciled identity *at open*. Phase 2 added the native `CodeLayer` and the
> `CodeDeltaDto`, produced inside the commit and proven structurally
> parity-equal. Phase 3 reshaped the public write result into the id-level
> `CommitDelta` with the Phase 2 `code_delta` **nested** inside it. Phase 4 cut the
> graph over to a **pure applier** of that native delta and removed every
> read-side write. Phase 5 restructures the *commit transaction itself*: it
> funnels every write through one native `commit()` that **stages** all next-state,
> writes the sidecar to temporaries and renames atomically, and **publishes the
> revision last** — so a failure in any in-lock step rolls the whole thing back to
> R−1 with **no torn publish** and **no bus delta enqueued**. If any earlier
> phase's milestone gate is not green, stop and finish it first.

---

## 0. The dev environment — read this first, it governs every command below

This repository is a **devenv.sh (Nix-managed) dev environment**. The toolchain
(the right `rustc`, `cargo`, `maturin`, `python`, `pytest`, `ruff`, and the
project's custom scripts) only exists *inside* the devenv shell. A `cargo` or
`pytest` run from a bare login shell is either missing or the wrong version and
will give you misleading results.

**The rule for the entire phase: every in-repo operation runs through the devenv
shell.** Two equivalent forms:

- One-shot (preferred in this guide, copy-pasteable):
  ```bash
  devenv shell -- cargo test --manifest-path rust/Cargo.toml project authored sidecar
  ```
- Interactive (if you are iterating rapidly):
  ```bash
  devenv shell        # drop into the environment once
  # …then run cargo / pytest / the project scripts directly inside it…
  ```

The project defines custom scripts inside `devenv.nix` — `build`, `tests`,
`test-rust`, `test-quick`, `clean`, `status`, etc. When this guide says "rebuild
the extension," it means `devenv shell -- build` (which runs maturin so Python
sees the freshly compiled Rust). **A pure `cargo test` does not refresh the
compiled extension Python imports.** Phase 5 adds a native fault-injection seam
and new typed exceptions that the Python rollback test
(`test_final_transaction_rollback.py`) exercises *through the session* — so after
**every** Rust change the Python suite must observe, run `devenv shell -- build`
before the pytest gate. Forgetting this is the single most common way to chase a
phantom failure (the test arms a fault on the *old* compiled extension and sees
the pre-Phase-5 behaviour).

The suites are slow. Allow **~10–15 minutes** for a full run; launch full suites
in the background and give them time rather than assuming a hang.

> Throughout the rest of this document, **assume every `cargo`, `pytest`,
> `maturin`, `ruff`, and project-script invocation is prefixed with
> `devenv shell --`**, even where a line is abbreviated for readability.

---

## 1. What Phase 5 changes, and why

**Goal (from the plan):** every write is one native transaction that either fully
publishes or fully rolls back, with the sidecar as a participant (§5.3, §5.10).
Once publication is the *last* in-lock step, no Python-side write lock is needed.

### Carried over from Phase 1 — funnel these through the commit (MUST address here)

Phase 1 made committed generations the *authoritative* record (ingest at open;
complete generations; snapshot reads no disk). Two write paths were intentionally
left for Phase 5 to fix because they belong to the "single commit funnel" work,
**not** the content gate. When you build `commit(mutation)` (Step 5.1), these two
must be routed through it. Each has an `xfail(strict=True)` test that will flip to
a failure (forcing the marker's removal) the moment you fix it — they are your
acceptance signal:

1. **`sync_all` must re-ingest disk, not just republish + Rescan.**
   Today `sync_all` (`rust/src/project.rs`, the `fn sync_all` PyO3 method) does
   `head.system.publish(head.store.capture())` + `apply_changes(&[Rescan])`. Since
   the generation is now authoritative, that **no longer discovers files created
   after open** — it republishes the existing (possibly empty) generation, so a new
   disk file is "File not in project". `sync_path` and pre-open `ingest_project`
   both work; `sync_all` is the gap. Fix: route `sync_all` through
   `ContentStore::ingest_project` (re-read the whole project into the generation as
   one batch) so new/changed/deleted files are picked up, then commit normally.
   - **Test (remove the xfail when fixed):**
     `test_graph_build.py::TestGraphConstruction::test_content_hash_updates_incrementally_by_semantic_body`.

2. **The watcher's `apply_watch_events` must distinguish an unsaved buffer from
   ingested disk content.** It drops any event whose path returns
   `ContentStore::has_overlay(path) == true` (the "unsaved buffer wins over disk"
   rule). Phase 1 now interns *every* project file at open, so `has_overlay` is
   true for all of them and **every watcher event is dropped** (`poll_changes`
   returns `None`). Fix as part of the commit funnel: track which paths carry a
   genuinely *unsaved overlay edit* (from `edit`/`edit_virtual`) separately from
   paths whose content was ingested from disk, and gate the buffer-wins rule on the
   former only.
   - **Tests (remove the xfails when fixed):**
     `test_watch.py::test_deleted_event`,
     `test_watch.py::test_injected_change_matches_expected_delta`,
     `test_watch.py::test_real_watcher_observes_disk_change` (strict=False — FS
     timing), and
     `test_gate8_bus.py::TestWatcherBus::test_inject_changes_fires_bus`.

**The situation today** (confirmed in the current code):

- **Publication happens first, persistence happens later, and persistence
  failures are swallowed.** `commit_head` (`rust/src/project.rs:1435`) calls
  `head.system.publish(head.store.capture())` **before** anything else
  (`project.rs:1444`) — the comment even says "publish BEFORE apply so
  apply_changes re-reads new content." It then applies the engine change
  (`:1446`) and reconciles identity (`:1450`). Reconciliation persists the
  identity registry *inside* `run_identity_reconciliation`
  (`project.rs:1410-1418`), and on a write failure it **logs and continues**:
  ```rust
  if let Err(e) = head.sidecar.write_atomic(&identity_path, &bytes) {
      log::error!("Failed to persist identity registry: {}", e);   // swallowed!
  }
  ```
  So a revision is published, observable, and bus-notified even though its durable
  identity never reached disk. That is precisely the §5.3 "a commit that succeeds
  in memory but fails to persist its sidecar is **not** committed" violation, and
  the §5.12 "do not swallow" violation.

- **The authored write publishes before it persists, and rolls back only its
  in-memory map.** `author` (`project.rs:1927`) bumps + **publishes** the revision
  at `project.rs:1958`, *then* copies-on-write the authored store (`:1967`), *then*
  serialises and persists the record (`:1979`,`:1989`). On a persistence failure
  it restores `head.authored = prior` (`:1994`) — but the revision has **already
  been published**: head has advanced, the generation is retained, and (post
  Phase 6) a bus delta would already be in flight. The in-memory rollback is a
  half-measure; head is not rolled back. This is the §5.3 / Phase 5.3 defect
  named in the plan.

- **There is no staging step and no single commit funnel.** Each write kind has
  its own body that mutates `head` in place and publishes mid-way:
  `commit_head` (`edit`/`edit_many`/`edit_virtual`), `sync_path_inner`
  (`project.rs:1478`, used by `sync_path` and `discard`), the watcher fold
  (`project.rs:1548`), `sync_all` (`project.rs:2033`, publishes at `:2037`), and
  `author` (`project.rs:1927`). They share helpers but not a transaction
  boundary, so "stage everything, then publish last, else roll back" cannot be
  enforced in one place.

- **Errors are generic.** Persistence and serialisation failures in `author`
  surface as `PyRuntimeError` (`project.rs:1984`,`:1995`); identity-persist
  failures are swallowed entirely. The native exception set
  (`rust/src/lib.rs:8-14`, registered `:40-46`) has `TyO3Error`,
  `ProjectClosedError`, `PathResolutionError`, `PositionError`,
  `RevisionEvictedError`, `ConfigError`, `FormatVersionError` — but **no**
  `SidecarWriteError`, `CommitFailed`, or `ReconcileAmbiguous`. So commit failures
  cannot be distinguished from any other internal error (§5.12 violation).

- **The sidecar write primitive is already crash-safe** — `Sidecar::write_atomic`
  (`rust/src/sidecar.rs:76`) writes `<path>.tmp`, `fsync`s, and renames over the
  target. Phase 5 does **not** reinvent this; it makes every commit *route its
  sidecar writes through the staging boundary* so a mid-commit failure leaves no
  half-written durable state, and it stops swallowing the error.

**What Phases 1–4 already put in place (you build on it, don't rebuild it):**

- One native commit produces the id-level `CommitDelta`
  (`CommitDeltaDto`, Phase 3) with the Phase 2 `code_delta` nested — the value
  every write returns and the value Phase 5's `commit()` returns on success.
- The code layer is native and authoritative (Phase 4); the post-commit Python
  graph update is a **pure applier** of `delta.code_delta` and takes no write
  lock. So "update the code layer" (§5.3 step 4) is already an in-lock native
  operation — Phase 5 stages it like the rest.
- Identity is reconciled at open (Phase 1.3) and on every commit (Phase 3), so
  the registry is always populated; Phase 5 only changes *when its persistence is
  committed* relative to publication.

**The fix this phase delivers:**

1. **A single commit entry point** `commit(mutation) -> Result<CommitDelta,
   CommitError>` (5.1) that every write kind funnels through.
2. **Stage-then-publish** (5.2): inside the lock, stage the next generation, the
   analysis change, the next identity registry, the next authored state, and the
   next code layer; compute the `CommitDelta`; write sidecar files to temporaries
   and atomically rename them; and **publish the revision last**. Any failure
   returns a typed error and leaves head, retained generations, registry, authored
   store, and sidecar **all at R−1**, with **no bus delta enqueued**.
3. **Authored ordering fixed** (5.3): `author` becomes stage → persist → publish,
   so a persistence failure rolls the whole commit back (head included).
4. **Typed errors** (5.4): `SidecarWriteError`, `CommitFailed`,
   `ReconcileAmbiguous` join the native exception set and are surfaced through
   `tyo3.exceptions`.
5. **A native test-only fault-injection seam** so the Phase 0 rollback test can
   force a failure at a named stage and assert rollback — without filesystem
   permission tricks (§0.4).

### Why "publish last" is the whole game

§5.3 is explicit and load-bearing: **publication MUST be the last in-lock step**,
so a reader that observes revision R also observes the fully-updated, fully-
persisted layers for R. Today publication is the *first* step, which is why a
later persistence failure produces a torn publish. The entire Phase 5 design is:
compute everything into *staging* structures that do not touch published state,
do all the fallible work (serialise, `write_atomic`) against staging, and only
when all of it has succeeded perform the **single** `head.system.publish(...)`
swap. After that swap there is nothing left that can fail. Before it, every
failure path simply drops the staging structures and returns — head never moved.

### Files in scope

| File | Role in Phase 5 |
|---|---|
| `rust/src/project.rs` | **edit (core)** — add `commit(mutation)`; introduce the staging structure; reorder publish to last; funnel `edit`/`edit_many`/`edit_virtual`/`sync_path`/`discard`/`sync_all`/`author`/`poll_changes` through it; add the fault-injection seam (5.1, 5.2, 5.3, 5.5) |
| `rust/src/identity.rs` | **edit** — stop persisting inside reconciliation; return the serialised-but-not-yet-written registry bytes (or a stage) to the commit so persistence is a staged, fallible, **propagated** step (5.2, 5.4) |
| `rust/src/sidecar.rs` | reference / small edit — `write_atomic` (`:76`) stays the durable primitive; optionally add a staged "prepare temp then commit rename" split if you stage renames (5.2) |
| `rust/src/lib.rs` | **edit** — define + register `SidecarWriteError`, `CommitFailed`, `ReconcileAmbiguous` (5.4) |
| `src/tyo3/exceptions.py` | **edit** — import + re-export the three new native exceptions (5.4) |
| `src/tyo3/tests/test_final_transaction_rollback.py` | the Phase 0 test that must go green this phase (do **not** weaken it); arms `session._inner._fault_inject(stage)` |
| `src/tyo3/tests/test_write_path.py`, `test_gate6_authored.py` | the regression surface that must stay green over the restructured commit |

---

## 2. Working rules for this phase (non-negotiable)

1. **Write/await the failing test first, then implement until green.** Phase 0
   shipped `test_final_transaction_rollback.py` under a module-level
   `xfail(strict=True)` (`test_final_transaction_rollback.py:62-66`) because the
   native fault-injection seam and staged rollback do not exist yet. Your job is
   to add the seam and the rollback, which flips the strict-xfail to a *failure*
   and forces you to remove the marker.
2. **Publication is the last in-lock step.** After the staging design, there must
   be exactly **one** `head.system.publish(...)` per commit and it must come
   *after* every fallible step (serialise, `write_atomic`, code-layer update,
   delta computation). Grep for `system.publish` and confirm no write path
   publishes before its persistence (§5.3 [INV]).
3. **A failed commit advances nothing.** On any in-lock failure: head, retained
   generations, the identity registry, the authored store, and the sidecar are all
   exactly as before, and **no bus delta is enqueued**. The rollback test asserts
   `s.head == before_head`, `s.id_for(...) == before_id`,
   `s.authored(...).status == "absent"`, and `sub.poll(timeout=0.0) is None` —
   honour every one.
4. **Never swallow a commit error.** Replace the `log::error!(...)`-and-continue on
   identity persistence (`project.rs:1414`) with a **propagated typed error**. A
   deliberate non-failure (e.g. an optional cache that is allowed to be absent) may
   be tolerated *only* with a one-line comment saying why (§5.12); a sidecar write
   failure is never tolerated.
5. **No Python-side write lock is needed — and none is added.** Once publication is
   the commit tail under the native `Mutex` (`project.rs:164`), the whole
   transaction is serialised in Rust. Do not introduce a Python lock to "help";
   the point of this phase is that the native commit is the one boundary.
6. **The fault seam is test-only and explicit.** It arms a **one-shot** fault at a
   named stage and is consumed by the next commit. It must not change behaviour
   when unarmed, must be `#[cfg]`-guarded or clearly gated so it cannot fire in
   normal use, and must inject the failure at the real stage (after staging that
   stage's work, before publish) so the test proves *rollback semantics*, not OS
   behaviour (§0.4).
7. **Don't reshape the delta or the bus.** Phase 5 changes *when and how* a commit
   becomes durable+published; it does **not** change the `CommitDelta` shape
   (Phase 3) or unify the Python post-commit path / bus (that is Phase 6). The
   success return value is the same `CommitDelta` callers already get.
8. **Milestone gate after the phase** (both must pass, both via devenv):
   ```bash
   devenv shell -- pytest -q --no-cov
   devenv shell -- cargo test --manifest-path rust/Cargo.toml
   ```

---

## 3. Pre-flight — establish the red baseline

Confirm the starting state before changing anything, so you can prove your work
moved the needle.

```bash
# 1. Phases 1–4 are landed and the Rust core is green.
devenv shell -- cargo test --manifest-path rust/Cargo.toml \
  content overlay project entity identity code_layer code_delta commit_delta authored sidecar

# 2. The rollback contract test is RED in exactly the expected way: the whole
#    module is xfail(strict=True) and each case calls pytest.fail(...) from
#    _arm_fault because no native _fault_inject seam exists yet.
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_final_transaction_rollback.py -q --no-cov -rA

# 3. The write-path + authored suites are green — your regression target.
devenv shell -- pytest src/tyo3/tests/test_write_path.py src/tyo3/tests/test_gate6_authored.py -q --no-cov
```

Record the output. Two North Stars: (a) the rollback test is a strict-xfail today
because the fault seam is absent and the commit cannot roll back; by the end of
Phase 5 all three cases are real green assertions and the marker is removed.
(b) `test_write_path` / `test_gate6_authored` stay green throughout — the commit
restructuring must be behaviour-preserving on the success path.

---

## 4. Step-by-step implementation

Do the steps in order. Each step lists **what**, **why**, **where**, and a
**verify** command (devenv-prefixed). Commit at the natural breakpoints noted.

> **Sequencing matters and is deliberate.** Add the typed errors (5.4) first so
> the commit code can return them as you write it. Then introduce the staging
> structure and reorder publish-to-last for the content/identity/code path (5.2),
> funnelling the write kinds through one `commit()` (5.1). Then fix the authored
> ordering on the same staging machinery (5.3). Add the fault seam (5.5) last,
> wired to the staging boundaries you just built, and use it to turn the rollback
> test green.

### Step 5.4 (do first) — Typed errors

**What.** Add three native exceptions and surface them through `tyo3.exceptions`:
`SidecarWriteError`, `CommitFailed`, `ReconcileAmbiguous` (alongside the existing
`RevisionEvicted`, `ProjectClosed`, and format-version errors). Do **not** map
commit failures onto a generic internal error.

**Why.** §5.12. The rollback test catches `tyo3.exceptions.TyO3Error`
(`test_final_transaction_rollback.py:28`,`:76`), so each new error must subclass
`TyO3Error`. Distinct types let callers (and Phase 6) tell a sidecar write failure
from an ambiguous reconcile from a generic commit failure.

**Where / how.**
- In `rust/src/lib.rs`, mirror the existing `create_exception!` lines
  (`lib.rs:8-14`) — each new error is a subclass of `TyO3Error`:
  ```rust
  create_exception!(tyo3._native_impl, SidecarWriteError, TyO3Error);
  create_exception!(tyo3._native_impl, CommitFailed, TyO3Error);
  create_exception!(tyo3._native_impl, ReconcileAmbiguous, TyO3Error);
  ```
  and register them in the module init next to the others (`lib.rs:40-46`):
  ```rust
  m.add("SidecarWriteError", m.py().get_type::<SidecarWriteError>())?;
  m.add("CommitFailed", m.py().get_type::<CommitFailed>())?;
  m.add("ReconcileAmbiguous", m.py().get_type::<ReconcileAmbiguous>())?;
  ```
- Import them into `project.rs` alongside the existing
  `use crate::{ConfigError as PyConfigError, …}` (`project.rs:10`).
- In `src/tyo3/exceptions.py`, add the three names to the native import block
  (`exceptions.py:11-19`), the `ImportError` fallback stubs (`:20-41`), and
  `__all__` (`:73-85`). Follow the existing pattern exactly — the native classes
  are canonical; Python only re-exports.

**Define the semantics now (you wire them in 5.2/5.3):**
- `SidecarWriteError` — a `write_atomic` (or temp/rename) failure while persisting
  identity or an authored record. Carries the path and the underlying IO error.
- `CommitFailed` — any other in-lock failure that forced a rollback (engine apply,
  code-layer staging, delta computation, or a fired fault at a non-sidecar stage).
- `ReconcileAmbiguous` — reconciliation could not bind deterministically (the
  §5.5 rule-3 ambiguity); reserved here, raised where reconciliation already
  detects ambiguity. If reconciliation has no ambiguous path yet, register the
  type and leave a `// raised once structural reconciliation can be ambiguous`
  note rather than forcing a call site.

**Verify.**
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml
devenv shell -- build
devenv shell -- python -c "import tyo3.exceptions as e; print(e.SidecarWriteError, e.CommitFailed, e.ReconcileAmbiguous)"
```

> Commit here: `feat(errors): typed SidecarWriteError / CommitFailed / ReconcileAmbiguous`.

---

### Step 5.2 — Stage all state, publish last, roll back on any failure

This is the core of the phase. It has two moves: **(a)** introduce a `CommitStage`
that holds the *next* state without touching published state, and route the
fallible work (serialise, `write_atomic`, engine apply, code-layer update, delta
compute) against it; **(b)** make `head.system.publish(...)` the **single, last**
in-lock step, with every earlier failure returning a typed error and dropping the
stage.

**Why.** §5.3 [INV] (steps 1–7 under one lock; publication last; no torn publish;
a failure leaves committed state at R−1, *including durable side effects*).

**Where / how.**

**(a) Capture the rollback baseline and build the stage.** At the top of the
commit body, while holding the lock, capture everything a rollback must restore —
*before* mutating anything:
```rust
struct CommitStage {
    // the prior published generation/revision to restore on failure
    prior_capture: GenerationHandle,     // head.store.capture() BEFORE mutation
    prior_revision: Revision,
    // staged next-state, not yet published / not yet swapped in:
    next_registry_bytes: Vec<u8>,        // serialised, ready to write_atomic
    next_authored: Option<AuthoredStore>,// staged COW store (author path)
    authored_writes: Vec<(PathBuf, Vec<u8>)>, // (record_path, bytes) to persist
    code_delta: dto::CodeDeltaDto,       // Phase 2 produced delta (for the result)
    delta: dto::CommitDeltaDto,          // Phase 3 id-level result
    // …whatever else the commit must restore or hand back
}
```
The exact shape depends on how Phases 1–3 store head state; the principle is:
**no field of `head` that is observable to a reader is mutated until the stage is
fully built and all fallible work has succeeded.** Today the commit mutates
`head.store` (`apply_batch`/`bump_revision`), `head.registry` (rebind),
`head.authored`, and `head.code_layer` *in place and early*. Phase 5 moves those
mutations behind the stage so they can be abandoned.

Practically, you have two implementation strategies — pick the one that fits the
existing borrow structure and **write down which you chose**:

- **Strategy A — clone-and-swap (simplest, recommended).** Compute the next
  generation, next registry, next authored store, and next code layer into local
  variables (cloning the current head pieces as needed; the content `Generation`
  is `Arc`-shared so this is cheap, §2 *Generation*). Do **all** fallible work
  (serialise registry, `write_atomic` identity, `write_atomic` authored records,
  engine `apply_changes`, `produce_code_delta`, build `CommitDelta`) against these
  locals. Only at the very end, assign them back into `head.*` and call
  `head.system.publish(next_capture)`. On any failure, simply `return Err(...)` —
  the locals are dropped and `head.*` was never touched.

  This makes rollback *automatic*: there is nothing to undo because nothing was
  done to `head` until the irrevocable tail. The cost is cloning the registry /
  authored map per commit; both are small and (for the authored store) already
  copy-on-write today (`project.rs:1967`).

- **Strategy B — mutate-then-restore (only if A is infeasible).** Snapshot the
  fields you mutate (`prior_*` above), mutate in place, and on failure restore
  every snapshot before returning. This is error-prone (every early-return must
  restore *all* fields) — use it only if borrow constraints make A impossible, and
  add a test per failure stage proving each field is restored.

**(b) Reorder to publish last.** Rewrite `commit_head` (`project.rs:1435`) so the
sequence becomes (the §5.3 order):
```
acquire lock (already held by the PyO3 method)
  1. stage next generation         (compute, do not publish)
  2. apply engine change to a staged db / record events  → may fail → CommitFailed
  3. reconcile identity → next registry (in memory)       → may fail → ReconcileAmbiguous
  4. update the code layer → produce code_delta (staged)  → may fail → CommitFailed
  5. compute the CommitDelta (created/changed/…/affected)
  6. serialise + write_atomic the identity registry        → may fail → SidecarWriteError
     serialise + write_atomic any authored records          → may fail → SidecarWriteError
  7. PUBLISH: assign staged state into head; head.system.publish(next_capture)  ← LAST
release lock
→ (Phase 6) enqueue the delta to the bus, outside the lock
```
Concrete deletions/relocations:
- **Remove the early publish.** `head.system.publish(head.store.capture())` at
  `project.rs:1444` (and the analogues in `sync_path_inner` `:1497`/`:1555`,
  `sync_all` `:2037`, `author` `:1958`) moves to the **end** of the commit. There
  must be exactly one publish per commit, at the tail.
- **Move identity persistence out of `run_identity_reconciliation` and propagate
  its error.** Today persistence lives at `project.rs:1410-1418` and is swallowed.
  Change `run_identity_reconciliation` (`project.rs:1373`) to reconcile and return
  the *serialised registry bytes* (or the registry to serialise) **without
  writing**; the commit does the `write_atomic` as staged step 6 and returns
  `SidecarWriteError` on failure:
  ```rust
  let identity_path = head.sidecar.identity_db_path();
  head.sidecar.write_atomic(&identity_path, &next_registry_bytes)
      .map_err(|e| SidecarWriteError::new_err(
          format!("failed to persist identity registry at {identity_path:?}: {e}")))?;
  ```
  The `?` short-circuits *before* publish, so head never advances on a persist
  failure — exactly what `test_identity_persist_failure_rolls_back` asserts.
- **Engine apply / code-layer failures → `CommitFailed`.** If `apply_changes`
  (`project.rs:1446`) or the Phase 2 `produce_code_delta` can fail, surface it as
  `CommitFailed` *before* publish. `test_code_layer_failure_publishes_no_partial_
  revision` arms a fault at the `code_layer` stage and asserts head does not move.

**Verify.**
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml project identity sidecar
devenv shell -- build
# Success path unchanged:
devenv shell -- pytest src/tyo3/tests/test_write_path.py src/tyo3/tests/test_mvcc_snapshots.py -q --no-cov
```
Add a Rust unit test that forces a `write_atomic` failure on identity (point the
sidecar at an un-writable staged path, or use the 5.5 seam directly) and asserts
`head.store.revision()` is unchanged and the registry in memory still maps the old
hash. The Python rollback assertions arrive in 5.5.

> Commit here: `refactor(commit): stage next-state and publish last; propagate sidecar errors`.

---

### Step 5.1 — A single commit entry point

**What.** Funnel all write kinds (`edit`, `edit_many`, `edit_virtual`,
`sync_path`, `discard`, `sync_all`, `author`, watcher `poll_changes`) through one
native function `commit(mutation) -> Result<CommitDelta, CommitError>`.

**Why.** §5.3. One transaction boundary means the "stage → … → publish last →
rollback on failure" guarantee from 5.2 holds for *every* write, not just the ones
you remembered to update. It is also what lets Phase 6 attach **one** post-commit
path: every write returns the same `CommitDelta` from the same funnel.

**Where / how.**
- Define a `Mutation` enum describing *what* a write does, decoupled from *how* it
  commits:
  ```rust
  enum Mutation {
      Overlay { changes: Vec<content::Change> },   // edit / edit_many / edit_virtual
      SyncPath { abs: SystemPathBuf },             // sync_path / discard
      SyncAll,                                      // full rescan
      Author { layer: String, id: String, value: serde_json::Value },
      Poll { events: Vec<ChangeEvent> },           // watcher fold
  }
  ```
- `commit(head: &mut HeadState, mutation: Mutation) -> Result<dto::CommitDeltaDto,
  PyErr>` does the staged transaction from 5.2: it interprets the `Mutation` into
  staged content changes + analysis events, runs steps 1–7, and returns the
  `CommitDelta` on success or a typed error on failure. The existing per-kind
  helpers (`commit_head`, `sync_path_inner`, the watcher fold) become the *bodies
  of the `Mutation` match arms* inside `commit`, sharing the one staging + publish
  tail.
- The PyO3 methods become thin: parse arguments, build a `Mutation`, call
  `commit`, `pythonize` the result or convert the error. For example `edit`
  (`project.rs:1834`) builds `Mutation::Overlay { changes }` and calls `commit`;
  `author` (`project.rs:1927`) builds `Mutation::Author { … }` (its ordering fix
  is 5.3). This matches the Phase 11 intent ("PyO3 methods stay thin — parse
  arguments, call core logic, convert errors") without yet splitting files.
- **`poll_changes` (`project.rs:2135`) routes through `commit` too.** The watcher
  fold becomes `Mutation::Poll { events }`; an empty event set still returns
  `None`/no-op (the existing `:1548` "no events → no revision" behaviour) — model
  that as `commit` returning a sentinel the method maps to `None`, *not* as a
  published empty revision.

**Verify.**
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml project
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_write_path.py src/tyo3/tests/test_gate7_watcher.py -q --no-cov
```
All write kinds still produce the same `CommitDelta` they did after Phase 3; the
only observable change is that a failure now rolls back instead of tearing.

> Commit here: `refactor(commit): single native commit(mutation) funnel for all write kinds`.

---

### Step 5.3 — Fix the authored-write ordering

**What.** The authored write currently publishes its revision (`project.rs:1958`)
**before** persisting the record (`:1989`) and rolls back only its in-memory map
(`:1994`). Re-order it to **stage → persist → publish**, so a persistence failure
rolls the whole commit back (head included).

**Why.** §5.3 / Phase 5.3. `test_authored_persist_failure_rolls_back`
(`test_final_transaction_rollback.py:87`) asserts that on a forced authored-persist
failure, `s.head == before_head`, the authored store is still `absent`, and no bus
delta is enqueued. Today head has already advanced by the time persistence runs.

**Where / how.** Re-express `author` as a `Mutation::Author` arm of `commit`
(5.1), reusing the staging tail (5.2):
1. **Validate early** (unchanged): layer is a declared authored layer
   (`project.rs:1938`), value JSON parses (`:1943`), id exists in the registry
   (`:1949`). These are pre-stage checks; they may return their existing
   `ConfigError`/`ValueError` before any staging.
2. **Stage** the copy-on-write authored store into a local (`next_authored`), and
   build the `(record_path, bytes)` to persist — **do not** assign
   `head.authored` yet, and **do not** bump/publish the revision yet. Serialise
   the `AuthoredRecordDoc` (`project.rs:1972-1978`) into the stage; a serialise
   failure returns `CommitFailed` (or `SidecarWriteError` if you classify it as a
   persistence-prep failure) *before* any state moves.
3. **Persist** via `write_atomic` (`project.rs:1989`) as staged step 6; on failure
   return `SidecarWriteError` — **no** revision was published, so there is nothing
   to roll back beyond dropping the stage.
4. **Publish last**: assign `head.authored = next_authored`, bump + capture +
   `head.system.publish(...)`, and build the `CommitDelta` (the authored delta
   carries `authored_ids`, Phase 3). The empty-content revision bump that makes an
   authored edit a real retained revision (`project.rs:1957`) moves into this tail.

Delete the in-memory-only rollback (`head.authored = prior` at `:1983`,`:1994`):
with stage-then-publish there is no published state to restore, so the `prior`
dance is obsolete.

**Verify.**
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml authored project
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_gate6_authored.py -q --no-cov
```
The authored success path (`test_gate6_authored`) must stay green; the rollback
case turns green in 5.5.

> Commit here: `fix(authored): stage → persist → publish; roll the whole commit back on failure`.

---

### Step 5.5 — The native fault-injection seam (turns the rollback test green)

**What.** Add a test-only native method that arms a **one-shot** fault at a named
commit stage, consumed by the next commit, so the Phase 0 rollback test can force a
failure *after* a stage's work and *before* publish, and assert rollback.

**Why.** §0.4 / the test's docstring (`test_final_transaction_rollback.py:14-17`):
the seam must be a native test-only fault injector, **not** a filesystem permission
trick, because the goal is to prove *rollback semantics*, not OS behaviour. The
test discovers the seam by attribute name (`test_final_transaction_rollback.py
:51`): it tries `_fault_inject`, then `fault_inject`, then `_inject_commit_fault`
on `session._inner`. Provide **`_fault_inject(stage: str)`** (the first it tries).

**Where / how.**
- Add a small armed-fault cell to `PyTyProject` (or to `HeadState`), e.g.
  `armed_fault: Mutex<Option<String>>` (or a `Cell`/field reset each commit). Gate
  the whole mechanism so it is unmistakably test-only — either `#[cfg(test)]` is
  *not* sufficient here (the test runs against the built extension, not a cargo
  test), so use a clearly-named, clearly-documented field that is inert unless
  armed, and consider compiling the arming method only when a `fault-injection`
  cargo feature is on if you want it stripped from release builds. **At minimum,
  document that it is a test seam and that an unarmed cell is a no-op.**
- The PyO3 method:
  ```rust
  /// TEST-ONLY: arm a one-shot commit fault at `stage`. The next commit raises a
  /// typed error after staging `stage`'s work and before publish, then rolls back.
  fn _fault_inject(&self, stage: &str) -> PyResult<()> {
      *self.armed_fault.lock().unwrap() = Some(stage.to_string());
      Ok(())
  }
  ```
- **Fire the fault at the real stage boundary**, inside `commit`, *after* that
  stage's staged work and *before* publish. Take-and-clear the cell so it is
  one-shot:
  ```rust
  fn check_fault(head, stage: &str) -> Result<(), PyErr> {
      if head.take_armed_fault_if(stage) {        // matches & clears
          return Err(match stage {
              "identity_persist" => SidecarWriteError::new_err("injected: identity persist"),
              "authored_persist" => SidecarWriteError::new_err("injected: authored persist"),
              "code_layer"       => CommitFailed::new_err("injected: code layer"),
              other              => CommitFailed::new_err(format!("injected: {other}")),
          });
      }
      Ok(())
  }
  ```
  Wire `check_fault(head, "code_layer")?` right after staging the code layer /
  producing the code delta (5.2 step 4); `check_fault(head, "identity_persist")?`
  right at the identity `write_atomic` step (step 6) — either before the real write
  or by making the seam force the write to fail; `check_fault(head,
  "authored_persist")?` at the authored `write_atomic` step (5.3 step 3). All three
  fire **before** the single publish, so the `?` rolls back via the 5.2 machinery.
  The stage names match the test's `_FAULT_STAGES`
  (`test_final_transaction_rollback.py:30`).
- **Keep it one-shot and isolated.** Arming applies to exactly the next commit;
  clear it whether or not it fires this commit's matching stage, so a stale arm
  cannot leak into an unrelated later write.

**Verify (the phase's Python acceptance).**
```bash
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_final_transaction_rollback.py -q --no-cov -rA
```
Remove the module-level `@pytest.mark.xfail`
(`test_final_transaction_rollback.py:62-66`) only once all three cases pass on
their own (an xfail that starts passing with the marker still on is itself a
failure under `xfail-strict`). Each case must show: the raised error is a
`TyO3Error` subclass, `s.head` unchanged, the registry/authored store unchanged,
and `sub.poll(timeout=0.0) is None`.

> Commit here: `feat(commit): test-only one-shot fault seam; rollback contract green`.

---

## 5. Acceptance — the Phase 5 gate

Run exactly what the plan's Phase 5 "Acceptance" lists, all via devenv:

```bash
devenv shell -- build   # ensure pytest imports the freshly built extension

devenv shell -- pytest \
  src/tyo3/tests/test_final_transaction_rollback.py \
  src/tyo3/tests/test_write_path.py \
  src/tyo3/tests/test_gate6_authored.py \
  -q --no-cov -rA

devenv shell -- cargo test --manifest-path rust/Cargo.toml project authored sidecar
```

Then the full milestone gate (run the suites in the background; ~10–15 min):

```bash
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml
```

**Exit criteria (all must hold):**
- **Every write funnels through one native `commit(mutation)`** — `edit`,
  `edit_many`, `edit_virtual`, `sync_path`, `discard`, `sync_all`, `author`, and
  watcher `poll_changes` share the one staged transaction.
- **Publication is the last in-lock step** — exactly one `head.system.publish(...)`
  per commit, after every fallible step. No write path publishes before its
  persistence.
- **A failed write advances nothing** — head, retained generations, the identity
  registry, the authored store, and the sidecar are all unchanged, and **no bus
  delta is enqueued**. Proven by `test_final_transaction_rollback.py` with its
  `xfail` marker removed (all three stages).
- **No sidecar error is swallowed** — the identity-persist `log::error!` is gone;
  persistence failures propagate as `SidecarWriteError`.
- **The authored write is stage → persist → publish** — a persistence failure rolls
  the whole commit back, head included; the in-memory-only `prior` rollback is
  deleted.
- **Typed errors exist and are surfaced** — `SidecarWriteError`, `CommitFailed`,
  `ReconcileAmbiguous` are registered natively and re-exported from
  `tyo3.exceptions`; commit failures are not mapped onto a generic internal error.
- The fault seam is **test-only, one-shot, inert when unarmed**.
- Both full suites stay green; `test_write_path` / `test_gate6_authored` behaviour
  is preserved over the restructured commit.

---

## 6. Pitfalls specific to this phase

- **Forgetting `devenv shell -- build` before the rollback pytest.** Phase 5 adds
  a native seam and native exceptions; a green `cargo test` does not refresh the
  compiled extension. If `_arm_fault` keeps calling `pytest.fail(...)` "no matter
  what you fix," you skipped the rebuild and the test is probing the *old*
  extension.
- **Publishing before persisting (the cardinal Phase 5 sin).** The whole point is
  that `head.system.publish(...)` is the *tail*. If any `system.publish` survives
  ahead of a `write_atomic` — in `commit_head`, `sync_path_inner`, `sync_all`, or
  `author` — you still have a torn publish. Grep for `system.publish` and confirm
  one per commit, last.
- **Swallowing the identity-persist error.** Replacing
  `log::error!(...)`-and-continue (`project.rs:1414`) with a propagated
  `SidecarWriteError` is the single most important behavioural change; if it is
  still logged-and-ignored, `test_identity_persist_failure_rolls_back` stays red.
- **Rolling back only the in-memory authored map.** Restoring `head.authored =
  prior` without rolling back the published revision (today's bug at
  `project.rs:1994`) is exactly what 5.3 fixes. With stage-then-publish there is no
  published state to restore — delete the `prior` dance, do not "improve" it.
- **Strategy-B restore gaps.** If you mutate-then-restore (5.2 Strategy B) instead
  of clone-and-swap, every early `return Err` must restore *every* mutated field
  (`store`, `registry`, `authored`, `code_layer`). A missed field is a silent
  partial rollback the success-path tests will not catch — add a per-stage Rust
  test. Prefer Strategy A.
- **A fault that fires after publish.** The seam must inject *before* the single
  publish, or it proves nothing about rollback (the revision is already public).
  Wire `check_fault` at the staged step, ahead of the publish tail.
- **A leaky one-shot fault.** If arming is not cleared when its stage is not
  reached this commit, a later unrelated write fires the stale fault. Take-and-
  clear at the boundary; arm applies to the next commit only.
- **An empty `poll_changes` publishing a no-op revision.** The watcher fold must
  still return `None` / not bump the revision when there are no events
  (`project.rs:1548` behaviour). Model "no events" as `commit` not publishing, not
  as a published empty revision.
- **Adding a Python write lock.** Once publication is the native commit tail, the
  native `Mutex` is the only writer boundary needed (§5.3). Do not reintroduce a
  Python-side lock; that re-creates the split-ownership this refactor removes.
- **Reshaping the delta or touching the bus.** Phase 5 changes commit *durability
  and ordering*, not the `CommitDelta` shape (Phase 3) or the Python post-commit
  path / bus (Phase 6). Keep the success return value identical.
- **Cloning cost panic.** Strategy A clones the registry/authored map per commit.
  The content `Generation` is `Arc`-shared (cheap); the registry/authored maps are
  small. Do not "optimise" this by sharing mutable state back into `head` before
  the publish tail — that reopens the torn-publish window.

---

## 7. Suggested commit sequence for the phase

1. `feat(errors): typed SidecarWriteError / CommitFailed / ReconcileAmbiguous` (5.4)
2. `refactor(commit): stage next-state and publish last; propagate sidecar errors` (5.2)
3. `refactor(commit): single native commit(mutation) funnel for all write kinds` (5.1)
4. `fix(authored): stage → persist → publish; roll the whole commit back on failure` (5.3)
5. `feat(commit): test-only one-shot fault seam; rollback contract green` (5.5)

This maps to the plan's single commit
`refactor(commit): single native commit() with staging + rollback`; squash on
landing if the series is preferred as one reviewed commit.

Every commit passes its focused `cargo`/`pytest` slice; the last one passes the
rollback contract test and both full suites (the milestone gate). Remember: **all
of it through `devenv shell --`.**

---

## 8. What Phase 5 deliberately leaves for later (so you don't over-reach)

- **One unified Python post-commit path + non-blocking bus** is **Phase 6**. Phase
  5 makes `commit()` return one `CommitDelta` from one funnel and guarantees
  publish-last durability; Phase 6 collapses the per-method Python post-commit
  steps into one `_after_commit` hook, makes *every* path publish (including
  `discard`), and removes the writer-blocking overflow policy. Phase 5 must not
  block the writer on bus delivery — but the bus *redesign* is Phase 6.
- **Id-level derived invalidation** is **Phase 7** — it consumes the
  `created_ids`/`changed_ids`/`deleted_ids` the Phase 3 delta already carries.
  Phase 5 leaves derived scheduling where it is.
- **Read-surface / convenience-API lifetime fixes** are **Phase 8**; **AST-canonical
  hashing** is **Phase 9**; **single config source** is **Phase 10**. None of them
  belong in the commit-transaction restructuring.
- **Splitting `project.rs` into open / commit / snapshot / watch / authored
  modules** is **Phase 11**. Phase 5 makes the PyO3 write methods *thin* (parse →
  `Mutation` → `commit` → convert) and concentrates the transaction in one
  `commit` funnel, which is exactly the seam Phase 11 will cut along — but do **not**
  move files yet. Keep the change behavioural, not structural.

Keeping these out of Phase 5 isolates the high-value, high-risk change — making
every write one staged, publish-last, fully-rollback-able transaction with the
sidecar as a participant — behind the Phase 0 rollback contract test, and keeps
both suites green at every commit.
