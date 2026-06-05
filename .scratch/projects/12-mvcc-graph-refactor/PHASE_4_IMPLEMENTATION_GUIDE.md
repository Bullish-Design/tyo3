# Phase 4 Implementation Guide — Snapshots: the independent MVCC read surface

> Audience: an engineer who has finished **Phase 1**
> (`PHASE_1_IMPLEMENTATION_GUIDE.md`, `ContentStore` + `OverlaySystem`), **Phase 2**
> (`PHASE_2_IMPLEMENTATION_GUIDE.md`, the HEAD db built over the overlay via real
> discovery), and **Phase 3** (`PHASE_3_IMPLEMENTATION_GUIDE.md`, the write path:
> `edit` / `edit_virtual` / `sync_path` / `sync_all` → store mutation → publish →
> `apply_changes` → `Revision` + `SyncResult`), and is now implementing **Phase 4**
> of `MVCC_SUBSTRATE_ARCHITECTURE.md`.
>
> Goal of Phase 4: build the **real MVCC read surface**. Today `snapshot()` clones
> the HEAD `ProjectDatabase` — which **shares the HEAD `Zalsa`** — and works around
> snapshot isolation by eagerly materialising every file's `source_text`
> (`project.rs:871-889`). That clone is the bug behind architecture §0 / Phase 3
> §7.1: a live snapshot **blocks the writer forever**. Phase 4 replaces it with an
> **independent per-revision database**: `snapshot(at)` captures the pinned
> `Generation` (O(1) `Arc` clone) and builds a *fresh* `ProjectDatabase` over
> `OverlaySystem::frozen(root, generation, rev)` — its own `Zalsa`, so it can never
> be cancelled by a HEAD `apply_changes` and never blocks one.
>
> Three things land together because they are the same change:
>
> 1. **Independent snapshot databases** (own `Zalsa`) replacing the shared clone.
> 2. **Read-once disk capture** in the frozen overlay, which makes snapshot
>    isolation *structural* and lets us **delete the eager-materialisation hack**.
> 3. **Collapsing the two duplicated read walls into one.** Today `PyTyProject` and
>    `PySnapshot` carry ~24 identical read methods each (`project.rs:894-1290` vs
>    `1310-1697`). Phase 4 keeps the wall on `PySnapshot` only and makes
>    `TyO3Session` reads sugar over a cached **head snapshot**.
>
> **Still out of scope:** the concurrency *stress proof* (N readers + hot writer —
> **Phase 5**; Phase 4 proves single-threaded isolation + builds the mechanism it
> relies on); the graph (`apply_delta`, `Snapshot.graph()` — **Phases 6–7**); the
> file watcher (**Phase 8**); the floating cancel-retry warm `session.check()` fast
> path (**Phase 9**).
>
> When you finish: the project compiles, the **entire existing Python suite still
> passes** (snapshots are now *more* correct, not different in observable result),
> a held snapshot no longer blocks an edit, a snapshot pinned at R keeps reading R
> after many HEAD edits, `snapshot(at=r)` time-travels to a retained revision, and
> the eager-materialisation loop is gone.

---

## 0. Mental model (read this first)

### 0.1 Why the current snapshot is broken (the §0 constraint, concretely)

`snapshot()` today does `clone_locked_state(...)` → `TyProjectState { db: head.db.clone(), root }`.
A `ProjectDatabase::clone()` **shares the same `Arc<Zalsa>` and the same clone
counter** (architecture §0, vendored `salsa-0.26.2/src/storage.rs:25-35`). So:

- The cloned snapshot db is *not* isolated: salsa is lazy, so the snapshot's first
  read of an un-memoised file hits **live disk** and can see edits made *after* the
  snapshot was taken. The eager `for f in project.files(): source_text(f)` loop
  (`project.rs:882-885`) pre-warms every memo to paper over this — O(files) per
  snapshot, and it cannot represent virtual buffers or pin disk files that get
  created later.
- Worse: while that clone is alive, `head.db.apply_changes` (Phase 3's `edit`)
  calls `cancel_others`, which **blocks until `clones == 1`** — i.e. forever, until
  the snapshot drops. That is Phase 3 §7.1's hazard and the reason
  `test_snapshot_must_be_closed_before_edit` exists.

### 0.2 The fix: one `Zalsa` per revision

Phase 4 makes a snapshot a **brand-new `ProjectDatabase`** built over a *frozen*
`OverlaySystem`. A fresh `ProjectDatabase` has its **own `Zalsa`** (its own clone
counter). Therefore:

- HEAD's `apply_changes` sees `clones == 1` on **HEAD's** `Zalsa` (the snapshot is
  not a clone of it) → **never blocks**.
- The snapshot's `Zalsa` is never touched by a `&mut` mutation (a snapshot is
  read-only; we never call `apply_changes` on it) → its in-flight reads are
  **never cancelled**.

This is the entire payoff of paying for per-revision storage (architecture §0, §7).

### 0.3 The content is pinned by the held `Generation`, not by a memo

A snapshot pins revision R by holding an `Arc<ContentMap>` (the `Generation`
captured from the store at R). Overlaid files (agent edits, virtual buffers,
deletions) are *in* that generation, so they are frozen by construction. The only
remaining leak is **disk-backed files not yet in the generation**: the frozen
overlay must not read them live on every access, or two reads of the same file
could straddle a disk change. Phase 4 closes this with **read-once disk capture**
(§2): on a miss, the frozen overlay reads disk *once*, interns the result into its
own content cell, and serves that forever. Combined with the substrate being the
system of record (all real disk changes flow through `sync_path` → a *new*
revision → a *new* generation; the snapshot keeps the old one), the snapshot
**never races live disk**. This is the structural version of what the eager loop
did by hand — so the eager loop is deleted (architecture §2.2, §10 phase 4).

### 0.4 What ty / the existing code gives us (do not reimplement)

