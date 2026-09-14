# Project 29 — Semantic-plane cleanup

Three behaviour-preserving cleanups to the native semantic plane
(`IdentityRegistry` + `CodeLayer`), plus one verified bug fix. Groundwork for
the later Jujutsu-context and semantic-persistence work.

This project is the **rescoped** answer to the external
`tyo3_v2_refactoring_concept.md` proposal (Phases 1 and 4 of that document).
Read [DESIGN.md](DESIGN.md) for why the original Phases 1 and 4 do not survive
contact with the code, and what replaces them.

## Why

The v2 concept document proposes merging `IdentityRegistry` and `CodeLayer` into
one `SemanticState` holding one `EntityRecord` per `DurableId`. Tracing the code
shows the premise is wrong: the two are not two views of one thing. They are a
pipeline stage and its output, with different key sets, different lifetimes,
different persistence, and deliberately different semantics (last-known facts vs
current-revision facts). Merging them deletes the distinction reconciliation
depends on.

What *is* wrong in this area is smaller, more concrete, and partly a live bug.
See [DESIGN.md](DESIGN.md) §2.

## Scope — three steps, in order

- **Step 1 — Explicit entity populations.** The code layer holds three distinct
  id populations (real entities, synthetic `<module>` nodes, external stubs)
  distinguished today by ad-hoc string-shape guesses. The guess is **wrong for
  external stubs**, and `session.code.ids()` leaks them as entities. Fix at the
  source: emit an explicit discriminator from Rust; make Python read it.
  *Rust + Python. Contains the one real bug fix in this project.*
  **Python half landed 2026-09-13** (the bug fix + 12 tests, suite at 828). The
  Rust half — naming the sentinels, the `Population` enum — is outstanding.

- **Step 2 — De-`Option` the identity registry.** `TyProjectState.registry` is
  `Option<IdentityRegistry>`, but every production path passes `Some`. Costs 25
  unwrap sites across six files. Make it a plain, default-empty
  `IdentityRegistry`. *Rust only. Mechanical.*

- **Step 3 — `reverse_deps` derivability invariant.** The scoped in-commit
  producer maintains `reverse_deps` edge-by-edge and never rebuilds it. Nothing
  checks it still equals what the canonical edge set implies. If it drifts,
  `affected_ids` under-fires and derived artifacts go **silently stale** — the
  worst failure class in this system. Add the derivation, a debug assertion, and
  a randomized-edit test. *Rust + test. Closes a silent-corruption risk.*

## Explicitly not in scope

- Merging `Anchor` and `NodeData` into one record (DESIGN §3 — investigated and
  rejected, with evidence).
- A `SemanticState` struct (DESIGN §5 — deferred to project 31, after 30 tells
  us what the boundary should be).
- Caching the produced `CodeLayer` on snapshots (DESIGN §4 — the one change here
  with real performance impact; sized as its own project 30).
- Jujutsu integration (DESIGN §6 — decided: use `pyjutsu`, Python-side, optional
  extra; own project).

## Files

- [DESIGN.md](DESIGN.md) — the verified analysis: what the two structures really
  are, the four asymmetries, the reproduced bug, why the doc's Phase 1/4 are
  rejected, and the deferred work with sizing.
- [IMPLEMENTATION.md](IMPLEMENTATION.md) — the step-by-step guide. Follow it in
  order; each step lands independently and green.
- [KICKOFF.md](KICKOFF.md) — a self-contained prompt to start a clean session.

## Status

- 2026-09-13 — Written.
- 2026-09-13 — **Step 1, Python half landed.** `is_entity_node` added, all
  callers moved, `tests/test_entity_populations.py` added. Suite 828 green,
  parity-oracle green. Uncommitted in the working tree: `gitman` was not on
  PATH. Remaining: Step 1 Rust half, Step 2, Step 3.

## Related

- `tyo3_v2_refactoring_concept.md` (external proposal, reviewed in DESIGN §1).
- proj 31 in that document's numbering (`Project 31, #1b/#2`) — the decision to
  retire the per-commit structural delta and build graphs on demand. Step 3 here
  guards the index that decision left as the sole in-commit structural state.
- `tests/parity_oracle.py` — the tiered structural/cosmetic oracle. The safety
  net for all three steps.
