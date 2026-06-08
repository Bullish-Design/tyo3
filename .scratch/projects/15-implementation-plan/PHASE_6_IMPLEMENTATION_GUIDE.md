# Phase 6 (V2) — The scoped native producer (keystone)

> Execution guide for **Phase 6 of `REFINED_IMPLEMENTATION_PLAN_V2.md`**. Read
> `REFINED_IMPLEMENTATION_CONCEPT_V2.md` §5 (the affected-set model) and V1 §5.3
> (the commit transaction) / §5.4 (the delta + reverse-dep index) once before
> starting.
>
> **This phase replaces the old "Phase 6 = the bus."** The producer was deferred
> across Phases 2–5; building it now is what ends the deferral snowball. After
> this phase `affected_ids` is genuinely transitive at the source, and Phase 7
> can delete every bridge the deferral forced into existence.
>
> **Depends on Phases 1–5** (content gate, native code layer behind the parity
> oracle, id-level `CommitDelta`, pure-applier cutover, native `commit(mutation)`).
> If any of their milestone gates is red, finish it first.

---

## 0. Dev environment — read first

Every in-repo operation runs through the **devenv shell** (Nix-managed
toolchain). Two forms:

```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml code_layer code_delta
devenv shell                # interactive; then run scripts directly
```

This phase is **mostly native** (the producer lives in `rust/src/code_layer.rs`
and is driven from `rust/src/project.rs`'s commit). **After every Rust change you
must `devenv shell -- build`** before any pytest gate observes it — `cargo test`
alone does not refresh the compiled extension Python imports. Suites are slow;
allow ~10–15 min and launch full runs in the background.

> Throughout, assume every `pytest` / `cargo` / `maturin` / `ruff` / script
> invocation is prefixed with `devenv shell --`.

---

## 1. What Phase 6 changes, and why

**The situation today (post-Phase-5):**

- `build_commit_delta` (`rust/src/project.rs`) emits `code_delta = None` and an
  `affected_ids` that is **seeds-only** (`changed ∪ deleted`), because
  `head.code_layer` is never populated in-commit (`reverse_deps` is empty). The
  Python `Delta.from_commit_delta` papers over this with an Option-B transitive
  walk over the materialised head graph — the bridge Phase 7 will delete.
- The producer **machinery already exists and is parity-verified**
  (`code_layer.rs`): `produce_code_delta` (`:388`), the passes
  (`resolve_references` `:726`, `inherits_pass`, the overrides pass),
  `CodeLayer::diff_from` (`:253`), `CodeLayer::affected_closure` (`:232`), and
  `add_edge`/`remove_edge` that maintain `reverse_deps` (`:191`/`:203`). What is
  missing is the **in-commit driver**: nothing updates `head.code_layer` as part
  of the commit, and the producer runs (when it runs) over *all* files.

**The fix this phase delivers (Concept V2 §5.1):**

1. The commit runs the producer over the **dirty scope** (changed files ∪ their
   one-hop importers) and updates `head.code_layer` in place, under the same lock
   as the content/identity mutation (V1 §5.3 step 4).
2. The commit emits the **minimal incremental `code_delta`** from
   `diff_from(prev_layer)` — `Some({…})` for real structural change, `Some({})`
   for a cosmetic edit, and the full-rebuild path **only** for genuine `rescan`
   (cold start, `sync_all`). `code_delta = None` is retired.
3. `affected_ids = affected_closure(changed ∪ deleted)` over the freshly-updated
   `reverse_deps`, seeding deletions from the **prior** layer's reverse-deps
   (V1 §5.4 closure subtlety).
4. The result: a transitive, **container-granular, never-miss** `affected` set,
   computed natively. Method-level precision is deferred to the optional async
   layer (Phase 9), by design.

**Why container-granular is correct and sufficient (Concept V2 §5.2):** the
analysis engine emits only *named* structural edges; an inference-flow dependency
(`w.draw()` through an inferred type) is not an edge. Coverage is preserved
because a member-body edit moves the *container's* `content_hash` (the container
hash subsumes member bodies) and the container is reachable by a named chain.
This is guarded by `src/tyo3/graph/tests/test_inference_flow_coverage.py` — keep
it green throughout.

