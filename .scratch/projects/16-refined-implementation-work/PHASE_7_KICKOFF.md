# Phase 7 kickoff prompt (V2)

Paste the block below into a new session to start implementing Phase 7 (the
pure-projection bus + deleting the read-surface scaffolding the deferred producer
forced into existence).

---

```
We're continuing the TyO3 spine refactor. All planning is done and lives in
.scratch/projects/15-implementation-plan/ — your job this session is to implement
Phase 7: make the bus a PURE PROJECTION of the CommitDelta, delete the interim
Option-B bridge and any dead read-surface scaffolding, finish the one
post-commit path, and add the refinement-channel seam (contract only).

Before writing any code, read in this order:
  1. .scratch/projects/15-implementation-plan/START_HERE_V2.md   (orientation — read fully)
  2. REFINED_IMPLEMENTATION_CONCEPT_V2.md §4.2, §5, §6           (projections; what V2 deletes)
  3. REFINED_IMPLEMENTATION_PLAN_V2.md                            (current state + phase map)
  4. PHASE_7_IMPLEMENTATION_GUIDE.md                              (the step-by-step you'll execute)
Then skim, for current ground truth:
  5. .scratch/projects/16-refined-implementation-work/PROGRESS.md §9.6  (what Phase 6 landed)

Critical context — Phase 6 is LANDED (this is the whole reason Phase 7 can run):
- affected_ids is now TRANSITIVE AT THE SOURCE (the scoped native in-commit
  producer; head.code_layer + reverse_deps maintained every revision; code_delta
  is minimal-incremental, None retired for content writes). So the bus delta's
  affected no longer needs the Python transitive walk — that bridge
  (_compute_affected / _resolve_files / the materialised-head-graph dependency in
  bus/delta.py) is now pure dead weight. Deleting it is Step 7.1, and it must NOT
  regress test_gate8_bus::test_scoped_reverse_dep_delivery (the capability the
  bridge was protecting — now provided natively).

Two places where the Phase 7 guide PREDATES reality — verify before following literally:
- "Delete the ~1500-line read-surface builder (CodeGraph.build)" (Step 7.2):
  Phase 4 ALREADY deleted ~1140 lines of the legacy read-surface build.
  `CodeGraph.build` today is a THIN NATIVE builder (it applies full_code_delta),
  not the legacy six-pass build. So 7.2 is mostly "confirm the legacy build is
  gone + remove any orphaned dead helpers", NOT a big delete. Grep and read
  graph/graph.py first to see what actually remains.
- "Demote the parity oracle" (Step 7.3): Phase 6 turned
  src/tyo3/graph/tests/test_incremental_parity.py into a REAL incremental-vs-rebuild
  oracle (session.graph maintained by incremental deltas vs CodeGraph.build full
  rebuild) — it is now load-bearing, not scaffolding. KEEP it as a
  native-correctness suite (the guide allows this). Only drop the "legacy-build
  vs delta-build" framing if the legacy build is truly gone; do not delete the
  scenarios, and do not delete CodeGraph.build if the oracle still needs a
  full-rebuild baseline. Think before cutting.

Also do not revert in graph/graph.py: Phase 6 fixed _remove_code_edge to remove
the SPECIFIC matched parallel edge by index (incident_edges →
remove_edge_from_index), not an arbitrary one by endpoints. Multi-import /
multi-ref module pairs depend on it; the parity oracle will catch a regression.

Working rules:
- Run EVERYTHING through `devenv shell --`. This phase is mostly Python; after any
  Rust touch run `devenv shell -- build` before a pytest gate (stale extension is
  the #1 phantom failure). Suites are slow (~10–15 min); run full suites in the
  background.
- Keep the bus contract from weakening: test_final_bus_contract.py ends fully
  green (zero xfail), test_gate8_bus.py stays green, and
  test_inference_flow_coverage.py stays green.
- The milestone gate after the phase:
    devenv shell -- pytest -q --no-cov
    devenv shell -- cargo test --manifest-path rust/Cargo.toml
  Known-baseline failures that are NOT yours (do not chase): test_final_derived_contract
  ×2 (→ Phase 8) and test_final_hash_ast::test_formatting_only_hashes_same (→ Phase 10).
- Track progress in .scratch/projects/16-refined-implementation-work/PROGRESS.md.

Start by reading the docs (incl. PROGRESS §9.6 and the actual current
bus/delta.py + graph/graph.py), then give me a short implementation plan for
Phase 7 (steps 7.1–7.5 mapped to the real functions you'll touch, and how you'll
reconcile the two stale-guide points above) and the order you'll verify it. Wait
for my go-ahead before editing code.
```

---

**Variant — skip the plan checkpoint and implement directly.** Replace the final
paragraph above with:

> Start by reading the docs (including PROGRESS §9.6 and the actual current
> bus/delta.py + graph/graph.py), then implement Phase 7 step by step per the
> guide — reconciling the two stale-guide points noted above against the real
> code — running the verify commands as you go.
