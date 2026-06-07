# Phase 2 — Session Kickoff Prompt

> Paste the block below to start a fresh session focused on implementing Phase 2
> ("Native code layer + code delta, parity-only"). It pins the authoritative
> guides, the dev-environment entrypoints, the mandatory milestone gate, and the
> exact repo state to start from.

---

```
Please implement Phase 2 of the TyO3 spine refactor — "Native code layer + code
delta (parity-only)" — fully and correctly, following the step-by-step guide.

PRIMARY GUIDE (read first, in full):
@.scratch/projects/15-implementation-plan/PHASE_2_IMPLEMENTATION_GUIDE.md

SUPPORTING CONTEXT (read the relevant sections as needed):
@.scratch/projects/15-implementation-plan/REFINED_IMPLEMENTATION_CONCEPT.md  (§5.4 delta, §5.5 identity, §5.6 hashing, §3 the salsa constraint)
@.scratch/projects/15-implementation-plan/REFINED_IMPLEMENTATION_PLAN.md      ("The safety net: the parity oracle", Phase 2 section)
@.scratch/projects/16-refined-implementation-work/PROGRESS.md                 (progress tracker — §5 is Phase 2; UPDATE it when the phase is complete)

You may also consult the other PHASE_<n>_IMPLEMENTATION_GUIDE docs for context.

WHAT PHASE 2 IS (in one line): make Rust maintain the authoritative code-graph
structure and produce a code delta inside the commit, emitted ALONGSIDE the
existing Python read-surface build and checked with the tiered parity oracle. The
native delta is NOT yet authoritative — the legacy Python build stays authoritative
until Phase 4. Go slowly; lean on parity. This is the high-risk core of the refactor.

Steps to implement in order (all detailed in the guide):
  2.1  Add structural fields to `rust/src/entity.rs` (file, full range, NAME range,
       qualified_name). The name range must sit on the class/def *name*, or
       inheritance/supertype resolution silently returns nothing.
  2.2  New `rust/src/code_layer.rs` — CodeLayer { nodes, edges (deterministic Ord),
       reverse_deps }; register the module; add a CodeLayer field to head state.
  2.3  New `rust/src/dto/code_delta.rs` — node DTO, edge DTO, CodeDelta; field names
       must match the Python mirror exactly. Design it to NEST into Phase 3's commit delta.
  2.4  The producer passes, native, inside the commit, correctly phased:
       materialise ALL nodes first → containment → references/imports (+reverse_deps)
       → inheritance in TWO passes (all `inherits`, THEN all `overrides`) → diff
       against current CodeLayer to emit a minimal incremental delta (+ full path for
       cold start / rescan). A single combined inheritance pass is forbidden.
  2.5  Emit the delta on the commit result, expose to Python, and assert parity via
       the oracle (`src/tyo3/tests/parity_oracle.py`, `test_final_parity_oracle.py`).

Watch the documented failure modes (guide §6): qualified-name format (dotted
`User.save` vs registry `::`-joined-with-file-prefix — must match exactly or edges
resolve to the wrong nodes); reference-target resolution must match the legacy
resolver case-for-case; inheritance cursor on the name range; borrow-checker friction
on the producer's captures. Remember the parity oracle is structural-strict +
cosmetic-downgradeable ON PURPOSE — do NOT re-tighten cosmetic fields to byte-for-byte.

DEV ENVIRONMENT — every in-repo command goes through devenv scripts:
  - Build the extension (sccache + mold, fast):     devenv shell -- build
  - Fast Python tests (parallel, no-cov; accepts narrowed targets, e.g.
      `test-fast test_final_parity_oracle.py`):       devenv shell -- test-fast
  - Rust tests:                                       devenv shell -- cargo test --manifest-path rust/Cargo.toml <filter>
  - Lint gate:                                        devenv shell -- clippy
  ALWAYS rebuild (`devenv shell -- build`) after a Rust change before pytest.
  Suites are slow (~10–15 min) — run full suites in the background and wait.

WORKING RULES (non-negotiable):
  1. Write/await the failing parity test first, then implement until green.
  2. Never weaken a test to make progress. Don't swallow errors.
  3. *** MILESTONE GATE IS MANDATORY — DO NOT SKIP IT. *** After the phase, run BOTH
     full suites and confirm green:
        devenv shell -- test-fast            (full Python suite)
        devenv shell -- cargo test --manifest-path rust/Cargo.toml
     (The Phase 1 series skipped this and silently regressed 8 tests. Don't repeat that.)
  4. Commits: NO AI attribution / no Co-Authored-By footers (project policy).

CURRENT REPO STATE you're starting from (branch `spine-refactor-phase-1`,
HEAD `a26a33c`):
  - Phase 0 (invariant tests + parity oracle) and Phase 1 (content gate) are DONE,
    committed, and pushed.
  - Full suite is green EXCEPT 3 genuinely pre-existing later-phase failures
    (test_final_derived_contract ×2 → Phase 7; test_final_hash_ast ×1 → Phase 9) and
    5 xfail-tracked tests. Treat those as the known baseline — do not let Phase 2
    add NEW failures beyond them.
  - Two Phase 1 carry-overs are deferred to Phase 5 (xfail-marked, documented in the
    Phase 5 guide): `sync_all` must re-ingest disk; the watcher must distinguish
    unsaved buffers from ingested content. These are NOT Phase 2's job — ignore them.

ACCEPTANCE (guide §5): `devenv shell -- cargo test --manifest-path rust/Cargo.toml
code_layer code_delta entity` plus the parity suite, all green, with the legacy build
still authoritative; then both full milestone suites green. Update PROGRESS.md §5 and
the dashboard when complete. Commit per the guide's suggested sequence (§7).

Start by reading the Phase 2 guide and establishing the red baseline (guide §3), then
work step by step. Ask me before making any design decision the guide leaves open.
```

---

## Why these emphases (notes for the human, not part of the prompt)

- **The milestone gate is front-and-center** because the single biggest lesson
  from the Phase 1 verification was that the original series skipped the full-suite
  run and silently regressed 8 previously-green tests.
- The prompt **pins the exact starting HEAD and the known-baseline failures** so the
  new session can distinguish a real Phase-2 regression from pre-existing noise.
- It **scopes out the Phase 5 carry-overs** (watcher / `sync_all`) so the agent
  isn't derailed by those xfails.
- It encodes the **devenv entrypoints** (`build`, `test-fast`) and the
  **no-AI-attribution** commit policy.
