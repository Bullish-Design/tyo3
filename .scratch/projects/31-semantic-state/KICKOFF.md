# KICKOFF — Semantic-state cleanup (project 31)

You are working in **TyO3**: a Python semantic engine built on Astral `ty` +
Salsa, with a Rust/PyO3 core (durable code identity, a native code layer,
layered annotations, a delta bus, a daemon, and a Neovim plugin).

**Read these first, in order:**

- `.scratch/projects/31-semantic-state/INVESTIGATION.md` — why there is **no**
  `SemanticState` type, with the evidence. Read §4 (invariants), §6 (what
  project 30 already solved) and §8 (rejected designs) before touching code.
- `.scratch/projects/31-semantic-state/IMPLEMENTATION.md` — the step-by-step
  guide. Follow it in order.

This file is the actionable summary. Where it and IMPLEMENTATION.md differ,
IMPLEMENTATION.md wins.

## The decision, in one paragraph

Project 29 §5 deferred a `SemanticState { identities, code }` until after
project 30. Project 30 moved the boundary: HEAD now owns an `IdentityRegistry`
plus an `Arc<CodeLayer>` (`rust/src/project.rs:120`, `:141`), the read state
carries identities plus an optional carried layer (`:84`, `:99`), and head
snapshots serve the committed layer through `CodeLayer::diff_from`. The
investigation concluded that **no wrapper type is justified** and that the
remaining work is comments plus two deduplications. Do not reopen the type
question without new evidence — §8 rejects Designs B, C and D individually.

## Why no type (the three findings you must not re-litigate)

1. **The pair cannot be published as one value.** The layer is produced *from*
   the already-reconciled registry: `commit.rs:905` mutates `head.registry`,
   then `:915-916` runs the producer, which reads `state.registry` via
   `analysis.rs:123` → `convert/symbols.rs:99-102`. A `SemanticState` would
   still be assigned field-by-field.
2. **A wrapper would state something false.** At `commit.rs:915` the read clone
   carries an R−1 layer beside an R registry (`project.rs:193`). Nothing reads
   it. Two named fields say nothing; one struct would claim they describe one
   revision.
3. **Memory forbids an owned duplicate.** Same-revision snapshots cost 2.05 MiB
   each because the `Arc` is shared; project 30 measured an isolated layer at
   12.83 MiB.

## The plan — three lanes, in order

1. **Step 0** — comment corrections only. `project.rs:94-98` omits the
   time-travel `None` producer and calls the fallback lazy when it memoises
   nothing. Zero behaviour risk; land it first.
2. **Step 1** — extract `full_code_delta_for`. `snapshot.rs:61-76` and
   `methods.rs:675-690` are identical **character for character**, and only the
   `methods.rs` copy is covered by the parity oracle. **Run `parity-oracle`
   before anything else** — `methods.rs` is its input.
3. **Step 2** — extract `HeadState::servable_code_layer`. Keep the `is_head`
   guard at the snapshot site; it is deliberate.
4. **Step 3** — re-measure with the three committed probes; record in
   INVESTIGATION §14.

## Working rules

- **No new struct, enum or trait.** If a step grows one, stop and re-read
  INVESTIGATION §8.
- **`devenv shell -- parity-oracle` must stay green.** Every step is a pure
  refactor or a comment; any parity movement is a bug.
- Baseline: **171 Rust tests, 833 Python tests, 8 parity-oracle tests**, clippy
  clean at trunk `aa87019`. Record before you start; counts must not drop.
- Version control goes through **gitman**. One lane per step:
  `gitman start 31-step0-docs` → work → `gitman save -m ...` → `gitman land` →
  `gitman push`. Never raw `jj`/`git`.
- **Commit your notes.** Project 29's docs were lost once because they sat
  uncommitted in an orphaned change.

## Commands

```sh
devenv shell -- check-rust        # fast type-check, ~1s — the Rust edit loop
devenv shell -- clippy            # -D warnings, matches the CI gate
devenv shell -- build             # maturin develop (~17s)
devenv shell -- parity-oracle     # MUST stay green
devenv shell -- tests             # cargo test + full Python suite
```

Note: this shell requires a secrets reason. Prefix with
`SECRETSPEC_REASON="<why>"` or export it once.

## Start here

1. `devenv shell -- tests` — confirm 171 Rust + 833 Python, all green.
2. Read INVESTIGATION.md §4, §6, §8, then IMPLEMENTATION.md.
3. Reproduce INVESTIGATION §6.3 with
   `PYTHONPATH=src python .scratch/projects/31-semantic-state/probe_timing.py .`
   so you have your own baseline.
4. `gitman start 31-step0-docs` and do the comment corrections.

## After this project

Two sized-but-unscheduled follow-ups (INVESTIGATION §15.2). The larger one —
read-only sessions never benefit from project 30, measured at 4.40 s per
`full_code_delta` call, unbounded — is **blocked on a decision about
architectural constraint 13**, not on engineering. Raise it before starting.
