# TyO3 Spine Refactor — PROGRESS

> Tracks the refactoring of the TyO3 codebase so that **Rust owns committed truth**
> and **Python is a read-only projection plus integrations**.
>
> **The plan has been revised — see `REFINED_IMPLEMENTATION_*_V2.md`.** The
> current plan lives in `.scratch/projects/15-implementation-plan/`:
> - **`START_HERE_V2.md`** — orientation for a fresh session (read first)
> - `REFINED_IMPLEMENTATION_CONCEPT_V2.md` — current architecture (supersedes V1 from Phase 6)
> - `REFINED_IMPLEMENTATION_PLAN_V2.md` — current phase map (6–14) + acceptance
> - `PHASE_6_IMPLEMENTATION_GUIDE.md` … `PHASE_14_IMPLEMENTATION_GUIDE.md` — V2 step-by-step guides
> - `PHASE_6_KICKOFF.md` — paste-able kickoff prompt for the next session
> - V1 `REFINED_IMPLEMENTATION_CONCEPT.md` / `_PLAN.md` remain authoritative for **Phases 0–5** + framing/§5 rules.

---

## 0. V2 RE-DIRECTION (current plan — read this before §1 onward)

**Everything below §0 uses the V1 phase numbering and is retained as the
historical record of Phases 0–6.** The plan was re-sequenced on **2026-06-08**;
§1–§19 are accurate history but their *forward-looking* numbering (Phase 7+) is
superseded by the V2 map here.

### Why the re-direction

The V1 plan assumed the **native in-commit code-delta producer** (which maintains
the reverse-dep index and emits a transitive `affected` set) already existed. It
was deferred at Phases 3/4/5 (sound risk calls each time), so every later phase
consumed a capability that was never built — producing accidental complexity (the
Option-B bus bridge in `aceabf6`, the "affected seeds-only" caveat threaded
through the phases). **V2 builds the producer first**, then deletes every bridge
and scaffold the deferral forced into existence.

### Two findings that shaped V2 (verified 2026-06-08)

1. **Computing `affected` is cheap** — a graph walk over the maintained
   `reverse_deps` index, **not** re-type-checking the blast radius. The ~100×
   `open()` regression came from the *full, every-file* producer, never the
   *scoped* one (dirty files ∪ one-hop importers).
2. **The analysis engine emits no inference-flow edges** — `w = make_widget();
   w.draw()` produces no `render → draw` edge (probed; only named refs
   `render → make_widget → Widget`). So the synchronous `affected` set is
   **container-granular, nominally-complete, never-miss**: coverage holds because a
   member-body edit moves the *container's* `content_hash` (the container hash
   subsumes member bodies) and the container is reachable by the named chain.
   Method-level precision is an **optional async layer** (V2 Phase 9), never
   required for correctness. Guarded by
   `src/tyo3/graph/tests/test_inference_flow_coverage.py` (3 tests, green — **keep
   green every phase**).

### Central V2 idea

`affected` is **sound-and-coarse synchronously** (in-commit, container granularity)
and **precise-and-optional asynchronously** (a Python worker narrows
container→method over a frozen snapshot, publishes on a bus refinement channel;
graceful degradation since the coarse set is a sound superset). Correctness never
depends on the async layer.

### V2 phase map (remaining work) + old→new numbering

| V2 | Title | Status | (was V1) |
|----|-------|--------|----------|
| 0–5 | content gate · native layer · id-delta · cutover · native commit | ✅ **DONE** | same |
| **6** | **Scoped native producer (keystone)** — `affected_ids` transitive at source | ✅ **DONE** (Option B; see §9.6) | *(deferred; not a V1 phase)* |
| 7 | Pure-projection bus; delete read-surface builder + Option-B bridge; refinement-channel seam | 🔴 **NEXT** (interim `aceabf6` landed; V2 reworks) | V1 Phase 6 (bus) |
| 8 | Unified derived invalidation (per-layer key locality) | 🔴 | V1 Phase 7 |
| 9 | Async precision refinement layer | 🔴 | *(new in V2)* |
| 10 | AST-canonical hashing (+ explicit container-subsumes-members invariant) | 🔴 | V1 Phase 9 |
| 11 | Read surface & convenience APIs | 🔴 | V1 Phase 8 |
| 12 | Single config source (+ precision knobs) | 🔴 | V1 Phase 10 |
| 13 | Split the monoliths | 🔴 | V1 Phase 11 |
| 14 | Warnings/typing/hygiene + end-to-end acceptance | 🔴 | V1 Phases 12+13 |

### Status of the "Phase 6 ✅ DONE" work below (§9)

The one-post-commit-path / non-blocking-bus / id-level-delta work **did land**
(`aceabf6` et al., closing defects #2 bus-half and #6) and stays. But it carries
the **interim Option-B bridge** (`_compute_affected`/`_resolve_files` expanding
`affected` over the materialised head graph) precisely because the producer was
deferred. **V2 Phase 7 deletes that bridge** once **V2 Phase 6** makes
`affected_ids` transitive at the source, and also deletes the legacy read-surface
builder and demotes the parity oracle. So treat §9 as "interim bus landed," not
"bus final."

### What already exists (don't rebuild in Phase 6)

The producer *machinery* is built and parity-verified in `rust/src/code_layer.rs`
(`produce_code_delta`, `CodeLayer::diff_from`, `affected_closure`,
`add_edge`/`remove_edge` maintaining `reverse_deps`). **V2 Phase 6 is the
in-commit *scoped driver*, not new machinery.** ✅ **Landed** — see §9.6 below.

### §9.6 — V2 Phase 6: scoped native producer ✅ **DONE** (2026-06-08)

> **`affected_ids` is now transitive at the source.** The in-commit producer
> re-derives the **dirty scope** (changed ∪ created ∪ deleted ∪ one-hop
> importers) in place over the prior `head.code_layer`, maintains `reverse_deps`
> edge-by-edge, and emits the minimal incremental `code_delta`. `code_delta =
> None` is retired for content writes (only non-reconciling `author` carries
> `None`, caught by `_is_authored_only`). The seeds-only-`affected` caveat
> threaded through Phases 3–6 is **resolved**.
>
> **Strategy chosen (user-confirmed 2026-06-08): "Option B" — in-place scoped
> re-derivation, not full-rebuild-plus-diff.** B honours the "cost proportional
> to the edit" invariant and working-rule 3 ("maintain `reverse_deps`
> edge-by-edge, never rebuild per commit"); A (full build every commit, gated to
> cheap passes) was rejected as a guide-rule-3 violation dressed up as cheap.
> A→B is contract-neutral (identical layer/delta/affected), so A would have been
> thrown away; B is landed once. The strict parity oracle bounds the risk.
>
> **What landed**
> - `code_layer.rs`: `CodeLayer` gained a per-file ordered node index
>   (`file_to_nodes`) so the scoped builder reconstructs the order-sensitive
>   `name_to_id` collision fallback identically to a full rebuild.
>   `produce_code_delta_scoped` + `Builder::seeded`/`build_scoped`/`evict_file`/
>   `gc_external_stubs`: seed from `prev`, evict each dirty file's nodes +
>   outgoing edges (via `remove_edge`), re-run the engine passes
>   (`document_symbols`/`occurrences`/`supertypes`) **only** for the dirty scope
>   (carrying non-dirty files verbatim — sound because any cross-file edge
>   implies an import, so an affected importer is itself one-hop dirty), GC
>   orphaned external stubs. `affected_closure_with_deleted` walks `next`'s
>   reverse-deps + seeds **deletions** from `prev` (a deleted id's inbound edges
>   are gone from `next`). The full `Builder::build` survives as the
>   **rescan/cold-start** path (load-bearing, not throwaway).
> - `project.rs`: `run_staged` drives the producer in-lock after reconciliation,
>   before the deferred publish; `Baseline` now captures `code_layer` and
>   `rollback` restores it (rollback test 3 — no torn layer). `produce_layer`
>   routes rescan / cold-start (empty `prev`) → full build, else scoped.
>   `build_commit_delta` threads `Option<CodeDeltaDto>`; the seeds-only
>   `affected_closure` call in `run_identity_reconciliation` is removed (computed
>   in `run_staged` over the maintained index).
> - **`graph.py` (applier bug the real incremental deltas exposed):**
>   `_remove_code_edge` now removes the **specific** matched parallel edge by
>   index (`incident_edges` → `remove_edge_from_index`); it was removing an
>   arbitrary parallel edge by endpoints (`remove_edge(src,tgt)`), which
>   corrupted multi-`IMPORTS`/multi-`REFERENCES` module pairs. Latent until
>   Phase 6 — full rescans never exercised incremental edge removal.
>
> **Lazy population (perf):** `head.code_layer` is **not** built at open (that is
> the `OPEN_TIMEOUT` / ~100x trap). It stays empty until the **first write**,
> which one-time full-builds (emitted as a wholesale rescan delta); every
> subsequent write is scoped. `open()`/concurrency/mvcc/perf slices show no
> regression.
>
> **Gate (2026-06-08):** `cargo test` **158/0**. `pytest -q` — only the **3
> documented pre-existing later-phase failures** (`test_final_derived_contract`
> ×2 → Phase 8; `test_final_hash_ast::test_formatting_only_hashes_same` → Phase
> 10); **zero new**. Parity oracle (`test_incremental_parity.py`, all 9
> scenarios) is now a *real* incremental-vs-rebuild test and green;
> `test_inference_flow_coverage.py` green; new `test_affected_closure.py`
> (4 tests) proves transitivity + the deletion-from-prior-layer subtlety + the
> scoped-not-full-rescan perf guard.
>
> **Leaves for Phase 7:** delete the interim Option-B bus bridge
> (`_compute_affected`/`_resolve_files`) — now that `affected_ids` is transitive
> at the source, the bus delta becomes a pure projection. [[spine-refactor-v2-plan]]

---

### §9.7 — V2 Phase 7: pure-projection bus + refinement seam ✅ **DONE** (2026-06-08)

> **The bus delta is now a pure projection of the `CommitDelta`** — no graph, no
> Option-B bridge, no path→id reconstruction. The interim
> `_compute_affected`/`_resolve_files`/`_to_relative` helpers and the
> `graph`/`root` params of `Delta.from_commit_delta` are deleted;
> `grep CodeGraph\|_compute_affected\|_resolve_files\|_head_graph src/tyo3/bus/`
> is empty. `session._publish_delta` calls `Delta.from_commit_delta(result)` with
> no head-graph argument — the bus no longer depends on a materialised head graph.
>
> **The key design decision (user-confirmed 2026-06-08): keep file-interest as a
> first-class, reverse-dep-aware API and emit `affected_files` natively.** The
> guide's literal "just read `touched_files`" projection would have regressed
> `test_gate8_bus::test_scoped_reverse_dep_delivery` (a file-interest subscriber
> on `app.py` must fire when `models.py`, which it imports, changes — but native
> `touched_files` is only the directly-edited files). The id→file map for
> reverse-dependents lived only in the deleted head graph. Rationale: id-interest
> is *statically* resolvable (closure is native), but file-interest's reverse-dep
> frontier is *dynamic per-revision* — deprecating file-interest wouldn't remove
> that cost, it would relocate it onto every subscriber (a graph dependency, N×,
> consumer-side). So the delta carries it once: **the cheapest place to pay.**
>
> **What landed**
> - `dto/commit_delta.rs`: new `affected_files: Vec<String>` — the
>   project-relative files of the `affected_ids` closure (`<external>` filtered),
>   disjoint role from `touched_files` (directly edited).
> - `project.rs`: `run_staged` computes `affected_files` from `next.nodes`
>   (node→file) right after `affected_closure_with_deleted`, deduped via a
>   `BTreeSet`; deleted seeds are absent from `next` and resolve via
>   `touched_files` instead (correct, not a gap). `build_commit_delta` now takes
>   `&head.root` and emits **project-relative** `touched_files` via
>   `native_to_graph` — so Python does zero path math. The per-category
>   `created`/`changed`/`deleted` fields stay native (consumed by the
>   still-path-shaped derived invalidation → Phase 8); only the bus-facing
>   `touched_files` is normalised.
> - `bus/delta.py`: `from_commit_delta` is a pure projection —
>   `affected = frozenset(delta.affected_ids)`,
>   `files = touched_files | affected_files`. Helpers deleted.
> - `bus/refinement.py` (new): `AffectedRefinement{revision, narrowed, added}` —
>   the refinement-channel **contract seam** (Concept V2 §5.4). `Bus.publish_refinement`
>   fans out on a **separate** channel (own subscription queue, own
>   `poll_refinement`) so a late refinement for R never touches the primary
>   `revision > last` assertion. **Nothing emits a refinement yet** (Phase 9).
> - **7.2/7.3 — nothing to cut (verified, not assumed).** `CodeGraph.build` is
>   already the *thin native builder* (Phase 4 deleted the ~1140-line legacy
>   read-surface build); it is load-bearing as the parity oracle's full-rebuild
>   baseline + the conftest shared cache, so it is **kept**.
>   `test_incremental_parity.py` is the *real* incremental-vs-rebuild oracle and
>   stays. `parity_oracle.py`'s tiered comparator is live test infra (a green
>   `test_final_parity_oracle.py` + `test_graph_incremental.py`), not the legacy
>   builder — **not deleted** ("think before cutting"). No orphaned private
>   helpers in `graph.py`. The Phase-6 `_remove_code_edge` index-precise
>   parallel-edge fix is untouched.
>
> **Strengthened contract:** `test_gate8_bus::test_scoped_reverse_dep_delivery`
> dropped its `_g = session.graph` line — reverse-dep file delivery now works with
> **no** head-graph materialisation, proving the native `affected_files` path.
> New `test_final_bus_contract::test_refinement_channel_delivers_after_primary_delta`
> proves ordered, independent refinement delivery for an already-delivered
> revision. `test_final_bus_contract.py` is fully green (zero xfail).
>
> **Gate (2026-06-08):** full `pytest -q --no-cov` — only the **3 documented
> pre-existing later-phase failures** (`test_final_derived_contract` ×2 → Phase 8;
> `test_final_hash_ast::test_formatting_only_hashes_same` → Phase 10); **zero
> new**. `cargo test` **158/0**. `test_gate8_bus.py` (36) + `test_final_bus_contract.py`
> (5, zero xfail) green; `test_incremental_parity.py` + `test_inference_flow_coverage.py`
> green.
>
> **Leaves for later:** emitting refinements → Phase 9; derived invalidation
> consuming `affected` → Phase 8; bus file structure → Phase 13.
> [[spine-refactor-v2-plan]]

