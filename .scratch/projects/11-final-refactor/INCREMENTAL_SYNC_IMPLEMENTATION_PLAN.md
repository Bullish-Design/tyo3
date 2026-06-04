# Incremental Sync Implementation Plan

## Goal

Implement disk-backed incremental sync for TyO3 as an explicit dual-mode backend:

- Keep the current full-rebuild implementation as the default stable mode.
- Add an `incremental_disk` mode that mutates the existing ty `ProjectDatabase` through upstream ty change events.
- Preserve the current session read API and eventually preserve the snapshot API.
- Use the work as a natural stepping stone toward Option 6, without forcing Option 6 into the first incremental milestone.

This plan is based on the actual ty APIs available in the vendored Ruff/ty checkout, not on a hypothetical invalidation layer.

## What Ty Already Provides

### `ProjectDatabase::apply_changes`

Ty already has the core incremental API:

```rust
pub fn apply_changes(
    &mut self,
    changes: &[ChangeEvent],
    project_options_overrides: Option<&ProjectOptionsOverrides>,
) -> ChangeResult
```

Source:

- `ty_project/src/db/changes.rs`
- re-exported result type: `ty_project::ChangeResult`

This method handles the hard parts we do not want to duplicate:

- File content changes via `File::sync_path_only`.
- File creation via `File::sync_path` and project-file-set insertion.
- File and directory deletion.
- Recursive directory sync.
- `.gitignore`, `.ignore`, and related ignore-file changes.
- Project config rediscovery for `pyproject.toml`, `ty.toml`, config overrides, and extra configuration paths.
- Custom stdlib `VERSIONS` updates.
- Incremental project-file discovery through `ProjectFilesWalker::incremental`.
- Full rescan fallback through `ChangeEvent::Rescan`.

This is the right primitive for disk-backed incremental sync. TyO3 should not attempt to hand-edit salsa inputs directly unless `apply_changes` proves insufficient for a specific feature.

### `ChangeEvent`

Ty exposes `ty_project::watch::ChangeEvent`:

```rust
pub enum ChangeEvent {
    Opened(SystemPathBuf),
    Created { path: SystemPathBuf, kind: CreatedKind },
    Changed { path: SystemPathBuf, kind: ChangedKind },
    Deleted { path: SystemPathBuf, kind: DeletedKind },
    CreatedVirtual(SystemVirtualPathBuf),
    ChangedVirtual(SystemVirtualPathBuf),
    DeletedVirtual(SystemVirtualPathBuf),
    Rescan,
}
```

Useful supporting types:

- `CreatedKind::{File, Directory, Any}`
- `ChangedKind::{FileContent, FileMetadata, Any}`
- `DeletedKind::{File, Directory, Any}`
- `ExistingPathKind::from_system(system, path)`
- `ChangeEvent::file_content_changed(path)`

TyO3 can synthesize these events from explicit `sync_path()` calls before it adds any real file watcher.

### `ProjectWatcher`

Ty also exposes `ty_project::watch::ProjectWatcher` and `directory_watcher`.
`ProjectWatcher::update(&db)` watches:

- the project root,
- configured included paths,
- module search paths outside the project root,
- extra configuration paths.

This is useful for a later `watch` mode. It should not be part of the first incremental milestone because a manual `sync_path()` API is easier to test and easier to keep deterministic.

### Ruff Database File Helpers

The underlying `ruff_db` file table gives us useful building blocks:

- `db.files().try_system(db, path)` to check whether a system path already has
  a known salsa `File`.
- `system_path_to_file(db, path)` to intern or look up an existing file and
  require that it currently exists as a file.
- `File::sync_path`, `File::sync_path_only`, and recursive sync are already
  used by `apply_changes`.

TyO3's current `rust/src/files.rs` resolver canonicalizes the target path. That
is correct for read APIs, but wrong for sync APIs because deleted paths and
not-yet-created leaf paths must still be representable. Incremental sync needs
a separate path resolver.

### Cancellation Behavior

Incremental writes are not the same concurrency model as the current full
rebuild. TyO3 currently swaps in a fresh database on `reload()`, so outstanding
read clones continue against the old database.

