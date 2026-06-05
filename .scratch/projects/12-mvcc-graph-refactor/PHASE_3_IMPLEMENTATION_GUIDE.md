# Phase 3 Implementation Guide — Revisions + the write path

> Audience: an engineer who has finished **Phase 1**
> (`PHASE_1_IMPLEMENTATION_GUIDE.md`, the `ContentStore` + `OverlaySystem`) and
> **Phase 2** (`PHASE_2_IMPLEMENTATION_GUIDE.md`, the HEAD db built over the
> overlay via real discovery), and is now implementing **Phase 3** of
> `MVCC_SUBSTRATE_ARCHITECTURE.md`.
>
> Goal of Phase 3: turn the (currently idle) `store`/`system` handles on
> `HeadState` into a **live write path**. After Phase 3 the head can be mutated:
> `edit` / `edit_many` / `edit_virtual` overlay in-memory content, `sync_path`
> ingests a disk change, `sync_all` triggers a rescan. Every mutation flows
> through the store, republishes the overlay, drives ty's incremental
> `ProjectDatabase::apply_changes`, bumps an application **`Revision`**, and
> returns a first-class **`SyncResult`** describing the delta.
>
> **Still out of scope:** independent (never-cancelled) MVCC snapshots, collapsing
> the duplicated read walls, deleting the eager-materialisation hack in
> `snapshot()` (all **Phase 4**); the concurrency stress proof (**Phase 5**); the
> graph (**Phases 6–7**); the file watcher (**Phase 8**); the floating
> cancel-retry `check()` fast path (**Phase 9**).
>
> When you finish: the project compiles, the **entire existing Python suite still
> passes unchanged**, and a new suite proves that an overlay edit changes what
> `check()` / `document_symbols()` see *without touching disk*, that the
> application revision advances, and that `SyncResult` reports the right delta.

---

## 0. Mental model (read this first)

Today the head is a read-only thing built once at `open`/`reload`. Phase 2 left
two fields on `HeadState` wired but **idle**:

```rust
struct HeadState {
    db: ProjectDatabase,
    root: SystemPathBuf,
    store: ContentStore,      // <- Phase 3 starts mutating this
    system: OverlaySystem,    // <- Phase 3 starts publishing to this
}
```

The whole of Phase 3 is one repeated five-step move, performed under the existing
`Mutex<Option<HeadState>>` lock:

```
1. mutate   head.store           (insert_text / delete / forget — persistent map, O(log n))
2. publish  head.system.publish(head.store.capture())   (ArcSwap store, O(1), lock-free)
3. apply    head.db.apply_changes(&events, None) -> ChangeResult   (ty does ALL incremental work)
4. bump     read head.store.revision()            (the application Revision)
5. report   build a SyncResult { revision, created, changed, deleted, flags, rescan }
```

**Ordering is load-bearing.** You must `publish` (step 2) *before* `apply_changes`
(step 3): `apply_changes` calls `File::sync_path`, which re-reads the file's
content and revision *through the overlay*. If you have not republished the new
generation yet, ty re-reads the *old* content and the edit is invisible. (The
architecture doc §3.1 numbers `publish` as "step 5" — that is logical grouping,
not temporal order. Publish before apply. Phase 2 §12 already states this.)

### What ty does for us (do not reimplement any of it)

`ProjectDatabase::apply_changes(&mut self, &[ChangeEvent], Option<&overrides>) ->
ChangeResult` (`ty_project/src/db/changes.rs:38`) is the entire incremental
engine: it syncs file content/metadata into salsa (`File::sync_path` /
`sync_path_only` / `sync_virtual_path`), handles created/deleted files and
directories, detects config/ignore/stdlib changes, rediscovers the project when
needed, and walks the file set. We feed it a precise `Vec<ChangeEvent>` and read
back a `ChangeResult { project_changed, custom_stdlib_changed }`. That is the
whole contract.

`ChangeResult` deliberately does **not** tell us *which* files changed
(`changes.rs:21`). That is fine and intended: **we synthesised the events, so we
already know the delta.** Carrying that delta ourselves is what `SyncResult` is.

### The reference for event synthesis

ty's own language server is the blueprint for turning an edit into a
`ChangeEvent`. Read these in the vendored checkout
(`~/.cargo/git/checkouts/ruff-b18f69e2b025fac7/3cb09eb/`):

- **Opening / first edit of a system file** → `Created{File}` vs `Opened`:
  `ty_server/src/session.rs:1213-1223` (`open_document_in_db`). The "is this file
  new?" test is `db.files().try_system(db, path).is_none_or(|f| !f.exists(db))`.
- **Subsequent content edit** → `ChangeEvent::file_content_changed(path)`
  (a `Changed{FileContent}`) for system paths, `ChangedVirtual(path)` for virtual
  paths: `ty_server/src/session.rs:1849-1860` (`update_in_db`).
- **Disk change ingest** (our `sync_path`) → classify with
  `ExistingPathKind::from_system` into `Created` / `Changed` / `Deleted`:
  `ty_server/src/server/api/notifications/did_change_watched_files.rs:41-71`.
- **Rescan** (our `sync_all`) → a single `[ChangeEvent::Rescan]`
  (`watch.rs:57`; handled at `changes.rs:268`).

