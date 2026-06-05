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

    /// Apply `f` to a clone of the current `ContentMap`, swap the generation,
    /// and bump the application revision. Centralises the copy-on-write pattern.
    fn mutate(&mut self, f: impl FnOnce(&mut ContentMap)) -> Revision {
        let mut map = (*self.generation).clone();
        f(&mut map);
        self.generation = Arc::new(map);
        self.revision = Revision(self.revision.0 + 1);
        self.record_retained();
        self.revision
    }

    // ── System-path overlay methods ─────────────────────────────────────

    /// Overlay `path` with in-memory text. Returns the new revision.
    pub fn insert_text(&mut self, path: SystemPathBuf, text: impl Into<Arc<str>>) -> Revision {
        let version = self.next_version();
        let doc = Document::Text {
            text: text.into(),
            version,
        };
        self.mutate(|m| {
            m.system = m.system.insert(path, doc);
        })
    }

    /// Overlay `path` as deleted. Returns the new revision.
    pub fn delete(&mut self, path: SystemPathBuf) -> Revision {
        let version = self.next_version();
        self.mutate(|m| {
            m.system = m.system.insert(path, Document::Deleted { version });
        })
    }

    /// Drop any overlay for `path` (revert to whatever disk says). Returns the
    /// new revision.
    pub fn forget(&mut self, path: &SystemPathBuf) -> Revision {
        let path = path.clone();
        self.mutate(|m| {
            m.system = m.system.remove(&path);
        })
    }

    // ── Virtual-path overlay methods (Phase 3) ──────────────────────────

    /// Overlay a virtual path (e.g. "untitled:1") with in-memory text.
    pub fn insert_virtual(
        &mut self,
        path: SystemVirtualPathBuf,
        text: impl Into<Arc<str>>,
    ) -> Revision {
        let version = self.next_version();
        let doc = Document::Text {
            text: text.into(),
            version,
        };
        self.mutate(|m| {
            m.virtual_files = m.virtual_files.insert(path, doc);
        })
    }

    /// Drop any overlay for a virtual path.
    pub fn forget_virtual(&mut self, path: &SystemVirtualPathBuf) -> Revision {
        let path = path.clone();
        self.mutate(|m| {
            m.virtual_files = m.virtual_files.remove(&path);
        })
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
