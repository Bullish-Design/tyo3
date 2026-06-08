# Phase 9 (V2) — Async precision refinement layer

> Execution guide for **Phase 9 of `REFINED_IMPLEMENTATION_PLAN_V2.md`**. Read
> Concept V2 §5.2–§5.4 (container-granular coverage, the residual miss class,
> the async refinement) and V1 §3 (the salsa constraint) once before starting.
>
> **This is a new capability with no V1 antecedent.** It is the optional layer
> that sharpens the synchronous, container-granular `affected` set to
> method-level precision — asynchronously, in Python, over frozen snapshots, with
> graceful degradation. It exists **only** to improve precision; the system is
> fully correct without it.
>
> **Depends on Phases 6–8.** The synchronous coarse `affected` must be a sound
> superset (Phase 6), the bus must have the refinement channel (Phase 7), and
> derived invalidation must be id-level (Phase 8) so it can consume a refinement.

---

## 0. Dev environment

All commands via `devenv shell --`. Mostly **Python** (a new worker +
config plumbing); the `precision` config knob's validation is a small native
touch (`rust/src/config.rs`) — **rebuild after that** (`devenv shell -- build`).
Suites are slow.

---

## 1. What Phase 9 changes, and why

The synchronous `affected` set is **container-granular**: editing `Widget.draw`
marks every referrer of `Widget` affected, even ones that only call
`Widget.serialize`. That is sound (never-miss) but imprecise. Phase 9 adds a
background worker that **narrows** the coarse set to the entities that actually
depend on the changed *members*, and publishes the result as an
`AffectedRefinement` on the bus's refinement channel (Phase 7).

**Why async, why Python, why safe (Concept V2 §5.4):**
- **Async** — the per-occurrence member-access resolution (`goto_type_definition`
  etc.) is the expensive part; keeping it off the commit hot path means the
  writer never pays for precision.
- **Python** — it owns the bus and graph projection; precision becomes a *purely
  additive* layer that can be enabled/disabled/removed without touching Rust.
- **Safe by construction** — the synchronous set is a sound superset, so narrowing
  can never introduce a miss. If the worker lags, crashes, or never runs, the
  coarse set remains correct. **Graceful degradation** is the headline property.

### Files in scope

| File | Role |
|---|---|
| `src/tyo3/precision/refiner.py` (new) | **core** — the background worker: snapshot → member-access resolution → narrowed affected → publish refinement (9.2, 9.3) |
| `src/tyo3/session.py` | **edit** — start/stop the refiner per the config knob; hand it committed revisions (9.1, 9.2) |
| `src/tyo3/bus/refinement.py` | **reference** — the channel from Phase 7 (9.2) |
| `rust/src/config.rs` | **edit (small)** — validate `code_graph.precision` / `refinement` (9.1) |
| `src/tyo3/config.py` | **edit** — surface the knobs (9.1) |
| `src/tyo3/tests/test_precision_refinement.py` (new) | narrowing correctness; worker-crash graceful degradation; container-mode no-op (9.3) |

---

## 2. Working rules

1. **Never query the live head from the worker.** The worker runs ty queries over
   a **frozen snapshot pinned at the revision it refines** — never the head db
   (V1 §3: a reader sharing the live db blocks/cancels the writer). Open the
   snapshot, query, close it in `finally`.
2. **Narrow only (default).** The refinement is a subset of the coarse affected
   set. Never publish an `added` set in narrowing mode.
3. **Graceful degradation is a test, not a hope.** A test must kill/stall the
   worker and assert the coarse set is still correct and delivered.
4. **`precision=container` ⇒ the worker does not run.** Zero overhead; all suites
   pass unchanged.
5. **Refinements ride the Phase-7 channel**, separate from the primary
   revision-ordered delta stream.

---

## 3. Step-by-step

### Step 9.1 — Config knobs

**Where / how.** Add `code_graph.precision ∈ {container, method}` (default
`container`) and `code_graph.refinement ∈ {sync, async}` (default `async`) to the
native config (`rust/src/config.rs` validate + defaults) and surface them in
`src/tyo3/config.py`. Reject unknown values with a typed `ConfigError` at open
(consistent with the coordination-config validation pattern). **Rebuild** after
the Rust change.

