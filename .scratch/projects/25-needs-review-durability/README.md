# Project 25 — `needs_review` durability (E + A)

Make the durable-identity **review state** (`needs_review`) a *persistent,
trustworthy* signal instead of the current transient, edge-triggered one, so it
can back a navigable `vim.diagnostic` (the Phase-2 LSP bridge layer namespace),
`authored().status`, and the CLI tour's "note flagged for review" claim.

## Why

Surfacing review-state as a native diagnostic (proj 24, Phase 2) exposed that the
engine's `needs_review` is an **edge signal** ("this entity changed in the most
recent commit that reconciled its file"), not **level state** ("this note is
stale until a human re-approves it"). Concretely, a flag set by an edit is
*cleared* by: saving (identical re-commit), editing a different function in the
same file, or any reconcile that re-settles the entity — while re-authoring the
note (the intuitive "I reviewed it" action) does **not** clear it. See
[DESIGN.md](DESIGN.md) §1 for the verified mechanism and code anchors.

## Scope — two parts

- **E (plugin, low-risk, do first):** stop the editor write path from
  double-committing identical overlay text (debounced `TextChanged` sync *then*
  `:w`/BufWritePost sync). Dedup on a per-buffer text hash. Removes the most
  common accidental clear and is a free efficiency win. Pure Lua.
- **A (engine, the real fix):** anchor review-state to the entity body **as it
  was when the note was authored**. Add a `reviewed_hash` to the authored record,
  stamped at author time; compute `needs_review` as `current_anchor_hash !=
  reviewed_hash` (level-triggered). Re-authoring re-stamps it (= acknowledge).
  Rust + format-version migration + Python API + tests.

After A, the layer-state diagnostic, `authored().status`, and the tour all become
trustworthy, and the demo coda can stop avoiding `:w`.

## Files

- [DESIGN.md](DESIGN.md) — full analysis: the mechanism (with `file:line`
  anchors), the edge-vs-level reframe, the detailed E and A designs, edge cases,
  the format migration, the decoupling of the bus edge-signal from the persistent
  state, the test matrix, sequencing, and risks.
- [KICKOFF.md](KICKOFF.md) — a self-contained implementation prompt to start a
  clean session.

## Status

- 2026-06-10 — Written. Not started. Decision: **doing E + A** (B is the documented
  fallback if the format migration is deferred).

## Related

- proj 24 (native LSP bridge): the layer-state `vim.diagnostic` namespace that
  consumes `needs_review` — `.scratch/projects/24-native-lsp-integration/`,
  PR #14 (`native-lsp-phase2`).
- Memory: `needs-review-cleared-by-noop-recommit`, `native-lsp-bridge-shipped`,
  `durable-identity-binding-rules`, `spine-refactor-v2-plan` ("Rust owns
  committed truth").
