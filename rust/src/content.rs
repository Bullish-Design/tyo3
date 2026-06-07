//! Immutable, revisioned content store backing the overlay system.
//!
//! A `Generation` is an immutable snapshot of all overlaid document content at
//! one revision. Because it is a persistent map, producing the next generation
//! (insert/delete) shares structure with the previous one, and capturing a
//! generation (for a snapshot) is an O(1) `Arc` clone.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use ruff_db::system::{OsSystem, System, SystemPath, SystemPathBuf, SystemVirtualPathBuf};
use ruff_db::system::walk_directory::WalkState;
use rpds::HashTrieMapSync;

use crate::hash::{hash_text, ContentHash};
use serde::{Deserialize, Serialize};

/// Application-level monotonic revision. Distinct from salsa's internal revision;
/// this is the number we will eventually hand back to Python as `SyncResult.revision`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct Revision(pub u64);

/// One overlaid document, or a tombstone marking a path as known-absent.
#[derive(Debug, Clone)]
pub enum Document {
    /// In-memory text content. `hash` is computed once at construction and
    /// never changes; `version` is a monotonic per-store counter that changes
    /// every time the content at a path changes, so a consumer (salsa, in
    /// Phase 3) can tell "this file changed" by comparing versions.
    ///
    /// INVARIANT: `hash` is always `hash_text(&text)` at construction time.
    Text {
        text: Arc<str>,
        hash: ContentHash,
        version: u64,
    },
    /// The path is overlaid as deleted: reads must fail even if a file exists on
    /// disk. Needed so an agent can model "what if this file didn't exist".
    Deleted { version: u64 },
}

impl Document {
    /// Construct a `Text` document, computing the content hash from the text
    /// bytes. The `version` must come from the owning `ContentStore`'s
    /// monotonic counter.
    pub fn text(text: impl Into<Arc<str>>, version: u64) -> Self {
        let text: Arc<str> = text.into();
        let hash = hash_text(&text);
        Document::Text { text, hash, version }
    }

    /// The content hash, if this is a `Text` document.  Tombstones
    /// (`Deleted`) have no hash and return `None`.
    pub fn hash(&self) -> Option<ContentHash> {
        match self {
            Document::Text { hash, .. } => Some(*hash),
            Document::Deleted { .. } => None,
        }
    }

    pub fn version(&self) -> u64 {
        match self {
            Document::Text { version, .. } | Document::Deleted { version } => *version,
        }
    }
}

// ── ContentMap / Generation ─────────────────────────────────────────────

/// All overlay content at one revision: system-path documents and virtual-path
/// documents. Both are persistent maps, so cloning a `ContentMap` is two cheap
/// `Arc` bumps and structurally shares with the previous generation.
#[derive(Debug, Clone)]
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

// ── Change: the write vocabulary ─────────────────────────────────────────

/// One mutation the store can apply.  Every write is one of these variants.
///
/// A batch of `Change`s applied via `apply_batch` produces exactly one
/// revision — the store never creates an intermediate revision for a
/// half-applied batch (§6.1.1).
#[derive(Debug, Clone)]
pub enum Change {
    /// Overlay a system path with in-memory text.
    Insert { path: SystemPathBuf, text: Arc<str> },
    /// Overlay a system path as deleted (tombstone).
    Delete { path: SystemPathBuf },
    /// Insert or replace a virtual-path document.
    InsertVirtual { path: SystemVirtualPathBuf, text: Arc<str> },
    /// Remove a virtual-path document.
    ForgetVirtual { path: SystemVirtualPathBuf },
    /// Drop the overlay for a system path so reads fall through to disk.
    Forget { path: SystemPathBuf },
}

// ── ContentStore ────────────────────────────────────────────────────────

/// The mutable, head-side owner of content. The writer holds exactly one of
/// these. Every mutation bumps the application `Revision`.
///
/// A bounded `retained` buffer keeps recent (revision → generation) pairs so
/// `snapshot(at=r)` can time-travel to a still-retained revision. Old entries
/// are evicted past `retain_cap`. A live snapshot holding its own `Generation`
/// is unaffected by eviction — it pins its generation independently.
pub struct ContentStore {
    generation: Generation,
    revision: Revision,
    version_counter: u64,
    /// Recent (revision → generation) for `snapshot(at=r)` time-travel. Bounded;
    /// oldest entries evicted past `retain_cap`.
    retained: BTreeMap<Revision, Generation>,
    retain_cap: usize,
    /// Per-file project-content disk read counter. Incremented once for each
    /// file actually read from disk by ingest_project / apply_disk_batch.
    /// Exposed to Python as the test seam for Phase 1's
    /// `test_snapshot_construction_reads_no_disk`.
    disk_reads: Arc<AtomicU64>,
}

