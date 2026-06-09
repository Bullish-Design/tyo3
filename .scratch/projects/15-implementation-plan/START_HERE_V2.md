# START HERE — TyO3 spine refactor, V2 (read this first)

You are picking up the TyO3 spine refactor in a fresh session. This file orients
you before you open any phase guide. Read it fully, then read in this order:

1. `REFINED_IMPLEMENTATION_CONCEPT.md` §1–§5 (V1) — system framing, vocabulary,
   the normative rules, and **the salsa constraint (§3)**. Still authoritative for
   *framing*.
2. `REFINED_IMPLEMENTATION_CONCEPT_V2.md` — the **current** architecture. Supersedes
   V1 from Phase 6 onward.
3. `REFINED_IMPLEMENTATION_PLAN_V2.md` — the current phase map + acceptance.
4. The `PHASE_<n>_IMPLEMENTATION_GUIDE.md` for the phase you are doing.

---

## Where things stand

- **Phases 0–5 are landed and authoritative** (content gate, native code layer
  behind a parity oracle, id-level `CommitDelta`, pure-applier cutover, native
  `commit()` with Strategy B+ rollback). Do not redo them.
- **Commit `aceabf6` landed an *interim* bus** (`Delta.from_commit_delta` with an
  Option-B transitive bridge over the materialised head graph). This is a stopgap;
  **Phase 7 deletes it.** Do not build on it.
- **✅ COMPLETE (2026-06-08): the spine refactor (V2) is done.** All of Phases
  6–14 landed; all ten §6.3 deviations closed; all five gate commands clean
  (`ruff check`, `ruff format --check`, `cargo clippy --all-targets -- -D
  warnings`, `cargo test`, `pytest -q`); `test_final_acceptance.py` proves the
  whole story end to end. See PROGRESS §9.14. Nothing below is "next work" — it
  is the historical record of how the refactor was sequenced.

## The numbering caveat (avoid a costly mix-up)

V2 keeps Phases 0–5 as landed, makes the **producer = Phase 6**, and shifts the
rest down by one relative to V1, inserting a new async-precision phase at 9:

| V2 | Title | (was V1) |
|----|-------|----------|
| 6 | Scoped native producer (keystone) | *(deferred; not a V1 phase)* |
| 7 | Pure-projection bus; delete read-surface scaffolding | V1 Phase 6 (bus) |
| 8 | Unified derived invalidation | V1 Phase 7 (derived) |
| 9 | Async precision refinement | *(new in V2)* |
| 10 | AST-canonical hashing | V1 Phase 9 |
| 11 | Read surface & convenience APIs | V1 Phase 8 |
| 12 | Single config source | V1 Phase 10 |
| 13 | Split the monoliths | V1 Phase 11 |
| 14 | Warnings/typing/hygiene + acceptance | V1 Phases 12+13 |

When a guide says "Phase N," it means the **V2** number. The `PHASE_6` and
`PHASE_7` guide files were overwritten with their V2 content (the V1 bus and
derived guides live in git history if you need them).

## The two findings that shaped V2 (don't re-derive them)

1. **Computing `affected` is cheap** — a graph walk over the maintained
   `reverse_deps` index, **not** re-type-checking the blast radius. The ~100×
   `open()` regression that justified deferring the producer came from the *full,
   every-file* producer, never the *scoped* one. Phase 6 is the scoped driver.
2. **The analysis engine emits no inference-flow edges.** `w = make_widget();
   w.draw()` produces no `render → draw` edge — only named refs
   (`render → make_widget → Widget`). So the synchronous `affected` set is
   **container-granular, nominally-complete, never-miss**: coverage holds because
   a member-body edit moves the *container's* `content_hash` (the container hash
   subsumes member bodies) and the container is reachable by the named chain.
   Method-level precision is the **optional async layer** (Phase 9), never required
   for correctness.

These are guarded by `src/tyo3/graph/tests/test_inference_flow_coverage.py`
(3 tests, currently green). **Keep them green through every phase.**

## What already exists (don't rebuild it)

The producer *machinery* is built and parity-verified in `rust/src/code_layer.rs`:
`produce_code_delta`, `CodeLayer::diff_from` (incremental delta), `affected_closure`
(BFS), and `add_edge`/`remove_edge` that maintain `reverse_deps`. **Phase 6 is the
in-commit scoped driver that calls this machinery over the dirty scope — not new
machinery.**

## Non-negotiable working rules (every phase)

- **Run everything through `devenv shell --`** (Nix toolchain). Never bare
  `pytest`/`cargo`.
- **After any Rust change, `devenv shell -- build`** before a pytest gate — the #1
  phantom-failure source is testing a stale extension.
- Suites are slow (~10–15 min); run full suites in the background.
- The milestone gate after each phase:
  ```bash
  devenv shell -- pytest -q --no-cov
  devenv shell -- cargo test --manifest-path rust/Cargo.toml
  ```
- Each phase guide ends with explicit **exit criteria** — do not move to the next
  phase until they all hold.

## The destination (one sentence)

One revision = one native transaction producing one id-level `CommitDelta` whose
`affected` is transitive and container-granular; Python is read-only projections +
integrations + one optional async layer that sharpens precision without ever being
required for correctness.
