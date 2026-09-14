# IMPLEMENTATION — Snapshot code-layer cache (project 30)

Read [DESIGN.md](DESIGN.md) first. Design A (carry an `Arc<CodeLayer>` on the
read clone) is the chosen approach; §3.2 records why B was rejected.

## Commands

```sh
devenv shell -- check-rust        # fast type-check, ~1s
devenv shell -- clippy            # -D warnings, matches the CI gate
devenv shell -- build             # maturin develop (~17s)
devenv shell -- tests             # cargo test + full Python suite
devenv shell -- parity-oracle     # MUST stay green — this project is a pure speed-up
```

Baseline on `main` at `314371c8`: **170 Rust tests, 833 Python tests**, parity
oracle green. Record the counts; they must not drop.

---

## Step 0 — Measure the memory first (do not skip)

DESIGN §4 estimates ~6 MB per revision by arithmetic. **Confirm it before
writing the feature.** If the real number is far higher, stop and revisit
Design B.

Add a temporary `#[cfg(test)]` probe or a one-shot binary that builds the layer
for `src/tyo3` and reports the resident size of `CodeLayer` — nodes, edges, and
the two indexes separately, so you know which dominates.

Also record: what a pinned snapshot already costs today (it holds a
`ProjectDatabase` clone). The layer's share of the total is the number that
matters, not its absolute size.

**Exit condition.** A measured per-revision figure, and a decision on whether to
bound live pinned revisions.

---

## Step 1 — `HeadState.code_layer` becomes `Arc<CodeLayer>`

`rust/src/project.rs:135` and the commit path.

1. `pub(crate) code_layer: Arc<CodeLayer>` on `HeadState`.
2. `rust/src/project/commit.rs:948` — `head.code_layer = Arc::new(next);`
3. `commit.rs` takes the prior layer with `std::mem::take`; with an `Arc` use
   `Arc::clone(&head.code_layer)` and pass `&*prev` to the producer. The
   producer signature (`&CodeLayer`) does not change.
4. The rollback `Baseline` (`commit.rs`, `code_layer: head.code_layer.clone()`)
   becomes an `Arc` clone — cheaper than today's deep clone, and a free win on
   every commit.

No behaviour change. `devenv shell -- check-rust`, then `tests`.

**Exit condition.** Suite green, counts unchanged. Commit this separately — it
is a safe refactor and isolates any `Arc` fallout from the feature itself.

---

## Step 2 — Carry the layer on the read clone

1. `rust/src/project.rs:81` — add to `TyProjectState`:

```rust
/// The committed code layer for this state's revision, when one exists.
///
/// `None` only for a read clone taken before any commit has produced a layer
/// (and in test constructors). A `Some` lets `full_code_delta` serve the
/// committed layer instead of rebuilding it; `None` falls back to the full
/// `Builder::build`, so this can only ever cost speed, never correctness.
pub(crate) code_layer: Option<Arc<CodeLayer>>,
```

2. Both `ReadCloneSource` impls (`project.rs:162`, `:178`) clone the `Arc`.
   `TyProjectState::read_clone` propagates whatever it holds; `HeadState::read_clone`
   passes `Some(Arc::clone(&self.code_layer))`.
3. Every other `TyProjectState` construction gets `code_layer: None`. Grep for
   `TyProjectState {` — the entity-extraction state in `commit.rs` and the test
   helper in `code_layer.rs` are the ones to expect.

**Exit condition.** Compiles, suite green. Still no behaviour change — nothing
reads the new field yet.

---

## Step 3 — Serve the carried layer

`rust/src/project/snapshot.rs:58` (`Snapshot::full_code_delta`) and the head
accessor at `rust/src/project/methods.rs` (`PyTyProject::full_code_delta`).

```rust
let empty = CodeLayer::new();
let delta = match state.code_layer.as_deref() {
    Some(layer) => layer.diff_from(&empty, revision, true),
    None => produce_code_delta(&state, &empty, revision, true, None).1,
};
```

Keep the `py.detach(...)` wrapper — `diff_from` over 33k edges is not free and
must not hold the GIL.

**The head accessor is the parity oracle's input.** Its output must be
byte-identical either way. That is exactly what `parity-oracle` checks, so run
it before anything else.

**Exit condition.** `devenv shell -- parity-oracle` green. Suite green.

---

## Step 4 — Prove equivalence directly

The parity oracle compares against the legacy Python builder. Add a test that
compares the two *native* paths against each other, which is the specific
invariant this project relies on:

In `rust/src/code_layer.rs` tests, over a multi-file fixture and several edit
generations:

```
produce_code_delta(state, EMPTY, rev, true, None).0     // full rebuild
    ==
the layer carried from produce_code_delta_scoped(...)   // committed layer
```

Assert equality of `nodes`, `edges`, `reverse_deps` and `file_to_nodes`. Reuse
`reconciled_state_at_in` from project 29 Step 3's test — it already threads a
registry across generations.

This is the assertion that makes Step 3 safe, and it is cheap because the
fixture harness already exists.

**Exit condition.** The equivalence test passes across at least five
generations including a file deletion.

---

## Step 5 — Re-measure

Repeat DESIGN §1.1 and §1.2 on the same project and record the new table in
DESIGN.md under a "§1.4 After" heading. Expected: `full_code_delta` drops from
~2.0s to the cost of `diff_from` alone; `apply_code_delta` (0.682s) and
`refresh_diagnostics` (0.110s) are unchanged.

If `full_code_delta` does not drop by roughly the predicted amount, the carried
layer is not being hit — check that the snapshot's `read_clone` actually
received a `Some`.

**Exit condition.** Measured before/after in DESIGN.md, and the memory figure
from Step 0 recorded alongside it.

---

## After this project

**Project 31 — the corrected `SemanticState`.** See
`.scratch/projects/29-semantic-plane-cleanup/DESIGN.md` §5. After this project,
the read clone carries identities *and* a layer, which is most of what that
boundary was going to name. Re-read §5 before starting: this project may have
already answered the question, or moved where the line belongs.