---

### §9.8 — V2 Phase 8: unified derived invalidation + per-layer key locality ✅ **DONE** (2026-06-08)

> **Derived invalidation is now id-level and affected-driven, with declared
> per-layer key locality.** The genuine xfails are real passes, the two stale
> XPASS markers are retired, the gate5 self-healing test is un-skipped, and two
> new locality tests prove the local-vs-semantic split.
>
> **The crux (Concept V2 §5.5): locality lives in one place — the cache key.**
> `derive/dag.py::resolve_input` computes the `input_hash` per declared locality:
> `local` ⇒ the entity's own `content_hashes[profile]` (unchanged); `semantic` ⇒
> `hash(content_hash + dependency-closure fingerprint)`. Because the read path,
> the commit-time invalidate loop, and the scheduler all key off that one
> `input_hash`, the two behaviours fall out of a single mechanism: a `local`
> layer's key only moves when its own text changes (affected-but-unchanged ids
> are correct no-ops), while a `semantic` layer's key moves when any dependency
> changed. The new `_dependency_fingerprint` walks the **transitive forward-dep
> closure over the pinned snapshot graph** (never the live head — §5.9), sorts
> `id=content_hash` pairs, and hashes them.
>
> **What landed**
> - **8.1 key locality (declared, not inferred).** New `KeyLocality{Local,Semantic}`
>   on `rust/src/config.rs::LayerCfg` (`#[serde(default)]` → flows through
>   `config_json`'s `raw`); `config.py::LayerConfig.key_locality` (default
>   `"local"`); `DerivedLayer.key_locality`; the `resolve_input` fold above. This
>   is the **only** Rust touch — one field, no inner-loop Rust.
> - **8.2 one affected-driven loop.** `session._invalidate_derived` feeds the loop
>   **durable ids** — `affected_ids ∪ changed_ids ∪ created_ids`, deletes via
>   `deleted_ids` — replacing the inert path-shaped `result.created|result.changed`
>   feed (which `graph.symbol("a.py")`-raised and got swallowed). `dag.invalidate`
>   compares the locality-aware key per `(layer, id)`, marks stale, enqueues eager.
> - **8.3 read-time staleness + self-heal.** `Snapshot.derived`: present ⇒ `fresh`
>   (content-addressed key already encodes move-reuse); miss ⇒ synchronous
>   `recompute_now` over the pinned snapshot (serves both `block` and `stale` —
>   there is no async worker until Phase 9); on failure ⇒ last-good (`stale`/
>   `failed`) else `failed` (recorded failure) else `absent`. Unresolvable id
>   (deleted / not reconciled to this snapshot) ⇒ `absent`, not an `AttributeError`.
> - **8.4 one pinned snapshot.** `dag.invalidate` uses a single cold snapshot for
>   invalidation **and** eager recompute, closed in `finally`. Deleted the leaked
>   second `session.snapshot()` (the XPASS leak warning is gone).
> - **8.5 typed store errors.** New `StoreError` base + `StoreBackendBroken`
>   (`StoreBackendUnavailable` reparented). `FsStore.get`: `FileNotFoundError` ⇒
>   `None`, every other `OSError` ⇒ `StoreBackendBroken`. `LanceDbStore` stops
>   `except Exception: pass` on query/write/delete/nearest — a genuine empty
>   result is still `None`, a backend failure now propagates (§5.12).
> - **8.6 tests.** Retired 2 stale `xfail(strict=True)` + 3 genuine xfails in
>   `test_final_derived_contract.py`; un-skipped `test_gate5_derived::test_self_healing_*`;
>   added `test_semantic_layer_recomputes_on_dependency_change` +
>   `test_local_layer_skips_dependency_only_change` (a 2-function module where
>   `caller` references `dep`; editing `dep`'s body puts `caller` in `affected`
>   without changing its own hash — semantic recomputes, local does not).
>
> **Two triage calls (verified, not assumed)**
> 1. **gate5 self-healing move-leg rewritten to an *atomic* `edit_many` move.** As
>    written it did a non-atomic 3-commit move and reused the original `foo_id`.
>    Verified: identity reconciliation does **not** rebind a DurableId across a
>    multi-commit move (the new location gets a fresh id), and overlay-created new
>    files don't enter the snapshot graph at all — both producer/identity concerns,
>    out of Phase 8 scope. The atomic move (the pattern the already-green
>    `test_move_unchanged_reuses_artifact` uses) faithfully tests "move unchanged ⇒
>    artifact reused."
> 2. **gate7's `uppercase_generator(artifact: bytes)` has the wrong arity** (gets a
>    list of `GenInput`, always `GeneratorFailed`s). The test only asserts the
>    status is `fresh/stale/failed`; the read path returns `failed` (not `absent`)
>    on a recompute failure with no last-good, preserving that.
>
> **Design note (deviation from guide §8.3, user-approved up front):** with no
> async worker in Phase 8, `stale` and `block` converge on the success path —
> both recompute synchronously at read. Honest async stale-serving is Phase 9
> (the refinement channel). Every target test was traced against this model.
>
> **Gate (2026-06-08):** full `pytest -q --no-cov` — **670 passed, 2 xfailed,
> 1 failed**; the lone failure is `test_final_hash_ast::test_formatting_only_hashes_same`
> (→ Phase 10), the only documented remaining baseline; **zero new**, **zero
> XPASS**. `cargo test` **158/0**. The Phase-7 bus contracts
> (`test_final_bus_contract.py`, `test_gate8_bus.py`), `test_gate5_derived.py`,
> `test_gate7_read_surface.py`, and `test_inference_flow_coverage.py` stay green.
>
> **Leaves for later:** async method-precision narrowing of `affected` → Phase 9
> (the derived loop will consume the narrowed set when a refinement arrives);
> read-surface / convenience-view lifetime → Phase 11; cross-revision move
> identity-tracking and overlay-new-file graph surfacing are pre-existing
> producer/identity gaps (not Phase 8).
> [[spine-refactor-v2-plan]]

---

### §9.9 — V2 Phase 9: async container→method precision refinement ✅ **DONE** (2026-06-08)

> **The optional precision layer is live: a background worker narrows the
> container-granular coarse `affected` set to method precision over a frozen
> snapshot and publishes an `AffectedRefinement` on the Phase-7 channel.** It is
> purely additive — the system is fully correct without it, and a lagging,
> crashing, or disabled worker leaves the synchronous coarse set intact
> (graceful degradation, proven by a test). Default `precision = container` means
> the worker is never built and the writer pays nothing.
>
> **The narrowing (the crux).** Editing `Widget.draw`'s body moves both `draw`'s
> and `Widget`'s content hash (subsumption), so `changed_ids ⊇ {Widget, draw}`
> and the coarse `affected` reaches every Widget referrer via the named chain
> (`consume_a`, `consume_b`, `make_widget`, …). The refiner classifies `Widget`
> as a **subsumption-only container** (a changed class owning a changed member),
> so it does **not** re-expand Widget's full referrer set — that container-
> granular fan-out is exactly the over-fire being narrowed. Instead it resolves
> the changed *member* precisely:
>   1. `changed member id → (file, selection_range.start)` via the snapshot graph
>      (`graph.symbol(id)`).
>   2. `snap.find_references(file, line, col)` — ty's type-aware resolver returns
>      `w.draw()` in `consume_a` but **not** `w.serialize()` in `consume_b`.
>   3. each occurrence `(path, range) → enclosing entity id` via a tightest-range
>      lookup over `graph.symbols_in_file` (the **id↔position↔id bridge** — graph
>      files are project-relative, ref paths absolute, reconciled by an
>      abs-path file index keyed off `snap._root`).
>   4. `narrowed = (member_users ∪ closure ∪ changed) ∩ affected ∪ changed` —
>      always a subset of the coarse set, always retaining `changed_ids`.
> Result: `{Widget, consume_a, consume_b, make_widget, draw}` → `{Widget,
> consume_a, draw}` (the serialize-only user and the bare constructor dropped).
>
> **Salsa-safe by construction.** The worker opens `session.snapshot(at=R)` (an
> independent frozen Zalsa db), queries, and closes it in `finally` — it never
> touches the live head db (V1 §3: a reader sharing the live db blocks/cancels
> the writer). It reads the *already-built* `session._bus` directly (never
> `_get_bus()`), so it never builds a bus the writer didn't ask for and no-ops
> when there are no subscribers.
>
> **Fed without blocking primary delivery.** `session._after_commit` keeps its
> order (`_invalidate_head_snap → _apply_graph_delta → _schedule_derived →
> _publish_delta`) and appends `_maybe_refine(delta)` **strictly last** — so the
> coarse set is always published before precision is even considered. With
> `precision=method` the coarse `(revision, changed_ids, affected_ids)` is handed
> to the refiner (async: enqueued on a daemon `queue.Queue`; sync: computed
> inline) which narrows and publishes. The daemon swallows its own exceptions and
> stays alive; `_maybe_refine` swallows wiring errors — a refiner failure can
> never surface as a write-path failure or a miss.
>
> **What landed**
> - **9.1 config (single-source).** `rust/src/config.rs`: new `[code_graph]` →
>   `CodeGraphCfg { precision, refinement }` on `RawConfig` (`#[serde(default)]`,
>   `deny_unknown_fields`), defaults `container`/`async`; `ConfigError::InvalidValue`
>   + a `validate()` arm rejecting unknown values **at open** (deny_unknown_fields
>   on `RawConfig` made a native struct unavoidable anyway, so this is also the
>   long-term-correct shape). Surfaced through `config_json → TyConfig.code_graph`
>   (`config.py::CodeGraphConfig`) — **no second TOML parser** (unlike the watcher's
>   `_read_coordination_config`, which Phase 12 will fold in). Session reads
>   `self._config.code_graph.{precision,refinement}`. 4 new `cargo test` config
>   cases (accept/reject/defaults).
> - **9.2 worker + wiring.** New `src/tyo3/precision/{__init__,refiner.py}`:
>   `PrecisionRefiner` (daemon thread + queue, mirroring the watcher precedent;
>   stop+join in `session.close()` before bus teardown). `session._maybe_refine`
>   / `_get_refiner` (lazy) wired into `_after_commit` after `_publish_delta`.
> - **9.3 tests.** `test_precision_refinement.py` (4, green): `test_narrows_to_member_users`
>   (async, the full narrowing), `test_graceful_degradation_on_worker_crash`
>   (monkeypatched-to-raise worker ⇒ coarse set still delivered & complete, writer
>   never raises, no refinement leaks, session stays healthy), `test_container_mode_is_a_no_op`
>   (refiner never built, no refinement), `test_sync_refinement_runs_inline`.
>
> **NOT built: 9.4 expansion mode.** Off-by-default, eventually-consistent on the
> non-nominal miss class (§5.3); no use case requested it. The refiner is
> narrow-only — `added` is never published.
>
> **Precision boundary (documented, not a correctness gap).** A class that
> simultaneously changes its own structure *and* a member body is narrowed by
> member-users only, which can drop a pure-type referrer of that class. This is a
> *precision* limit (the coarse set is always delivered first), made rigorous when
> Phase 10's container-subsumes-members hashing lets the refiner distinguish a
> subsumption-only change from a genuine structural one.
>
> **Gate (2026-06-08):** `cargo test` **162/0** (158 + 4). `test_precision_refinement.py`
> 4/4 green. Full `pytest -q --no-cov` unchanged from the Phase-8 baseline +4 new
> greens; only remaining baseline failure stays `test_final_hash_ast::test_formatting_only_hashes_same`
> (→ Phase 10). Phase-7 bus contracts, Phase-8 derived suites, and
> `test_inference_flow_coverage.py` stay green; `container` default leaves every
> existing suite untouched.
>
> **Leaves for later:** the derived loop consuming a refinement (optional/forward,
> Phase 8 made it possible); 9.4 expansion if ever needed; snapshot-lifetime
> hygiene reviewed with Phase 11; config consolidation (drop the watcher's direct
> TOML read) in Phase 12.
> [[spine-refactor-v2-plan]]