**Verify.**
```bash
devenv shell -- build
devenv shell -- cargo test --manifest-path rust/Cargo.toml config
```

### Step 9.2 — The refiner worker

**Where / how.** `src/tyo3/precision/refiner.py`:
- A daemon worker (thread or task) fed committed revisions (revision + the coarse
  `changed_ids`/`affected_ids`) from `_after_commit` when `precision=method`.
- For each revision R:
  1. `with session.snapshot(revision=R) as snap:` (frozen, isolated).
  2. For each changed *member* id, resolve which consumers actually reference that
     member: run member-access resolution over the snapshot
     (`find_references` / occurrence + `goto_type_definition` to confirm the
     inferred receiver type maps to the changed member's container).
  3. Compute `narrowed = {ids in coarse affected that truly depend on a changed
     member} ∪ changed_ids`. (Always retain the directly-changed ids.)
  4. `bus.publish_refinement(AffectedRefinement(revision=R, narrowed=narrowed))`.
- If `refinement=sync` (opt-in, for tests or small projects), run the same
  computation inline in `_after_commit` before returning — but this re-introduces
  the per-occurrence cost on the writer, so it is **not** the default.

**Verify.**
```bash
devenv shell -- pytest src/tyo3/tests/test_precision_refinement.py::test_narrows_to_member_users -q --no-cov -rA
```
Assert: editing `Widget.draw` yields a coarse affected of all Widget referrers,
then a refinement narrowing it to `draw` users only, at the same revision R.

### Step 9.3 — Graceful degradation + container-mode no-op

**Where / how.** Two tests:
- **graceful degradation:** with `precision=method`, inject a worker failure/stall
  (raise inside the refiner, or never schedule it); assert the coarse affected set
  was still delivered and is correct — no miss, no crash of the writer/bus.
- **container no-op:** with `precision=container`, assert the refiner never starts
  and no refinement is ever published; the full suite passes unchanged.

**Verify.**
```bash
devenv shell -- pytest src/tyo3/tests/test_precision_refinement.py -q --no-cov -rA
```

### Step 9.4 — (Optional) expansion mode

**Where / how.** Behind `precision=method` + an explicit `coverage=expansive`
sub-flag (off by default), the worker may additionally discover **non-nominal**
dependents (duck-typed/`Any`/protocol flows — Concept V2 §5.3) and publish them as
`added` ids. Document clearly that this is **eventually-consistent on that miss
class**: between the coarse delivery and the expansion, a non-nominal dependent
has not been notified. Only build this if a use case needs it.

> Commit here: `feat(precision): async container→method affected refinement over snapshots`.

---

## 4. Acceptance

```bash
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_precision_refinement.py -q --no-cov -rA
# container default leaves everything unchanged:
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml
```

**Exit criteria:** with `precision=container` (default) the worker never runs and
all suites pass; with `precision=method` a refinement narrows `Widget.draw`'s
affected from "all Widget referrers" to "draw users" at the same revision; a
worker-crash test proves the coarse set stays correct; refinements ride the
Phase-7 channel without disturbing primary revision ordering.

---

## 5. Pitfalls

- **Querying the live head db from the worker.** Blocks/cancels the writer
  (V1 §3). Always use a frozen snapshot pinned at R.
- **Forgetting to retain `changed_ids` in the narrowed set.** Narrowing must never
  drop the directly-changed entities.
- **Letting a worker failure surface as a miss.** The coarse set is the safety
  net; never gate primary delivery on the refinement.
- **Defaulting to `sync` or `method`.** Defaults are `container` + `async` so the
  writer pays nothing unless precision is explicitly requested.
- **Treating expansion as sound.** It is eventually-consistent on a real miss
  class — document it, don't present it as complete.

---

## 6. What Phase 9 leaves for later

- **Single config source** — Phase 12 surfaces these knobs from the one validated
  native config (this phase adds the validation + a Python facade read).
- **Read-surface lifetime hygiene** for the snapshots the refiner opens — reviewed
  alongside Phase 11.
