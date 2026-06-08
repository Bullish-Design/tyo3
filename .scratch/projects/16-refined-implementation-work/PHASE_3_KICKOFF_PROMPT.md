# Phase 3 — Kickoff Prompt

> Paste the block below into a fresh session to start Phase 3. It is
> self-contained; it points at the guides and flags the one critical handoff
> from Phase 2 (the deferred in-commit producer hookup) that the Phase 3 guide's
> §1.25 assumes is already done.

---

Please implement **Phase 3 of the TyO3 spine refactor — "The id-level commit delta"** — fully and correctly, following the step-by-step guide.

**PRIMARY GUIDE (read first, in full):**
`@.scratch/projects/15-implementation-plan/PHASE_3_IMPLEMENTATION_GUIDE.md`

**SUPPORTING CONTEXT (read the relevant sections as needed):**
- `@.scratch/projects/15-implementation-plan/REFINED_IMPLEMENTATION_CONCEPT.md` (§5.4 the delta + reverse-dep index, §5.5 identity/reconciliation, §5.3 the commit transaction)
- `@.scratch/projects/15-implementation-plan/REFINED_IMPLEMENTATION_PLAN.md` (Phase 3 section + "The safety net: the parity oracle")
- `@.scratch/projects/16-refined-implementation-work/PROGRESS.md` (progress tracker — §6 is Phase 3; **§5 documents what Phase 2 actually delivered vs deferred** — read it. UPDATE the tracker when the phase is complete.)

**WHAT PHASE 3 IS (one line):** replace the path-shaped `SyncResultDto` with one structured, entity-identity-level `CommitDelta` (`created_ids` / `changed_ids` / `deleted_ids` / structured `moved` / `affected_ids` / nested `code_delta` / `touched_files` / `rescan`), where `changed` means "content hash changed" and `affected_ids` is the closure of `changed ∪ deleted` under the reverse-dependency index.

