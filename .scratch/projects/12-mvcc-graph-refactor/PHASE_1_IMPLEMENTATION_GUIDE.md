# Phase 1 Implementation Guide — ContentStore + OverlaySystem

> Audience: an engineer new to this codebase implementing **Phase 1** of
> `../11-final-refactor/MVCC_SUBSTRATE_ARCHITECTURE.md`.
>
> Goal of Phase 1: build the **foundation** — an immutable, revisioned content
> store and an overlay `System` that serves in-memory content first and falls
> back to disk. **No Python API changes, no `apply_changes`, no snapshots, no
> graph.** Those are Phases 2–7. Phase 1 is pure Rust plumbing plus tests.
>
> When you finish, the project will compile, all existing tests will still pass,
> and a new suite of Rust unit tests will prove the overlay/frozen invariants.

---

## 0. Mental model (read this first)

A ty `ProjectDatabase` gets all file content through a trait called `System`
(`ruff_db::system::System`). The default is `OsSystem`, which reads the real
disk. ty's own language server replaces it with `LSPSystem`, an **overlay**: an
in-memory map of open documents layered on top of `OsSystem`. When ty asks for a
file's content, `LSPSystem` checks its in-memory map first and only hits disk on a
miss.

We are building our own version of that overlay, called `OverlaySystem`, plus the
immutable content store behind it. This is the single most important component in
the whole architecture: **every database — the live "head" and every pinned MVCC
snapshot — is a `ProjectDatabase` over one of these overlay systems.** In Phase 1
we just build and test the component in isolation.

### Reference implementations to study before you start

Open these in the vendored ruff checkout and read them. Paths are on this machine:

- **The trait you must implement:**
  `~/.cargo/git/checkouts/ruff-b18f69e2b025fac7/3cb09eb/crates/ruff_db/src/system.rs`
  — search for `pub trait System`. This is the contract.
- **The blueprint to copy:**
  `.../crates/ty_server/src/system.rs` — `struct LSPSystem` and
  `impl System for LSPSystem`. Our `OverlaySystem` is structurally the same idea:
  overlay map first, `native_system` fallback. Notice how almost every method just
  calls `self.native_system.<method>()` and only a handful
  (`path_metadata`, `read_to_string`, `read_virtual_path_to_string`,
  `source_type`) consult the overlay.
- **How a db is constructed over a custom system:**
  `.../crates/ty_project/src/db.rs` — `ProjectDatabase::use_defaults<S>` and
  `fallible<S>`. Both are generic over `S: System + Send + Sync + RefUnwindSafe`,
  so passing an `OverlaySystem` "just works".
