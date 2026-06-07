# TyO3 Final Refactoring Guide

This guide is written for an intern or contributor who needs to refactor TyO3
into a final, personal-use-ready library. Follow it in order. Do not skip ahead:
later phases assume earlier invariants are already true.

The goal is not a large rewrite for its own sake. The goal is to make ownership
and truth simple:

- Rust owns committed revision truth.
- Deltas are DurableId-level.
- Python projections are honest projections.
- Reads do not write.
- Writes publish only complete revisions.

## Working Rules

1. Create one branch for this refactor.
2. Keep each phase small enough to review.
3. Write or un-xfail the failing test before changing implementation.
4. Do not relax tests to make progress.
5. Do not swallow exceptions unless the contract explicitly says absence is OK.
6. After each phase run the phase-specific tests and the focused gate tests.
7. After each milestone run:
   - `devenv shell -- pytest -q --no-cov`
   - `devenv shell -- cargo test --manifest-path rust/Cargo.toml`
8. Before final completion run:
   - `devenv shell -- pytest -q`
   - `devenv shell -- cargo test --manifest-path rust/Cargo.toml`
   - `devenv shell -- cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings`
   - `devenv shell -- ruff check src`
   - `devenv shell -- ruff format --check src`

If a command is not available in the devenv, document it in the PR and add it to
the environment as part of the refactor.

## Phase 0 - Add Final Invariant Tests First

Do this before implementation changes. The tests should initially fail where the
current implementation is wrong.

### 0.1 Add `test_final_content_spine.py`

Create tests for:

1. A snapshot at R does not read a file that was created on disk after R.
2. A snapshot at R keeps old file content after disk changes later.
3. Two snapshots at the same R are identical even if disk changes between their
   construction.
4. Snapshot construction does not walk/read live disk for missing project files.
5. `snapshot(at=evicted)` raises `RevisionEvictedError`.

Implementation hint:

- Use a temp project with `a.py`.
- Commit/open/sync to R.
- Mutate disk behind TyO3.
- Request `snapshot(at=R)` and assert R's content.
- To prove no disk read, use a file that would cause a visible diagnostic or symbol
  if read from disk after R.

Acceptance:

- The tests fail on the current implementation for the disk-prepopulation bug.
- They pass only after Phase 1.

### 0.2 Add `test_final_no_read_side_writes.py`

Create tests for:

1. `session.graph` does not change `session.head`.
2. `session.snapshot().graph()` does not change `session.head`.
3. `session.latest.check()` does not change `session.head`.
4. `session.entity(id)` or replacement convenience API does not close a snapshot
   before the returned value is usable.

Acceptance:

- The `session.graph` test should catch the current `sync_all` priming path.

### 0.3 Add `test_final_commit_delta_contract.py`

Create tests for:

1. Single function edit returns `changed_ids` containing the function DurableId,
   not the file path.
2. Editing two functions in the same file reports two distinct ids.
3. Cosmetic whitespace edit does not report `changed_ids`.
4. Pure move reports `moved` with id, old location, and new location.
5. Delete reports `deleted_ids` and authored records become orphaned, not deleted.
6. Rescan sets `rescan=True` and consumers treat it as full rebuild.

Acceptance:

- These tests should fail until Phase 2/3 introduces the new delta DTO.

### 0.4 Add `test_final_transaction_rollback.py`

Create tests that force failures:

1. Identity sidecar write failure leaves `head`, registry, and sidecar unchanged.
2. Authored record persistence failure leaves `head`, authored store, and sidecar
   unchanged.
3. Graph/layer update failure does not publish a partial revision.

Implementation hint:

- Prefer test seams over chmod tricks. Add a native test-only fault injection flag
  if needed.
- The point is to prove rollback semantics, not filesystem permission behavior.

### 0.5 Add `test_final_bus_contract.py`

Create tests for:

1. `edit`, `edit_many`, `edit_virtual`, `sync_path`, `discard`, `sync_all`,
   `author`, and `poll_changes` all publish exactly one relevant bus delta when
   they commit.