### Files in scope

| File | Role in Phase 6 |
|---|---|
| `rust/src/project.rs` | **edit (core)** — drive the producer inside `commit` over the dirty scope; update `head.code_layer`; build the incremental `code_delta` + transitive `affected_ids` (6.1–6.3) |
| `rust/src/code_layer.rs` | **edit** — add/confirm a scoped `produce` entry that re-derives only the dirty scope and diffs against `prev`; confirm `reverse_deps` maintenance + `affected_closure` (6.1–6.3) |
| `rust/src/dto/commit_delta.rs` | **reference** — `affected_ids` / `code_delta` shape (unchanged; producer now fills them) |
| `src/tyo3/graph/tests/test_incremental_parity.py` | the parity oracle — must stay green (incremental == rebuild) |
| `src/tyo3/graph/tests/test_inference_flow_coverage.py` | the container-granular coverage guard — must stay green |
| `src/tyo3/tests/test_commit_delta_*` / a new closure test | prove `affected_ids` is transitive |

---

## 2. Working rules (non-negotiable)

1. **The producer runs inside the lock, before publication** (V1 §5.3 step 4 ⊂
   the single transaction; publication is step 7, strictly last). The code-layer
   mutation must not be observable half-applied.
2. **Scope, don't rebuild.** The dirty scope is changed files ∪ one-hop
   importers. A full rebuild runs **only** for `rescan` (cold start / `sync_all`).
   A normal edit must not re-analyze the project.
3. **`reverse_deps` is maintained, not recomputed.** Use `add_edge`/`remove_edge`
   so the index updates edge-by-edge; never rebuild it from scratch per commit.
4. **Deletion closure seeds from the prior layer.** A deleted id's dependents
   live in the *old* reverse-deps; capture the prior `head.code_layer`'s relevant
   neighbourhood before overwriting it (V1 §5.4).
5. **Parity stays green.** Applying the incremental `code_delta` to a fresh graph
   must equal a full rebuild over the same final content
   (`test_incremental_parity.py`). This is the correctness oracle for the
   incremental path.
6. **No perf regression at `open()`.** The full producer regressed `open()` ~100×;
   the scoped producer must not. Measure before/after (6.4 verify).
7. **Container-granularity is the contract, not a bug.** Do **not** try to add
   inference-flow edges here. Method precision is Phase 9. Keep
   `test_inference_flow_coverage.py`'s canary green (no `render → draw` edge).

---

## 3. Pre-flight — establish the baseline

```bash
# Producer machinery + parity are green today (full/rescan path).
devenv shell -- build
devenv shell -- cargo test --manifest-path rust/Cargo.toml code_layer code_delta affected
devenv shell -- pytest src/tyo3/graph/tests/test_incremental_parity.py \
  src/tyo3/graph/tests/test_inference_flow_coverage.py -q --no-cov

# Capture the open() baseline to compare against after 6.1–6.3.
devenv shell -- pytest src/tyo3/tests/ -q --no-cov -k "open or concurrency" -rA
```

Record `affected_ids` for a base-class edit *today* (it will be seeds-only) so
you can prove the change once the producer lands.

---

## 4. Step-by-step

### Step 6.1 — Drive the producer over the dirty scope inside the commit

**What.** In the commit body (`rust/src/project.rs`, the staged-commit path from
Phase 5), after content is staged and identity reconciled for the new revision,
run the producer over the **dirty scope** and update `head.code_layer`.

**Why.** V1 §5.3 step 4: the code-layer structural change is part of the single
transaction. Concept V2 §5.1: re-analyzing only the dirty scope keeps cost
proportional to the edit.