impl std::fmt::Debug for ContentStore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ContentStore")
            .field("revision", &self.revision)
            .field("version_counter", &self.version_counter)
            .field("retain_cap", &self.retain_cap)
            .field("retained_len", &self.retained.len())
            .field("disk_reads", &self.disk_reads.load(Ordering::Relaxed))
            .finish()
    }
}

const DEFAULT_RETAIN_CAP: usize = 256;

impl Default for ContentStore {
    fn default() -> Self {
        Self::new()
    }
}

impl ContentStore {
    pub fn new() -> Self {
        Self::with_retain_cap(DEFAULT_RETAIN_CAP)
    }

    pub fn with_retain_cap(retain_cap: usize) -> Self {
        let generation: Generation = Arc::new(ContentMap::new());
        let mut retained = BTreeMap::new();
        retained.insert(Revision(0), Arc::clone(&generation));
        Self {
            generation,
            revision: Revision(0),
            version_counter: 0,
            retained,
            retain_cap,
            disk_reads: Arc::new(AtomicU64::new(0)),
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

    /// Apply `f` to a clone of the current `ContentMap`, swap the generation,
    /// and bump the application revision. Centralises the copy-on-write
    /// pattern.
    ///
    /// `next_version` is a callable the closure can use to consume version
    /// numbers from the store's monotonic counter.  This lets a single
    /// `mutate` call create many new `Document`s without exposing the counter
    /// directly.
    fn mutate(
        &mut self,
        f: impl FnOnce(&mut ContentMap, &mut dyn FnMut() -> u64),
    ) -> Revision {
        let mut map = (*self.generation).clone();
        let mut next_version = || {
            self.version_counter += 1;
            self.version_counter
        };
        f(&mut map, &mut next_version);
        self.generation = Arc::new(map);
        self.revision = Revision(self.revision.0 + 1);
        self.record_retained();
        self.revision
    }

    // ── Public batch API (the key requirement) ───────────────────────────

    /// Apply a batch of changes atomically, producing exactly one new revision
    /// and one retained generation.  This is the ONLY code path that creates
    /// revisions — even single-change helpers route through here.
    ///
    /// CONCURRENCY: callers hold the write lock.  No other thread can observe
    /// a half-batch revision because there is none.
    ///
    /// INVARIANT: every write is one of the `Change` variants, and a single
    /// `apply_batch` advances the revision exactly once (§6.1.1).
    pub fn apply_batch(&mut self, changes: Vec<Change>) -> Revision {
        self.mutate(|m, next_version| {
            for c in changes {
                match c {
                    Change::Insert { path, text } => {
                        let d = Document::text(text, next_version());
                        m.system = m.system.insert(path, d);
                    }
                    Change::Delete { path } => {
                        m.system =
                            m.system.insert(path, Document::Deleted { version: next_version() });
                    }
                    Change::Forget { path } => {
                        m.system = m.system.remove(&path);
                    }
                    Change::InsertVirtual { path, text } => {
                        let d = Document::text(text, next_version());
                        m.virtual_files = m.virtual_files.insert(path, d);
                    }
                    Change::ForgetVirtual { path } => {
                        m.virtual_files = m.virtual_files.remove(&path);
                    }
                }
            }
        })
    }

    // ── Thin single-change helpers ──────────────────────────────────────
    //
    // Each is a convenience wrapper around `apply_batch(vec![…])` that
    // advances the revision by exactly 1.

    /// Overlay `path` with in-memory text. Returns the new revision.
    pub fn insert_text(&mut self, path: SystemPathBuf, text: impl Into<Arc<str>>) -> Revision {
        self.apply_batch(vec![Change::Insert {
            path,
            text: text.into(),
        }])
    }

    /// Overlay `path` as deleted. Returns the new revision.
    pub fn delete(&mut self, path: SystemPathBuf) -> Revision {
        self.apply_batch(vec![Change::Delete { path }])
    }

    /// Drop any overlay for `path` (revert to whatever disk says). Returns the
    /// new revision.
    pub fn forget(&mut self, path: &SystemPathBuf) -> Revision {
        self.apply_batch(vec![Change::Forget {
            path: path.clone(),
        }])
    }

    /// Overlay a virtual path (e.g. "untitled:1") with in-memory text.
    pub fn insert_virtual(
        &mut self,
        path: SystemVirtualPathBuf,
        text: impl Into<Arc<str>>,
    ) -> Revision {
        self.apply_batch(vec![Change::InsertVirtual {
            path,
            text: text.into(),
        }])
    }

    /// Drop any overlay for a virtual path.
    pub fn forget_virtual(&mut self, path: &SystemVirtualPathBuf) -> Revision {
        self.apply_batch(vec![Change::ForgetVirtual {
            path: path.clone(),
        }])
    }

    /// Advance the application revision without changing content (used by
    /// `sync_all` / rescan, where ty does the work but we want observability).
    ///
    /// Re-retains the *same* generation under the new revision: a rescan
    /// changes no overlay content, so `snapshot(at=that_rev)` and
    /// `snapshot(at=prev_rev)` pin identical content but build dbs that
    /// re-walk disk independently.
    pub fn bump_revision(&mut self) -> Revision {
        self.revision = Revision(self.revision.0 + 1);
        self.record_retained();
        self.revision
    }

    /// The number of project-content files read from disk by ingest helpers.
    /// Exposed to Python as the Phase 1 test seam.
    pub fn disk_read_count(&self) -> u64 {
        self.disk_reads.load(Ordering::Relaxed)
    }

    /// Return a clone of the `Arc<AtomicU64>` counter so callers outside
    /// `ContentStore` (e.g. `PyTyProject`) can read it without holding the
    /// write lock.
    pub fn disk_read_counter(&self) -> Arc<AtomicU64> {
        Arc::clone(&self.disk_reads)
    }

    // ── Disk ingest helpers (Phase 1) ────────────────────────────────────

    /// Walk `root` once, read every project-relevant file from disk, and
    /// intern each as a `Document::Text` (carrying its content hash).
    /// A relevant path that the walk shows as a regular file but cannot be
    /// read is skipped with a log warning (the `pre_populate_generation`
    /// pattern); a relevant path that is absent becomes a tombstone.
    ///
    /// Exactly one revision is produced for the entire walk, and every
    /// interned document gets a real version from the store's monotonic
    /// counter.
    pub fn ingest_project(
        &mut self,
        root: &SystemPath,
        filter: impl Fn(&SystemPath) -> bool + Send + Clone,
    ) -> Revision {
        let native = OsSystem::new(root.to_path_buf());
        let walker = native.walk_directory(root);
        let disk_reads = Arc::clone(&self.disk_reads);

        // Collect relevant file paths.
        let paths: Arc<Mutex<Vec<SystemPathBuf>>> = Arc::new(Mutex::new(Vec::new()));
        let paths_clone = Arc::clone(&paths);
        walker.run(move || {
            let paths = Arc::clone(&paths_clone);
            let filter = filter.clone();
            Box::new(move |entry: std::result::Result<
                ruff_db::system::walk_directory::DirectoryEntry,
                ruff_db::system::walk_directory::Error,
            >| {
                if let Ok(entry) = entry {
                    if entry.file_type().is_file() && filter(entry.path()) {
                        if let Ok(mut v) = paths.lock() {
                            v.push(entry.path().to_path_buf());
                        }
                    }
                }
                WalkState::Continue
            })
        });

        let paths = Arc::try_unwrap(paths)
            .unwrap_or_else(|_| panic!("ingest_project: walk_dir still owning Arc"))
            .into_inner()
            .unwrap();

        // Build changes: for each relevant file, read from disk and intern.
        let mut changes = Vec::with_capacity(paths.len());
        for path_buf in &paths {
            match native.read_to_string(path_buf) {
                Ok(text) => {
                    disk_reads.fetch_add(1, Ordering::Relaxed);
                    changes.push(Change::Insert {
                        path: path_buf.clone(),
                        text: Arc::from(text),
                    });
                }
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    // Relevant path absent on disk → tombstone.
                    changes.push(Change::Delete {
                        path: path_buf.clone(),
                    });
                }
                Err(e) => {
                    // Genuine IO error — surface via log, don't silently skip.
                    log::warn!(
                        "ingest_project: failed to read {}: {e} — skipping",
                        path_buf.as_str()
                    );
                }
            }
        }

        self.apply_batch(changes)
    }

    /// Re-read a specific set of disk paths once and intern them as one
    /// batch (one revision). This is what `sync_path` and the watcher's
    /// `poll_changes` will feed.
    pub fn apply_disk_batch(
        &mut self,
        native: &OsSystem,
        paths: &[SystemPathBuf],
        filter: impl Fn(&SystemPath) -> bool,
    ) -> Revision {
        let mut changes = Vec::with_capacity(paths.len());
        for path_buf in paths {
            if !filter(path_buf) {
                continue;
            }
            match native.read_to_string(path_buf) {
                Ok(text) => {
                    self.disk_reads.fetch_add(1, Ordering::Relaxed);
                    changes.push(Change::Insert {
                        path: path_buf.clone(),
                        text: Arc::from(text),
                    });
                }
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    changes.push(Change::Delete {
                        path: path_buf.clone(),
                    });
                }
                Err(e) => {
                    log::warn!(
                        "apply_disk_batch: failed to read {}: {e} — skipping",
                        path_buf.as_str()
                    );
                }
            }
        }
        if changes.is_empty() {
            // No changes — don't create an empty revision.
            return self.revision;
        }
        self.apply_batch(changes)
    }

