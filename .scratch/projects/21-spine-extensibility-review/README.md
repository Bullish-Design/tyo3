# Project 21 — TyO3 spine extensibility review

A full-stack review + design engagement: how good is the TyO3 "spine" as a backend
for **attaching arbitrary user-defined data to any AST node**, and how do we make
"attach anything" trivial via a programmatic Python API? See `REVIEW_BRIEF.md` for
the charter.

## Deliverables

1. **[`ASSESSMENT.md`](ASSESSMENT.md)** — how well the spine supports
   attach-anything + the nvim workflow *today*. End-to-end trace, per-subsystem
   findings (each with `file:line` evidence), the rubric table, and a plain-English
   verdict.
2. **[`ROADMAP.md`](ROADMAP.md)** — prioritized improvements (impact × effort):
   quick wins (expose `convert/` RPCs, identity-preserving rename, layer discovery,
   shared-snapshot cards) vs. architectural bets (the registration API, a richer
   producer protocol, async serve, read concurrency), with sequencing and risks
   vs. the §12 landmines.
3. **[`API_DESIGN.md`](API_DESIGN.md)** — the concrete Python registration API:
   `register_layer/generator/store`, the `AuthoredLayerSpec`/`DerivedLayerSpec` and
   `Producer`/`Store` protocols, how it reaches the daemon + editor, migration from
   the config-string model, and worked examples (`tests`, `references`,
   `diagnostics`, embeddings) ending in a ~20-line "define a new attachment"
   narrative.
4. **[`IMPLEMENTATION_GUIDE.md`](IMPLEMENTATION_GUIDE.md)** — a step-by-step build
   plan for an intern to implement everything in the roadmap + API design: every
   task as Goal → Why → Files → Steps (with code skeletons) → Test → Acceptance →
   Landmines → Estimate, phased (A quick wins / B registration spine / C powerful
   + fast), with a suggested 11-PR sequence, contract tests, and the cost×direction
   decision rule for layer authors.
5. **[`ARCHITECTURE_DELTA.md`](ARCHITECTURE_DELTA.md)** — the codebase architecture
   *after* the work lands: before→after by subsystem, the new components, a
   module-level change inventory, and the invariants deliberately preserved.
6. **[`KICKOFF_PROMPT.md`](KICKOFF_PROMPT.md)** — a self-contained brief to paste
   into a fresh session to start the implementation (reading order, branch +
   baseline, ground rules, task order, when to pause for review).
7. **[`SPIKE_FINDINGS.md`](SPIKE_FINDINGS.md)** — six throwaway spikes run against
   the live engine + daemon that empirically resolve the review's open questions
   (custom layers work config-only; the card auto-includes them; **the
   reverse-dependency "references-as-a-layer" correctness wall**; **the identity
   matrix that overturned the first-pass rename quick-win**; `find_references`
   returns callers; the derived render bug). Two results changed prior
   recommendations — the assessment/roadmap/API doc are updated accordingly.
   Scripts under `spikes/` (throwaway, untracked).

## The one-paragraph finding

The spine is **real and already half-generic**: durable identity, a uniform
`LayerView` read protocol, content-addressed invalidation with per-layer locality,
and — crucially — an `entity_at` card and nvim panel that **enumerate arbitrary
layers automatically**. Adding a value-shaped attachment over an existing mechanism
is config-only and touches zero core files. The gap is exactly the §2 vision:
**there is no programmatic registration API**; custom generator *types* and store
*backends* require editing core dispatch (`make_generator`, `open_store`); values
are free-form; the rich Rust `convert/` surface (references/hover/rename) is
exposed to Python but **stranded behind the daemon**; and every *author/discover*
path is hardcoded to the three built-in layers even though *surfacing* is generic.
The design centers on giving the write/produce/discover path the same generality
the read/serve/card path already has.

## Method & ground rules followed

- **Read-only review.** No engine/plugin code was modified. Findings are backed by
  traced call paths and `file:line` citations, not vibes.
- **Baseline** (`devenv shell -- test-fast`, branch `nvim-plugin`): 681 passed, 1
  failed, 1 error in 145s. The failure
  (`test_gate4_config_sidecar::test_sidecar_is_sole_path_owner_in_source`) is
  **pre-existing and unrelated** — `demo/tour.py:175` constructs a `.tyo3` path the
  invariant test wants confined to the `Sidecar` helper (committed `05e609a`). The
  concurrency `test_document_symbols` error is a separate fixture issue. Neither
  touches the spine code under review. Native extension built and verified
  (`check-so` ✅). Details in `ROADMAP.md` §Baseline note.
- **Spikes were run** (throwaway, against the live engine/daemon on temp projects;
  scripts under `spikes/`, untracked — no engine/plugin code modified). They
  confirmed the static tracing *and* surfaced two findings that overturned
  first-pass recommendations (the rename quick-win and the references-layer
  invalidation). See `SPIKE_FINDINGS.md`. The "what core files must you touch"
  answer holds: the four hardpoints are `generators.py::make_generator`,
  `stores/__init__.py::open_store`, `handlers.py::_METHODS`, and the Lua author
  call sites.