With `apply_changes`, the canonical database is mutated in place. Ty and salsa
intentionally trigger cancellation during some mutations:

- `ProjectDatabase::system_mut()` calls `trigger_cancellation()`.
- `IndexedFiles::indexed_mut()` calls `trigger_cancellation()` before mutating
  the project file set.
- Salsa documents that `trigger_cancellation()` can block while snapshots exist.
- Ty's CLI catches `salsa::Cancelled` around background checks.

Therefore incremental sync must include read retry/cancellation handling, and
it must not claim the current clone-based `PySnapshot` is automatically safe in
incremental mode.

### Public In-Memory System

`ruff_db::system` publicly re-exports:

- `MemoryFileSystem`
- `InMemorySystem`
- `TestSystem`

`InMemorySystem` is documented as test-oriented, but it is public and provides
a practical prototype path for independent frozen snapshots and Option 6
experiments. Production use needs validation because it intentionally omits
some real file-system behavior such as symlinks, hardlinks, and permissions.

## Current TyO3 Baseline

Current `rust/src/project.rs` behavior:

- `PyTyProject::open(root)` canonicalizes the root.
- It creates `OsSystem::new(root)`.
- It creates `ProjectMetadata::new("tyo3-project", root)`.
- It creates `ProjectDatabase::use_defaults(metadata, system)`.
- `reload()` repeats that full construction and swaps the whole
  `TyProjectState`.
- Read APIs clone the database under a mutex and run ty work in `py.detach`.
- `snapshot()` clones the current database and eagerly materializes
  `source_text` for every project file to protect against later disk reads.

That design is simple and currently correct because the canonical database is
never mutated in place.

Incremental mode changes that invariant. The comments and concurrency model in
`rust/src/project.rs` must be updated as part of the implementation.

## Target Behavior

### Stable Default

`TyO3Session(root)` continues to use the current full-rebuild mode until
incremental sync has passed the full correctness and concurrency gate.

### New Incremental Disk Mode

Users can opt in:

```python
session = TyO3Session(root, sync_mode="incremental_disk")
```

or, if we prefer a boolean for the first implementation:

```python
session = TyO3Session(root, incremental_sync=True)
```

Recommended final API: `sync_mode`, because it leaves room for later modes:

- `"rebuild"`: current behavior.
- `"incremental_disk"`: disk-backed incremental sync.
- `"overlay"` or `"lsp"`: future Option 6-style overlay/in-memory mode.

### Incremental Public Methods

Add these to live sessions only:

```python
session.sync_path(path: str | Path) -> SyncResult
session.sync_paths(paths: Iterable[str | Path]) -> SyncResult
session.sync_all() -> SyncResult
```

Semantics:

- `sync_path()` tells ty that one disk path changed.
- `sync_paths()` batches several disk paths into one `apply_changes` call.
- `sync_all()` applies `ChangeEvent::Rescan`.
- In rebuild mode, these can either call `reload()` or raise
  `NotImplementedError`. Prefer calling `reload()` for `sync_all()` and raising
  for path-specific sync so callers do not think the path-specific fast path is
  active.

`reload()` remains available in both modes:

- Rebuild mode: current full database reconstruction.
- Incremental mode: `ChangeEvent::Rescan` through `apply_changes`.

### Sync Result

Return a small structured result so tests and callers can observe what happened:

```python
class SyncResult(BaseModel):
    revision: int
    project_changed: bool
    custom_stdlib_changed: bool
    mode: Literal["rebuild", "incremental_disk"]
    rescan: bool = False
```

At the Rust layer this can be a DTO serialized through the existing
`pythonize` path.

## Phase 0 - Guardrails And Decisions

1. Keep `rebuild` as the default.
2. Make `incremental_disk` explicit and documented as experimental until the
   snapshot phase is complete.
3. Use ty's own project construction path in incremental mode:
   `ProjectMetadata::discover`, then `apply_configuration_files`, then
   `ProjectDatabase::fallible`.