**Where / how.**
- Compute the dirty scope from the staged mutation: the changed/created/deleted
  files, plus their **one-hop importers** (files that import a changed file —
  needed so a reference that was previously unresolved becomes resolvable, and
  vice versa). Importers are available from the file-level import edges the
  producer already resolves; if not yet indexed, derive them from
  `head.code_layer` edges of kind `Imports`.
- Call the producer scoped to that set. `produce_code_delta` (`code_layer.rs:388`)
  already collects entities, materialises nodes, resolves references/imports
  (`resolve_references`), and runs the inheritance two-pass. Ensure it accepts a
  **scope** (a set of files) rather than always walking all files; the full path
  is the scope = all-files special case.
- Apply the produced nodes/edges into `head.code_layer` via the maintained
  `upsert_node`/`add_edge`/`remove_edge` so `reverse_deps` updates incrementally.
- This must happen **before** `store.publish_staged` (the last in-lock step). On
  any error here, the Phase-5 rollback restores the prior layer (capture a
  `Baseline` of `head.code_layer` alongside the existing registry/authored
  baseline).

**Verify.**
```bash
devenv shell -- build
devenv shell -- cargo test --manifest-path rust/Cargo.toml code_layer commit
devenv shell -- pytest src/tyo3/graph/tests/test_incremental_parity.py -q --no-cov
```

### Step 6.2 — Emit the minimal incremental `code_delta`

**What.** Replace `code_delta = None` with the incremental delta from
`CodeLayer::diff_from(prev_layer)`; reserve the full-rebuild delta for `rescan`.

**Why.** Phase 4's three-state consumer (`None` → rebuild; `Some({})` → no-op;
`Some({…})` → apply). With the producer landed, the commit can emit `Some(…)`
precisely, so the Python applier never needs to rebuild except on `rescan`.

