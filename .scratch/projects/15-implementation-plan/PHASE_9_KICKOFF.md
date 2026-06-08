# Phase 9 kickoff — Async precision refinement (paste into a fresh session)

We're continuing the TyO3 spine refactor. All planning is done and lives in
`.scratch/projects/15-implementation-plan/`. Your job this session is to implement
**Phase 9: the async precision refinement layer** — an *optional* background
worker that **narrows** the synchronous, container-granular `affected` set to
method-level precision over a frozen snapshot, and publishes the result as an
`AffectedRefinement` on the bus's refinement channel. It exists **only** to
improve precision; the system is fully correct without it. **Graceful degradation
is the headline property.**

Before writing any code, read in this order:
  1. `.scratch/projects/15-implementation-plan/START_HERE_V2.md`            (orientation — read fully)
  2. `REFINED_IMPLEMENTATION_CONCEPT_V2.md` §5.2–§5.4                       (container-granular coverage, the residual miss class, the async refinement — the crux)
  3. `REFINED_IMPLEMENTATION_CONCEPT.md` (V1) §3                            (the salsa constraint — why the worker must never share the live db)
  4. `REFINED_IMPLEMENTATION_PLAN_V2.md`  (Phase 9 section)                 (current state + phase map)
  5. `PHASE_9_IMPLEMENTATION_GUIDE.md`                                      (the step-by-step 9.1–9.4 you'll execute)
Then skim, for current ground truth:
  6. `.scratch/projects/16-refined-implementation-work/PROGRESS.md` §9.7 and §9.8  (what Phases 7 and 8 landed)

## Critical context — Phases 6, 7, 8 are LANDED (this is why Phase 9 can run)

- **Phase 6** made the synchronous `affected_ids` a **sound superset**:
  transitive, container-granular, never-miss. This is the safety net Phase 9
  narrows — narrowing a sound superset can never introduce a miss.
- **Phase 7** added the **refinement channel** as a contract seam: `bus/refinement.py`
  defines `AffectedRefinement{revision, narrowed, added}`; `Bus.publish_refinement`
  fans it out on a **separate** queue; `Subscription.poll_refinement(timeout=...)`
  drains it. The channel never touches the primary `revision > last` ordering
  invariant. **Nothing emits a refinement yet — Phase 9 is the producer.**
- **Phase 8** made derived invalidation id-level and affected-driven, so a
  consumer *can* act on a narrowed set. (Phase 9 doesn't have to wire the derived
  loop to consume refinements — that's optional/forward — but the capability now
  exists.)

So Phase 9's *substrate* is fixed (sound coarse set + delivery channel). The
remaining task is the *producer*: the worker that re-resolves member access over
a frozen snapshot and publishes the narrowing.

## Where Phase 9 actually starts — what exists vs what you build

- **Exists, do not rebuild:** `bus/refinement.py` (`AffectedRefinement`),
  `Bus.publish_refinement` (`src/tyo3/bus/bus.py:124`), `Subscription.poll_refinement`
  /`_offer_refinement` + the separate `_refinements` deque (`src/tyo3/bus/subscription.py`).
  `test_final_bus_contract.py::test_refinement_channel_delivers_after_primary_delta`
  already proves ordered, independent refinement delivery (it injects a refinement
  manually). Keep it green.
- **You build:** `src/tyo3/precision/refiner.py` (the package does **not** exist
  yet — `src/tyo3/precision/` is new); the `precision`/`refinement` config knobs;
  the start/stop + feed wiring in `session.py`; `src/tyo3/tests/test_precision_refinement.py`.

## Stale-guide points to reconcile (VERIFY before following the guide literally)

The guide was written before Phases 6–8 landed. Three things to check against the
real code first:

1. **Snapshot pin API is `at=`, NOT `revision=`.** The guide writes
   `with session.snapshot(revision=R) as snap:`. The real signature is
   `session.snapshot(at: int | None = None)` (`session.py:957`; see
   `test_gate6_authored.py` for `session.snapshot(at=r1)`). Use `snapshot(at=R)`.

2. **Read-surface queries are position-based, not id-based.** The guide says
   resolve member access with `find_references` / `goto_type_definition`. Those
   exist on the snapshot but take `(path, line, column)` —
   `goto_type_definition(path, line, column)`, `find_references(...)`,
   `document_symbols(path)` (`session.py:173–241`). There is **no** id-keyed
   member-access resolver. So the refiner must map a changed member id → its
   `(file, range)` (via the snapshot graph's `symbol(id)`) → run the positional
   query → map results back to ids. Budget for that id↔position bridge; it's the
   real work of 9.2. The graph also gives you cheap structural referrers
   (`graph.dependents(id)`, `graph.references_to(id)`) to seed/bound the candidate
   set before the expensive per-occurrence type resolution.

3. **There are TWO config-read paths — pick the right precedent.** Layer/store/
   generator config flows native → `config_json` → `config.py::TyConfig`. But the
   **runtime/coordination knobs** (`[coordination.bus]`, `[coordination.watcher]`)
   are read in Python by `session._read_coordination_config()` reading the sidecar
   TOML **directly** (`session.py:803`), bypassing `TyConfig`. `precision`/
   `refinement` are runtime knobs of the same flavor. Decide deliberately: mirror
   the `_read_coordination_config` direct-TOML pattern (simplest, consistent with
   the closest precedent), **or** add a native `code_graph` section to
   `rust/src/config.rs` `RawConfig` (there is currently **no** `code_graph`
   section — only `coordination`) + validation + `config.py`. The guide assumes the
   native path; the lighter, precedent-matching path is the Python read. Either is
   defensible — state your choice in the plan. If you do touch `config.rs`, it has
   `#[serde(deny_unknown_fields)]` on its structs, so a new `[code_graph]` section
   requires a new typed struct on `RawConfig`; **rebuild** after.

## Things to verify exist before relying on them

- **Feed point:** `_after_commit(delta)` (`session.py`) is the single post-commit
  hook every write funnels through (`_invalidate_head_snap` → `_apply_graph_delta`
  → `_schedule_derived` → `_publish_delta`). When `precision=method`, hand the
  refiner `(revision, changed_ids, affected_ids)` from here — **after**
  `_publish_delta`, so the primary delivery never waits on precision.
- **The bus is lazy.** `session._get_bus()` builds the `Bus` only when something
  subscribes. The refiner publishes via the bus; make sure it tolerates "no bus /
  no subscribers" (no-op publish) without erroring.

## Working rules (every phase)

- Run EVERYTHING through `devenv shell --`. Phase 9 is **mostly Python** (new
  `src/tyo3/precision/`, `session.py`, tests). Any `config.rs` touch needs
  `devenv shell -- build` before the pytest gate. Suites are slow (~10–15 min);
  run full suites in the background.
- **Never query the live head db from the worker (V1 §3).** Open a frozen
  `snapshot(at=R)`, query, close it in `finally`. A reader sharing the live db
  blocks/cancels the writer — that is the whole reason this is async over
  snapshots.
- **Narrow only (default).** The refinement is a **subset** of the coarse set,
  and it must **always retain `changed_ids`**. Never publish `added` in narrowing
  mode (expansion = 9.4, off by default, eventually-consistent — only if a use
  case needs it; otherwise skip it).
- **Defaults are `precision=container` + `refinement=async`.** With
  `precision=container` the worker **never starts** and every existing suite
  passes unchanged. The writer pays nothing unless precision is explicitly asked
  for.
- **Graceful degradation is a test, not a hope.** A test must kill/stall the
  worker and assert the coarse set was still delivered and is correct.
- Don't weaken what's green: the Phase-7 bus contracts (`test_final_bus_contract.py`,
  `test_gate8_bus.py`), the Phase-8 derived suites (`test_final_derived_contract.py`,
  `test_gate5_derived.py`), and `test_inference_flow_coverage.py` stay green.
- Don't regress Phases 6–8: don't reintroduce a head-graph read on the write path;
  don't gate primary delivery on the refinement; don't make `container` mode pay.

## The milestone gate after the phase

```bash
devenv shell -- build   # only if you touched config.rs; harmless otherwise
devenv shell -- pytest src/tyo3/tests/test_precision_refinement.py -q --no-cov -rA
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml
```

After Phase 9, the full-suite baseline should be **unchanged except your new
green `test_precision_refinement.py`**: the only remaining known-baseline failure
stays `test_final_hash_ast::test_formatting_only_hashes_same` (→ Phase 10), and
`cargo test` stays **158/0** (or 158+N if you added `config.rs` tests). With the
default `precision=container`, `pytest -q --no-cov` must look identical to the
Phase-8 gate (670 passed / 2 xfailed / 1 failed) plus your new tests.

Track progress in `.scratch/projects/16-refined-implementation-work/PROGRESS.md`
(add a `§9.9 — V2 Phase 9` record alongside §9.7/§9.8). Commit at the end with
`feat(precision): async container→method affected refinement over snapshots`
(no AI attribution — house rule).

## Start here

Read the docs above (incl. PROGRESS §9.7/§9.8 and the actual current
`bus/refinement.py`, `bus/bus.py::publish_refinement`,
`bus/subscription.py::poll_refinement`, `session.py::_after_commit` +
`snapshot`, and the position-based read-surface queries), then give me a short
Phase 9 implementation plan: steps 9.1–9.3 mapped to the real functions you'll
touch, your config-path decision (native `code_graph` vs Python direct-TOML — with
your reasoning), how the refiner bridges id→position→id for member-access
resolution, how it's fed from `_after_commit` without blocking primary delivery,
and exactly how the graceful-degradation and container-no-op tests will prove the
safety property. Note explicitly whether you're building 9.4 expansion mode (you
probably should NOT unless asked). **Wait for my go-ahead before editing code.**
