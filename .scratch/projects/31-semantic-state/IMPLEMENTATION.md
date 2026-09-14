# IMPLEMENTATION — Semantic-state cleanup (project 31, historical guide)

Read [INVESTIGATION.md](INVESTIGATION.md) first. It concludes **no new type**;
this file is the completed Design A execution guide it recommended (§9). The
three original steps, plus the later eager-open follow-up, have already landed.
This is retained for provenance and should not be treated as a new work order;
use [README.md](README.md) for the current status and next work.

## Ground rules

- **No new struct, enum or trait.** If a step starts to grow one, stop and
  re-read INVESTIGATION §8 — Designs B/C/D were rejected on evidence.
- **Behaviour-preserving throughout.** Every step is a pure refactor or a
  comment. Any parity movement means a step changed output, which is a bug.
- **`TyProjectState.code_layer` stays `Option<Arc<CodeLayer>>`** and
  **`HeadState.code_layer` stays a non-optional `Arc<CodeLayer>`**. Four
  legitimate `None` producers (INVESTIGATION §4.3).
- **Do not change `CodeNodeDto`'s wire shape.** Parity-oracle strict tier.
- Version control: route through **gitman**. One lane per step.

## Commands

```sh
devenv shell -- check-rust        # fast type-check, ~1s — the Rust edit loop
devenv shell -- clippy            # -D warnings, matches the CI gate
devenv shell -- build             # maturin develop (~17s) — needed to run Python
devenv shell -- parity-oracle     # MUST stay green
devenv shell -- tests             # cargo test + full Python suite
```

## Baseline — record before you start

Verified at trunk `aa87019` on 2026-09-14:

| Gate | Result |
|---|---|
| `check-rust` | exit 0 |
| `cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings` | exit 0 |
| `parity-oracle` | **8 passed** |
| `tests` | **171 Rust, 833 Python** |

Counts must not drop. Steps 1 and 2 each *add* tests, so the final counts rise.

---

# Step 0 — Correct the comments

**Goal.** Make the real rules readable at the point of use. No code changes.

**Risk.** None. Comments only.

## 0.1 `rust/src/project.rs:94-104` — the carried-layer field

Today's comment omits the most important `None` producer and misdescribes the
fallback:

```rust
/// The committed code layer for this state's revision, when one exists.
///
/// `None` is used by read clones taken before any commit has produced a
/// layer, and by analysis/test constructors. Consumers fall back to a full
/// rebuild when it is absent.
```

Replace the body with all four producers and the correct fallback semantics:

- a read clone taken before the first reconciling commit (the head layer is
  still the empty placeholder from `open.rs:168`);
- **a time-travel snapshot** — `methods.rs:607-613` deliberately withholds the
  head layer, which is only valid for the head revision;
- the pre-reconcile extraction state (`commit.rs:356-365`);
- `build_frozen` (`open.rs:243`) and test constructors.

State plainly: **the fallback is a stateless recompute, not a lazy cache.** Both
call sites discard the rebuilt layer (`snapshot.rs:61`, `methods.rs:673`), so a
miss costs speed on *every* call, never correctness. Project 29 DESIGN §5's
"lazily produced layer" is superseded — say so, so the phrase is not reintroduced.

## 0.2 `rust/src/project.rs:128` and `:152` — the two planes

On `HeadState.registry` and `HeadState.code_layer`, record the asymmetry that
makes them non-interchangeable (INVESTIGATION §4.1), in one line each:

- registry — **last-known** facts across revisions; retains `Orphaned` anchors
  (`identity.rs:187-211`); persisted every commit (`commit.rs:551-567`); survives
  `reload()` (`methods.rs:137`).
- code layer — **current** facts for exactly one revision; adds synthetic
  `<module>` nodes and external stubs; never persisted; discarded by `reload()`
  (`methods.rs:141` → `open.rs:168`).

Add the ordering constraint: the layer is produced **from** the reconciled
registry, so `commit.rs:905` must precede `:916`. This is the fact that makes a
combined `SemanticState` impossible to publish atomically.

## 0.3 `rust/src/project/commit.rs:915` — the stale carried layer

`head.read_clone()` there yields a state whose registry is at R and whose
`code_layer` is at R−1. Say so, and say that no consumer reads it: `Builder`
touches only `state.root`, `state.db`, `state.registry` and
`state.hash_policies` (`code_layer.rs:561-573`). Add: **do not start reading
`state.code_layer` inside the commit** — it is stale by construction there.