**Where / how.**
- Capture `prev = head.code_layer.clone()` (cheap; it's `BTreeMap`/`BTreeSet`)
  before applying the producer's output, then `let code_delta =
  new_layer.diff_from(&prev, revision, rescan)`.
- For a cosmetic edit (no structural change) `diff_from` yields an empty delta
  (`Some({})`) — the applier no-ops. For a real change it yields the upserts /
  moves / removals / edge deltas. For `rescan=true`, `diff_from(&CodeLayer::default(), …)`
  yields the full delta.
- Set `code_delta = Some(dto)` always; **never** `None` (that signal is retired).
  **Never** signal rebuild via `rescan` on the *nested* delta (the applier reads
  nested `rescan` as "replace wholesale" — see `phase4-producer-deferred`).

**Verify.**
```bash
devenv shell -- pytest src/tyo3/graph/tests/test_incremental_parity.py -q --no-cov -rA
# cosmetic edit ⇒ empty nested delta ⇒ applier no-op:
devenv shell -- pytest src/tyo3/tests/test_graph_apply_code_delta.py -q --no-cov
```

### Step 6.3 — Transitive `affected_ids` over the maintained `reverse_deps`

**What.** Compute `affected_ids = affected_closure(changed_ids ∪ deleted_ids)`,
seeding deletions from the prior layer.

**Why.** V1 §5.4: the affected set is the closure of the changed/deleted set
under inbound edges, computable from the reverse-dep index without a global scan.

**Where / how.**
- `let seeds: BTreeSet = changed_ids ∪ deleted_ids;`
- For **deleted** ids, their dependents are recorded in the *prior* layer's
  `reverse_deps` (the new layer no longer has the deleted node's inbound edges).
  Union the prior layer's `reverse_deps` neighbourhoods for deleted ids into the
  walk, or run the closure over a transient index = `new.reverse_deps ∪
  prior.reverse_deps[deleted]`. Add a test (deleting a base class still reports
  its subclasses).
- `let affected = closure_index.affected_closure(&seeds);` → fill
  `CommitDelta.affected_ids`.

**Verify.**
```bash
devenv shell -- build
# NEW test: base-class edit reports subclasses + their importers in affected_ids.
devenv shell -- pytest src/tyo3/tests/ -q --no-cov -k affected -rA
devenv shell -- pytest src/tyo3/graph/tests/test_inference_flow_coverage.py -q --no-cov
```
Confirm: editing `Widget.draw`'s body now yields `affected_ids` containing the
ids of `Widget`, `make_widget`, and `render` (container-granular coverage via the
class-hash + named chain), **not** seeds-only.

### Step 6.4 — Retire full-rebuild-per-commit; guard the perf

**What.** Ensure the only full producer run is `rescan`; everything else is
scoped. Add a regression guard on `open()` / commit cost.

**Why.** Working rule 6 — the full producer is the ~100× trap.

**Where / how.**
- Audit the commit path: the full producer must be reachable only when
  `rescan=true` (cold start, `sync_all`, watcher coarse change). Assert in a test
  that a single-file `edit` re-analyzes only the dirty scope (e.g. instrument a
  counter, or assert wall-clock stays within a factor of the pre-producer
  baseline).
- Re-run the `open`/`concurrency` slice and compare to the pre-flight baseline.

**Verify.**
```bash
devenv shell -- pytest src/tyo3/tests/ -q --no-cov -k "open or concurrency" -rA
devenv shell -- cargo test --manifest-path rust/Cargo.toml   # full Rust gate
```

> Commit here: `feat(commit): scoped in-commit code-layer producer; transitive affected`.

---

## 5. Acceptance — the Phase 6 gate

```bash
devenv shell -- build
devenv shell -- pytest \
  src/tyo3/graph/tests/test_incremental_parity.py \
  src/tyo3/graph/tests/test_inference_flow_coverage.py \
  -q --no-cov -rA
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml
```

**Exit criteria:**
- `head.code_layer` is maintained in-commit; `reverse_deps` is non-empty and
  correct after every revision.
- `affected_ids` is the transitive, container-granular closure (a base-class edit
  reports its subclasses and their importers); proven natively, no Python walk.
- `code_delta` is the minimal incremental delta (`Some(…)`); `None` is retired;
  full rebuild only on `rescan`.
- The parity oracle is green (incremental == rebuild).
- `test_inference_flow_coverage.py` green (container coverage + the no-edge canary).
- No `open()` / commit perf regression vs the pre-flight baseline.
- A failed producer run rolls the commit back to R−1 (no torn layer).

---

## 6. Pitfalls specific to this phase

- **Forgetting `devenv shell -- build` after a Rust edit** — the pytest gate
  probes the old extension. The #1 phantom-failure source.
- **Rebuilding `reverse_deps` per commit.** Defeats the whole cost argument. Use
  `add_edge`/`remove_edge`; maintain, don't recompute.
- **Closure over the wrong layer for deletions.** A deleted id's dependents are in
  the *prior* reverse-deps. Seed from the old layer or you under-report (a §5.4
  no-miss violation).
- **Signaling rebuild via nested `rescan`.** The applier reads nested `rescan` as
  "wipe the graph." Empty nested delta = cosmetic no-op; full rebuild is the
  *unnested* `rescan` path. (See `phase4-producer-deferred`.)
- **Letting the full producer run on a normal edit.** That is the ~100× trap.
  Scope to dirty files ∪ importers; full only on `rescan`.
- **Trying to add inference-flow edges.** Out of scope and impossible from the
  occurrence stream; method precision is Phase 9. Keep the no-edge canary green.
- **Mutating the layer outside the lock or after publish.** It must be in-lock,
  pre-publish, and rolled back on failure (Phase-5 baseline).

---

## 7. What Phase 6 leaves for later

- **Deleting the Python transitive bridge** (`_compute_affected`/`_resolve_files`)
  and the legacy read-surface builder — **Phase 7** (they can only be deleted once
  this phase makes `affected` transitive at the source).
- **Method-level precision** — **Phase 9** (async, additive, over snapshots).
- **Derived invalidation consuming the transitive affected** — **Phase 8**.
- The container-subsumes-members invariant is *relied on* here and *made
  explicit/owned* in **Phase 10** (hashing).
