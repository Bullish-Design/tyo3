# Phase 7 — Repair derived layers

> A step-by-step execution guide for **Phase 7** of
> `REFINED_IMPLEMENTATION_PLAN.md`. Read that plan's "Phase 7" section and §5.7
> (the derived-layer cache + invalidation), §5.8 (the derivation DAG), §5.9
> (cross-layer consistency), and §5.12 (the error model / "fail loudly") of
> `REFINED_IMPLEMENTATION_CONCEPT.md` once before starting — this guide assumes
> that vocabulary (the `ContentHash`-keyed artifact cache, the
> `(input_hash, generator_version)` store key, id-level invalidation, read-time
> staleness, the per-layer serving policy, typed store errors) and turns it into
> concrete edits against the code as it exists today.
>
> **Phase 7 depends on Phases 1–6.** Phase 3 reshaped the public write result
> into the id-level `CommitDelta` (`created_ids / changed_ids / deleted_ids /
> moved[{id,…}] / authored_ids / affected_ids / code_delta / touched_files /
> rescan`) and mirrored it as a Python model (`src/tyo3/models/delta.py`). Phase 4
> made the native code delta authoritative and turned `CodeGraph` into a pure
> applier. Phase 5 funnelled every write through one native `commit()` that
> publishes the revision last. Phase 6 collapsed the per-method Python post-commit
> tails into **one** `_after_commit(delta)` hook that calls
> `_schedule_derived(delta)` — which today points at the existing
> `_invalidate_derived`. **Phase 7 fixes what that hook calls into:** the derived
> invalidation that is currently *inert* (fed path-shaped data, so nothing is ever
> invalidated), the recompute that never actually runs (the lazy queue is a
> dead-end), the eager pass that **leaks a snapshot it never closes**, and the
> stores that **swallow IO/query errors**. If any earlier phase's milestone gate
> is not green, stop and finish it first.

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
  devenv shell -- pytest src/tyo3/tests/test_final_derived_contract.py -q --no-cov
  ```
- Interactive (if you are iterating rapidly):
  ```bash
  devenv shell        # drop into the environment once
  # …then run pytest / the project scripts directly inside it…
  ```

**Phase 7 is pure Python.** It touches `src/tyo3/derive/` and `src/tyo3/stores/`
only — **no Rust**. That means you do **not** need `devenv shell -- build`
between iterations (there is no compiled extension change to refresh). Run it once
at the very end before the final gate if you want zero doubt, but the
fast inner loop is just `pytest`. (Contrast Phases 5/6, which had native touches
and *required* a rebuild — not here.)

The suites are slow. Allow **~10–15 minutes** for a full run; launch full suites
in the background and give them time rather than assuming a hang.

> Throughout the rest of this document, **assume every `pytest`, `ruff`, and
> project-script invocation is prefixed with `devenv shell --`**, even where a
> line is abbreviated for readability.

---

## 1. What Phase 7 changes, and why

**Goal (from the plan):** derived invalidation is precise and id-level, staleness
is honest, eager/lazy recompute leaks no snapshot, and stores report errors
instead of hiding them (§5.7, §5.12).

### The situation today (confirmed in the current code)

This is **defect #5** from the concept (§6.3.5): "Derived invalidation is silently
inert. It is fed the path-shaped `created/changed` values as if they were durable
ids; the per-entity hash lookup raises and is swallowed, so nothing is
invalidated. It also opens a second snapshot it never closes." Concretely:

- **Invalidation is fed path-shaped data.** `TyO3Session._invalidate_derived`
  (`session.py:781`) builds the dirty set as
  ```python
  dirty = set(result.created) | set(result.changed)
  dag.invalidate(self, dirty, set(result.deleted), result.revision)
  ```
  but `result.created` / `result.changed` / `result.deleted` on the `CommitDelta`
  model are the **path-shaped metadata** fields (file-path strings), *not* the
  id-level `created_ids` / `changed_ids` / `deleted_ids` (`models/delta.py:51-55`
  documents them as "metadata (NOT ids)"). So `DerivationDAG.invalidate`
  (`derive/dag.py:117`) iterates *file paths* as if they were `DurableId`s. The
  first thing it does per "id" is `layer.applies_to(_kind_for_id(snap, durable_id))`
  (`dag.py:140`); `_kind_for_id` (`dag.py:290`) does
  `snapshot.graph().symbol(file_path)`, which finds no node, falls into its bare
  `except Exception: pass`, and returns `""`; `applies_to("")` is `False` for a
  layer scoped to `entity_kinds = ["function"]`. **Every entity is skipped, so
  nothing is ever invalidated.** This is the inert-invalidation defect.

- **The recompute never actually runs (the lazy queue is a dead-end).** Default
  config is `recompute = "lazy"`, `serving = "stale"` (`rust/src/config.rs:580-586`).
  On a stale-serving read miss, `Snapshot.derived` (`session.py:1632-1634`) does
  `scheduler.enqueue(L, durable_id, input_hash, lazy=True)` and then serves
  last-good. But the lazy queue (`RecomputeScheduler._lazy`,
  `derive/scheduler.py:47`) is **enqueued and never processed** — there is no
  caller of anything that drains `_lazy`. `process_all` (`scheduler.py:68`) only
  processes the *eager* `_pending` list, and `invalidate` only enqueues eager when
  `layer.recompute == "eager"` (`dag.py:156`). So with the default `lazy`
  recompute, the generator is **never invoked** after a change — the artifact is
  never refreshed. (Even if 7.1 is fixed so the changed id reaches `invalidate`,
  the recompute still would not run.)

- **The eager pass leaks a snapshot.** `DerivationDAG.invalidate` opens one
  snapshot for the invalidation scan and closes it in `finally` (`dag.py:136-162`)
  — good — but then calls
  ```python
  scheduler.process_all(self, session.snapshot())   # dag.py:165
  ```
  which opens a **second** snapshot as an argument and **never closes it**, even
  when `process_all` early-returns because nothing is pending (`scheduler.py:74`).
  Every commit on a project with a derived layer leaks one snapshot. This is what
  `test_eager_recompute_leaks_no_snapshot` catches (it arms
  `ResourceWarning`-as-error and forces `gc.collect()`).

- **Stores swallow errors.** `LanceDbStore` (`stores/lancedb_store.py`) wraps
  every operation in `try/except Exception` and returns `None` / `[]` / silently
  no-ops on `get` (`:69-71`), `put` (`:87-88`), `delete` (`:96-98`), and `nearest`
  (`:114-115`). A backend write that fails looks like a success; a query that
  errors looks like "no results." `FsStore` (`stores/fs.py`) is closer to honest
  but does not *explicitly* distinguish a genuinely-absent file from an IO error
  on an existing file. §5.7 ("a recompute failure leaves the prior artifact
  intact… never a partial artifact") and §5.12 ("recoverable conditions surface as
  typed, catchable errors… library code does not swallow") require these to be
  honest.

- **A stale Gate-5 xfail.** `test_gate5_derived.py::test_self_healing_derived_layer_cache_hit_recompute_reuse`
  (`:15`) is marked `xfail(reason="Gate 5 Step 0: API does not exist yet")` — but
  the API *does* exist now (it was built in Gate 5). The plan (§7.5) requires it to
  be un-skipped and proven: an unrelated edit does **not** recompute; a content
  change recomputes exactly once; a move reuses the artifact; (the broader file
  also covers a `generator_version` bump → new key-space and a failure → last-good
  intact).

### What Phases 1–6 already put in place (you build on it, don't rebuild it)

- One native commit returns one id-level `CommitDelta` with **populated**
  `created_ids` / `changed_ids` / `deleted_ids` (Phase 3). These are exactly the
  inputs §7.1 wants — and they are *direct seeds*, which is what derived
  invalidation iterates (it does **not** iterate the transitive `affected_ids`; see
  the callout below).
- One post-commit hook `_after_commit(delta)` (Phase 6) that calls
  `_schedule_derived(delta)` — the single seam Phase 7 sharpens.
- The read-time resolver `Snapshot.derived` (`session.py:1595`) already resolves
  the artifact by the entity's content hash at the pinned revision and forms the
  `(input_hash, generator_version)` store key. Phase 7 makes its *staleness honest*
  and makes lazy recompute actually run — it does not rewrite the resolver from
  scratch.
- The content-addressed `ArtifactCache` (`derive/cache.py`), the per-layer
  `DerivedLayer` runtime (`derive/layer.py`), and the `RecomputeScheduler`
  (`derive/scheduler.py`) all exist. Phase 7 *wires them correctly*, it does not
  re-architect them.

### The fix this phase delivers

1. **Id-level invalidation inputs (7.1):** feed `invalidate` the
   `created_ids`/`changed_ids` dirty set and the `deleted_ids` deleted set.
2. **Read-time staleness + lazy recompute that runs (7.2):** staleness is a pure
   read-time hash comparison; a lazy recompute actually executes (on read), so a
   changed entity is refreshed and a failure reports `failed` while keeping
   last-good.
3. **One snapshot, closed (7.3):** invalidation and any eager recompute share
   **one** pinned snapshot, closed in `finally`; the second leaked snapshot is
   removed.
4. **Typed store errors (7.4):** `FsStore` returns "missing" only for a genuinely
   absent file and propagates other IO errors; `LanceDbStore` stops swallowing
   query/write errors; a missing optional backend stays the distinct typed
   `StoreBackendUnavailable`.
5. **The self-healing test is un-skipped and the contract turns green (7.5):** the
   Gate-5 self-healing xfail is removed and the five
   `test_final_derived_contract.py` xfails are removed as each contract lands.

### Why "id-level inputs" is the whole game

Everything downstream of `invalidate` is already correct *if it is fed real
durable ids*: `resolve_input` keys on the entity's content hash, the cache is
content-addressed, the binding comparison detects change vs reuse. The single
reason the whole subsystem is inert is that it is fed file-path strings that never
match a node, so the per-id `applies_to` check filters everything out and the
hash lookup is swallowed (`dag.py:143-147`). Fixing the one line in
`_invalidate_derived` (7.1) is what "turns the currently-inert invalidation back
on" (plan §7.1). The rest of Phase 7 makes the now-live path *honest* (recompute
actually runs, snapshots close, stores report errors).

> ### ⚠️ Carried-forward consequence: `affected_ids` is seeds-only — and that is correct for derived invalidation
>
> The in-commit code-layer producer is still deferred (user-confirmed across
> Phases 3/4/5; [[phase4-producer-deferred]]), so `head.code_layer` /
> `reverse_deps` are empty and `delta.affected_ids` is the **seeds only**
> (`changed ∪ deleted`), not the transitive reverse-dep closure. **This does not
> affect Phase 7.** Plan §7.1 is explicit: derived invalidation builds its dirty
> set from `created_ids` + `changed_ids` and its deleted set from `deleted_ids` —
> the **direct seeds**, never `affected_ids`. The five contract tests each edit an
> entity and assert *that entity* recomputes (and an *unrelated* one does not);
> none assert transitive-dependent recompute. So consume `created_ids` /
> `changed_ids` / `deleted_ids` and **do not** reach for `affected_ids` here. When
> the producer lands later, transitive derived invalidation (a dependent's hash is
> unchanged but its upstream changed) is a separate, additive concern.

### Files in scope

| File | Role in Phase 7 |
|---|---|
| `src/tyo3/session.py` | **edit (core)** — `_invalidate_derived` (`:781`) must build the dirty/deleted sets from the **id-level** delta fields (7.1); confirm `_schedule_derived`/`_after_commit` invalidate exactly once (7.1). `Snapshot.derived` (`:1595`) staleness/recompute honesty (7.2) |
| `src/tyo3/derive/dag.py` | **edit (core)** — `invalidate` (`:117`): one snapshot closed in `finally`, no second leaked snapshot (7.3); recompute actually runs (7.2); tighten the swallowed lookups (`:143-147`, `:290-298`) |
| `src/tyo3/derive/scheduler.py` | **edit** — drain the lazy queue so a lazy recompute runs on read; keep failure → `mark_failed` + last-good intact (7.2) |
| `src/tyo3/derive/layer.py` | reference / small edit — `binding`/`mark_failed`/`mark_clear`/`last_good_store_key` stay the state used by read-time staleness (7.2) |
| `src/tyo3/stores/fs.py` | **edit** — genuine-absence vs IO error (7.4) |
| `src/tyo3/stores/lancedb_store.py` | **edit** — stop swallowing query/write errors; keep `StoreBackendUnavailable` distinct (7.4) |
| `src/tyo3/stores/base.py` | reference — the `Store`/`VectorStore` protocols |
| `src/tyo3/tests/test_final_derived_contract.py` | the Phase 0 test that must go green — remove its **5** `xfail(strict=True)` markers as each contract lands (do **not** weaken it) |
| `src/tyo3/tests/test_gate5_derived.py` | un-skip the self-healing test (`:15`); keep the rest green |

---

## 2. Working rules for this phase (non-negotiable)

1. **Write/await the failing test first, then implement until green.** Phase 0
   shipped `test_final_derived_contract.py` with **five** `xfail(strict=True)`
   cases (`:95`, `:119`, `:143`, `:174`, `:198`). Your job removes the reason for
   each; remove a marker **only** once that case passes on its own (an xfail that
   starts passing with the marker still on is itself a failure under
   `xfail-strict`).
2. **Id-level, never path-shaped.** Invalidation consumes `created_ids` /
   `changed_ids` / `deleted_ids` — durable ids. Grep `_invalidate_derived` and the
   `invalidate` call chain and confirm no file-path field (`result.created`,
   `.changed`, `.deleted`, `.touched_files`) feeds a `DurableId` parameter.
3. **Exactly one invalidation per commit, leaking zero snapshots.** The derivation
   pass uses **one** pinned snapshot for invalidation *and* any eager recompute and
   closes it in a `finally`. There is **no** second `session.snapshot()` that is
   not closed. Confirm `invalidate` opens at most one snapshot per call.
4. **Honest staleness; last-good is sacred.** A recompute failure **never**
   overwrites or deletes the prior artifact — it marks the target `failed` and the
   reader serves the last-good artifact tagged `failed`. A miss with a last-good is
   `stale`/`failed`; a genuine miss with nothing ever produced is `absent`. Never
   write a partial artifact (§5.7).
5. **Stores fail loudly.** A store returns "missing" (`None` / `has() == False`)
   **only** for a genuinely absent key, and **propagates** every other IO/query
   error. A missing optional backend is the distinct typed `StoreBackendUnavailable`
   — separable from "not found" and from "backend errored." No bare
   `except Exception: pass` that hides a real failure (§5.12).
6. **Don't reshape the delta, the commit, or the bus.** Phase 7 consumes the
   Phase 3 `CommitDelta` unchanged and runs inside the Phase 6 post-commit hook. It
   changes only `derive/` and `stores/` (plus the two `session.py` seams listed
   above). No Rust.
7. **Don't over-reach into Phase 8.** The convenience-read lifetime fixes
   (`session.code` / `.layer` / `.entity` returning views over closed snapshots)
   and `DerivedLayerView.value`'s broad `except Exception: return None`
   (`layers/derived.py:53`) are **Phase 8**. Phase 7 fixes the derived *cache /
   invalidation / store* layer, not the convenience-view lifetime.
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
# 1. Phases 1–6 are landed and green (Phase 6 must be done first — Phase 7 runs
#    inside its post-commit hook). The compiled extension is current:
devenv shell -- build

# 2. The derived contract test is RED in exactly the expected way: five cases are
#    xfail(strict=True) (invalidation fed paths → inert; move-reuse; precise
#    recompute; eager snapshot leak; failure last-good).
devenv shell -- pytest src/tyo3/tests/test_final_derived_contract.py -q --no-cov -rA

# 3. The Gate-5 derived suite is green, with the self-healing test still xfail.
devenv shell -- pytest src/tyo3/tests/test_gate5_derived.py -q --no-cov -rA
```