## 0.4 `rust/src/project/methods.rs:607-613` — the retention decision

The existing comment is correct. Add the cost, so a future "cache time-travel
layers too" edit meets the number first: project 30 Step 0 measured a retained
repository-scale layer at **12.83 MiB**; retaining per revision needs a bound
(project 30 DESIGN §4.1).

## 0.5 Verify

```sh
devenv shell -- check-rust && devenv shell -- clippy
devenv shell -- tests
```

**Exit condition.** 171 Rust / 833 Python, unchanged. No behaviour touched.

---

# Step 1 — One serve-or-rebuild path

**Goal.** Delete a character-for-character duplication that sits half on and half
off the parity oracle.

**Risk.** Low, but this is the highest-risk step in the project: it touches the
oracle's input. Run `parity-oracle` first.

## 1.1 The duplication, verified before Step 1

```sh
diff <(sed -n '61,76p' rust/src/project/snapshot.rs) \
     <(sed -n '675,690p' rust/src/project/methods.rs)   # no output at baseline
```

`tests/test_final_parity_oracle.py:247` calls `assert_parity(session)`, which
resolves through `tests/parity_oracle.py:417-420` to
`session._inner.full_code_delta` — i.e. the head call site **only**. Before
Step 1, the snapshot call site had no oracle coverage and could diverge with
the suite green. Step 1 removed that second body; both call sites now call the
shared helper.

## 1.2 The extraction

In `rust/src/project.rs`, next to `clone_locked_state`:

```rust
/// Serve `state`'s committed code layer as a full (`rescan = true`) delta, or
/// rebuild it when no layer is carried.
///
/// A carried layer is byte-equivalent to a fresh full `Builder::build`
/// (`code_layer.rs:525`, proved by
/// `scoped_producer_matches_full_rebuild_across_generations`), so a miss costs
/// speed, never correctness. Pure and GIL-free: callers wrap it in `py.detach`.
///
/// ONE definition, two callers (`PySnapshot::full_code_delta` and
/// `PyTyProject::full_code_delta`). Only the latter is covered by the parity
/// oracle — keep them on this single path so the uncovered one cannot drift.
pub(crate) fn full_code_delta_for(
    state: &TyProjectState,
    revision: u64,
) -> dto::CodeDeltaDto {
    let empty = crate::code_layer::CodeLayer::new();
    match state.code_layer.as_deref() {
        Some(layer) => layer.diff_from(&empty, revision, true),
        None => {
            let (_next, delta) =
                crate::code_layer::produce_code_delta(state, &empty, revision, true, None);
            delta
        }
    }
}
```

This is the existing home for shared read-path helpers. Every project
submodule imports the shared state and helpers through `use super::*`, so both
callers see the helper directly without making `snapshot.rs` an indirect
dependency.

Both call sites collapse to (`snapshot.rs:61`, `methods.rs:673`):

```rust
let delta = py.detach(move || full_code_delta_for(&state, revision));
```

**Keep `py.detach` at the call sites.** `diff_from` over 36k edges must not hold
the GIL. Only the pure computation moves.

## 1.3 Test — head and snapshot graphs must agree

Add to `tests/test_graph_snapshots.py` (which already holds the head-graph
tests). At one revision, `session.graph` and `session.snapshot().graph()` must
produce the same node set and the same `(source, target, kind)` edge relation
set. This is the invariant the duplication threatened and that the oracle does
not check.

Make it non-vacuous: assert the graph is non-empty first.

## 1.4 Verify

```sh
devenv shell -- check-rust && devenv shell -- clippy
devenv shell -- build
devenv shell -- parity-oracle     # FIRST — methods.rs is its input
devenv shell -- tests
```

**Exit condition.** The `diff` in §1.1 no longer applies (one copy remains);
`parity-oracle` green at 8; suite green at 171 Rust / 834+ Python.

---

# Step 2 — One cache-hit rule

**Goal.** State "an empty head layer is a cache miss" once instead of twice.

**Risk.** Low. Two expressions become one function call.

## 2.1 The duplication

- `project.rs:221` — `(!self.code_layer.is_empty()).then(|| Arc::clone(&self.code_layer))`
- `methods.rs:613` — `if is_head { head.servable_code_layer() } else { None }`

The shared clause is duplicated; the `is_head` clause is **deliberate** and
belongs only at the snapshot site. Preserve that distinction — do not fold
`is_head` into the helper.

