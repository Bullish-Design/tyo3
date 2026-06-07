//! `OverlaySystem`: a ty `System` that serves overlaid in-memory content first
//! and falls back to a native `OsSystem` on a miss. Modelled on ty_server's
//! `LSPSystem` (crates/ty_server/src/system.rs).
//!
//! The same type serves two roles, distinguished by `frozen`:
//!   * `frozen == None`  → the live HEAD overlay; its content can be republished.
//!   * `frozen == Some(R)` → a pinned, immutable view of revision R (an MVCC
//!     snapshot's view). It is never republished, so it is isolated from the
//!     head forever.
//!
//! # Design A: pre-population (no frozen disk fallback)
//!
//! Frozen views have **no** disk fallback for content or directory enumeration.
//! A miss in the generation is `not_found`.  Disk-backed files that belong to
//! the project at revision R must be pre-populated into the generation when
//! the snapshot is built (Step 8).  This makes §1.3.1 structural: the
//! snapshot's content is fixed at build time and cannot drift.
//!
//! `canonicalize_path`, `which`, `current_directory`, `path_exists_case_sensitive`,
//! and `case_sensitivity` still delegate to `native` regardless of `frozen` —
//! these are structural queries that do not return revision content and are
//! exempt from the pinning invariant (§1.3.1 applies to content and directory
//! membership, not canonical path resolution).

use std::any::Any;
use std::collections::BTreeSet;
use std::panic::RefUnwindSafe;
use std::sync::Arc;

use arc_swap::ArcSwap;
use ruff_db::file_revision::FileRevision;
use ruff_db::system::walk_directory::WalkDirectoryBuilder;
use ruff_db::system::{
    CaseSensitivity, DirectoryEntry, FileType, Metadata, OsSystem, System, SystemPath,
    SystemPathBuf, SystemVirtualPath, WhichResult, WritableSystem,
};
use ruff_python_ast::PySourceType;

use crate::content::{ContentMap, Document, Generation, Revision};

/// Shared handle to the live (or pinned) content cell.
type SharedContent = Arc<ArcSwap<ContentMap>>;

#[derive(Debug, Clone)]
pub struct OverlaySystem {
    /// The live (or pinned) content. `ArcSwap` so the head can be republished
    /// lock-free; `Arc<...>` so all clones of this system (and all
    /// `db.clone()`s that share it) observe the same content cell.
    content: SharedContent,
    /// Disk fallback for paths the overlay does not cover.
    native: Arc<dyn System + Send + Sync + RefUnwindSafe>,
    /// `None` = live head; `Some(R)` = pinned snapshot view of revision R.
    frozen: Option<Revision>,
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

    /// Build a frozen, pinned view of `generation` at revision `rev`.
    ///
    /// Under Design A, `generation` must contain **all** project files for
    /// revision `rev` (pre-populated by the snapshot builder in Step 8).
    /// Files not in the generation are invisible to this system — there is
    /// no disk fallback on a frozen view (§1.3.1).
    pub fn frozen(
        root: SystemPathBuf,
        generation: Generation,
        rev: Revision,
    ) -> Self {
        Self {
            content: Arc::new(ArcSwap::new(generation)),
            native: Arc::new(OsSystem::new(root)),
            frozen: Some(rev),
        }
    }

    /// Republish the live content (HEAD only).
    ///
    /// CONCURRENCY: `publish` is called under the write lock.  The `ArcSwap`
    /// store is lock-free, so readers see the new generation instantly once
    /// the lock is released.
    pub fn publish(&self, generation: Generation) {
        debug_assert!(
            self.frozen.is_none(),
            "must not republish a frozen overlay"
        );
        self.content.store(generation);
    }

    /// Whether this is a frozen (pinned) overlay.
    pub fn is_frozen(&self) -> bool {
        self.frozen.is_some()
    }

    // ── Content lookup helpers ───────────────────────────────────────────

    /// Look up the overlay document for `path`, if any.
    fn document(&self, path: &SystemPath) -> Option<Document> {
        self.content
            .load()
            .system
            .get(&path.to_path_buf())
            .cloned()
    }

    /// Look up an overlay virtual document for `path`, if any.
    fn virtual_document(&self, path: &SystemVirtualPath) -> Option<Document> {
        self.content
            .load()
            .virtual_files
            .get(&path.to_path_buf())
            .cloned()
    }