Record the output. Three North Stars: (a) the five
`test_final_derived_contract.py` cases become real green assertions and their
markers are removed by the end of Phase 7; (b) the Gate-5 self-healing xfail is
removed and the case passes; (c) the rest of `test_gate5_derived.py` stays green
throughout — the cache/store/generator unit tests are your regression surface.

> **A note on Phase-6 wiring.** `_after_commit` (Phase 6) calls
> `_schedule_derived(delta)`, which the Phase-6 guide pointed at the *existing*
> `_invalidate_derived`. Before you start, confirm **where** `_invalidate_derived`
> is invoked: today (pre-Phase-6) it is called *inside* `_apply_graph_delta`
> (`session.py:1177`, `:1190`). After Phase 6 it should be called once, from
> `_schedule_derived`. If you find it called from **both** `_apply_graph_delta`
> and `_schedule_derived`, that is a double-invalidation — collapse it to one call
> site (prefer `_schedule_derived`, so `_apply_graph_delta` stays a pure graph
> applier). Read the actual tree; match it, not this prose.

---

## 4. Step-by-step implementation

Do the steps in order. Each step lists **what**, **why**, **where**, and a
**verify** command (devenv-prefixed). Commit at the natural breakpoints noted.

> **Sequencing matters and is deliberate.** Feed invalidation real ids first
> (7.1) so the rest of the pipeline receives the changed entity at all; then make
> the recompute actually run and staleness honest (7.2) so a changed entity is
> refreshed and a failure is reported; then fix the snapshot leak (7.3) on the now-
> live pass; then make stores honest (7.4); then un-skip the self-healing test and
> remove the contract xfails (7.5). Removing each xfail marker is the last move of
> the step that earns it.

