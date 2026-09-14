# Project 30 — Snapshot code-layer cache

**Status: complete.** The carried `Arc<CodeLayer>` path is landed. This overview
is current where it describes that behavior; the original measurements and
implementation sequence remain as historical evidence below.

Project 30 stopped fresh snapshots from rebuilding the entire code layer from
scratch. The commit already computes it, and snapshots now carry and serve that
layer when the revision matches; the full rebuild remains the safe fallback.

The original pre-implementation probe on TyO3's own `src/tyo3` (74 files,
3258 nodes, 32849 edges) measured the redundant rebuild at **2.0s of the 2.8s**
a first `snapshot.graph()` cost.

## Why this mattered (before Project 30)

Before this project, `_RecordingContext.__init__` called `snapshot.graph()`
(`src/tyo3/session/views.py:47`), and **every traced derived production on a
fresh snapshot paid a full project rebuild before the producer did any work**.
For the LLM, embedding and summarisation producers the derived-layer machinery
exists to serve, this was pure dead weight on every revision.

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
- The snapshot now serves the carried layer through the shared helper
  (`rust/src/project.rs:291`); only a legitimate miss rebuilds against an
  **empty** prev (`rust/src/project.rs:291-303`).

So matching snapshots no longer recompute a value the commit already produced
and verified; the rebuild remains only as a correctness-preserving fallback.

## The trade-off

`TyProjectState` (`rust/src/project.rs:81`) is the cheap read clone behind every
snapshot. It carries an optional `Arc<CodeLayer>`: matching revisions share the
layer, while time-travel and other legitimate misses use the full-rebuild
fallback. Carrying one costs memory per distinct pinned revision.

The original arithmetic estimate was **~6 MB** per distinct revision. Project 30
measured a repository-scale retained layer at **12.83 MiB**. With `Arc`,
snapshots at the same revision share one allocation, so the bound is *live
revisions*, not *live snapshots*; the current memory caveat is recorded in
Project 31's investigation §14.2.

The estimate and the measurement-first instruction are historical; the design
was measured and landed. Any future retention policy still needs a bounded
memory decision.

## Files

- [DESIGN.md](DESIGN.md) — the measurements, the mechanism, the memory question,
  and the two candidate designs.
- [IMPLEMENTATION.md](IMPLEMENTATION.md) — step-by-step guide.

## Historical status

- 2026-09-14 — Initial report written; measurements taken on `main` at
  `314371c8` (post-project-29).
- 2026-09-14 — Project completed and landed. See §1.4 for the after
  measurements and Project 31 for the later eager-open follow-up.

## Related

- Project 29 (`.scratch/projects/29-semantic-plane-cleanup/`) — DESIGN §4.4
  first identified this defect and sized it as its own project. Landed.
- Project 31 — the completed semantic-state investigation. It introduced no
  `SemanticState` type; see [its overview](../31-semantic-state/README.md).
