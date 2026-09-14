# Project 30 — Snapshot code-layer cache

Stop every fresh snapshot from rebuilding the entire code layer from scratch.
The commit already computed it; the snapshot throws it away and recomputes.

Measured on TyO3's own `src/tyo3` (74 files, 3258 nodes, 32849 edges), the
redundant rebuild is **2.0s of the 2.8s** a first `snapshot.graph()` costs.

## Why this matters

`_RecordingContext.__init__` calls `snapshot.graph()`
(`src/tyo3/session/views.py:47`). **Every traced derived production on a fresh
snapshot therefore pays a full project rebuild before the producer does any
work** — even a producer that reads nothing but its own entity. For the LLM,
embedding and summarisation producers the derived-layer machinery exists to
serve, this is pure dead weight on every revision.

The v2 concept document never mentions it. It is the largest single performance
defect found while reviewing that proposal.

## Why it is safe

The work is already done and already correct:

- `produce_code_delta_scoped` documents its output as "byte-identical to a full
  `Builder::build` over the same final content" (`rust/src/code_layer.rs:525`) —
  that is the parity oracle's invariant, and project 29 Step 3 now asserts a
  related one (`reverse_deps` derivability) on every debug run.
- The commit stores that layer at `rust/src/project/commit.rs:948`
  (`head.code_layer = next`).
- The snapshot rebuilds it against an **empty** prev
  (`rust/src/project/snapshot.rs:58`), i.e. a full `Builder::build`.

So the snapshot is recomputing a value the commit already produced and verified.

## The trade-off

`TyProjectState` (`rust/src/project.rs:81`) is the cheap read clone behind every
snapshot. It currently carries **no** code layer — that is what makes it cheap.
Carrying one costs memory per pinned revision.

Rough estimate for this project: **~6 MB** per distinct revision with a live
pinned snapshot (32849 edges × ~165 B + 3258 nodes × ~250 B). With `Arc`,
snapshots at the same revision share one allocation, so the bound is *live
revisions*, not *live snapshots*.

**That number is an estimate, not a measurement. Measure it before committing to
the design** — see [DESIGN.md](DESIGN.md) §4. It is the only real risk here.

## Files

- [DESIGN.md](DESIGN.md) — the measurements, the mechanism, the memory question,
  and the two candidate designs.
- [IMPLEMENTATION.md](IMPLEMENTATION.md) — step-by-step guide.

## Status

- 2026-09-14 — Written. Not started. Measurements taken on `main` at `314371c8`
  (post-project-29).

## Related

- Project 29 (`.scratch/projects/29-semantic-plane-cleanup/`) — DESIGN §4.4
  first identified this defect and sized it as its own project. Landed.
- Project 31 (proposed, in 29's DESIGN §5) — the corrected `SemanticState`
  boundary. Do it **after** this project: caching the layer on snapshots may
  reshape where that boundary belongs.