### Step 7.1 (do first) — Feed invalidation id-level inputs

**What.** `_invalidate_derived` builds the dirty set from the commit delta's
**`created_ids` and `changed_ids`**, and the deleted set from **`deleted_ids`** —
durable ids, not the path-shaped `created`/`changed`/`deleted` metadata.

**Why.** §5.7 / plan §7.1. This is the change that "turns the currently-inert
invalidation back on." With paths, `applies_to(_kind_for_id(...))` filters every
entity out (`dag.py:140`, `:290`); with real ids, `_kind_for_id` resolves the
node kind and `resolve_input` resolves the content hash.

**Where / how.**
- In `session.py`, change `_invalidate_derived` (`:781-787`):
  ```python
  def _invalidate_derived(self, result: CommitDelta) -> None:
      dag = self._get_derivation()
      if dag.is_empty:
          return
      dirty = set(result.created_ids) | set(result.changed_ids)
      deleted = set(result.deleted_ids)
      dag.invalidate(self, dirty, deleted, result.revision)
  ```
  Note the `_ids` suffix on every field — that is the entire defect.
- **Confirm single invocation.** Ensure `_invalidate_derived` runs exactly once
  per commit. If Phase 6 left it called from both `_apply_graph_delta` and
  `_schedule_derived`, remove it from `_apply_graph_delta` (`:1177`, `:1190`) so
  the applier is purely structural and `_schedule_derived` owns derived
  invalidation. (If your tree already has it in one place, leave it.)
