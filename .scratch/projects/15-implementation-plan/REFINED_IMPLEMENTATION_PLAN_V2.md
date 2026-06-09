# TyO3 — Implementation Plan V2 (remaining phases 6–14)

> Companion to `REFINED_IMPLEMENTATION_CONCEPT_V2.md`. **Supersedes
> `REFINED_IMPLEMENTATION_PLAN.md` from Phase 6 onward.** Phases 0–5 are landed;
> see V1 for their record. This plan re-sequences the remaining work around the
> producer-first decision and the container-granular / async-precision affected
> model (Concept V2 §5).
>
> Every phase has a detailed `PHASE_<n>_IMPLEMENTATION_GUIDE.md`. The milestone
> gate after each phase is the two-command suite, via devenv:
> ```
> devenv shell -- pytest -q --no-cov
> devenv shell -- cargo test --manifest-path rust/Cargo.toml
> ```

---

## Current state (entering V2)

Landed and authoritative (V1):
- **Phase 1** content gate — complete generations, O(1) capture, no snapshot disk reads.
- **Phase 2** native code layer + code delta **behind a parity oracle** (full producer, legacy build still authoritative).
- **Phase 3** id-level `CommitDelta` (precise `changed_ids`, structured `moved`, nested `code_delta`, `affected_closure` helper).
- **Phase 4** cutover — `CodeGraph` is a pure applier; three-state `Option<CodeDeltaDto>`.
- **Phase 5** one native `commit(mutation)` — Strategy B+ deferred-publish, staging + rollback.

Partial / to be reworked:
- **Bus (old Phase 6)** — one commit (`aceabf6`) landed an *interim* id-level
  `Delta.from_commit_delta` with an Option-B transitive bridge (id-keyed
  `_compute_affected`/`_resolve_files` over the materialised head graph). **V2
  Phase 7 deletes this bridge** once the producer (Phase 6) makes `affected`
  transitive at the source.

Deferred keystone (the reason for V2):
- **The scoped in-commit producer** — `head.code_layer` is empty, `reverse_deps`
  empty, `affected_ids` seeds-only. **V2 Phase 6 builds it.**

---

## Phase 6 — The scoped native producer (keystone)

**Goal:** `head.code_layer` (`nodes + edges + reverse_deps`) is maintained
incrementally inside the commit, so `affected_ids` is a genuinely transitive,
container-granular, never-miss closure — computed natively, not by a Python walk.

- **6.1** Run the producer inside `commit` over the **dirty scope** (changed
  files ∪ one-hop importers), updating `head.code_layer`.
- **6.2** Emit the minimal incremental `code_delta` via `CodeLayer::diff_from`
  (full delta only for genuine `rescan` / cold start / `sync_all`).
- **6.3** Compute `affected_ids = affected_closure(changed ∪ deleted)` over the
  maintained `reverse_deps`, seeding deletions from the prior layer.
- **6.4** Retire the "every commit emits a full rescan" behavior; keep a tested
  full-rebuild path for `rescan=true`.

**Acceptance:** the parity oracle stays green (incremental == rebuild); a
reverse-dep closure test shows a change to a base class reports its subclasses
and their importers in `affected_ids`; `open()` shows no regression vs the
pre-producer baseline. Container-granular coverage proven by
`test_inference_flow_coverage.py`.

---

## Phase 7 — Pure-projection bus; delete the read-surface scaffolding

**Goal:** with `affected` transitive at the source, the bus delta is a *pure
projection*; the legacy graph builder and the Option-B bridge are deleted; the
refinement-channel seam is added.

- **7.1** Delete `Delta`'s id-keyed transitive helpers (`_compute_affected`,
  `_resolve_files`) and the materialised-head-graph dependency;
  `from_commit_delta` reads `affected` straight from the delta.
- **7.2** Delete the legacy read-surface builder in `graph/graph.py`; `CodeGraph`
  is solely a `code_delta` applier + algorithms.
- **7.3** Demote the parity oracle to a focused native-correctness suite; keep the
  inference-flow regression tests.