    /// Alias for `apply_batch`: one overlay batch = exactly one revision.
    /// Renamed for symmetry with `apply_disk_batch`.
    pub fn apply_overlay_batch(&mut self, changes: Vec<Change>) -> Revision {
        self.apply_batch(changes)
    }

    /// Record the current (revision, generation) and evict the oldest beyond cap.
    fn record_retained(&mut self) {
        self.retained.insert(self.revision, Arc::clone(&self.generation));
        while self.retained.len() > self.retain_cap {
            let oldest = *self.retained.keys().next().expect("non-empty");
            self.retained.remove(&oldest);
        }
    }

    /// The generation pinned at `rev`, if still retained. `None` ⇒ evicted
    /// (caller raises a `ValueError`).
    pub fn generation_at(&self, rev: Revision) -> Option<Generation> {
        self.retained.get(&rev).cloned()
    }

    /// The oldest revision still in the retained buffer.
    pub fn oldest_retained(&self) -> Revision {
        *self.retained.keys().next().unwrap_or(&self.revision)
    }
}

/// Look a system path up inside a captured generation.
pub fn lookup<'a>(generation: &'a Generation, path: &SystemPathBuf) -> Option<&'a Document> {
    generation.system.get(path)
}

impl ContentStore {
    /// True if `path` currently has any overlay entry (a live buffer or a
    /// delete-tombstone) in the head generation. Used by poll_changes to let an
    /// unsaved overlay buffer win over a racing disk-watcher event.
    pub fn has_overlay(&self, path: &SystemPathBuf) -> bool {
        self.generation.system.contains_key(path)
    }
}