2. Bus deltas are id-level.
3. Notifications are ordered by revision.
4. Slow subscriber does not block writer.
5. `overflow="block"` is rejected by config, or excluded from compliant mode.

### 0.6 Add `test_final_derived_contract.py`

Create tests for:

1. Derived invalidation receives DurableIds.
2. Move with unchanged hash reuses artifact.
3. Body change recomputes only affected ids.
4. Eager recompute closes snapshots.
5. Generator failure leaves last-good artifact intact and reports `failed`.

### 0.7 Add `test_final_hash_ast.py`

Create tests for:

1. Formatting variations hash the same.
2. Identifier/literal/control-flow changes hash differently.
3. Multi-line docstrings respect `include_docstrings`.
4. String literals that are not docstrings are not stripped.
5. Decorators, annotations, defaults, overloads, nested definitions, and class
   bases are included in the canonical hash.

## Phase 1 - Make Committed Generations Complete

This is the foundation. Do not continue until snapshots are truly revision-owned.

### 1.1 Define "project-relevant content"

In Rust, create a single function that decides what belongs in a generation:

- Python source files.
- Project config files used by ty discovery:
  - `pyproject.toml`
  - `ty.toml`
  - `setup.cfg`
  - `setup.py`
- `.tyo3/config.toml` and other sidecar files only if they are part of TyO3
  committed state. Be explicit.

Files to work in:

- `rust/src/project.rs`
- `rust/src/content.rs`
- `rust/src/overlay.rs`

### 1.2 Add disk ingest into `ContentStore`

Add native helpers:

- `ContentStore::ingest_project(root, filter) -> Revision`
- `ContentStore::apply_disk_batch(paths) -> Revision`
- `ContentStore::apply_overlay_batch(changes) -> Revision`

Rules:

- Disk is read once at ingest/commit time.
- Missing files become tombstones if their absence is part of a committed change.
- One batch is one revision.
- The retained generation for a revision is complete for all project-relevant
  content at that revision.

### 1.3 Seed content on open

On `TyProject.open`:

1. Load and validate config.
2. Create `ContentStore`.
3. Ingest project-relevant disk content into revision 0 or a clearly documented
   initial revision.
4. Build live `ProjectDatabase` over the live overlay using that generation.
5. Reconcile identity against the initial content without exposing a separate
   user-visible write.

Important:

- Opening a project should not require the first `session.graph` call to prime
  identity.
- If the initial ingest produces revision 0, document that revision 0 is the
  initial disk state.
- If it produces revision 1, make tests and docs explicit.

### 1.4 Delete snapshot disk pre-population

Remove or neuter:

- `pre_populate_generation`
- Any live `OsSystem` file reads from snapshot construction

`build_frozen` should receive a complete `Generation` and build the frozen overlay
directly.

### 1.5 Keep frozen overlay strict

Do not reintroduce disk fallback into `OverlaySystem::frozen`.

The frozen overlay may synthesize directory metadata from generation keys. That is
fine because it derives membership from the generation, not disk.

### 1.6 Acceptance

Run:

- `devenv shell -- cargo test --manifest-path rust/Cargo.toml content overlay project::phase4`
- `devenv shell -- pytest src/tyo3/tests/test_final_content_spine.py -q --no-cov`
- `devenv shell -- pytest src/tyo3/tests/test_mvcc_snapshots.py src/tyo3/tests/test_mvcc_concurrency.py -q --no-cov`

Exit criteria:

- Snapshot construction performs no project-content disk reads.
- Snapshot capture is O(1) in content.
- All same-revision snapshots are identical.

## Phase 2 - Replace `SyncResult` With an Id-Level Commit Delta

The current path-shaped `SyncResult` is the source of many downstream mistakes.

### 2.1 Add explicit native DTOs

In `rust/src/dto/sync.rs`, replace or extend `SyncResultDto` with:

```rust
pub struct CommitDeltaDto {
    pub revision: u64,
    pub created_ids: Vec<String>,
    pub changed_ids: Vec<String>,
    pub deleted_ids: Vec<String>,
    pub moved: Vec<MovedEntityDto>,
    pub authored_ids: Vec<String>,
    pub affected_ids: Vec<String>,
    pub touched_files: Vec<String>,
    pub rescan: bool,
    pub project_changed: bool,
    pub custom_stdlib_changed: bool,
}

pub struct MovedEntityDto {
    pub id: String,
    pub old_qualified_path: String,
    pub new_qualified_path: String,
    pub old_file: String,
    pub new_file: String,
}
```

Keep compatibility aliases only if needed, but the new internal and public path
should use `CommitDelta`.

### 2.2 Make reconciliation return ids and moves

Update `rust/src/identity.rs`:

- `Reconciliation::moved()` should expose ids plus old/new paths.
- `changed` should mean content hash changed, not exact-binding happened.
- `retired` should be deleted ids.
- `minted` should be created ids.

Do not infer changed ids from touched files.

### 2.3 Compute exact changed ids

During commit:

1. Extract entities for the affected scope.
2. Reconcile old registry to new registry.
3. Compare old and new content hashes per DurableId.
4. Emit:
   - created ids for minted anchors
   - changed ids for same id with different content hash
   - moved entries for same id with different location and same hash
   - deleted ids for retired anchors
5. If precise delta cannot be computed, set `rescan=True`.

### 2.4 Track touched files as metadata

Keep files separately:

- `touched_files` for UI, file-interest bus matching, and diagnostics.
- Never use file paths in `changed_ids`.

### 2.5 Update Python models

In `src/tyo3/models/analysis.py`, introduce:

- `MovedEntity`
- `CommitDelta`

Then update session methods to return `CommitDelta` or a renamed `SyncResult` whose
fields are id-level. Prefer the new name to avoid semantic confusion.

### 2.6 Update graph, bus, and derived runtime

Update:

- `src/tyo3/bus/delta.py`
- `src/tyo3/session.py`
- `src/tyo3/derive/dag.py`
- `src/tyo3/graph/graph.py`

Rules:

- Graph consumes `moved` as structured objects.
- Derived invalidation consumes `changed_ids | created_ids`.
- Bus publishes id-level deltas.
- File interests match against `touched_files` plus files of affected ids.

### 2.7 Acceptance

Run:

- `devenv shell -- pytest src/tyo3/tests/test_final_commit_delta_contract.py -q --no-cov`
- `devenv shell -- pytest src/tyo3/tests/test_gate5_derived.py src/tyo3/tests/test_gate8_bus.py -q --no-cov`
- `devenv shell -- cargo test --manifest-path rust/Cargo.toml identity`

Exit criteria:

- No downstream code treats a path as a DurableId.
- Move entries are structured.
- A two-function edit in one file reports two changed ids.

## Phase 3 - Remove Read-Side Mutations

### 3.1 Delete `_prime_identity_registry`

Remove from `src/tyo3/graph/graph.py`:

- `_prime_identity_registry`
- `_graph_identity_primed` behavior
- any tests that set `_graph_identity_primed`

Replace with:

- identity is reconciled on open and every write
- graph build fails with a typed/reportable error if required ids are missing

### 3.2 Make graph reads pure

Rules:

- `TyO3Session.graph` must not call `sync_all`.
- `Snapshot.graph()` must not mutate session state.
- `LatestView.graph()` must either be removed or clearly return a noncanonical
  projection.

### 3.3 Decide graph ownership

Choose one path.

Preferred final path:

- Rust owns canonical code-layer state or enough id-level structural index to
  compute deltas and affected closures.
- Python `CodeGraph` becomes a projection built from a pinned snapshot for
  exploration/export.

Acceptable short-term personal-use path:

- Remove live mutable `session.graph`.
- Make all graph access pinned:
  - `with session.snapshot() as snap: graph = snap.graph()`