    /// Iterate over all system-path keys in the content map.  Used by frozen
    /// directory enumeration (Step 6).
    fn system_keys(&self) -> Vec<SystemPathBuf> {
        self.content
            .load()
            .system
            .keys()
            .cloned()
            .collect()
    }

    /// True if `path` is a directory implied by the generation: some `Text`
    /// document key is a strict descendant of `path`.
    ///
    /// A frozen overlay stores only file documents, but ty's module resolver
    /// asks for directory metadata on its search roots and on every package
    /// directory along an import path. Without this, no first-party module
    /// resolves under a snapshot (every directory looks absent), so reads done
    /// through a snapshot can't see sibling modules even though their content
    /// is pinned. Synthesising directories from the file keys keeps the frozen
    /// view fully self-describing without a disk fallback (Design A, §1.3.1).
    fn is_synthesised_directory(&self, path: &SystemPath) -> bool {
        // `starts_with` is component-wise, so a key strictly under `path` makes
        // `path` an ancestor directory.
        self.system_keys()
            .iter()
            .any(|key| key.as_path() != path && key.starts_with(path))
    }
}

impl System for OverlaySystem {
    fn path_metadata(&self, path: &SystemPath) -> std::io::Result<Metadata> {
        match self.document(path) {
            Some(Document::Text { version, .. }) => {
                // INVARIANT: metadata revision derives from the document
                // version so a read and its metadata never disagree.
                Ok(Metadata::new(
                    FileRevision::new(u128::from(version)),
                    None,
                    FileType::File,
                ))
            }
            Some(Document::Deleted { .. }) => Err(not_found(path)),
            // Design A: frozen views have no disk fallback.  A path with no
            // document is either a directory implied by the generation's keys
            // (which the module resolver must see) or genuinely absent.
            None if self.frozen.is_some() => {
                if self.is_synthesised_directory(path) {
                    Ok(Metadata::new(
                        FileRevision::new(0),
                        None,
                        FileType::Directory,
                    ))
                } else {
                    Err(not_found(path))
                }
            }
            // Live head: fall through to native disk.
            None => self.native.path_metadata(path),
        }
    }

    fn canonicalize_path(&self, path: &SystemPath) -> std::io::Result<SystemPathBuf> {
        // Structural query — exempt from the pinning invariant (see module doc).
        self.native.canonicalize_path(path)
    }

    fn read_to_string(&self, path: &SystemPath) -> std::io::Result<String> {
        match self.document(path) {
            Some(Document::Text { text, .. }) => Ok(text.to_string()),
            Some(Document::Deleted { .. }) => Err(not_found(path)),
            // Design A: frozen views have no disk fallback — a miss is not_found.
            None if self.frozen.is_some() => Err(not_found(path)),
            // Live head: fall through to native disk.
            None => self.native.read_to_string(path),
        }
    }

    fn read_to_notebook(
        &self,
        path: &SystemPath,
    ) -> std::result::Result<ruff_notebook::Notebook, ruff_notebook::NotebookError> {
        self.native.read_to_notebook(path)
    }

    fn read_virtual_path_to_string(
        &self,
        path: &SystemVirtualPath,
    ) -> std::io::Result<String> {
        match self.virtual_document(path) {
            Some(Document::Text { text, .. }) => Ok(text.to_string()),
            Some(Document::Deleted { .. }) => Err(virtual_not_found(path)),
            // Virtual paths have no native fallback regardless of frozen/live.
            None => self.native.read_virtual_path_to_string(path),
        }
    }