- **Our current equivalent (what we're generalising):**
  `rust/src/project.rs` lines ~703–730 (`open`) and ~739–755 (`reload`) build a db
  over `OsSystem`. Phase 2 will swap that for our overlay; Phase 1 only adds the
  new code path, it does not touch `open`/`reload`.

### The one invariant Phase 1 must prove

A **frozen** overlay (a pinned generation) must be completely isolated from later
mutations of the **live** overlay. This is the seed of MVCC: a snapshot pinned at
revision R keeps seeing R's content forever, no matter what the writer does next.
Our final test asserts exactly this.

---

## 1. Add dependencies

Edit `rust/Cargo.toml`.

In `[dependencies]` add:

```toml
# ── Content store ──────────────────────────────────────
rpds = "1"          # persistent (immutable, structurally-shared) HashTrieMap
arc-swap = "1"      # lock-free atomic swap of the live content generation
```

Add a `[dev-dependencies]` section if one does not already exist:

```toml
[dev-dependencies]
tempfile = "3"      # real temp directories for OsSystem-backed tests
```

Why these:

- **`rpds::HashTrieMapSync`** is an immutable hash map. `insert`/`remove` return a
  *new* map sharing most structure with the old one; cloning a map is `O(1)`
  (it's an `Arc` bump). That's what makes "publish a new revision" and "pin a
  snapshot" cheap. **Use the `Sync` variant** (`HashTrieMapSync`, which is
  `Arc`-backed) — the plain `HashTrieMap` is `Rc`-backed and is not `Send + Sync`,
  which the `System` trait requires.
- **`arc-swap::ArcSwap`** lets the live overlay's content pointer be replaced
  atomically and read lock-free, so many reader threads never contend. (We don't
  swap anything in Phase 1, but we design the field for it so Phase 3 is a
  drop-in.)

---

## 2. New file: `rust/src/content.rs`

The immutable content layer. No `System` here — just data.

```rust
//! Immutable, revisioned content store backing the overlay system.
//!
//! A `Generation` is an immutable snapshot of all overlaid document content at
//! one revision. Because it is a persistent map, producing the next generation
//! (insert/delete) shares structure with the previous one, and capturing a
//! generation (for a snapshot) is an O(1) `Arc` clone.

use std::sync::Arc;

use ruff_db::system::SystemPathBuf;
use rpds::HashTrieMapSync;

/// Application-level monotonic revision. Distinct from salsa's internal revision;
/// this is the number we will eventually hand back to Python as `SyncResult.revision`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Revision(pub u64);

/// One overlaid document, or a tombstone marking a path as known-absent.
#[derive(Debug, Clone)]
pub enum Document {
    /// In-memory text content. `version` is a monotonic per-store counter; it
    /// changes every time the content at a path changes, so a consumer (salsa,
    /// in Phase 3) can tell "this file changed" by comparing versions.
    Text { text: Arc<str>, version: u64 },
    /// The path is overlaid as deleted: reads must fail even if a file exists on
    /// disk. Needed so an agent can model "what if this file didn't exist".
    Deleted { version: u64 },
}

impl Document {
    pub fn version(&self) -> u64 {
        match self {
            Document::Text { version, .. } | Document::Deleted { version } => *version,
        }
    }
}

/// An immutable snapshot of all overlay content at one revision.
pub type Generation = Arc<HashTrieMapSync<SystemPathBuf, Document>>;

/// The mutable, head-side owner of content. The writer (Phase 3) holds exactly
/// one of these. Phase 1 only needs construct / insert / delete / capture.
#[derive(Debug)]
pub struct ContentStore {
    generation: Generation,
    revision: Revision,
    version_counter: u64,
}

impl Default for ContentStore {
    fn default() -> Self {
        Self::new()
    }
}

impl ContentStore {
    pub fn new() -> Self {
        Self {
            generation: Arc::new(HashTrieMapSync::new_sync()),
            revision: Revision(0),
            version_counter: 0,
        }
    }

    /// The current revision.
    pub fn revision(&self) -> Revision {
        self.revision
    }

    /// Capture the current content as an immutable generation (O(1) Arc clone).
    /// This is what a snapshot will pin.
    pub fn capture(&self) -> Generation {
        Arc::clone(&self.generation)
    }

    fn next_version(&mut self) -> u64 {
        self.version_counter += 1;
        self.version_counter
    }

    /// Overlay `path` with in-memory text. Returns the new revision.
    pub fn insert_text(&mut self, path: SystemPathBuf, text: impl Into<Arc<str>>) -> Revision {
        let version = self.next_version();
        let doc = Document::Text { text: text.into(), version };
        self.generation = Arc::new(self.generation.insert(path, doc));
        self.revision = Revision(self.revision.0 + 1);
        self.revision
    }

    /// Overlay `path` as deleted. Returns the new revision.
    pub fn delete(&mut self, path: SystemPathBuf) -> Revision {
        let version = self.next_version();
        self.generation = Arc::new(self.generation.insert(path, Document::Deleted { version }));
        self.revision = Revision(self.revision.0 + 1);
        self.revision
    }

    /// Drop any overlay for `path` (revert to whatever disk says). Returns the
    /// new revision.
    pub fn forget(&mut self, path: &SystemPathBuf) -> Revision {
        self.generation = Arc::new(self.generation.remove(path));
        self.revision = Revision(self.revision.0 + 1);
        self.revision
    }
}

/// Look a path up inside a captured generation.
pub fn lookup<'a>(generation: &'a Generation, path: &SystemPathBuf) -> Option<&'a Document> {
    generation.get(path)
}
```

> Note the `HashTrieMapSync::new_sync()` constructor — check the exact name in the
> `rpds` version that resolves (`cargo doc -p rpds --open`, or rust-analyzer). Some
> versions expose `HashTrieMapSync::new_sync()`, others `new()`. Adjust if needed.

---

## 3. New file: `rust/src/overlay.rs`

The `System` implementation. This is the heart of Phase 1.

```rust
//! `OverlaySystem`: a ty `System` that serves overlaid in-memory content first
//! and falls back to a native `OsSystem` on a miss. Modelled on ty_server's
//! `LSPSystem` (crates/ty_server/src/system.rs).
//!
//! The same type serves two roles, distinguished by `frozen`:
//!   * `frozen == None`  → the live HEAD overlay; its content can be republished.
//!   * `frozen == Some(R)` → a pinned, immutable view of revision R (an MVCC
//!     snapshot's view). It is never republished, so it is isolated from the
//!     head forever.

use std::any::Any;
use std::panic::RefUnwindSafe;
use std::sync::Arc;

use arc_swap::ArcSwap;
use ruff_db::file_revision::FileRevision;
use ruff_db::system::walk_directory::WalkDirectoryBuilder;
use ruff_db::system::{
    CaseSensitivity, DirectoryEntry, FileType, Metadata, OsSystem, Result, System, SystemPath,
    SystemPathBuf, SystemVirtualPath, WhichResult, WritableSystem,
};
use ruff_python_ast::PySourceType;

use crate::content::{Document, Generation};

type SharedContent = Arc<ArcSwap<rpds::HashTrieMapSync<SystemPathBuf, Document>>>;

#[derive(Debug, Clone)]
pub struct OverlaySystem {
    /// The live (or pinned) content. `ArcSwap` so the head can be republished
    /// lock-free in Phase 3; `Arc<...>` so all clones of this system (and all
    /// `db.clone()`s that share it) observe the same content cell.
    content: SharedContent,
    /// Disk fallback for paths the overlay does not cover.
    native: Arc<dyn System + Send + Sync + RefUnwindSafe>,
    /// `None` = live head; `Some(R)` = pinned snapshot view of revision R.
    frozen: Option<crate::content::Revision>,
}

impl OverlaySystem {
    /// Build a live HEAD overlay rooted at `root`, starting from `initial`
    /// content (usually an empty generation).
    pub fn live(root: SystemPathBuf, initial: Generation) -> Self {
        Self {
            content: Arc::new(ArcSwap::new(initial)),
            native: Arc::new(OsSystem::new(root)),
            frozen: None,
        }
    }

    /// Build a frozen, pinned view of `generation` at revision `rev`. Used by the
    /// snapshot path in Phase 4; included now so the isolation test can exist.
    pub fn frozen(root: SystemPathBuf, generation: Generation, rev: crate::content::Revision) -> Self {
        Self {
            content: Arc::new(ArcSwap::new(generation)),
            native: Arc::new(OsSystem::new(root)),
            frozen: Some(rev),
        }
    }

    /// Republish the live content (HEAD only). No-op-able in Phase 1; exercised
    /// in Phase 3.
    pub fn publish(&self, generation: Generation) {
        debug_assert!(self.frozen.is_none(), "must not republish a frozen overlay");
        self.content.store(generation);
    }

    pub fn is_frozen(&self) -> bool {
        self.frozen.is_some()
    }

    /// Snapshot-look-up the overlay document for `path`, if any.
    fn document(&self, path: &SystemPath) -> Option<Document> {
        // `load()` is a cheap RCU read; clone the small `Document` (Arc<str> inside).
        self.content.load().get(&path.to_path_buf()).cloned()
    }
}

impl System for OverlaySystem {
    fn path_metadata(&self, path: &SystemPath) -> Result<Metadata> {
        match self.document(path) {
            Some(Document::Text { version, .. }) => Ok(Metadata::new(
                FileRevision::new(u128::from(version)),
                None,
                FileType::File,
            )),
            Some(Document::Deleted { .. }) => Err(not_found(path)),
            None => self.native.path_metadata(path),
        }
    }

    fn read_to_string(&self, path: &SystemPath) -> Result<String> {
        match self.document(path) {
            Some(Document::Text { text, .. }) => Ok(text.to_string()),
            Some(Document::Deleted { .. }) => Err(not_found(path)),
            None => self.native.read_to_string(path),
        }
    }

    fn source_type(&self, path: &SystemPath) -> Option<PySourceType> {
        match self.document(path) {
            // Overlaid text is treated as Python source unless the extension says
            // otherwise; mirror LSPSystem's behaviour if you need notebooks later.
            Some(Document::Text { .. }) => path
                .extension()
                .and_then(PySourceType::try_from_extension)
                .or(Some(PySourceType::Python)),
            Some(Document::Deleted { .. }) => None,
            None => self.native.source_type(path),
        }
    }

    // ── Everything below delegates to the native system ─────────────────────
    // Phase 1 overlays *system text files only*. Notebooks and virtual paths are
    // handled by later phases; for now they fall through to disk.

    fn canonicalize_path(&self, path: &SystemPath) -> Result<SystemPathBuf> {
        self.native.canonicalize_path(path)
    }
    fn read_to_notebook(
        &self,
        path: &SystemPath,
    ) -> std::result::Result<ruff_notebook::Notebook, ruff_notebook::NotebookError> {
        // See "Gotchas" — you may delegate without importing ruff_notebook by using
        // the associated return types via the trait; simplest is to add the dep.
        self.native.read_to_notebook(path)
    }
    fn read_virtual_path_to_string(&self, path: &SystemVirtualPath) -> Result<String> {
        self.native.read_virtual_path_to_string(path)
    }
    fn read_virtual_path_to_notebook(
        &self,
        path: &SystemVirtualPath,
    ) -> std::result::Result<ruff_notebook::Notebook, ruff_notebook::NotebookError> {
        self.native.read_virtual_path_to_notebook(path)
    }
    fn which(&self, name: &str) -> WhichResult {
        self.native.which(name)
    }
    fn path_exists_case_sensitive(&self, path: &SystemPath, prefix: &SystemPath) -> bool {
        // Overlaid existing paths are case-checked by the native system; good
        // enough for Phase 1.
        self.native.path_exists_case_sensitive(path, prefix)
    }
    fn case_sensitivity(&self) -> CaseSensitivity {
        self.native.case_sensitivity()
    }
    fn current_directory(&self) -> &SystemPath {
        self.native.current_directory()
    }
    fn user_config_directory(&self) -> Option<SystemPathBuf> {
        self.native.user_config_directory()
    }
    fn cache_dir(&self) -> Option<SystemPathBuf> {
        self.native.cache_dir()
    }
    fn read_directory<'a>(
        &'a self,
        path: &SystemPath,
    ) -> Result<Box<dyn Iterator<Item = Result<DirectoryEntry>> + 'a>> {
        self.native.read_directory(path)
    }
    fn walk_directory(&self, path: &SystemPath) -> WalkDirectoryBuilder {
        self.native.walk_directory(path)
    }
    fn env_var(&self, name: &str) -> std::result::Result<String, std::env::VarError> {
        self.native.env_var(name)
    }
    fn as_writable(&self) -> Option<&dyn WritableSystem> {
        // Phase 1: not writable through the overlay. Writes (save-to-disk) are a
        // later phase.
        None
    }
    fn as_any(&self) -> &dyn Any {
        self
    }
    fn as_any_mut(&mut self) -> &mut dyn Any {
        self
    }
    fn dyn_clone(&self) -> Box<dyn System> {
        Box::new(self.clone())
    }
}

fn not_found(path: &SystemPath) -> std::io::Error {
    std::io::Error::new(
        std::io::ErrorKind::NotFound,
        format!("No such file (overlaid as deleted): {path}"),
    )
}

/// Construct a `ProjectDatabase` over a fresh live overlay. Phase-1 helper used by
/// tests; Phase 2 wires this into `PyTyProject::open`.
pub fn open_overlay_database(root: SystemPathBuf) -> ProjectDatabaseHandle {
    use ruff_python_ast::name::Name;
    use ty_project::{ProjectDatabase, ProjectMetadata};

    let empty: Generation = Arc::new(rpds::HashTrieMapSync::new_sync());
    let system = OverlaySystem::live(root.clone(), empty);
    let metadata = ProjectMetadata::new(Name::new("tyo3-project"), root);
    ProjectDatabase::use_defaults(metadata, system)
}

// Type alias just to keep the helper signature readable.
type ProjectDatabaseHandle = ty_project::ProjectDatabase;
```

---

## 4. Wire the modules into the crate

Edit `rust/src/lib.rs` and add, next to the existing `mod files;`:

```rust
mod content;
mod overlay;
```

Do **not** register anything new in the `#[pymodule]` — Phase 1 exposes nothing to
Python.

---

## 5. Tests — `rust/src/overlay.rs` `#[cfg(test)]` module

Add this at the bottom of `overlay.rs`. These are the deliverable proof of Phase 1.
They run with `devenv shell -- test-rust` (which runs `cargo test` under the hood;
see `MEMORY.md` — never call cargo directly outside the devenv shell).

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::content::ContentStore;
    use ruff_db::system::SystemPath;
    use std::io::Write;

    /// A temp dir with one file `a.py` containing `disk_contents`, plus the
    /// matching `SystemPathBuf` root and an `OsSystem`-backed overlay.
    fn fixture(disk_contents: &str) -> (tempfile::TempDir, SystemPathBuf, SystemPathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let mut f = std::fs::File::create(dir.path().join("a.py")).unwrap();
        f.write_all(disk_contents.as_bytes()).unwrap();
        let root = SystemPathBuf::from_path_buf(dir.path().to_path_buf()).unwrap();
        let a = root.join("a.py");
        (dir, root, a)
    }

    #[test]
    fn reads_fall_through_to_disk_when_not_overlaid() {
        let (_dir, root, a) = fixture("X = 1\n");
        let sys = OverlaySystem::live(root, Arc::new(rpds::HashTrieMapSync::new_sync()));
        assert_eq!(sys.read_to_string(&a).unwrap(), "X = 1\n");
        assert!(sys.path_metadata(&a).is_ok());
    }

    #[test]
    fn overlay_text_shadows_disk() {
        let (_dir, root, a) = fixture("X = 1\n");
        let mut store = ContentStore::new();
        store.insert_text(a.clone(), "Y = 2\n");
        let sys = OverlaySystem::live(root, store.capture());
        assert_eq!(sys.read_to_string(&a).unwrap(), "Y = 2\n");
    }

    #[test]
    fn overlay_delete_tombstone_hides_disk_file() {
        let (_dir, root, a) = fixture("X = 1\n");
        let mut store = ContentStore::new();
        store.delete(a.clone());
        let sys = OverlaySystem::live(root, store.capture());
        assert!(sys.read_to_string(&a).is_err());
        assert!(sys.path_metadata(&a).is_err());
    }

    #[test]
    fn metadata_revision_changes_with_content() {
        let (_dir, root, a) = fixture("X = 1\n");
        let mut store = ContentStore::new();
        store.insert_text(a.clone(), "v1");
        let r1 = OverlaySystem::live(root.clone(), store.capture())
            .path_metadata(&a).unwrap().revision();
        store.insert_text(a.clone(), "v2");
        let r2 = OverlaySystem::live(root, store.capture())
            .path_metadata(&a).unwrap().revision();
        assert_ne!(r1, r2, "content change must bump the file revision");
    }

    /// THE Phase-1 invariant: a frozen view is isolated from later head edits.
    #[test]
    fn frozen_view_is_isolated_from_later_mutation() {
        let (_dir, root, a) = fixture("X = 1\n");
        let mut store = ContentStore::new();
        let r0 = store.insert_text(a.clone(), "pinned\n");

        // Pin the generation at r0.
        let frozen = OverlaySystem::frozen(root.clone(), store.capture(), r0);

        // Mutate the head afterwards.
        store.insert_text(a.clone(), "moved on\n");
        let head = OverlaySystem::live(root, store.capture());

        assert_eq!(frozen.read_to_string(&a).unwrap(), "pinned\n");
        assert_eq!(head.read_to_string(&a).unwrap(), "moved on\n");
    }

    /// Smoke test: a ProjectDatabase builds over the overlay and reads disk
    /// content through it. (No mutation-through-salsa — that's Phase 3.)
    #[test]
    fn project_database_builds_over_overlay() {
        use ruff_db::source::source_text;
        let (_dir, root, a) = fixture("VALUE = 42\n");
        let db = open_overlay_database(root.clone());
        let file = ruff_db::files::system_path_to_file(&db, &a).unwrap();
        assert!(source_text(&db, file).as_str().contains("VALUE = 42"));
    }
}
```

> `Metadata::revision()` — confirm the accessor name (`revision()` vs a public
> field) against `ruff_db::system::Metadata` and adjust. If there is no getter,
> compare via `path_metadata(...).unwrap()` debug output or add the comparison the
> type supports.

---

## 6. Build, test, iterate

From the project root, always inside the devenv shell (see `MEMORY.md`):

```bash
devenv shell -- check-rust          # fast type/borrow check while iterating
devenv shell -- test-rust           # run the Rust test suite
devenv shell -- tests               # full suite — confirm nothing regressed
```

Expect to spend most of your time resolving trait/type friction against this exact
ty pin (rev `3cb09eb`). That friction *is* the task; the code above is correct in
shape but the vendored API may differ in small ways (method names, `Result`
aliases, constructor names). Use rust-analyzer / `cargo doc` to confirm each
signature.

---

## 7. Gotchas & decisions (read before you debug)

1. **`HashTrieMapSync`, not `HashTrieMap`.** The `System` trait is
   `Sync + Send`. The `Rc`-backed `HashTrieMap` is neither. If you see "cannot be
   sent between threads safely", you used the wrong variant.

2. **`RefUnwindSafe` is required.** `ProjectDatabase` stores
   `Arc<dyn System + Send + Sync + RefUnwindSafe>`. `ArcSwap`, `Arc`, and our
   `Document` are all `RefUnwindSafe`, so `#[derive]`/auto should satisfy it. If
   the compiler complains, the offending field is usually a closure or raw
   pointer — you have none, so re-read the error; it's likely the `native` field's
   bound, which the alias already states.