- Bus and derived invalidation use native id-level deltas, not Python graph state.
- Do not claim that Python graph is transaction-updated L0 state.

Do not keep the current middle state.

### 3.4 Acceptance

Run:

- `devenv shell -- pytest src/tyo3/tests/test_final_no_read_side_writes.py -q --no-cov`
- `devenv shell -- pytest src/tyo3/tests/test_graph*.py -q --no-cov`

Exit criteria:

- No read accessor changes `head`.
- No private priming flag exists.
- Graph build either succeeds from committed identity or fails loudly.

## Phase 4 - Rebuild the Write Transaction Boundary

### 4.1 Centralize native commit

Create one native commit entry point:

```rust
fn commit(&mut self, mutation: Mutation) -> Result<CommitDeltaDto, CommitError>
```

All write methods should call it:

- `edit`
- `edit_many`
- `edit_virtual`
- `sync_path`
- `discard`
- `sync_all`
- `author`
- watcher `poll_changes`

### 4.2 Stage state before publication

Inside commit:

1. Clone/stage mutable state:
   - content store next generation
   - analysis change events
   - identity registry next state
   - authored store next state if applicable
   - code-layer state if canonical native graph is implemented
2. Compute id-level delta.
3. Prepare sidecar writes as temp files.
4. Apply analysis changes against staged/publishable content.
5. Persist sidecar files atomically.
6. Publish revision last.
7. Return `CommitDeltaDto`.

If any step fails, return a typed error and leave all public state at the prior
revision.

### 4.3 Do not publish before fallible side effects

Current authored write publishes before persistence. Fix it.

Required rollback behavior:

- `head` unchanged
- retained generations unchanged
- registry unchanged
- authored store unchanged
- sidecar unchanged except possibly discarded temp files
- no bus delta

### 4.4 Add typed errors

Extend native exceptions or DTO errors for:

- `SidecarWriteError`
- `CommitFailed`
- `ReconcileAmbiguous`
- `GeneratorFailed` remains Python-side if generators stay Python-side

Do not map commit failures to `InternalTyError`.

### 4.5 Acceptance

Run:

- `devenv shell -- pytest src/tyo3/tests/test_final_transaction_rollback.py -q --no-cov`
- `devenv shell -- pytest src/tyo3/tests/test_write_path.py src/tyo3/tests/test_gate6_authored.py -q --no-cov`
- `devenv shell -- cargo test --manifest-path rust/Cargo.toml project authored sidecar`

Exit criteria:

- No failed write advances the revision.
- No sidecar error is swallowed.
- All write methods share the same commit path.

## Phase 5 - Make Sidecar Persistence Durable and Honest

### 5.1 Treat sidecar writes as commit participants

Identity and authored records are durable knowledge. They are not optional logs.

Rules:

- `identity.db` write failure fails the commit.
- authored record write failure fails the commit.
- newer format version fails open with `FormatVersionError`.
- corrupt known-version authored record should fail loudly unless a documented
  recovery mode is explicitly requested.

### 5.2 Use revision-stamped sidecar state

Add revision metadata to persisted identity/authored files where missing:

- current registry revision
- record revision
- format version

On open:

- Load sidecar.
- Ingest project content.
- Reconcile sidecar identity against current content.
- Never attach authored records to code without reconciliation.

### 5.3 Make crash safety explicit

`write_atomic` is good, but final crash safety should define startup behavior:

- Ignore `.tmp` files.
- Reject newer versions.
- Reconcile identity after restart.
- Cache is regenerable and can be missing.
- Authored records are never silently dropped.

### 5.4 Acceptance

Run:

- `devenv shell -- pytest src/tyo3/tests/test_gate4_config_sidecar.py src/tyo3/tests/test_gate6_authored.py -q --no-cov`
- Add tests for interrupted identity write and authored write rollback.

Exit criteria:

- Sidecar failure modes are typed and tested.

## Phase 6 - Repair Derived Layers

### 6.1 Make derived inputs purely id/hash based

Derived invalidation should consume:

- `created_ids`
- `changed_ids`
- `deleted_ids`
- `moved`
- `affected_ids`

It must not consume paths.

### 6.2 Prefer read-time staleness over mutable stale flags

For a derived layer and entity id at snapshot R:

1. Resolve entity content hash under the layer hash profile.
2. Build store key `(input_hash, generator_version)`.
3. If artifact exists, return fresh.
4. If artifact missing and last-good exists, return stale or failed.
5. If no artifact exists, return absent or block per policy.

This makes staleness honest without requiring transaction-time derived state.

### 6.3 Fix scheduler snapshot lifetime

In `DerivationDAG.invalidate`:

- Use one pinned snapshot for invalidation and eager recompute.
- Close it in `finally`.
- Do not call `scheduler.process_all(self, session.snapshot())` without closing.

### 6.4 Make store errors typed

Update stores:

- `FsStore.get`: only return `None` for missing file.
- `FsStore.put`: propagate IO errors.
- `LanceDbStore`: do not swallow query/write errors.
- Backend unavailable remains `StoreBackendUnavailable`.

### 6.5 Finish Gate 5 xfail

Un-xfail `test_self_healing_derived_layer_cache_hit_recompute_reuse`.

The test must prove:

- unrelated edit does not recompute
- content change recomputes
- move reuses artifact
- generator version bump uses new keyspace
- failure keeps last-good

### 6.6 Acceptance

Run:

- `devenv shell -- pytest src/tyo3/tests/test_final_derived_contract.py src/tyo3/tests/test_gate5_derived.py -q --no-cov`

Exit criteria:

- No remaining Gate 5 xfail.
- Derived invalidation is id-level.
- No snapshot leaks.

## Phase 7 - Repair the Bus

### 7.1 Centralize post-commit publication

After a successful native commit returns a `CommitDelta`, call exactly one helper:

```python
def _after_commit(self, delta: CommitDelta) -> None:
    self._invalidate_head_snap()
    self._publish_delta(delta)
    self._schedule_derived(delta)
```

Do not let individual write methods choose their own hook sequence.

If graph remains a Python projection, do not update it here. Invalidate projection
caches instead.

### 7.2 Publish from every write path

Covered methods:

- `edit`
- `edit_many`
- `edit_virtual`
- `sync_path`
- `discard`
- `sync_all`
- `author`
- `poll_changes`

### 7.3 Remove writer-blocking policy

Config should reject `overflow="block"` for the refined bus.

Supported policies:

- `coalesce`
- `drop_and_mark_lagged`
- `error_and_close`

All policies must avoid blocking the writer.

### 7.4 Deliver ordered id-level deltas

Bus `Delta` should be a thin immutable wrapper over native `CommitDelta`.

Interest matching:

- id interest matches `affected_ids`
- file interest matches `touched_files` plus files resolved from affected ids
- layer interest matches touched layers
- rescan matches all

### 7.5 Acceptance

Run:

- `devenv shell -- pytest src/tyo3/tests/test_final_bus_contract.py src/tyo3/tests/test_gate8_bus.py -q --no-cov`

Exit criteria:

- `discard` publishes.
- Slow subscribers do not stall commits.
- Deltas are ordered and id-level.

## Phase 8 - Repair Read Surface and Convenience APIs

### 8.1 Remove closed-snapshot views

Current `session.code`, `session.layer`, and `session.entity` close the snapshot
before returning lazy values.

Choose one final API shape:

Option A, preferred:

- Keep only explicit snapshot views:
  - `with session.snapshot() as snap: snap.code.value(id)`
- Remove `session.code`, `session.layer`, and `session.entity`, or make them return
  eager immutable values.

Option B:

- Returned view owns the snapshot and closes it when the view is closed.
- Provide context manager support:
  - `with session.code_view() as code: ...`

Do not return lazy views tied to closed snapshots.

### 8.2 Keep `LatestView` honest

`LatestView` can be useful for warm single-layer reads, but:

- no `entity`
- no cross-layer diff
- no pinned revision
- no mutable graph reference

