# Phase 4 kickoff prompt

> Paste the block below into a fresh session to start Phase 4. It assumes Phases
> 0–3 are landed and pushed on `spine-refactor-phase-1` (HEAD `563de02`).

---

```
Please implement **Phase 4 of the TyO3 spine refactor — "Cutover: the Python graph becomes a pure applier"** — fully and correctly, following the step-by-step guide.

**PRIMARY GUIDE (read first, in full):**
@.scratch/projects/15-implementation-plan/PHASE_4_IMPLEMENTATION_GUIDE.md

**SUPPORTING CONTEXT (read the relevant sections as needed):**
- @.scratch/projects/15-implementation-plan/REFINED_IMPLEMENTATION_CONCEPT.md (§5.3 the commit transaction, §5.4 the delta, §5.9 read-side writes / pure projection)
- @.scratch/projects/15-implementation-plan/REFINED_IMPLEMENTATION_PLAN.md (Phase 4 section + "The safety net: the parity oracle")
- @.scratch/projects/16-refined-implementation-work/PROGRESS.md (progress tracker — §7 is Phase 4; **§6 documents exactly what Phase 3 delivered vs deferred** — read it. UPDATE the tracker + dashboard + xfail countdown when the phase is complete.)

**WHAT PHASE 4 IS (one line):** make the native code delta *authoritative* — the Python `CodeGraph` becomes a **pure applier** of `code_delta` (no FFI, no read-surface reads, no session/snapshot access), the six-pass read-surface build is **deleted**, every **read-side write is removed** (`_prime_identity_registry` / `sync_all` on a read path), and the snapshot graph is built over the snapshot's **own frozen database** — all behind the still-green parity oracle.

**⚠️ CRITICAL HANDOFF FROM PHASE 3 — the nested `code_delta` is EMPTY (this is now documented in the guide; read its §1 "Critical carry-over from Phase 3" callout):**
Phase 3 (user-approved "Option B") shipped the id-level `CommitDelta` but **deliberately did NOT run the in-commit producer**. Two facts the cutover depends on:
- `head.code_layer` is **EMPTY** (`produce_code_delta` is a full build that regressed `open()`/`test_concurrency` in Phase 2), so `reverse_deps` is empty and `affected_ids` is currently seeds-only (`changed ∪ deleted`). The `CodeLayer::affected_closure` helper exists and is unit-tested but has nothing to walk.
- `CommitDelta.code_delta` is **EMPTY on every commit** — `build_commit_delta` (`rust/src/project.rs`) sets it to `CodeDeltaDto::default()`, which pythonizes to a **non-null** dict, NOT `None`. So a naïve `if code_delta is None:` branch never fires and applying that empty delta would leave the head graph un-updated.

**THE RECOMMENDED, ALREADY-DECIDED MECHANISM (in the guide §1 callout + §4.2(a)):** make the field a true `Option` with **three-state** semantics:
- change Rust `CommitDeltaDto.code_delta` to `Option<CodeDeltaDto>`; `build_commit_delta` emits `None` while the producer isn't running (Python `CommitDelta.code_delta` is already `dict | None`);
- `None` (absent) → "no structural delta computed" → **rebuild** from `full_code_delta()`;
- `Some({})` (present, empty) → "computed; nothing changed" (e.g. whitespace-only edit) → **no-op**;
- `Some({…})` (present, populated) → **apply** incrementally.
Do NOT collapse *empty* into *rebuild* (that forces a full rebuild on every cosmetic edit — a perf bug), and do NOT signal rebuild via `rescan=true` on the nested delta (the applier reads `rescan=true` as "this delta is the complete set, replace wholesale", so an empty rescan delta wipes the graph). With this in place the post-commit head update is just `None → _rebuild_head_graph_from_native()` — landing the cutover with parity green and **zero producer work**. The cold ~4.5s `full_code_delta()` cost is paid once per session (typeshed warmup); subsequent calls run over a warm salsa db (tens-to-low-hundreds of ms) — **re-verify with `test_concurrency`**.

**THE ONE OPEN FORK TO CONFIRM WITH ME BEFORE COMMITTING:** whether to *also* implement the scoped incremental in-commit producer this phase (run `produce_code_delta` over dirty files + importers, diff against `head.code_layer`, store it back, emit `Some(real_delta)` — which makes `reverse_deps` maintained and `affected_ids` transitive for free) **or** ship the `None`→full-rebuild path and defer the producer to a later perf pass (it's purely additive — no consumer change). I lean defer-the-producer unless `test_concurrency`/graph-test timing demands it. **Ask me before deciding** (same as Phase 3's affected-closure fork; see the `phase3-affected-closure-deferred` project memory). The `Option<CodeDeltaDto>` mechanism above ships regardless of which way the fork goes.

**Steps to implement in order (all detailed in the guide §4):**
- **4.1** Harden `CodeGraph.apply_code_delta(self, code_delta)` into the real **pure** applier (upsert/remove/move nodes, add/remove edges, maintain ALL secondary indices incl. the file-level reverse-dep `_file_importers`, handle `rescan` full deltas, set `self._revision`). Reuse `_add_node`/`_add_edge`/`_rebuild_indexes`; apply nodes before edges; no FFI/session/snapshot access. Add focused applier unit tests.
- **4.2(a)** Land the `Option<CodeDeltaDto>` mechanism above; flip the post-commit head update + the `graph` property to the native delta, **revision-gated** — *while the legacy build still exists*, A/B against parity. Factor a `_rebuild_head_graph_from_native()` helper.
- **4.2(b)** **Only once (a) is green and parity holds**, delete the read-surface construction from `graph/graph.py` (six-pass `build`, `_collect_symbols_for_file`, `_materialize_file_nodes`, occurrence/inheritance resolution, legacy `apply_delta`/`_index_files`/`_handle_moved_entities`/`_revalidate_inbound_inheritance`, dead resolution indices). Keep the query/projection surface. Decide where read-only diagnostics live.
- **4.3** Remove `_prime_identity_registry` + the `_graph_identity_primed` flag; reads never call `sync_all`. A missing-identity graph build raises a typed `GraphBuildFailure`, never repairs via a write. Grep the whole tree for `sync_all` on read paths (incl. `LatestView`).
- **4.4** Add a `full_code_delta()` accessor on the **snapshot/frozen** native handle (`rust/src/project.rs`) computed over the frozen db; `Snapshot.graph()` builds from *that*, mutating no session state.

**KEEP THE PARITY SAFETY NET INTACT (this is the seatbelt for the cutover):** Phase 2's parity oracle (`test_final_parity_oracle.py`, `src/tyo3/tests/parity_oracle.py`) must stay green at EVERY step. A **structural** parity diff means you deleted ahead of what the native side reproduces — revert the deletion and fix the *producer* (Phase 2 territory), never the test. Delete the legacy build **last**, only once the native path drives both head and snapshot graphs and structural parity still holds. Don't collapse/re-point the oracle's `legacy_graph` until the legacy build is truly gone.

**WATCH THE DOCUMENTED FAILURE MODES (guide §6):** deleting the legacy build before the native path is authoritative; an impure applier reaching for `source.files()`/`document_symbols`; stale rustworkx indices after `remove_nodes_from` (use `_rebuild_indexes`); edges-before-nodes; a move leaking into create+delete at the graph level; re-homing a `sync_all` onto a read path (incl. diagnostics refresh); the snapshot graph built over the live head instead of its frozen db; diagnostics silently disappearing.

**DEV ENVIRONMENT — every in-repo command goes through devenv scripts:**
- Build the extension: `devenv shell -- build` (**required** after every Rust change before pytest)
- Fast Python tests (parallel, no-cov; accepts narrowed targets): `devenv shell -- test-fast test_final_no_read_side_writes.py`
- Rust tests: `devenv shell -- cargo test --manifest-path rust/Cargo.toml <one-filter-at-a-time>`
- Suites take ~2–3 min; run full suites in the background and wait (use background tasks, not foreground sleeps).

**WORKING RULES (non-negotiable):**
1. Structural parity green at every step; legacy build deleted last. The applier is pure (no FFI/session/snapshot). Reads never write — prove it with `test_final_no_read_side_writes.py`, not inspection. Revision-gate the apply (stale = no-op; gap = full rebuild; never a silent partial).
2. Never weaken a test or the comparator's structural tier. Don't swallow errors (typed `GraphBuildFailure`).
3. **MILESTONE GATE IS MANDATORY.** After the phase, run BOTH full suites and confirm green:
   - `devenv shell -- test-fast` (full Python suite)
   - `devenv shell -- cargo test --manifest-path rust/Cargo.toml`
   The known-good baseline is **3 pre-existing later-phase failures** (`test_final_derived_contract` ×2 → Phase 7; `test_final_hash_ast::test_formatting_only_hashes_same` → Phase 9) plus the xfail-tracked tests. Do not add NEW failures. Re-run `test_concurrency.py` to confirm you did not reintroduce a per-commit/open slowdown (especially the rebuild-per-commit path, and doubly so if you take the producer fork).
4. Write/await the failing test first: `test_final_no_read_side_writes.py` has strict-xfail cases for Phase 4 (PROGRESS.md §17 says **2 remain** — one went green via Phase 1.3; the guide text says 3, so verify the actual count at the red baseline). Remove each marker only once its case is genuinely green (an xpass under strict is a failure).
5. Commits: NO AI attribution / no `Co-Authored-By` footers (project policy). Use the guide's §7 5-commit sequence; squash on landing if preferred.

**CURRENT REPO STATE you're starting from (branch `spine-refactor-phase-1`, HEAD `563de02`, pushed):**
- Phases 0,1,2,3 are DONE, committed, and pushed. Phase 3 = commits `5c00b34`..`ebe0c97`; `563de02` is a docs update to the Phase 4 guide (the carry-over callout).
- Full milestone gate green EXCEPT the 3 pre-existing Phase 7/9 failures above and the xfail-tracked tests. Rust: 155 passed / 0 failed. Python: 639 passed, 19 xfailed, 3 failed (the baseline 3).
- Phase 3 landed: `rust/src/dto/commit_delta.rs` (`CommitDeltaDto`/`MovedEntityDto`), `Reconciliation::classify` + `old_hash` bindings + `ReconcileClasses`/`MovedBinding`/`file_of_qualified_path` (`rust/src/identity.rs`), `CodeLayer::affected_closure` (`rust/src/code_layer.rs`), `build_commit_delta` + all 6 commit bodies returning `CommitDeltaDto` + `is_project_config_file`→rescan (`rust/src/project.rs`), Python `models/delta.py` (`CommitDelta`/`MovedEntity`), session write methods returning `CommitDelta`, and duck-typed `bus/delta.py` + `graph/graph.py` post-commit helpers. Re-read PROGRESS.md §6 for the exact delivered-vs-deferred boundary.

**ACCEPTANCE (guide §5):** `test_final_no_read_side_writes.py` (all markers removed) + `test_graph*.py` + `test_final_parity_oracle.py` all green over the native-only path; the pure applier is the ONLY graph builder/updater (head + snapshot), revision-gated; the read-surface build, `apply_delta`, and `_prime_identity_registry`/`_graph_identity_primed` are deleted; no read accessor advances head; snapshot graph built over the frozen db. Then both full milestone suites green. Update PROGRESS.md §7 + dashboard + xfail countdown + defect #3 status when complete. Commit per the guide's §7 sequence.

Start by reading the Phase 4 guide and establishing the red baseline (guide §3), then work step by step. **Ask me before making any design decision the guide leaves open** — in particular the incremental-producer-vs-full-rebuild fork (the `Option<CodeDeltaDto>` mechanism ships either way).
```
