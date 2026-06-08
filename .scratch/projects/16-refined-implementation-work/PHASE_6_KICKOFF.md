# Phase 6 kickoff prompt (V2)

Paste the block below into a new session to start implementing Phase 6 (the
scoped native code-layer producer).

---

```
We're continuing the TyO3 spine refactor. All planning is done and lives in
.scratch/projects/15-implementation-plan/ — your job this session is to implement
Phase 6 (the scoped native code-layer producer), the keystone that makes
affected_ids transitive at the source.

Before writing any code, read in this order:
  1. .scratch/projects/15-implementation-plan/START_HERE_V2.md   (orientation — read fully)
  2. REFINED_IMPLEMENTATION_CONCEPT_V2.md §4–§5                   (the affected-set model)
  3. REFINED_IMPLEMENTATION_PLAN_V2.md                            (current state + phase map)
  4. PHASE_6_IMPLEMENTATION_GUIDE.md                              (the step-by-step you'll execute)

Critical context (also in START_HERE_V2.md):
- Phases 0–5 are landed; commit aceabf6 is an INTERIM bus bridge that Phase 7
  deletes — do not build on it.
- The producer MACHINERY already exists and is parity-verified in
  rust/src/code_layer.rs (produce_code_delta, diff_from, affected_closure,
  add_edge/remove_edge maintaining reverse_deps). Phase 6 is the in-commit SCOPED
  DRIVER that calls it over the dirty scope — NOT new machinery. Don't rebuild it.
- Synchronous affected is intentionally container-granular / never-miss; method
  precision is a later optional async phase. Keep
  src/tyo3/graph/tests/test_inference_flow_coverage.py green throughout.

Working rules:
- Run EVERYTHING through `devenv shell --`. After any Rust change, run
  `devenv shell -- build` before any pytest gate (stale-extension is the #1
  phantom failure). Suites are slow (~10–15 min); run full suites in the background.

Start by reading the four docs, then give me a short implementation plan for
Phase 6 (steps 6.1–6.4 from the guide, mapped to the actual functions in
project.rs / code_layer.rs you'll touch) and the order you'll verify it. Wait for
my go-ahead before editing code.
```

---

**Variant — skip the plan checkpoint and implement directly.** Replace the final
paragraph above with:

> Start by reading the four docs, then implement Phase 6 step by step per the
> guide, running the verify commands as you go.
