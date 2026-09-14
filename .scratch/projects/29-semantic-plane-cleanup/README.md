# Project 29 — Semantic-plane cleanup

**Status: complete.** This project and its three implementation steps are
landed. The current follow-on decisions are recorded in [Project 31's
overview](../31-semantic-state/README.md).

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

What was wrong in this area was smaller and more concrete: one entity-population
classification bug and two native invariants that needed to be made explicit.
All three were addressed. See [DESIGN.md](DESIGN.md) §2 for the historical
evidence.

## Completed scope

- **Step 1 — Explicit entity populations.** The code layer's three distinct id
  populations (real entities, synthetic `<module>` nodes, external stubs) were
  classified by ad-hoc string-shape guesses. The external-stub guess was wrong,
  and `session.code.ids()` leaked stubs as entities. The source now emits an
  explicit `Population` discriminator and Python reads it, with regression
  coverage for the leak.

- **Step 2 — De-`Option` the identity registry.** `TyProjectState.registry` was
  an unnecessary `Option<IdentityRegistry>` with 25 unwrap sites across six
  files. It is now a plain, default-empty `IdentityRegistry`.

- **Step 3 — `reverse_deps` derivability invariant.** The scoped in-commit
  producer maintains `reverse_deps` edge-by-edge, and the canonical derivation,
  debug assertions, and randomized-edit coverage now guard against silently
  stale derived artifacts.

## Explicitly not in scope

- Merging `Anchor` and `NodeData` into one record (DESIGN §3 — investigated and
  rejected, with evidence).
- A `SemanticState` struct (DESIGN §5 — investigated and rejected by project
  31; see its [settled decision](../31-semantic-state/README.md)).
- Caching the produced `CodeLayer` on snapshots (DESIGN §4 — completed by
  project 30; the remaining time-travel retention question is recorded by
  project 31).
- Jujutsu integration (DESIGN §6 — decided: use `pyjutsu`, Python-side, optional
  extra; own project).

## Files

- [DESIGN.md](DESIGN.md) — the verified analysis: what the two structures really
  are, the four asymmetries, the reproduced bug, why the doc's Phase 1/4 are
  rejected, and the deferred work with sizing.
- [IMPLEMENTATION.md](IMPLEMENTATION.md) — the step-by-step guide. Follow it in
  order; each step lands independently and green.
- [KICKOFF.md](KICKOFF.md) — a self-contained prompt to start a clean session.

## Historical status

- 2026-09-13 — Written.
- 2026-09-14 — All three steps landed and the working-tree loss risk was
  resolved through the gitman workflow. Use current source and the linked
  Project 31 overview for present behavior.

## Related

- `tyo3_v2_refactoring_concept.md` (external proposal, reviewed in DESIGN §1).
- proj 31 in that document's numbering (`Project 31, #1b/#2`) — the decision to
  retire the per-commit structural delta and build graphs on demand. Step 3 here
  guards the index that decision left as the sole in-commit structural state.
- `tests/parity_oracle.py` — the tiered structural/cosmetic oracle. The safety
  net for all three steps.
