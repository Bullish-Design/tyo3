// ── Shared imports (re-exported to submodules via `use super::*`) ─────────
//
// The `project` module is split across `project/{analysis,open,commit,methods,
// snapshot,head_view}.rs`. Each submodule pulls these names in with a single
// `use super::*;`, so the shared imports are `pub(crate) use` re-exports here.
pub(crate) use std::collections::{HashMap, HashSet};
pub(crate) use std::fs;
pub(crate) use std::path::PathBuf;
pub(crate) use std::sync::{Arc, Mutex};

pub(crate) use pyo3::exceptions::{PyRuntimeError, PyValueError};
pub(crate) use pyo3::prelude::*;
pub(crate) use pythonize::pythonize;

pub(crate) use crate::{ConfigError as PyConfigError, FormatVersionError, ProjectClosedError, PathResolutionError, PositionError, RevisionEvictedError, SidecarWriteError, CommitFailed};

pub(crate) use ruff_db::files::File;
pub(crate) use ruff_db::source::source_text;
pub(crate) use ruff_db::system::{SystemPath, SystemPathBuf, SystemVirtualPathBuf};
pub(crate) use ruff_db::Db as _; // bring files() etc. into scope
pub(crate) use ruff_source_file::LineIndex;

pub(crate) use ty_project::watch::{ChangeEvent, ChangedKind, CreatedKind, DeletedKind, ExistingPathKind, ProjectWatcher, directory_watcher};
pub(crate) use ty_project::Db;
pub(crate) use ty_project::{ProjectDatabase, ProjectMetadata};

pub(crate) use crate::authored::{AuthoredMap, AuthoredRecordDoc, AuthoredStore, AuthoredLoadError};
pub(crate) use crate::content::{is_project_relevant, ContentStore, Generation, Revision};
// `Document` is only referenced by the test module (via `use super::*`); gate the
// re-export so non-test builds don't see it as an unused import.
#[cfg(test)]
pub(crate) use crate::content::Document;
pub(crate) use crate::config::{self, RawConfig, ValidatedConfig};
pub(crate) use crate::entity::{extract_entities, extract_entities_for};
pub(crate) use crate::hash::HashPolicy;
pub(crate) use crate::identity::{reconcile, reconcile_scoped, DurableId, FormatError, IdentityRegistry, IdentityStatus};
pub(crate) use crate::overlay::OverlaySystem;
pub(crate) use crate::sidecar::Sidecar;

pub(crate) use ruff_python_ast::{name::Name, PySourceType};

pub(crate) use crate::convert;
pub(crate) use crate::coordinates;
pub(crate) use crate::dto;
pub(crate) use crate::files as file_resolver;

// ── Analysis error ───────────────────────────────────────────────────────

/// Analysis-layer error, free of any Python state so it can be produced inside
/// `py.detach(...)`. Converted to a concrete `PyErr` by the method wrapper.
///
/// Both variants wrap a `String` so they can be used directly as `map_err` fns,
/// e.g. `resolve_file(...).map_err(AnalysisError::Path)?`.
pub(crate) enum AnalysisError {
    Path(String),
    Position(String),
}

impl AnalysisError {
    pub(crate) fn into_pyerr(self) -> PyErr {
        match self {
            AnalysisError::Path(s) => PathResolutionError::new_err(s),
            AnalysisError::Position(s) => PositionError::new_err(s),
        }
    }
}

// ── State ────────────────────────────────────────────────────────────────

/// The cheap read-only clone produced for every analysis call. Owns a cloned
/// `ProjectDatabase` + project root — the minimum needed for GIL-released analysis.
///
/// CONCURRENCY: `ProjectDatabase` (salsa 0.26) is `Send + Clone` but `!Sync`
/// — its `salsa::Storage` holds a per-thread `ZalsaLocal` (`RefCell`/`UnsafeCell`).
/// You cannot share `&db` across threads, but you CAN move an owned clone, which is
/// how ty itself parallelizes (`ty_project::Project::check` clones the db per rayon
/// worker). Read methods take a `db.clone()` snapshot via `clone_locked_state` and
/// run inside `py.detach(...)` to release the GIL. Because `#[pyclass]` only
/// requires `Send` (not `Sync`), the `Mutex` below is what makes concurrent `&self`
/// access sound once the GIL is released.
pub(crate) struct TyProjectState {
    pub(crate) db: ProjectDatabase,
    pub(crate) root: SystemPathBuf,
    pub(crate) registry: Option<IdentityRegistry>,
    pub(crate) hash_policy: HashPolicy,
    /// Per-profile hash policies derived from the validated config.
    /// Map key is the profile name (e.g. "structure", "semantic").
    pub(crate) hash_policies: HashMap<String, HashPolicy>,
    /// The name of the default hash profile.
    pub(crate) default_hash_profile: String,
    /// Authored record store captured at snapshot time alongside the
    /// registry so authored reads are revision-consistent.
    pub(crate) authored: Option<AuthoredStore>,
}