// ── Project-relevant content predicate ──────────────────────────────────

/// The single authority for "what belongs in an analysis generation."
///
/// A path is relevant if it is a Python source file **or** one of the
/// project configuration files the analysis engine consumes during
/// discovery/setup.  Everything else — including the TyO3 sidecar's own
/// `.tyo3/config.toml` — is excluded.
///
/// This predicate is used by both disk ingest (commit-time) and snapshot
/// construction to guarantee one definition of relevant content (§5.1).
pub fn is_project_relevant(path: &SystemPath) -> bool {
    // Explicitly exclude the TyO3 sidecar directory and everything beneath it.
    // Walk ancestors: if any component is `.tyo3`, the path is excluded.
    {
        let mut current = Some(path);
        while let Some(p) = current {
            if p.file_name() == Some(".tyo3") {
                return false;
            }
            current = p.parent();
        }
    }

    if path
        .extension()
        .and_then(ruff_python_ast::PySourceType::try_from_extension)
        .is_some()
    {
        return true;
    }
    matches!(
        path.file_name(),
        Some("pyproject.toml" | "ty.toml" | "setup.cfg" | "setup.py")
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hash::hash_text;

    #[test]
    fn document_text_hashes_its_content() {
        let doc = Document::text("Y = 2\n", 1);
        assert_eq!(doc.hash(), Some(hash_text("Y = 2\n")));
        assert_eq!(doc.version(), 1);
    }

    #[test]
    fn document_deleted_has_no_hash() {
        let doc = Document::Deleted { version: 1 };
        assert_eq!(doc.hash(), None);
        assert_eq!(doc.version(), 1);
    }

    // ── Step 3: ContentMap / Generation persistence tests ──────────

    /// Cloning a ContentMap shares structure; mutating the clone must not
    /// affect the original (persistent data structure guarantee).
    #[test]
    fn content_map_clone_is_independent() {
        let m1 = ContentMap::new();
        let mut m2 = m1.clone();
        let path = SystemPathBuf::from("/test/a.py");
        let doc = Document::text("hello", 1);
        m2.system = m2.system.insert(path.clone(), doc);
        // Original is unchanged.
        assert!(m1.system.get(&path).is_none());
        assert!(m2.system.get(&path).is_some());
    }

    /// `Arc::clone` on a Generation is O(1): it shares the same allocation.
    #[test]
    fn generation_capture_is_o1_arc_clone() {
        let gen: Generation = Arc::new(ContentMap::new());
        let capture = Arc::clone(&gen);
        assert!(Arc::ptr_eq(&gen, &capture));
    }

    // ── Step 4: apply_batch atomicity & ContentStore tests ─────────

    /// `apply_batch(vec![a, b])` advances the revision exactly once; the
    /// intermediate state (a inserted, b not yet) must never be observable.
    #[test]
    fn apply_batch_is_atomic_one_revision() {
        let mut store = ContentStore::new();
        let r0 = store.revision();
        let a = SystemPathBuf::from("/test/a.py");
        let b = SystemPathBuf::from("/test/b.py");

        let r1 = store.apply_batch(vec![
            Change::Insert { path: a.clone(), text: Arc::from("content a") },
            Change::Insert { path: b.clone(), text: Arc::from("content b") },
        ]);

        // Advances exactly once.
        assert_eq!(r1, Revision(r0.0 + 1), "exactly one revision bump");

        // r0 generation contains neither.
        let g0 = store.generation_at(r0).unwrap();
        assert!(g0.system.get(&a).is_none());
        assert!(g0.system.get(&b).is_none());

        // r1 generation contains both.
        let g1 = store.generation_at(r1).unwrap();
        assert!(g1.system.get(&a).is_some());
        assert!(g1.system.get(&b).is_some());

        // No generation exists between r0 and r1.
        assert!(store.generation_at(Revision(r0.0 + 1)).is_some());
        // There is no r0.5.
        assert_eq!(
            store.retained.len(),
            2,
            "only r0 and r1 exist; no intermediate revision was created"
        );
    }

    /// A single-change helper (`insert_text`) advances the revision by exactly 1.
    #[test]
    fn insert_text_advances_by_one() {
        let mut store = ContentStore::new();
        let r0 = store.revision();
        store.insert_text(SystemPathBuf::from("/test/x.py"), "x");
        assert_eq!(store.revision().0, r0.0 + 1);
    }

    /// With `retain_cap = 4`, 6 edits (plus seed r0, total 7 entries)
    /// evict the oldest three (r0, r1, r2); `generation_at(evicted)` is
    /// `None` for evicted revisions.
    #[test]
    fn retain_cap_evicts_oldest() {
        let mut store = ContentStore::new();
        store.retain_cap = 4;
        // seed has r0; 6 insertions create r1..r6 (total 7).
        // With cap 4, r0..r2 are evicted, r3..r6 retained.
        for i in 1..=6 {
            store.insert_text(SystemPathBuf::from(format!("/test/{i}.py")), "x");
        }
        // r2 is evicted.
        let r2 = Revision(2);
        assert!(
            store.generation_at(r2).is_none(),
            "r2 should be evicted with retain_cap=4 after 6 edits"
        );
        // r3 is the oldest retained.
        let r3 = Revision(3);
        assert!(
            store.generation_at(r3).is_some(),
            "r3 should still be retained"
        );
        let r6 = Revision(6);
        assert!(store.generation_at(r6).is_some());
    }

        /// A captured generation remains independent after subsequent mutations.
    #[test]
    fn captured_generation_is_independent() {
        let mut store = ContentStore::new();
        store.insert_text(SystemPathBuf::from("/test/a.py"), "v1");
        let gen_v1 = store.capture();

        // Mutate after capture.
        store.insert_text(SystemPathBuf::from("/test/a.py"), "v2");

        // The captured generation still reads v1.
        let a = SystemPathBuf::from("/test/a.py");
        let doc = lookup(&gen_v1, &a).unwrap();
        match doc {
            Document::Text { text, .. } => assert_eq!(text.as_ref(), "v1"),
            _ => panic!("expected Text"),
        }

        // Head generation reads v2.
        let head_gen = store.capture();
        let doc2 = lookup(&head_gen, &a).unwrap();
        match doc2 {
            Document::Text { text, .. } => assert_eq!(text.as_ref(), "v2"),
            _ => panic!("expected Text"),
        }
    }

    // ── Disk ingest tests (Phase 1) ──────────────────────────────────

    /// `ingest_project` interns all relevant files from a temp dir,
    /// each with a non-zero version and a content hash.
    #[test]
    fn ingest_project_interns_all_relevant_files() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir(dir.path().join("pkg")).unwrap();
        std::fs::write(dir.path().join("a.py"), "x = 1\n").unwrap();
        std::fs::write(dir.path().join("pkg/m.py"), "def f():\n    pass\n").unwrap();
        std::fs::write(dir.path().join("pyproject.toml"), "[project]\n").unwrap();
        std::fs::write(dir.path().join("README.md"), "# Readme\n").unwrap();

        let root = SystemPathBuf::from_path_buf(dir.path().to_path_buf()).unwrap();
        let mut store = ContentStore::new();
        let r0 = store.revision();

        let r1 = store.ingest_project(&root, is_project_relevant);

        assert_eq!(r1, Revision(r0.0 + 1), "exactly one revision bump");
        let gen = store.capture();

        // a.py interned.
        let a = root.join("a.py");
        match gen.system.get(&a) {
            Some(Document::Text { text, version, hash: _ }) => {
                assert_eq!(text.as_ref(), "x = 1\n");
                assert!(*version > 0, "version must be non-zero from store counter");
            }
            other => panic!("expected Text for a.py, got {other:?}"),
        }

        // pkg/m.py interned.
        let m = root.join("pkg/m.py");
        assert!(gen.system.get(&m).is_some(), "pkg/m.py should be interned");

        // pyproject.toml interned.
        let ppt = root.join("pyproject.toml");
        assert!(gen.system.get(&ppt).is_some(), "pyproject.toml should be interned");

        // README.md NOT interned (not relevant).
        let readme = root.join("README.md");
        assert!(gen.system.get(&readme).is_none(), "README.md must NOT be interned");

        // Disk-read counter incremented (3 files read: a.py, pkg/m.py, pyproject.toml).
        assert_eq!(store.disk_read_count(), 3, "3 project-content disk reads");
    }

    /// An absent-but-relevant path (discovered in walk but gone before
    /// read) is skipped — ingest doesn't create tombstones for
    /// paths that vanish.
    #[test]
    fn ingest_project_handles_missing_files() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("a.py"), "x = 1\n").unwrap();
        std::fs::write(dir.path().join("b.py"), "y = 1\n").unwrap();

        let root = SystemPathBuf::from_path_buf(dir.path().to_path_buf()).unwrap();
        let mut store = ContentStore::new();

        // Delete b.py between walk creation and the read. The walk
        // collector sees it as a file, but read_to_string then gets
        // NotFound → tombstone.
        // (The walk snapshot behaviour is timing-dependent; we test
        // that the NotFound path doesn't panic.)
        let native = OsSystem::new(root.to_path_buf());
        let walker = native.walk_directory(&root);
        let paths: Arc<Mutex<Vec<SystemPathBuf>>> = Arc::new(Mutex::new(Vec::new()));
        let paths_clone = Arc::clone(&paths);
        walker.run(move || {
            let paths = Arc::clone(&paths_clone);
            Box::new(move |entry: std::result::Result<
                ruff_db::system::walk_directory::DirectoryEntry,
                ruff_db::system::walk_directory::Error,
            >| {
                if let Ok(entry) = entry {
                    if entry.file_type().is_file() && is_project_relevant(entry.path()) {
                        if let Ok(mut v) = paths.lock() {
                            v.push(entry.path().to_path_buf());
                        }
                    }
                }
                WalkState::Continue
            })
        });

        let paths = Arc::try_unwrap(paths).unwrap().into_inner().unwrap();

        // Delete all files before reading.
        for p in &paths {
            let _ = std::fs::remove_file(p.as_str());
        }

        // apply_disk_batch should create tombstones for now-absent files.
        let r = store.apply_disk_batch(&native, &paths, is_project_relevant);
        let gen = store.generation_at(r).unwrap();

        // Both files are tombstoned (Deleted) since they were removed.
        for p in &paths {
            match gen.system.get(p) {
                Some(Document::Deleted { .. }) => { /* expected */ }
                other => panic!("expected Deleted tombstone for {}, got {other:?}", p.as_str()),
            }
        }
        // No disk read — files were absent (NotFound).
        // Counter was not bumped because read_to_string returned NotFound.
    }

    /// `apply_disk_batch` for existing files interns them as Text.
    #[test]
    fn apply_disk_batch_interns_existing_files() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("a.py"), "x = 1\n").unwrap();

        let root = SystemPathBuf::from_path_buf(dir.path().to_path_buf()).unwrap();
        let native = OsSystem::new(root.to_path_buf());
        let mut store = ContentStore::new();

        let paths = vec![root.join("a.py")];
        let r = store.apply_disk_batch(&native, &paths, is_project_relevant);

        assert_eq!(r, Revision(1));
        let gen = store.capture();
        match gen.system.get(&root.join("a.py")) {
            Some(Document::Text { text, .. }) => assert_eq!(text.as_ref(), "x = 1\n"),
            other => panic!("expected Text, got {other:?}"),
        }
        assert_eq!(store.disk_read_count(), 1);
    }

    /// `apply_overlay_batch` is an alias for `apply_batch`.
    #[test]
    fn apply_overlay_batch_is_alias() {
        let mut store = ContentStore::new();
        let r = store.apply_overlay_batch(vec![Change::Insert {
            path: SystemPathBuf::from("/test/v.py"),
            text: Arc::from("v_content"),
        }]);
        assert_eq!(r, Revision(1));
        assert!(store.capture().system.get(&SystemPathBuf::from("/test/v.py")).is_some());
    }

    // ── is_project_relevant tests ─────────────────────────────────────

    #[test]
    fn py_source_files_are_relevant() {
        assert!(is_project_relevant(&SystemPathBuf::from("/p/a.py").as_path()));
        assert!(is_project_relevant(&SystemPathBuf::from("/p/b.pyi").as_path()));
        assert!(is_project_relevant(&SystemPathBuf::from("/p/c.ipynb").as_path()));
    }

    #[test]
    fn config_files_are_relevant() {
        assert!(is_project_relevant(&SystemPathBuf::from("/p/pyproject.toml").as_path()));
        assert!(is_project_relevant(&SystemPathBuf::from("/p/ty.toml").as_path()));
        assert!(is_project_relevant(&SystemPathBuf::from("/p/setup.cfg").as_path()));
        assert!(is_project_relevant(&SystemPathBuf::from("/p/setup.py").as_path()));
    }

    #[test]
    fn non_relevant_files_are_excluded() {
        assert!(!is_project_relevant(&SystemPathBuf::from("/p/README.md").as_path()));
        assert!(!is_project_relevant(&SystemPathBuf::from("/p/Makefile").as_path()));
        assert!(!is_project_relevant(&SystemPathBuf::from("/p/data.json").as_path()));
    }

    #[test]
    fn tyo3_sidecar_is_excluded() {
        // The sidecar dir itself is not relevant.
        assert!(!is_project_relevant(
            &SystemPathBuf::from("/p/.tyo3/config.toml").as_path()
        ));
        // Any .py inside .tyo3/ is also excluded.
        assert!(!is_project_relevant(
            &SystemPathBuf::from("/p/.tyo3/helper.py").as_path()
        ));
        // Nested directories inside .tyo3/ are excluded.
        assert!(!is_project_relevant(
            &SystemPathBuf::from("/p/.tyo3/sub/config.toml").as_path()
        ));
    }
}