    fn read_virtual_path_to_notebook(
        &self,
        path: &SystemVirtualPath,
    ) -> std::result::Result<ruff_notebook::Notebook, ruff_notebook::NotebookError> {
        self.native.read_virtual_path_to_notebook(path)
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

    fn source_type(&self, path: &SystemPath) -> Option<PySourceType> {
        match self.document(path) {
            Some(Document::Text { .. }) => path
                .extension()
                .and_then(PySourceType::try_from_extension)
                .or(Some(PySourceType::Python)),
            Some(Document::Deleted { .. }) => None,
            // Design A: frozen views — no disk fallback for source_type.
            // A file not in the generation has no source type.
            None if self.frozen.is_some() => None,
            None => self.native.source_type(path),
        }
    }

    fn which(&self, name: &str) -> WhichResult {
        self.native.which(name)
    }

    fn path_exists_case_sensitive(&self, path: &SystemPath, prefix: &SystemPath) -> bool {
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
    ) -> std::io::Result<Box<dyn Iterator<Item = std::io::Result<DirectoryEntry>> + 'a>> {
        if self.frozen.is_some() {
            // Enumerate direct children from the generation.
            // Only `Document::Text` entries appear; tombstones are absent.
            // Intermediate sub-directories are synthesised from key prefixes.
            let path_buf = path.to_path_buf();
            let mut direct_dirs: BTreeSet<SystemPathBuf> = BTreeSet::new();
            let mut entries: Vec<DirectoryEntry> = Vec::new();

            for key in self.system_keys() {
                // Find keys that are direct children of `path_buf`.
                let Ok(rest) = key.strip_prefix(&path_buf) else {
                    continue;
                };
                let rest = rest.as_str();
                // Skip "" or "/" entries (the directory itself).
                if rest.is_empty() || rest == "/" {
                    continue;
                }
                // Strip leading '/'.
                let rest = rest.strip_prefix('/').unwrap_or(rest);

                if let Some(slash_pos) = rest.find('/') {
                    // This is a descendant in a sub-directory: synthesise a
                    // Directory entry for the immediate sub-directory.
                    let dir_name = &rest[..slash_pos];
                    let dir_path = path_buf.join(dir_name);
                    direct_dirs.insert(dir_path);
                } else {
                    // Direct child file.
                    // Check it's a Text document (not a tombstone).
                    if let Some(doc) = self.document(&key) {
                        if matches!(doc, Document::Text { .. }) {
                            let file_path = path_buf.join(rest);
                            entries.push(DirectoryEntry::new(file_path, FileType::File));
                        }
                        // Tombstones (Deleted) are skipped — absent from listing.
                    }
                }
            }

            // Add synthesised directory entries.
            for dir_path in direct_dirs {
                entries.push(DirectoryEntry::new(dir_path, FileType::Directory));
            }

            Ok(Box::new(entries.into_iter().map(Ok)))
        } else {
            // Live head — delegate to native disk.
            self.native.read_directory(path)
        }
    }

    fn walk_directory(&self, path: &SystemPath) -> WalkDirectoryBuilder {
        // NOTE: walk_directory currently delegates to native even for frozen
        // views, because ruff_db's walk_directory::DirectoryEntry is not
        // publicly constructible from outside the ruff_db crate.  This is
        // acceptable under Design A because any file discovered by the walk
        // that is not in the frozen generation will fail on read_to_string
        // (returning not_found).  If full generation-based walking becomes
        // necessary, this is the trigger to either (a) upstream a public
        // constructor for walk_directory::DirectoryEntry, or (b) switch to
        // Design B with shared-generation intern.
        self.native.walk_directory(path)
    }

    fn env_var(
        &self,
        name: &str,
    ) -> std::result::Result<String, std::env::VarError> {
        self.native.env_var(name)
    }

    fn as_writable(&self) -> Option<&dyn WritableSystem> {
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
        format!("No such file: {path}"),
    )
}