## 2.2 The change

In `rust/src/project.rs`, a new `impl HeadState` block beside
`impl ReadCloneSource for HeadState`:

```rust
impl HeadState {
    /// The head's code layer when it is real, `None` while it is still the
    /// empty placeholder installed by `build_head_with_config` (`open.rs:168`).
    ///
    /// Non-empty projects materialize the head layer at open after identity
    /// reconciliation. Treating the empty placeholder as a miss keeps empty
    /// projects on the rebuild fallback, which yields the same (empty) delta —
    /// so this can only cost speed, never correctness.
    ///
    /// Callers that pin a NON-head revision must not use this: the head layer is
    /// valid only for the head revision (`methods.rs:607-613`).
    pub(crate) fn servable_code_layer(&self) -> Option<Arc<crate::code_layer::CodeLayer>> {
        (!self.code_layer.is_empty()).then(|| Arc::clone(&self.code_layer))
    }
}
```

- `project.rs:204` → `code_layer: self.servable_code_layer(),`
- `methods.rs:613` → `let code_layer = if is_head { head.servable_code_layer() } else { None };`

## 2.3 Tests

**Rust** (`rust/src/project.rs` test module). Keep it to the pure predicate —
`commit()` is only ever driven from `#[pymethods]` (`methods.rs:209`, `:252`,
`:287`) and no Rust test drives the funnel today, so do **not** build a commit
harness for this:

1. A freshly built head (`build_head`) returns `None` — the placeholder is empty.
2. After assigning a non-empty layer directly, it returns `Some`, and
   `Arc::ptr_eq` holds against `head.code_layer` (proving the `Arc` is shared,
   not deep-cloned).

**Python** (`tests/test_mvcc_snapshots.py`, which already time-travels at
`:71-76`). The behavioural half of the `is_head` rule:

3. Edit a file, then assert `snapshot(at=head-1).graph()` reflects the **old**
   content while `snapshot().graph()` reflects the **new** content. This proves
   the time-travel path rebuilds against its frozen db instead of serving the
   head layer — the correctness consequence of the `is_head` guard, and the one
   thing a timing test would only prove flakily.

## 2.4 Verify

```sh
devenv shell -- check-rust && devenv shell -- clippy
devenv shell -- build && devenv shell -- parity-oracle && devenv shell -- tests
```

**Exit condition.** No `code_layer.is_empty()` call remains outside
`servable_code_layer`. Suite green, counts risen by the new tests.

---

# Step 3 — Re-measure and record

**Goal.** Prove the refactor moved nothing.

No code. Re-run the three probes and append the results to INVESTIGATION §14
under an "After" heading:

```sh
devenv shell -- bash -c 'cd "$DEVENV_ROOT" && PYTHONPATH=src python \
  .scratch/projects/31-semantic-state/probe_timing.py .'
devenv shell -- bash -c 'cd "$DEVENV_ROOT" && PYTHONPATH=src python \
  .scratch/projects/31-semantic-state/probe_memory.py'
devenv shell -- bash -c 'cd "$DEVENV_ROOT" && PYTHONPATH=src python \
  .scratch/projects/31-semantic-state/probe_read_clone.py . repo-root'
```

Expected for the original three Project 31 lanes: every figure within run-to-run
noise of INVESTIGATION §6.3 / §14.2 — carried-layer `full_code_delta` ≈ 0.15–0.18
s, fallback ≈ 4.1–4.4 s, same-revision snapshots ≈ 2 MiB each. The later eager
open follow-up supersedes the pre-commit head fallback; its measurements are in
INVESTIGATION §14.5. A movement outside the applicable band means a step
changed behaviour; bisect the relevant lane.

Read `probe_memory.py`'s confound note (INVESTIGATION §14.2) before quoting its
per-revision figure: it is an upper bound, not an isolated layer measurement.

**Exit condition.** Before/after recorded in INVESTIGATION §14.

---

# After this project

The long-lived-session follow-up has been implemented in the
`31-eager-open-layer` lane: `open()` materializes the initial code layer after
identity reconciliation, so repeated read-only graph calls use the carried
layer. The full startup/read tradeoff is recorded in INVESTIGATION §14.5.

The remaining sized follow-up is per-revision layer retention for time travel.
It costs ~12.83 MiB/revision and needs a retention bound; only pursue it if time
travel becomes a hot workload.