- **`OverlaySystem::frozen(root, generation, rev)`** already exists
  (`overlay.rs:54-64`) and sets `frozen = Some(rev)`. Phase 4 finally *uses* it,
  and teaches the frozen branch to capture-on-miss instead of falling through to
  live disk.
- **`build_head`** (Phase 2) is the construction blueprint:
  `ProjectMetadata::discover` → `apply_configuration_files` → `ProjectDatabase::fallible`
  with a `use_defaults` fallback. `build_frozen` (§4) is the same shape over the
  frozen system, minus the `ContentStore`.
- **All `compute_*` cores** (`project.rs:175-790`) already take `&TyProjectState`
  and are the *single* analysis implementation. Collapsing the read walls is purely
  about which `#[pyclass]` exposes the thin wrappers — the cores never duplicate.
- **The Python `_ReadOps` mixin** (`session.py:85`) is already shared between
  `TyO3Session` and `Snapshot`; Phase 4 only changes *which native handle* it
  dispatches to for a session.

---

## 1. Prerequisite check

Phase 4 assumes Phases 1–3 are merged/working. Run the baseline first (always via
devenv — see `MEMORY.md`, never bare cargo/pytest):

```bash
devenv shell -- check-rust
devenv shell -- test-rust
devenv shell -- tests
```

You rely on, and will extend:

- `crate::content::{ContentStore, ContentMap, Generation, Revision, Document}`
  (`content.rs`) — Phase 1/3. You will add **revision retention** (§3).
- `crate::overlay::OverlaySystem` with `live` / `frozen` / `publish` and the
  `frozen: Option<Revision>` discriminator (`overlay.rs`) — Phase 1. You will add
  **read-once disk capture** to the frozen branch (§2).
- `build_head` (`project.rs`) — Phase 2. You will add a sibling `build_frozen` (§4).
- `HeadState { db, root, store, system }`, `TyProjectState { db, root }`,
  `ReadCloneSource`, `clone_locked_state`, the `compute_*` cores, `PyTyProject`,
  `PySnapshot` — Phases 2/3.
- The Phase-3 write methods + `head` getter on `PyTyProject`, and (Phase 3 Python
  layer) the `edit` / `sync_*` wrappers on `TyO3Session`.

If Phase 3's `test_snapshot_must_be_closed_before_edit` is present, Phase 4
**removes the constraint it documents** (independent snapshots no longer block the
writer). Convert it into a test that asserts the *opposite* (§8.2).

---

## 2. Read-once disk capture in the frozen overlay (`rust/src/overlay.rs`)

This is the meatiest part and the one that makes deleting eager materialisation
safe. Today the three content-serving methods fall through to **live** `native`
disk on a miss, *regardless of `frozen`* (`overlay.rs:91-101`, `107-113`,
`136-145`). For a frozen overlay that is the isolation hole. Replace the
frozen-miss behaviour with **capture-once-then-serve**, reusing the existing
`content` cell (an `ArcSwap<ContentMap>`) as the capture store via an additive,
race-safe `rcu`.

### 2.1 Add a capture-version counter to `OverlaySystem`

Captured disk files need a stable `FileRevision` so `path_metadata` and
`read_to_string` agree (salsa reads both and must see one consistent revision per
file). Assign each captured file a monotonic version from a counter on the system.

```rust
use std::sync::atomic::{AtomicU64, Ordering};

#[derive(Debug, Clone)]
pub struct OverlaySystem {
    content: SharedContent,
    native: Arc<dyn System + Send + Sync + RefUnwindSafe>,
    frozen: Option<Revision>,
    /// Monotonic version source for disk files captured lazily by a *frozen*
    /// overlay (read-once capture, §2.2). Shared across clones of this system so
    /// every `db.clone()` of one snapshot observes the same captured content.
    /// Unused on a live head (which floats to disk and never captures). Starts
    /// high to avoid colliding with store `Document` versions in debug output.
    capture_version: Arc<AtomicU64>,
}
```

Initialise it in **both** constructors (`live` and `frozen`):

```rust
capture_version: Arc::new(AtomicU64::new(1 << 32)),
```

> `Arc<AtomicU64>` (not a bare `AtomicU64`) because `OverlaySystem: Clone` and every
> `db.clone()` shares the same content cell — the capture counter must be shared
> too, or two clones of one snapshot could assign different versions to the same
> path and trip salsa's revision check. (On a live head this field is inert.)

### 2.2 The capture primitive

Add a private method that reads a disk file exactly once and interns it into the
frozen content cell, idempotently and race-safely:

```rust
impl OverlaySystem {
    /// Frozen read-once capture. If `path` is already in the content cell (overlaid
    /// or previously captured), return it. Otherwise read disk *once*, intern the
    /// result additively, and return it. Idempotent: concurrent callers converge on
    /// a single captured `Document` (last CAS wins; content is identical).
    ///
    /// Only meaningful when `self.frozen.is_some()`. The live head never calls this.
    fn capture_disk_file(&self, path: &SystemPath) -> std::io::Result<Document> {
        // Fast path: already overlaid or captured.
        if let Some(doc) = self.document(path) {
            return Ok(doc);
        }
        // Read disk exactly once.
        let text = self.native.read_to_string(path)?;
        let version = self.capture_version.fetch_add(1, Ordering::Relaxed);
        let doc = Document::Text { text: text.into(), version };

        // Additive, race-safe intern. Do NOT clobber a concurrent capture of the
        // same path: if another thread already interned it, keep theirs.
        let key = path.to_path_buf();
        self.content.rcu(|cur| {
            if cur.system.contains_key(&key) {
                Arc::clone(cur)
            } else {
                let mut next = (**cur).clone();              // 2 Arc bumps, structural
                next.system = next.system.insert(key.clone(), doc.clone());
                Arc::new(next)
            }
        });

        // Return whatever is now stored (the race winner), falling back to ours.
        Ok(self.document(path).unwrap_or(doc))
    }
}
```

Notes:

- **`ArcSwap::rcu`** runs the closure, CASes the new value, and retries the closure
  if another writer raced — so the `contains_key` guard inside is re-evaluated on
  each attempt and the capture is exactly-once-observable. Confirm `rcu`'s exact
  signature against the `arc-swap` version in `Cargo.toml` (it takes
  `FnMut(&Arc<T>) -> Arc<T>` and returns the previous `Arc`).
- **This does not violate "frozen = immutable".** The *revision* and the *overlaid*
  content of a frozen snapshot never change; capture only *fills in* disk files the
  snapshot was always going to read, pinning them at first read. The
  `publish()`-guard (`debug_assert!(self.frozen.is_none())`, `overlay.rs:68-73`)
  stays — capture is a different operation (additive, allowed on frozen; never used
  on live).
- **Capture is content-only for files.** Directories and absent paths are handled
  in `path_metadata` (§2.3) and never interned as `Document::Text`.

### 2.3 Branch the three content methods on `frozen`

Update `read_to_string`, `path_metadata`, and `source_type` so a **frozen** miss
captures, while a **live** miss keeps floating to disk (the head is supposed to see
latest).

```rust
fn read_to_string(&self, path: &SystemPath) -> std::io::Result<String> {
    match self.document(path) {
        Some(Document::Text { text, .. }) => Ok(text.to_string()),
        Some(Document::Deleted { .. }) => Err(not_found(path)),
        None if self.frozen.is_some() => match self.capture_disk_file(path)? {
            Document::Text { text, .. } => Ok(text.to_string()),
            Document::Deleted { .. } => Err(not_found(path)),
        },
        None => self.native.read_to_string(path),     // live head: float to disk
    }
}

fn path_metadata(&self, path: &SystemPath) -> std::io::Result<Metadata> {
    match self.document(path) {
        Some(Document::Text { version, .. }) => Ok(Metadata::new(
            FileRevision::new(u128::from(version)),
            None,
            FileType::File,
        )),
        Some(Document::Deleted { .. }) => Err(not_found(path)),
        None if self.frozen.is_some() => {
            // Pin structure via the native metadata, but for FILES derive the
            // revision from the captured content so it matches read_to_string.
            let meta = self.native.path_metadata(path)?;
            if meta.file_type().is_file() {
                let doc = self.capture_disk_file(path)?;
                Ok(Metadata::new(
                    FileRevision::new(u128::from(doc.version())),
                    meta.permissions(),
                    FileType::File,
                ))
            } else {
                // Directories/symlinks: structure floats to live disk (documented
                // caveat §10.4). Don't capture as text.
                Ok(meta)
            }
        }
        None => self.native.path_metadata(path),
    }
}

fn source_type(&self, path: &SystemPath) -> Option<PySourceType> {
    match self.document(path) {
        Some(Document::Text { .. }) => path
            .extension()
            .and_then(PySourceType::try_from_extension)
            .or(Some(PySourceType::Python)),
        Some(Document::Deleted { .. }) => None,
        None if self.frozen.is_some() => {
            // Capture so the type is decided against pinned content, then classify.
            match self.capture_disk_file(path) {
                Ok(Document::Text { .. }) => path
                    .extension()
                    .and_then(PySourceType::try_from_extension)
                    .or(Some(PySourceType::Python)),
                _ => None,
            }
        }
        None => self.native.source_type(path),
    }
}
```

> Confirm `Metadata::new` / `Metadata::file_type` / `Metadata::permissions` /
> `FileType::is_file` accessor names against `ruff_db::system::{Metadata, FileType}`
> at rev `3cb09eb` and adjust. `Document::version()` already exists
> (`content.rs:31`).

### 2.4 Directory walking and virtual paths in frozen mode

Leave `read_directory` / `walk_directory` delegating to `native` (live disk). The
*set* of files a frozen snapshot enumerates therefore reflects live disk at the
moment it walks — but **content** of each enumerated file is pinned by capture, and
overlaid created/deleted files are reflected via the generation. Under the "store
is system of record" invariant (architecture §2.2) the disk file-set does not drift
relative to R, because every real change flows through `sync_path` → a new
revision. This is the one honest seam; document it (§10.4), do not try to snapshot
the directory tree in Phase 4.

Virtual-path reads already serve the generation's `virtual_files` map first
(Phase 3); they are frozen-correct for free (overlaid virtual buffers are *in* the
captured generation; there is no disk to capture). Leave them as Phase 3 left them.

---

## 3. Revision retention for time-travel (`rust/src/content.rs`)

`snapshot(at=None)` pins the current head generation — trivial (`store.capture()`).
`snapshot(at=r)` must hand back the generation *as it was at revision r*. The store
currently keeps only the live `generation` and drops superseded ones. Add a bounded
retention buffer so recent revisions can be time-travelled to; older ones are
evicted and requesting them is an explicit error.

```rust
use std::collections::BTreeMap;

pub struct ContentStore {
    generation: Generation,
    revision: Revision,
    version_counter: u64,
    /// Recent (revision → generation) for `snapshot(at=r)` time-travel. Bounded;
    /// the oldest entries are evicted past `retain_cap`. Holds `Arc<ContentMap>`s,
    /// so a retained generation is cheap (shared structure) until it is the *only*
    /// holder and gets evicted. Independent of snapshot liveness: a live snapshot
    /// pins its own generation regardless of what the store retains.
    retained: BTreeMap<Revision, Generation>,
    retain_cap: usize,
}
```

Wire retention into construction and every revision bump:

```rust
const DEFAULT_RETAIN_CAP: usize = 256;

impl ContentStore {
    pub fn new() -> Self {
        let generation: Generation = Arc::new(ContentMap::new());
        let mut retained = BTreeMap::new();
        retained.insert(Revision(0), Arc::clone(&generation));
        Self {
            generation,
            revision: Revision(0),
            version_counter: 0,
            retained,
            retain_cap: DEFAULT_RETAIN_CAP,
        }
    }

    /// Record the current (revision, generation) and evict the oldest beyond cap.
    fn record_retained(&mut self) {
        self.retained.insert(self.revision, Arc::clone(&self.generation));
        while self.retained.len() > self.retain_cap {
            // BTreeMap is ordered by Revision; pop the smallest key.
            let oldest = *self.retained.keys().next().expect("non-empty");
            self.retained.remove(&oldest);
        }
    }

    /// The generation pinned at `rev`, if still retained. `None` ⇒ evicted (caller
    /// raises). The *current* revision is always retained.
    pub fn generation_at(&self, rev: Revision) -> Option<Generation> {
        self.retained.get(&rev).cloned()
    }

    pub fn oldest_retained(&self) -> Revision {
        *self.retained.keys().next().unwrap_or(&self.revision)
    }
}
```

Then call `record_retained()` at the **end** of `mutate` and `bump_revision`
(after `self.revision` is advanced and `self.generation` is set):

```rust
fn mutate(&mut self, f: impl FnOnce(&mut ContentMap)) -> Revision {
    let mut map = (*self.generation).clone();
    f(&mut map);
    self.generation = Arc::new(map);
    self.revision = Revision(self.revision.0 + 1);
    self.record_retained();            // ← add
    self.revision
}

pub fn bump_revision(&mut self) -> Revision {
    self.revision = Revision(self.revision.0 + 1);
    self.record_retained();            // ← add  (retains the same generation under a new rev)
    self.revision
}
```

> Retention bounds memory the way architecture §9 asks ("retained-revision cap").
> `retain_cap` is a constant for Phase 4; surfacing it as a session option is a
> trivial later addition, not needed now. Note that `bump_revision` (rescan) re-maps
> the *same* generation under a new revision — correct: a rescan changes no overlay
> content, so `snapshot(at=that_rev)` and `snapshot(at=prev_rev)` pin identical
> content but build dbs that re-walk disk independently.

---

## 4. `build_frozen` — the independent snapshot database (`rust/src/project.rs`)

A sibling of `build_head` (Phase 2 §3) that constructs a **fresh** `ProjectDatabase`
over a **frozen** overlay. It owns no `ContentStore` and never publishes.

```rust
/// Build an independent, revision-pinned `ProjectDatabase` over a frozen overlay.
///
/// Construction mirrors `build_head` (discover → apply user config → fallible,
/// with a `use_defaults` fallback) but over `OverlaySystem::frozen(...)`, so the
/// resulting db has its OWN `Zalsa`: it can never be cancelled by a HEAD
/// `apply_changes`, and a HEAD `apply_changes` never blocks on it (architecture §0).
///
/// `generation` is the content pinned at `rev` (captured from the store, O(1)).
/// Disk files not in `generation` are captured read-once by the frozen overlay
/// (overlay.rs §2), so the snapshot never races live disk.
fn build_frozen(root: SystemPathBuf, generation: Generation, rev: Revision) -> TyProjectState {
    let system = OverlaySystem::frozen(root.clone(), generation, rev);

    let built: Result<ProjectDatabase, String> = ProjectMetadata::discover(&root, &system)
        .map_err(|e| format!("project discovery failed: {e}"))
        .and_then(|mut metadata| {
            metadata
                .apply_configuration_files(&system)
                .map_err(|e| format!("failed to apply configuration files: {e}"))?;
            ProjectDatabase::fallible(metadata, system.clone())
                .map_err(|e| format!("failed to build snapshot database: {e:#}"))
        });

    let db = match built {
        Ok(db) => db,
        Err(err) => {
            tracing::warn!("{err}. Falling back to default project settings for snapshot.");
            let metadata =
                ProjectMetadata::new(ruff_python_ast::name::Name::new("tyo3-project"), root.clone());
            ProjectDatabase::use_defaults(metadata, system)
        }
    };

    TyProjectState { db, root }
}
```

Notes:

- **Returns a `TyProjectState`**, the exact type `PySnapshot` reads dispatch on and
  the `compute_*` cores consume. No new read plumbing needed.
- **Discovery runs through the frozen overlay**, so `pyproject.toml` / `ty.toml` is
  read (and capture-pinned) at R: if an agent overlaid a hypothetical config, the
  snapshot honours it. Config is therefore pinned consistently with content.
- **Re-discovery vs. metadata reuse (optimisation, optional).** `build_frozen`
  re-runs discovery per snapshot, which is the bulk of the cold cost (§9). If the
  head's `ProjectMetadata` is cheaply cloneable and reachable, you may reuse it and
  call `ProjectDatabase::fallible(metadata.clone(), system)` directly, skipping
  `discover`/`apply_configuration_files`. Do this only if it is clean against rev
  `3cb09eb`; otherwise keep re-discovery. The cached head snapshot (§6) amortises
  the cost across a revision regardless, so this is not load-bearing for Phase 4.

---

## 5. `snapshot(at)` + `PySnapshot` (collapse the read walls)

### 5.1 Rewrite `PyTyProject::snapshot` and delete the eager loop

Replace the whole of `snapshot()` (`project.rs:866-889`):

```rust
/// Pin a revision-isolated MVCC snapshot. `at=None` pins the current head
/// revision; `at=r` time-travels to a still-retained revision (else an error).
///
/// The returned snapshot owns an INDEPENDENT `ProjectDatabase` (its own `Zalsa`),
/// so holding it across HEAD edits neither blocks the writer nor risks
/// cancellation (architecture §0). No eager materialisation: content is pinned by
/// the captured `Generation` + read-once disk capture (overlay.rs §2).
#[pyo3(signature = (at=None))]
fn snapshot(&self, at: Option<u64>) -> PyResult<PySnapshot> {
    let guard = lock_state(&self.inner, "snapshot")?;
    let head = guard.as_ref().unwrap();
    let root = head.root.clone();

    let (generation, rev) = match at {
        None => (head.store.capture(), head.store.revision()),
        Some(r) => {
            let rev = Revision(r);
            let gen = head.store.generation_at(rev).ok_or_else(|| {
                PyValueError::new_err(format!(
                    "revision {} is no longer retained (oldest retained: {})",
                    r,
                    head.store.oldest_retained().0
                ))
            })?;
            (gen, rev)
        }
    };
    drop(guard); // release the head lock BEFORE the (cold) db build — never hold it across discovery

    let state = build_frozen(root, generation, rev);
    Ok(PySnapshot {
        inner: Mutex::new(Some(state)),
        revision: rev.0,
    })
}
```