4. Keep current `ProjectMetadata::new` and `ProjectDatabase::use_defaults` for
   rebuild mode initially.
5. Document the behavior difference: incremental mode follows ty's config
   discovery semantics; rebuild mode preserves current TyO3 defaults.

Why this split matters:

- `apply_changes(Rescan)` rediscoveres project metadata through ty.
- If incremental mode started with `ProjectMetadata::new`, the first rescan
  could silently switch config semantics.
- Starting incremental mode with `discover` makes the mode internally
  consistent.

Acceptance criteria:

- No behavior changes for existing `TyO3Session(root)`.
- New mode can be enabled explicitly.
- Invalid config behavior is understood and tested for incremental mode.

## Phase 1 - Native State And Database Construction

Edit `rust/src/project.rs`.

### Add Mode And Revision State

Add a native enum:

```rust
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SyncMode {
    Rebuild,
    IncrementalDisk,
}
```

Extend `TyProjectState`:

```rust
struct TyProjectState {
    db: ProjectDatabase,
    root: SystemPathBuf,
    sync_mode: SyncMode,
    revision: u64,
}
```

If we implement an active-snapshot guard in the interim snapshot phase, add it
here too:

```rust
active_snapshots: Arc<AtomicUsize>
```

Update all clone helpers to copy the new fields.

### Add Construction Helpers

Create helpers:

```rust
fn build_rebuild_database(root: SystemPathBuf) -> ProjectDatabase
fn build_incremental_database(root: SystemPathBuf) -> PyResult<ProjectDatabase>
```

`build_rebuild_database` keeps today's behavior:

```rust
let system = OsSystem::new(root.clone());
let metadata = ProjectMetadata::new(Name::new("tyo3-project"), root);
ProjectDatabase::use_defaults(metadata, system)
```

`build_incremental_database` follows ty's tested watcher setup:

```rust
let system = OsSystem::new(root.clone());
let mut metadata = ProjectMetadata::discover(&root, &system)?;
metadata.apply_configuration_files(&system)?;
let db = ProjectDatabase::fallible(metadata, system)?;
```

Map metadata and database errors to a Python exception that the Python wrapper
will surface as `ProjectOpenError`. The Rust layer currently has no dedicated
open error class, so a `PyRuntimeError` or `PathResolutionError` is acceptable
for the first pass as long as `TyO3Session.__init__` wraps it.

### Update `open`

Change the PyO3 signature to accept a mode:

```rust
#[pyo3(signature = (root, sync_mode = "rebuild"))]
fn open(root: &str, sync_mode: &str) -> PyResult<Self>
```

Parse mode strings strictly:

- `"rebuild"`
- `"incremental_disk"`

Reject unknown values with a clear error.

Acceptance criteria:

- `TyProject.open(root, "rebuild")` behaves exactly like current open.
- `TyProject.open(root, "incremental_disk")` builds through ty discovery.
- `revision == 0` after open.

## Phase 2 - Sync Path Resolution And Event Synthesis

Do not reuse `rust/src/files.rs::resolve_file` for sync. It canonicalizes the
target path and rejects missing files, which breaks deletes.

Add a new helper, either in `rust/src/project.rs` near sync code or in a new
`rust/src/sync.rs` module:

```rust
fn resolve_sync_path(root: &SystemPath, path: &str) -> Result<SystemPathBuf, AnalysisError>
```

Rules:

1. If `path` is absolute, use it as the candidate path.
2. If `path` is relative, join it to the project root.
3. Do not canonicalize the final leaf.
4. For an existing path, it is fine to canonicalize to match ty's internal
   absolute path behavior.
5. For a missing path, canonicalize the nearest existing parent if possible,
   append the remaining components, and convert to `SystemPathBuf`.
6. Reject non-UTF-8 paths.
7. Do not require the path to be inside the root. A discovered ty project can
   include external paths through configuration, so absolute external sync
   paths must be representable.

Add an event classifier:

```rust
fn change_event_for_path(db: &ProjectDatabase, path: SystemPathBuf) -> ChangeEvent
```

Use ty's classifier where possible:

```rust
let kind = ExistingPathKind::from_system(db.system(), &path);
```

Recommended mapping:

- Existing file:
  - If `db.files().try_system(db, &path)` returns a known file whose status is
    currently exists, emit `Changed { kind: FileContent }`.
  - Otherwise emit `Created { kind: File }`.
- Existing directory:
  - Emit `Created { kind: Directory }`.
  - This is intentionally "directory may now contain new project files"; ty's
    `apply_changes` will walk it as needed.
- Existing non-file/non-directory:
  - Emit `Created { kind: Any }`.
- Missing path:
  - Emit `Deleted { kind: Any }`.
  - `DeletedKind::Any` is important because ty treats ambiguous deletes
    recursively when needed.

For `sync_paths`, deduplicate identical `SystemPathBuf`s before building
events. If any path explicitly maps to a rescan later, collapse the whole batch
to `[ChangeEvent::Rescan]`.

Acceptance criteria:

- Deleted files can be synced without path canonicalization failure.
- Newly created files can be synced.
- Newly created directories can be synced and walked.
- Absolute external configured paths can be synced.

## Phase 3 - Apply Changes Under The Lock

Add a mutation helper:

```rust
fn apply_disk_changes_locked(
    state: &mut TyProjectState,
    changes: Vec<ChangeEvent>,
) -> SyncResultDto
```

Rules:

1. Require `state.sync_mode == SyncMode::IncrementalDisk`.
2. Hold the project mutex while mutating `state.db`.
3. Call:

   ```rust
   let change_result = state.db.apply_changes(&changes, None);
   ```

4. Increment `state.revision` after every successful apply call, even if the
   change result reports no project structure change. This gives callers a
   simple monotonic revision.
5. Set `rescan = changes.iter().any(ChangeEvent::is_rescan)`.
6. Return `project_changed` and `custom_stdlib_changed` from `ChangeResult`.

Do not hold the Python GIL for extra work here. The PyO3 method body will have
the GIL, but this mutation is serialized by the Rust mutex anyway. If mutation
cost becomes visible, consider `py.detach` for the mutation later, but do not
start there.

Add methods:

```rust
fn sync_path(&self, py: Python<'_>, path: &str) -> PyResult<PyObject>
fn sync_paths(&self, py: Python<'_>, paths: Vec<String>) -> PyResult<PyObject>
fn sync_all(&self, py: Python<'_>) -> PyResult<PyObject>
```

`sync_all` emits `[ChangeEvent::Rescan]`.

`reload()` dispatch:

```rust
match state.sync_mode {
    SyncMode::Rebuild => rebuild_and_swap(),
    SyncMode::IncrementalDisk => apply_disk_changes_locked(state, vec![ChangeEvent::Rescan]),
}
```

If a future watcher is present, call `ProjectWatcher::update(&state.db)` after
`project_changed()` or after a rescan.

Acceptance criteria:

- Existing `reload()` tests pass in rebuild mode.
- Incremental mode can update one changed file without rebuilding the database.
- Incremental mode can rescan the project through `reload()` or `sync_all()`.

## Phase 4 - Cancellation-Aware Read APIs

In rebuild mode, the current read pattern is fine:

1. Lock.
2. Clone database.
3. Drop lock.
4. Run compute inside `py.detach`.

In incremental mode, a cloned read can be cancelled by an in-place write. Add a
helper that retries reads when salsa cancellation occurs:

```rust
fn read_with_retry<T, F>(
    inner: &Mutex<Option<TyProjectState>>,
    op_name: &str,
    py: Python<'_>,
    f: F,
) -> PyResult<T>
where
    T: Send + Ungil,
    F: Fn(&TyProjectState) -> T + Send + Copy + Ungil + std::panic::UnwindSafe,
```

Exact bounds may need adjustment, but the behavior should be:

1. Clone locked state.
2. Run the compute closure inside `py.detach`.
3. Wrap the compute body in `salsa::Cancelled::catch`.
4. On `Ok(value)`, return it.
5. On `Err(cancelled)`, clone the latest state and retry.
6. Retry a small fixed number of times, for example 3.
7. If all retries are cancelled, return a clear internal error:
   `"analysis was repeatedly cancelled by concurrent incremental writes"`.

