# TyO3 Final Review

This review consolidates the refined concept/spec/gate documents in
`.scratch/projects/13-refined-concept/` with a deep pass over the current Rust,
Python, and test suite implementation.

The goal is not merely to make the suite green. The goal is a personal-use-ready
library whose architecture is simple, deterministic, recoverable, and honest
about what is canonical.

## Review Scope

Inputs reviewed:

- `REFINED_CONCEPT.md`, `REFINED_SPEC.md`, `REFINED_ARCHITECTURE.md`
- `GATE_1_*` through `GATE_8_*` guides
- `GATES_1-3_REFACTORING_GUIDE.md`
- Rust native layer in `rust/src/`
- Python session/read surface in `src/tyo3/session.py`
- Python graph, layers, derived runtime, bus, stores, sidecar mirror, models
- Gate tests, graph tests, MVCC/concurrency tests, property tests, Rust unit tests

Verification performed during review:

- `devenv shell -- pytest src/tyo3/tests/test_gate*.py -q`
  - Passed, exit code 0. One expected xfail remains in Gate 5.
- `devenv shell -- pytest -q`
  - Passed, exit code 0.
- `devenv shell -- pytest -q --no-cov`
  - Passed, exit code 0.
- `devenv shell -- cargo test --manifest-path rust/Cargo.toml`
  - Passed: 124 Rust tests, 0 failed.
  - Emitted 16 Rust warnings.

## Executive Verdict

TyO3 has several strong foundations:

- The Rust `ContentStore` has immutable generations and retained revisions.
- The frozen overlay itself correctly avoids direct disk fallback.
- Snapshots own independent `ProjectDatabase` storage, which is the right answer
  for salsa cancellation and writer isolation.
- Identity reconciliation is deterministic and has meaningful tests.
- Symbol DTOs now carry `durable_id` and per-profile `content_hashes`.
- Code graph construction is phased and has the right high-level shape.
- Config validation is much more serious than a typical prototype.
- Authored records are structurally shared and versioned.
- The subscription bus has the right conceptual shape.

However, the implementation is not final-ready yet. The largest problems are not
small bugs. They are contract mismatches between the refined architecture and the
current ownership boundaries:

- Committed revisions are not fully self-contained because snapshot construction
  can still populate missing files from live disk.
- The single write transaction ends in Rust, then Python mutates the code graph,
  derived runtime, and bus afterward.
- The public write delta is path-shaped, while the refined architecture requires
  DurableId-shaped deltas.
- Read operations can mutate state through the `CodeGraph.build -> sync_all`
  priming path.
- Some persistence failures are logged or partially rolled back after a revision
  has already advanced.

The codebase can get to a clean final state, but the refactor should be done from
the spine outward: first content/revision truth, then delta identity, then layer
ownership, then bus and convenience APIs.

## Target Architecture

The refined design becomes simple if the following rules are made non-negotiable.

1. A committed revision is a complete, self-contained truth.
   - Snapshot reads at R never consult live disk for project content or directory
     membership.
   - A snapshot may use disk only for exempt structural operations that do not
     return project content or membership.

2. A write publishes exactly one revision after all in-lock state is coherent.
   - Content, analysis state, identity, code-layer state, authored metadata, and
     any persistent transaction side effects succeed together or fail together.

3. Deltas are DurableId-level.
   - Paths are metadata.
   - `created`, `changed`, `deleted`, `moved`, `authored`, and `affected` are ids.
   - A move is not a string. It is `{id, old_location, new_location}`.

4. Read paths never commit.
   - Building or reading a graph must not call `sync_all`, advance `head`, persist
     identity, or publish to the bus.

5. Python projections are allowed, but they must not pretend to be canonical.
   - If `CodeGraph` remains Python-side, decide whether it is a projection built
     from snapshots or a true transaction participant. Do not leave it in between.

6. Derived layers are content-hash keyed and should not need transaction-time
   mutable stale state unless that state is also revisioned and owned by the
   transaction.

7. The bus is a notification layer, not a write participant.
   - It receives the committed id-level delta after publication.
   - It never blocks the writer.

## P0 Findings

### P0.1 Snapshots Can Still Capture Live Disk