- **Drop the head lock before `build_frozen`.** Discovery + db construction is the
  cold cost; doing it under the head `Mutex` would serialise it against the writer
  for no reason. We only need the lock to capture the generation (O(1)).
- **`PyValueError`** for an evicted revision — confirm it is imported in
  `project.rs` (`pyo3::exceptions::PyValueError`); add the import if missing.
- **`source_text` import** (`project.rs:12`) becomes unused once the eager loop is
  deleted, unless other code uses it — check and remove the import if it now warns.

### 5.2 `PySnapshot` gains a revision and stays the single read wall

`PySnapshot` keeps its ~24 read methods (`project.rs:1306-1697`) — they are now the
**only** read wall. Add an immutable `revision` field and a getter:

```rust
#[pyclass(name = "TySnapshot", module = "tyo3._native_impl", frozen)]
pub struct PySnapshot {
    inner: Mutex<Option<TyProjectState>>,
    /// The application revision this snapshot is pinned to (immutable).
    revision: u64,
}

#[pymethods]
impl PySnapshot {
    /// The revision this snapshot is pinned to.
    #[getter]
    fn revision(&self) -> u64 {
        self.revision
    }

    // ... existing files/check/.../hover read methods unchanged ...
    // ... existing close() unchanged ...
}
```

The read methods need **no change**: they already `clone_locked_state(&self.inner, ...)`
→ clone the (now frozen, independent) `db` and run detached. Cloning the frozen db
shares the *frozen* `Zalsa` among the snapshot's own read-clones; since reads never
mutate, `cancel_others` never fires within a snapshot.

### 5.3 Remove the duplicated read wall from `PyTyProject`

Delete **all ~24 read methods** from `impl PyTyProject` (`project.rs:894-1290`):
`files`, `check`, `check_file`, `document_symbols`, `workspace_symbols`,
`goto_definition`, `goto_declaration`, `goto_type_definition`, `find_references`,
`semantic_tokens`, `file_occurrences`, `type_hierarchy`, `inlay_hints`, `hints`,
`code_actions`, `selection_ranges`, `folding_ranges`, `signature_help`,
`completions`, `document_highlights`, `can_rename`, `rename`, `hover`.

After deletion, `impl PyTyProject` retains only: `open`, `reload`, `close`,
`snapshot`, the Phase-3 write methods (`edit` / `edit_many` / `edit_virtual` /
`sync_path` / `discard` / `sync_all`), and the `head` getter. The `compute_*`
cores and `clone_locked_state` stay (the snapshot wall and `build_frozen` use them).

> This is the architecture §6.1 collapse: "exactly one read surface." The session's
> *convenience* reads move to the Python layer (§6), implemented as sugar over a
> head snapshot. If you prefer to keep a Rust-level head read path warm, that is the
> **Phase 9** floating fast path — explicitly out of scope here. For Phase 4, one
> wall.

---

## 6. Python layer: session reads as head-snapshot sugar (`src/tyo3/session.py`)

`_ReadOps` currently calls `self._inner.<method>(...)`. For a `Snapshot`,
`self._inner` is the native snapshot — fine. For a `TyO3Session`, `self._inner` is
the native `TyProject`, which **no longer has read methods** (§5.3). Route session
reads through a **cached head snapshot**, rebuilt lazily after each mutation.

### 6.1 Indirection point in `_ReadOps`

Add one method and replace every `self._inner` *read* dispatch with `self._native()`:

```python
class _ReadOps:
    _inner: Any
    _closed: bool

    def _native(self) -> Any:
        """The native handle that read methods dispatch to. Overridden by
        TyO3Session to return a cached head snapshot; Snapshot uses itself."""
        return self._inner

    # ... every read method: self._inner.foo(...)  ->  self._native().foo(...)
```

Mechanically replace in `files`, `check`, `check_file`, `document_symbols`,
`workspace_symbols`, `_goto`, `find_references`, `document_highlights`,
`can_rename`, `rename`, `selection_ranges`, `folding_ranges`, `inlay_hints`,
`hints`, `code_actions`, `signature_help`, `completions`, `semantic_tokens`,
`file_occurrences`, `hover`, `type_hierarchy`. The `except`/`model_validate`
wrappers are unchanged — only the call target moves from `self._inner` to
`self._native()`.

### 6.2 `Snapshot`: trivial override + revision

```python
class Snapshot(_ReadOps):
    def __init__(self, native_snapshot: Any) -> None:
        self._inner = native_snapshot
        self._closed = False

    def _native(self) -> Any:
        self._check_open()
        return self._inner

    @property
    def revision(self) -> int:
        """The revision this snapshot is pinned to."""
        self._check_open()
        return self._inner.revision

    # close / __enter__ / __exit__ / __del__ unchanged
```

### 6.3 `TyO3Session`: cached head snapshot

