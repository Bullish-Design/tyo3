# KICKOFF — Semantic-plane cleanup (project 29, historical)

> Project 29 is complete. This file is retained as the original execution
> brief and is not a current work order. See [README.md](README.md) for status
> and [Project 31](../31-semantic-state/README.md) for the later semantic-state
> decision.

You are working in **TyO3**: a Python semantic engine built on Astral `ty` +
Salsa, with a Rust/PyO3 core (durable code identity, a native code layer, layered
annotations, a delta bus, a daemon, and a Neovim plugin).

**Read these two files before you touch anything:**

- `.scratch/projects/29-semantic-plane-cleanup/DESIGN.md` — the verified
  analysis. Every claim has a `file:line` anchor or a reproduction.
- `.scratch/projects/29-semantic-plane-cleanup/IMPLEMENTATION.md` — the
  step-by-step guide. Follow it in order.

This file is the actionable summary. Where it and IMPLEMENTATION.md differ,
IMPLEMENTATION.md wins.

## What this project is

Three behaviour-preserving cleanups to the native semantic plane
(`IdentityRegistry` + `CodeLayer`), one of which fixes a verified bug.

It is the **rescoped** answer to an external proposal,
`tyo3_v2_refactoring_concept.md`, which asked for Phases 1 and 4: merge the two
structures into one `SemanticState` holding one `EntityRecord` per `DurableId`.
That premise was traced and **rejected** — the two structures are a pipeline
stage and its output, not two views of one thing, and merging them deletes the
last-known-vs-current distinction reconciliation depends on. DESIGN §2–3 has the
evidence. Do not re-litigate it; do not implement the merge.

## The three steps

### Step 1 — Explicit entity populations (**Python half already landed**)

> Historical starting state: the Python half of the bug fix was **done** and
> initially sat uncommitted in the working tree:
> `is_entity_node` added, every caller moved, `tests/test_entity_populations.py`
> added. Suite **828 passed**, parity-oracle green. **Only the Rust half is
> left** — IMPLEMENTATION §1.3: name the `<module>` / `<external>` sentinels into
> constants, add the `Population` enum and `NodeData::population()`, and replace
> the ten scattered string literals. That is what stops the guess reappearing.


The code layer holds three id populations: real entities (ULIDs), synthetic
module nodes (`"<module>" + file`), and external stubs (`"package::name"`,
`file == "<external>"`). Python classifies them by string prefix
(`src/tyo3/graph/identity.py:68`), and **that guess is wrong for external
stubs** — their ids carry no prefix at all. Result:
`session.code.ids()` leaks external stubs as entities, contradicting its own
docstring.

Reproduce it first (IMPLEMENTATION §1.2), then:
- Rust: centralize `<module>` / `<external>` sentinels into named constants and
  constructors in `rust/src/code_layer.rs`; add a `Population` enum and
  `NodeData::population()`.
- Python: add `is_entity_node(durable_id, *, external)` — `external` is the only
  reliable discriminator and only the producer knows it. Narrow
  `is_entity_durable_id` to "not a module id" and say so. Move every caller onto
  the node-based check. Delete the dead `startswith("<external>")` branch in
  `file_from_durable_id`.
- Tests: unit + the leak regression + `value()` gate + layer diff.

### Step 2 — De-`Option` the identity registry (Rust only; mechanical)

`TyProjectState.registry: Option<IdentityRegistry>` (`rust/src/project.rs:84`)
but every production path passes `Some`. 25 unwrap sites across six files. Make
it a plain default-empty `IdentityRegistry`.

Safe because `Some(&empty)` is behaviourally identical to `None` — verified at
`rust/src/convert/symbols.rs:94`, where `registry.and_then(|r| r.by_path(...))`
short-circuits either way. The full site list is in IMPLEMENTATION §2.2.

### Step 3 — `reverse_deps` derivability invariant (Rust + test)

`CodeLayer.reverse_deps` is maintained edge-by-edge and **never rebuilt**. It is
the sole input to the affected-set closure, which drives derived-layer
invalidation. If it drifts, `affected_ids` under-fires and derived artifacts go
**silently stale**. Nothing checks it after a real scoped incremental commit.

Add `CodeLayer::derived_reverse_deps()` (the canonical definition, derived from
`edges`), assert equality under `#[cfg(debug_assertions)]` on both producer
paths, and add a multi-generation Rust test plus a Python commit-path test.
Because the suite runs debug builds, this turns every existing test into a check
of the invariant.

Do not skip generation 3 of the Rust test (removing a `References` edge while an
`Inherits` edge between the same pair survives) — that is the `remove_edge`
parallel-edge pruning case, the specific fragility this step exists to guard.

## Working rules

- **Land each step independently and green.** Do not start the next until the
  previous one is merged. One gitman lane per step.
- **Do not change `CodeNodeDto`'s wire shape.** Its fields are in the parity
  oracle's strict structural tier. All three steps are achievable without it.
- **`devenv shell -- parity-oracle` must stay green** after every step.
- The suite is **816 tests**. Record the baseline before you start; the count
  must not drop.
- The v2 document's §7.1 (`SemanticDelta` carrying `edges_added`/`edges_removed`)
  and §8.2–8.3 (incremental projection + cold-rebuild parity) reinstate machinery
  that `Project 31, #1/#2` deliberately retired — see
  `rust/src/dto/commit_delta.rs:20` and `src/tyo3/graph/applier.py:88`. **Out of
  scope. Do not adopt them.**

## Commands

```sh
devenv shell -- check-rust        # fast type-check, ~1s — the Rust edit loop
devenv shell -- clippy            # -D warnings, matches the CI gate
devenv shell -- build             # maturin develop (~17s) — needed to run Python
devenv shell -- test-fast         # the fast Python suite
devenv shell -- tests             # cargo test + full Python suite
devenv shell -- parity-oracle     # the tiered structural/cosmetic oracle
```

## Out of scope (already sized, later projects)

- **Project 30** — cache the produced `CodeLayer` on snapshots. Every traced
  derived production on a fresh snapshot currently pays a full project rebuild
  (`rust/src/project/snapshot.rs:58`, `src/tyo3/session/views.py:44`). Real
  performance win; own project.
- **Project 31** — investigated the proposed corrected `SemanticState` and
  rejected the new type. See `../31-semantic-state/README.md`; its current
  eager-open and carried-layer behavior is authoritative.
- **Jujutsu context** — decided: use `../pyjutsu` (in-process `jj-lib` binding),
  not `../gitman` (a workflow writer, wrong layer). Python-side adapter at
  `src/tyo3/integrations/jujutsu.py`, optional extra `tyo3[jj]`. DESIGN §6.
- **Merging `Anchor` and `NodeData`** — investigated and rejected. DESIGN §3.

## Start here

1. `devenv shell -- tests` — confirm the baseline is green. Expect **828**
   (816 plus the 12 from the landed Python half).
2. Read DESIGN.md, then IMPLEMENTATION.md.
3. Run the §1.2 reproduction. It must return the **ULID only** — that confirms
   the landed fix. If it still leaks stubs, the working tree was reverted; stop
   and say so.
4. Begin at IMPLEMENTATION §1.3 (the Rust half of Step 1).
   Note: `gitman` was not on PATH last session — check before assuming a lane.
