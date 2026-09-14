# Project 31 — semantic-state cleanup

**Status: complete.** Landed on `main` and pushed on 2026-09-14. The final
implementation is the `31-eager-open-layer` follow-up, currently at trunk
`9118bb9f`.

## Outcome

Project 31 does not introduce a `SemanticState` type. The evidence showed that
the identity registry and code layer have different lifetimes, different key
sets, and a required production order; wrapping them would claim an invariant
the commit window does not provide. The approved cleanup landed: comments were
corrected, the duplicated full-delta path was extracted, the duplicated layer
hit rule was extracted, and non-empty projects now materialize the initial code
layer during `open()` for long-lived read performance.

## Current behavior

- `HeadState` owns a deep-cloned `IdentityRegistry` and an `Arc<CodeLayer>`.
- The initial non-empty `CodeLayer` is produced after open-time identity
  reconciliation; an empty project keeps the empty-layer miss behavior.
- Repeated head and same-revision snapshot graph reads use the carried layer.
- Time-travel snapshots intentionally rebuild because the head layer is valid
  for the head revision only.
- `TyProjectState.code_layer` remains `Option<Arc<CodeLayer>>`; no wire shape,
  Python projection, GIL boundary, or rollback contract changed.

At repository scale, the eager-open tradeoff is approximately 4.69 s paid once
at startup instead of approximately 4.4 s on every first/read-only full-delta
call. Repeated reads are approximately 0.17–0.19 s. The isolated layer-memory
reference remains approximately 12.83 MiB; see the memory confound note before
using RSS figures.

## What is next

The remaining performance candidate is bounded per-revision code-layer
retention for time travel. It should only become a project if real Neovim or
daemon usage shows that time-travel snapshots are frequent enough to justify
the memory cost and an eviction policy.

`apply_code_delta` remains a measured graph cost, but re-incrementalising the
wholesale Python projection is an explicit architectural constraint and is not
queued here.

The next investigation is the agent-facing control plane:
[Project 32 kickoff](../32-agent-control-plane/KICKOFF.md). It is a clean,
investigation-first brief for understanding the Python, daemon, bus, native,
and Neovim surfaces before proposing more API or lifecycle work.

## Read this project

- [INVESTIGATION.md](INVESTIGATION.md) — evidence, invariants, rejected designs,
  implementation outcome, and measurements.
- [IMPLEMENTATION.md](IMPLEMENTATION.md) — the historical step-by-step guide,
  now annotated with its completed status and the eager-open follow-up.
- [SPIKES.md](SPIKES.md) — the pre-implementation spike results.
- `probe_timing.py`, `probe_memory.py`, and `probe_read_clone.py` — repeatable
  probes; read the memory confound note in INVESTIGATION §14.2 first.
- [Project 32 KICKOFF](../32-agent-control-plane/KICKOFF.md) — the next clean
  investigation brief for agents working on TyO3’s control plane.

## Historical context

Project 29 §5 proposed a `SemanticState` after project 30. Project 30 instead
established the carried `Arc<CodeLayer>` boundary. Project 31 verified that the
boundary is already correct and deliberately kept the fields separate.

References in source comments such as `Project 31, #1b/#2/#3` belong to the
separate v2 concept-document numbering for retiring the per-commit structural
delta. They are not references to this scratch project and must not be
renumbered.