The `ChangeEvent` vocabulary and its `*Kind` enums live in
`ty_project/src/watch.rs:24-130` and are re-exported as `ty_project::watch::*`.

---

## 1. Prerequisite check

Phase 3 assumes Phases 1 and 2 are merged/working. Run the baseline first (always
via devenv — see `MEMORY.md`, never bare cargo/pytest):

```bash
devenv shell -- check-rust
devenv shell -- test-rust
devenv shell -- tests
```

You rely on, and will extend:

- `crate::content::{ContentStore, Generation, Revision, Document}` — Phase 1.
- `crate::overlay::OverlaySystem` with `live` / `frozen` / `publish` and
  `Clone`-shares-content semantics — Phase 1.
- `HeadState { db, root, store, system }` behind
  `PyTyProject.inner: Mutex<Option<HeadState>>`, built by `build_head` — Phase 2.
- The `#[allow(dead_code)]` on `HeadState.system` (Phase 2 §2.1 note) — **remove
  it** in Phase 3, the field is now read by every write method.

---

## 2. Extend `ContentStore` for virtual buffers (`rust/src/content.rs`)

Phase 1's store keys only `SystemPathBuf`. `edit_virtual` needs a parallel map
keyed by `SystemVirtualPathBuf`. The cleanest change is to make a `Generation`
carry **both** maps in a small struct, so a single `capture()` still pins all
overlay content at once and a single `publish()` swaps it atomically.

### 2.1 The content map

Replace the bare type alias

```rust
pub type Generation = Arc<HashTrieMapSync<SystemPathBuf, Document>>;
```

with a two-map struct:

```rust
use ruff_db::system::{SystemPathBuf, SystemVirtualPathBuf};

/// All overlay content at one revision: system-path documents and virtual-path
/// documents. Both are persistent maps, so cloning a `ContentMap` is two cheap
/// `Arc` bumps and structurally shares with the previous generation.
#[derive(Debug, Clone, Default)]
pub struct ContentMap {
    pub system: HashTrieMapSync<SystemPathBuf, Document>,
    pub virtual_files: HashTrieMapSync<SystemVirtualPathBuf, Document>,
}

impl ContentMap {
    pub fn new() -> Self {
        Self {
            system: HashTrieMapSync::new_sync(),
            virtual_files: HashTrieMapSync::new_sync(),
        }
    }
}

/// An immutable snapshot of all overlay content at one revision.
pub type Generation = Arc<ContentMap>;
```

> `#[derive(Default)]` on `ContentMap` works only if `HashTrieMapSync: Default`
> in this `rpds` version; if not, drop the derive and keep the explicit `new()`.

### 2.2 Store mutators (system + virtual)

`ContentStore` now holds a `Generation` (an `Arc<ContentMap>`) and rebuilds it on
each mutation. Keep the existing system-path methods working unchanged for Phase 1
tests (`insert_text` / `delete` / `forget` still bump + return `Revision`), but
have them operate on the `.system` sub-map:

```rust
pub struct ContentStore {
    generation: Generation,
    revision: Revision,
    version_counter: u64,
}

impl ContentStore {
    pub fn new() -> Self {
        Self {
            generation: Arc::new(ContentMap::new()),
            revision: Revision(0),
            version_counter: 0,
        }
    }

    pub fn revision(&self) -> Revision { self.revision }
    pub fn capture(&self) -> Generation { Arc::clone(&self.generation) }

    fn next_version(&mut self) -> u64 {
        self.version_counter += 1;
        self.version_counter
    }

    /// Replace the generation by applying `f` to a clone of the current map,
    /// then bump the application revision. Centralises the copy-on-write.
    fn mutate(&mut self, f: impl FnOnce(&mut ContentMap)) -> Revision {
        let mut map = (*self.generation).clone();   // 2 Arc bumps, structurally shared
        f(&mut map);
        self.generation = Arc::new(map);
        self.revision = Revision(self.revision.0 + 1);
        self.revision
    }

    pub fn insert_text(&mut self, path: SystemPathBuf, text: impl Into<Arc<str>>) -> Revision {
        let version = self.next_version();
        let doc = Document::Text { text: text.into(), version };
        self.mutate(|m| { m.system = m.system.insert(path, doc); })
    }

    pub fn delete(&mut self, path: SystemPathBuf) -> Revision {
        let version = self.next_version();
        self.mutate(|m| { m.system = m.system.insert(path, Document::Deleted { version }); })
    }

    pub fn forget(&mut self, path: &SystemPathBuf) -> Revision {
        let path = path.clone();
        self.mutate(|m| { m.system = m.system.remove(&path); })
    }

    // ── virtual buffers (new in Phase 3) ───────────────────────────────────
    pub fn insert_virtual(&mut self, path: SystemVirtualPathBuf, text: impl Into<Arc<str>>) -> Revision {
        let version = self.next_version();
        let doc = Document::Text { text: text.into(), version };
        self.mutate(|m| { m.virtual_files = m.virtual_files.insert(path, doc); })
    }

    pub fn forget_virtual(&mut self, path: &SystemVirtualPathBuf) -> Revision {
        let path = path.clone();
        self.mutate(|m| { m.virtual_files = m.virtual_files.remove(&path); })
    }
}
```