```python
class TyO3Session(_ReadOps):
    def __init__(self, root: str | StdPath) -> None:
        ...
        self._inner = _native.TyProject.open(root_str)
        self._root = StdPath(root_str).resolve()
        self._closed = False
        self._head_snap: Any = None          # cached native head snapshot (current revision)

    def _native(self) -> Any:
        """A head snapshot pinned at the current revision, built lazily and reused
        across reads until the next mutation invalidates it. Because snapshots are
        independent (own Zalsa), holding it does NOT block a later edit."""
        self._check_open()
        if self._head_snap is None:
            self._head_snap = self._inner.snapshot(None)
        return self._head_snap

    def _invalidate_head_snap(self) -> None:
        """Drop the cached head snapshot after a mutation so the next read re-pins
        at the new revision."""
        snap, self._head_snap = self._head_snap, None
        if snap is not None:
            try:
                snap.close()
            except Exception:
                pass

    def snapshot(self, at: int | None = None) -> Snapshot:
        """Pin an explicit MVCC snapshot. ``at=None`` pins the current revision;
        ``at=r`` time-travels to a still-retained revision."""
        self._check_open()
        try:
            native_snapshot = self._inner.snapshot(at)
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in snapshot(): {e}") from e
        return Snapshot(native_snapshot)

    def reload(self) -> None:
        self._check_open()
        self._invalidate_head_snap()
        try:
            self._inner.reload()
        except _NativeClosedError as e:
            raise ProjectClosedError(str(e)) from e
        except Exception as e:
            raise InternalTyError(f"Unexpected error in reload(): {e}") from e

    def close(self) -> None:
        if self._closed:
            return
        self._invalidate_head_snap()
        self._inner.close()
        self._closed = True
```

**Every mutation method must call `self._invalidate_head_snap()` first.** That is
`reload` (above) and the Phase-3 Python write wrappers (`edit`, `edit_many`,
`edit_virtual`, `sync_path`, `discard`, `sync_all`). If Phase 3's Python layer
didn't add those wrappers yet, add the invalidation when it does; if it did, insert
the call at the top of each. Forgetting one means reads keep seeing the
pre-edit revision until the next forced rebuild — a correctness bug, so audit them.

> **Why this is consistent and not a regression.** Within a revision, every read
> reuses one frozen db (open → check → symbols → goto all hit the same pinned db) —
> so a session's reads are mutually consistent, which the old shared-clone path did
> *not* guarantee under concurrent edits. After an edit the cache is dropped and the
> next read re-pins. Existing tests (open → reads → close, no interleaved edits) see
> identical results. The cost is one cold db build per revision read; that is the
> inherent snapshot warmup (architecture §9), amortised by reuse and addressed for
> one-off "latest" glances by the optional Phase 9 floating fast path.

### 6.4 `.pyi` stub

Update `src/tyo3/_native_impl.pyi`: `TyProject.snapshot(self, at: int | None = ...)`,
remove the read-method signatures from `TyProject` (they moved off it), and add
`revision: int` (property) to `TySnapshot`. Keep `TySnapshot`'s read-method
signatures. Match the existing stub's style.

---

## 7. The §0 hazards — now resolved (verify, don't just assert)

Phase 3 §7 listed two hazards that Phase 4 *removes*:

- **§7.1 "a live `PySnapshot` blocks the writer."** Gone: the snapshot is no longer
  a clone of the HEAD db. HEAD's `apply_changes` sees `clones == 1` on HEAD's
  `Zalsa`. Proven by `test_edit_while_snapshot_open_does_not_block` (§8.2).
- **§7.2 "concurrent HEAD reads can be cancelled."** For *snapshot* reads: gone —
  a snapshot's `Zalsa` is never `&mut`-mutated, so its reads never see
  `salsa::Cancelled`. For the *session convenience* reads: they now run on a head
  snapshot too (§6), so they are likewise non-cancellable. (A genuinely warm,
  floating, cancellable head read is the deferred Phase 9 fast path.) Proven by
  `test_snapshot_read_never_cancelled_during_edits` (§8.2; full N-reader stress is
  Phase 5).

The single-writer `Mutex<Head>` from Phase 3 is unchanged. Snapshots are `Send` and
independent; Phase 5 will exercise true parallel readers against a hot writer.

---

## 8. Tests

### 8.1 Rust unit tests (`project.rs` / `overlay.rs` `#[cfg(test)]`)

Reuse the `tempfile` dev-dependency. Build a `HeadState` via `build_head`, mutate
it, snapshot via `build_frozen`, and assert isolation directly (no Python).

```rust
#[cfg(test)]
mod phase4_tests {
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

    fn read(state: &TyProjectState, path: &SystemPathBuf) -> String {
        let f = ruff_db::files::system_path_to_file(&state.db, path).unwrap();
        source_text(&state.db, f).as_str().to_string()
    }

    /// THE Phase-4 invariant: a snapshot pinned at R keeps reading R's content
    /// across many later HEAD edits — including for disk-backed files captured
    /// read-once (no eager materialisation).
    #[test]
    fn snapshot_is_isolated_from_later_head_edits() {
        let (_dir, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new());
        let a = root.join("a.py");

        // Snapshot @ r0 (disk content "X = 1"), captured lazily on first read.
        let snap0 = build_frozen(root.clone(), head.store.capture(), head.store.revision());
        assert!(read(&snap0, &a).contains("X = 1"));  // capture-once pins it

        // Many HEAD edits land afterwards.
        for i in 2..=5 {
            head.store.insert_text(a.clone(), format!("X = {i}\n"));
            head.system.publish(head.store.capture());
            let ev = ty_project::watch::ChangeEvent::file_content_changed(a.clone());
            head.db.apply_changes(std::slice::from_ref(&ev), None);
        }

        // Snapshot still reads r0; head reads latest.
        assert!(read(&snap0, &a).contains("X = 1"));
        let head_state = head.read_clone();
        assert!(read(&head_state, &a).contains("X = 5"));
    }

    /// Read-once capture pins disk content even if disk changes out from under a
    /// snapshot AFTER the snapshot first read the file.
    #[test]
    fn read_once_capture_pins_first_read() {
        let (_dir, root) = project("DISK = 1\n");
        let head = build_head(root.clone(), ContentStore::new());
        let a = root.join("a.py");

        let snap = build_frozen(root.clone(), head.store.capture(), head.store.revision());
        assert!(read(&snap, &a).contains("DISK = 1"));   // captures "DISK = 1"

        // Mutate the real file on disk (an out-of-band change).
        std::fs::write(_dir.path().join("a.py"), "DISK = 999\n").unwrap();

        // The snapshot still serves the captured first read.
        assert!(read(&snap, &a).contains("DISK = 1"));
    }

    /// Overlaid (never-on-disk) content is pinned by the generation.
    #[test]
    fn snapshot_pins_overlay_buffer() {
        let (_dir, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new());
        let a = root.join("a.py");
        head.store.insert_text(a.clone(), "OVERLAY = 1\n");
        head.system.publish(head.store.capture());
        let ev = ty_project::watch::ChangeEvent::file_content_changed(a.clone());
        head.db.apply_changes(std::slice::from_ref(&ev), None);

        let snap = build_frozen(root.clone(), head.store.capture(), head.store.revision());
        assert!(read(&snap, &a).contains("OVERLAY = 1"));
    }

    /// Time-travel: snapshot(at=r) reaches a retained revision; eviction errors.
    #[test]
    fn time_travel_to_retained_revision() {
        let (_dir, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new());
        let a = root.join("a.py");

        let r0 = head.store.revision();
        head.store.insert_text(a.clone(), "X = 2\n");
        let _r1 = head.store.revision();

        let g0 = head.store.generation_at(r0).expect("r0 still retained");
        let snap0 = build_frozen(root.clone(), g0, r0);
        assert!(read(&snap0, &a).contains("X = 1"));   // pinned to r0's (disk) content
    }
}
```