- `DerivationDAG.invalidate`'s signature (`dag.py:117`) is already
  `(session, dirty, deleted, revision)` taking `set[str]` of ids — no change to
  the signature, only to what flows in.

**Verify.**
```bash
devenv shell -- pytest \
  src/tyo3/tests/test_final_derived_contract.py::test_invalidation_receives_durable_ids \
  -q --no-cov -rA
```
This case primes the cache, edits the body, reads, and asserts the changed
entity's durable id appears in `_CALLS` (the ids the generator was invoked with).
It will *still fail* until 7.2 makes the recompute actually run — that is
expected. Do **not** remove its xfail marker yet; the marker comes off at the end
of 7.2/7.5 when the case passes on its own.

> Commit here: `fix(derived): feed invalidation id-level created/changed/deleted ids`.

---

### Step 7.2 — Read-time staleness + a lazy recompute that actually runs

**What.** Two coupled moves: (a) make staleness a **pure read-time hash
comparison** (the resolver already keys on the content hash — make its
`fresh`/`stale`/`failed`/`absent` honest and independent of transaction-time
mutable flags); (b) make a **lazy recompute actually execute** so a changed entity
is refreshed and a generator failure is observable. Today the lazy queue
(`scheduler._lazy`) is enqueued and never drained, so under the default
`recompute = "lazy"` the generator never runs after a change.

**Why.** §5.7. "On a delta, for each affected entity compare the new content hash
to the cached binding: unchanged ⇒ reuse; changed/missing ⇒ mark stale and
schedule recompute." And: "A recompute failure leaves the prior artifact intact
and marks the target `failed`, never a partial artifact." The five contract tests
pin this precisely:
- `test_invalidation_receives_durable_ids` — after a body change, the generator is
  invoked with the changed id.
- `test_move_unchanged_reuses_artifact` — a move with **unchanged** content hash
  hits the cache at the same `(input_hash, version)` key → reused, **no**
  recompute, `_CALLS` unchanged.
- `test_body_change_recomputes_only_affected` — only the changed entity
  recomputes; an unaffected one does not.
- `test_generator_failure_keeps_last_good` — a recompute failure yields
  `status == "failed"` and serves the **last-good** artifact intact.