The helper should preserve existing typed errors from the compute functions.
For compute methods that return `Result<T, AnalysisError>`, catch cancellation
outside that result.

Apply this helper to all live-session read methods:

- `files`
- `check`
- `check_file`
- `document_symbols`
- `workspace_symbols`
- `goto_definition`
- `goto_declaration`
- `goto_type_definition`
- `find_references`
- `semantic_tokens`
- `file_occurrences`
- `type_hierarchy`
- `hover`

Snapshot reads are handled separately in the snapshot phase.

Acceptance criteria:

- Concurrent read/write stress tests do not expose salsa cancellation as a
  Python panic.
- Rebuild mode keeps current behavior.
- Incremental mode may return either pre-write or post-write results for a
  racing live-session read, but never crashes.

## Phase 5 - Python API Surface

Edit `src/tyo3/session.py`.

### Constructor

Add a mode parameter:

```python
def __init__(
    self,
    root: str | StdPath,
    *,
    sync_mode: Literal["rebuild", "incremental_disk"] = "rebuild",
) -> None:
```

Pass it to the native layer:

```python
self._inner = _native.TyProject.open(root_str, sync_mode)
self._sync_mode = sync_mode
```

Keep positional `root` compatibility.

### Methods

Add:

```python
def sync_path(self, path: str | StdPath) -> SyncResult: ...
def sync_paths(self, paths: Iterable[str | StdPath]) -> SyncResult: ...
def sync_all(self) -> SyncResult: ...
```

Mapping:

- `_NativeClosedError` -> `ProjectClosedError`
- path errors -> existing path exception
- unexpected native errors -> `InternalTyError`

Edit `src/tyo3/_native_impl.pyi`:

```python
@staticmethod
def open(root: str, sync_mode: str = "rebuild") -> TyProject: ...
def sync_path(self, path: str) -> Any: ...
def sync_paths(self, paths: list[str]) -> Any: ...
def sync_all(self) -> Any: ...
```

Add a Python model for `SyncResult` in the same area as the other DTO-backed
models.

Acceptance criteria:

- Existing constructor calls are unchanged.
- Incremental mode is explicit.
- Sync methods are only on `TyO3Session`, not `Snapshot`.

## Phase 6 - Snapshot Safety In Incremental Mode

This is the main gate before incremental mode can become the default.

The current snapshot design holds a clone of the live salsa database. That is
safe with full rebuild because `reload()` swaps databases. It is not safe to
blindly reuse with in-place incremental mutation because ty may call
`trigger_cancellation()` while a snapshot handle exists.

There are two implementation stages.

### Stage 6A - Correct Interim Guard

For the first experimental incremental release, choose one conservative rule:

Option A: disable snapshots in incremental mode.

```python
session = TyO3Session(root, sync_mode="incremental_disk")
session.snapshot()  # raises NotImplementedError or InternalTyError with clear text
```

Option B: allow snapshots, but refuse writes while any incremental snapshot is
open.

Implementation:

- Add `active_snapshots: Arc<AtomicUsize>` to `TyProjectState`.
- Increment when `PySnapshot` is created from an incremental session.
- Decrement in `PySnapshot.close()` and `Drop`.
- `sync_path`, `sync_paths`, `sync_all`, and incremental `reload()` check the
  count and raise a clear error if it is nonzero.

Recommended interim choice: Option A. It is easier to explain and avoids
surprising write failures. Option B is useful only if we need snapshot parity
inside experimental incremental mode before independent snapshots are ready.

Acceptance criteria:

- Incremental writes cannot deadlock behind live clone-based snapshots.
- Tests prove the chosen behavior.

### Stage 6B - Independent Frozen Snapshots

To make incremental mode fully production-ready, implement snapshots that do
not share salsa storage with the live session.

The target shape is:

1. Snapshot creation reads the current synced revision from the live database.
2. It captures the project files and any required config/search-path files at
   that revision.