> `head.read_clone()` is the `ReadCloneSource` impl from Phase 2 — fine to call in
> tests. Confirm `ChangeEvent::file_content_changed` is the Phase-3 helper you used;
> if your write path classifies differently, mirror that.

### 8.2 Python end-to-end tests

Add `src/tyo3/tests/test_mvcc_snapshots.py` (and update/replace the Phase-3
`test_snapshot_must_be_closed_before_edit`). Match the suite's import style and
`check()` shape.

```python
def test_edit_while_snapshot_open_does_not_block(tmp_path):
    """Phase 4 removes Phase 3's constraint: an OPEN snapshot no longer blocks an
    edit, because the snapshot is an independent db (own Zalsa)."""
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        snap = s.snapshot()           # leave it OPEN
        snap.check()
        s.edit("a.py", "x = 2\n")     # must NOT hang
        assert snap.check() is not None   # snapshot still usable, still pinned
        snap.close()


def test_snapshot_pins_revision_across_edits(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x: int = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        with s.snapshot() as snap:
            before = _num_diags(snap.check())
            s.edit("a.py", "x: int = 'bad'\n")     # introduces a type error on HEAD
            # snapshot is pinned: its diagnostics are unchanged
            assert _num_diags(snap.check()) == before
            # the session (head) sees the new error
            assert _num_diags(s.check()) > before


def test_snapshot_revision_getter(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        r0 = s.snapshot()
        assert r0.revision == s._inner.head      # pinned at current head revision
        r0.close()


def test_time_travel_snapshot(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        r0 = s._inner.head
        s.edit("a.py", "x = 2\n")
        with s.snapshot(at=r0) as old:
            # reads the project as of r0 (disk "x = 1"), not the edited buffer
            assert old.revision == r0


def test_no_eager_materialization_regression(tmp_path):
    """Smoke: snapshot reads still correct without the eager loop."""
    (tmp_path / "a.py").write_text("VALUE = 42\n")
    with TyO3Session(str(tmp_path)) as s:
        with s.snapshot() as snap:
            assert any("a.py" in str(f) for f in snap.files())
            snap.check()
```

> Shape `_num_diags` to the suite's diagnostics access. `s._inner.head` reaches the
> native revision getter (Phase 3); if the Python layer exposes `head`/`revision` as
> a property, use that instead. The load-bearing assertions are
> **edit-does-not-hang** and **snapshot diagnostics unchanged while head changes**.

---

## 9. Build, test, iterate

```bash
devenv shell -- check-rust     # type/borrow check while iterating
devenv shell -- test-rust      # Phase 1–4 Rust tests
devenv shell -- tests          # FULL Python suite — the regression guard
```

The full Python suite passing is the proof that routing session reads through head
snapshots and collapsing the read wall changed *correctness for the better* without
changing observable results. Watch suite **runtime**: a cold db build per revision
is the expected cost (§6.3). If it regresses unacceptably on the existing fixtures,
the metadata-reuse optimisation in §4 (skip per-snapshot re-discovery) is the lever
— but confirm correctness first; do not trade it for speed.

---

## 10. Gotchas & decisions (read before you debug)

1. **A snapshot must be a fresh db, never `head.db.clone()`.** If you accidentally
   clone the head db (sharing its `Zalsa`), the blocking bug returns and
   `test_edit_while_snapshot_open_does_not_block` hangs. `build_frozen` constructs a
   brand-new `ProjectDatabase` — that independence is the whole point (§0).

2. **Capture must be idempotent and shared across db clones.** `capture_version` is
   `Arc<AtomicU64>` and the intern uses `rcu` with a `contains_key` guard so two
   read-clones of one snapshot reading the same uncaptured file converge on one
   `Document`/version. A bare `AtomicU64`, or clobbering on race, gives two versions
   for one path and salsa may see an inconsistent `FileRevision`.

3. **`path_metadata` and `read_to_string` must agree on the captured revision.**
   Both go through `capture_disk_file`, which is idempotent — whichever salsa calls
   first captures, the other reuses. Don't compute the version twice independently.

4. **Directory structure floats in frozen mode.** `read_directory`/`walk_directory`
   hit live disk; the snapshot's *file set* reflects disk at walk time, while
   *content* is pinned. This is sound under "store is system of record"
   (architecture §2.2): real changes arrive via `sync_path` → a new revision. Don't
   try to freeze the directory tree in Phase 4; just document it.

5. **Drop the head `Mutex` before `build_frozen`.** Holding it across discovery
   serialises the cold build against the writer for no reason (§5.1). Capture under
   the lock (O(1)); build outside it.

6. **Invalidate the cached head snapshot on EVERY mutation.** `reload`, `edit`,
   `edit_many`, `edit_virtual`, `sync_path`, `discard`, `sync_all`. Miss one and the
   session serves a stale revision from reads (§6.3). It's a quiet bug — audit the
   write wrappers.

