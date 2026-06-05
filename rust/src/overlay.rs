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
    /// lock-free in Phase 3; `Arc<...>` so all clones of this system (and all
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

    /// Build a frozen, pinned view of `generation` at revision `rev`. Used by the
    /// snapshot path in Phase 4; included now so the isolation test can exist.
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

    /// Republish the live content (HEAD only). No-op-able in Phase 1; exercised
    /// in Phase 3.
    pub fn publish(&self, generation: Generation) {
        debug_assert!(
            self.frozen.is_none(),
            "must not republish a frozen overlay"
        );
        self.content.store(generation);
    }

    pub fn is_frozen(&self) -> bool {
        self.frozen.is_some()
    }

    /// Snapshot-look-up the overlay document for `path`, if any.
    fn document(&self, path: &SystemPath) -> Option<Document> {
        // `load()` is a cheap RCU read; clone the small `Document` (Arc<str> inside).
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
}

impl System for OverlaySystem {
    fn path_metadata(&self, path: &SystemPath) -> std::io::Result<Metadata> {
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

    fn canonicalize_path(&self, path: &SystemPath) -> std::io::Result<SystemPathBuf> {
        self.native.canonicalize_path(path)
    }

    fn read_to_string(&self, path: &SystemPath) -> std::io::Result<String> {
        match self.document(path) {
            Some(Document::Text { text, .. }) => Ok(text.to_string()),
            Some(Document::Deleted { .. }) => Err(not_found(path)),
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
        self.native.read_directory(path)
    }

    fn walk_directory(&self, path: &SystemPath) -> WalkDirectoryBuilder {
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
        format!("No such file (overlaid as deleted): {path}"),
    )
}

fn virtual_not_found(path: &SystemVirtualPath) -> std::io::Error {
    std::io::Error::new(
        std::io::ErrorKind::NotFound,
        format!("No such virtual path (overlaid as deleted): {path}"),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::content::ContentStore;
    use std::io::Write;
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

    #[test]
    fn reads_fall_through_to_disk_when_not_overlaid() {
        let (_dir, root, a) = fixture("X = 1\n");
        let sys = OverlaySystem::live(
            root,
            Arc::new(ContentMap::new()),
        );
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

    /// Smoke test: a ProjectDatabase builds over the overlay and reads disk
    /// content through it.
    #[test]
    fn project_database_builds_over_overlay() {
        use ruff_db::source::source_text;
        let (_dir, root, a) = fixture("VALUE = 42\n");
        let empty: Generation = Arc::new(ContentMap::new());
        let system = OverlaySystem::live(root.clone(), empty);
        let metadata = ProjectMetadata::new(Name::new("tyo3-project"), root);
        let db = ProjectDatabase::use_defaults(metadata, system);
        let file =
            ruff_db::files::system_path_to_file(&db, &a).unwrap();
        assert!(source_text(&db, file).as_str().contains("VALUE = 42"));
    }
}