Evidence:

- `build_frozen` calls `pre_populate_generation` before constructing the frozen
  overlay.
- `pre_populate_generation` walks the live project root and reads missing files
  from `OsSystem`.
- Relevant files:
  - `rust/src/project.rs:1067`
  - `rust/src/project.rs:1100`
  - `rust/src/project.rs:1121`

Why this violates the refined design:

- `REFINED_SPEC` requires reads at committed revision R to be satisfied from the
  revision-owned generation.
- If a file was not already interned into the generation, the snapshot builder
  reads whatever disk says at snapshot construction time, not what R contained.
- This also makes snapshot creation O(files), contrary to the O(1) capture target.

Impact:

- Time-travel snapshots can observe content from the wrong time.
- Two snapshots at the same revision can differ if disk changes between snapshot
  construction calls and the generation lacks the relevant entries.
- Derived hashes, identity, graph edges, and diffs can all be built over a
  revision that never actually committed.

Fix direction:

- Make committed generations complete for all project-relevant files and config.
- Move project disk ingest to open/sync/watch/commit time.
- Delete live disk pre-population from snapshot construction.
- Keep the frozen overlay no-disk-fallback behavior.

### P0.2 The Single Write Transaction Is Split Across Rust and Python

Evidence:

- Rust `commit_head` mutates/publishes native content and identity, then returns a
  `SyncResultDto`.
- Python write methods then call `_apply_graph_delta()` and `_publish_delta()`.
- Relevant files:
  - `rust/src/project.rs:1435`
  - `src/tyo3/session.py:939`
  - `src/tyo3/session.py:950`
  - `src/tyo3/session.py:951`
  - `src/tyo3/session.py:952`

Why this violates the refined design:

- The refined transaction requires all layer state to be coherent before
  publication.
- The Python graph and derived runtime are updated after native publication.
- If `_apply_graph_delta()` or derived invalidation fails, native revision R
  remains committed.
- If another thread reads between native return and Python updates, it can observe
  content/identity at R and Python layer state at R-1.

Impact:

- Snapshots and bus subscribers can see partial revisions.
- Derived invalidation can be skipped by exceptions or race with later writes.
- The live `session.graph` is an optional mutable cache, not a true L0 layer.

Fix direction:

- Choose one canonical owner for transaction state.
- Preferred final design: move canonical commit delta and code-layer state into
  Rust, and make Python `CodeGraph` a projection/export layer.
- Minimum acceptable repair: a Python transaction coordinator lock around native
  write, graph update, derived invalidation, and bus publication, plus delayed
  native publication until all participants succeed. This is harder to make
  correct than native ownership.

### P0.3 Read-Side Graph Construction Commits a Revision

Evidence:

- `TyO3Session.graph` lazily calls `CodeGraph.build(self)`.
- `CodeGraph.build` calls `_prime_identity_registry(session)`.
- `_prime_identity_registry` calls `session.sync_all()` unless a private flag is
  set.
- Relevant files:
  - `src/tyo3/session.py:732`
  - `src/tyo3/session.py:739`
  - `src/tyo3/graph/graph.py:34`
  - `src/tyo3/graph/graph.py:39`

Why this violates the refined design:

- A read accessor must not publish a revision, mutate identity, persist sidecar
  state, or enqueue bus notifications.
- `session.graph` is semantically a read, but it can perform a write.

Impact:

- Users can observe `session.head` changing from a property access.
- Tests can pass accidentally because graph reads prime identity.
- The codebase has to carry `_graph_identity_primed` hacks.

Fix direction:

- Remove `_prime_identity_registry`.
- Reconcile identity on open/sync/write as an explicit write lifecycle step.
- If identity is missing for a snapshot graph, report a typed build failure rather
  than mutating the session.

### P0.4 The Delta Contract Is Not DurableId-Level

Evidence:

- `SyncResultDto.created/changed/deleted` are documented as path strings.
- Python `SyncResult` mirrors them as `list[str]` with no stronger type.
- Bus code tries to convert paths to ids opportunistically if a graph exists.
- Derived invalidation treats those same strings as DurableIds.
- Relevant files:
  - `rust/src/dto/sync.rs:1`
  - `rust/src/project.rs:1453`
  - `src/tyo3/models/analysis.py:82`
  - `src/tyo3/bus/delta.py:119`
  - `src/tyo3/session.py:753`