**⚠️ CRITICAL HANDOFF FROM PHASE 2 — READ THIS BEFORE TRUSTING THE GUIDE'S §1.25 ("What is already in place from Phase 2"):**
Phase 2 **deferred the eager in-commit producer hookup** (a user-approved decision — see PROGRESS.md §5 and the long comment in `run_identity_reconciliation` in `rust/src/project.rs`). The reason: the Phase 2 `produce_code_delta` is a **full** build (full semantic analysis + cold typeshed warmup, ~4.5s for a 1-file project), so running it per open/commit made `open()` ~100× slower and broke `test_no_deadlock_on_repeat_sessions`. Consequences you must account for:
- `HeadState.code_layer` is **empty** (it's `#[allow(dead_code)]` for now) and `CodeLayer.reverse_deps` is **NOT populated at commit time**. The guide's claim that "Phase 3 reads `reverse_deps` … the producer itself is unchanged" is therefore **not yet true** — you must make it true.
- `produce_code_delta(state, prev, revision, rescan, scope)` already accepts a `scope: Option<&HashSet<String>>` but currently **ignores it and always full-builds**. To wire it into the commit affordably, you must implement real scoping (re-analyze only the dirty files + their importers, diff against the stored layer) so a single `edit()` doesn't pay the full-project cost. Verify this with a perf sanity check (an `edit()` and a session `open()` must stay in the tens-to-low-hundreds of ms, not seconds) and re-run `test_concurrency.py` — do not reintroduce the Phase 2 regression.
- **Decide deliberately** whether the affected closure needs a fully-maintained code layer in the commit, or whether you can compute `affected_ids` from a lighter id-level reverse-dep index. Whatever you choose, `reverse_deps` (or its equivalent) must be **correct after every revision and maintained incrementally** (§5.4), without the full-build cost. **If this requires a design choice the guide leaves open, ask me before committing to it.**

**Steps to implement in order (all detailed in the guide §4):**
- **3.1** New `rust/src/dto/commit_delta.rs` — `CommitDelta` + `MovedEntity` DTOs; register in `dto/mod.rs`. Nest the existing Phase 2 `CodeDeltaDto` as a field — **do not change its shape.**
- **3.2** `rust/src/identity.rs` — reconciliation emits ids + structured moves + a **precise** changed set. The current `Reconciliation::changed()` returns **all** `Exact` bindings (over-fires — violates §5.4 "no over-fire"); it must compare old vs new content hash and only report genuinely changed ids. Capture the old hash **before** `rebind`. `Binding::Moved` already carries `old_path`; add the new location. `moved` must be `{id, old_qualified_path, new_qualified_path, old_file, new_file}` and must never be reported as create+delete.
- **3.3** `rust/src/code_layer.rs` — add an `affected_closure(seeds)` helper over `reverse_deps`; `rust/src/project.rs` — commit bodies build a `CommitDelta`, compute the changed set + affected closure, and nest the code delta. (This is where the deferred in-commit producer hookup from Phase 2 lands — done incrementally per the handoff note above.)
- **3.4** Mirror `CommitDelta` + `MovedEntity` as Python models (use default factories for every list/dict field — never a shared mutable default); write methods in `session.py` validate/return `CommitDelta`. Prefer a new model name over reusing the old `SyncResult` name; keep a thin deprecated shim only if external callers require one.

**Watch the documented failure modes (guide §6):** `changed` must mean content-hash-changed, never "an exact-path rebinding happened"; ids must never be inferred from touched files; a move with unchanged body must keep its id and cache hits (not appear in created+deleted); `affected_ids` must come from the reverse-dep closure, not a global scan; don't weaken `test_final_commit_delta_contract.py`.

**KEEP THE PARITY SAFETY NET INTACT:** Phase 2's parity oracle (`test_final_parity_oracle.py`, `src/tyo3/tests/parity_oracle.py`) is green and the legacy `CodeGraph.build` is still authoritative (cutover is Phase 4). Phase 3 must not break parity. Do **not** re-tighten the cosmetic tier.

**DEV ENVIRONMENT — every in-repo command goes through devenv scripts:**
- Build the extension: `devenv shell -- build` (**required** after every Rust change before pytest)
- Fast Python tests (parallel, no-cov; accepts narrowed targets): `devenv shell -- test-fast test_final_commit_delta_contract.py`
- Rust tests: `devenv shell -- cargo test --manifest-path rust/Cargo.toml <filter>` (one filter at a time)
- Suites are fast now (~90s full) thanks to the dev dependency-optimization profile in `rust/Cargo.toml`; still, run full suites in the background and wait.

**WORKING RULES (non-negotiable):**
1. Write/await the failing test first (the 6 `test_final_commit_delta_contract.py` tests are module-level `xfail(strict=True)`); implement until they go green, then remove the xfail marker (never before — an xpass under strict is a failure).
2. Never weaken a test to make progress. Don't swallow errors.
3. **MILESTONE GATE IS MANDATORY — DO NOT SKIP IT.** After the phase, run BOTH full suites and confirm green:
   - `devenv shell -- test-fast` (full Python suite)
   - `devenv shell -- cargo test --manifest-path rust/Cargo.toml`
   The known-good baseline is **3 pre-existing later-phase failures** (`test_final_derived_contract` ×2 → Phase 7; `test_final_hash_ast::test_formatting_only_hashes_same` → Phase 9) plus the xfail-tracked tests. Do not add NEW failures beyond that baseline — and specifically re-run `test_concurrency.py` to confirm you did not reintroduce Phase 2's per-commit slowdown.
4. Commits: NO AI attribution / no `Co-Authored-By` footers (project policy).

**CURRENT REPO STATE you're starting from (branch `spine-refactor-phase-1`, HEAD `ea6f371`, pushed):**
- Phases 0, 1, 2 are DONE, committed, and pushed. Phase 2 = `ea6f371` "feat(rust): native code layer + code delta behind the parity oracle".
- Full suite is green EXCEPT the 3 pre-existing Phase 7/9 failures above and the xfail-tracked tests (including the 6 `test_final_commit_delta_contract.py` xfails that are Phase 3's target).
- Phase 2's `CodeLayer`, `produce_code_delta`, `dto/code_delta.rs`, the pure `CodeGraph.apply_code_delta`, and `PyTyProject.full_code_delta()` all exist and are tested. Re-read PROGRESS.md §5 for the exact "delivered vs deferred" boundary — especially the deferred in-commit hookup that Phase 3 inherits.

**ACCEPTANCE (guide §5):** `devenv shell -- cargo test --manifest-path rust/Cargo.toml identity` plus `devenv shell -- test-fast test_final_commit_delta_contract.py`, all green (no downstream code treats a path as a durable id; moves are structured; a two-function edit in one file reports two changed ids; a whitespace-only edit reports empty `changed_ids`); then both full milestone suites green. Update PROGRESS.md §6 and the dashboard when complete. Commit per the guide's suggested sequence (§7).

Start by reading the Phase 3 guide and establishing the red baseline (guide §3), then work step by step. **Ask me before making any design decision the guide leaves open** — in particular the reverse-dep / affected-closure approach and how to make the in-commit producer incremental (the Phase 2 carry-over).