7. **`snapshot(at=r)` for an evicted revision is a `ValueError`, not a panic.**
   `generation_at` returns `None` past the retention cap; surface it cleanly with
   the oldest-retained revision in the message (§5.1).

8. **`bump_revision` re-retains the same generation.** A rescan changes no overlay
   content, so `record_retained` maps the unchanged generation under the new
   revision — correct and intended (§3).

9. **Remove now-dead code.** After deleting the eager loop and the `PyTyProject`
   read wall: the `source_text` import (`project.rs:12`) and possibly some
   `compute_*` helpers used *only* by deleted methods may warn. The `compute_*`
   cores are still used by `PySnapshot`, so they stay; only genuinely-unreferenced
   imports go. Let `check-rust` guide you.

10. **`#[pyo3(signature = (at=None))]`** is required for the optional `at` arg on
    the Rust `snapshot`; without it PyO3 treats `at` as required. Confirm the
    attribute form against the PyO3 version in `Cargo.toml`.

---

## 11. Explicitly OUT of scope for Phase 4

- The **concurrency stress proof** — N parallel reader snapshots against a hot
  writer, asserting throughput-unaffected and no cancellation (**Phase 5**). Phase 4
  proves single-threaded isolation and *builds the independent-db mechanism* Phase 5
  stresses; it does not add the stress harness.
- The **graph**: `Snapshot.graph()`, `apply_delta`, the reverse-dep index,
  cross-revision `graph.diff` (**Phases 6–7**). `SyncResult` already carries the
  delta; don't wire the graph now.
- The **file watcher** as a change source (**Phase 8**).
- The **floating, warm, cancel-retry `session.check()` fast path** (architecture §6
  "floating" reads). Phase 4 deliberately makes session reads *consistent*
  (head-snapshot sugar); the *warm latest* path is **Phase 9**.
- `save(path)` write-through to disk (`WritableSystem`) — still deferred (Phase 3
  §11); `discard` remains enough.
- Notebook *overlay* content — still delegated to disk.
- Surfacing `retain_cap` as a session option — a trivial later addition.

If you find yourself writing a parallel-reader stress harness, touching
`src/tyo3/graph/`, implementing `WritableSystem`, or adding a cancel-retry loop
around a warm head read, stop — you've left Phase 4.

---

## 12. Definition of Done

- [ ] `overlay.rs`: `OverlaySystem` gains `capture_version: Arc<AtomicU64>` (both
      constructors); `capture_disk_file` interns disk reads race-safely;
      `read_to_string` / `path_metadata` / `source_type` capture on a **frozen**
      miss and still float to disk on a **live** miss.
- [ ] `content.rs`: `ContentStore` gains bounded `retained` + `retain_cap`;
      `record_retained` called from `mutate` and `bump_revision`; `generation_at` /
      `oldest_retained` added; `new()` retains revision 0.
- [ ] `project.rs`: `build_frozen` builds an independent `ProjectDatabase` over
      `OverlaySystem::frozen`; `snapshot(at)` captures (or time-travels) and builds
      a `PySnapshot` over it, with the head lock dropped before the build; the
      **eager-materialisation loop is deleted**.
- [ ] `project.rs`: `PySnapshot` gains an immutable `revision` field + getter and is
      the **only** read wall; all ~24 read methods removed from `PyTyProject`;
      now-dead imports cleaned.
- [ ] `session.py`: `_ReadOps` dispatches reads via `self._native()`;
      `TyO3Session` caches a head snapshot, exposes `snapshot(at=None)`, and
      **invalidates the cache on every mutation** (`reload` + all write wrappers) and
      on `close`; `Snapshot` overrides `_native`, adds a `revision` property.
- [ ] `_native_impl.pyi`: `TyProject.snapshot(at=...)`; read methods removed from
      `TyProject`; `TySnapshot.revision` added.
- [ ] Rust tests pass: `snapshot_is_isolated_from_later_head_edits`,
      `read_once_capture_pins_first_read`, `snapshot_pins_overlay_buffer`,
      `time_travel_to_retained_revision`.
- [ ] Python tests pass: `edit_while_snapshot_open_does_not_block`,
      `snapshot_pins_revision_across_edits`, `snapshot_revision_getter`,
      `time_travel_snapshot`, `no_eager_materialization_regression`; the Phase-3
      "must close before edit" test is inverted/removed.
- [ ] `devenv shell -- tests` shows **no regressions** (results unchanged; note any
      runtime delta from cold snapshot builds).
- [ ] `devenv shell -- check-rust` clean.
- [ ] PR description notes any signature deviations (esp. `ArcSwap::rcu`,
      `Metadata::{file_type,permissions,new}`, `#[pyo3(signature=...)]`,
      `PyValueError` import) and the observed suite-runtime impact.

---

## 13. How this seeds Phase 5+

You now have the MVCC read surface the rest of the substrate stands on:

- **Phase 5** stresses exactly what Phase 4 built: spawn N threads each holding a
  `snapshot()` and reading in a loop while another thread hammers `edit`/`sync_path`.
  Assert (a) the writer's throughput is unaffected by held snapshots (no
  `cancel_others` block — §7), (b) no snapshot read ever raises `salsa::Cancelled`,
  and (c) each snapshot's reads match its pinned revision. The independent-db design
  and read-once capture are precisely the properties under test.
- **Phase 7** adds `Snapshot.graph()` — a copy-on-pin `PyDiGraph` keyed to the same
  revision a `Snapshot` already reads, giving a consistent `snap.check()` +
  `snap.graph()` pair. The `revision` getter and the frozen db are the hooks it
  builds on.
- **Phase 9**'s optional floating `session.check()` fast path is the warm
  complement to §6's consistent (cold) head-snapshot reads — added without
  disturbing the single read wall, because the wall is already the one place reads
  live.

Keep the snapshot db independent, the capture idempotent, and the head-snapshot
cache invalidated on every write, and Phases 5–9 drop straight onto this surface.