Why this violates the refined design:

- The refined delta is precise at entity identity level.
- Path strings cannot distinguish multiple changed entities in one file.
- A move is a durable-id-preserving event, not path deletion plus creation.

Impact:

- Derived invalidation can silently no-op because it receives file paths where it
  expects entity ids.
- Bus notifications may be file-scoped rather than entity-scoped.
- Consumers cannot rely on `changed` meaning content-hash changed.

Fix direction:

- Introduce a new native `CommitDeltaDto`.
- Use explicit fields:
  - `created_ids`
  - `changed_ids`
  - `deleted_ids`
  - `moved: list[MovedEntityDto]`
  - `authored_ids`
  - `affected_ids`
  - `touched_files`
  - `rescan`
- Keep a compatibility facade only temporarily, and mark it deprecated.

### P0.5 Move Events Are Overloaded Strings

Evidence:

- `run_identity_reconciliation` maps moved DurableIds to current
  `qualified_path` strings.
- Graph and bus code normalize `result.moved` as if each entry were a path.
- Relevant files:
  - `rust/src/project.rs:1396`
  - `src/tyo3/graph/graph.py:1980`
  - `src/tyo3/bus/delta.py:135`

Why this violates the refined design:

- A move must preserve the DurableId and report old/new location separately.
- A qualified path is neither a DurableId nor a filesystem path.

Impact:

- Moved entity graph payloads may not update.
- Bus `moved_ids` can be empty or wrong.
- Derived caches may not reuse artifacts correctly on moves.

Fix direction:

- Make reconciliation return `MovedEntity { id, old_path, new_path }`.
- Keep content hash comparison separate from move detection.
- Update graph and bus to consume this structured move object.

### P0.6 Persistence Failures Can Leave Published Partial State

Evidence:

- Identity persistence failures are logged but not returned.
- Authored writes advance the revision and publish the generation before writing
  the authored record; on persistence failure they roll back only the authored
  in-memory map.
- Relevant files:
  - `rust/src/project.rs:1409`
  - `rust/src/project.rs:1933`
  - `rust/src/project.rs:1951`
  - `rust/src/project.rs:1987`

Why this violates the refined design:

- The refined transaction requires either full publication or rollback to the
  prior revision.
- A commit that succeeds in memory but fails in sidecar persistence is not durable
  knowledge.
- A commit that returns an error after advancing the revision is not atomic.

Impact:

- Identity can be stale after restart while callers believe a commit succeeded.
- Authored writes can leave revision gaps or published empty revisions after an
  error.
- The system can lose authored knowledge silently across process restart.

Fix direction:

- Stage state and sidecar writes before publication.
- Return typed errors on sidecar write failures.
- Do not bump/publish revision until all required persistence has succeeded.
- Add rollback tests that assert `head`, registry, authored store, and sidecar are
  unchanged after forced failure.

## P1 Findings

### P1.1 `discard()` Does Not Publish to the Bus

Evidence:

- `discard()` calls `_apply_graph_delta(result)` and returns.
- It omits `_publish_delta(result)`.
- Relevant file: `src/tyo3/session.py:1002`

Impact:

- Subscribers miss a committed revision.
- Gate 8's "every committed revision" guarantee is broken.

Fix direction:

- Add a regression test first.
- Publish the committed delta from `discard`.
- Better: centralize all Python post-commit hooks behind one method so write paths
  cannot diverge.

### P1.2 Bus `overflow = "block"` Can Stall the Writer

Evidence:

- `Subscription._offer` waits when queue is full and overflow is `block`.
- Relevant file: `src/tyo3/bus/subscription.py:72`

Impact:

- Slow subscribers can block the publishing thread.
- This contradicts the refined bus requirement.

Fix direction:

- Remove `block` from the validated config, or rename it to an explicitly
  non-compliant debug mode that is never used by default.
- Preferred final states:
  - `coalesce`: union deltas without hiding affected ids.
  - `drop_and_mark_lagged`: subscriber must rescan.
  - `error`: mark lagged and close subscription.

