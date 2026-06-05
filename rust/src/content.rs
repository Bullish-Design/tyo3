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
        let doc = Document::Text {
            text: text.into(),
            version,
        };
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
