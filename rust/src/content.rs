//! Immutable, revisioned content store backing the overlay system.
//!
//! A `Generation` is an immutable snapshot of all overlaid document content at
//! one revision. Because it is a persistent map, producing the next generation
//! (insert/delete) shares structure with the previous one, and capturing a
//! generation (for a snapshot) is an O(1) `Arc` clone.

use std::collections::BTreeMap;
use std::sync::Arc;

use ruff_db::system::{SystemPathBuf, SystemVirtualPathBuf};
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
#[derive(Debug)]
pub struct ContentStore {
    generation: Generation,
    revision: Revision,
    version_counter: u64,
    /// Recent (revision → generation) for `snapshot(at=r)` time-travel. Bounded;
    /// oldest entries evicted past `retain_cap`.
    retained: BTreeMap<Revision, Generation>,
    retain_cap: usize,
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
}
