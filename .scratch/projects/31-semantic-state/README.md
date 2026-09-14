# Project 31 — `SemanticState`, investigated

**Outcome: no new type. Document only.**

Project 29 §5 deferred a `SemanticState { identities, code }` on HEAD until
after project 30, "because caching the layer on snapshots may reshape the
boundary". It did. This project traced the post-30 code and concluded that the
boundary project 31 was going to name is already correct, already enforced, and
cannot be improved by a wrapper type.

Read [INVESTIGATION.md](INVESTIGATION.md) for the evidence. The three findings
that decide it:

1. **The pair cannot be published as one value.** The code layer is produced
   *from* the already-reconciled registry (`commit.rs:905` → `:915-916` →
   `analysis.rs:123` → `convert/symbols.rs:99-102`). A `SemanticState` on HEAD
   would still be mutated field-by-field.
2. **A wrapper would turn a benign inconsistency into a false claim.** Inside the
   commit, the read clone at `commit.rs:915` carries an R−1 layer beside an R
   registry (`project.rs:193`). Nobody reads it; two named fields say nothing,
   one struct named `SemanticState` would say something untrue.
3. **Memory forbids any owned duplicate.** Measured: 2.05 MiB per snapshot at the
   same revision (layer `Arc`-shared) versus 13.42 MiB per distinct revision.
   `Arc` sharing is worth ~11.4 MiB per extra same-revision snapshot.

## What is recommended instead

Design A — three small lanes, no new types (INVESTIGATION §9, §12):

- **Step 0** — comment corrections. `project.rs:94-98` omits the time-travel
  `None` producer, and project 29's "lazily produced layer" is no longer true:
  the layer is eagerly carried and the fallback memoises nothing.
- **Step 1** — extract `full_code_delta_for`. `snapshot.rs:61-76` and
  `methods.rs:675-690` are verbatim twins, and the `methods.rs` copy is the
  parity oracle's input.
- **Step 2** — extract `HeadState::servable_code_layer`. The "an empty layer is a
  cache miss" rule is stated twice (`project.rs:193`, `methods.rs:611-615`).

## Reproducing the measurements

```sh
devenv shell -- bash -c 'cd "$DEVENV_ROOT" && PYTHONPATH=src python \
  .scratch/projects/31-semantic-state/probe_timing.py .'
devenv shell -- bash -c 'cd "$DEVENV_ROOT" && PYTHONPATH=src python \
  .scratch/projects/31-semantic-state/probe_memory.py'
devenv shell -- bash -c 'cd "$DEVENV_ROOT" && PYTHONPATH=src python \
  .scratch/projects/31-semantic-state/probe_read_clone.py . repo-root'
```

Verified baseline at trunk `aa87019`: **171 Rust tests, 833 Python tests,
8 parity-oracle tests**, `check-rust` and `clippy -D warnings` clean.

## Files

- [INVESTIGATION.md](INVESTIGATION.md) — the full report: ownership table,
  data-flow diagram, verified invariants, design comparison, rejected designs
  with reasons, migration plan, test obligations, measurements, risks.
- `probe_timing.py` / `probe_memory.py` / `probe_read_clone.py` — the three
  reproducible measurements.

## Status

- 2026-09-14 — Investigated. Decision: **document only**. Design A's three steps
  are specified but **not started**.

## Related

- `.scratch/projects/29-semantic-plane-cleanup/` — DESIGN §5 proposed this
  project. **That section is superseded**; see INVESTIGATION §6.2.
- `.scratch/projects/30-snapshot-layer-cache/` — the carried `Arc<CodeLayer>`.
  Its DESIGN §4.1 memory figures are independently reproduced here (§14.2).
- **Naming collision:** `Project 31, #1b/#2/#3` in eleven places across
  `src/tyo3` and `rust/src` refers to the *v2 concept document's* project 31
  (retiring the per-commit structural delta), not this one. Do not renumber
  those citations.