### P1.3 Derived Invalidation Uses the Wrong IDs and Leaks a Snapshot

Evidence:

- `_invalidate_derived` passes path strings to `dag.invalidate`.
- `DerivationDAG.invalidate` opens a snapshot and closes it, then calls
  `scheduler.process_all(self, session.snapshot())` with a fresh unclosed snapshot.
- Relevant files:
  - `src/tyo3/session.py:753`
  - `src/tyo3/derive/dag.py:117`
  - `src/tyo3/derive/dag.py:164`

Impact:

- Invalidation can skip all actual entities.
- Eager recompute can leak pinned snapshots.
- Recompute can run against a different head snapshot than the invalidation pass.

Fix direction:

- Feed id-level deltas.
- Use a single pinned snapshot for invalidate and eager process.
- Always close snapshots with `try/finally`.
- Consider making staleness a pure read-time comparison of current input hash
  against last-good cache binding.

### P1.4 Session Convenience APIs Return Closed Lazy Views

Evidence:

- `session.code`, `session.layer(name)`, and `session.entity(id)` create a snapshot,
  return a lazy object tied to it, then immediately close the snapshot.
- Relevant file: `src/tyo3/session.py:1305`

Impact:

- Returned views can fail later because their snapshot is closed.
- The API looks convenient but is unsafe unless every view eagerly materializes all
  data before snapshot close.

Fix direction:

- Either keep the snapshot alive in the returned object, or return eager immutable
  values.
- Prefer context-managed reads for layer views:
  - `with session.snapshot() as snap: snap.code...`
- If convenience sugar remains, return value objects, not lazy views.

### P1.5 Hashing Is Text-Heuristic, Not AST-Canonical

Evidence:

- `normalise_entity_source` strips/normalizes source text line-by-line.
- The module documentation says AST-normalized hashing, but the implementation is
  not an AST canonical renderer.
- Relevant file: `rust/src/hash.rs:89`

Impact:

- Hashes can change on syntactic formatting that should be cosmetic.
- Hashes can stay unchanged when the heuristic accidentally erases meaningful
  constructs.
- Docstring/comment handling is especially fragile for multi-line strings and
  string literals that are not docstrings.

Fix direction:

- Parse entity source into AST.
- Render a canonical form from AST nodes under a `HashPolicy`.
- Keep comments/docstrings policy explicit.
- Add property tests for formatting, docstrings, strings, literals, decorators,
  annotations, nested classes, and overloads.

### P1.6 Store Backends Swallow Operational Errors

Evidence:

- `LanceDbStore` catches broad exceptions and returns `None`, empty lists, or no-op.
- `DerivedLayerView.value`, `AuthoredLayerView.value`, and diff methods also hide
  broad exceptions.
- Relevant files:
  - `src/tyo3/stores/lancedb_store.py:40`
  - `src/tyo3/stores/lancedb_store.py:75`
  - `src/tyo3/layers/derived.py:53`
  - `src/tyo3/layers/authored.py:39`

Impact:

- Missing optional backend, corrupt store, query failure, and absent artifact can
  all look identical.
- Users cannot distinguish "not found" from "backend broken".

Fix direction:

- Replace broad exception swallowing with typed errors.
- Only treat true "not found" as absence.
- Log with enough context when intentionally degrading behavior.

### P1.7 Python and Rust Duplicate Config/Sidecar Responsibilities

Evidence:

- Rust loads and validates `.tyo3/config.toml`.
- Python re-reads coordination settings from TOML in `_read_coordination_config`.
- Python has a separate `Sidecar` mirror.
- Relevant files:
  - `rust/src/config.rs`
  - `rust/src/sidecar.rs`
  - `src/tyo3/config.py`
  - `src/tyo3/sidecar.py`
  - `src/tyo3/session.py:772`

Impact:

- Python can silently ignore invalid coordination config because it catches all
  exceptions and falls back to defaults.
- Config behavior can diverge across languages.

Fix direction:

- Treat Rust validated config JSON as the single source for Python.
- Remove direct TOML reads from Python.
- Keep Python sidecar paths only as a lightweight path facade if needed.