If `latest.graph()` remains, it should return a clearly noncanonical projection or
be removed.

### 8.3 Stop swallowing read surface errors

Layer views should not catch broad exceptions and return `None` unless absence is
the exact documented outcome.

Replace with:

- typed absence
- typed backend failure
- graph build failure
- format/config failure

### 8.4 Acceptance

Run:

- `devenv shell -- pytest src/tyo3/tests/test_gate7_read_surface.py src/tyo3/tests/test_final_no_read_side_writes.py -q --no-cov`

Exit criteria:

- No convenience API returns closed lazy state.
- Cross-layer reads are pinned or explicitly floating.

## Phase 9 - Make Hashing Truly AST-Canonical

### 9.1 Replace line heuristics

Current `normalise_entity_source` is text-line based. Replace it with an AST-based
canonical renderer.

Approach:

1. Parse the entity source with Ruff parser.
2. Walk AST nodes.
3. Emit canonical tokens for:
   - node kind
   - identifiers
   - literals
   - signatures
   - annotations
   - decorators
   - bases
   - control flow
   - assignments
4. Exclude comments always unless the parser can preserve them reliably.
5. Include/exclude docstrings according to `HashPolicy`.

### 9.2 Define policy behavior precisely

Document and test:

- whitespace ignored
- trailing comma ignored
- comments included/excluded if supported
- docstrings included/excluded
- annotations significant
- decorators significant
- default values significant
- import aliases significant

### 9.3 Keep hash width and stability

- Continue using at least 128 bits.
- Keep output stable across machines.
- Store as a hex string or decimal string consistently. Prefer hex for readability.

### 9.4 Acceptance

Run:

- `devenv shell -- pytest src/tyo3/tests/test_final_hash_ast.py -q --no-cov`
- `devenv shell -- cargo test --manifest-path rust/Cargo.toml hash entity`

Exit criteria:

- Hashing tests prove canonical AST behavior.
- Documentation no longer overclaims compared to implementation.

## Phase 10 - Simplify Code Organization

Do this after behavioral fixes so movement is easier to review.

### 10.1 Split `rust/src/project.rs`

Suggested modules:

- `project/open.rs`
- `project/state.rs`
- `project/commit.rs`
- `project/snapshot.rs`
- `project/watch.rs`
- `project/identity.rs` or keep identity separate
- `project/authored.rs`
- `project/py_methods.rs`

Keep PyO3 methods thin. They should parse arguments, call core logic, and convert
errors.

### 10.2 Split `src/tyo3/session.py`

Suggested modules:

- `session.py`: public `TyO3Session`, small facade only
- `snapshot.py`: `Snapshot`, `LatestView`
- `writes.py`: Python post-commit hooks if any remain
- `read_ops.py`: shared read wrappers
- `exceptions.py`: public errors

Avoid circular imports by moving protocols/models out of implementation modules.

### 10.3 Split `src/tyo3/graph/graph.py`

Suggested modules:

- `graph/build.py`
- `graph/incremental.py`
- `graph/queries.py`
- `graph/diff.py`
- `graph/diagnostics.py`
- `graph/external.py`
- `graph/models.py`

If graph becomes a projection, name it accordingly:

- `CodeGraphProjection`
- `SnapshotCodeGraph`

### 10.4 Remove duplicate config reads

Python should use `TyConfig.from_json(self._inner.config_json())` only.

Delete Python TOML parsing in `_read_coordination_config`.

Expose validated coordination config in Rust JSON.

### 10.5 Acceptance

Run the full suite after each module split:

- `devenv shell -- pytest -q --no-cov`
- `devenv shell -- cargo test --manifest-path rust/Cargo.toml`

Exit criteria:

- No behavior changes.
- Public imports still work.
- Internal modules have clear ownership.

## Phase 11 - Remove Warning and Typing Debt

### 11.1 Rust warnings

Fix all warnings from:

- `cargo test`
- `cargo clippy --all-targets -- -D warnings`