- **7.4** Finish the one post-commit path (`_after_commit`), publish from every
  write, non-blocking overflow policies, revision-order **assertion** (this is the
  clean completion of the old Phase 6; reconcile with `aceabf6`).
- **7.5** Add the **refinement-channel** contract to the bus: a revision-stamped
  affected-refinement message may arrive after a revision's primary delta.
  Contract + delivery only; nothing emits refinements yet.

**Acceptance:** `test_final_bus_contract.py` fully green (no xfail); the bus
imports no graph; `grep` shows no `from_sync_result`, no `_compute_affected`, no
read-surface builder; the refinement channel has a contract test (a manually
injected refinement for R is delivered after R's delta, ordered).

---

## Phase 8 — Unified derived invalidation

**Goal:** one affected-driven invalidation loop, per-layer key locality, honest
read-time staleness, no snapshot leaks, typed stores (Concept V2 §5.5; V1 §5.7/§5.8).

- **8.1** Declare per-layer **key locality**: `local` ⇒ key `content_hash`;
  `semantic` ⇒ key `content_hash + dependency-closure fingerprint`.
- **8.2** One loop: for each id in `affected`, recompute the layer key; changed ⇒
  stale ⇒ schedule. Local layers no-op on `affected`-without-`changed`.
- **8.3** Read-time staleness (resolve key at the snapshot; presence == fresh) —
  remove transaction-time mutable derived flags.
- **8.4** One pinned snapshot for invalidation + recompute, closed in `finally`.
- **8.5** Typed store errors (absent vs backend-unavailable vs backend-broken).
- **8.6** Un-skip the self-healing derived test (unrelated edit no-recompute;
  content change recomputes; move reuses; version bump new key-space; failure
  keeps last-good).

**Acceptance:** `test_final_derived_contract.py`, `test_gate5_derived.py` green;
a semantic-layer test proves recompute on a transitive (dependency) change with
no own-text change; a local-layer test proves no recompute on the same.

---

## Phase 9 — Async precision refinement layer

**Goal:** optional method-level precision, computed asynchronously in Python over
frozen snapshots, delivered on the Phase-7 refinement channel, with graceful
degradation (Concept V2 §5.4).

- **9.1** `code_graph.precision ∈ {container, method}` (default `container`); a
  `refinement = {sync, async}` sub-knob (default `async`).
- **9.2** A background worker: for committed R, open a frozen snapshot at R,
  resolve member-access targets (`find_references`/occurrence/`goto_type_definition`
  over the snapshot), compute the **narrowed** affected, publish a refinement.
- **9.3** Narrowing only (sound superset ⇒ no miss). Worker failure/lag leaves the
  coarse set intact (graceful degradation) — proven by a test that kills the
  worker and asserts coarse correctness.
- **9.4** Optional **expansion** mode (off by default) that adds non-nominal
  dependents, documented as eventually-consistent on that miss class.

**Acceptance:** with `precision=container` the worker never runs and all suites
pass; with `precision=method` a test shows the refinement narrows
`Widget.draw`'s affected from "all Widget referrers" to "draw users only" at the
same revision; a worker-crash test shows coarse correctness preserved.

---

## Phase 10 — AST-canonical hashing

**Goal:** the content hash reflects meaning, not formatting (V1 §5.6), and the
container-subsumes-members invariant is explicit and tested.

- **10.1** Replace the line-heuristic normaliser with an AST renderer (ruff
  parser): canonical tokens for kind/identifiers/literals (literal *content*
  exact)/signatures/annotations/decorators/bases/control-flow/assignments;
  comments always excluded; docstrings per policy.
- **10.2** Per-layer docstring/comment policy.
- **10.3** ≥128-bit width, machine-stable, hex-encoded.
- **10.4** Promote the **container-subsumes-members** behavior to a documented
  invariant; move `test_inference_flow_coverage.py`'s hash-subsumption test into
  the permanent suite and reference it from the hash module docs.

**Acceptance:** `test_final_hash_ast.py` green; `cargo test hash entity` green;
`"a  b"` → `"a b"` changes the hash; a member-body edit still moves the container
hash (the coverage invariant).

---

## Phase 11 — Read surface and convenience APIs

**Goal:** no read returns a view over a closed snapshot; read paths don't hide
failures (V1 §5.2/§5.12).

- **11.1** Remove closed-snapshot views; reads go through an explicit pinned
  snapshot, or the view owns and closes its snapshot.
- **11.2** Keep the floating "latest" view honest (warm single-layer reads only).
- **11.3** Stop swallowing read-surface errors; typed absence vs backend failure.

**Acceptance:** `test_gate7_read_surface.py`, `test_final_no_read_side_writes.py`
green; no convenience API returns closed lazy state.

---

## Phase 12 — Single config source

**Goal:** one validated config surfaced from Rust; no silent Python fallback
(V1 §5.12).

- **12.1** Python consumes the native validated config as JSON; delete the Python
  TOML re-read + default fallback.
- **12.2** Surface coordination (bus capacity/overflow, watcher) **and the new
  precision knobs** (`code_graph.precision`, `refinement`) in the native config.
- **12.3** Invalid config fails loudly at open.

**Acceptance:** invalid coordination/precision config raises `ConfigError` at
open; config + sidecar tests green.

---

## Phase 13 — Split the monoliths

**Goal:** structure becomes reviewable; each split is behaviour-preserving.

- **13.1** `rust/src/project.rs` → open / head-state / commit / snapshot / watch /
  authored / PyO3 wrappers. Identity and code layer stay separate.
- **13.2** `src/tyo3/session.py` → facade / views / post-commit hook / read
  wrappers / exceptions.
- **13.3** `src/tyo3/graph/graph.py` → applier / queries / diff / diagnostics /
  models; rename to reflect "projection," not "transaction participant."

**Acceptance:** full suite green after each split; public imports unchanged.

---

## Phase 14 — Zero warnings, typing, hygiene + end-to-end acceptance ✅ **DONE** (2026-06-08)

**Goal:** the codebase is clean and the whole story is proven end to end.

> **LANDED — the spine refactor (V2) is complete.** All five gate commands clean
> (`ruff check`, `ruff format --check`, `cargo clippy --all-targets -- -D
> warnings`, `cargo test`, `pytest -q`). Lints to zero via real per-finding
> decisions (delete dead code / `cfg(test)`-gate test-only helpers / narrow
> `#[allow]` with reasons); F821/F841 resolved (several were genuine missing
> assertions, now added); `test_final_acceptance.py` proves the whole story and
> is wired into the `test-final` gate. See PROGRESS §9.14.

- **14.1** Rust warnings to zero; `clippy --all-targets -- -D warnings` in the gate.
- **14.2** Reduce untyped values; typed models for native payloads; factories for
  mutable defaults.
- **14.3** Replace broad `except Exception: pass` with typed handling.
- **14.4** `test_final_acceptance.py`: open → pin snapshot → author intent → read
  graph from a pinned snapshot → configure a deterministic derived layer → edit /
  move / delete entities → diff old vs new snapshots → verify bus deltas (and, with
  `precision=method`, a refinement) → close/reopen → identity + authored survive →
  delete derived cache and verify recompute → assert no source files changed.

**Acceptance:** `pytest -q` (no unexpected skips); `cargo test`; `clippy -D
warnings`, `ruff check`, `ruff format --check` all clean; acceptance test green.

---

## Suggested commit series (V2, remaining)

1. `feat(commit): scoped in-commit code-layer producer; transitive affected` (6)
2. `refactor(bus): pure-projection delta; delete read-surface builder + bridge; refinement channel` (7)
3. `fix(derived): unified affected-driven invalidation with per-layer key locality` (8)
4. `feat(precision): async container→method affected refinement over snapshots` (9)
5. `refactor(hash): AST-canonical hashing; explicit container-subsumes-members invariant` (10)
6. `fix(read): owned-lifetime convenience views; stop swallowing read errors` (11)
7. `refactor(config): single validated config source; surface precision knobs` (12)
8. `refactor: split project / session / graph monoliths` (13)
9. `chore: zero warnings, typing, hygiene; end-to-end acceptance` (14)