- (and `test_eager_recompute_leaks_no_snapshot`, whose leak is 7.3.)

**Where / how.**
- **The cache-hit/reuse path is already correct** and must stay so: a move that
  doesn't change the body keeps the same content hash, so
  `L.keys_for(input_hash)` is identical and `L.cache.get(key)` hits →
  `status="fresh"`, artifact reused, generator not called. This is why
  `invalidate`'s `if prior == input_hash: continue` (`dag.py:149-152`) and the
  content-addressed cache are load-bearing — do not regress them.
- **Make the lazy recompute run.** Pick one coherent mechanism and apply it
  consistently (this is the one genuine design choice in Phase 7 — see the
  decision callout at the end of this section):
  - The literal meaning of "lazy" is *compute on first read*. So when
    `Snapshot.derived` (`session.py:1595`) misses at the current hash under
    `serving == "stale"`, instead of enqueuing a lazy item that nothing drains
    (`:1634`), **drive the recompute synchronously on that read** via the
    scheduler (the existing `recompute_now`, `scheduler.py:121`, already does
    exactly this for `block` serving: resolve input → generate → cache.put →
    bind → return). On success return the fresh artifact; on
    `GeneratorFailed`/`Exception` `mark_failed` and serve the last-good tagged
    `failed`; if no last-good, `absent`.
  - Equivalently, you may keep `_pending`/`_lazy` but ensure the lazy items are
    actually processed at read time (a `process_lazy_for(layer, id)` that the
    resolver calls). The dead-end today is that *nothing* drains `_lazy`.
  - Keep the **idempotency** guarantee (`scheduler._done` keyed by
    `(layer_name, input_hash)`, `scheduler.py:43`): the same `(layer, input_hash)`
    computes at most once even across repeated reads.
- **Honest status at read time.** Reduce reliance on transaction-time mutable
  flags. The resolver should determine status purely from: does the cache hold an
  artifact at the *current* `(input_hash, version)` (→ `fresh`); else is there a
  last-good and is the target in `L._failed` (→ `failed`) or not (→ `stale`); else
  `absent`. `DerivedLayer.mark_stale` (`layer.py:104-106`) is already a no-op with
  the comment "staleness is inferred" — keep staleness inferred from the
  hash/cache comparison, not from a stored stale-bit. `_failed`
  (`layer.py:108`,`:43`) is the one piece of recompute-outcome state the reader
  needs, and it is set only by a real failed recompute and cleared by a successful
  one (`mark_clear`, `layer.py:111`).
- **Last-good is preserved on failure.** `bind` (`layer.py:90-94`) records
  `_last_good[id]` only on a successful put; a failure path must **not** touch the
  cache or `_last_good`. The resolver's stale/failed branch
  (`session.py:1635-1640`) reads `last_good_store_key` + `cache._store.get` — keep
  that, and ensure a failed recompute leaves both the cache entry and the binding
  from the *previous* good revision intact.

**Verify.**
```bash
devenv shell -- pytest \
  src/tyo3/tests/test_final_derived_contract.py::test_invalidation_receives_durable_ids \
  src/tyo3/tests/test_final_derived_contract.py::test_move_unchanged_reuses_artifact \
  src/tyo3/tests/test_final_derived_contract.py::test_body_change_recomputes_only_affected \
  src/tyo3/tests/test_final_derived_contract.py::test_generator_failure_keeps_last_good \
  -q --no-cov -rA
```
Remove the four corresponding `@pytest.mark.xfail` markers (`:95`, `:119`, `:143`,
`:198`) **only** as each case passes on its own. (`test_eager_recompute_leaks_no_snapshot`
at `:174` is 7.3.)

> Commit here: `fix(derived): read-time staleness; lazy recompute runs; failure keeps last-good`.

> ### Decision to confirm before/while doing 7.2 — serving "stale" vs. running the recompute on read
>
> §5.7 describes `serving = "stale"` as *serve last-good immediately, recompute in
> the background*. There is **no background recompute worker** in the current
> single-threaded design, and the contract tests (`test_invalidation_receives_durable_ids`,
> `test_body_change_recomputes_only_affected`, `test_generator_failure_keeps_last_good`)
> require the generator to have **actually run** by the time the test reads the
> snapshot. The only place that can happen synchronously is the read (lazy =
> compute-on-read) or the commit-time invalidation pass (eager). The recommended
> resolution — **drive the lazy recompute synchronously on the read miss** —
> satisfies all five tests and matches the literal definition of "lazy."
> **Confirm this with the requester** (the alternative is to recompute eagerly
> inside the commit/`invalidate` pass for *all* changed entities regardless of the
> `recompute` policy, which is heavier and changes commit latency). Either way,
> `serving` still governs *what is served while a recompute is outstanding*
> (last-good tagged `stale`, vs `block` which computes-then-serves); the question
> is only *who drives the lazy compute* in the absence of a background worker.

---

### Step 7.3 — Fix the recompute snapshot lifetime (one snapshot, closed)

**What.** The derivation pass uses **one** pinned snapshot for both invalidation
and eager recompute and closes it in a `finally`. The second
`session.snapshot()` opened as an argument to `process_all` and never closed is
removed.

**Why.** §5.2's lifetime contract + plan §7.3. `DerivationDAG.invalidate`
(`dag.py:135-165`) currently opens a snapshot for the scan (closed in `finally`,
`:162`) and then opens a **second** one for `process_all` (`:165`) that is never
closed — even when `process_all` early-returns with nothing pending
(`scheduler.py:74`). `test_eager_recompute_leaks_no_snapshot` arms
`ResourceWarning`-as-error and `gc.collect()`s to catch exactly this.