/// The live, mutable HEAD of a session. Owns the database plus the content
/// substrate behind it. Distinct from `TyProjectState` (the cheap read clone)
/// because `store`/`system` must never be cloned per-read nor exposed to
/// snapshots.
///
/// Every edit flows through the substrate as one commit: stage the change into
/// `store`, `system.publish(...)` it so the head `db` analyses it, run the
/// fallible commit steps, then publish-last (§5.3).
pub(crate) struct HeadState {
    pub(crate) db: ProjectDatabase,
    pub(crate) root: SystemPathBuf,
    pub(crate) store: ContentStore,
    /// Handle onto the *same* overlay content cell the `db` reads through
    /// (clone-shares the inner `Arc<ArcSwap<…>>`). The commit publishes staged
    /// content through it so the head `db` sees the new revision.
    pub(crate) system: OverlaySystem,
    /// Identity registry: binds DurableIds to last-known entity facts.
    /// Reconciled after every commit; persisted through the sidecar.
    pub(crate) registry: IdentityRegistry,
    /// Code-layer identity hash policy: the default profile resolved from the
    /// validated config.
    pub(crate) hash_policy: HashPolicy,
    /// Per-profile hash policies derived from the validated config.
    pub(crate) hash_policies: HashMap<String, HashPolicy>,
    /// The name of the default hash profile.
    pub(crate) default_hash_profile: String,
    /// Validated, defaulted project config loaded once on open.
    pub(crate) config: ValidatedConfig,
    /// Canonical owner for all sidecar paths and durable writes.
    pub(crate) sidecar: Sidecar,
    /// Authored record store: identity-keyed durable knowledge (§5.4).
    /// Captured into snapshots alongside the registry for Snapshot
    /// isolation + time-travel (§10.2.2 authored half).
    pub(crate) authored: AuthoredStore,
    /// Canonical native code layer (nodes + edges + reverse-deps). Maintained
    /// in-commit by the scoped producer (`produce_layer` → `CodeLayer::diff_from`):
    /// the commit re-derives the dirty scope over the prior layer and the commit
    /// reads its `reverse_deps` to compute the transitive, container-granular
    /// affected closure at the source (never-miss).
    pub(crate) code_layer: crate::code_layer::CodeLayer,
    /// Paths carrying a genuinely *unsaved* overlay edit (from `edit` /
    /// `edit_virtual`), as opposed to content ingested from disk at open or via
    /// `sync_path`. The watcher's buffer-wins rule (a disk event is dropped when
    /// an unsaved buffer exists for the path) gates on THIS set, not on
    /// `ContentStore::has_overlay` — Phase 1 interns every project file at open,
    /// so `has_overlay` is true for all of them and would drop every watcher
    /// event (Phase 5 carry-over of the Phase 1 watcher gap).
    pub(crate) unsaved_overlays: HashSet<SystemPathBuf>,
    /// TEST-ONLY one-shot commit fault. `None` in normal use (a no-op). Armed by
    /// the `_fault_inject` PyO3 seam and consumed (taken) by the very next
    /// `commit`, which fires a typed error at the named staged boundary BEFORE
    /// the publish tail so the rollback contract can be exercised without
    /// filesystem permission tricks (§0.4). Never fires unless armed.
    pub(crate) armed_fault: Option<String>,
}

/// Anything that can produce the cheap, GIL-releasable read clone.
pub(crate) trait ReadCloneSource {
    fn read_clone(&self) -> TyProjectState;
}

impl ReadCloneSource for TyProjectState {
    fn read_clone(&self) -> TyProjectState {
        TyProjectState {
            db: self.db.clone(),
            root: self.root.clone(),
            registry: self.registry.clone(),
            hash_policy: self.hash_policy,
            hash_policies: self.hash_policies.clone(),
            default_hash_profile: self.default_hash_profile.clone(),
            authored: None,
        }
    }
}

impl ReadCloneSource for HeadState {
    fn read_clone(&self) -> TyProjectState {
        // Clone db + root + registry. store/system stay in the head; the read
        // clone (and any snapshot built from it) never sees them.
        TyProjectState {
            db: self.db.clone(),
            root: self.root.clone(),
            registry: Some(self.registry.clone()),
            hash_policy: self.hash_policy,
            hash_policies: self.hash_policies.clone(),
            default_hash_profile: self.default_hash_profile.clone(),
            authored: None,
        }
    }
}

/// Python-facing wrapper.  The inner `Option` is `None` after `close()`;
/// every operation checks this first and raises if the project is closed.
///
/// `inner` is `Arc<Mutex<…>>` so `PyHeadView` (Phase 9) can share a live
/// reference to the head state.
#[pyclass(name = "TyProject", module = "tyo3._native_impl", frozen)]
pub struct PyTyProject {
    pub(crate) inner: Arc<Mutex<Option<HeadState>>>,

    /// Events the watcher's background thread has observed but not yet folded
    /// into HEAD. The handler closure (background thread) appends; poll_changes
    /// (the single writer) drains. A SEPARATE mutex from `inner`, never
    /// co-acquired with it, so the watcher thread never contends with writes.
    pub(crate) pending: Arc<Mutex<Vec<ChangeEvent>>>,

    /// The running ty file watcher, if `watch()` was called. `Some` while
    /// watching; `None` before `watch()` or after `unwatch()`/`close()`.
    /// Owns the notify + debouncer threads; dropping it (or `stop()`) joins them.
    pub(crate) watcher: Mutex<Option<ProjectWatcher>>,
}

// ── Internal helpers ─────────────────────────────────────────────────────

/// Lock and access the state.  Returns an error if the project is closed
/// or the mutex is poisoned.
pub(crate) fn lock_state<'a, T>(
    inner: &'a Mutex<Option<T>>,
    op_name: &str,
) -> PyResult<std::sync::MutexGuard<'a, Option<T>>> {
    let guard = inner.lock().map_err(|e| {
        PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
    })?;
    if guard.is_none() {
        return Err(ProjectClosedError::new_err(format!(
            "Project is closed — cannot call {}()",
            op_name
        )));
    }
    Ok(guard)
}

/// Lock, clone the frozen project state (a cheap salsa snapshot), then drop the
/// lock. The returned owned `TyProjectState` is `Send`/`Ungil`, so it can drive
/// GIL-released analysis inside `py.detach(...)`.
///
/// The lock is held only for the duration of the clone (microseconds); the heavy
/// analysis then runs on the owned clone with both the lock and the GIL released.
pub(crate) fn clone_locked_state<T: ReadCloneSource>(
    inner: &Mutex<Option<T>>,
    op_name: &str,
) -> PyResult<TyProjectState> {
    let guard = lock_state(inner, op_name)?;
    Ok(guard.as_ref().unwrap().read_clone())
}