3. It constructs an independent database backed by a frozen system.
4. The snapshot owns that independent database.
5. Later incremental writes on the live session cannot cancel or block snapshot
   reads.

Possible implementation routes:

Route 1: `InMemorySystem` prototype.

- Use `ruff_db::system::MemoryFileSystem` or `InMemorySystem`.
- Enumerate `project.files(&state.db)`.
- For each file, materialize `source_text(&state.db, file)` and write it into
  the memory filesystem at its system path.
- Copy project configuration files needed for `ProjectMetadata::discover`.
- Copy enough directory structure for project discovery and file walking.
- Build a fresh `ProjectDatabase` in the memory system.

Concerns:

- `InMemorySystem` is documented as test-oriented.
- It omits symlinks, hardlinks, and permissions.
- Copying only project Python files may miss imported files from configured
  external search paths.
- Rediscovery may not exactly match live metadata if untracked config/search
  files are not captured.

Route 2: production `FrozenOverlaySystem`.

- Implement a small `System` wrapper in TyO3.
- It delegates to `OsSystem` for uncaptured paths.
- It overlays captured file contents, metadata, and path existence for paths in
  the project file set and relevant config/search roots.
- It captures directory listings for project roots so future disk-created files
  do not leak into old snapshots.
- It supports virtual files later for Option 6.

This is closer to Option 6 and is the recommended production route, but it is
more work.

Route 3: snapshot rebuild from disk.

- Build a fresh database from current disk and eagerly materialize.
- This is easy but not correct if disk has unsynced edits that the live
  incremental session has not consumed yet.
- Do not use this as the production snapshot implementation.

Recommended path:

1. Use Stage 6A for the first incremental milestone.
2. Prototype Route 1 in tests to understand required captured files.
3. Implement Route 2 for production snapshot parity and as the first real
   Option 6 stepping stone.

Acceptance criteria for full snapshot readiness:

- `snapshot()` is available in incremental mode.
- Existing snapshot isolation tests pass in both modes.
- A snapshot taken before `sync_path()` never sees the synced edit.
- Live incremental writes do not block on open snapshots.
- Snapshot reads do not receive `salsa::Cancelled` from live-session writes.

## Phase 7 - Optional Watch Mode

Do this after manual sync APIs are correct.

Add optional watcher state:

```rust
struct TyProjectState {
    watcher: Option<ProjectWatcher>,
    pending_changes: crossbeam::channel::Receiver<Vec<ChangeEvent>>,
    ...
}
```

Possible API:

```python
session.start_watching()
session.poll_changes() -> SyncResult | None
session.stop_watching()
```

Do not start a background Rust thread that mutates the database immediately.
Manual polling is simpler and avoids surprise writes racing every read.

Implementation outline:

1. Create a `directory_watcher` with a channel sender.
2. Wrap it in `ProjectWatcher::new(watcher, &state.db)`.
3. Store the receiver and watcher.
4. `poll_changes()` drains available event batches.
5. Apply all drained changes in one `apply_changes` call.
6. If the project changed, call `watcher.update(&state.db)`.
7. If the watcher reports errored paths, expose that in `SyncResult` or a
   separate status method.

Acceptance criteria:

- Manual `sync_path()` remains the deterministic test baseline.
- Watch mode can be enabled without changing default behavior.
- Watcher updates after config/search path changes.

## Phase 8 - Option 6 Stepping Stones

Disk-backed incremental sync does not itself provide true LSP buffer semantics,
but it builds several pieces needed by Option 6:

- A stable write pipeline around `ChangeEvent`.
- A clear distinction between read snapshots and live mutable state.
- Cancellation-aware reads.
- Revision tracking.
- A sync API that can later accept virtual/overlay changes.
- Tests that compare incremental state against fresh rebuild state.

When moving toward Option 6, extend the event layer rather than replacing it:

- Disk file changed: `Changed { kind: FileContent }`
- Disk file created: `Created`
- Disk file deleted: `Deleted`
- Unsaved editor buffer created: `CreatedVirtual`
- Unsaved editor buffer changed: `ChangedVirtual`
- Unsaved editor buffer closed: `DeletedVirtual`