> The Phase-1 `lookup` free function (if you keep it) now takes
> `&generation.system`. Adjust its body or inline the two call sites — it is only
> used in tests.

### 2.3 Note on "one revision per edit"

`mutate` bumps `revision` by exactly 1 per call. `edit_many` (a batch) performs N
`insert_text` calls and therefore consumes N revision numbers, but publishes and
`apply_changes` **once** and reports the *final* revision. That is the intended
"one published revision per batch" — intermediate numbers are simply never
published or snapshotted. (A purist alternative — separate per-file content
versions from the application revision so a batch advances the counter by exactly
1 — is more churn for no observable gain and is deferred. Don't do it now.)

---

## 3. Serve virtual content from `OverlaySystem` (`rust/src/overlay.rs`)

Phase 1's overlay only consulted the (then bare-map) generation for system paths
and delegated all virtual-path methods to disk. Now that a generation carries a
virtual map, wire the four virtual touch-points the same overlay-first way
`LSPSystem` does (`ty_server/src/system.rs:212-236, 184-191`).

### 3.1 Update the shared-content type and lookups

```rust
use ruff_db::system::SystemVirtualPath;     // add
use crate::content::{ContentMap, Document, Generation, Revision};

type SharedContent = Arc<ArcSwap<ContentMap>>;   // was Arc<ArcSwap<HashTrieMapSync<…>>>
```

`document()` now reads the `.system` sub-map; add a virtual lookup:

```rust
fn document(&self, path: &SystemPath) -> Option<Document> {
    self.content.load().system.get(&path.to_path_buf()).cloned()
}

fn virtual_document(&self, path: &SystemVirtualPath) -> Option<Document> {
    self.content.load().virtual_files.get(&path.to_path_buf()).cloned()
}
```

`live` / `frozen` constructors are unchanged except that `initial` is now an
`Arc<ContentMap>` (the new `Generation`) — no code change, just the new type. The
Phase-1 tests that pass `Arc::new(rpds::HashTrieMapSync::new_sync())` as the empty
generation must become `Arc::new(ContentMap::new())` (or
`Arc::new(ContentMap::default())`); update those few test call sites.

### 3.2 Serve virtual reads + source type

Replace the three delegating virtual methods, and add `virtual_path_source_type`,
mirroring `LSPSystem`:

```rust
fn read_virtual_path_to_string(&self, path: &SystemVirtualPath) -> std::io::Result<String> {
    match self.virtual_document(path) {
        Some(Document::Text { text, .. }) => Ok(text.to_string()),
        Some(Document::Deleted { .. }) => Err(virtual_not_found(path)),
        None => self.native.read_virtual_path_to_string(path),
    }
}

fn virtual_path_source_type(&self, path: &SystemVirtualPath) -> Option<PySourceType> {
    match self.virtual_document(path) {
        Some(Document::Text { .. }) => path
            .extension()
            .and_then(PySourceType::try_from_extension)
            .or(Some(PySourceType::Python)),
        Some(Document::Deleted { .. }) => None,
        None => self.native.virtual_path_source_type(path),
    }
}
```

Keep `read_to_notebook` / `read_virtual_path_to_notebook` delegating to disk —
notebook *overlay* content is still out of scope (Phase 1 §8 deferred it; nothing
in Phase 3 needs it). Add a `virtual_not_found` helper next to `not_found`:

```rust
fn virtual_not_found(path: &SystemVirtualPath) -> std::io::Error {
    std::io::Error::new(
        std::io::ErrorKind::NotFound,
        format!("No such virtual path (overlaid as deleted): {path}"),
    )
}
```

> `publish()` already exists from Phase 1 and is unchanged — it `ArcSwap::store`s
> the new generation. The `debug_assert!(self.frozen.is_none())` guard still holds:
> we only ever publish to the live head.

---

## 4. The `SyncResult` DTO (`rust/src/dto/`)

Add a new DTO module mirroring the existing convention (plain serde structs,
serialized to a Python dict via `pythonize`).

Create `rust/src/dto/sync.rs`:

```rust
/// The delta produced by a write to the head, surfaced to Python as a dict.
///
/// `created` / `changed` / `deleted` are the project-relative-or-absolute path
/// strings we synthesised events for (the same paths the caller passed, resolved).
/// `revision` is the new application revision. `rescan` is true for `sync_all`,
/// where the delta is unknown and a full graph rebuild is required (Phase 6).
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SyncResultDto {
    pub revision: u64,
    pub created: Vec<String>,
    pub changed: Vec<String>,
    pub deleted: Vec<String>,
    pub project_changed: bool,
    pub custom_stdlib_changed: bool,
    pub rescan: bool,
}
```

Register it in `rust/src/dto/mod.rs` next to the others:

```rust
mod sync;
pub use sync::*;
```

> This is the Python-facing object. Internally you *could* also carry resolved
> `File` handles (the architecture's `SyncDelta` does, for the graph), but `File`
> is a salsa-interned handle bound to a specific db and is meaningless across the
> FFI boundary. Phase 6 will re-resolve these path strings to `File`s against the
> snapshot it operates on. For Phase 3, **paths are the delta.**

---

## 5. The sync-path resolver + event synthesis (`rust/src/project.rs`)

Two small free functions. Keep them separate from the existing **read-path**
resolver (`files::resolve_file`, which canonicalises — wrong for writes, because a
deleted or not-yet-created path must stay representable; architecture §4).

```rust
use ruff_db::system::{SystemPath, SystemPathBuf, SystemVirtualPathBuf};
use ty_project::watch::{ChangeEvent, ChangedKind, CreatedKind, DeletedKind, ExistingPathKind};

/// Resolve a caller-supplied path to the absolute `SystemPathBuf` used as BOTH
/// the overlay store key AND the `ChangeEvent` path. Absolute → as-is; relative →
/// joined onto the project root. The leaf is never canonicalised (so deletes and
/// new paths are representable). The SAME value must key the store and the event,
/// or the overlay lookup inside `apply_changes` won't align.
fn resolve_sync_path(root: &SystemPath, path: &str) -> SystemPathBuf {
    let p = SystemPath::new(path);
    if p.is_absolute() {
        p.to_path_buf()
    } else {
        root.join(p)
    }
}

/// Classify a *content overlay* edit of a system path into a `ChangeEvent`.
/// New (not yet interned, or interned-but-absent) → `Created{File}`; otherwise an
/// existing file's content changed → `Changed{FileContent}`. Mirrors ty_server's
/// `open_document_in_db` "is_maybe_new_system_file" test.
fn classify_overlay_edit(db: &ProjectDatabase, path: &SystemPathBuf) -> ChangeEvent {
    let is_new = db
        .files()
        .try_system(db, path)
        .is_none_or(|f| !f.exists(db));
    if is_new {
        ChangeEvent::Created { path: path.clone(), kind: CreatedKind::File }
    } else {
        ChangeEvent::file_content_changed(path.clone())
    }
}

/// Classify a *disk ingest* (`sync_path` / `discard`) after the overlay for
/// `path` has been forgotten, so the overlay falls through to disk truth.
fn classify_disk_sync(
    system: &OverlaySystem,
    db: &ProjectDatabase,
    path: &SystemPathBuf,
) -> ChangeEvent {
    match ExistingPathKind::from_system(system, path) {
        ExistingPathKind::File => {
            let is_new = db.files().try_system(db, path).is_none_or(|f| !f.exists(db));
            if is_new {
                ChangeEvent::Created { path: path.clone(), kind: CreatedKind::File }
            } else {
                ChangeEvent::Changed { path: path.clone(), kind: ChangedKind::Any }
            }
        }
        // Directory or absent → treat as a (possibly recursive) delete.
        _ => ChangeEvent::Deleted { path: path.clone(), kind: DeletedKind::Any },
    }
}
```

> `ExistingPathKind::from_system(system: &dyn System, …)` takes a `&dyn System`;
> `&OverlaySystem` coerces. It reads `path_metadata` through the overlay — which,
> because we forgot the overlay entry first, reflects disk. (`watch.rs:140`.)

There is intentionally **no** generic "bucket this event" helper: a virtual path
isn't a `SystemPath`, so a single match over `ChangeEvent` is awkward. Instead each
write method below buckets **at the call site**, where it already holds the
resolved path string and knows the category (created / changed / deleted). That is
simpler and avoids re-deriving the path from the event.

---

## 6. The write methods (`#[pymethods] impl PyTyProject`)

All five share the same skeleton. Factor the common tail into one helper so each
method is three lines of intent plus the event:

```rust
/// Publish the captured store generation, apply `events` to the db, and build the
/// SyncResult. Caller has already mutated `head.store` and bucketed the paths.
///
/// PRECONDITION: `head.store` is already mutated; this captures+publishes it.
fn commit_head(
    head: &mut HeadState,
    events: &[ChangeEvent],
    created: Vec<String>,
    changed: Vec<String>,
    deleted: Vec<String>,
    rescan: bool,
) -> dto::SyncResultDto {
    // 2. publish BEFORE apply (see §0) so apply_changes re-reads new content.
    head.system.publish(head.store.capture());
    // 3. ty does all incremental work.
    let result = head.db.apply_changes(events, None);
    // 4 + 5.
    dto::SyncResultDto {
        revision: head.store.revision().0,
        created,
        changed,
        deleted,
        project_changed: result.project_changed(),
        custom_stdlib_changed: result.custom_stdlib_changed(),
        rescan,
    }
}
```

Then the methods (note: writes hold the GIL — they are fast/incremental; reads
keep using `py.detach`. See §7 for why we do **not** detach writes in Phase 3):

```rust
#[pymethods]
impl PyTyProject {
    /// Overlay `path` with in-memory `text` (no disk write). Returns a SyncResult.
    fn edit<'py>(&self, py: Python<'py>, path: &str, text: &str) -> PyResult<Bound<'py, PyAny>> {
        let mut guard = lock_state(&self.inner, "edit")?;
        let head = guard.as_mut().unwrap();

        let abs = resolve_sync_path(&head.root, path);
        let event = classify_overlay_edit(&head.db, &abs);     // classify BEFORE mutating
        head.store.insert_text(abs.clone(), text);             // 1. mutate

        let path_str = abs.as_str().to_string();
        let (created, changed) = match &event {
            ChangeEvent::Created { .. } => (vec![path_str], vec![]),
            _ => (vec![], vec![path_str]),
        };
        let dto = commit_head(head, std::slice::from_ref(&event), created, changed, vec![], false);
        drop(guard);
        pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    /// Overlay many files atomically (one publish, one apply_changes, one revision).
    fn edit_many<'py>(&self, py: Python<'py>, edits: std::collections::HashMap<String, String>) -> PyResult<Bound<'py, PyAny>> {
        let mut guard = lock_state(&self.inner, "edit_many")?;
        let head = guard.as_mut().unwrap();

        let mut events = Vec::with_capacity(edits.len());
        let (mut created, mut changed) = (Vec::new(), Vec::new());
        for (path, text) in edits {
            let abs = resolve_sync_path(&head.root, &path);
            let event = classify_overlay_edit(&head.db, &abs);
            head.store.insert_text(abs.clone(), text);
            match &event {
                ChangeEvent::Created { .. } => created.push(abs.as_str().to_string()),
                _ => changed.push(abs.as_str().to_string()),
            }
            events.push(event);
        }
        let dto = commit_head(head, &events, created, changed, vec![], false);
        drop(guard);
        pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    /// Overlay a virtual/unsaved buffer (e.g. "untitled:1"). No disk involvement.
    fn edit_virtual<'py>(&self, py: Python<'py>, uri: &str, text: &str) -> PyResult<Bound<'py, PyAny>> {
        let mut guard = lock_state(&self.inner, "edit_virtual")?;
        let head = guard.as_mut().unwrap();

        let vpath = SystemVirtualPathBuf::from(uri);
        let is_new = head.db.files().try_virtual_file(&vpath).is_none();
        head.store.insert_virtual(vpath.clone(), text);

        let event = if is_new {
            ChangeEvent::CreatedVirtual(vpath.clone())
        } else {
            ChangeEvent::ChangedVirtual(vpath.clone())
        };
        let (created, changed) = if is_new {
            (vec![uri.to_string()], vec![])
        } else {
            (vec![], vec![uri.to_string()])
        };
        let dto = commit_head(head, std::slice::from_ref(&event), created, changed, vec![], false);
        drop(guard);
        pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    /// Ingest a disk change for `path`: drop any overlay for it and re-read disk.
    fn sync_path<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let mut guard = lock_state(&self.inner, "sync_path")?;
        let head = guard.as_mut().unwrap();

        let abs = resolve_sync_path(&head.root, path);
        head.store.forget(&abs);                       // 1. drop overlay → disk shows through
        head.system.publish(head.store.capture());     // publish so classify sees disk
        let event = classify_disk_sync(&head.system, &head.db, &abs);

        let path_str = abs.as_str().to_string();
        let (created, changed, deleted) = match &event {
            ChangeEvent::Created { .. } => (vec![path_str], vec![], vec![]),
            ChangeEvent::Deleted { .. } => (vec![], vec![], vec![path_str]),
            _ => (vec![], vec![path_str], vec![]),
        };
        // Already published above; apply directly (don't double-publish).
        let result = head.db.apply_changes(std::slice::from_ref(&event), None);
        let dto = dto::SyncResultDto {
            revision: head.store.revision().0,
            created, changed, deleted,
            project_changed: result.project_changed(),
            custom_stdlib_changed: result.custom_stdlib_changed(),
            rescan: false,
        };
        drop(guard);
        pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    /// Drop the overlay buffer for `path`, reverting to disk. (Alias semantics of
    /// sync_path: forget + reclassify against disk.)
    fn discard<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        // identical body to sync_path; factor a private `fn sync_path_inner(head, path)`
        // and call it from both. Kept separate in the public API for intent.
        self.sync_path(py, path)
    }

    /// Rescan everything (== the old `reload` semantics, now via apply_changes).
    fn sync_all<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let mut guard = lock_state(&self.inner, "sync_all")?;
        let head = guard.as_mut().unwrap();

        // Rescan keeps existing overlay buffers (they remain the system of record);
        // we just tell ty to re-walk and re-read everything.
        head.system.publish(head.store.capture());
        let result = head.db.apply_changes(&[ChangeEvent::Rescan], None);
        // bump the revision so callers can observe that a rescan happened.
        let revision = head.store.bump_revision().0;   // see §6.1
        let dto = dto::SyncResultDto {
            revision,
            created: vec![], changed: vec![], deleted: vec![],
            project_changed: result.project_changed(),
            custom_stdlib_changed: result.custom_stdlib_changed(),
            rescan: true,
        };
        drop(guard);
        pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    /// The current application revision.
    #[getter]
    fn head(&self) -> PyResult<u64> {
        let guard = lock_state(&self.inner, "head")?;
        Ok(guard.as_ref().unwrap().store.revision().0)
    }
}
```

One cleanup before you compile: factor `sync_path`'s body into a private
`fn sync_path_inner(head: &mut HeadState, abs: SystemPathBuf) -> dto::SyncResultDto`
that both `sync_path` and `discard` call. As written above, `discard` re-enters via
`self.sync_path(py, path)` — that works (the first guard is dropped before the
second lock), but a shared inner fn is cleaner and avoids re-locking and
re-resolving the path.

### 6.1 Add `ContentStore::bump_revision`

`sync_all` changes no content but should still advance the revision (a rescan is a
new observable state). Add to `content.rs`:

```rust
/// Advance the application revision without changing content (used by rescan).
pub fn bump_revision(&mut self) -> Revision {
    self.revision = Revision(self.revision.0 + 1);
    self.revision
}
```

### 6.2 Make `reload` delegate to `sync_all`'s engine? No — leave it.

`reload` (Phase 2) still does a full `build_head` rebuild. Architecture §10.2 says
`reload` *becomes* `sync_all`, but keep **both**: `reload` is the hard cold-reset
(new `ProjectDatabase`, new discovery — picks up config-file edits on disk),
`sync_all` is the warm in-place rescan. They are different tools; do not collapse
them. The existing Python suite calls `reload` — leave its name and behaviour
intact (regression guard).

---

## 7. The §0 hazards you MUST respect in Phase 3

Phase 3 mutates the **HEAD** db in place. Per architecture §0, this interacts with
salsa's `cancel_others` (`apply_changes` blocks until it is the only live handle
on the shared `Zalsa`). Two consequences you must handle now and cannot defer:

### 7.1 A live `PySnapshot` will BLOCK the writer

`snapshot()` (unchanged in Phase 3) clones the HEAD `ProjectDatabase` — which
**shares the HEAD `Zalsa`**. If such a snapshot is alive when you call
`apply_changes`, `cancel_others` waits for `clones == 1` forever → the edit
**hangs**. This is exactly the bug independent per-revision snapshots fix in
**Phase 4**.

For Phase 3 this means: **do not hold a snapshot across an edit.** Document it on
the write methods, and in tests always `close()` the snapshot (or let it drop)
before editing. Do **not** try to fix `snapshot()` here — Phase 4 owns that.

### 7.2 Concurrent HEAD reads can be cancelled

A read (`check`, etc.) clones the db under the lock, drops the lock, and runs
detached (GIL released). If another thread calls `edit` during that window,
`apply_changes` sets the cancellation flag and the in-flight read query may unwind
with `salsa::Cancelled`. In **single-threaded** edit-then-read sequences (all
Phase 3 tests) this never happens — there is no concurrent read. Robust concurrent
HEAD reads get the cancel-retry wrapper in **Phase 9**; true never-cancelled reads
come from **Phase 4** snapshots. For Phase 3, state the limitation and keep tests
sequential.

### 7.3 Why writes hold the GIL

We deliberately do **not** wrap `apply_changes` in `py.detach` in Phase 3. Edits
are warm and incremental (cheap), and holding the GIL keeps the write path
trivially correct (no `&mut` borrow crossing the detach boundary, no extra Send
juggling). The "release the GIL during heavy work" pattern stays on the *read*
path. Revisit only if benchmarks (Phase 10) show writes starving other threads.

---

## 8. Tests

### 8.1 Rust unit tests (`project.rs` `#[cfg(test)]`)

These prove the engine without the Python layer. Reuse the `tempfile`
dev-dependency. (You can build a `HeadState` directly via `build_head`, mutate its
store/system/db, and assert through `source_text` / `db.check()`.)

```rust
#[cfg(test)]
mod phase3_tests {
    use super::*;
    use ruff_db::source::source_text;
    use std::io::Write;

    fn project(a_py: &str) -> (tempfile::TempDir, SystemPathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let mut f = std::fs::File::create(dir.path().join("a.py")).unwrap();
        f.write_all(a_py.as_bytes()).unwrap();
        let root = SystemPathBuf::from_path_buf(dir.path().canonicalize().unwrap()).unwrap();
        (dir, root)
    }

    /// An overlay edit changes what the db reads — without touching disk.
    #[test]
    fn edit_overlays_content_without_disk_write() {
        let (_dir, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new());
        let a = root.join("a.py");

        // baseline reads disk
        let f = ruff_db::files::system_path_to_file(&head.db, &a).unwrap();
        assert!(source_text(&head.db, f).as_str().contains("X = 1"));

        // overlay edit
        head.store.insert_text(a.clone(), "Y = 2\n");
        head.system.publish(head.store.capture());
        let ev = ChangeEvent::file_content_changed(a.clone());
        head.db.apply_changes(std::slice::from_ref(&ev), None);

        let f2 = ruff_db::files::system_path_to_file(&head.db, &a).unwrap();
        assert!(source_text(&head.db, f2).as_str().contains("Y = 2"));
        // disk is untouched
        assert_eq!(std::fs::read_to_string(_dir.path().join("a.py")).unwrap(), "X = 1\n");
    }

    /// The application revision advances on each edit.
    #[test]
    fn revision_advances() {
        let (_dir, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new());
        let r0 = head.store.revision().0;
        head.store.insert_text(root.join("a.py"), "X = 2\n");
        assert!(head.store.revision().0 > r0);
    }

    /// A created (previously-absent) file is classified Created and becomes visible.
    #[test]
    fn edit_creates_new_file() {
        let (_dir, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new());
        let b = root.join("b.py");
        let ev = classify_overlay_edit(&head.db, &b);
        assert!(matches!(ev, ChangeEvent::Created { .. }));
        head.store.insert_text(b.clone(), "Z = 3\n");
        head.system.publish(head.store.capture());
        head.db.apply_changes(std::slice::from_ref(&ev), None);
        let f = ruff_db::files::system_path_to_file(&head.db, &b).unwrap();
        assert!(source_text(&head.db, f).as_str().contains("Z = 3"));
    }

    /// A virtual buffer is analysed without ever creating a disk file.
    #[test]
    fn edit_virtual_is_disk_free() {
        let (_dir, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new());
        let vpath = ruff_db::system::SystemVirtualPathBuf::from("untitled:1");
        head.store.insert_virtual(vpath.clone(), "VV = 9\n");
        head.system.publish(head.store.capture());
        head.db.apply_changes(&[ChangeEvent::CreatedVirtual(vpath.clone())], None);
        let vf = head.db.files().virtual_file(&head.db, &vpath);
        assert!(source_text(&head.db, vf.file()).as_str().contains("VV = 9"));
    }

    /// sync_path on a disk edit re-reads disk after forgetting the overlay.
    #[test]
    fn sync_path_reingests_disk() {
        let (_dir, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new());
        let a = root.join("a.py");
        // overlay it, then change disk underneath, then sync_path should win = disk
        head.store.insert_text(a.clone(), "OVERLAY = 1\n");
        head.system.publish(head.store.capture());
        std::fs::write(_dir.path().join("a.py"), "DISK = 2\n").unwrap();

        head.store.forget(&a);
        head.system.publish(head.store.capture());
        let ev = classify_disk_sync(&head.system, &head.db, &a);
        head.db.apply_changes(std::slice::from_ref(&ev), None);

        let f = ruff_db::files::system_path_to_file(&head.db, &a).unwrap();
        assert!(source_text(&head.db, f).as_str().contains("DISK = 2"));
    }
}
```

> Confirm `VirtualFile::file()` (the accessor from `virtual_file(...)` to a `File`)
> against `ruff_db/src/files.rs:163` — the method name may be `.file()` or the
> `VirtualFile` may `Deref`/expose a `File` differently. Adjust the one line.

### 8.2 Python end-to-end tests

Add `src/tyo3/tests/test_write_path.py`. Match the existing import style /
`check()` return shape used by the suite (e.g. `test_rust_integration.py`).

```python
def test_edit_changes_diagnostics_without_disk_write(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x: int = 1\n")
    proj = TyProject.open(str(tmp_path))
    try:
        before = proj.check()
        r = proj.edit("a.py", "x: int = 'not an int'\n")   # type error now
        assert r["revision"] > 0
        assert "a.py" in r["changed"][0]
        after = proj.check()
        # the overlay edit introduced a diagnostic the original didn't have
        assert _num_diags(after) > _num_diags(before)
        # disk is untouched
        assert p.read_text() == "x: int = 1\n"
    finally:
        proj.close()


def test_head_revision_advances(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    proj = TyProject.open(str(tmp_path))
    try:
        r0 = proj.head
        proj.edit("a.py", "x = 2\n")
        assert proj.head > r0
    finally:
        proj.close()


def test_edit_virtual_is_analyzable(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    proj = TyProject.open(str(tmp_path))
    try:
        r = proj.edit_virtual("untitled:1", "y: int = 'bad'\n")
        assert r["revision"] > 0
        # no file was written to disk
        assert not (tmp_path / "untitled:1").exists()
    finally:
        proj.close()


def test_snapshot_must_be_closed_before_edit(tmp_path):
    """Phase 3 contract: a live snapshot shares the HEAD Zalsa and blocks the
    writer (architecture §0). Close it before editing. (Phase 4 removes this.)"""
    (tmp_path / "a.py").write_text("x = 1\n")
    proj = TyProject.open(str(tmp_path))
    try:
        snap = proj.snapshot()
        snap.check()
        snap.close()              # <- MUST close before edit, or edit() hangs
        proj.edit("a.py", "x = 2\n")
    finally:
        proj.close()
```

Shape `_num_diags` / the diagnostics access to the suite's existing convention.
The load-bearing assertion is **`check()` after `edit()` differs from before, and
disk is unchanged** — that is the proof that overlay edits drive analysis.

---

## 9. Build, test, iterate

```bash
devenv shell -- check-rust     # type/borrow check while iterating
devenv shell -- test-rust      # Phase 1 + 2 + 3 Rust tests
devenv shell -- tests          # FULL Python suite — the regression guard
```

The full Python suite passing unchanged proves the write path is additive: reads,
`reload`, `snapshot`, and `close` behave exactly as before.

---

## 10. Gotchas & decisions (read before you debug)

1. **Publish before apply (§0/§3.1 in this guide).** The single most common bug:
   editing the store but reading stale content because `apply_changes` ran before
   `publish`. Order is store → publish → apply.

2. **Same `SystemPathBuf` keys the store and the event.** `resolve_sync_path`'s
   output must be used verbatim as both the overlay key and the `ChangeEvent` path.
   If you canonicalise for one and not the other, `apply_changes`'s overlay lookup
   misses and the file looks empty/unchanged.

3. **Classify *before* mutating, for overlay edits.** `classify_overlay_edit` asks
   "does this file already exist?" — call it before `insert_text`, otherwise the
   answer is unaffected anyway (the store insert doesn't intern a `File`), but the
   ordering keeps the intent obvious. For `sync_path` you must `forget` *then*
   classify (you want disk truth).

4. **A live `PySnapshot` hangs `apply_changes`.** Architecture §0. Tests must close
   snapshots before editing. Not fixable in Phase 3 — Phase 4.

5. **Don't detach writes.** `apply_changes` takes `&mut db`; keep it on the GIL
   (§7.3). Detaching buys little and complicates borrows.

6. **`edit_many` consumes N revision numbers but publishes once.** Intended (§2.3).
   `SyncResult.revision` is the final one.

7. **`SystemVirtualPathBuf::from(uri)`** — confirm the constructor
   (`From<&str>` / `from`) against `ruff_db::system`. Virtual URIs like
   `"untitled:1"` are passed through verbatim; we don't validate scheme.

8. **`try_system` / `try_virtual_file` live on `db.files()`**, not on `db`
   directly (`ruff_db/src/files.rs:121,181`). `db.files()` returns `&Files`.

9. **`reload` stays a full rebuild.** Do not merge it into `sync_all` (§6.2).

10. **Remove the Phase-2 `#[allow(dead_code)]` on `HeadState.system`.** It is now
    read by every write method; the allow will itself warn as unnecessary.

---

## 11. Explicitly OUT of scope for Phase 3

- Independent, never-cancelled MVCC snapshots (`OverlaySystem::frozen` wired into
  `snapshot()`); collapsing the duplicated `PyTyProject`/`PySnapshot` read walls;
  deleting the eager-materialisation hack in `snapshot()` — **Phase 4**.
- The concurrency stress proof (N readers + hot writer) — **Phase 5**.
- The graph (`apply_delta`, reverse-dep index, `Snapshot.graph()`) — **Phases 6–7**.
  `SyncResult` carries the delta the graph will later consume; don't wire the graph
  now.
- `save(path)` (flush an overlay buffer to disk) — needs `WritableSystem`
  write-through (`as_writable` still returns `None`). Defer; `discard` (drop the
  overlay) is enough for Phase 3.
- Notebook *overlay* content — still delegated to disk (Phase 1 §8).
- The file watcher as a change source — **Phase 8**.
- The floating cancel-retry `check()` fast path — **Phase 9**.

If you find yourself editing `snapshot()`, building a `frozen` system, touching
`src/tyo3/graph/`, or implementing `WritableSystem`, stop — you've left Phase 3.

---

## 12. Definition of Done

- [ ] `content.rs`: `ContentMap { system, virtual_files }`; `Generation =
      Arc<ContentMap>`; `ContentStore` gains `insert_virtual` / `forget_virtual` /
      `bump_revision`; system-path methods preserved (Phase 1 tests still green).
- [ ] `overlay.rs`: `SharedContent = Arc<ArcSwap<ContentMap>>`; `document` reads
      `.system`; `virtual_document` reads `.virtual_files`;
      `read_virtual_path_to_string` + `virtual_path_source_type` serve overlay
      first; empty-generation call sites updated to `ContentMap::new()`.
- [ ] `dto/sync.rs`: `SyncResultDto` added and re-exported.
- [ ] `project.rs`: `resolve_sync_path`, `classify_overlay_edit`,
      `classify_disk_sync`, `commit_head`/`sync_path_inner` helpers; `#[pymethods]`
      `edit`, `edit_many`, `edit_virtual`, `sync_path`, `discard`, `sync_all`, and
      the `head` getter; Phase-2 `#[allow(dead_code)]` on `system` removed.
- [ ] Rust tests pass: overlay-edit-without-disk, revision-advances,
      create-new-file, virtual-is-disk-free, sync_path-reingests-disk.
- [ ] Python tests pass: edit-changes-diagnostics, head-advances,
      edit_virtual-analyzable, snapshot-closed-before-edit.
- [ ] `devenv shell -- tests` shows **no regressions** in the existing suite
      (`reload` / `snapshot` / `close` / all reads unchanged).
- [ ] `devenv shell -- check-rust` clean.
- [ ] PR description notes any signature deviations (esp. `SystemVirtualPathBuf`
      construction, `VirtualFile`→`File` accessor, `HashTrieMapSync::Default`).

---

## 13. How this seeds Phase 4

You now have a mutable head that produces revisions and deltas:

- **Phase 4** builds the real MVCC read surface. `snapshot(at)` will
  `head.store.capture()` (O(1)) and construct an **independent** `ProjectDatabase`
  over `OverlaySystem::frozen(root, generation, rev)` — its own `Zalsa`, so it can
  never be cancelled by a HEAD `apply_changes` and never blocks one (resolving the
  §7.1 hazard). The eager-materialisation hack in today's `snapshot()` and the
  duplicated read walls collapse into that one frozen read view.
- **Phase 6** consumes `SyncResult` (the `created`/`changed`/`deleted` path lists,
  and `rescan`) to drive the incremental graph, re-resolving those paths to `File`s
  against the snapshot it operates on.

Keep the write path's ordering (store → publish → apply) and the §0 discipline
clean, and Phase 4 drops straight onto `head.store.capture()` +
`OverlaySystem::frozen`.