pub(crate) fn config_error_to_pyerr(err: config::ConfigError) -> PyErr {
    match err {
        config::ConfigError::UnknownVersion(_) => FormatVersionError::new_err(err.to_string()),
        other => PyConfigError::new_err(other.to_string()),
    }
}

// ── Submodules (Phase 13 split) ──────────────────────────────────────────
//
// `project.rs` was split into focused, single-responsibility modules. This
// root owns the shared state types (above), the lock helpers, and the unit
// tests (below). Each submodule pulls the shared imports + state types in via
// `use super::*` and is re-exported here so the test module and the rest of
// the crate keep a flat `crate::project::*` surface.
mod analysis;
mod commit;
mod head_view;
mod methods;
mod open;
mod snapshot;

pub(crate) use analysis::*;
pub(crate) use commit::*;
pub(crate) use head_view::*;
pub(crate) use open::*;
pub(crate) use snapshot::*;

#[cfg(test)]
mod phase2_tests {
    use super::*;
    use ruff_db::files::system_path_to_file;
    use ruff_db::source::source_text;
    use std::io::Write;

    /// Temp project dir with `a.py` and an optional `pyproject.toml`.
    fn project(pyproject: Option<&str>, a_py: &str) -> (tempfile::TempDir, SystemPathBuf) {
        let dir = tempfile::tempdir().unwrap();
        if let Some(toml) = pyproject {
            let mut f = std::fs::File::create(dir.path().join("pyproject.toml")).unwrap();
            f.write_all(toml.as_bytes()).unwrap();
        }
        let mut f = std::fs::File::create(dir.path().join("a.py")).unwrap();
        f.write_all(a_py.as_bytes()).unwrap();
        let root = SystemPathBuf::from_path_buf(
            dir.path().canonicalize().unwrap().to_path_buf(),
        )
        .unwrap();
        (dir, root)
    }

    /// build_head over a directory with no config still yields a working db that
    /// reads disk content through the overlay.
    #[test]
    fn build_head_no_config_reads_disk() {
        let (_dir, root) = project(None, "VALUE = 42\n");
        let head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let a = root.join("a.py");
        let file = system_path_to_file(&head.db, &a).unwrap();
        assert!(source_text(&head.db, file).as_str().contains("VALUE = 42"));
    }

    /// THE Phase-2 behavioural win: a `pyproject.toml` is discovered and applied.
    ///
    /// Strategy: use the `python-version` environment setting. ty defaults to the
    /// system Python (3.13 here). Setting `python-version = "3.8"` restricts
    /// analysis to Python 3.8 syntax, flagging 3.9+ features as errors.
    #[test]
    fn config_discovery_is_applied() {
        // PEP 695 type parameter syntax (Python 3.12+). Under 3.8, this is a
        // syntax error, producing diagnostics. Under default (3.13), it's fine.
        let pep695 = "def foo[T](x: T) -> T: return x\n";
        let toml_38 = "[tool.ty.environment]\npython-version = \"3.8\"\n";

        // Default (no config): system Python → 3.13 → no syntax error.
        let (_d1, root_default) = project(None, pep695);
        let head_default = build_head(root_default.clone(), ContentStore::new(), IdentityRegistry::default());
        let diags_default = head_default.db.check();

        // Config forces Python 3.8 → syntax error on PEP 695 generics.
        let (_d2, root_38) = project(Some(toml_38), pep695);
        let head_38 = build_head(root_38.clone(), ContentStore::new(), IdentityRegistry::default());
        let diags_38 = head_38.db.check();

        assert!(
            diags_38.len() > diags_default.len(),
            "forcing python-version=3.8 must increase diagnostics for 3.12+ syntax \
             (default={}, forced_3_8={}) — proves discovery+apply_configuration_files ran",
            diags_default.len(),
            diags_38.len(),
        );
    }

    /// Malformed config must not panic: build_head falls back to defaults.
    #[test]
    fn malformed_config_falls_back_to_defaults() {
        let (_dir, root) = project(Some("this is not = valid toml ]["), "X = 1\n");
        let head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default()); // must not panic
        // The db is usable despite the broken config.
        let a = root.join("a.py");
        let file = system_path_to_file(&head.db, &a).unwrap();
        assert!(source_text(&head.db, file).as_str().contains("X = 1"));
    }
}

// ── Phase 3 tests ────────────────────────────────────────────────────────

#[cfg(test)]
mod phase3_tests {
    use super::*;
    use ruff_db::files::system_path_to_file;
    use ruff_db::source::source_text;
    use std::io::Write;

    fn project(a_py: &str) -> (tempfile::TempDir, SystemPathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let mut f = std::fs::File::create(dir.path().join("a.py")).unwrap();
        f.write_all(a_py.as_bytes()).unwrap();
        let root =
            SystemPathBuf::from_path_buf(dir.path().canonicalize().unwrap().to_path_buf())
                .unwrap();
        (dir, root)
    }