The blocker is path and file identity:

- Current TyO3 read APIs resolve paths through disk canonicalization.
- Option 6 needs a resolver that can map editor documents to ty `File`s even
  when the document is unsaved, deleted on disk, or virtual.
- A `FrozenOverlaySystem` or production overlay system would address both
  snapshot independence and future editor-buffer semantics.

## Phase 9 - Test Plan

Add `src/tyo3/tests/test_incremental_sync.py`.

### Construction

1. `test_default_sync_mode_is_rebuild`
   - `TyO3Session(root)` still opens.
   - Existing `reload()` behavior remains.

2. `test_incremental_mode_opens`
   - `TyO3Session(root, sync_mode="incremental_disk")` opens.
   - `files()` returns expected project files.

3. `test_unknown_sync_mode_raises`
   - Invalid mode is rejected with a clear error.

### File Content Changes

4. `test_sync_path_changed_file_updates_symbols`
   - Open incremental session.
   - Read `document_symbols("main.py")`.
   - Modify `main.py` on disk.
   - Call `sync_path("main.py")`.
   - Assert new symbol appears.

5. `test_sync_paths_batches_multiple_changes`
   - Modify two files.
   - Call `sync_paths([...])`.
   - Assert both results update.

### Created And Deleted Files

6. `test_sync_path_created_file_updates_files`
   - Create `new_module.py`.
   - Call `sync_path("new_module.py")`.
   - Assert `files()` includes it.

7. `test_sync_path_deleted_file_updates_files`
   - Delete an existing project file.
   - Call `sync_path("deleted_file.py")`.
   - Assert `files()` excludes it.
   - Assert `check_file("deleted_file.py")` raises the existing path error.

8. `test_sync_path_created_directory_walks_python_files`
   - Create a directory with one or more `.py` files.
   - Call `sync_path("new_package")`.
   - Assert the new files are indexed.

### Rescan And Parity

9. `test_sync_all_matches_fresh_rebuild`
   - Apply several edits.
   - Call `sync_all()`.
   - Open a rebuild session on the same root.
   - Compare `files()` and selected symbols/diagnostics.

10. `test_incremental_reload_matches_sync_all`
    - In incremental mode, `reload()` and `sync_all()` should both rescan.

11. `test_incremental_eventual_parity_after_sequence`
    - Sequence: change file, create file, delete file, rescan.
    - Compare against a fresh rebuild session.

### Config And Ignore Files

12. `test_incremental_mode_uses_ty_config_discovery`
    - Add a `pyproject.toml` or `ty.toml`.
    - Open incremental mode.
    - Verify behavior follows ty config discovery.

13. `test_sync_path_config_change_rescans_project`
    - Change `pyproject.toml` or `ty.toml`.
    - Call `sync_path` for the config file.
    - Assert `SyncResult.project_changed` is true when expected.

14. `test_sync_path_ignore_file_updates_index`
    - Use `.gitignore` or `.ignore`.
    - Change ignore behavior.
    - Call `sync_path`.
    - Assert files are added/removed as expected.

### Concurrency

15. `test_incremental_read_while_syncing_does_not_crash`
    - Start several reader threads calling `check`, `workspace_symbols`, and
      `document_symbols`.
    - In another thread, edit files and call `sync_path`.
    - Assert no uncaught `salsa::Cancelled`, no panic, no deadlock.

16. `test_incremental_repeated_cancellation_retries_are_bounded`
    - If possible, force frequent writes while running heavy reads.
    - Assert the retry error is clean if the retry bound is exceeded.

### Snapshot Gate

17. Interim mode:
    - If snapshots are disabled in incremental mode, assert `snapshot()` raises
      the documented error.
    - If active-snapshot write guard is implemented instead, assert writes fail
      cleanly while a snapshot is open.

18. Full mode after independent snapshots:
    - Port all existing snapshot isolation tests to incremental mode.
    - Especially cover: snapshot before edit, edit disk, `sync_path`, old
      snapshot does not see the edit, new snapshot does.