### P1.8 The Codebase Has Monoliths and Leaky Abstractions

Evidence:

- `rust/src/project.rs` handles project open, write transactions, snapshots,
  watchers, authored writes, identity, conversion, and PyO3 methods.
- `src/tyo3/session.py` handles read facade, write facade, watcher daemon, bus,
  graph cache, derived runtime, authored facade, and convenience views.
- `src/tyo3/graph/graph.py` handles graph construction, incremental update,
  diagnostics, diff helpers, dependency queries, graph algorithms, and external
  resolution.

Impact:

- Ownership boundaries are difficult to reason about.
- Review is harder because side effects are spread across many layers.
- New contributors are likely to patch symptoms rather than preserve invariants.

Fix direction:

- Split by responsibility:
  - Native transaction/core
  - Native snapshot/read handle
  - Identity registry
  - Content ingest
  - Sidecar persistence
  - Python projections
  - Python stores/generators
  - Bus

### P1.9 Rust Warnings Are Not Acceptable for Final Quality

Evidence:

- `cargo test` emits 16 warnings.
- Warnings include unused imports, dead variants, unused methods, and confusing
  lifetime syntax.

Impact:

- Warnings hide real regressions.
- Dead code indicates incomplete or abandoned design paths.

Fix direction:

- Drive `cargo test` warnings to zero.
- Add `cargo clippy` to the quality gate.
- Use `#[allow(...)]` only with a short reason.

## Test Suite Gaps

The test suite is broad and valuable, but it currently misses several refined
invariants.

Missing or insufficient tests:

- Snapshot construction must not read live disk for files missing from the
  generation.
- `session.graph` must not advance `head`.
- Every write method, including `discard`, publishes exactly one bus delta.
- Delta fields are DurableIds, not paths.
- A pure move reports `{id, old_location, new_location}` and does not emit
  created+deleted.
- Derived invalidation receives ids and recomputes/reuses as specified.
- Forced sidecar persistence failure rolls back revision and in-memory state.
- Authored write failure does not publish an empty revision.
- `overflow="block"` is rejected or proven never to block the writer.
- Session convenience APIs do not return views over closed snapshots.
- Snapshot/diff/layer code does not swallow graph build or store errors.
- Hashing is AST-canonical, not line-normalized.

The remaining xfail in `test_gate5_derived.py` is also a signal that the derived
layer story is not complete.

## Recommended Architecture Decision

The cleanest final direction is:

1. Rust owns the authoritative spine:
   - content generations
   - revision counter
   - identity registry
   - id-level commit delta
   - sidecar persistence
   - snapshot handles

2. The code graph must be either:
   - a Rust-owned canonical L0 layer updated in the native transaction, or
   - a Python projection built only from pinned snapshots and never described as
     canonical transaction state.

For the refined design, choose the first long-term. For a smaller personal-use
release, the second can be acceptable only if documentation and APIs stop claiming
that the live Python graph is transaction-updated canonical state.

3. Python owns projections and integrations:
   - Pydantic public models
   - optional derived generators
   - optional artifact store adapters
   - subscription queue objects around native deltas
   - ergonomic API wrappers

4. Derived staleness should be content-hash derived wherever possible:
   - If a cache key for the current input hash exists, it is fresh.
   - If not, serve last-good as stale or block, per layer policy.
   - Mutable per-id binding state must not be the source of truth unless it is
     revisioned and persisted.

## Final Readiness Bar

TyO3 is final-ready when all of the following are true:

- No snapshot read of committed project content can touch live disk.
- No read accessor can advance the revision.
- Every write has one id-level `CommitDelta`.
- `created/changed/deleted/moved/authored/affected` are DurableIds.
- The write transaction either fully publishes or fully rolls back.
- Sidecar failures are typed errors, not warnings.
- Code graph state is either canonical in the transaction or explicitly a pinned
  projection.
- Derived invalidation/recompute uses id-level deltas and closes snapshots.
- Bus delivery is complete, ordered, scoped, and non-blocking.
- Public convenience APIs do not return closed lazy views.
- Rust warnings are zero.
- The default test command has zero xfail once the final API is declared complete.
- A dedicated final acceptance suite proves every invariant above.