---

### §9.10 — V2 Phase 10: AST-canonical content hashing + container-subsumes-members invariant ✅ **DONE** (2026-06-08)

> **The content hash is now computed over a canonical rendering of the entity's
> AST subtree, not a line-by-line text heuristic.** The hash reflects *meaning,
> not formatting*: whitespace outside literals and trailing-comma style are
> insignificant (they aren't in the AST), while literal *content* — including the
> exact text inside string literals — is significant. Comments are never in the
> AST so are always excluded; docstrings are identified **positionally** (the
> first string-statement of a def/class/module body) and excluded/included per
> policy. The container-subsumes-members property is promoted from an accident to
> a **documented, tested invariant**.
>
> **AST source: reuse the ty db's parsed module (no re-parse, no new prod dep).**
> ty's `document_symbols` sets `full_range == Stmt::range()` for
> def/class/assign/import symbols, so an entity's `full_range` looks up its
> defining `Stmt` by **exact range equality** in a per-file index built from
> `ruff_db::parsed::parsed_module(db, file)` — the same module ty already parsed
> for this revision. This sidesteps the re-parse-the-slice trap (a sliced method
> body is indented and won't parse standalone). A class's `StmtClassDef` subtree
> inherently contains its methods' `StmtFunctionDef` bodies → subsumption for free.
>
> **The renderer (`rust/src/hash.rs`).** A `SourceOrderVisitor` (`CanonicalRenderer`)
> emits a `\u{1f}`-separated token stream: `enter_node` emits one tag per node
> *kind* (structural skeleton over every node type); `visit_identifier` captures
> all names uniformly (def/class/attr/param/alias/keyword); `visit_expr` adds the
> scalar leaves the traversal omits — `Expr::Name.id`, and crucially the
> **boolean/comparison operators** (the source-order traversal surfaces `Operator`
> and `UnaryOp` but **not** `BoolOp`/`CmpOp`, so `a and b`≡`a or b` and `a<b`≡`a>b`
> would have collided); `visit_string_literal`/`visit_bytes_literal`/
> `visit_interpolated_string_element` emit exact literal content; `visit_stmt`
> emits the `is_async` flag (skipped by the traversal). Docstrings: a one-shot
> `pending_doc_scope` flag set in `visit_stmt` for def/class (and at the module
> entry) is consumed by the next `visit_body`; no other `visit_body` runs between
> a def/class and its own body, so nested scopes nest correctly and a leading
> string in an `if`/`for` suite is **not** mistaken for a docstring.
>
> **Both hashing call sites converted to one shared renderer.** (1) `entity.rs`
> (the `Entity.content_hash` / identity-registry anchor — read by the no-config
> tests via `sym.content_hash`); (2) `convert/symbols.rs::collect_symbols_recursive`
> (the per-profile `content_hashes` DTO map — read by the profile tests and reused
> by the `code_layer` producer and graph nodes). Both call `hash::entity_normal_form`,
> which renders the matched `Stmt` (degraded raw-slice fallback only if no stmt
> matches — never happens for real symbols). `project.rs::compute_document_symbols`
> builds the stmt index once per call and threads it down. **Encoding kept
> DECIMAL** end-to-end (`ContentHash(u128)` is already ≥128-bit/deterministic/
> machine-stable; hex would force a derived-cache-key rebuild for zero benefit —
> 10.3 is "don't regress encoding"). The old `normalise_entity_source` /
> `collapse_whitespace` / `is_likely_docstring_line` / `strip_trailing_comma` and
> the two `extract_range` helpers were **deleted**, not extended.
>
> **The three target tests flipped** (`test_final_hash_ast.py`): the lone baseline
> failure `test_formatting_only_hashes_same` now passes; the two
> `xfail(strict=True)` tests (`test_string_literal_whitespace_hashes_differently`,
> `test_docstring_policy_and_non_docstring_strings`) pass with the markers removed
> and the stale "Phase 9" (V1) references corrected to "Phase 10" (V2).
>
> **Invariant promoted (10.4).** New Rust test
> `hash.rs::class_hash_changes_when_method_body_changes` (a class hash moves when a
> method body changes) co-located with the renderer, plus a load-bearing module
> doc-comment: *container-subsumes-members is required for `affected`-set coverage
> of inference-flow deps — never hash members independently of their container for
> "finer cache keys"; do it in the derived layer's key (Phase 8), or you silently
> reintroduce a §5.4 no-miss regression (Concept V2 §5.2–§5.3).* The three Python
> `test_inference_flow_coverage.py` guards stay green.
>
> **Gate (2026-06-08):** `cargo test` **167/0** (162 + 5 new hash tests:
> string-literal-content, comparison/bool operators, non-docstring-string,
> class-subsumes-member, whitespace-around-operators). `ruff_python_parser` added
> as a **dev-dependency only** (hash.rs unit tests parse a snippet then render;
> production never re-parses). `test_final_hash_ast.py` 5/5 green, **zero xfail /
> zero XPASS**. `test_inference_flow_coverage.py` 3/3 green. Phase-7 bus, Phase-8
> derived suites, and Phase-9 precision suite unaffected (a content-addressed
> cache-key *value* change is fine; the key *shape* is unchanged).
> [[spine-refactor-v2-plan]] [[phase9-precision-refinement-done]]

---

### §9.11 — V2 Phase 11: owned-lifetime convenience reads + typed read errors ✅ **DONE** (2026-06-08)

> **No convenience read returns a view backed by an already-closed snapshot
> (deviation #7 closed), the floating-latest surface no longer hands out the
> mutable canonical head graph, and the layer views stop swallowing read
> failures into `None` (typed absence ≠ typed failure, V1 §5.12).** Pure-Python
> phase — no Rust, no rebuild. The Phase-10 baseline (fully green
> `pytest -q --no-cov`) is preserved; no new xfail/XPASS.
>
> **Lifetime-shape decision (11.1): view-owns-snapshot, NOT remove-the-sugar.**
> The acceptance test `test_latest_warm_snapshot_consistent` calls `session.code`,
> `session.layer("upper")`, `session.entity(id).code`, and `session.diff(snap2)`
> as **one-liners** — never via `with session.snapshot()`. So the guide's
> *preferred* "remove the lazy sugar, force an explicit pinned snapshot" shape
> would break the gate's own call sites; the tests drove the choice to the guide's
> *alternative* (the returned view owns + closes its snapshot).
>   - **The two genuinely-lazy reads** (`session.code`, `session.layer`) return a
>     new **`_OwnedView`** wrapper (`session.py`, defined just above
>     `TyO3Session`). It pins the fresh head snapshot for the view's lifetime and
>     closes it on context-manager exit / `close()` / GC, delegating all reads to
>     the wrapped `CodeLayerView`/`DerivedLayerView`/`AuthoredLayerView` via
>     `__getattr__`. **The footgun is gone by construction** — the snapshot is
>     never closed before the view is handed back (the Pitfall the guide names).
>   - **Why a wrapper, not an `_owns` flag on the view classes:** those classes
>     are *shared* with `Snapshot.code`/`Snapshot.layer`, where the `Snapshot`
>     owns the lifetime (closing there would be wrong). More subtly, a `Snapshot`
>     **caches** its views and each view refers back to the `Snapshot` — a
>     reference cycle. Putting the owned-snapshot on the view would drag it into
>     that cycle, so reclamation would fall to the cyclic GC with unspecified
>     `__del__` order → a spurious `Snapshot` `ResourceWarning`. `_OwnedView` sits
>     **outside** the cycle (wrapper → snapshot → view → snapshot), so refcount
>     reclaims it deterministically and the snapshot is released promptly with no
>     warning and no leak.
>   - **The two eager reads** (`session.entity`, `session.diff`) were already
>     safe — `EntityView.from_snapshot` (`models/view.py`) and
>     `SnapshotDiff.compute` (`models/diff.py`) fully materialise frozen
>     dataclasses *before* the snapshot closes. They were rewritten to acquire the
>     snapshot via `with self.snapshot() as snap: return snap.entity(...)` /
>     `snap.diff(...)` so the materialise-then-close contract is explicit (and a
>     future reader can't "fix" them into lazy state). The docstrings now state
>     the eager-before-close rationale.
>
> **Floating-latest honesty (11.2).** `LatestView` already enforced the honest
> boundary (no `entity`/`diff`/`revision`/`close` — verified, untouched). Its
> only remaining leak was `graph()` returning `session.graph` — the **mutable,
> in-place-maintained canonical** head graph, a reference that mutates under the
> caller. It now returns `session.graph._pin_at(session.head)` — an **immutable,
> `_frozen=True`, revision-stamped point-in-time copy**: clearly non-canonical,
> floats per call (re-pins newest head), never mutates under the caller, never
> advances head. (Sibling `graph()` accessors left canonical: `session.py:~795`
> = `TyO3Session.graph` canonical head; `Snapshot.graph` = canonical pinned.)
>
> **Typed absence vs typed failure (11.3).** Removed the broad
> `except Exception: return None` / `: continue` that conflated "not present"
> with backend/format/graph-build failure:
>   - `layers/authored.py` — `value()`: keep `status == "absent" → None`
>     (legitimate absence), drop the `except` so a backend/format error
>     propagates. `ids()`: narrow the native-fast-path `except` to
>     `(AttributeError, NotImplementedError)` (genuinely-unsupported enumeration
>     only — and the native snapshot has **no** `authored_ids`, so the fast path
>     is dead and the graph scan is the live path) and drop the per-id scan
>     `except` (absence is a *status*, not an exception). `diff()`: drop the
>     per-id `except`.
>   - `layers/derived.py` — `value()`: drop the `except`; `snap.derived()` already
>     reports absence *in the value* (`status == "absent"`), never `None`, so the
>     `except` only ever hid genuine store/scheduler/generator failures. Return
>     type tightened `DerivedValue | None → DerivedValue`. Left
>     `_cache_key`/`_input_hash_for`'s `node is None → None` (legitimate absence).
>   - `layers/code.py` — **verified, no change.** Both `value()` and
>     `_content_hash` already return `None` only for genuine absence (`node is
>     None` / not an entity id) and never wrap the read in a broad `except`, so a
>     graph-build failure already propagates.
>   - **No production caller** dereferences these layer-view `value()`/`ids()`
>     methods (only `EntityView`/`SnapshotDiff` read via `snap.authored`/
>     `snap.derived` directly), so the tightened semantics regress nothing.
>
> **Phase-9 refiner carry-over: confirmed, no change.** `precision/refiner.py`
> opens `session.snapshot(at=R)` and closes it in a `finally`
> (`refiner.py:229-234`) — already correct; nothing to fix.
>
> **Reads never advance head — preserved.** `test_final_no_read_side_writes.py`
> (3/3) stays green: `_OwnedView` only pins/copies; `LatestView.graph()` is a
> pure projection + copy; the eager reads materialise without writing.
>
> **Gate (2026-06-08):** `test_gate7_read_surface.py` **15/15** +
> `test_final_no_read_side_writes.py` **3/3** green (`-rA`). Full
> `pytest -q --no-cov` green — Phase-10 baseline preserved, **zero new
> xfail/XPASS**.
>
> **Out of scope / leaves for later.** `models/diff.py:120,133` still wrap a
> derived/authored layer-diff in `except Exception: continue` — outside Phase 11's
> declared layer-view scope; folds into **Phase 14.3** ("replace broad
> `except Exception: pass` with typed handling"). Module structure (the
> `_OwnedView` placement, view-module split) is **Phase 13**.
> [[spine-refactor-v2-plan]] [[phase9-precision-refinement-done]]

---

## 1. The six defects this refactor removes (from §6.3 of the Concept)

| # | Defect | Status |
|---|--------|--------|
| 1 | Snapshot construction reads live disk (`pre_populate_generation` at snapshot time) | ✅ **Phase 1 DONE** |
| 2 | Transaction split across the lock boundary (Rust lock released before Python graph delta + bus) | ✅ **DONE** (Phase 5 in-lock stage→publish-last→rollback; Phase 6 closed the bus/post-commit-path half: one `_after_commit` hook, id-level deltas in asserted revision order, non-blocking bus) |
| 3 | Read accessor performs a write (`session.graph` → `sync_all` → advances head) | ✅ **Phase 4 DONE** |
| 4 | Delta is path-shaped not id-level (`SyncResultDto` has file-path strings, not `DurableId`s) | ✅ **Phase 3 DONE** |
| 5 | Derived invalidation is silently inert (fed path-shaped values, opens snapshot it never closes) | 🔴 **V2 Phase 8** |
| 6 | One write path forgets to publish (`discard` applies graph delta but never publishes to bus) | ✅ **DONE** (the single `_after_commit` hook makes "publish every revision" true by construction) |
| 7 | Convenience reads return views over closed snapshots (`session.code`, `.layer`, `.entity`) | ✅ **V2 Phase 11 DONE** (`_OwnedView` owns/pins the snapshot; eager `entity`/`diff` materialise-then-close; floating-latest `graph()` non-canonical; layer views stop swallowing read failures) |
| 8 | Hashing is text-heuristic not AST-canonical (collapses whitespace inside string literals) | 🔴 **V2 Phase 10** |
| 9 | Config parsed twice with silent fallback (Python re-reads config.toml, swallows errors) | 🔴 **V2 Phase 12** |
| 10 | Three central files are monoliths (`project.rs`, `session.py`, `graph/graph.py`) | 🔴 **V2 Phase 13** |

> **Phase numbers in this table are V2** (see §0). Beyond these 10 defects, V2 adds
> the deferred keystone — the **scoped in-commit producer (V2 Phase 6)** that makes
> `affected_ids` transitive at the source — and a new **async precision refinement
> layer (V2 Phase 9)**.

---

## 2. Phase Dashboard

| Phase | Goal | Final Tests | Rust Files | Py Files | Status |
|-------|------|-------------|------------|----------|--------|
| **0** | Invariant tests + parity oracle | 8 test files | — | `parity_oracle.py`, 8 test files | ✅ **DONE** |
| **1** | Complete committed generations (content gate) | `test_final_content_spine.py` | `content.rs`, `project.rs`, `overlay.rs` | — | ✅ **DONE** (2 carry-overs → Phase 5) |
| **2** | Native code layer + code delta (parity-only) | Parity suite | `code_layer.rs` (new), `entity.rs`, `dto/code_delta.rs` (new) | `graph/graph.py` (applier), `parity_oracle.py` (existing) | ✅ **DONE** |
| **3** | Id-level commit delta | `test_final_commit_delta_contract.py` | `identity.rs`, `dto/commit_delta.rs` (new), `code_layer.rs`, `project.rs` | `models/delta.py` (new) | ✅ **DONE** |
| **4** | Cutover: Python graph as pure applier | `test_final_no_read_side_writes.py`, parity suite | `project.rs`, snapshot code-delta accessor, `code_layer.rs` (producer bound), `dto/commit_delta.rs` (`Option`) | `graph/graph.py` (~1140 lines deleted), `session.py` | ✅ **DONE** |
| **5** | Single native `commit()` with staging + rollback | `test_final_transaction_rollback.py` | `project.rs`, `content.rs`, `lib.rs` | `exceptions.py`, `session.py` | ✅ **DONE** |
| **6** | One post-commit path; non-blocking bus | `test_final_bus_contract.py` | `config.rs` (overflow policy) | `session.py`, `bus/` | ✅ **DONE** |
| **7** | Repair derived layers | `test_final_derived_contract.py` | — | `derive/`, `stores/` | 🔴 **Not started** |
| **8** | Read surface + convenience APIs | `test_final_no_read_side_writes.py` | — | `session.py` | 🔴 **Not started** |
| **9** | AST-canonical hashing | `test_final_hash_ast.py` | `hash.rs` | — | 🔴 **Not started** |
| **10** | Single config source | Config tests | `config.rs` | `config.py` | 🔴 **Not started** |
| **11** | Split monolith files | Full suite | `project.rs` → many | `session.py`, `graph/graph.py` | 🔴 **Not started** |
| **12** | Zero warnings, typing, exception hygiene | `ruff check`, `clippy` | All | All | 🔴 **Not started** |
| **13** | End-to-end acceptance suite | `test_final_acceptance.py` | — | `test_final_acceptance.py` (new) | 🔴 **Not started** |

> ⚠️ **This dashboard uses V1 numbering (historical).** For the current remaining
> work and numbering, see **§0** and `REFINED_IMPLEMENTATION_PLAN_V2.md`. In V2:
> the **producer is Phase 6** (next), the **bus is Phase 7** (rework the interim
> `aceabf6`), derived → 8, async precision → 9 (new), hashing → 10, read surface →
> 11, config → 12, split → 13, hygiene+acceptance → 14.

---

## 3. Phase 0 — Invariant tests first + parity harness ✅ **DONE**

Phase 0 was completed and shipped. The following test files encode the target contracts, each tagged with its target phase:

| Test File | Tests | Target Phase | Markers |
|-----------|-------|--------------|---------|
| `test_final_content_spine.py` | 5 tests: snapshot isolation from disk, identical-at-R, no disk read at capture, eviction error | **Phase 1** | 1 `xfail(strict)` — disk-read counter seam |
| `test_final_no_read_side_writes.py` | 3 tests: graph read, snapshot graph, latest check — none advance head | **Phase 4** | 3 `xfail(strict)` — read-side writes still exist |
| `test_final_commit_delta_contract.py` | 6 tests: id-level delta, structured moves, no-over-fire, rescan | **Phase 3** | Marked `xfail` at module level |
| `test_final_transaction_rollback.py` | 3 tests: identity/authored/code-layer failure rolls back fully | **Phase 5** | Marked `xfail` at module level |
| `test_final_bus_contract.py` | 4 tests: every write publishes, id-level ordered deltas, slow subscriber, config reject | **Phase 6** | 3 `xfail(strict)` |
| `test_final_derived_contract.py` | 5 tests: id-level invalidation, move reuse, precise recompute, no leak, failed staleness | **Phase 7** | 5 `xfail(strict)` |
| `test_final_hash_ast.py` | 5 tests: formatting stable, string whitespace significant, docstring policy, signature surface | **Phase 9** | 2 `xfail(strict)` |
| `test_final_parity_oracle.py` | 9 tests: comparator, structural/cosmetic, projections, `assert_parity` | **Phases 2–4** | 1 `xfail(strict)` — native half not ready |

**Key artifacts built in Phase 0:**
- `src/tyo3/tests/parity_oracle.py` (452 lines) — the tiered comparator with `compare_graphs`, `assert_graphs_equal`, `assert_parity`, `legacy_graph`
- All 8 `test_final_*.py` files committed, with appropriate `xfail(strict=True)` markers

---

## 4. Phase 1 — Content Gate: Complete Committed Generations ✅ **DONE**

> **Completed & verified 2026-06-07.** All six steps implemented (commits
> `1061976`..`3c49c5b`) and independently audited against the guide. Focused gate
> green: `cargo test content overlay project` (23/19/30), `config` (23 incl. the
> partial-config fix), and the full Phase 1 pytest acceptance set
> (`test_final_content_spine` + `test_mvcc_snapshots` + `test_mvcc_concurrency`).
>
> **Fixes applied during verification (beyond the intern's 6 commits):**
> - **Config-load bug** (pre-existing, surfaced by acceptance test 5): a partial
>   `.tyo3/config.toml` (e.g. only `[spine] retain_cap`) left `hashing.profiles`
>   empty so the default `spine.default_hash_profile = "structure"` dangled and
>   `open()` failed. `RawConfig::load` now seeds the built-in `structure` profile
>   (matches `defaults()`); regression test added. Unblocked
>   `test_pinning_evicted_revision_raises_typed_error`.
> - **Revision convention** (Step 1.3 audit the intern missed): head now starts at
>   **revision 1** after open-ingest. Updated
>   `test_write_path::test_head_starts_at_one_after_open_ingest` to assert it.
> - **Sidecar-at-open**: opening now creates `.tyo3/identity.db` (reconcile-at-open
>   persists). This is **acceptable, not a §5.10 violation** — `.tyo3/` is
>   git-ignored. Relaxed `test_open_without_sidecar_touches_no_source_files` to
>   check only the *source* tree. See CONCEPT §5.10 clarification.
> - **test fragility**: `test_graph_queries::test_children` assumed `modules[0]`
>   has children; Phase 1's ingest reordered files. Robustified (graph is correct).
> - Stale `pre_populate_generation` doc-comments removed from `build_frozen`.
>
> **⚠️ Two regressions intentionally DEFERRED to Phase 5** (xfail-marked, documented
> in `PHASE_5_IMPLEMENTATION_GUIDE.md` §1 "Carried over from Phase 1"):
> 1. **`sync_all` no longer discovers files created after open** — it republishes
>    the existing generation + Rescan instead of re-ingesting disk. Fix: route
>    `sync_all` through `ingest_project`. xfail:
>    `test_graph_build::test_content_hash_updates_incrementally_by_semantic_body`.
> 2. **Watcher drops all events** — `apply_watch_events`' `has_overlay()` buffer-wins
>    guard now matches every ingested file. Fix: distinguish unsaved buffers from
>    ingested content in the commit funnel. xfails: `test_watch::test_deleted_event`,
>    `test_watch::test_injected_change_matches_expected_delta`,
>    `test_watch::test_real_watcher_observes_disk_change`,
>    `test_gate8_bus::test_inject_changes_fires_bus`.

### 4.1 What was done

1. **`is_project_relevant()` authority** — promote the existing `snapshot_relevant_file` predicate, add `.tyo3/` exclusion
2. **Disk ingest helpers** — `ingest_project()`, `apply_disk_batch()` on `ContentStore`
3. **Disk-read counter seam** — `Arc<AtomicU64>` bumped per file read, exposed as `project_content_disk_reads()` on `PyTyProject`
4. **Seed content at open** — ingest project after config load, *before* building the live db; reconcile identity at open
5. **Delete `pre_populate_generation`** — remove from `build_frozen`; O(1) snapshot capture
6. **Frozen overlay strictness** — confirm/lock in with tests that frozen overlay never falls through to disk

### 4.2 Files in scope

| File | Change |
|------|--------|
| `rust/src/content.rs` | Add `ingest_project()`, `apply_disk_batch()`, `is_project_relevant()` (or new `relevance.rs`) |
| `rust/src/project.rs` | Remove `pre_populate_generation()`; change open path to ingest first; add counter seam |
| `rust/src/overlay.rs` | Add frozen-strictness tests; review `walk_directory` for frozen fallthrough |
| `src/tyo3/tests/test_final_content_spine.py` | Remove the `xfail` marker from `test_snapshot_construction_reads_no_disk` |

### 4.3 Concrete changes needed

#### `content.rs`
- Add `Document::Text` already exists with `ContentHash` (line ~45)
- `apply_batch` already exists (line ~203); either rename to `apply_overlay_batch` or leave as-is
- New: `ingest_project(root: &SystemPath, filter: impl Fn(&SystemPath) -> bool) -> Revision`
- New: `apply_disk_batch(paths: &[SystemPath]) -> Revision`
- The walk reuses `OsSystem::walk_directory` pattern from current `pre_populate_generation`
- Every file actually read from disk bumps an `Arc<AtomicU64>` counter

#### `project.rs`
- `build_frozen` (line ~1114): replace `pre_populate_generation` call with direct generation usage
- `build_head_with_config` (line ~955): ingest store before the capture used to build db
- `open` (line ~1680): sequence = load config → create ContentStore → `ingest_project` → build db → reconcile identity
- Add `fn project_content_disk_reads(&self) -> u64` on `PyTyProject`
- Delete `pre_populate_generation` function (lines ~1067–1112) and unused imports
- Initial revision convention: store seeds `Revision(0)` empty; `ingest_project` as first batch → `Revision(1)`. Document and assert.

### 4.4 Acceptance
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml content overlay project
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_final_content_spine.py \
  src/tyo3/tests/test_mvcc_snapshots.py \
  src/tyo3/tests/test_mvcc_concurrency.py -q --no-cov
```

### 4.5 Git commit series
1. `refactor(content): single is_project_relevant authority; exclude sidecar`
2. `feat(content): disk ingest helpers (ingest_project / apply_disk_batch)`
3. `feat(project): project-content disk-read counter seam`
4. `refactor(project): ingest project content at open; reconcile identity at open`
5. `refactor(content): remove snapshot disk pre-population; O(1) capture`
6. `test(overlay): lock in frozen-overlay strictness; no disk fallthrough`

---

## 5. Phase 2 — Native code layer + code delta (parity-only) ✅ **DONE**

> **Completed & verified 2026-06-07.** All five steps implemented; the parity
> oracle's native half now exists and **structural parity is exact** for the
> `_PROJECT_A` fixture (8 nodes, 15 edges, zero structural *and* zero cosmetic
> diffs). `test_final_parity_oracle.py::test_assert_parity_native_half_matches_legacy`
> (renamed from `..._not_ready_yet`, xfail removed) is green. Milestone gate:
> full Rust suite **145 passed**; full Python suite green (see §17 countdown — the
> parity xfail is retired).
>
> **What landed**
> - `entity.rs`: `Entity` gained `file`, `full_range`, `name_range`,
>   `qualified_name` (the node-facing decomposition of `qualified_path`). Unit
>   test pins the name range to the symbol name only.
> - `code_layer.rs` (new): `CodeLayer { nodes, edges (derive Ord), reverse_deps }`,
>   synthetic-id helper `make_module_durable_id`, and `produce_code_delta` — a
>   faithful native port of the legacy six-pass build. It reads the **same**
>   analysis cores the FFI read surface uses (`compute_document_symbols` /
>   `compute_file_occurrences` / `compute_supertypes`), so node payloads are
>   identical to the legacy build by construction, then ports graph.py's
>   resolution case-for-case: ordered name→id map with the `""` short-name
>   collision sentinel + ordered file-scan fallback, range-cache enclosing-symbol
>   lookup, import edges + external stubs, two-pass inheritance (all `inherits`
>   then all `overrides`), and the overrides BFS.
> - `dto/code_delta.rs` (new): `CodeNodeDto` / `CodeNodeMovedDto` / `CodeEdgeDto`
>   / `CodeDeltaDto`, field names matching the Python applier; carries the
>   structural `name`/`external` and cosmetic `package`/`content_hashes`
>   explicitly so the applier is trivial and parity-exact.
> - `project.rs`: `CodeLayer` field on `HeadState` (carried for Phase 3, empty
>   in Phase 2); `PyTyProject.full_code_delta()` returns a full cold-start delta
>   (pure read, no head mutation) — the surface the parity oracle probes.
> - `graph/graph.py`: `apply_code_delta` — a **pure** applier (no FFI, no
>   session/snapshot), Phase-4-ready, plus `_add_code_edge` / `_remove_code_edge`
>   / `_node_from_code_delta` helpers.
>
> **Deliberately deferred:**
> - **Eager in-commit hookup (§5.3 step 4).** The guide wires `produce_code_delta`
>   into every commit. The Phase 2 producer is a *full* build (full semantic
>   analysis: occurrence resolution + cold typeshed/type-hierarchy warmup), so an
>   eager per-commit/per-open hookup made `open()` ~100×slower (1-file fixture:
>   ~4.5 s vs ~0.7 s) and serialised on the GIL — regressing
>   `test_concurrency::test_no_deadlock_on_repeat_sessions` (the only NEW failure
>   the eager wiring introduced). **Decision (user-confirmed 2026-06-07): defer
>   the eager hookup to Phase 3**, where the producer becomes incremental/scoped
>   (re-analyse only dirty files + importers, diff against the stored layer) and
>   the build is driven lazily/incrementally rather than eagerly per open. Phase 2
>   serves parity on demand via `full_code_delta()`; the `CodeLayer` field +
>   `reverse_deps` are in place so Phase 3 inherits a correct index. The deferral
>   is documented in `run_identity_reconciliation` (project.rs).
> - The legacy `CodeGraph.build` stays authoritative (cutover is Phase 4); the
>   code delta is not threaded into `SyncResultDto` (the id-level `CommitDelta` is
>   Phase 3); plain `import x` module edges via the goto fallback are wired but
>   untested (the parity fixture has none). The producer's incremental diff path
>   exists (Rust-unit-tested for no-over-fire) but Phase 2 only exercises the full
>   path through the oracle.

### 5.1 What was done

1. **Extend `Entity` with structural fields** — add `file`, full `range`, **name `range`**, `qualified_name` to `rust/src/entity.rs`
2. **New `rust/src/code_layer.rs`** — `CodeLayer` with `nodes: Map<DurableId, NodeData>`, `edges: Set<Edge>`, `reverse_deps: Map<DurableId, Set<DurableId>>`
3. **New `rust/src/dto/code_delta.rs`** — `CodeDeltaDto` with node DTO, edge DTO, `nodes_upserted`, `nodes_removed`, `nodes_moved`, `edges_added`, `edges_removed`
4. **The producer** (in `commit_head` or equivalent) — collect entities → materialise nodes → containment edges → reference/import edges → inheritance (two passes: all `INHERITS` then all `OVERRIDES`) → diff against current `CodeLayer` → emit minimal incremental `CodeDelta`
5. **Register the module** — add `mod code_layer` to `rust/src/lib.rs`; add `mod code_delta` to `rust/src/dto/mod.rs`
6. **Emit alongside the legacy build** — attach code delta to commit result; parity tests compare via oracle

### 5.2 Key risks & failure modes
- **Qualified-name format mismatch**: analysis engine uses dotted (`User.save`); identity registry uses `::`-joined with file prefix. Must match exactly.
- **Reference target resolution**: must match legacy resolver case-for-case.
- **Inheritance cursor position**: name range must position on the class/def *name*, not on `class`/`def` keyword or decorator.
- **Borrow-checker friction**: the producer captures head state and root; may require careful lifetime management.
- **Two-pass inheritance**: add *all* `inherits` edges first, *then* compute *all* `overrides`. A single combined pass is forbidden.

### 5.3 Acceptance
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml code_layer code_delta entity
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_final_parity_oracle.py -q --no-cov
```

### 5.4 Git commit series
1. `feat(entity): add structural fields (file, ranges, qualified_name) for code-layer producer`
2. `feat(code_layer): native CodeLayer with nodes, edges, reverse_deps`
3. `feat(dto): CodeDeltaDto wire contract`
4. `feat(commit): produce native code delta alongside legacy build; parity-check`

---

## 6. Phase 3 — Id-level commit delta ✅ **DONE**

> **Completed & verified 2026-06-07.** The public per-write value is now the
> id-level `CommitDelta` (Rust `CommitDeltaDto`, Python `models/delta.py`), with
> the Phase 2 `CodeDeltaDto` **nested**. All 6 `test_final_commit_delta_contract.py`
> tests pass; module-level `xfail` removed. Milestone gate green: **Rust 155
> passed / 0 failed**; **Python 639 passed, 19 xfailed, 3 failed** — the 3 being
> the documented pre-existing later-phase baseline (`test_final_derived_contract`
> ×2 → Phase 7; `test_final_hash_ast::test_formatting_only_hashes_same` → Phase 9).
> `test_concurrency` green; suite ~2:19 (no Phase 2 per-commit regression).
>
> **What landed**
> - `dto/commit_delta.rs` (new): `CommitDeltaDto` + `MovedEntityDto`. Nests
>   `CodeDeltaDto` (gained `Default`; shape unchanged). Carries id-level
>   `created_ids`/`changed_ids`/`deleted_ids`/structured `moved`/`authored_ids`/
>   `affected_ids`, the nested `code_delta`, and **path-shaped metadata**
>   (`touched_files` + per-category `created`/`changed`/`deleted`) consumed by the
>   still-path-shaped Phase 6/7 post-commit helpers.
> - `identity.rs`: `Binding` variants carry `old_hash` (captured **before** the
>   in-pass rebind). New `Reconciliation::classify(entities) -> ReconcileClasses`
>   gives the **precise, hash-based** changed set (no over-fire), structured
>   `MovedBinding` (id + old/new qualified path + old/new file via the single
>   tested `file_of_qualified_path` helper), created (minted), deleted (retired).
>   7 new unit tests (edit / whitespace / two-funcs / pure-move / delete / new /
>   split).
> - `code_layer.rs`: `CodeLayer::affected_closure(seeds)` — deterministic BFS over
>   `reverse_deps` (+ 2 unit tests, incl. the empty-layer = seeds-only case).
> - `project.rs`: `run_identity_reconciliation` surfaces the id-level classes +
>   `affected_ids`; all 6 commit bodies build `CommitDeltaDto` via a shared
>   `build_commit_delta`; `sync_path` on a project-config file (`is_project_config_file`)
>   signals **rescan** (coarse change, §5.4). `SyncResultDto` retired from the
>   write path (the Rust struct + Python `SyncResult` model are kept for the
>   legacy bus unit tests).
> - Python: `models/delta.py` (`CommitDelta` + `MovedEntity`, default factories);
>   every write method returns `CommitDelta`; `bus/delta.py` + `graph/graph.py`
>   post-commit helpers duck-type structured `moved` + `authored_ids` so the
>   path-shaped bus/graph/derived consumers keep working (rewrite is Phase 6–7).
>
> **Deliberately deferred (user-confirmed 2026-06-07 — "Option B"):** live native
> reverse-dep maintenance. `head.code_layer` stays **empty** in Phase 3 (the
> in-commit producer is full-build and too expensive — the Phase 2 regression),
> so `affected_ids` equals the seeds (`changed ∪ deleted`). The transitive
> closure lights up in **Phase 4** when the code layer becomes authoritative and
> is built+maintained. No capability lost: the bus still computes transitive
> affected via the Python `CodeGraph`. The `affected_closure` algorithm itself is
> landed and unit-tested. Rationale: doing it now duplicates the hardest deferred
> Phase 2 work against a still-legacy-authoritative graph, at high regression risk
> and with no contract-level validation (the contract tests don't assert
> `affected_ids`).

### 6.1 What needs to happen

1. **Replace `SyncResultDto`** with a new `CommitDelta` DTO: `revision`, `created_ids`, `changed_ids`, `deleted_ids`, `moved[{id, old_qualified_path, new_qualified_path, old_file, new_file}]`, `authored_ids`, `affected_ids`, `code_delta` (nested Phase 2 delta), `touched_files`, `rescan`, `project_changed`, `custom_stdlib_changed`
2. **Fix reconciliation to emit ids and structured moves**:
   - `Binding::Moved` needs both `old_path` and `new_path`/new location
   - `Reconciliation::changed()` must compare old vs new content hash (currently returns **all** Exact bindings, unchanged or not — violates §5.4 no-over-fire)
   - `changed` must mean "content hash changed", not "exact-path rebinding happened"
3. **Compute `affected_ids`** during commit as closure of `changed ∪ deleted` under `reverse_deps` (built in Phase 2)
4. **Mirror `CommitDelta` in Python** — pydantic model with default factories for all list/dict fields; keep thin deprecated shim for any external callers

### 6.2 Concrete changes needed

#### `rust/src/identity.rs`
- `Reconciliation::changed()` (line ~523): add content-hash comparison to suppress unchanged entities from `changed`
- `Binding::Moved`: add `new_path`/`new_location` field alongside existing `old_path`
- `IdentityDelta`: add structured `MovedEntry { id, old_path, new_path }` alongside path-level fields
- Add affected-set computation (closure under reverse_deps from Phase 2)

#### `rust/src/dto/sync.rs`
- Replace `SyncResultDto` with `CommitDeltaDto` (or add as new DTO, keeping old one as deprecated)
- `created_ids: Vec<String>` (DurableId hex), `changed_ids: Vec<String>`, `deleted_ids: Vec<String>`
- `moved: Vec<MovedEntityDto>`, `authored_ids: Vec<String>`, `affected_ids: Vec<String>`
- `code_delta: CodeDeltaDto`, `touched_files: Vec<String>`
- `rescan: bool`, `project_changed: bool`, `custom_stdlib_changed: bool`

#### `rust/src/project.rs`
- `commit_head` (line ~1435): build the new `CommitDeltaDto` instead of `SyncResultDto`
- `sync_path_inner` (line ~1478): same
- The watcher fold (line ~1548): same
- All write methods return `CommitDeltaDto`

#### Python side
- New model in `src/tyo3/models/` or similar: `CommitDelta` pydantic model
- Keep `SyncResult` as thin deprecated shim or remove if no external callers

### 6.3 Acceptance
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml identity
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_final_commit_delta_contract.py -q --no-cov
```

---

## 7. Phase 4 — Cutover: Python graph as pure applier ✅ **DONE**

> **Completed & verified 2026-06-07.** The native code delta is **authoritative**:
> `CodeGraph.apply_code_delta` is the pure applier (no FFI/session/snapshot), and
> the **only** mechanism that builds/updates a graph (head + snapshot), revision-
> gated. The six-pass read-surface build, `apply_delta`, the resolution/
> inheritance passes, the dead resolution indices, `_add_stub_node`, and
> `_prime_identity_registry`/`_graph_identity_primed` are **deleted** (~1140 lines
> from `graph/graph.py`). No read accessor advances head. Snapshot graph builds
> over its own frozen db. Milestone gate green: **Rust 156/0**; **Python 647
> passed, 16 xfailed, 3 failed** (the 3 = documented Phase 7×2 + Phase 9 baseline;
> zero NEW failures). `test_concurrency` green (1:48 — no rebuild-per-commit
> regression).
>
> **Commits:** `41b838a` (4.1 applier) · `13f2b08` (4.2a head/post-commit native +
> `Option<CodeDeltaDto>` three-state) · `9bba822` (4.4 snapshot frozen-db delta) ·
> `35d3e47` (4.2b+4.3 delete read-surface build + priming; producer bound).
>
> **What landed**
> - **Applier (4.1):** rescan delta clears+rebuilds wholesale; a stale/duplicate
>   incremental delta (`revision <= current`) is a no-op; edge removals
>   symmetrically prune `_file_importers`. Unit-tested in
>   `test_graph_apply_code_delta.py`.
> - **Authoritative (4.2a):** `CommitDeltaDto.code_delta` is `Option<CodeDeltaDto>`;
>   `build_commit_delta` emits `None` (producer deferred — see decision callout).
>   Three-state consumer in `_apply_graph_delta`: `None`→rebuild from
>   `full_code_delta()` (in place, so the head-graph instance is stable);
>   `Some({})`→no-op; `Some({…})`→apply (gap→rebuild). `session.graph` /
>   `_rebuild_head_graph_from_native()` build from the native delta — no priming,
>   no `sync_all`.
> - **Snapshot (4.4):** `PySnapshot::full_code_delta` produces over the snapshot's
>   **frozen** db + pinned registry; `Snapshot.graph()` applies it (no session ref).
> - **Delete + bound (4.2b/4.3):** deleted the read-surface build/updater/priming;
>   **bounded the native producer to under-root files** — `compute_files` returns
>   `project.files(db)`, unbounded over a warm live head (it includes every
>   stdlib/typeshed file the type-checker opened: a 1-file fixture exploded to
>   3965 nodes / 18363 ref edges); the frozen snapshot overlay already bounds it.
>   Diagnostics re-homed to a read-only `refresh_diagnostics(source)` (one
>   `source.check()`, never the applier). `CodeGraph.build` survives as a thin
>   native builder (guide oracle-note); the apply_delta/rebuild tests were migrated
>   to drive the native post-commit head path (`s.graph` vs a fresh `build`).
>
> **Deliberately deferred (user-confirmed 2026-06-07):** the in-commit incremental
> producer — see the design-decision callout above. `head.code_layer` /
> `reverse_deps` stay empty; `affected_ids` seeds-only until it lands.
> [[phase4-producer-deferred]]

> ### Design decision (user-confirmed 2026-06-07): defer the in-commit producer
>
> The one open fork the guide leaves to the requester — **scoped incremental
> in-commit producer vs. `None`→full-rebuild** — is resolved as **defer the
> producer** (same call shape as Phase 3's affected-closure deferral).
>
> - `CommitDeltaDto.code_delta` becomes a true **`Option<CodeDeltaDto>`**;
>   `build_commit_delta` emits **`None`** while the producer isn't running.
> - **Three-state consumer contract** (in `_apply_graph_delta`):
>   `None` (absent) → **rebuild** head graph from `full_code_delta()`;
>   `Some({})` (present, empty) → **no-op** (e.g. whitespace-only edit);
>   `Some({…})` (present, populated) → **apply** incrementally.
>   *Empty ≠ rebuild* (that would full-rebuild every cosmetic edit), and rebuild
>   is **never** signalled via `rescan=true` on the nested delta (the applier
>   reads `rescan` as "replace wholesale" → an empty rescan delta wipes the graph).
> - **Why defer:** the producer is *purely additive* (no consumer change to flip
>   `None`→`Some` later), and coupling it with the cutover compounds two
>   independent risks — the cutover's parity risk and the producer's perf
>   regression (full `produce_code_delta` per commit regressed `open()` ~100× /
>   deadlocked `test_concurrency` in Phase 2). The correct producer is
>   *scoped/incremental* (dirty files + importers, diff against `head.code_layer`)
>   — a real perf pass with its own `open()`-regression guard, filed as the
>   follow-up. Long-term destination is still the in-commit producer
>   (Concept §7: Rust owns the canonical code layer + reverse_deps); deferring
>   costs no rework.
> - **Consequence carried forward:** `head.code_layer` stays empty, `reverse_deps`
>   empty, `affected_ids` seeds-only — until the producer lands (Phase 4 perf
>   follow-up, or later).
>
> **Red baseline recorded (2026-06-07):** parity suite GREEN (native half matches
> legacy); `test_final_no_read_side_writes.py` = **2 xfail** to remove
> (`session.graph`, `latest.check`) — the `snapshot().graph()` case already went
> green via Phase 1.3. (The guide text says 3; the actual red count is 2.)

### 7.1 What needs to happen

1. **Add `apply_code_delta(code_delta)` to `CodeGraph`** — pure function of graph + delta; upsert nodes, add/remove edges, update indices; **no FFI calls, no read-surface reads, no session/snapshot touches**
2. **Make native delta authoritative** — switch post-commit graph update to call `apply_code_delta` (revision-gated)
3. **Delete legacy Python read-surface build** — remove from `graph/graph.py`: per-file symbol collection, node materialisation, occurrence-based reference resolution, inheritance/override passes, full rebuild, old delta-application internals
4. **Remove read-side writes** — delete `_prime_identity_registry` and the `_graph_identity_primed` flag; reading graph must never call `sync_all`
5. **Snapshot `graph()` from native state** — snapshot code delta computed over frozen database; no read-surface walk; must not mutate session state

### 7.2 Files in scope

| File | Change |
|------|--------|
| `src/tyo3/graph/graph.py` | Add `apply_code_delta()`; remove `build()` (the 6-pass read-surface build), `_index_files()`, `_resolve_references_via_occurrences()`, `_revalidate_inbound_inheritance()`, `_prime_identity_registry()`, old `apply_delta()` |
| `src/tyo3/session.py` | Remove `_graph_identity_primed`; switch `_apply_graph_delta` to native delta |
| `src/tyo3/tests/test_final_no_read_side_writes.py` | Remove all 3 `xfail` markers |
| `src/tyo3/tests/test_final_parity_oracle.py` | Remove `xfail` from `test_assert_parity_native_half_not_ready_yet` |
| Rust `project.rs` | Add snapshot code-delta accessor (full code delta from frozen db) |

### 7.3 Acceptance
```bash
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_final_no_read_side_writes.py \
  src/tyo3/tests/test_graph*.py -q --no-cov
# Plus the full parity suite
```

---

## 8. Phase 5 — Single native `commit()` with staging + rollback ✅ **DONE**

> **Completed & verified 2026-06-08.** Every write (`edit` / `edit_many` /
> `edit_virtual` / `sync_path` / `discard` / `sync_all` / `author` /
> `poll_changes`) funnels through one native `commit(mutation)` that STAGES all
> next-state, runs every fallible step against the stage, and PUBLISHES the
> revision LAST. The three rollback-contract cases are real green assertions
> (module xfail removed); the focused gate (`test_final_transaction_rollback` +
> `test_write_path` + `test_gate6_authored`) and `cargo test project authored
> sidecar` are green. The two Phase-1 carry-overs are closed (xfails removed:
> `test_watch` ×3 + `test_gate8_bus::test_inject_changes_fires_bus`).
>
> **Strategy chosen (user-confirmed 2026-06-08): "B+ deferred-publish."** True
> Strategy A (stage a separate next-db, swap on success) is *provably* blocked by
> salsa: `ProjectDatabase::apply_changes(&mut self)` calls `trigger_cancellation`
> and a `db.clone()` shares `Zalsa` storage, so a rollback-clone would deadlock
> the next mutation (and a fresh per-commit db is the documented ~100× perf trap).
> So the **store** (the observable `head` revision + snapshot content + retained
> window) is kept genuinely publish-last: `ContentStore::stage` builds the next
> generation without advancing the store, the commit publishes it to the *overlay*
> only (so the live db can analyse it for reconciliation), and the store
> `publish_staged` (revision bump + retained-record) is the single, last in-lock
> step. A failed commit never touches the store. The `registry`/`authored`/overlay
> are reconciled in place and restored from a captured `Baseline` on failure; the
> salsa db is left benignly ahead (it re-reads the restored overlay → same content
> → recomputes equal). Fault seam = an always-compiled-but-inert `armed_fault`
> field (no cargo feature), one-shot, taken at commit start.
>
> **What landed**
> - `lib.rs` / `exceptions.py`: typed `SidecarWriteError` / `CommitFailed` /
>   `ReconcileAmbiguous` (subclasses of `TyO3Error`, registered + re-exported;
>   `ReconcileAmbiguous` has no call site yet — registered with a note).
> - `content.rs`: `stage` / `stage_unchanged` / `publish_staged` / `next_revision`
>   (deferred-publish primitives) + `stage_reingest` (sync_all disk re-ingest,
>   preserving unsaved buffers) + shared `walk_relevant_files` / `read_disk_changes`.
> - `project.rs`: one `commit(head, Mutation)` funnel (`Mutation` enum +
>   `StagedCommit`/`AuthoredPlan`/`Baseline` + `build_plan`/`run_staged`/
>   `check_fault`/`persist_identity`/`rollback`). `run_identity_reconciliation`
>   no longer persists (was a swallowed `log::error!`) and takes `next_rev`;
>   persistence is a staged, propagated step. Identity persistence is also an
>   explicit propagated step at open. Authored write is now stage→persist→publish
>   with the in-memory-only `prior` rollback **deleted**. PyO3 write methods are
>   thin (parse → `Mutation` → `commit` → pythonize). `_fault_inject(stage)` seam.
>   New `HeadState` fields: `unsaved_overlays` (the watcher buffer-wins set, now
>   gated on genuinely-unsaved edits, not `has_overlay`) and `armed_fault`.
> - `session.py`: write methods re-raise native `TyO3Error` subclasses untouched
>   (no re-wrap into `InternalTyError`) so typed commit errors surface (§5.12).
>
> **Defect #2 is Phase 5 *partial*:** the in-lock half (stage → publish-last →
> rollback under the native `Mutex`) is closed; the bus / Python post-commit-path
> half completes in **Phase 6**.
>
> **Deliberately NOT done (out of scope, unchanged):** the in-commit code-layer
> producer stays deferred (the `code_layer` fault still fires at its real staged
> boundary, but it stages nothing — `code_delta` remains `None` → Phase-4
> head-graph rebuild); the `CommitDelta` shape and the bus are untouched.
> `graph/tests/test_incremental_parity.py::test_moved_entity_...` stays a
> *non-strict* xfail: Phase 5 fixed the watcher event-dropping half, but
> watcher-driven cross-file *move detection* is an identity-layer (§5.5 rule 2)
> matter outside the commit funnel and outside the gated testpaths.

### 8.1 What needs to happen

1. **Funnel all writes through one `commit(mutation)`** — `edit`, `edit_many`, `edit_virtual`, `sync_path`, `discard`, `sync_all`, `author`, `poll_changes` all call the same native entry point
2. **Stage-publish ordering** — inside lock: stage next generation → analysis change events → identity registry → authored state → code layer → compute delta → write sidecar (temp+rename+fsync) → apply analysis change → **publish revision last**. If any step fails: return typed error; leave all state at prior revision; no bus delta enqueued
3. **Fix authored-write ordering** — currently publishes before persisting. Re-order to stage → persist → publish
4. **Fix identity persistence error swallowing** — currently `log::error!("…")` and continues. Must be a typed error that rolls back the commit
5. **Typed errors** — `SidecarWriteError`, `CommitFailed`, `ReconcileAmbiguous` surfaced to Python
6. **[Phase 1 carry-over] `sync_all` re-ingests disk** — route `sync_all` through `ContentStore::ingest_project` so files created after open are discovered (today it republishes the existing generation + Rescan and misses them). Removes xfail on `test_graph_build::test_content_hash_updates_incrementally_by_semantic_body`.
7. **[Phase 1 carry-over] watcher buffer-vs-ingest** — `apply_watch_events` must gate its buffer-wins rule on genuinely *unsaved* overlay edits, not on any `has_overlay` hit (Phase 1 ingests every file at open, so the current guard drops all watcher events). Removes xfails on `test_watch::{test_deleted_event, test_injected_change_matches_expected_delta, test_real_watcher_observes_disk_change}` and `test_gate8_bus::test_inject_changes_fires_bus`.

### 8.2 Files in scope

| File | Change |
|------|--------|
| `rust/src/project.rs` | Rewrite `commit_head` → `commit(mutation)` with staging; wire all write kinds through it; add fault-injection seam for tests; restore prior state on failure; fix authored ordering |
| `rust/src/sidecar.rs` | Ensure atomic write-temp-then-rename with fsync for all sidecar persistence |
| `rust/src/authored.rs` | Remove separate publish-before-persist pattern |
| `rust/src/identity.rs` | Identity persistence becomes part of the staged commit, not inline inside `run_identity_reconciliation` |
| `src/tyo3/tests/test_final_transaction_rollback.py` | Remove the module-level `xfail` markers |
| Python error models | Map native `SidecarWriteError`, `CommitFailed`, `ReconcileAmbiguous` to Python exceptions |

### 8.3 Acceptance
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml project authored sidecar
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_final_transaction_rollback.py \
  src/tyo3/tests/test_write_path.py src/tyo3/tests/test_gate6_authored.py -q --no-cov
```

---

## 9. Phase 6 — One post-commit path; non-blocking bus ✅ **DONE**

> **Completed & verified 2026-06-08.** Every write
> (`edit`/`edit_many`/`edit_virtual`/`sync_path`/`discard`/`sync_all`/
> `poll_changes`/`author`) funnels through **one** `_after_commit(delta)` hook
> (invalidate head snap → apply graph delta → schedule derived → publish), so no
> path can diverge and every committed revision publishes — closing **defect #6**
> (`discard` published nothing) by construction and the **bus/post-commit-path
> half of defect #2**. The bus `Delta` is an **id-level** projection of the
> `CommitDelta`; the bus **asserts** revision order; and the writer-blocking
> overflow policy is **rejected at open** with a typed `ConfigError`. All 3
> `test_final_bus_contract.py` xfail markers removed; `test_gate8_bus` green over
> the restructured bus. Milestone gate: **Rust 158/0** (+2 overflow tests);
> **Python** — full suite at the documented baseline (only the 2 Phase-7 +
> 1 Phase-9 pre-existing failures; **zero new**; xfail countdown 10→7).
>
> **Commits:** `refactor(bus): id-level Delta…` (6.4) · `refactor(session):
> single _after_commit…` (6.1) · `fix(session/bus): every write publishes;
> assert revision order; deliver ALL revisions` (6.2) · `fix(bus/config): reject
> writer-blocking overflow…` (6.3).
>
> **What landed**
> - **6.4 — id-level Delta.** `Delta.from_commit_delta(delta, graph, root)` builds
>   the bus delta straight from the commit delta's id fields; the **lossy
>   path→id** reconstruction is deleted (`from_sync_result`, `_ids_in_files`,
>   `_resolve_layers_from_files`). `_layers_touched` is keyed off the id fields.
> - **6.1 — one hook.** `_after_commit` + `_schedule_derived`; derived
>   invalidation split out of `_apply_graph_delta`; `author` routes through the
>   hook with `_apply_graph_delta` a no-op on an **authored-only** delta
>   (`_is_authored_only`). Grep-proven: the only callers of
>   `_apply_graph_delta`/`_publish_delta`/`_invalidate_head_snap`/`_schedule_derived`
>   are inside `_after_commit` (plus the legit non-write callers `reload`/`close`).
> - **6.2 — publish every write + assert order.** Bus order check promoted from a
>   logged warning to a hard `assert delta.revision > last`. **ALL/rescan
>   delivery is unconditional** (even an empty scoped slice), so an `Interest.ALL`
>   subscriber gets exactly one delta per write; empty-drop kept only for
>   genuinely-empty *scoped* slices.
> - **6.3 — non-blocking bus.** `config.rs` `validate()` rejects any non-`{coalesce,
>   drop_and_mark_lagged, error_and_close}` overflow with
>   `ConfigError::BlockingOverflow`; the producer-side `_cond.wait()` `"block"`
>   branch is **deleted** (no producer-side wait survives — only consumer
>   `poll`/`__next__`). `error_and_close` closes outside the lock (close()
>   re-acquires it).
>
> ### Design decisions (user-consulted 2026-06-08)
> - **(a) Affected-set provenance → Option B (keep the transitive walk for
>   `affected` only).** The guide-literal Option A (delete the walk, use the
>   native seeds-only `affected_ids`) **broke** `test_gate8_bus::
>   test_scoped_reverse_dep_delivery` (asserts reverse-dep delivery: an `app.py`
>   subscriber notified when `models.py` changes) — the exact capability Phase 3
>   deferred the producer *on condition of keeping* ("no capability lost: bus
>   still computes transitive affected via the Python `CodeGraph`"). So the bus
>   delta's **ids are a pure native projection**, but `affected` is still expanded
>   over the materialised head graph — **id→id and id→file only**, never path→id.
>   `_compute_affected`/`_resolve_files` are now id-keyed and land for deletion
>   when the native in-commit producer ships a transitive `affected_ids` (zero bus
>   change then). gate8 stays fully green; no test weakened. [[phase4-producer-deferred]]
> - **(b) `author` → one hook** with `_apply_graph_delta` a no-op on the
>   authored-only delta (not a bespoke tail) — genuinely one post-commit path.
> - **(c) Overflow rename → clean rename + migrate gate8.** Reject both old names
>   (`block`/`error`); migrate `test_gate8_bus`'s `overflow="error"` →
>   `drop_and_mark_lagged` and its `from_sync_result`/`SyncResult` cases →
>   `from_commit_delta`/`CommitDelta` (behaviour preserved). No back-compat shim
>   — the guide's "delete `from_sync_result`" pitfall.

### 9.1 What needs to happen

1. **Centralise post-commit hook** — add `_after_commit(delta: CommitDelta)` to session: invalidate head snap → apply graph delta (pure applier from Phase 4) → schedule derived (id-level, Phase 7) → publish to bus
2. **Collapse per-method sequences** — every write method becomes: call native `commit` → validate delta → call `_after_commit(delta)` → return. Remove all hand-copied per-method post-commit code
3. **Fix `discard`** — it must publish (currently returns without `_publish_delta`)
4. **Fix `author`** — ensure bus delta is published
5. **Remove writer-blocking overflow policy** — config validation rejects it; supported non-blocking policies: `coalesce`, `drop_and_mark_lagged`, `error_and_close`
6. **Delivery is scoped, ordered, id-level** — bus delta is thin immutable wrapper over `CommitDelta`; interest matching on `affected_ids` / `touched_files` / layers; `rescan` matches all

### 9.2 Files in scope

| File | Change |
|------|--------|
| `src/tyo3/session.py` | Add `_after_commit()`; rewrite every write method to use it; remove `_publish_delta` duplication; remove `_invalidate_head_snap` duplication; remove `_apply_graph_delta` old path |
| `src/tyo3/bus/` | Update delta wrapper to use `CommitDelta`; fix interest matching; ensure non-blocking overflow |
| `rust/src/config.rs` | Validate overflow policy: reject writer-blocking policies |
| `src/tyo3/tests/test_final_bus_contract.py` | Remove `xfail` markers |

### 9.3 Acceptance
```bash
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_final_bus_contract.py \
  src/tyo3/tests/test_gate8_bus.py -q --no-cov
```

---

> ⚠️ **Sections 10–16 below are the V1 remaining-phase notes, superseded by the
> V2 guides** (`PHASE_6_…_PHASE_14_IMPLEMENTATION_GUIDE.md`). They remain as
> reference for the *content* of each task, but the **numbering, sequencing, and
> approach are V2** (see §0). Notably: derived layers = **V2 Phase 8** (now with
> per-layer key locality); hashing = **V2 Phase 10** (with the explicit
> container-subsumes-members invariant); read surface = **V2 Phase 11**; config =
> **V2 Phase 12** (also surfaces precision knobs); split = **V2 Phase 13**. The bus
> rework and the new producer/async-precision phases (V2 6/7/9) are described only
> in the V2 guides, not below.

## 10. Phase 7 — Repair derived layers 🔴 **Not started** — *superseded by V2 Phase 8*

### 10.1 What needs to happen

1. **Feed invalidation id-level inputs** — build dirty set from `commit_delta.created_ids + changed_ids`; deleted from `deleted_ids` (not path-shaped values)
2. **Read-time staleness** — resolve content hash at snapshot → form store key `(input_hash, generator_version)` → if artifact exists it's fresh; if missing but last-good exists it's stale/failed; if nothing exists it's absent
3. **Fix recompute snapshot lifetime** — one pinned snapshot for invalidation + eager recompute; close in `finally`; remove path that opens second snapshot and never closes it
4. **Typed store errors** — filesystem store: "missing" only for genuinely absent file, propagate other IO errors; vector store: stop swallowing query/write errors; missing optional backend: distinct typed "backend unavailable"
5. **Complete self-healing test** — un-skip derived-layer test: unrelated edit → no recompute; content change → recompute; move → reuse artifact; generator-version bump → new key-space; failure → keep last-good artifact

### 10.2 Files in scope

| File | Change |
|------|--------|
| `src/tyo3/derive/` | Invalidation uses id-level inputs; fix snapshot lifetime; read-time staleness |
| `src/tyo3/stores/` | Typed errors, stop swallowing |
| `src/tyo3/tests/test_final_derived_contract.py` | Remove all 5 `xfail` markers |
| `src/tyo3/tests/test_gate5_derived.py` | Un-skip self-healing test |

### 10.3 Acceptance
```bash
devenv shell -- pytest src/tyo3/tests/test_final_derived_contract.py \
  src/tyo3/tests/test_gate5_derived.py -q --no-cov
```

---

## 11. Phase 8 — Read surface + convenience APIs 🔴 **Not started**

### 11.1 What needs to happen

1. **Remove closed-snapshot views** — `session.code`, `session.layer`, `session.entity` currently open a snapshot, take lazy view, close snapshot, return view (access fails later). Fix: reads go through explicit pinned snapshot, or returned view owns its snapshot and is a context manager
2. **Keep floating "latest" view honest** — warm single-layer reads only; no entity accessor, no cross-layer diff, no pinned revision, no mutable graph reference
3. **Stop swallowing read-surface errors** — distinguish typed absence from typed backend failure, graph-build failure, format/config failure

### 11.2 Files in scope

| File | Change |
|------|--------|
| `src/tyo3/session.py` | Fix convenience reads (`code`, `layer`, `entity`); fix latest view |
| `src/tyo3/tests/test_gate7_read_surface.py` | Ensure passes |

### 11.3 Acceptance
```bash
devenv shell -- pytest src/tyo3/tests/test_gate7_read_surface.py \
  src/tyo3/tests/test_final_no_read_side_writes.py -q --no-cov
```

---

## 12. Phase 9 — AST-canonical hashing 🔴 **Not started**

### 12.1 What needs to happen

1. **AST renderer** — replace line-heuristic `normalise_entity_source` with ruff-parser-based walk that emits canonical tokens: node kind, identifiers, literals (with literal *content* preserved exactly), signatures, annotations, decorators, bases, control flow, assignments
2. **Exclude comments always**; include/exclude docstrings per `HashPolicy`
3. **Whitespace outside literals → ignored**; trailing commas → ignored; annotations, decorators, default values, import aliases → significant
4. **String literal whitespace is significant** — `"a  b"` must hash differently from `"a b"` (current bug)

### 12.2 Files in scope

| File | Change |
|------|--------|
| `rust/src/hash.rs` | Replace `normalise_entity_source` with AST-based canonical renderer |
| `src/tyo3/tests/test_final_hash_ast.py` | Remove `xfail` markers from `test_string_literal_whitespace_hashes_differently` and `test_docstring_policy_and_non_docstring_strings` |

### 12.3 Acceptance
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml hash entity
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_final_hash_ast.py -q --no-cov
```

---

## 13. Phase 10 — Single config source 🔴 **Not started**

### 13.1 What needs to happen

1. Rust already loads and validates config. Expose the validated config (including coordination settings: bus capacity, overflow policy, watcher enabled/debounce) as JSON from native side
2. Python consumes validated config from native side; stop re-reading `config.toml` directly
3. Delete Python routine that parses config TOML and falls back to defaults on exception
4. Invalid coordination config fails loudly at open instead of silently reverting to defaults

### 13.2 Files in scope

| File | Change |
|------|--------|
| `rust/src/config.rs` | Expose validated config (including bus/watcher settings) as JSON to Python |
| `src/tyo3/config.py` | Stop re-reading config.toml; consume native config JSON; remove silent fallback |
| `src/tyo3/session.py` | Open path uses native config for coordination settings |

### 13.3 Acceptance
```bash
devenv shell -- build
devenv shell -- pytest -k "config or sidecar" -q --no-cov
```

---

## 14. Phase 11 — Split monolith files 🔴 **Not started**

### 14.1 What needs to happen

**Rust `project.rs` (4185 lines)** → split into focused modules:
- `open.rs` (project opening, config)
- `head_state.rs` (per-revision head state)
- `commit.rs` (the write transaction)
- `snapshot.rs` (snapshot construction, frozen views)
- `watch.rs` (the watcher, poll_changes)
- `authored.rs` (authored writes)
- `pyo3_methods.rs` (thin PyO3 method wrappers)

**Python `session.py` (1723 lines)** → split:
- `/src/tyo3/session.py` → thin public `TyO3Session` facade
- `/src/tyo3/postcommit.py` → `_after_commit` hook
- `/src/tyo3/views.py` → snapshot/latest views
- `/src/tyo3/exceptions.py` → public exceptions
- Protocols/models move out of implementation modules

**Python `graph/graph.py` (2143 lines)** → split:
- `/src/tyo3/graph/applier.py` → `apply_code_delta`
- `/src/tyo3/graph/queries.py` → query methods
- `/src/tyo3/graph/diff.py` → graph diff
- `/src/tyo3/graph/diagnostics.py` → diagnostics
- `/src/tyo3/graph/models.py` → node/edge models
- Rename to `projection/` or `snapshot_graph/` to reflect new role

### 14.2 Acceptance
```bash
# Full suite after each split
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml
# Public imports still work
```

---

## 15. Phase 12 — Zero warnings, typing, exception hygiene 🔴 **Not started**

### 15.1 Requirements

1. `cargo clippy --all-targets -- -D warnings` — clean
2. `ruff check src` — clean
3. `ruff format --check src` — clean
4. Remove unused imports, dead variants, confusing lifetime syntax
5. Prefix intentionally-unused test bindings with `_`
6. Reduce untyped values in session, native type stubs, stores/generators
7. Replace mutable default arguments and fields with factories
8. Replace every broad `except Exception: pass` with typed catch that yields typed absence, logs with context, or re-raises domain error

---

## 16. Phase 13 — End-to-end acceptance suite 🔴 **Not started**

Create `src/tyo3/tests/test_final_acceptance.py` exercising the full lifecycle:

1. Open project → pin initial snapshot
2. Author intent on an entity
3. Read code graph from pinned snapshot
4. Configure deterministic derived layer
5. Edit one entity
6. Move another entity unchanged
7. Delete one entity
8. Read old and new snapshots → diff them
9. Verify bus notifications
10. Close and reopen → verify identity + authored records survive
11. Delete derived cache → verify recompute
12. Assert no source files were modified
13. Assert: same revision → same content; durable ids survive cosmetic edit + move; content hash changes only on meaningful edit; derived artifacts keyed by content hash; authored records present/needs-review/orphaned; bus deltas ordered and id-level; snapshot diff agrees with independent rebuild; no read accessor advanced head

---

## 17. Currently open xfail markers (countdown to green)

| File | `xfail` count | Will turn green in |
|------|---------------|-------------------|
| `test_final_content_spine.py` | 0 (✅ Phase 1 done) | — |
| `test_watch.py` (Phase 1 carry-over) | 0 (✅ Phase 5 done; markers removed) | — |
| `test_gate8_bus.py::test_inject_changes_fires_bus` (carry-over) | 0 (✅ Phase 5 done) | — |
| `test_graph_build.py::...incrementally...` (carry-over) | 0 (✅ Phase 4 migrated it to the native head path; marker removed) | — |
| `test_final_no_read_side_writes.py` | 0 (✅ Phase 4 done; all 3 reads side-effect-free) | — |
| `test_final_commit_delta_contract.py` | 0 (✅ Phase 3 done) | — |
| `test_final_transaction_rollback.py` | 0 (✅ Phase 5 done; module xfail removed) | — |
| `test_final_bus_contract.py` | 0 (✅ Phase 6 done; all 3 markers removed) | — |
| `test_final_derived_contract.py` | 5 | Phase 7 |
| `test_final_hash_ast.py` | 2 | Phase 9 |
| `test_final_parity_oracle.py` | 0 (✅ Phase 2 done) | — |
| **Total** | **7** (Phase 6 closed the 3 bus-contract xfails) | |

> Note: `graph/tests/test_incremental_parity.py::test_moved_entity_...` keeps its
> **non-strict** xfail. Phase 5 fixed the watcher event-dropping half (the
> `test_watch` inject tests now pass), but watcher-driven *cross-file* move
> detection is an identity-layer (§5.5 rule 2) matter outside the commit funnel.
> It is **not** in the milestone `testpaths` (`src/tyo3/tests`), so it is outside
> the gated count.

---

## 18. Suggested commit series (from the plan)

| # | Commit Message | Phase |
|---|----------------|-------|
| 1 | `test: add final invariant tests + parity oracle harness` | **0 ✅** |
| 2 | `refactor(content): complete generations; remove snapshot disk pre-population` | **1 ✅** |
| 3 | `feat(rust): native code layer + code delta behind the parity oracle` | **2 ✅** |
| 4 | `refactor(delta): id-level commit delta with structured moves + affected closure` | **3 ✅** |
| 5 | `refactor(graph): cut over to a pure applier; remove read-surface build + priming` | **4 ✅** |
| 6 | `refactor(commit): single native commit() with staging + rollback` | **5 ✅** |
| 7 | `fix(session/bus): one post-commit path; publish every write; non-blocking overflow` | **6 🔴** |
| 8 | `fix(derived): id-level invalidation; read-time staleness; close snapshots; typed stores` | **7 🔴** |
| 9 | `fix(read): eager / owned-lifetime convenience views; stop swallowing read errors` | **8 🔴** |
| 10 | `refactor(hash): AST-canonical hashing` | **9 🔴** |
| 11 | `refactor(config): single validated config source` | **10 🔴** |
| 12 | `refactor: split project / session / graph monoliths` | **11 🔴** |
| 13 | `chore: zero warnings, tighten typing, exception hygiene` | **12 🔴** |
| 14 | `test: final end-to-end acceptance scenario` | **13 🔴** |

---

## 19. Definition of done (from the plan)

- [ ] `devenv shell -- pytest -q` passes with no unexpected skips
- [ ] `devenv shell -- cargo test --manifest-path rust/Cargo.toml` passes
- [ ] `clippy -D warnings`, `ruff check`, and `ruff format --check` are all clean
- [ ] No snapshot construction reads live project content from disk
- [ ] No read accessor advances the revision
- [ ] Every write is one native commit returning one id-level commit delta, followed by one shared post-commit path
- [ ] A failed write fully rolls back; the sidecar is a commit participant; there is no torn publish
- [ ] Bus delivery is ordered, scoped, and non-blocking, and every write path publishes
- [ ] Derived invalidation is id-level and leaks no snapshots
- [ ] Convenience reads return eager values or own their snapshot lifetime
- [ ] Hashing is AST-canonical and treats string-literal content as significant
- [ ] The end-to-end acceptance test proves the whole story

---

## 20. Environment notes

- The dev environment is **devenv.sh (Nix-managed)**
- Every in-repo operation runs through `devenv shell --`:
  ```bash
  devenv shell -- cargo test --manifest-path rust/Cargo.toml <test-filter>
  devenv shell -- build  # maturin rebuild — required after Rust changes before pytest
  devenv shell -- pytest <test-path> -q --no-cov
  ```
- Full test suites take ~10–15 minutes. Run in background.
- `devenv shell -- build` is **required** after every Rust change before the Python suite observes it.
- Project scripts defined in `devenv.nix`: `build`, `tests`, `test-rust`, `test-quick`, `clean`, `status`

---

*Last updated: 2026-06-08 — **PLAN RE-DIRECTED TO V2** (see §0). The producer was deferred three times; V2 builds it first (the new Phase 6), then deletes the bridges/scaffolds the deferral created. Two findings drove it: computing `affected` is a cheap reverse-dep graph walk (not blast-radius re-typecheck), and ty emits no inference-flow edges → synchronous `affected` is container-granular/never-miss with method precision as an optional async layer. New phase map: 6 scoped producer (NEXT) · 7 pure-projection bus + delete read-surface scaffolding (reworks the interim `aceabf6`) · 8 derived (per-layer key locality) · 9 async precision (new) · 10 hashing · 11 read surface · 12 config · 13 split · 14 hygiene+acceptance. Plan/guides in `15-implementation-plan/` (`START_HERE_V2.md`, `*_V2.md`, `PHASE_6..14`, `PHASE_6_KICKOFF.md`). Regression guard landed: `graph/tests/test_inference_flow_coverage.py` (3 green). Everything below §0 is the V1 historical record.*

*Earlier: Phases 0–6 complete. **Phase 6 (one Python post-commit path; non-blocking bus) complete and verified** — note this is V1-Phase-6 (bus), which V2 renumbers to Phase 7 and reworks (delete the interim Option-B bridge): every write funnels through one `_after_commit` hook (so `discard` and `author` now publish — defects #2 bus-half and #6 closed), the bus `Delta` is an id-level projection of the `CommitDelta` with asserted revision order, and the writer-blocking overflow policy is rejected at open with a typed `ConfigError` (producer-side `_cond.wait()` deleted). Design calls: (a) kept the transitive `affected` walk over the Python `CodeGraph` (id→id only) rather than the guide-literal seeds-only deletion, to preserve reverse-dep delivery per the Phase 3 "no capability lost" decision — the id→id/id→file helpers land for deletion with the native producer; (b) `author` through the one hook (no-op graph apply on authored-only deltas); (c) clean overflow rename, gate8 cases migrated off the removed `from_sync_result`/`overflow="error"` API. Milestone gate: Rust 158/0; Python full suite at the documented baseline (3 pre-existing Phase-7×2 + Phase-9 failures, zero new); xfail countdown 10→7. Phases 7–13 ahead.*

*Earlier: Phase 5 (single native `commit()` with staging + rollback) complete and verified —* every write funnels through one native `commit(mutation)` that stages all next-state and publishes the revision LAST, using the user-confirmed "Strategy B+ deferred-publish" (the store is never touched before the publish tail; registry/authored/overlay restore-on-failure; the salsa db is left benignly ahead because it cannot be rolled back). Typed `SidecarWriteError`/`CommitFailed`/`ReconcileAmbiguous` surfaced; identity persistence no longer swallowed; authored write is stage→persist→publish (in-memory `prior` rollback deleted); test-only one-shot fault seam (`_fault_inject`, always-compiled-inert). The 3 rollback-contract cases are green (module xfail removed) and the 2 Phase-1 carry-overs are closed (`test_watch` ×3 + `test_gate8_bus::test_inject_changes_fires_bus`). Milestone gate: Rust 156/0; Python — focused gate green (rollback ×3 + write_path + gate6_authored + watch + gate8_bus), full-suite baseline unchanged (3 pre-existing Phase 7×2 + Phase 9 failures; xfail countdown 17→10). Defect #2 now Phase-5-partial (bus/post-commit half → Phase 6). Phases 6–13 ahead.*