Typical fixes:

- Remove unused imports.
- Remove dead variants or use them.
- Prefix intentionally unused test variables with `_`.
- Fix lifetime syntax in `AuthoredMap::ids_in_layer`.
- Add narrow `#[allow]` only when the unused code is intentionally reserved for a
  documented future feature.

### 11.2 Python typing

Reduce `Any` in:

- `src/tyo3/session.py`
- `src/tyo3/_native_impl.pyi`
- stores/generators

Add typed DTOs for native return dictionaries so wrappers validate less manually.

### 11.3 Mutable defaults

Replace mutable defaults with factories:

- `content_hashes: dict[str, str] = Field(default_factory=dict)` in Pydantic
  models.
- Audit dataclasses and class attributes.

### 11.4 Exception hygiene

Replace broad `except Exception: pass` with one of:

- typed catch and typed absence
- typed catch and log warning with context
- typed catch and re-raise domain error

Every swallowed exception needs a comment explaining why absence is correct.

### 11.5 Acceptance

Run:

- `devenv shell -- ruff check src`
- `devenv shell -- ruff format --check src`
- `devenv shell -- cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings`

Exit criteria:

- Zero warnings.
- No broad silent exception swallowing in production code.

## Phase 12 - Final Acceptance Suite

Create `src/tyo3/tests/test_final_acceptance.py` that exercises a full project:

1. Open project.
2. Snapshot initial revision.
3. Author intent on an entity.
4. Build/read code graph from a pinned snapshot.
5. Configure a deterministic derived layer.
6. Edit one entity.
7. Move another entity unchanged.
8. Delete one entity.
9. Read old and new snapshots.
10. Diff old and new snapshots.
11. Verify bus notifications.
12. Close and reopen.
13. Verify identity and authored records survive.
14. Delete cache and verify derived recompute.
15. Verify no source files were modified by sidecar behavior.

Assertions:

- Same revision means same content.
- DurableIds survive cosmetic edit and move.
- Content hash changes only on meaningful edits.
- Derived artifacts are keyed by content hash.
- Authored records are present, needs-review, or orphaned correctly.
- Bus deltas are ordered and id-level.
- Snapshot diff agrees with independent rebuild/projection.
- No read accessor advances head.

## Final Definition of Done

The refactor is done when all are true:

- `devenv shell -- pytest -q` passes with no unexpected xfail.
- `devenv shell -- pytest -q --no-cov` passes.
- `devenv shell -- cargo test --manifest-path rust/Cargo.toml` passes.
- `devenv shell -- cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings` passes.
- `devenv shell -- ruff check src` passes.
- `devenv shell -- ruff format --check src` passes.
- No snapshot construction reads live project content from disk.
- No read accessor can publish a revision.
- Native commit returns an id-level delta.
- Every write path uses the same commit and post-commit flow.
- Sidecar persistence errors roll back.
- Bus delivery is ordered, scoped, and non-blocking.
- Derived invalidation is id-level and closes snapshots.
- Convenience APIs are either eager values or own their snapshot lifetime.
- Rust warnings are zero.
- The final acceptance test proves the whole story end to end.

## Suggested Commit Plan

Use commits like:

1. `test: add final invariant regression tests`
2. `refactor(content): make committed generations complete`
3. `refactor(delta): introduce DurableId-level commit delta`
4. `refactor(graph): remove read-side identity priming`
5. `refactor(commit): centralize native write transaction`
6. `fix(sidecar): make persistence failures rollback commits`
7. `refactor(derived): consume id-level deltas and close snapshots`
8. `fix(bus): publish every write path and remove blocking overflow`
9. `fix(read): repair snapshot convenience lifetimes`
10. `refactor(hash): replace heuristic hashing with AST canonicalization`
11. `refactor: split session/project/graph monoliths`
12. `chore: zero warnings and tighten typing`
13. `test: add final acceptance scenario`

Each commit should pass its focused tests. Every third commit should pass the full
Python and Rust suites.