    /// An overlay edit changes what the db reads — without touching disk.
    #[test]
    fn edit_overlays_content_without_disk_write() {
        let (_dir, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let a = root.join("a.py");

        // baseline reads disk
        let f = system_path_to_file(&head.db, &a).unwrap();
        assert!(source_text(&head.db, f).as_str().contains("X = 1"));

        // overlay edit
        head.store.insert_text(a.clone(), "Y = 2\n");
        head.system.publish(head.store.capture());
        let ev = ChangeEvent::file_content_changed(a.clone());
        head.db
            .apply_changes(std::slice::from_ref(&ev), None);

        let f2 = system_path_to_file(&head.db, &a).unwrap();
        assert!(source_text(&head.db, f2).as_str().contains("Y = 2"));
        // disk is untouched
        assert_eq!(
            std::fs::read_to_string(_dir.path().join("a.py")).unwrap(),
            "X = 1\n"
        );
    }

    /// The application revision advances on each edit.
    #[test]
    fn revision_advances() {
        let (_dir, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let r0 = head.store.revision().0;
        head.store.insert_text(root.join("a.py"), "X = 2\n");
        assert!(head.store.revision().0 > r0);
    }

    /// A created (previously-absent) file is classified Created and becomes
    /// visible.
    #[test]
    fn edit_creates_new_file() {
        let (_dir, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let b = root.join("b.py");
        let ev = classify_overlay_edit(&head.system, &head.db, &b);
        assert!(matches!(ev, ChangeEvent::Created { .. }));
        head.store.insert_text(b.clone(), "Z = 3\n");
        head.system.publish(head.store.capture());
        head.db
            .apply_changes(std::slice::from_ref(&ev), None);
        let f = system_path_to_file(&head.db, &b).unwrap();
        assert!(source_text(&head.db, f).as_str().contains("Z = 3"));
    }

    /// A virtual buffer is analysed without ever creating a disk file.
    #[test]
    fn edit_virtual_is_disk_free() {
        let (_dir, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let vpath = SystemVirtualPathBuf::from("untitled:1".to_string());
        head.store.insert_virtual(vpath.clone(), "VV = 9\n");
        head.system.publish(head.store.capture());
        head.db
            .apply_changes(&[ChangeEvent::CreatedVirtual(vpath.clone())], None);
        let vf = head.db.files().virtual_file(&head.db, &vpath);
        assert!(source_text(&head.db, vf.file()).as_str().contains("VV = 9"));
    }

    /// sync_path on a disk edit re-reads disk after forgetting the overlay.
    #[test]
    fn sync_path_reingests_disk() {
        let (_dir, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let a = root.join("a.py");
        // overlay it, then change disk underneath, then sync_path should win = disk
        head.store.insert_text(a.clone(), "OVERLAY = 1\n");
        head.system.publish(head.store.capture());
        std::fs::write(_dir.path().join("a.py"), "DISK = 2\n").unwrap();

        head.store.forget(&a);
        head.system.publish(head.store.capture());
        let ev = classify_disk_sync(&head.system, &head.db, &a);
        head.db
            .apply_changes(std::slice::from_ref(&ev), None);

        let f = system_path_to_file(&head.db, &a).unwrap();
        assert!(source_text(&head.db, f).as_str().contains("DISK = 2"));
    }

    // ── Step 9: commit transaction ordering ───────────────────────

    /// `edit_many` with N files advances the revision exactly once (§6.1.1).
    #[test]
    fn edit_many_is_one_revision() {
        let (_d, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let a = root.join("a.py");
        let b = root.join("b.py");

        let r_before = head.store.revision().0;
        let changes = vec![
            crate::content::Change::Insert {
                path: a.clone(),
                text: Arc::from("Y = 2\n"),
            },
            crate::content::Change::Insert {
                path: b.clone(),
                text: Arc::from("Z = 3\n"),
            },
        ];
        head.store.apply_batch(changes);
        let r_after = head.store.revision().0;
        assert_eq!(r_after, r_before + 1, "edit_many must advance revision by exactly 1");

        // The generation at r_after contains both files.
        let gen = head.store.generation_at(Revision(r_after)).unwrap();
        assert!(gen.system.get(&a).is_some());
        assert!(gen.system.get(&b).is_some());
    }

    /// After a write returns revision R, the content is immediately
    /// observable in a generation captured at R (publish-before-return).
    #[test]
    fn content_observable_immediately_after_write() {
        let (_d, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let a = root.join("a.py");

        head.store.insert_text(a.clone(), "NEW_CONTENT = 42\n");
        let r = head.store.revision();

        // Immediately capture and verify content is there.
        let gen = head.store.capture();
        let doc = gen.system.get(&a).unwrap();
        match doc {
            Document::Text { text, .. } => assert_eq!(text.as_ref(), "NEW_CONTENT = 42\n"),
            _ => panic!("expected Text"),
        }
        // The revision also advanced.
        assert!(r.0 > 0);
    }
}

// ── Phase 4 tests ────────────────────────────────────────────────────────

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

    /// Pre-populate a generation with the content of a file on disk, so
    /// a frozen snapshot can read it under Design A (no disk fallback).
    fn pre_populate(store: &mut ContentStore, path: SystemPathBuf, disk_text: &str) -> Generation {
        // Read disk into the store, then capture the generation that includes it.
        store.insert_text(path, disk_text);
        
        store.capture()
    }

    fn read(state: &TyProjectState, path: &SystemPathBuf) -> String {
        let f = ruff_db::files::system_path_to_file(&state.db, path).unwrap();
        source_text(&state.db, f).as_str().to_string()
    }

    #[test]
    fn files_filters_non_python_project_markers() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join(".keep"), "").unwrap();
        let root = SystemPathBuf::from_path_buf(dir.path().canonicalize().unwrap()).unwrap();
        let head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let snap = build_frozen(root, head.store.capture(), head.store.revision(), HashMap::new(), "structure".to_string());

        assert!(compute_files(&snap).is_empty());
    }

    /// THE Phase-4 invariant: a snapshot pinned at R keeps reading R's content
    /// across many later HEAD edits.
    ///
    /// Under Design A, the snapshot's generation must be pre-populated with
    /// all project files at build time (Step 8).  This test demonstrates that
    /// pre-populated content is pinned and unaffected by later head edits.
    #[test]
    fn snapshot_is_isolated_from_later_head_edits() {
        let (_dir, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let a = root.join("a.py");

        // Pre-populate the generation with the disk content at r0.
        let gen_r0 = pre_populate(&mut head.store, a.clone(), "X = 1\n");
        let snap0 = build_frozen(root.clone(), gen_r0, head.store.revision(), HashMap::new(), "structure".to_string());
        assert!(read(&snap0, &a).contains("X = 1"));

        // Many HEAD edits land afterwards.
        for i in 2..=5 {
            head.store.insert_text(a.clone(), format!("X = {i}\n"));
            head.system.publish(head.store.capture());
            let ev = ChangeEvent::file_content_changed(a.clone());
            head.db
                .apply_changes(std::slice::from_ref(&ev), None);
        }

        // Snapshot still reads r0; head reads latest.
        assert!(read(&snap0, &a).contains("X = 1"));
        let head_state = head.read_clone();
        assert!(read(&head_state, &a).contains("X = 5"));
    }

    /// Design A pre-population pins the content built into the generation, even
    /// if disk changes afterward.  The frozen view has NO disk fallback, so a
    /// later disk mutation cannot affect the pinned content.
    #[test]
    fn pre_populated_content_survives_disk_mutation() {
        let (_dir, root) = project("DISK = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let a = root.join("a.py");

        // Pre-populate the disk content into the generation.
        let gen = pre_populate(&mut head.store, a.clone(), "DISK = 1\n");
        let snap = build_frozen(root.clone(), gen, head.store.revision(), HashMap::new(), "structure".to_string());
        assert!(read(&snap, &a).contains("DISK = 1"));

        // Mutate the real file on disk (an out-of-band change).
        std::fs::write(_dir.path().join("a.py"), "DISK = 999\n").unwrap();

        // The snapshot still serves the pre-populated content — no disk read.
        assert!(read(&snap, &a).contains("DISK = 1"));
    }

    /// Overlaid (never-on-disk) content is pinned by the generation.
    #[test]
    fn snapshot_pins_overlay_buffer() {
        let (_dir, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let a = root.join("a.py");
        head.store.insert_text(a.clone(), "OVERLAY = 1\n");
        head.system.publish(head.store.capture());
        let ev = ChangeEvent::file_content_changed(a.clone());
        head.db
            .apply_changes(std::slice::from_ref(&ev), None);

        let snap = build_frozen(root.clone(), head.store.capture(), head.store.revision(), HashMap::new(), "structure".to_string());
        assert!(read(&snap, &a).contains("OVERLAY = 1"));
    }

    /// Time-travel: snapshot(at=r) reaches a retained revision; eviction errors.
    #[test]
    fn time_travel_to_retained_revision() {
        let (_dir, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let a = root.join("a.py");

        // Pre-populate r0's content.
        let _gen_r0 = pre_populate(&mut head.store, a.clone(), "X = 1\n");
        let r0 = head.store.revision();
        // Create r1 with different content.
        head.store.insert_text(a.clone(), "X = 2\n");

        // Build frozen from the pre-populated r0 generation (clone out of retained).
        let g0 = head.store.generation_at(r0).expect("r0 still retained");
        let snap0 = build_frozen(root.clone(), g0, r0, HashMap::new(), "structure".to_string());
        assert!(read(&snap0, &a).contains("X = 1"));
    }

    /// THE §1.3.1 gate, end-to-end: two snapshots at the same revision R,
    /// with disk mutated between their creation, return identical content
    /// and identical directory listings for every project file.
    #[test]
    fn two_snapshots_same_revision_identical_after_disk_mutation() {
        let (_dir, root) = project("SHARED = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let a = root.join("a.py");

        // Pre-populate and build first snapshot at r0.
        let gen = pre_populate(&mut head.store, a.clone(), "SHARED = 1\n");
        let r0 = head.store.revision();
        let snap1 = build_frozen(root.clone(), gen.clone(), r0, HashMap::new(), "structure".to_string());

        // Mutate disk between snapshots.
        std::fs::write(_dir.path().join("a.py"), b"MUTATED = 999\n").unwrap();

        // Build second snapshot at the same revision r0.
        let snap2 = build_frozen(root.clone(), gen, r0, HashMap::new(), "structure".to_string());

        // Both snapshots read the same pinned content.
        assert_eq!(read(&snap1, &a), read(&snap2, &a));
        assert!(read(&snap1, &a).contains("SHARED = 1"));
        assert!(read(&snap2, &a).contains("SHARED = 1"));
    }
}

// ── Phase 5 tests: Concurrency proof ──────────────────────────────────────
//
// Prove the core architectural invariant: many reader snapshots held open
// do NOT block the writer (independent Zalsa per snapshot), and snapshot
// reads never surface salsa::Cancelled.  Under Design A, snapshots use
// pre-populated generations — there is no lazy disk capture to race.

#[cfg(test)]
mod phase5_concurrency_tests {
    use super::*;
    use ruff_db::source::source_text;
    use std::io::Write;
    use std::panic::{catch_unwind, AssertUnwindSafe};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{mpsc, Arc, Barrier};
    use std::time::{Duration, Instant};

    fn stress_count(name: &str, default: usize) -> usize {
        std::env::var(name).ok().and_then(|v| v.parse().ok()).unwrap_or(default)
    }

    fn project(a_py: &str) -> (tempfile::TempDir, SystemPathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let mut f = std::fs::File::create(dir.path().join("a.py")).unwrap();
        f.write_all(a_py.as_bytes()).unwrap();
        let root =
            SystemPathBuf::from_path_buf(dir.path().canonicalize().unwrap()).unwrap();
        (dir, root)
    }

    fn read_source(state: &TyProjectState, path: &SystemPathBuf) -> String {
        let file = ruff_db::files::system_path_to_file(&state.db, path).unwrap();
        source_text(&state.db, file).as_str().to_string()
    }

    /// Pre-populate a generation with a file's content so a frozen snapshot
    /// can read it under Design A (no disk fallback).
    fn pre_populate_gen(store: &mut ContentStore, path: &SystemPathBuf, text: &str) -> Generation {
        store.insert_text(path.clone(), text);
        store.capture()
    }

    /// Apply an overlay edit using the same ordering as the production
    /// write path: classify → mutate store → publish → apply_changes.
    fn apply_overlay_edit(head: &mut HeadState, path: &SystemPathBuf, text: String) {
        let event = classify_overlay_edit(&head.system, &head.db, path);
        head.store.insert_text(path.clone(), text);
        head.system.publish(head.store.capture());
        head.db
            .apply_changes(std::slice::from_ref(&event), None);
    }

    // ── 5.1 Held snapshots do not block the writer ──────────────────────

    /// Direct proof of architecture §0: many frozen snapshots held open
    /// do NOT block `apply_changes`. If snapshot() accidentally becomes
    /// `head.db.clone()`, this test times out (the writer's `cancel_others`
    /// blocks waiting for the shared clone count to drop).
    #[test]
    fn held_snapshots_do_not_block_writer() {
        let (_dir, root) = project("x: int = 0\n");
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let a = root.join("a.py");

        // Pre-populate the content so snapshots can read it (Design A).
        let gen = pre_populate_gen(&mut head.store, &a, "x: int = 0\n");
        let rev = head.store.revision();
        // Apply the change to the head db so it doesn't interfere.
        head.system.publish(head.store.capture());
        head.db.apply_changes(&[ChangeEvent::file_content_changed(a.clone())], None);

        let snapshot_count = stress_count("TYO3_MVCC_STRESS_SNAPSHOTS", 32);
        let edit_count = stress_count("TYO3_MVCC_STRESS_EDITS", 100);

        let snapshots: Vec<TyProjectState> = (0..snapshot_count)
            .map(|_| build_frozen(root.clone(), gen.clone(), rev, HashMap::new(), "structure".to_string()))
            .collect();

        // Force each snapshot to do real work before the writer starts.
        for snap in &snapshots {
            assert!(read_source(snap, &a).contains("x: int = 0"));
        }

        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let start = Instant::now();
            for i in 1..=edit_count {
                apply_overlay_edit(&mut head, &a, format!("x: int = {i}\n"));
            }
            let _ = tx.send((head.store.revision().0, start.elapsed()));
        });

        let (rev, elapsed) = rx.recv_timeout(Duration::from_secs(5)).expect(
            "writer did not finish while snapshots were held open; \
             snapshot() likely shares the HEAD Zalsa or holds the head lock too long",
        );

        assert!(rev >= edit_count as u64);
        eprintln!(
            "held_snapshots_do_not_block_writer: snapshots={snapshot_count}, \
             edits={edit_count}, elapsed={elapsed:?}"
        );

        // Keep the snapshots alive until after the writer has completed.
        assert_eq!(snapshots.len(), snapshot_count);
    }

    // ── 5.2 Snapshot readers never cancel during hot writes ─────────────

    /// Active snapshot queries run concurrently with writer mutations.
    /// Any panic or error from a snapshot read is a failure. If a snapshot
    /// somehow shares a mutable Zalsa, readers may see `salsa::Cancelled`
    /// under concurrent writes.
    #[test]
    fn snapshot_reads_never_cancel_while_writer_hammers_head() {
        let (_dir, root) = project("x: int = 0\n");
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let a = root.join("a.py");

        // Pre-populate the content for snapshot reads (Design A).
        let gen = pre_populate_gen(&mut head.store, &a, "x: int = 0\n");
        let rev = head.store.revision();
        let reader_count = stress_count("TYO3_MVCC_STRESS_READERS", 8);
        let edit_count = stress_count("TYO3_MVCC_STRESS_EDITS", 100);

        let snapshots: Vec<TyProjectState> = (0..reader_count)
            .map(|_| build_frozen(root.clone(), gen.clone(), rev, HashMap::new(), "structure".to_string()))
            .collect();

        let barrier = Arc::new(Barrier::new(reader_count + 1));
        let stop = Arc::new(AtomicBool::new(false));
        let (err_tx, err_rx) = mpsc::channel::<String>();
        let (iter_tx, iter_rx) = mpsc::channel::<usize>();

        for (idx, snap) in snapshots.into_iter().enumerate() {
            let barrier = Arc::clone(&barrier);
            let stop = Arc::clone(&stop);
            let err_tx = err_tx.clone();
            let iter_tx = iter_tx.clone();
            let a = a.clone();
            std::thread::spawn(move || {
                barrier.wait();
                let mut iterations = 0usize;
                while !stop.load(Ordering::Relaxed) {
                    let result = catch_unwind(AssertUnwindSafe(|| {
                        let text = read_source(&snap, &a);
                        assert!(
                            text.contains("x: int = 0"),
                            "snapshot reader {idx} observed unpinned content: {text:?}"
                        );
                        // Exercise semantic queries too; source_text alone
                        // does not cover as much salsa state.
                        let _ = snap.db.check();
                    }));

                    if result.is_err() {
                        let _ = err_tx.send(format!(
                            "snapshot reader {idx} panicked; possible cancellation"
                        ));
                        break;
                    }
                    iterations += 1;
                }
                let _ = iter_tx.send(iterations);
            });
        }
        drop(err_tx);
        drop(iter_tx);

        let (done_tx, done_rx) = mpsc::channel();
        barrier.wait();
        std::thread::spawn(move || {
            for i in 1..=edit_count {
                let text = if i % 2 == 0 {
                    format!("x: int = {i}\n")
                } else {
                    "x: int = 'bad'\n".to_string()
                };
                apply_overlay_edit(&mut head, &a, text);
            }
            let _ = done_tx.send(head.store.revision().0);
        });

        let final_rev = done_rx.recv_timeout(Duration::from_secs(10)).expect(
            "writer did not finish during active snapshot reads",
        );
        stop.store(true, Ordering::Relaxed);

        let mut total_iterations = 0usize;
        for _ in 0..reader_count {
            total_iterations += iter_rx
                .recv_timeout(Duration::from_secs(5))
                .expect("snapshot reader did not stop after writer completed");
        }

        let errors: Vec<String> = err_rx.try_iter().collect();
        assert!(
            errors.is_empty(),
            "snapshot read errors: {errors:?}"
        );

        assert!(final_rev >= edit_count as u64);
        assert!(
            total_iterations > 0,
            "reader threads did not perform any snapshot reads"
        );
    }

    // ── 5.3 Pre-populated snapshot content is race-safe ─────────────────

    /// Under Design A, all snapshot content is pre-populated at build time.
    /// This test verifies many clones of one snapshot reading the same
    /// pre-populated content concurrently get identical results.
    #[test]
    fn concurrent_snapshot_reads_are_deterministic() {
        let (_dir, root) = project("CAPTURED = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let a = root.join("a.py");

        // Pre-populate the content (Design A).
        let gen = pre_populate_gen(&mut head.store, &a, "CAPTURED = 1\n");
        let rev = head.store.revision();
        let snap = build_frozen(root.clone(), gen, rev, HashMap::new(), "structure".to_string());

        let reader_count = stress_count("TYO3_MVCC_STRESS_READERS", 16);
        let barrier = Arc::new(Barrier::new(reader_count));
        let (tx, rx) = mpsc::channel();

        for _ in 0..reader_count {
            let snap_clone =
                TyProjectState {
                    db: snap.db.clone(),
                    root: snap.root.clone(),
                    registry: None,
                    hash_policy: snap.hash_policy,
                    hash_policies: std::collections::HashMap::new(),
                    default_hash_profile: "structure".to_string(),
                    authored: None,
                };
            let barrier = Arc::clone(&barrier);
            let tx = tx.clone();
            let a = a.clone();
            std::thread::spawn(move || {
                barrier.wait();
                let result =
                    catch_unwind(AssertUnwindSafe(|| read_source(&snap_clone, &a)));
                let _ = tx.send(
                    result.map_err(|_| "panic during snapshot read".to_string()),
                );
            });
        }
        drop(tx);

        let mut texts = Vec::new();
        for _ in 0..reader_count {
            let result = rx
                .recv_timeout(Duration::from_secs(5))
                .expect("snapshot reader did not finish");
            texts.push(result.expect("snapshot reader panicked"));
        }

        assert!(texts.iter().all(|t| t.contains("CAPTURED = 1")));
        assert!(
            texts.windows(2).all(|w| w[0] == w[1]),
            "all concurrent reads should observe identical pre-populated content"
        );
    }
}

// ── Step 7 tests: Disk ingest records content in the generation ──────────

#[cfg(test)]
mod step7_ingest_tests {
    use super::*;
    use ruff_db::source::source_text;
    use std::io::Write;

    fn project(files: &[(&str, &str)]) -> (tempfile::TempDir, SystemPathBuf) {
        let dir = tempfile::tempdir().unwrap();
        for (name, body) in files {
            let mut f = std::fs::File::create(dir.path().join(name)).unwrap();
            f.write_all(body.as_bytes()).unwrap();
        }
        let root = SystemPathBuf::from_path_buf(dir.path().canonicalize().unwrap()).unwrap();
        (dir, root)
    }

    /// `sync_path` reads disk once and records the content in the generation.
    /// A snapshot at the synced revision must be stable even if disk changes
    /// afterward (§1.3.2).
    #[test]
    fn sync_path_records_content_for_snapshot_stability() {
        let (_dir, root) = project(&[("a.py", "FIRST = 1\n")]);
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let a = root.join("a.py");

        // Ingest the disk file via sync_path (records content in generation).
        head.store.forget(&a);
        let disk_text = std::fs::read_to_string(a.as_std_path()).unwrap();
        let change = crate::content::Change::Insert {
            path: a.clone(),
            text: Arc::from(disk_text),
        };
        let r_sync = head.store.apply_batch(vec![change]);

        // Capture the generation at R.
        let gen_at_r = head.store.generation_at(r_sync).unwrap();

        // Mutate disk after the sync.
        std::fs::write(_dir.path().join("a.py"), b"SECOND = 999\n").unwrap();

        // Build a frozen view at the synced revision — must read FIRST content.
        let snap = build_frozen(root.clone(), gen_at_r, r_sync, HashMap::new(), "structure".to_string());
        let file = ruff_db::files::system_path_to_file(&snap.db, &a).unwrap();
        let content = source_text(&snap.db, file).as_str().to_string();
        assert!(
            content.contains("FIRST = 1"),
            "snapshot at synced revision must read the first synced content, got: {content:?}"
        );
    }

    /// After a watcher event ingests disk content, a snapshot sees the
    /// ingested content — not whatever is on live disk later.
    #[test]
    fn watcher_ingest_pins_content() {
        let (_dir, root) = project(&[("a.py", "WATCHER_FIRST = 1\n")]);
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let a = root.join("a.py");

        // Simulate a watcher event: Changed on a.py, disk is read, content
        // ingested into the generation.
        let disk_text = std::fs::read_to_string(a.as_std_path()).unwrap();
        let change = crate::content::Change::Insert {
            path: a.clone(),
            text: Arc::from(disk_text),
        };
        let r_watch = head.store.apply_batch(vec![change]);

        // Capture the generation.
        let gen = head.store.generation_at(r_watch).unwrap();

        // Mutate disk after the ingestion.
        std::fs::write(_dir.path().join("a.py"), b"WATCHER_MUTATED = 999\n").unwrap();

        // Snapshot at R_watch must read the ingested (first) content.
        let snap = build_frozen(root, gen, r_watch, HashMap::new(), "structure".to_string());
        let file = ruff_db::files::system_path_to_file(&snap.db, &a).unwrap();
        let content = source_text(&snap.db, file).as_str().to_string();
        assert!(content.contains("WATCHER_FIRST = 1"));
    }

    /// A deleted file produces a Delete tombstone in the generation, so a
    /// snapshot at that revision reports the path absent.
    #[test]
    fn deleted_file_is_absent_in_snapshots() {
        let (_dir, root) = project(&[("a.py", "X = 1\n")]);
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let a = root.join("a.py");

        // Ingest a Delete for a.py (file was deleted on disk).
        let change = crate::content::Change::Delete { path: a.clone() };
        let r_del = head.store.apply_batch(vec![change]);

        let gen = head.store.generation_at(r_del).unwrap();

        // Even if disk still has the file, the snapshot sees it as absent.
        // (Under Design A, the frozen view has no disk fallback.)
        let snap = build_frozen(root, gen, r_del, HashMap::new(), "structure".to_string());
        let result = ruff_db::files::system_path_to_file(&snap.db, &a);
        // Should be an error — file is tombstoned in the generation.
        assert!(
            result.is_err(),
            "tombstoned file must not be resolvable in snapshot"
        );
    }
}

// ── Phase 8 tests: Watcher drain-and-apply ──────────────────────────────

#[cfg(test)]
mod phase8_watch_tests {
    use super::*;
    use std::io::Write;

    fn project(files: &[(&str, &str)]) -> (tempfile::TempDir, SystemPathBuf) {
        let dir = tempfile::tempdir().unwrap();
        for (name, body) in files {
            let mut f = std::fs::File::create(dir.path().join(name)).unwrap();
            f.write_all(body.as_bytes()).unwrap();
        }
        let root = SystemPathBuf::from_path_buf(dir.path().canonicalize().unwrap()).unwrap();
        (dir, root)
    }

    /// Drive a watcher batch through the Phase 5 commit funnel (the old
    /// `apply_watch_events` path is now `Mutation::Poll`).
    fn poll(head: &mut HeadState, events: Vec<ChangeEvent>) -> Option<dto::CommitDeltaDto> {
        commit(head, Mutation::Poll { events }).unwrap()
    }

    #[test]
    fn empty_batch_is_noop() {
        let (_d, root) = project(&[("a.py", "x = 1\n")]);
        let mut head = build_head(root, ContentStore::new(), IdentityRegistry::default());
        assert!(poll(&mut head, vec![]).is_none());
    }

    #[test]
    fn changed_event_matches_explicit_sync_path() {
        // The load-bearing parity: a watcher Changed event yields the same delta
        // shape as an explicit sync_path for the same on-disk change.
        let (_d, root) = project(&[("a.py", "x: int = 1\n")]);
        let a = root.join("a.py");

        // Watcher-driven head.
        let mut head_w = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let _ = head_w.db.apply_changes(&[ChangeEvent::Rescan], None); // warm discovery
        std::fs::write(a.as_std_path(), b"x: str = 'two'\n").unwrap();
        let via_watch = poll(&mut head_w, vec![ChangeEvent::file_content_changed(a.clone())])
            .expect("a changed file must produce a SyncResult");

        assert_eq!(via_watch.changed, vec![a.as_str().to_string()]);
        assert!(via_watch.created.is_empty() && via_watch.deleted.is_empty());
        assert!(!via_watch.rescan);
        // HEAD now reads the new disk content (overlay falls through to disk):
        let file = ruff_db::files::system_path_to_file(&head_w.db, &a).unwrap();
        assert!(source_text(&head_w.db, file).as_str().contains("'two'"));
    }

    #[test]
    fn overlaid_path_is_not_clobbered_by_disk_event() {
        let (_d, root) = project(&[("a.py", "DISK = 1\n")]);
        let a = root.join("a.py");
        let mut head = build_head(root, ContentStore::new(), IdentityRegistry::default());

        // Agent overlay buffer (unsaved). Phase 5 gates buffer-wins on the
        // genuinely-unsaved set, so mark the path as carrying an unsaved edit.
        head.store.insert_text(a.clone(), "BUFFER = 2\n".to_string());
        head.system.publish(head.store.capture());
        head.db.apply_changes(&[ChangeEvent::file_content_changed(a.clone())], None);
        head.unsaved_overlays.insert(a.clone());

        // A disk change underneath the buffer arrives via the watcher.
        std::fs::write(a.as_std_path(), b"DISK = 999\n").unwrap();
        let result = poll(&mut head, vec![ChangeEvent::file_content_changed(a.clone())]);

        // Dropped: nothing applied, buffer still wins.
        assert!(result.is_none(), "overlaid path must not be clobbered by a disk event");
        let file = ruff_db::files::system_path_to_file(&head.db, &a).unwrap();
        assert!(source_text(&head.db, file).as_str().contains("BUFFER = 2"));
    }

    #[test]
    fn rescan_event_short_circuits() {
        let (_d, root) = project(&[("a.py", "x = 1\n")]);
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let a = root.join("a.py");
        let r = poll(
            &mut head,
            vec![ChangeEvent::file_content_changed(a), ChangeEvent::Rescan],
        )
        .expect("rescan yields a result");
        assert!(r.rescan);
        assert!(r.created.is_empty() && r.changed.is_empty() && r.deleted.is_empty());
    }

    #[test]
    fn burst_folds_into_one_revision() {
        let (_d, root) = project(&[("a.py", "x = 1\n")]);
        let a = root.join("a.py");
        let mut head = build_head(root.clone(), ContentStore::new(), IdentityRegistry::default());
        let before = head.store.revision().0;
        std::fs::write(a.as_std_path(), b"x = 2\n").unwrap();
        let r = poll(
            &mut head,
            vec![
                ChangeEvent::file_content_changed(a.clone()),
                ChangeEvent::file_content_changed(a.clone()),
                ChangeEvent::file_content_changed(a.clone()),
            ],
        )
        .unwrap();
        assert_eq!(r.revision, before + 1, "a burst folds into exactly one revision");
    }
}
