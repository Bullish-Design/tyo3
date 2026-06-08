# Phase 8 kickoff — Unified derived invalidation (paste into a fresh session)

We're continuing the TyO3 spine refactor. All planning is done and lives in
`.scratch/projects/15-implementation-plan/`. Your job this session is to implement
**Phase 8: unified derived invalidation** — make derived-layer invalidation
id-level and driven by the now-transitive `affected` set, with **per-layer key
locality** (local vs semantic), read-time staleness, a fixed recompute-snapshot
lifetime, and typed store errors.

Before writing any code, read in this order:
  1. `.scratch/projects/15-implementation-plan/START_HERE_V2.md`            (orientation — read fully)
  2. `REFINED_IMPLEMENTATION_CONCEPT_V2.md` §5.5                            (the unified invalidation model — the crux)
  3. `REFINED_IMPLEMENTATION_CONCEPT.md` (V1) §5.7/§5.8/§5.9/§5.12          (content-hash cache, derivation DAG, cross-layer consistency, fail-loudly)
  4. `REFINED_IMPLEMENTATION_PLAN_V2.md`  (Phase 8 section)                 (current state + phase map)
  5. `PHASE_8_IMPLEMENTATION_GUIDE.md`                                      (the step-by-step 8.1–8.6 you'll execute)
Then skim, for current ground truth:
  6. `.scratch/projects/16-refined-implementation-work/PROGRESS.md` §9.6 and §9.7  (what Phases 6 and 7 landed)

## Critical context — Phases 6 and 7 are LANDED (this is why Phase 8 can run)

- **Phase 6** made `affected_ids` **transitive at the source** (the scoped native
  in-commit producer; `head.code_layer` + `reverse_deps` maintained every
  revision). The `CommitDelta.affected_ids` is the transitive, container-granular,
  never-miss closure of `changed ∪ deleted`.
- **Phase 7** made the bus a **pure projection** and added a native
  `affected_files`. It also removed the head-graph dependency from the publish
  path. The derived loop is the next consumer of the id-level delta.

So Phase 8's *input* is fixed (id-level transitive `affected`). The remaining task
is the *mechanism*: precise, per-layer-locality invalidation.

## Where Phase 8 actually starts — TRIAGE FIRST, the guide's "all xfail" is stale

The guide and PROGRESS describe `test_final_derived_contract.py` as wholesale
broken. **That is no longer true** — Phases 6/7 already fixed part of it. Verified
state (run `devenv shell -- pytest src/tyo3/tests/test_final_derived_contract.py
-q --no-cov -rA` first to re-confirm before touching anything):

- `test_move_unchanged_reuses_artifact` — **XPASS(strict)** → it now PASSES; the
  stale `xfail(strict=True)` marker is what makes it show as FAILED in the gate.
  **Just remove the marker.** (This is one of the two "known-baseline Phase 8
  failures" — it's not a failure, it's an un-retired xfail.)
- `test_eager_recompute_leaks_no_snapshot` — **XPASS(strict)** → also now passes;
  **remove the marker.** (The second "known-baseline Phase 8 failure.")
- `test_body_change_recomputes_only_affected` — **genuine XFAIL** (precise
  recompute still not scheduled). Real work.
- `test_generator_failure_keeps_last_good` — **genuine XFAIL**. Real work.
- `test_invalidation_receives_durable_ids` — **genuine XFAIL** (invalidation is
  still fed the path-shaped write result). Real work — this is the 8.2 rewire.
- `test_gate5_derived.py::test_self_healing_derived_layer_cache_hit_recompute_reuse`
  — **XFAIL** ("API does not exist yet — pinned for implementation"). This is the
  8.6 self-healing test to un-skip; it likely needs a small read-surface API.

So the real Phase 8 deliverables are: (a) retire the 2 stale XPASS markers; (b)
make the 3 genuine xfails pass; (c) build per-layer key locality + the two new
local-vs-semantic tests; (d) un-skip the self-healing test.

## The one known stale-guide point to reconcile (verify before following literally)

- **8.2 "feed the id-level loop":** today `session._invalidate_derived(result)`
  feeds the derived DAG the **path-shaped** `result.created`/`result.changed`/
  `result.deleted` (see `session.py` ~line 786: `dirty = set(result.created) |
  set(result.changed)`). That's the "inert invalidation" the genuine xfails are
  about. Rewire `_schedule_derived(delta)` → `_invalidate_derived` to drive off
  `delta.affected_ids` (the transitive container-granular closure) at id level,
  per guide §8.2. Read `src/tyo3/derive/{dag,scheduler,layer,cache}.py` and the
  current `dag.invalidate(...)` signature before changing the feed — don't assume
  the guide's pseudocode matches the real API.

## Things to verify exist before relying on them (the guide assumes them)

- **Dependency-closure fingerprint over the pinned snapshot** (8.1 semantic key):
  the guide wants `id → deps → their content hashes, sorted` computed **at the
  pinned snapshot** (NOT the head graph — head reads are racy / can take the write
  lock, V1 §5.9). Confirm the snapshot exposes an id→dependencies + per-id
  content-hash read surface; if it doesn't, that small read API is part of 8.1.
- **One pinned snapshot, closed in `finally`** (8.4): grep the derive pass for a
  snapshot open without a matching close. (The leak test already XPASSes, so the
  leak may already be gone — confirm, then make the single-snapshot lifetime
  explicit rather than incidental.)

## Working rules (every phase)

- Run EVERYTHING through `devenv shell --`. Phase 8 is **pure Python**
  (`src/tyo3/derive/`, `src/tyo3/stores/`, `session.py`) — no Rust in the inner
  loop. Run `devenv shell -- build` once before the final gate anyway. Suites are
  slow (~10–15 min); run full suites in the background.
- **Key locality is declared, not inferred. Default `local`.** A `semantic` layer
  recomputes on dependency-only changes; a `local` layer must NOT. Prove both with
  the two new tests (8.6).
- Don't weaken what's green: `test_gate5_derived.py` stays green; the Phase-7 bus
  contracts (`test_final_bus_contract.py`, `test_gate8_bus.py`) stay green; the
  producer/parity suites stay green.
- Don't regress Phase 6/7: don't reintroduce a head-graph read on the write path
  (use the pinned snapshot); don't re-path-shape the delta.

## The milestone gate after the phase

```bash
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_final_derived_contract.py src/tyo3/tests/test_gate5_derived.py -q --no-cov -rA
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml
```

After Phase 8, the **only** remaining known-baseline failure should be
`test_final_hash_ast::test_formatting_only_hashes_same` (→ Phase 10). If a
`test_final_derived_contract` test still fails, it's yours. `cargo test` should
stay 158/0.

Track progress in `.scratch/projects/16-refined-implementation-work/PROGRESS.md`
(add a `§9.8 — V2 Phase 8` record alongside §9.6/§9.7). Commit at the end with
`fix(derived): unified affected-driven invalidation with per-layer key locality`
(no AI attribution — house rule).

## Start here

Read the docs above (incl. PROGRESS §9.6/§9.7 and the actual current
`session.py` `_invalidate_derived`/`_schedule_derived` + `src/tyo3/derive/`), run
the triage pytest command to re-confirm the XPASS/XFAIL split, then give me a
short Phase 8 implementation plan (steps 8.1–8.6 mapped to the real functions
you'll touch, how you'll handle the 2 stale XPASS markers and the 3 genuine
xfails, and where the semantic-key dependency fingerprint comes from) and the
order you'll verify it. **Wait for my go-ahead before editing code.**