3. **`read_to_notebook` and `ruff_notebook`.** The trait's notebook methods
   reference `ruff_notebook::Notebook` / `NotebookError`. You have two clean
   options: (a) add `ruff_notebook = { git = ..., rev = "3cb09eb" }` to
   `Cargo.toml` (same rev as the others) and write the delegations as shown; or
   (b) keep the return types fully qualified through the trait so you never name
   the crate. Option (a) is simpler and forward-compatible (Phase 4 wants
   notebooks anyway). Match the existing `rev` exactly.

4. **`ArcSwap::load()` gives a guard, not a value.** `self.content.load()` returns
   a `Guard`; `.get(key)` borrows through it. Clone the small `Document` out
   (as shown) so you don't hold the guard across the disk-fallback call.

5. **Do not touch `project.rs` `open`/`reload` yet.** Phase 1 adds code; it does
   not rewire the session. Keeping `open`/`reload` on `OsSystem` means the entire
   existing Python test suite must still pass unchanged — that's your regression
   guard.

6. **`dyn_clone` / `Clone` semantics.** Cloning an `OverlaySystem` clones the
   `Arc<ArcSwap<…>>`, so the clone *shares* the same content cell and sees future
   `publish()`es. That is what we want for the head (all `db.clone()`s observe the
   same content). Don't "fix" it to deep-copy.

