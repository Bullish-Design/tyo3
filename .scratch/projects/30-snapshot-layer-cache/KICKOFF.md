# KICKOFF — Snapshot code-layer cache (project 30)

You are working in **TyO3**: a Python semantic engine built on Astral `ty` +
Salsa, with a Rust/PyO3 core (durable code identity, a native code layer,
layered annotations, a delta bus, a daemon, and a Neovim plugin).

**Read these first, in order:**

- `.scratch/projects/30-snapshot-layer-cache/DESIGN.md` — measurements, the
  mechanism, the memory question, and why Design A beat Design B.
- `.scratch/projects/30-snapshot-layer-cache/IMPLEMENTATION.md` — the
  step-by-step guide. Follow it in order.

This file is the actionable summary. Where it and IMPLEMENTATION.md differ,
IMPLEMENTATION.md wins.

## The defect, in one paragraph

Every commit produces a **complete** code layer — `produce_code_delta_scoped`
documents its output as "byte-identical to a full `Builder::build` over the same
final content" (`rust/src/code_layer.rs:525`) — and stores it at
`rust/src/project/commit.rs:948`. Every snapshot then **throws it away** and
rebuilds from an empty prev (`rust/src/project/snapshot.rs:58`), because
`TyProjectState` (`rust/src/project.rs:81`) carries no code layer.
`_RecordingContext.__init__` calls `snapshot.graph()` unconditionally
(`src/tyo3/session/views.py:47`), so **every traced derived production on a
fresh snapshot pays a full project rebuild before the producer runs** — even one
that reads nothing but its own entity.

Measured on `src/tyo3` (74 files, 3258 nodes, 32849 edges): the redundant
rebuild is **2.006s of the 2.798s** a first `snapshot.graph()` costs — 72%.

## The plan

**Design A:** carry an `Arc<CodeLayer>` on the read clone, and serve it from
`full_code_delta` instead of rebuilding. Keep the rebuild as the fallback when
the `Option` is `None`, so a miss can only ever cost speed, never correctness.

Design B (a Python-side per-revision memo) was considered and rejected — it
re-introduces the "graph as a second cache" shape that `Project 31, #1/#2`
deliberately removed. DESIGN §3.2.

## Do Step 0 first. Do not skip it.

DESIGN §4 estimates **~6 MB per live revision** by arithmetic, not measurement.
That is the only real risk in this project. **Measure the real resident size of
one `CodeLayer` at this scale before writing the feature.** If it lands far above
the estimate, stop and revisit Design B.

Also record what a pinned snapshot already costs today (it holds a
`ProjectDatabase` clone). The layer's *share* is the number that matters, not
its absolute size.

## Then, in order

1. **Step 1** — `HeadState.code_layer` becomes `Arc<CodeLayer>`. Pure refactor;
   also makes the rollback `Baseline` clone cheap. Land separately.
2. **Step 2** — add `code_layer: Option<Arc<CodeLayer>>` to `TyProjectState`;
   both `read_clone` impls propagate it. Nothing reads it yet.
3. **Step 3** — serve the carried layer from `Snapshot::full_code_delta` and the
   head accessor. Keep the `py.detach(...)` wrapper: `diff_from` over 33k edges
   must not hold the GIL. **The head accessor is the parity oracle's input** —
   run `parity-oracle` before anything else.
4. **Step 4** — prove the two native paths agree directly (full rebuild ==
   carried layer) across several edit generations including a deletion. Reuse
   `reconciled_state_at_in` from project 29 Step 3's test harness.
5. **Step 5** — re-measure and record a "§1.4 After" table in DESIGN.md.

## Working rules

- **`devenv shell -- parity-oracle` must stay green.** This project is a pure
  speed-up; any parity movement means Step 3 changed output, which is a bug.
- Baseline: **170 Rust tests, 833 Python tests**. Record before you start; the
  counts must not drop.
- Version control goes through **gitman**, which is now on PATH (repoman is
  wired as of `108e187`). One lane per step:
  `gitman start 30-step1` → work → `gitman save -m ...` → `gitman land` →
  `gitman push`. Never raw `jj`/`git`.
- **Commit your planning notes.** Project 29's docs were lost once because they
  sat uncommitted in an orphaned change. If you write notes, land them.

## Commands

```sh
devenv shell -- check-rust        # fast type-check, ~1s — the Rust edit loop
devenv shell -- clippy            # -D warnings, matches the CI gate
devenv shell -- build             # maturin develop (~17s)
devenv shell -- tests             # cargo test + full Python suite
devenv shell -- parity-oracle     # MUST stay green
```

## After this project

**Project 31 — the corrected `SemanticState`.** See
`.scratch/projects/29-semantic-plane-cleanup/DESIGN.md` §5. Re-read it before
starting: after this project the read clone carries identities *and* a layer,
which is most of what that boundary was going to name. This project may have
already answered the question, or moved where the line belongs.

**Jujutsu revision context.** Decided in 29's DESIGN §6: use `../pyjutsu`
(in-process `jj-lib` binding, reads publish no operation), not `../gitman`
(a workflow writer, wrong layer). Python-side adapter at
`src/tyo3/integrations/jujutsu.py`, optional extra `tyo3[jj]`. Independent of
30 and 31; can start any time.

## Start here

1. `devenv shell -- tests` — confirm 170 Rust + 833 Python, all green.
2. Read DESIGN.md, then IMPLEMENTATION.md.
3. Reproduce the measurement in DESIGN §1.1 so you have your own baseline.
4. `gitman start 30-step0` and do the memory measurement.