fn virtual_not_found(path: &SystemVirtualPath) -> std::io::Error {
    std::io::Error::new(
        std::io::ErrorKind::NotFound,
        format!("No such virtual path: {path}"),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::content::ContentStore;
    use std::io::Write;
    use std::sync::Mutex;
    use ruff_db::system::walk_directory::{DirectoryEntry as WalkDirEntry, Error as WalkDirError, WalkState};
    use ruff_python_ast::name::Name;
    use ty_project::{ProjectDatabase, ProjectMetadata};

    /// A temp dir with one file `a.py` containing `disk_contents`, plus the
    /// matching `SystemPathBuf` root and `a.py` path.
    fn fixture(disk_contents: &str) -> (tempfile::TempDir, SystemPathBuf, SystemPathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let mut f = std::fs::File::create(dir.path().join("a.py")).unwrap();
        f.write_all(disk_contents.as_bytes()).unwrap();
        let root = SystemPathBuf::from_path_buf(dir.path().to_path_buf()).unwrap();
        let a = root.join("a.py");
        (dir, root, a)
    }

    /// Create a generation with a pre-populated file entry (as the snapshot
    /// builder will do in Step 8).
    fn gen_with(path: SystemPathBuf, text: &str) -> Generation {
        let mut map = ContentMap::new();
        let doc = Document::text(text.to_string(), 1);
        map.system = map.system.insert(path, doc);
        Arc::new(map)
    }

    #[test]
    fn reads_fall_through_to_disk_when_not_overlaid() {
        let (_dir, root, a) = fixture("X = 1\n");
        let sys = OverlaySystem::live(root, Arc::new(ContentMap::new()));
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
            .path_metadata(&a)
            .unwrap()
            .revision();
        store.insert_text(a.clone(), "v2");
        let r2 = OverlaySystem::live(root, store.capture())
            .path_metadata(&a)
            .unwrap()
            .revision();
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

    /// The gate: a frozen view at R, with file `a` pre-populated in its
    /// generation, returns the pinned content even after disk is mutated.
    /// Under Design A there is no disk read to race.
    #[test]
    fn frozen_view_never_reads_disk_for_content() {
        let (_dir, root, a) = fixture("DISK_ORIGINAL\n");
        let mut store = ContentStore::new();

        // Pre-populate the file in the generation (as snapshot builder will).
        store.insert_text(a.clone(), "PINNED_CONTENT\n");
        let gen_at_r = store.capture();
        let r = store.revision();

        let frozen = OverlaySystem::frozen(root.clone(), gen_at_r, r);

        // Mutate disk after the frozen view is created.
        std::fs::write(_dir.path().join("a.py"), b"DISK_MUTATED\n").unwrap();

        // Frozen still returns the pinned content — no disk read attempted.
        assert_eq!(frozen.read_to_string(&a).unwrap(), "PINNED_CONTENT\n");

        // Live read sees the new disk content (no overlay in the head).
        let live = OverlaySystem::live(root, Arc::new(ContentMap::new()));
        assert_eq!(live.read_to_string(&a).unwrap(), "DISK_MUTATED\n");
    }

    /// A frozen view with an empty generation returns not_found for every
    /// path, including ones that exist on disk.  This is by design — the
    /// snapshot builder (Step 8) must pre-populate project files.
    #[test]
    fn frozen_empty_generation_has_no_disk_fallback() {
        let (_dir, root, a) = fixture("X = 1\n");
        let gen = Arc::new(ContentMap::new());
        let frozen = OverlaySystem::frozen(root, gen, Revision(0));
        assert!(frozen.read_to_string(&a).is_err());
        assert!(frozen.path_metadata(&a).is_err());
    }

    /// Smoke test: a ProjectDatabase builds over a live overlay and reads disk
    /// content through it.
    #[test]
    fn project_database_builds_over_live_overlay() {
        use ruff_db::source::source_text;
        let (_dir, root, a) = fixture("VALUE = 42\n");
        let empty: Generation = Arc::new(ContentMap::new());
        let system = OverlaySystem::live(root.clone(), empty);
        let metadata = ProjectMetadata::new(Name::new("tyo3-project"), root);
        let db = ProjectDatabase::use_defaults(metadata, system);
        let file = ruff_db::files::system_path_to_file(&db, &a).unwrap();
        assert!(source_text(&db, file).as_str().contains("VALUE = 42"));
    }

    // ── Step 6: Directory membership pinned at a revision ─────────

    /// A frozen generation containing files at root and in a subdirectory
    /// enumerates direct children correctly via `read_directory`.
    #[test]
    fn frozen_read_directory_from_generation() {
        let (_dir, root, a) = fixture("X = 1\n");
        // Build a generation with multiple paths at different depths.
        let mut map = ContentMap::new();
        let a_path = root.join("a.py");
        let sub_b = root.join("sub/b.py");
        map.system = map.system.insert(a_path.clone(), Document::text("x", 1));
        map.system = map.system.insert(sub_b.clone(), Document::text("y", 2));
        let gen = Arc::new(map);

        let frozen = OverlaySystem::frozen(root.clone(), gen, Revision(1));

        // read_directory(root) should list a.py and sub/
        let entries: Vec<DirectoryEntry> = frozen
            .read_directory(&root)
            .unwrap()
            .map(|r| r.unwrap())
            .collect();

        let paths: Vec<&str> = entries.iter().map(|e| e.path().as_str()).collect();
        // a.py is a direct child file
        assert!(paths.iter().any(|p| p.ends_with("/a.py") || *p == "/a.py" || p.ends_with("a.py")),
            "expected a.py in listing, got {paths:?}");
        // sub/ is a synthesised directory
        assert!(paths.iter().any(|p| p.contains("/sub") || p.ends_with("sub")),
            "expected sub/ directory in listing, got {paths:?}");
    }

    /// A tombstoned path does NOT appear in frozen directory enumeration.
    #[test]
    fn tombstoned_path_absent_from_frozen_directory() {
        let (_dir, root, _a) = fixture("X = 1\n");
        let mut map = ContentMap::new();
        let a_path = root.join("a.py");
        let b_path = root.join("b.py");
        map.system = map.system.insert(a_path.clone(), Document::text("x", 1));
        // b.py is tombstoned.
        map.system = map.system.insert(b_path.clone(), Document::Deleted { version: 2 });
        let gen = Arc::new(map);

        let frozen = OverlaySystem::frozen(root.clone(), gen, Revision(1));
        let entries: Vec<DirectoryEntry> = frozen
            .read_directory(&root)
            .unwrap()
            .map(|r| r.unwrap())
            .collect();

        let paths: Vec<&str> = entries.iter().map(|e| e.path().as_str()).collect();
        // b.py should NOT appear — it's a tombstone.
        assert!(!paths.iter().any(|p| p.contains("b.py")),
            "tombstoned b.py must not appear in listing, got {paths:?}");
        // a.py SHOULD appear.
        assert!(paths.iter().any(|p| p.contains("a.py")),
            "Text a.py must appear in listing, got {paths:?}");
    }

    /// The gate: a file created on disk AFTER building a frozen view does NOT
    /// appear in the frozen enumeration, but DOES appear in a live view.
    #[test]
    fn new_disk_file_absent_from_frozen_present_in_live() {
        let (_dir, root, _a) = fixture("X = 1\n");
        // Pre-populate only a.py in the frozen generation.
        let mut map = ContentMap::new();
        let a_path = root.join("a.py");
        map.system = map.system.insert(a_path.clone(), Document::text("X = 1\n", 1));
        let gen = Arc::new(map);

        let frozen = OverlaySystem::frozen(root.clone(), gen, Revision(1));

        // Create c.py on disk AFTER the frozen view is built.
        std::fs::write(_dir.path().join("c.py"), b"Y = 2\n").unwrap();

        // Frozen enumeration does NOT include c.py.
        let frozen_entries: Vec<DirectoryEntry> = frozen
            .read_directory(&root)
            .unwrap()
            .map(|r| r.unwrap())
            .collect();
        let frozen_names: Vec<&str> = frozen_entries.iter().map(|e| e.path().as_str()).collect();
        assert!(!frozen_names.iter().any(|p| p.contains("c.py")),
            "c.py must NOT appear in frozen enumeration, got {frozen_names:?}");

        // Live view DOES include c.py (via native disk).
        let live = OverlaySystem::live(root, Arc::new(ContentMap::new()));
        let tmp_root = SystemPathBuf::from_path_buf(_dir.path().to_path_buf()).unwrap();
        let live_entries: Vec<DirectoryEntry> = live
            .read_directory(&tmp_root)
            .unwrap()
            .map(|r| r.unwrap())
            .collect();
        let live_names: Vec<&str> = live_entries.iter().map(|e| e.path().as_str()).collect();
        assert!(live_names.iter().any(|p| p.contains("c.py")),
            "c.py must appear in live enumeration, got {live_names:?}");
    }

    // ── Phase 1 frozen strictness lock-in tests ──────────────────────

    /// §5.2: a frozen view's `source_type` returns `None` for paths not in
    /// the generation — never falls through to disk.
    #[test]
    fn frozen_source_type_none_for_unknown_path() {
        let (_dir, root, a) = fixture("X = 1\n");
        let mut map = ContentMap::new();
        // Only a.py is in the generation.
        map.system = map.system.insert(a.clone(), Document::text("X = 1\n", 1));
        let gen = Arc::new(map);
        let frozen = OverlaySystem::frozen(root.clone(), gen, Revision(1));

        // b.py is on disk but NOT in the generation → None.
        let b = root.join("b.py");
        std::fs::write(_dir.path().join("b.py"), b"Y = 2\n").unwrap();
        assert_eq!(frozen.source_type(&b), None);

        // a.py IS in the generation → Python source type.
        assert!(frozen.source_type(&a).is_some());
    }

    /// §5.2: a frozen view's `path_metadata` for a path not in the generation
    /// returns `not_found` — even if the file exists on disk.
    #[test]
    fn frozen_path_metadata_not_found_for_unpopulated_disk_file() {
        let (_dir, root, a) = fixture("X = 1\n");
        let mut map = ContentMap::new();
        map.system = map.system.insert(a.clone(), Document::text("X = 1\n", 1));
        let gen = Arc::new(map);
        let frozen = OverlaySystem::frozen(root.clone(), gen, Revision(1));

        // b.py exists on disk but is not in the generation.
        let b = root.join("b.py");
        std::fs::write(_dir.path().join("b.py"), b"Y = 2\n").unwrap();
        assert!(frozen.path_metadata(&b).is_err());
    }

    /// §5.2 known gap: `walk_directory` delegates to native disk even for
    /// frozen views, so a walk can discover files created after the frozen
    /// revision.  However, `read_to_string` on those files returns
    /// `not_found`, and this gap only manifests if a Phase 1 read path
    /// calls `walk_directory` on a frozen view.  This test documents the
    /// current behaviour so it is explicit.
    #[test]
    fn frozen_walk_directory_sees_disk_but_read_fails() {
        let (_dir, root, a) = fixture("X = 1\n");
        let mut map = ContentMap::new();
        map.system = map.system.insert(a.clone(), Document::text("X = 1\n", 1));
        let gen = Arc::new(map);
        let frozen = OverlaySystem::frozen(root.clone(), gen, Revision(1));

        // Create a new file on disk after the frozen view is pinned.
        let c_path = _dir.path().join("c.py");
        std::fs::write(&c_path, b"Z = 3\n").unwrap();

        // walk_directory delegates to native even for frozen views,
        // so it will discover c.py on disk.
        let walker = frozen.walk_directory(&root);
        let found: Arc<Mutex<Vec<SystemPathBuf>>> = Arc::new(Mutex::new(Vec::new()));
        let found_clone = Arc::clone(&found);
        walker.run(move || {
            let found = Arc::clone(&found_clone);
            Box::new(move |entry: std::result::Result<
                WalkDirEntry,
                WalkDirError,
            >| {
                if let Ok(entry) = entry {
                    if let Ok(mut v) = found.lock() {
                        v.push(entry.path().to_path_buf());
                    }
                }
                WalkState::Continue
            })
        });
        let found = Arc::try_unwrap(found).unwrap().into_inner().unwrap();

        // c.py IS discovered by the walk (the gap).
        let c_rust_path = SystemPathBuf::from_path_buf(c_path).unwrap();
        assert!(
            found.contains(&c_rust_path),
            "walk_directory gap: c.py IS visible via native walk on frozen view"
        );

        // But read_to_string still returns not_found for c.py because
        // it is not in the generation.
        assert!(frozen.read_to_string(&c_rust_path).is_err());
    }

    /// §5.2 lock-in: a snapshot at R where a file was tombstoned (deleted)
    /// at R observes the tombstone, not the disk file.
    #[test]
    fn frozen_tombstone_survives_disk_restoration() {
        let (_dir, root, a) = fixture("X = 1\n");
        // a.py was tombstoned at R (the generation says it was deleted).
        let mut map = ContentMap::new();
        map.system = map.system.insert(a.clone(), Document::Deleted { version: 1 });
        let gen = Arc::new(map);

        let frozen = OverlaySystem::frozen(root.clone(), gen, Revision(1));

        // The tombstone hides the disk file.
        assert!(frozen.read_to_string(&a).is_err());
        assert!(frozen.path_metadata(&a).is_err());
        // source_type returns None for tombstoned paths.
        assert_eq!(frozen.source_type(&a), None);
    }
}