---

## 8. Explicitly OUT of scope for Phase 1

Do not implement these — they have dedicated later phases and adding them now will
muddy the foundation:

- Read-once / write-through disk caching into the store (Phase 4 — it only becomes
  *observable* once snapshots share content across databases; adding interior
  mutation here is premature and complicates concurrency).
- Virtual-path (`SystemVirtualPath`) overlay content (later phase).
- Notebook overlay content (later phase; Phase 1 delegates notebooks to disk).
- `apply_changes` / `ChangeEvent` synthesis / `SyncDelta` (Phase 3).
- Any `PyTyProject` / `TyO3Session` / Python-facing change (Phase 2+).
- Snapshots exposed to Python, revision API, the graph (Phases 4–7).

If you find yourself editing `project.rs`, `session.py`, or anything in
`src/tyo3/graph/`, stop — you've left Phase 1.

---

## 9. Definition of Done

- [ ] `rpds`, `arc-swap` added to `[dependencies]`; `tempfile` to
      `[dev-dependencies]`; (optionally `ruff_notebook` at rev `3cb09eb`).
- [ ] `rust/src/content.rs` implements `Revision`, `Document`, `ContentStore`,
      `Generation`, `lookup`.
- [ ] `rust/src/overlay.rs` implements `OverlaySystem` with a full `impl System`,
      `live`/`frozen`/`publish` constructors, and `open_overlay_database`.
- [ ] `content` and `overlay` modules declared in `lib.rs`.
- [ ] All seven Phase-1 tests pass via `devenv shell -- test-rust`, including
      `frozen_view_is_isolated_from_later_mutation` and
      `project_database_builds_over_overlay`.
- [ ] `devenv shell -- tests` shows **no regressions** in the existing Python
      suite (you changed no existing code paths).
- [ ] `devenv shell -- check-rust` is clean (no warnings on the new modules; run
      `cargo clippy` equivalent if the repo lints it).
- [ ] A short note in the PR description listing any signature deviations you hit
      versus this guide (so we can correct the guide for later phases).

---

## 10. How this seeds Phase 2+

You will have built the component every later phase stands on:

- **Phase 2** swaps `PyTyProject::open`/`reload` onto `open_overlay_database`,
  keeping a `ContentStore` next to the db.
- **Phase 3** calls `store.insert_text(...)` then `overlay.publish(store.capture())`
  then `db.apply_changes(&events, None)` to make edits incremental.
- **Phase 4** calls `OverlaySystem::frozen(root, store.capture(), rev)` to build
  the independent, never-cancelled MVCC snapshot databases.

Get the foundation clean and well-tested and the rest of the architecture clicks
on top of it.