**Where / how.**
- Rewrite `invalidate` so the eager processing happens **inside** the same
  `try/finally` that owns the one snapshot:
  ```python
  def invalidate(self, session, dirty, deleted, revision):
      if self.is_empty:
          return
      scheduler = self._get_scheduler()
      snap = session.snapshot()
      try:
          for layer in self.iter_layers():
              for durable_id in dirty:
                  ...  # applies_to / resolve_input / binding compare / mark_stale / enqueue
              for durable_id in deleted:
                  layer.drop(durable_id)
          scheduler.process_all(self, snap)   # reuse the SAME snapshot — no new one
      finally:
          snap.close()
  ```
  The only change of substance is that `process_all` is fed the **already-open**
  `snap` and the call moves *inside* the `try`, so the one `finally: snap.close()`
  covers it. Delete the `session.snapshot()` at the old `:165`.
- If you keep the lazy-on-read mechanism from 7.2, the read-time recompute uses the
  **read's own** pinned snapshot (`self` inside `Snapshot.derived`), which the
  caller's `with s.snapshot() as snap:` already owns and closes — no extra snapshot
  there either.
- While you are here, tighten the two swallowed lookups now that ids are real
  (`dag.py:143-147` `except (KeyError, AttributeError): continue` and `:290-298`
  `_kind_for_id`'s bare `except Exception: pass`): a genuinely-absent entity at the
  snapshot is a legitimate "skip" (leave a one-line comment), but do not let the
  bare `except Exception` mask a programming error — narrow it to the typed absence
  you expect (`KeyError`/the graph's typed not-found) and let anything else
  propagate. (Full exception hygiene is Phase 12; do the minimum here to keep the
  now-live path honest.)

**Verify.**
```bash
devenv shell -- pytest \
  src/tyo3/tests/test_final_derived_contract.py::test_eager_recompute_leaks_no_snapshot \
  -q --no-cov -rA
```
Remove the `@pytest.mark.xfail` at `:174` once it passes. The test edits under
`serving = "block"`, opens a snapshot, reads `fresh`, then `gc.collect()`s with
`ResourceWarning` promoted to an error — a leaked snapshot's `__del__` would raise.

> Commit here: `fix(derived): one pinned snapshot for invalidation + recompute; close in finally`.

---

### Step 7.4 — Make store errors typed (stop swallowing)

**What.** A store returns "missing" only for a genuinely absent key and propagates
every other IO/query error; the vector store stops swallowing; a missing optional
backend stays the distinct typed `StoreBackendUnavailable`.

**Why.** §5.7 / §5.12. A swallowed store error makes a failed write look like a
success and a query error look like "no results" — silent data loss. The plan
§7.4 requires three distinguishable outcomes: **not found**, **backend
unavailable** (`StoreBackendUnavailable`, already defined in `exceptions.py:61`),
and **backend errored** (propagate).

**Where / how.**
- **`FsStore`** (`stores/fs.py`): `get` (`:16-20`) already returns `None` only when
  the path does not exist and otherwise lets `read_bytes()` propagate — make that
  *explicit* (a comment that `None` ⇔ genuinely-absent key; an IO error on an
  existing file propagates). `has` (`:32-33`) is a pure existence check — fine.
  `delete` (`:35-39`) swallows `FileNotFoundError` (delete-of-absent is idempotent,
  correct) but must **not** swallow other `OSError`s — narrow the `except` to
  `FileNotFoundError`. `put` (`:22-30`) already writes-temp-then-fsync-then-rename
  (crash-safe) — leave it.
- **`LanceDbStore`** (`stores/lancedb_store.py`): remove the blanket
  `try/except Exception` that returns `None` / `[]` / silently no-ops:
  - `get` (`:60-71`): a genuine "row not found" → `None`; a backend/query error →
    propagate (do not return `None` for a failed query). Keep
    `StoreBackendUnavailable` from `_ensure_table` (`:34-38`).
  - `put` (`:73-88`): a failed insert must **raise**, not silently no-op — a
    swallowed `put` is the most dangerous case (the artifact is silently lost and a
    later `get` recomputes forever). Propagate.
  - `delete` (`:93-98`) and `nearest` (`:100-115`): propagate real errors; an empty
    result set from a healthy backend is a legitimate `[]`.
  - Keep `_ensure_table`'s `ImportError → StoreBackendUnavailable` (`:33-38`) — that
    is the one place "optional backend not installed" maps to the typed
    unavailable error.
- **`open_store`** (`stores/__init__.py:23-52`) already raises
  `StoreBackendUnavailable` for a missing optional backend and `ValueError` for an
  unknown backend — leave it; it is the correct "backend unavailable" boundary.
- Do **not** introduce a new store exception type unless a test requires it; reuse
  `StoreBackendUnavailable` (unavailable) and let real IO/query errors propagate as
  their native exception type (Phase 12 will add typed wrappers if needed).

**Verify.**
```bash
# The store-related Gate-5 cases (backend-unavailable, fs no-nearest) stay green:
devenv shell -- pytest src/tyo3/tests/test_gate5_derived.py -q --no-cov -rA
```
There is no dedicated store-error case in `test_final_derived_contract.py`; the
acceptance for 7.4 is that the existing store tests stay green and no store
operation swallows a non-absence error. (If `lancedb` is not installed in the dev
environment, the LanceDb code paths are exercised only via the
`StoreBackendUnavailable` test — that is expected; do not add a hard `lancedb`
dependency.)

> Commit here: `fix(stores): typed errors; fs genuine-absence only; lancedb stops swallowing`.

---

### Step 7.5 — Un-skip the self-healing test; remove the contract xfails

**What.** Remove the stale Gate-5 self-healing xfail and prove the behaviour, and
remove any remaining `test_final_derived_contract.py` xfail markers as their cases
pass.

**Why.** Plan §7.5. The self-healing test
(`test_gate5_derived.py::test_self_healing_derived_layer_cache_hit_recompute_reuse`,
`:15`) is marked `xfail(reason="Gate 5 Step 0: API does not exist yet")` — a stale
marker; the API exists. It proves the three core behaviours end-to-end over a real
session: (1) an **unrelated** edit reuses the artifact (no recompute), (2) a
**body change** recomputes exactly once, (3) a **move unchanged** reuses the
artifact. With 7.1–7.3 in place these now hold.

**Where / how.**
- Delete the `@pytest.mark.xfail` at `test_gate5_derived.py:15`. The test helpers
  it relies on already exist in the module: `uppercase_generator` (`:634`) and the
  `_UPPERCASE_CALL_COUNT` global (`:343`). Run it and confirm green; do **not**
  alter the assertions (it counts generator calls precisely:
  `+1` on a body change, `+0` on an unrelated edit and on a move).
- Confirm the broader Gate-5 file is green, including the related contract cases it
  already owns: `test_generator_version_bump_new_key_space` (`:669` — a
  `generator_version` bump forms a new key-space, the prior keys remain) and the
  GC cases (`test_gc_never_is_noop` `:701`, `test_gc_preserves_active_and_prior_versions`
  `:741`). If a GC case regressed because `gc_orphans` (`dag.py:167`) shares the
  snapshot-lifetime pattern, apply the same one-snapshot-closed discipline there.
- Remove any `test_final_derived_contract.py` xfail markers not already removed in
  7.2/7.3. After Phase 7 there must be **zero** xfail markers in that file.

**Verify.**
```bash
devenv shell -- pytest src/tyo3/tests/test_final_derived_contract.py \
  src/tyo3/tests/test_gate5_derived.py -q --no-cov -rA
```
All `test_final_derived_contract.py` cases green with no markers; the Gate-5
self-healing case green with its marker removed; the rest of Gate-5 green.

> Commit here: `test(derived): un-skip self-healing; remove derived-contract xfails`.

---

## 5. Acceptance — the Phase 7 gate

Run exactly what the plan's Phase 7 "Acceptance" lists, all via devenv:

```bash
devenv shell -- build   # pure-Python phase, but rebuild once to remove all doubt

devenv shell -- pytest \
  src/tyo3/tests/test_final_derived_contract.py \
  src/tyo3/tests/test_gate5_derived.py \
  -q --no-cov -rA
```

Then the full milestone gate (run the suites in the background; ~10–15 min):

```bash
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml
```

**Exit criteria (all must hold):**
- **Invalidation is id-level** — `_invalidate_derived` consumes `created_ids` /
  `changed_ids` / `deleted_ids`; no path-shaped field feeds a `DurableId`
  parameter. Proven by `test_invalidation_receives_durable_ids` (xfail removed).
- **A move with unchanged hash reuses the artifact** — same `(input_hash,
  version)` key → cache hit → no recompute. Proven by
  `test_move_unchanged_reuses_artifact` (xfail removed).
- **A body change recomputes only the affected ids** — the changed entity
  recomputes exactly once; an unaffected one does not. Proven by
  `test_body_change_recomputes_only_affected` (xfail removed).
- **No snapshot leaks** — invalidation + eager recompute share one pinned snapshot,
  closed in `finally`; no second snapshot is opened. Proven by
  `test_eager_recompute_leaks_no_snapshot` (xfail removed).
- **A generator failure keeps the last-good artifact** — status `failed`, prior
  artifact served intact, no partial write. Proven by
  `test_generator_failure_keeps_last_good` (xfail removed).
- **Stores are honest** — `FsStore` returns "missing" only for a genuinely absent
  key and propagates other IO errors; `LanceDbStore` no longer swallows
  query/write errors; a missing optional backend is `StoreBackendUnavailable`.
- **No remaining xfail markers** in `test_final_derived_contract.py`; the Gate-5
  self-healing xfail is removed and that case passes; the rest of
  `test_gate5_derived.py` is preserved.
- Both full suites stay green at the documented baseline (the Phase-7 ×2 derived
  failures are now fixed; the remaining baseline failure is
  `test_final_hash_ast.py::test_formatting_only_hashes_same` → Phase 9 — do **not**
  add new failures).

---

## 6. Pitfalls specific to this phase

- **Feeding `affected_ids` to invalidation.** Tempting "for completeness," but
  wrong here: derived invalidation iterates the **direct seeds** (`created_ids` +
  `changed_ids`, `deleted_ids`), per plan §7.1. `affected_ids` is seeds-only today
  anyway (deferred producer), and transitive-dependent derived invalidation is a
  separate later concern. Use the `_ids` seed fields.
- **Leaving the lazy queue a dead-end.** Fixing only 7.1 (ids) without 7.2 (run the
  recompute) makes `test_invalidation_receives_durable_ids` /
  `test_body_change_recomputes_only_affected` still fail — the changed id reaches
  `invalidate`, marks stale, enqueues lazy, and then *nothing runs the generator*.
  The lazy queue must actually be drained (on read, recommended).
- **Recomputing on a move with an unchanged hash.** If your recompute trigger keys
  on "the id appeared in the delta" rather than "the content hash changed," a pure
  move will recompute and `test_move_unchanged_reuses_artifact` fails. The trigger
  must be the **hash comparison** (`prior == input_hash → reuse`), and a cache hit
  at the unchanged key must short-circuit before any generate.
- **Overwriting last-good on failure.** A failed recompute must not `cache.put` a
  partial artifact or clear `_last_good`/the binding. `mark_failed` only adds to
  `_failed`; the reader serves the prior artifact tagged `failed`. Writing anything
  on the failure path breaks `test_generator_failure_keeps_last_good`.
- **The second snapshot.** Deleting only the *symptom* (e.g. wrapping the leaked
  `session.snapshot()` in a `with`) but leaving two snapshots open during the pass
  still doubles snapshot pressure. Use **one** snapshot for the whole pass; feed it
  to `process_all`.
- **Closing a snapshot the read still needs.** If you drive lazy recompute on read,
  use the read's own pinned snapshot (`self` in `Snapshot.derived`); do **not** open
  and close a fresh snapshot inside the resolver — the artifact's `resolve_input`
  reads the same pinned graph the caller is holding.
- **Swallowing a real store error as "absent."** Returning `None` from a
  `LanceDbStore.get` that *errored* (not "row absent") makes a broken backend look
  empty and the layer recomputes forever (or serves wrong staleness). Distinguish
  absent (`None`) from errored (propagate) from unavailable
  (`StoreBackendUnavailable`).
- **Reintroducing config double-read or touching the bus/commit.** Phase 7 is
  `derive/` + `stores/` + two `session.py` seams. If you find yourself in
  `rust/`, `bus/`, or the commit funnel, you have strayed (Phases 6/10/11 own
  those).
- **Removing an xfail before the case passes.** Under `xfail-strict`, an xfail that
  *passes* with the marker on is reported as a failure (XPASS). Make the case pass
  first, then delete the marker — never the reverse.
- **Over-reaching into Phase 8.** `DerivedLayerView.value`'s broad
  `except Exception: return None` (`layers/derived.py:53`) and the convenience-read
  closed-snapshot lifetime are Phase 8. Leave them; Phase 7 fixes the cache /
  invalidation / store layer.

---

## 7. Suggested commit sequence for the phase

1. `fix(derived): feed invalidation id-level created/changed/deleted ids` (7.1)
2. `fix(derived): read-time staleness; lazy recompute runs; failure keeps last-good` (7.2)
3. `fix(derived): one pinned snapshot for invalidation + recompute; close in finally` (7.3)
4. `fix(stores): typed errors; fs genuine-absence only; lancedb stops swallowing` (7.4)
5. `test(derived): un-skip self-healing; remove derived-contract xfails` (7.5)

This maps to the plan's single commit
`fix(derived): id-level invalidation; read-time staleness; close snapshots; typed
stores`; squash on landing if the series is preferred as one reviewed commit.

Every commit passes its focused `pytest` slice; the last one passes the full
derived contract test (all xfail markers removed) and both full suites (the
milestone gate). **No AI-attribution / `Co-Authored-By` footers** (project
policy). Remember: **all of it through `devenv shell --`.**

---

## 8. What Phase 7 deliberately leaves for later (so you don't over-reach)

- **Convenience-read lifetime + read-surface error hygiene** is **Phase 8**:
  `session.code` / `.layer` / `.entity` returning lazy views over already-closed
  snapshots, the floating "latest" view, and `DerivedLayerView.value`'s broad
  `except Exception: return None` (`layers/derived.py:53`). Phase 7 fixes the
  derived *cache/invalidation/store*; the *view lifetime* is Phase 8.
- **Transitive derived invalidation** (a dependent entity whose own hash is
  unchanged but whose upstream layer artifact changed) waits on the in-commit
  code-layer producer + live `reverse_deps` ([[phase4-producer-deferred]]). Phase 7
  invalidates the direct seeds only — which is the full §7.1 contract and all five
  contract tests assert.
- **AST-canonical hashing** is **Phase 9**. Derived layers key on the content hash;
  Phase 9 makes that hash meaning-faithful. Phase 7 takes the hash as-is.
- **Single config source** is **Phase 10** — the `serving`/`recompute`/store config
  is consumed as it stands today; do not unify the config read here.
- **Splitting `derive/` / `session.py`** and full exception hygiene
  (`except Exception: pass` everywhere) are **Phases 11–12**. Phase 7 narrows only
  the swallows on the now-live derived path; the sweep is later.
- **A real background recompute worker.** Phase 7 makes lazy recompute run on read
  (or eagerly in the pass, per the 7.2 decision). A genuine async background
  derivation worker (true non-blocking `stale` serving) is a future enhancement, not
  part of this refactor.

Keeping these out of Phase 7 isolates its single high-value change — turning the
inert derived subsystem back on with id-level invalidation, honest read-time
staleness, a recompute that actually runs and never clobbers last-good, no leaked
snapshots, and stores that fail loudly — behind the Phase 0 derived contract test,
and keeps both suites green at every commit.