## Phase 10 - Performance And Regression Benchmarks

Extend existing performance tests or add targeted ones:

1. `sync_path` changed file should be materially faster than full rebuild on a
   multi-file fixture.
2. `sync_path` created file should avoid rebuilding unaffected files.
3. `sync_all` can be slower than path sync but should not be worse than current
   rebuild by a large margin.
4. Repeated edit/sync/read loops should show memo reuse.
5. Memory use should not grow unbounded across many syncs.

Useful comparison matrix:

| Scenario | Rebuild Mode | Incremental Disk Mode |
| --- | --- | --- |
| changed one file | `reload()` | `sync_path(file)` |
| created one file | `reload()` | `sync_path(file)` |
| deleted one file | `reload()` | `sync_path(file)` |
| unknown many changes | `reload()` | `sync_all()` |
| config changed | `reload()` | `sync_path(config)` |

## Phase 11 - Verification Commands

Use the repo's `devenv` scripts.

During Rust edit loop:

```bash
devenv shell -- check-rust
```

After native API changes:

```bash
devenv shell -- rebuild
```

Targeted tests:

```bash
devenv shell -- pytest src/tyo3/tests/test_incremental_sync.py -v
devenv shell -- pytest src/tyo3/tests/test_concurrency.py -v
devenv shell -- pytest src/tyo3/tests/test_rust_integration.py -v
```

Full test suite:

```bash
devenv shell -- tests
```

If watcher mode is added, keep watcher tests separate at first because file
watchers can be platform-sensitive and timing-sensitive.

## Definition Of Done

Incremental disk sync is ready as an experimental dual mode when:

- Existing default rebuild behavior is unchanged.
- `incremental_disk` opens through ty discovery.
- `sync_path`, `sync_paths`, `sync_all`, and incremental `reload()` work.
- Changed, created, deleted, directory, config, and ignore-file cases are tested.
- Live-session reads handle salsa cancellation with retry.
- Snapshot behavior in incremental mode is explicitly correct, either disabled
  with a clear error or guarded from deadlock.
- Incremental state matches a fresh rebuild after equivalent disk changes.

Incremental disk sync is ready to become the default only when:

- Independent snapshot support is implemented.
- Incremental snapshot parity passes the existing snapshot isolation suite.
- Concurrent read/write stress tests pass repeatedly.
- Performance tests show path sync is meaningfully faster than full rebuild.
- Config discovery behavior is intentionally documented and accepted.

## Main Risks

1. Snapshot deadlock or cancellation leakage.
   - Mitigation: disable or guard snapshots in experimental mode; implement
     independent snapshots before defaulting incremental mode.

2. Behavior change from ty config discovery.
   - Mitigation: keep rebuild mode unchanged; make incremental mode explicit;
     test config behavior.

3. Path normalization bugs.
   - Mitigation: separate sync path resolution from read path resolution; test
     missing leaves, deleted files, absolute paths, and external included paths.

4. Watcher nondeterminism.
   - Mitigation: implement manual sync first; add watcher as opt-in polling
     later.

5. In-memory snapshot incompleteness.
   - Mitigation: treat `InMemorySystem` as prototype support; prefer a
     production `FrozenOverlaySystem` for Option 6 and final snapshot parity.

## Recommended Implementation Order

1. Add `SyncMode`, revision state, and database construction helpers.
2. Add Python/native mode plumbing with no sync methods yet.
3. Add sync path resolver and event classifier.
4. Add `sync_path`, `sync_paths`, `sync_all`, and incremental `reload()`.
5. Add focused incremental tests for changed/created/deleted files.
6. Add cancellation-aware read retry.
7. Add concurrency stress tests.
8. Add config and ignore-file tests.
9. Add interim snapshot guard for incremental mode.
10. Benchmark against rebuild mode.
11. Prototype independent snapshots with `InMemorySystem`.
12. Implement production `FrozenOverlaySystem`.
13. Re-enable snapshots in incremental mode.
14. Consider making `incremental_disk` the default only after all gates pass.

