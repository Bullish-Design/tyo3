use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pythonize::pythonize;

use crate::{ConfigError as PyConfigError, FormatVersionError, ProjectClosedError, PathResolutionError, PositionError, RevisionEvictedError, SidecarWriteError, CommitFailed};

use ruff_db::files::File;
use ruff_db::source::source_text;
use ruff_db::system::{SystemPath, SystemPathBuf, SystemVirtualPathBuf};
use ruff_db::Db as _; // bring files() etc. into scope
use ruff_source_file::LineIndex;

use ty_project::watch::{ChangeEvent, ChangedKind, CreatedKind, DeletedKind, ExistingPathKind, ProjectWatcher, directory_watcher};
use ty_project::Db;
use ty_project::{ProjectDatabase, ProjectMetadata};

use crate::authored::{AuthoredMap, AuthoredRecordDoc, AuthoredStore, AuthoredLoadError};
use crate::content::{is_project_relevant, ContentStore, Document, Generation, Revision};
use crate::config::{self, RawConfig, ValidatedConfig};
use crate::entity::{extract_entities, extract_entities_for};
use crate::hash::HashPolicy;
use crate::identity::{reconcile, reconcile_scoped, DurableId, FormatError, IdentityRegistry, IdentityStatus};
use crate::overlay::OverlaySystem;
use crate::sidecar::Sidecar;

use ruff_python_ast::{name::Name, PySourceType};

use crate::convert;
use crate::coordinates;
use crate::dto;
use crate::files as file_resolver;

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
    fn into_pyerr(self) -> PyErr {
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
/// In Phase 2 `store`/`system` are wired but idle: no edits flow through them
/// yet. Phase 3 activates them (`store.insert_text` → `system.publish` →
/// `db.apply_changes`).
struct HeadState {
    db: ProjectDatabase,
    root: SystemPathBuf,
    store: ContentStore,
    /// Handle onto the *same* overlay content cell the `db` reads through
    /// (clone-shares the inner `Arc<ArcSwap<…>>`). Used by Phase 3 to publish.
    system: OverlaySystem,
    /// Identity registry: binds DurableIds to last-known entity facts.
    /// Reconciled after every commit; persisted through the sidecar.
    registry: IdentityRegistry,
    /// Code-layer identity hash policy. Defaults to Gate 2 behavior until
    /// Step 5 loads it from validated config.
    hash_policy: HashPolicy,
    /// Per-profile hash policies derived from the validated config.
    hash_policies: HashMap<String, HashPolicy>,
    /// The name of the default hash profile.
    default_hash_profile: String,
    /// Validated, defaulted project config loaded once on open.
    config: ValidatedConfig,
    /// Canonical owner for all sidecar paths and durable writes.
    sidecar: Sidecar,
    /// Authored record store: identity-keyed durable knowledge (§5.4).
    /// Captured into snapshots alongside the registry for Snapshot
    /// isolation + time-travel (§10.2.2 authored half).
    authored: AuthoredStore,
    /// Canonical native code layer (nodes + edges + reverse-deps). Phase 3 reads
    /// its `reverse_deps` for the `affected_closure` in the commit. It stays
    /// **empty** in Phase 3: the in-commit producer is full-build and too
    /// expensive to run eagerly (see `run_identity_reconciliation`), so the layer
    /// is not maintained until Phase 4 makes it authoritative. While empty,
    /// `affected_closure` returns exactly the seeds (`changed ∪ deleted`).
    code_layer: crate::code_layer::CodeLayer,
    /// Paths carrying a genuinely *unsaved* overlay edit (from `edit` /
    /// `edit_virtual`), as opposed to content ingested from disk at open or via
    /// `sync_path`. The watcher's buffer-wins rule (a disk event is dropped when
    /// an unsaved buffer exists for the path) gates on THIS set, not on
    /// `ContentStore::has_overlay` — Phase 1 interns every project file at open,
    /// so `has_overlay` is true for all of them and would drop every watcher
    /// event (Phase 5 carry-over of the Phase 1 watcher gap).
    unsaved_overlays: HashSet<SystemPathBuf>,
    /// TEST-ONLY one-shot commit fault. `None` in normal use (a no-op). Armed by
    /// the `_fault_inject` PyO3 seam and consumed (taken) by the very next
    /// `commit`, which fires a typed error at the named staged boundary BEFORE
    /// the publish tail so the rollback contract can be exercised without
    /// filesystem permission tricks (§0.4). Never fires unless armed.
    armed_fault: Option<String>,
}

/// Anything that can produce the cheap, GIL-releasable read clone.
trait ReadCloneSource {
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
    inner: Arc<Mutex<Option<HeadState>>>,

    /// Events the watcher's background thread has observed but not yet folded
    /// into HEAD. The handler closure (background thread) appends; poll_changes
    /// (the single writer) drains. A SEPARATE mutex from `inner`, never
    /// co-acquired with it, so the watcher thread never contends with writes.
    pending: Arc<Mutex<Vec<ChangeEvent>>>,

    /// The running ty file watcher, if `watch()` was called. `Some` while
    /// watching; `None` before `watch()` or after `unwatch()`/`close()`.
    /// Owns the notify + debouncer threads; dropping it (or `stop()`) joins them.
    watcher: Mutex<Option<ProjectWatcher>>,
}

// ── Internal helpers ─────────────────────────────────────────────────────

/// Lock and access the state.  Returns an error if the project is closed
/// or the mutex is poisoned.
fn lock_state<'a, T>(
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
fn clone_locked_state<T: ReadCloneSource>(
    inner: &Mutex<Option<T>>,
    op_name: &str,
) -> PyResult<TyProjectState> {
    let guard = lock_state(inner, op_name)?;
    Ok(guard.as_ref().unwrap().read_clone())
}

fn config_error_to_pyerr(err: config::ConfigError) -> PyErr {
    match err {
        config::ConfigError::UnknownVersion(_) => FormatVersionError::new_err(err.to_string()),
        other => PyConfigError::new_err(other.to_string()),
    }
}

/// Resolve a file handle and return its source text as a String.
fn resolve_file_and_source(
    state: &TyProjectState,
    path: &str,
) -> Result<(File, String), AnalysisError> {
    let file = file_resolver::resolve_file(
        &state.db,
        state.root.as_std_path(),
        path,
    )
    .map_err(AnalysisError::Path)?;

    let src = source_text(&state.db, file);
    let source_str = src.as_str().to_string();

    Ok((file, source_str))
}

// ── GIL-free analysis cores ──────────────────────────────────────────────
//
// Each `compute_*` takes `&TyProjectState`, touches no Python state, and
// returns a serializable DTO (or a bare value when it cannot fail). They are
// safe to call inside `py.detach(...)`.

/// List all source files in the project.
pub(crate) fn compute_files(state: &TyProjectState) -> Vec<String> {
    let project = state.db.project();
    let indexed = project.files(&state.db);
    indexed
        .iter()
        .filter(|f: &&File| {
            f.path(&state.db)
                .extension()
                .and_then(PySourceType::try_from_extension)
                .is_some()
        })
        .map(|f: &File| f.path(&state.db).as_str().to_string())
        .collect()
}

/// Run the project type-check and build the DTO.
fn compute_check(state: &TyProjectState) -> dto::CheckResultDto {
    let result = state.db.check();
    let diagnostics = convert::diagnostics::convert_diagnostics(&state.db, &result);
    dto::CheckResultDto {
        diagnostics,
        files_checked: None,
        elapsed_ms: None,
    }
}

/// Run a full project check and return only the diagnostics for `path`.
fn compute_check_file(
    state: &TyProjectState,
    path: &str,
) -> Result<dto::CheckResultDto, AnalysisError> {
    let (file, _) = resolve_file_and_source(state, path)?;
    let target_path = file.path(&state.db).as_str().to_string();

    // Full project check (Salsa-cached if unchanged)
    let all_diagnostics = state.db.check();

    // Filter in Rust — only convert matching diagnostics to DTOs
    let matching: Vec<_> = all_diagnostics
        .iter()
        .filter(|d| {
            convert::diagnostics::diagnostic_matches_file(&state.db, d, &target_path)
        })
        .collect();

    let diagnostics =
        convert::diagnostics::convert_diagnostic_refs(&state.db, &matching);

    Ok(dto::CheckResultDto {
        diagnostics,
        files_checked: Some(1),
        elapsed_ms: None,
    })
}

/// Get document symbols for a file.
pub(crate) fn compute_document_symbols(
    state: &TyProjectState,
    path: &str,
) -> Result<Vec<dto::SymbolDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;

    let flat_symbols = ty_ide::document_symbols(&state.db, file);
    let hierarchical = flat_symbols.to_hierarchical();

    let file_path = file.path(&state.db).as_str().to_string();
    let line_index = ruff_source_file::LineIndex::from_source_text(&source_str);

    let mut symbols: Vec<dto::SymbolDto> = Vec::new();
    let policies_ref = if state.hash_policies.is_empty() {
        None
    } else {
        Some(&state.hash_policies)
    };
    let default_profile = if state.hash_policies.is_empty() {
        None
    } else {
        Some(state.default_hash_profile.as_str())
    };
    for (id, info) in hierarchical.iter() {
        convert::symbols::collect_symbols_recursive(
            &hierarchical,
            id,
            &info,
            &source_str,
            &line_index,
            &file_path,
            None,
            None,
            state.registry.as_ref(),
            policies_ref,
            default_profile,
            &mut symbols,
        );
    }

    Ok(symbols)
}

/// Search for symbols matching a query across all workspace files.
fn compute_workspace_symbols(
    state: &TyProjectState,
    query: &str,
) -> Vec<dto::SymbolDto> {
    let results = ty_ide::workspace_symbols(&state.db, query);

    // Cache source text and LineIndex per file — workspace symbol results
    // from the same file reuse the precomputed LineIndex instead of rebuilding.
    let mut file_cache: HashMap<File, (String, LineIndex)> = HashMap::new();
    let mut symbols: Vec<dto::SymbolDto> = Vec::with_capacity(results.len());
    for ws_info in &results {
        let (source_str, line_index) = file_cache
            .entry(ws_info.file)
            .or_insert_with(|| {
                let src = source_text(&state.db, ws_info.file);
                let s = src.as_str().to_string();
                let idx = LineIndex::from_source_text(&s);
                (s, idx)
            });
        let file_path = ws_info.file.path(&state.db).as_str().to_string();

        let sym = convert::symbols::convert_symbol(
            source_str,
            line_index,
            &file_path,
            &ws_info.symbol.name,
            &ws_info.symbol.kind,
            ws_info.symbol.deprecated,
            ws_info.symbol.name_range,
            ws_info.symbol.full_range,
            ws_info
                .symbol
                .imported_from
                .as_ref()
                .map(|i| i.module_name().as_str()),
            None,
            None,
            None,
            std::collections::HashMap::new(),
        );
        symbols.push(sym);
    }

    symbols
}

/// Shared core for `goto_definition` / `goto_declaration` /
/// `goto_type_definition`.  Resolves the path, computes the source offset,
/// calls the provided navigation function, and converts the results.
///
/// `navigate_fn` is a plain function pointer — function pointers are
/// `Send`/`Ungil`, so they cross the `detach` boundary fine.
pub(crate) fn compute_navigate(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
    navigate_fn: fn(
        &dyn Db,
        File,
        ruff_text_size::TextSize,
    ) -> Option<ty_ide::RangedValue<ty_ide::NavigationTargets>>,
) -> Result<Vec<dto::DefinitionTargetDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;

    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    let targets = match navigate_fn(&state.db, file, offset) {
        Some(targets) => {
            convert::navigation::convert_navigation_targets(&state.db, &targets)
        }
        None => Vec::new(),
    };

    Ok(targets)
}

/// Find all references to the symbol at the given position.
fn compute_find_references(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
    include_declaration: bool,
) -> Result<Vec<dto::ReferenceDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;

    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    let references = match ty_ide::find_references(
        &state.db, file, offset, include_declaration,
    ) {
        Some(refs) => convert::navigation::convert_references(&state.db, &refs),
        None => Vec::new(),
    };

    Ok(references)
}

/// Return semantic tokens for a file, optionally scoped to a range.
fn compute_semantic_tokens(
    state: &TyProjectState,
    path: &str,
    start_line: Option<u32>,
    start_col: Option<u32>,
    end_line: Option<u32>,
    end_col: Option<u32>,
) -> Result<Vec<dto::SemanticTokenDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);

    let range = match (start_line, start_col, end_line, end_col) {
        (Some(sl), Some(sc), Some(el), Some(ec)) => {
            let start = coordinates::position_to_offset_with_index(
                &source_str, &line_index, sl, sc,
            )
            .map_err(AnalysisError::Position)?;
            let end = coordinates::position_to_offset_clamped_line_end_with_index(
                &source_str, &line_index, el, ec,
            )
            .map_err(AnalysisError::Position)?;
            Some(ruff_text_size::TextRange::new(start, end))
        }
        _ => None,
    };

    let tokens = ty_ide::semantic_tokens(&state.db, file, range);

    let result = convert::tokens::convert_semantic_tokens(
        &source_str,
        &line_index,
        &tokens,
    );

    Ok(result)
}

/// Batch-resolve all name occurrences in a file.
pub(crate) fn compute_file_occurrences(
    state: &TyProjectState,
    path: &str,
) -> Result<Vec<dto::NameOccurrenceDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);

    let result = convert::occurrences::convert_file_occurrences(
        &state.db,
        file,
        &source_str,
        &line_index,
    );

    Ok(result)
}

/// Query type hierarchy at a position: the item plus its supertypes and subtypes.
fn compute_type_hierarchy(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
) -> Result<Option<dto::TypeHierarchyDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    // Step 1: Prepare the hierarchy item at this position
    let item = match ty_ide::prepare_type_hierarchy(&state.db, file, offset) {
        Some(item) => item,
        None => return Ok(None),
    };

    // Step 2: Resolve supertypes and subtypes using the prepared item
    let supertypes = ty_ide::type_hierarchy_supertypes(
        &state.db, item.file, item.selection_range.start(),
    );
    let subtypes = ty_ide::type_hierarchy_subtypes(
        &state.db, item.file, item.selection_range.start(),
    );

    let item_dto = convert::hierarchy::convert_hierarchy_item(&state.db, &item);
    let supertypes_dto = convert::hierarchy::convert_hierarchy_items(&state.db, &supertypes);
    let subtypes_dto = convert::hierarchy::convert_hierarchy_items(&state.db, &subtypes);

    Ok(Some(dto::TypeHierarchyDto {
        item: item_dto,
        supertypes: supertypes_dto,
        subtypes: subtypes_dto,
    }))
}

/// Resolve only the direct supertypes (base classes) of the class at a position.
///
/// This is the lean half of `compute_type_hierarchy`: it runs
/// `prepare_type_hierarchy` + `type_hierarchy_supertypes` and deliberately
/// SKIPS `type_hierarchy_subtypes`. Subtype resolution scans every module in
/// the workspace (including typeshed/stdlib) to find inheritors and is, per
/// ty's own docs, "quite expensive in large projects" — yet CodeGraph build
/// only ever reads supertypes (for INHERITS edges). Skipping it removes that
/// global scan from the hot build path.
///
/// Returns an empty vec when the position is not on a class.
pub(crate) fn compute_supertypes(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
) -> Result<Vec<dto::TypeHierarchyItemDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    // Confirm the cursor is on a class and normalize to its name position.
    let item = match ty_ide::prepare_type_hierarchy(&state.db, file, offset) {
        Some(item) => item,
        None => return Ok(Vec::new()),
    };

    let supertypes = ty_ide::type_hierarchy_supertypes(
        &state.db, item.file, item.selection_range.start(),
    );
    Ok(convert::hierarchy::convert_hierarchy_items(&state.db, &supertypes))
}

/// Get inlay hints for a file (whole-file).
///
/// NOTE: ty_ide does not publicly re-export InlayHint, so the conversion
/// is inlined here to work with the inferred type.
fn compute_inlay_hints(
    state: &TyProjectState,
    path: &str,
) -> Result<Vec<dto::InlayHintDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);

    let text_len = ruff_text_size::TextSize::from(source_str.len() as u32);
    let full_range = ruff_text_size::TextRange::up_to(text_len);
    let settings = ty_ide::InlayHintSettings::default();

    let hints = ty_ide::inlay_hints(&state.db, file, full_range, &settings);
    Ok(hints
        .iter()
        .map(|hint| {
            // Flatten structured label parts into a single display string
            let label: String = hint.label.parts()
                .iter()
                .map(|p| p.text().to_string())
                .collect::<Vec<_>>()
                .join("");

            let loc = line_index.source_location(
                hint.position,
                &source_str,
                ruff_source_file::PositionEncoding::Utf32,
            );

            let kind = match hint.kind {
                ty_ide::InlayHintKind::Type => dto::InlayHintKindDto::Type,
                ty_ide::InlayHintKind::CallArgumentName => dto::InlayHintKindDto::CallArgumentName,
            };

            dto::InlayHintDto {
                position: dto::PositionDto {
                    line: loc.line.get() as u32,
                    column: (loc.character_offset.to_zero_indexed() + 1) as u32,
                },
                label,
                kind,
            }
        })
        .collect())
}

/// Get code actions (quick fixes) for a diagnostic at a range.
fn compute_code_actions(
    state: &TyProjectState,
    path: &str,
    start_line: u32,
    start_col: u32,
    end_line: u32,
    end_col: u32,
    diagnostic_id: &str,
) -> Result<Vec<dto::QuickFixDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let start_offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, start_line, start_col,
    )
    .map_err(AnalysisError::Position)?;
    let end_offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, end_line, end_col,
    )
    .map_err(AnalysisError::Position)?;

    let diagnostic_range = ruff_text_size::TextRange::new(start_offset, end_offset);
    let file_path = file.path(&state.db).as_str().to_string();

    let fixes = ty_ide::code_actions(&state.db, file, diagnostic_range, diagnostic_id);
    Ok(fixes
        .iter()
        .map(|fix| {
            convert::code_action::convert_quick_fix(&source_str, &line_index, &file_path, fix)
        })
        .collect())
}

/// Get hints (unused bindings, unreachable code) for a file.
fn compute_hints(
    state: &TyProjectState,
    path: &str,
) -> Result<Vec<dto::HintDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);

    let hints = ty_ide::hints(&state.db, file);
    Ok(hints
        .iter()
        .map(|h| convert::hints::convert_hint(&source_str, &line_index, h))
        .collect())
}

/// Get signature help at a position.
fn compute_signature_help(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
) -> Result<Option<dto::SignatureHelpDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    match ty_ide::signature_help(&state.db, file, offset) {
        Some(info) => {
            Ok(Some(convert::signature::convert_signature_help(&info)))
        }
        None => Ok(None),
    }
}

/// Get completion suggestions at a position.
fn compute_completions(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
    auto_import: bool,
) -> Result<Vec<dto::CompletionDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    let settings = ty_ide::CompletionSettings { auto_import };
    let completions = ty_ide::completion(&state.db, &settings, file, offset);
    Ok(convert::completion::convert_completions(&state.db, &completions))
}

/// Compute selection ranges at a position.
fn compute_selection_ranges(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
) -> Result<Vec<dto::RangeDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    let ranges = ty_ide::selection_range(&state.db, file, offset);
    Ok(ranges
        .iter()
        .map(|r| coordinates::range_to_dto_with_index(&source_str, &line_index, *r))
        .collect())
}

/// Compute folding ranges for a file (whole-file by default).
fn compute_folding_ranges(
    state: &TyProjectState,
    path: &str,
) -> Result<Vec<dto::FoldingRangeDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);

    let ranges = ty_ide::folding_ranges(&state.db, file, None);
    Ok(ranges
        .iter()
        .map(|r| convert::folding::convert_folding_range(&source_str, &line_index, r))
        .collect())
}

/// Return the editable range of the symbol at the position, or None.
fn compute_can_rename(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
) -> Result<Option<dto::RangeDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    let range = ty_ide::can_rename(&state.db, file, offset);
    Ok(range.map(|r| {
        coordinates::range_to_dto_with_index(&source_str, &line_index, r)
    }))
}

/// Perform a rename operation, returning all edit locations.
fn compute_rename(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
    new_name: &str,
) -> Result<Option<dto::WorkspaceEditDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    match ty_ide::rename(&state.db, file, offset, new_name) {
        Some(targets) => {
            Ok(Some(convert::rename::convert_rename_edits(
                &state.db, &targets, new_name,
            )))
        }
        None => Ok(None),
    }
}

/// Find document highlights for the symbol at the given position.
///
/// Highlights are identical to references but scoped to the current file.
fn compute_document_highlights(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
) -> Result<Vec<dto::ReferenceDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    let refs = match ty_ide::document_highlights(&state.db, file, offset) {
        Some(targets) => convert::navigation::convert_references(&state.db, &targets),
        None => Vec::new(),
    };
    Ok(refs)
}

/// Get hover information for the symbol at the given position.
///
/// NOTE: ty_ide does not publicly re-export Hover/HoverContent, so the entire
/// hover is rendered as Markdown and returned as a single content item.
fn compute_hover(
    state: &TyProjectState,
    path: &str,
    line: u32,
    column: u32,
) -> Result<Option<dto::HoverDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;

    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(AnalysisError::Position)?;

    match ty_ide::hover(&state.db, file, offset) {
        None => Ok(None),
        Some(hover_value) => {
            let file_range = hover_value.file_range();
            let file_path = file.path(&state.db).as_str().to_string();

            // Render the entire hover as Markdown
            let rendered = hover_value
                .display(&state.db, ty_ide::MarkupKind::Markdown)
                .to_string();

            let hover_dto = convert::hover::convert_hover_markdown_with_index(
                &source_str,
                &line_index,
                file_path,
                file_range,
                rendered,
            );

            Ok(Some(hover_dto))
        }
    }
}

// ── Head builder ─────────────────────────────────────────────────────────

/// Build a live HEAD over an `OverlaySystem`, seeding the overlay from
/// `initial_store` (an empty store on first open; the preserved store on reload).
///
/// Construction mirrors ty_server (`ty_server/src/session.rs:602`), but keeps
/// the caller's root as a hard project boundary. `ProjectMetadata::discover`
/// walks upward, which is correct for a CLI but wrong for TyO3 fixture/session
/// roots: opening `fixtures/simple_package` must not analyze the whole repo.
///
///   1. discover project metadata from disk (`pyproject.toml` / `ty.toml`),
///   2. discard discovered metadata if it came from an ancestor root,
///   3. layer user-level configuration on top,
///   4. build the db with `fallible` (surfaces config errors),
///   5. on any failure, fall back to a default blank project (never panic).
/// Load all authored records from disk for every layer declared with
/// `origin = "authored"`.  Returns an `AuthoredStore` (rpds-backed map)
/// containing every valid record on disk.  A missing `authored/` directory
/// yields an empty store (first run).  Corrupt or newer-format records
/// propagate `AuthoredLoadError` upward so the session does not open
/// (§11.3.5).
fn load_authored_records(
    config: &ValidatedConfig,
    sidecar: &Sidecar,
) -> Result<AuthoredStore, AuthoredLoadError> {
    let mut map = AuthoredMap::default();
    for name in &config.topo_order {
        let Some(layer_cfg) = config.raw.layers.get(name) else {
            continue;
        };
        if !matches!(layer_cfg.origin, config::LayerOrigin::Authored) {
            continue;
        }
        let dir = sidecar.authored_dir(name);
        if !dir.exists() {
            continue;
        }
        let entries = match std::fs::read_dir(&dir) {
            Ok(e) => e,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            // Skip dirs, non-.json files, and temp files.
            if !path.is_file() {
                continue;
            }
            let Some(file_name) = path.file_stem().and_then(|n| n.to_str()) else {
                continue;
            };
            if path.extension().and_then(|e| e.to_str()) != Some("json") {
                continue;
            }
            let bytes = match std::fs::read(&path) {
                Ok(b) => b,
                Err(_) => continue,
            };
            let doc = AuthoredRecordDoc::from_bytes(&bytes)?;
            // History from disk: re-insert each historical version if the
            // layer keeps history.  Put history versions in order (oldest
            // first), then current — each `put` pushes the previous current
            // into history, so the final record has the full history chain.
            if layer_cfg.history {
                for hist_version in &doc.history {
                    map = map.put(
                        &doc.layer,
                        &doc.durable_id,
                        hist_version.clone(),
                        layer_cfg.history,
                    );
                }
            }
            map = map.put(
                &doc.layer,
                &doc.durable_id,
                doc.current,
                layer_cfg.history,
            );
        }
    }
    Ok(AuthoredStore::new(map))
}

fn build_head(root: SystemPathBuf, initial_store: ContentStore, registry: IdentityRegistry) -> HeadState {
    let config = config::validate(RawConfig::defaults()).expect("default config is valid");
    build_head_with_config(root, initial_store, registry, config)
}

fn build_head_with_config(
    root: SystemPathBuf,
    initial_store: ContentStore,
    registry: IdentityRegistry,
    config: ValidatedConfig,
) -> HeadState {
    use ruff_python_ast::name::Name;

    // The overlay the db reads through. The clone handed to fallible/use_defaults
    // shares the same content cell, so `system.publish(...)` (Phase 3) is visible
    // to the db.
    let system = OverlaySystem::live(root.clone(), initial_store.capture());

    // 1+2+3+4: discover → clamp → apply user config → build. Each step's error
    // is mapped to a string so the chain has one error type (no `anyhow`
    // dependency).
    let built: Result<ProjectDatabase, String> = ProjectMetadata::discover(&root, &system)
        .map_err(|e| format!("project discovery failed: {e}"))
        .map(|metadata| {
            if metadata.root() == &*root {
                metadata
            } else {
                ProjectMetadata::new(
                    Name::new(root.file_name().unwrap_or("root")),
                    root.clone(),
                )
            }
        })
        .and_then(|mut metadata| {
            metadata
                .apply_configuration_files(&system)
                .map_err(|e| format!("failed to apply configuration files: {e}"))?;
            ProjectDatabase::fallible(metadata, system.clone())
                .map_err(|e| format!("failed to build project database: {e:#}"))
        });

    let db = match built {
        Ok(db) => db,
        Err(err) => {
            // 4. Fallback: blank project over the same overlay, defaults substituted.
            log::warn!("{err}. Falling back to default project settings.");
            let metadata = ProjectMetadata::new(Name::new("tyo3-project"), root.clone());
            ProjectDatabase::use_defaults(metadata, system.clone())
        }
    };

    let hash_policies: HashMap<String, HashPolicy> = config
        .raw
        .hashing
        .profiles
        .iter()
        .map(|(name, profile)| (name.clone(), HashPolicy::from(profile)))
        .collect();
    let default_hash_profile = config.raw.spine.default_hash_profile.clone();

    HeadState {
        db,
        sidecar: Sidecar::new(root.as_std_path()),
        root,
        store: initial_store,
        system,
        registry,
        hash_policy: config.hash_policy_for("code"),
        hash_policies,
        default_hash_profile,
        config,
        authored: AuthoredStore::default(),
        code_layer: crate::code_layer::CodeLayer::new(),
        unsaved_overlays: HashSet::new(),
        armed_fault: None,
    }
}

/// Build an independent, revision-pinned `ProjectDatabase` over a frozen overlay.
///
/// Construction mirrors `build_head` (discover → apply user config → fallible,
/// with a `use_defaults` fallback) but over `OverlaySystem::frozen(...)`, so the
/// resulting db has its OWN `Zalsa`: it can never be cancelled by a HEAD
/// `apply_changes`, and a HEAD `apply_changes` never blocks on it (architecture §0).
///
/// `generation` is the content pinned at `rev` (captured from the store, O(1)).
/// Since Phase 1, the generation is already *complete* for revision R: every
/// project-relevant file (Python sources plus the `pyproject.toml` / `ty.toml`
/// config files that `ProjectMetadata::discover` / `apply_configuration_files`
/// read) was interned at open and at every commit. The frozen overlay therefore
/// pins exactly this generation with no disk fallback (overlay.rs §2), so the
/// snapshot never races live disk and capture stays O(1).
fn build_frozen(
    root: SystemPathBuf,
    generation: Generation,
    rev: Revision,
    hash_policies: HashMap<String, HashPolicy>,
    default_hash_profile: String,
) -> TyProjectState {
    // Phase 1: the generation is already complete (ingested at open and
    // updated at every commit), so no disk pre-population is needed.
    // The frozen overlay pins exactly the generation as-is, achieving
    // O(1) snapshot capture with no disk content reads.
    let system = OverlaySystem::frozen(root.clone(), generation, rev);

    let built: Result<ProjectDatabase, String> = ProjectMetadata::discover(&root, &system)
        .map_err(|e| format!("project discovery failed: {e}"))
        .map(|metadata| {
            if metadata.root() == &*root {
                metadata
            } else {
                ProjectMetadata::new(
                    Name::new(root.file_name().unwrap_or("root")),
                    root.clone(),
                )
            }
        })
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
            log::warn!("{err}. Falling back to default project settings for snapshot.");
            let metadata =
                ProjectMetadata::new(Name::new("tyo3-project"), root.clone());
            ProjectDatabase::use_defaults(metadata, system)
        }
    };

    TyProjectState {
        db,
        root,
        registry: None,
        hash_policy: if let Some(p) = hash_policies.get(&default_hash_profile) {
            *p
        } else {
            HashPolicy::default()
        },
        hash_policies,
        default_hash_profile,
        authored: None,
    }
}

// ── Sync-path resolver & event synthesis (Phase 3) ──────────────────────

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
///
/// Also checks disk via `ExistingPathKind::from_system` as a fallback for files
/// that exist on disk but have not yet been interned in salsa (lazy interning).
fn classify_overlay_edit(
    system: &OverlaySystem,
    db: &ProjectDatabase,
    path: &SystemPathBuf,
) -> ChangeEvent {
    let is_new = match db.files().try_system(db, path) {
        Some(f) => !f.exists(db),
        None => {
            // Not interned yet — check if the file exists on disk.
            // If it does, it's a change; if not, it's a creation.
            !matches!(
                ExistingPathKind::from_system(system, path),
                ExistingPathKind::File
            )
        }
    };
    if is_new {
        ChangeEvent::Created {
            path: path.clone(),
            kind: CreatedKind::File,
        }
    } else {
        ChangeEvent::file_content_changed(path.clone())
    }
}

/// Classify a *disk ingest* for `path`: first the overlay for this path has
/// been forgotten, so `ExistingPathKind::from_system` reads the underlying disk
/// truth through the overlay. Returns the appropriate `ChangeEvent`.
///
/// Phase 5 inlines the equivalent classification into `build_plan`'s `SyncPath`
/// arm (from the disk-read result + db interning, before the overlay is
/// republished), so this standalone helper is now exercised only by the
/// `sync_path_reingests_disk` unit test that documents the classification.
#[cfg_attr(not(test), allow(dead_code))]
fn classify_disk_sync(
    system: &OverlaySystem,
    db: &ProjectDatabase,
    path: &SystemPathBuf,
) -> ChangeEvent {
    match ExistingPathKind::from_system(system, path) {
        ExistingPathKind::File => {
            let is_new = db.files().try_system(db, path).is_none_or(|f: File| !f.exists(db));
            if is_new {
                ChangeEvent::Created {
                    path: path.clone(),
                    kind: CreatedKind::File,
                }
            } else {
                ChangeEvent::Changed {
                    path: path.clone(),
                    kind: ChangedKind::Any,
                }
            }
        }
        // Directory or absent → treat as a (possibly recursive) delete.
        _ => ChangeEvent::Deleted {
            path: path.clone(),
            kind: DeletedKind::Any,
        },
    }
}

fn identity_scope_from_events(events: &[ChangeEvent]) -> Option<HashSet<String>> {
    let scope: HashSet<String> = events
        .iter()
        .filter_map(|event| event.system_path())
        .filter(|path| {
            path.extension()
                .and_then(PySourceType::try_from_extension)
                .is_some()
        })
        .map(|path| path.as_str().to_string())
        .collect();

    if scope.is_empty() {
        None
    } else {
        Some(scope)
    }
}

#[derive(Default)]
struct IdentityDelta {
    /// Minted ids this revision.
    created_ids: Vec<String>,
    /// Ids whose content hash changed (no over-fire).
    changed_ids: Vec<String>,
    /// Retired ids this revision (== `orphaned`).
    deleted_ids: Vec<String>,
    /// Structured moves: id + old/new qualified path + old/new file.
    moved: Vec<dto::MovedEntityDto>,
    /// Closure of `changed ∪ deleted` under the head code layer's reverse-deps.
    /// Phase 3: the layer is empty, so this equals the seeds.
    affected_ids: Vec<String>,
    needs_review: Vec<String>,
    orphaned: Vec<String>,
    extracted: usize,
    scope_files: usize,
    /// DurableIds flagged `needs_review` that also have an authored record
    /// in a `review_on_change = true` layer.
    authored_needs_review: Vec<String>,
    /// DurableIds flagged `orphaned` that also have an authored record
    /// in a `review_on_change = true` layer.
    authored_orphaned: Vec<String>,
}

/// Assemble the public `CommitDeltaDto` from the id-level identity classes and
/// the path-shaped metadata the write method produced.
///
/// The nested `code_delta` (§6.2) is the minimal incremental delta from the
/// in-commit producer (`produce_layer` → `CodeLayer::diff_from`): `Some({…})`
/// for a structural change, `Some({})` for a cosmetic edit (applier no-op), and
/// a full rescan-flagged delta on `rescan` / cold start. `None` is only carried
/// by writes that don't reconcile (e.g. `author`). `touched_files` is the union
/// of the path-level created/changed/deleted strings — metadata only.
fn build_commit_delta(
    revision: u64,
    created: Vec<String>,
    changed: Vec<String>,
    deleted: Vec<String>,
    identity: IdentityDelta,
    code_delta: Option<dto::CodeDeltaDto>,
    rescan: bool,
    project_changed: bool,
    custom_stdlib_changed: bool,
) -> dto::CommitDeltaDto {
    let mut touched_files = created.clone();
    touched_files.extend(changed.iter().cloned());
    touched_files.extend(deleted.iter().cloned());

    dto::CommitDeltaDto {
        revision,
        created_ids: identity.created_ids,
        changed_ids: identity.changed_ids,
        deleted_ids: identity.deleted_ids,
        moved: identity.moved,
        authored_ids: vec![],
        affected_ids: identity.affected_ids,
        code_delta,
        touched_files,
        created,
        changed,
        deleted,
        needs_review: identity.needs_review,
        orphaned: identity.orphaned,
        authored_needs_review: identity.authored_needs_review,
        authored_orphaned: identity.authored_orphaned,
        identity_extracted: identity.extracted,
        identity_scope_files: identity.scope_files,
        rescan,
        project_changed,
        custom_stdlib_changed,
    }
}

/// Intersect the reconciliation `needs_review`/`orphaned` ids with the set
/// of ids that have an authored record in a `review_on_change = true` layer.
///
/// A layer with `review_on_change = false` contributes nothing here — its
/// records always report status `present` regardless of registry status.
fn compute_authored_lifecycle(
    config: &ValidatedConfig,
    authored: &AuthoredStore,
    nr_ids: &[String],
    orph_ids: &[String],
) -> (Vec<String>, Vec<String>) {
    // Collect the set of all durable ids that have an authored record in a
    // review_on_change layer.
    let mut monitored: std::collections::HashSet<String> = std::collections::HashSet::new();
    for name in &config.topo_order {
        let Some(layer_cfg) = config.raw.layers.get(name) else {
            continue;
        };
        if !matches!(layer_cfg.origin, config::LayerOrigin::Authored) {
            continue;
        }
        if !layer_cfg.review_on_change {
            continue;
        }
        for id in authored.ids_in_layer(name) {
            monitored.insert(id.to_string());
        }
    }

    let authored_nr: Vec<String> = nr_ids
        .iter()
        .filter(|id| monitored.contains(id.as_str()))
        .cloned()
        .collect();
    let authored_orph: Vec<String> = orph_ids
        .iter()
        .filter(|id| monitored.contains(id.as_str()))
        .cloned()
        .collect();
    (authored_nr, authored_orph)
}

/// Derive the authored record status for `(layer, id)` at a snapshot revision.
///
/// Status is derived, not stored — the registry (captured per snapshot) is
/// the single source of truth.  The key decision table:
///
/// ```text
/// status_at(layer, id, R) =
///     absent                       if no authored record for (layer, id) with rev ≤ R
///     present                      if layer.review_on_change == false
///     map(registry@R .anchor(id).status)
///         Active      → present
///         NeedsReview → needs_review
///         Orphaned    → orphaned
/// ```
fn derive_authored_status(
    config: &ValidatedConfig,
    registry: Option<&IdentityRegistry>,
    layer: &str,
    id: &str,
) -> String {
    // If review_on_change is false, always present.
    let review_on_change = config
        .raw
        .layers
        .get(layer)
        .map(|lc| lc.review_on_change)
        .unwrap_or(false);
    if !review_on_change {
        return "present".to_string();
    }

    // Map registry status.
    let durable = DurableId(id.to_string());
    match registry.and_then(|r| r.status_of(&durable)) {
        Some(IdentityStatus::Active) => "present".to_string(),
        Some(IdentityStatus::NeedsReview) => "needs_review".to_string(),
        Some(IdentityStatus::Orphaned) => "orphaned".to_string(),
        None => "present".to_string(),  // unknown id → treat as present (no lifecycle to surface)
    }
}

/// Shared identity reconciliation: extract entities, reconcile against
/// registry, persist, and return identity delta fields.
///
/// `scope` is derived from the committed `ChangeEvent` paths. Identity anchors
/// are file-local (`qualified_path` begins with the defining file), so this is
/// the bounded closure needed by the identity layer; cross-file graph
/// invalidation continues to use the code graph's reverse-dependency index.
fn run_identity_reconciliation(
    head: &mut HeadState,
    scope: Option<&HashSet<String>>,
    next_rev: Revision,
) -> IdentityDelta {
    let state = TyProjectState {
        db: head.db.clone(),
        root: head.root.clone(),
        registry: None,
        hash_policy: head.hash_policy,
        hash_policies: head.hash_policies.clone(),
        default_hash_profile: head.default_hash_profile.clone(),
        authored: None,
    };
    let entities = match scope {
        Some(scope) => extract_entities_for(&state, scope),
        None => extract_entities(&state),
    };
    let extracted = entities.len();
    // Reconcile against the revision this staged commit WILL publish
    // (`next_rev`), not the store's current (pre-publish) revision — the store
    // bump is deferred to the publish tail (§5.3 publish-last).
    let recon = match scope {
        Some(scope) => reconcile_scoped(&mut head.registry, &entities, next_rev, scope),
        None => reconcile(&mut head.registry, &entities, next_rev),
    };

    // Id-level classification (§5.4): created / changed (hash-based, no over-fire)
    // / deleted / structured moves. Reads the old hashes captured on the bindings
    // before the in-pass rebind overwrote them.
    let classes = recon.classify(&entities);
    let created_ids: Vec<String> = classes.created.iter().map(|id| id.0.clone()).collect();
    let changed_ids: Vec<String> = classes.changed.iter().map(|id| id.0.clone()).collect();
    let deleted_ids: Vec<String> = classes.deleted.iter().map(|id| id.0.clone()).collect();
    let moved: Vec<dto::MovedEntityDto> = classes
        .moved
        .iter()
        .map(|m| dto::MovedEntityDto {
            id: m.id.0.clone(),
            old_qualified_path: m.old_qualified_path.clone(),
            new_qualified_path: m.new_qualified_path.clone(),
            old_file: m.old_file.clone(),
            new_file: m.new_file.clone(),
        })
        .collect();

    let needs_review: Vec<String> = recon.needs_review.iter().map(|id| id.0.clone()).collect();
    let orphaned: Vec<String> = recon.retired.iter().map(|id| id.0.clone()).collect();

    // affected_ids is NOT computed here: the in-commit producer (§6.1) runs
    // AFTER reconciliation and maintains `head.code_layer.reverse_deps`, so the
    // transitive, container-granular closure is computed in `run_staged` over
    // the freshly-updated layer (seeding deletions from the prior layer, §6.3).
    // Left empty here and overwritten there.
    let affected_ids: Vec<String> = Vec::new();

    // Authored lifecycle surfacing: intersect reconciliation output with
    // authored record ids in `review_on_change = true` layers.
    let (authored_needs_review, authored_orphaned) =
        compute_authored_lifecycle(&head.config, &head.authored, &needs_review, &orphaned);

    // NOTE (Phase 5): identity persistence is NO LONGER done here. It used to
    // `write_atomic` the registry and *swallow* the error (log-and-continue),
    // which is the §5.3 "succeeds in memory but fails to persist" / §5.12 "do
    // not swallow" violation this phase removes. Persistence is now a staged,
    // fallible, *propagated* step in `commit` (`persist_identity` → the typed
    // `SidecarWriteError`), run BEFORE the deferred publish so a write failure
    // rolls the whole commit back to R−1.

    // NOTE (Phase 2): the native code layer is *not* produced eagerly here.
    // The Phase 2 producer is a full build (full semantic analysis: occurrence
    // resolution + type-hierarchy/typeshed warmup), so running it inside every
    // commit/open would make `open()` ~100x slower and serialise on the GIL.
    // Phase 2's deliverable is parity, served on demand by
    // `PyTyProject::full_code_delta`. The eager in-commit hookup (§5.3 step 4)
    // is deferred to Phase 3, where the producer becomes incremental/scoped and
    // the commit is staged (Phase 5). See PROGRESS.md §5.

    IdentityDelta {
        created_ids,
        changed_ids,
        deleted_ids,
        moved,
        affected_ids,
        needs_review,
        orphaned,
        extracted,
        scope_files: scope.map_or(0, |scope| scope.len()),
        authored_needs_review,
        authored_orphaned,
    }
}

// ── Phase 5: the single native commit(mutation) funnel ───────────────────
//
// Every write kind (edit / edit_many / edit_virtual / sync_path / discard /
// sync_all / author / watcher poll_changes) funnels through `commit`. It STAGES
// all next-state, runs every fallible step (serialise, write_atomic, engine
// apply, code-layer stage, delta compute) against the stage, and PUBLISHES the
// revision LAST (`ContentStore::publish_staged`). A failure in any in-lock step
// returns a typed error and rolls back to R−1 with no torn publish and no bus
// delta (§5.3). Strategy B+ ("deferred publish"): the content store — the
// observable `head` revision, snapshot content, and retained window — is never
// mutated until the publish tail, so a failed commit cannot have advanced it.
// The registry/authored/overlay are mutated in place (reconciliation must run
// the live db over the staged content) and restored from a baseline on failure;
// the salsa db is left benignly ahead (it re-reads the restored overlay → same
// content → recomputes equal) because salsa cannot be rolled back: it shares
// `Zalsa` storage with any clone, and a rollback clone would deadlock the next
// `apply_changes` (it blocks until it is the sole live handle).

/// What a write does, decoupled from how it commits.
enum Mutation {
    /// edit / edit_many / edit_virtual — overlay buffer edits already classified
    /// against the pre-edit content by the thin PyO3 method.
    Overlay {
        changes: Vec<crate::content::Change>,
        events: Vec<ChangeEvent>,
        created: Vec<String>,
        changed: Vec<String>,
        /// System paths that gained a genuinely unsaved buffer (for the watcher
        /// buffer-wins set). Empty for virtual edits.
        unsaved: Vec<SystemPathBuf>,
    },
    /// sync_path / discard — re-read one path from disk.
    SyncPath { abs: SystemPathBuf },
    /// sync_all — re-ingest the whole project from disk (one rescan revision).
    SyncAll,
    /// author — write one authored record (no content / engine change).
    Author { layer: String, id: String, value: serde_json::Value },
    /// watcher poll_changes — fold a drained batch of disk events.
    Poll { events: Vec<ChangeEvent> },
}

/// An authored record staged for persistence (the §5.3 stage → persist →
/// publish ordering). Built before any state moves; persisted as a staged step;
/// the in-memory store swap happens only at the publish tail.
struct AuthoredPlan {
    next_store: AuthoredStore,
    record_path: std::path::PathBuf,
    bytes: Vec<u8>,
    id: String,
}

/// The fully-built next-state of a commit, ready for the staged transaction.
/// Nothing here is observable to a reader until `run_staged` reaches its tail.
struct StagedCommit {
    /// The next generation, built without advancing the store.
    staged_gen: Generation,
    /// Engine change events to apply to the head db.
    events: Vec<ChangeEvent>,
    /// Path-shaped delta metadata (Phase 3 still carries these alongside ids).
    created: Vec<String>,
    changed: Vec<String>,
    deleted: Vec<String>,
    rescan: bool,
    /// Identity reconciliation scope (None ⇒ full reconcile).
    scope: Option<HashSet<String>>,
    /// Run identity reconciliation + persist the registry? (false for author.)
    reconcile: bool,
    /// Authored record to persist + swap in at the publish tail (author only).
    authored: Option<AuthoredPlan>,
    /// Paths to add to the unsaved-overlay set at the publish tail.
    unsaved_insert: Vec<SystemPathBuf>,
    /// Paths to drop from the unsaved-overlay set at the publish tail.
    unsaved_remove: Vec<SystemPathBuf>,
}

/// State a failed commit must restore. The store is NOT here: it is never
/// mutated before the publish tail (deferred publish), so there is nothing to
/// undo for it.
struct Baseline {
    published_gen: Generation,
    registry: IdentityRegistry,
    authored: AuthoredStore,
    /// The code layer before the in-commit producer ran. A failed commit must
    /// restore it so a torn (half-re-derived) layer is never observable (§6.1 /
    /// rollback test 3).
    code_layer: crate::code_layer::CodeLayer,
}

/// TEST-ONLY: fire the armed one-shot fault if it names `stage`. The arm is
/// taken once at the top of the commit, so this is a pure check; it returns the
/// typed error the rollback contract expects (a sidecar persist failure →
/// `SidecarWriteError`; a non-sidecar stage → `CommitFailed`). Inert (always
/// `Ok`) unless the test armed exactly this stage.
fn check_fault(armed: &Option<String>, stage: &str) -> PyResult<()> {
    if armed.as_deref() == Some(stage) {
        return Err(match stage {
            "identity_persist" | "authored_persist" => {
                SidecarWriteError::new_err(format!("injected commit fault at stage {stage:?}"))
            }
            other => CommitFailed::new_err(format!("injected commit fault at stage {other:?}")),
        });
    }
    Ok(())
}

/// Serialise the reconciled identity registry and persist it crash-safely.
/// Propagates a `SidecarWriteError` on a write failure (§5.12 — never swallowed,
/// as the pre-Phase-5 `log::error!`-and-continue did), so the `?` short-circuits
/// BEFORE the deferred publish and the commit rolls back.
fn persist_identity(head: &HeadState) -> PyResult<()> {
    let bytes = head.registry.to_bytes().map_err(|e| {
        CommitFailed::new_err(format!("failed to serialise identity registry: {e}"))
    })?;
    let identity_path = head.sidecar.identity_db_path();
    head.sidecar.write_atomic(&identity_path, &bytes).map_err(|e| {
        SidecarWriteError::new_err(format!(
            "failed to persist identity registry at {identity_path:?}: {e}"
        ))
    })
}

/// Restore the baseline after a failed commit and hand the error back. The store
/// is untouched (deferred publish), so head / retained / snapshot content are
/// already at R−1; this re-publishes the prior generation to the live overlay
/// and restores the in-place registry / authored mutations.
fn rollback(head: &mut HeadState, base: Baseline, err: PyErr) -> PyErr {
    head.system.publish(base.published_gen);
    head.registry = base.registry;
    head.authored = base.authored;
    head.code_layer = base.code_layer;
    err
}

/// A disk-read result → a store `Change` (Insert with content, or a tombstone
/// when the file could not be read).
fn disk_change(path: &SystemPathBuf, disk_text: std::io::Result<String>) -> crate::content::Change {
    match disk_text {
        Ok(text) => crate::content::Change::Insert { path: path.clone(), text: Arc::from(text) },
        Err(_) => crate::content::Change::Delete { path: path.clone() },
    }
}

/// Build the next-state plan for `mutation` WITHOUT mutating any observable head
/// state. Returns `Ok(None)` for a no-op (an empty watcher poll), or `Err` for a
/// pre-stage validation failure (author config / JSON / id checks). Disk reads,
/// event classification, and authored serialisation all happen here, before the
/// staged transaction begins.
fn build_plan(head: &mut HeadState, mutation: Mutation) -> PyResult<Option<StagedCommit>> {
    match mutation {
        Mutation::Overlay { changes, events, created, changed, unsaved } => {
            let staged_gen = head.store.stage(changes);
            let scope = identity_scope_from_events(&events);
            Ok(Some(StagedCommit {
                staged_gen,
                events,
                created,
                changed,
                deleted: vec![],
                rescan: false,
                scope,
                reconcile: true,
                authored: None,
                unsaved_insert: unsaved,
                unsaved_remove: vec![],
            }))
        }
        Mutation::SyncPath { abs } => {
            // Read disk once; classify Created/Changed/Deleted from the result
            // and the db's current interning (equivalent to classify_disk_sync,
            // but without depending on the overlay being already republished).
            let disk_text = std::fs::read_to_string(abs.as_std_path());
            let path_str = abs.as_str().to_string();
            let (change, event, created, changed, deleted) = match disk_text {
                Ok(text) => {
                    let is_new = head
                        .db
                        .files()
                        .try_system(&head.db, &abs)
                        .is_none_or(|f: File| !f.exists(&head.db));
                    let change = crate::content::Change::Insert {
                        path: abs.clone(),
                        text: Arc::from(text),
                    };
                    if is_new {
                        (change,
                         ChangeEvent::Created { path: abs.clone(), kind: CreatedKind::File },
                         vec![path_str], vec![], vec![])
                    } else {
                        (change,
                         ChangeEvent::Changed { path: abs.clone(), kind: ChangedKind::Any },
                         vec![], vec![path_str], vec![])
                    }
                }
                Err(_) => (
                    crate::content::Change::Delete { path: abs.clone() },
                    ChangeEvent::Deleted { path: abs.clone(), kind: DeletedKind::Any },
                    vec![], vec![], vec![path_str],
                ),
            };
            let staged_gen = head.store.stage(vec![change]);
            // Syncing a project-config file is a coarse change (§5.4): rescan.
            let rescan = crate::content::is_project_config_file(&abs);
            let scope = if rescan {
                None
            } else {
                identity_scope_from_events(std::slice::from_ref(&event))
            };
            Ok(Some(StagedCommit {
                staged_gen,
                events: vec![event],
                created,
                changed,
                deleted,
                rescan,
                scope,
                reconcile: true,
                authored: None,
                unsaved_insert: vec![],
                // A disk sync supersedes any unsaved buffer for this path.
                unsaved_remove: vec![abs],
            }))
        }
        Mutation::SyncAll => {
            // Re-ingest the whole project from disk so files created/changed
            // after open are discovered (Phase 5 carry-over of the Phase 1
            // `sync_all` gap). Unsaved overlay buffers are preserved.
            let root = head.root.clone();
            let skip = head.unsaved_overlays.clone();
            let staged_gen = head.store.stage_reingest(&root, is_project_relevant, &skip);
            Ok(Some(StagedCommit {
                staged_gen,
                events: vec![ChangeEvent::Rescan],
                created: vec![],
                changed: vec![],
                deleted: vec![],
                rescan: true,
                scope: None,
                reconcile: true,
                authored: None,
                unsaved_insert: vec![],
                unsaved_remove: vec![],
            }))
        }
        Mutation::Author { layer, id, value } => {
            // Pre-stage validation (may return ConfigError / ValueError before
            // any state moves).
            let lcfg = head.config.authored_layer_config(&layer).ok_or_else(|| {
                PyConfigError::new_err(format!("'{layer}' is not a declared authored layer"))
            })?;
            let history = lcfg.history;
            let durable_id = DurableId(id.clone());
            if head.registry.get(&durable_id).is_none() {
                return Err(PyValueError::new_err(format!(
                    "durable id '{id}' is not known to the identity registry"
                )));
            }
            // Stage the copy-on-write authored store and serialise the record —
            // do NOT swap it into head yet (that is the publish tail, §5.3).
            let version = crate::authored::AuthoredVersion {
                value,
                revision: head.store.next_revision().0,
            };
            let new_map = head.authored.put(&layer, &id, version, history);
            let next_store = AuthoredStore::new(new_map);
            let rec = next_store.records_get(&layer, &id).unwrap();
            let doc = AuthoredRecordDoc {
                format_version: crate::authored::AUTHORED_FORMAT_VERSION,
                layer: layer.clone(),
                durable_id: id.clone(),
                current: rec.current.clone(),
                history: rec.history.clone(),
            };
            let bytes = doc.to_bytes().map_err(|e| {
                CommitFailed::new_err(format!("failed to serialise authored record: {e}"))
            })?;
            let record_path = head.sidecar.record_path(&layer, &id);
            Ok(Some(StagedCommit {
                // No content change: the authored edit is a revision with
                // identical content (an empty bump, retained).
                staged_gen: head.store.stage_unchanged(),
                events: vec![],
                created: vec![],
                changed: vec![],
                deleted: vec![],
                rescan: false,
                scope: None,
                reconcile: false,
                authored: Some(AuthoredPlan { next_store, record_path, bytes, id }),
                unsaved_insert: vec![],
                unsaved_remove: vec![],
            }))
        }
        Mutation::Poll { events } => build_poll_plan(head, events),
    }
}

/// Build the staged plan for a drained watcher batch (the old `apply_watch_events`
/// fold). Returns `Ok(None)` when nothing survives filtering, so `poll_changes`
/// returns `None` WITHOUT bumping the revision (a no-op, never a published empty
/// revision).
fn build_poll_plan(
    head: &mut HeadState,
    events: Vec<ChangeEvent>,
) -> PyResult<Option<StagedCommit>> {
    if events.is_empty() {
        return Ok(None);
    }
    // Rescan short-circuit: if ty lost sync, redo everything (like sync_all, but
    // the watcher path stages the current content — the engine re-walks disk).
    if events.iter().any(|e| e.is_rescan()) {
        return Ok(Some(StagedCommit {
            staged_gen: head.store.stage_unchanged(),
            events: vec![ChangeEvent::Rescan],
            created: vec![],
            changed: vec![],
            deleted: vec![],
            rescan: true,
            scope: None,
            reconcile: true,
            authored: None,
            unsaved_insert: vec![],
            unsaved_remove: vec![],
        }));
    }

    // Keep only real-path events whose path is NOT a genuinely unsaved buffer
    // (the buffer-wins rule, now gated on `unsaved_overlays`, not `has_overlay`
    // — Phase 1 interns every file at open, so `has_overlay` would drop them all).
    let mut store_changes: Vec<crate::content::Change> = Vec::with_capacity(events.len());
    let mut kept_events: Vec<ChangeEvent> = Vec::with_capacity(events.len());
    let (mut created, mut changed, mut deleted) = (Vec::new(), Vec::new(), Vec::new());
    for event in events {
        let Some(path) = event.system_path() else {
            continue;
        };
        let path = path.to_path_buf();
        if head.unsaved_overlays.contains(&path) {
            // Unsaved buffer wins; ignore the disk event.
            continue;
        }
        let path_str = path.as_str().to_string();
        let disk_text = std::fs::read_to_string(path.as_std_path());
        match &event {
            ChangeEvent::Created { .. } => {
                created.push(path_str);
                store_changes.push(disk_change(&path, disk_text));
            }
            ChangeEvent::Deleted { .. } => {
                deleted.push(path_str);
                store_changes.push(crate::content::Change::Delete { path: path.clone() });
            }
            ChangeEvent::Changed { .. } => {
                changed.push(path_str);
                store_changes.push(disk_change(&path, disk_text));
            }
            _ => continue,
        }
        kept_events.push(event);
    }

    if kept_events.is_empty() {
        return Ok(None);
    }

    let staged_gen = head.store.stage(store_changes);
    let scope = identity_scope_from_events(&kept_events);
    Ok(Some(StagedCommit {
        staged_gen,
        events: kept_events,
        created,
        changed,
        deleted,
        rescan: false,
        scope,
        reconcile: true,
        authored: None,
        unsaved_insert: vec![],
        unsaved_remove: vec![],
    }))
}

/// Convert an absolute native path string to a project-relative POSIX graph
/// path (matching `code_layer::Builder::to_graph_path`). Returns `None` for a
/// path outside the root (external — no graph node).
fn native_to_graph(root: &SystemPathBuf, native: &str) -> Option<String> {
    let rest = native.strip_prefix(root.as_str())?;
    let rest = rest.strip_prefix('/').unwrap_or(rest);
    (!rest.is_empty()).then(|| rest.to_string())
}

/// Drive the in-commit code-layer producer (§6.1/6.2). Returns the next layer
/// and the minimal incremental `code_delta`.
///
/// - `rescan` (or an unscoped write) → a full build diffed against an empty
///   layer, i.e. a full `rescan`-flagged delta the Python applier applies
///   wholesale.
/// - an empty `prev` (first write of a session — the head layer is built lazily,
///   never at open, to keep `open()` off the producer's cost path) → a one-time
///   full build, likewise emitted as a full delta.
/// - otherwise → the scoped producer over the dirty graph paths (the identity
///   scope = changed ∪ created ∪ deleted), expanding one-hop importers and
///   maintaining `reverse_deps` edge-by-edge.
fn produce_layer(
    state: &TyProjectState,
    prev: &crate::code_layer::CodeLayer,
    staged: &StagedCommit,
    next_rev: Revision,
) -> (crate::code_layer::CodeLayer, dto::CodeDeltaDto) {
    use crate::code_layer::{produce_code_delta, produce_code_delta_scoped, CodeLayer};
    let rev = next_rev.0;
    let dirty: Option<HashSet<String>> = staged.scope.as_ref().map(|s| {
        s.iter()
            .filter_map(|n| native_to_graph(&state.root, n))
            .collect()
    });
    match dirty {
        Some(dirty) if !staged.rescan && !prev.is_empty() => {
            produce_code_delta_scoped(state, prev, &dirty, rev)
        }
        // Full build: rescan, cold start (empty prev), or an unscoped write.
        _ => produce_code_delta(state, &CodeLayer::new(), rev, staged.rescan, None),
    }
}

/// Run the staged transaction: publish the staged content to the live overlay,
/// apply the engine change, reconcile, fire the staged fault boundaries, persist
/// the sidecar, and PUBLISH the revision LAST. Any `?` failure leaves the store
/// untouched (the publish tail was never reached) and propagates so the caller
/// rolls the in-place mutations back.
fn run_staged(
    head: &mut HeadState,
    staged: StagedCommit,
    armed: &Option<String>,
) -> PyResult<dto::CommitDeltaDto> {
    let next_rev = head.store.next_revision();

    // 1. Publish the staged content to the live overlay so the head db analyses
    //    the next generation. (Observable only to lock-guarded floating reads;
    //    new snapshots pin the store, which is still at R−1.)
    head.system.publish(Arc::clone(&staged.staged_gen));

    // 2. Apply the engine change (skipped for authored writes — no content
    //    changed, so the db must not be touched).
    let (project_changed, custom_stdlib_changed) = if staged.events.is_empty() {
        (false, false)
    } else {
        let result = head.db.apply_changes(&staged.events, None);
        (result.project_changed(), result.custom_stdlib_changed())
    };

    // 3 + 4 + 5. Identity reconcile → code-layer produce → identity persist.
    let (identity, code_delta) = if staged.reconcile {
        let mut identity = run_identity_reconciliation(head, staged.scope.as_ref(), next_rev);

        // ── Scoped in-commit code-layer producer (§6.1/6.2/6.3) ──
        // Re-derive the dirty scope over the prior layer, update head.code_layer,
        // and emit the minimal incremental code_delta. Runs inside the lock,
        // before the deferred publish; rolled back via the captured Baseline.
        let prev = std::mem::take(&mut head.code_layer);
        let state = head.read_clone();
        let (next, code_delta) = produce_layer(&state, &prev, &staged, next_rev);

        // affected_ids = transitive, container-granular closure of changed ∪
        // deleted over the freshly-maintained reverse_deps, seeding deletions
        // from the prior layer (§6.3).
        let seeds: std::collections::BTreeSet<String> = identity
            .changed_ids
            .iter()
            .chain(identity.deleted_ids.iter())
            .cloned()
            .collect();
        let deleted: std::collections::BTreeSet<String> =
            identity.deleted_ids.iter().cloned().collect();
        identity.affected_ids = next
            .affected_closure_with_deleted(&prev, &seeds, &deleted)
            .into_iter()
            .collect();
        head.code_layer = next;

        // Code-layer staging boundary fault seam: a producer/code-layer failure
        // must publish no partial revision (rollback test 3).
        check_fault(armed, "code_layer")?;
        // Identity persistence: a staged, fallible, PROPAGATED step.
        check_fault(armed, "identity_persist")?;
        persist_identity(head)?;
        (identity, Some(code_delta))
    } else {
        (IdentityDelta::default(), None)
    };

    // 6. Authored persistence (author only): stage → persist (publish at tail).
    if let Some(ap) = &staged.authored {
        check_fault(armed, "authored_persist")?;
        head.sidecar.write_atomic(&ap.record_path, &ap.bytes).map_err(|e| {
            SidecarWriteError::new_err(format!(
                "failed to persist authored record at {:?}: {e}",
                ap.record_path
            ))
        })?;
    }

    // 7. PUBLISH LAST — the single, last in-lock observable mutation. After this
    //    point nothing can fail; before it, every failure path left the store at
    //    R−1.
    let revision = head.store.publish_staged(staged.staged_gen).0;

    // Swap in the authored store and update the unsaved-overlay set now that the
    // commit is irrevocable.
    let authored_ids = if let Some(ap) = staged.authored {
        head.authored = ap.next_store;
        vec![ap.id]
    } else {
        vec![]
    };
    for p in staged.unsaved_insert {
        head.unsaved_overlays.insert(p);
    }
    for p in &staged.unsaved_remove {
        head.unsaved_overlays.remove(p);
    }

    // 8. Build the id-level commit delta.
    let mut dto = build_commit_delta(
        revision,
        staged.created,
        staged.changed,
        staged.deleted,
        identity,
        code_delta,
        staged.rescan,
        project_changed,
        custom_stdlib_changed,
    );
    if !authored_ids.is_empty() {
        dto.authored_ids = authored_ids;
    }
    Ok(dto)
}

/// The single native commit funnel (§5.3). Builds the plan, captures the
/// rollback baseline, takes the one-shot fault arm, runs the staged transaction,
/// and rolls back on any failure. Returns `Ok(None)` for a no-op (an empty
/// watcher poll → Python `None`, NOT a published empty revision).
fn commit(head: &mut HeadState, mutation: Mutation) -> PyResult<Option<dto::CommitDeltaDto>> {
    let Some(staged) = build_plan(head, mutation)? else {
        return Ok(None);
    };
    let base = Baseline {
        published_gen: head.store.capture(),
        registry: head.registry.clone(),
        authored: head.authored.clone(),
        code_layer: head.code_layer.clone(),
    };
    // One-shot: consumed by THIS commit whether or not its stage is reached, so
    // a stale arm can never leak into a later unrelated write.
    let armed = head.armed_fault.take();
    match run_staged(head, staged, &armed) {
        Ok(dto) => Ok(Some(dto)),
        Err(e) => Err(rollback(head, base, e)),
    }
}

/// Pythonize a commit result for the write methods that always publish a
/// revision (every kind except `poll_changes`). A `None` here would mean a
/// staged plan reported a no-op for a write that must produce a delta — a bug,
/// surfaced as `CommitFailed` rather than silently swallowed.
fn commit_dto_to_py<'py>(
    py: Python<'py>,
    dto: Option<dto::CommitDeltaDto>,
) -> PyResult<Bound<'py, PyAny>> {
    let dto = dto.ok_or_else(|| CommitFailed::new_err("commit produced no delta"))?;
    pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
}

// ── PyO3 Methods ─────────────────────────────────────────────────────────

#[pymethods]
impl PyTyProject {
    /// Open a project at the given root directory.
    #[staticmethod]
    fn open(root: &str) -> PyResult<Self> {
        let root_path = PathBuf::from(root);
        let absolute = root_path.canonicalize().map_err(|e| {
            PathResolutionError::new_err(format!("Cannot resolve root '{}': {}", root, e))
        })?;
        let s = absolute.to_str().ok_or_else(|| {
            PathResolutionError::new_err(format!(
                "Path '{}' contains non-UTF-8 characters",
                absolute.display()
            ))
        })?;
        let system_root = SystemPathBuf::from(s);

        let sidecar = Sidecar::new(&absolute);
        let raw_config = RawConfig::load(&sidecar).map_err(config_error_to_pyerr)?;
        let config = config::validate(raw_config).map_err(config_error_to_pyerr)?;
        if sidecar.exists() {
            if config.raw.sidecar.gitignore_cache {
                sidecar.ensure_gitignore_cache_policy().map_err(|e| {
                    PyConfigError::new_err(format!("failed to update sidecar .gitignore: {e}"))
                })?;
            }
            sidecar.ensure_layout_marker().map_err(|e| {
                PyConfigError::new_err(format!("failed to update sidecar layout marker: {e}"))
            })?;
        }
        let registry = match fs::read(sidecar.identity_db_path()) {
            Ok(bytes) => match IdentityRegistry::from_bytes(&bytes) {
                Ok(registry) => registry,
                Err(FormatError::UnknownVersion(version)) => {
                    return Err(FormatVersionError::new_err(format!(
                        "unknown identity.db format_version: {version}"
                    )));
                }
                Err(e) => {
                    log::warn!("Failed to load identity registry: {} — starting fresh.", e);
                    IdentityRegistry::default()
                }
            },
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => IdentityRegistry::default(),
            Err(e) => {
                log::warn!("Failed to read identity registry: {} — starting fresh.", e);
                IdentityRegistry::default()
            }
        };

        let retain_cap = config.raw.spine.retain_cap;
        let mut store = ContentStore::with_retain_cap(retain_cap);

        // Phase 1: ingest project content into the initial revision before
        // building the live database.  This makes the generation complete so
        // no later read triggers a write, and snapshot construction can
        // build frozen views directly from the generation without disk I/O.
        //
        // Convention: the store seeds Revision(0) as empty; ingest_project
        // produces Revision(1) as the first populated generation, so the
        // initial observable head revision is 1.
        store.ingest_project(&system_root, is_project_relevant);

        let mut head = build_head_with_config(
            system_root,
            store,
            registry,
            config.clone(),
        );

        // Phase 1: reconcile identity at open so the registry is populated
        // before any read occurs.  Identity must already be resolved so a
        // later graph read never calls sync_all (Phase 4). The open ingest
        // already published the initial revision, so reconcile against the
        // current revision (not next_revision — that offset is for the
        // deferred-publish commit path).
        let open_rev = head.store.revision();
        run_identity_reconciliation(&mut head, None, open_rev);
        // Persist the reconciled registry at open so a reopen restores bindings
        // (§5.10). Previously done inside reconciliation; Phase 5 makes identity
        // persistence an explicit, propagated step.
        persist_identity(&head)?;

        // Load authored records for each declared authored layer (§11.3.2).
        head.authored = load_authored_records(&head.config, &head.sidecar)
            .map_err(|e| {
                match e {
                    AuthoredLoadError::Json(msg) => {
                        FormatVersionError::new_err(format!("authored record parse error: {msg}"))
                    }
                    AuthoredLoadError::UnknownVersion(v) => {
                        FormatVersionError::new_err(format!(
                            "unknown authored record format_version: {v}"
                        ))
                    }
                }
            })?;

        Ok(PyTyProject {
            inner: Arc::new(Mutex::new(Some(head))),
            pending: Arc::new(Mutex::new(Vec::new())),
            watcher: Mutex::new(None),
        })
    }

    fn config_json(&self) -> PyResult<String> {
        let guard = lock_state(&self.inner, "config_json")?;
        let head = guard.as_ref().unwrap();
        serde_json::to_string(&head.config)
            .map_err(|e| PyConfigError::new_err(format!("failed to serialise config: {e}")))
    }

    // ── Lifecycle: Reload ────────────────────────────────────────────

    /// Reload the project: drop the current database and re-create it.
    /// Clears all cached diagnostics and symbol data. Preserves any overlay
    /// content across the rebuild so Phase 3 edits survive a reload.
    ///
    /// Holds the lock throughout — no window where concurrent callers
    /// see a closed project.
    fn reload(&self) -> PyResult<()> {
        let mut guard = lock_state(&self.inner, "reload")?;
        let head = guard.as_mut().unwrap();

        let root = head.root.clone();
        // Preserve overlay content, identity registry, and validated config across the rebuild.
        let store = std::mem::replace(&mut head.store, ContentStore::new());
        let registry = std::mem::replace(&mut head.registry, IdentityRegistry::default());
        let config = head.config.clone();

        // Rebuild while still holding the lock, then swap atomically.
        *guard = Some(build_head_with_config(root, store, registry, config));
        drop(guard);

        // If a watcher is running, update its watched paths (the head db
        // changed). If update() errors, we leave the old watch set — fine for
        // a same-root reload where paths are identical.
        let mut w = self.watcher.lock().map_err(|e| {
            PyRuntimeError::new_err(format!("watcher lock poisoned: {e}"))
        })?;
        if let Some(ref mut pw) = *w {
            let inner_guard = lock_state(&self.inner, "reload")?;
            pw.update(&inner_guard.as_ref().unwrap().db);
        }
        Ok(())
    }

    // ── Lifecycle: Close ─────────────────────────────────────────────

    /// Close the project and free all resources.
    /// Idempotent — closing an already-closed project is a no-op.
    fn close(&self) -> PyResult<()> {
        // Stop the watcher first so its threads don't outlive the project.
        let mut w = self.watcher.lock().map_err(|e| {
            PyRuntimeError::new_err(format!("watcher lock poisoned: {e}"))
        })?;
        if let Some(pw) = w.take() {
            pw.stop();
        }
        drop(w);

        let mut guard = self.inner.lock().map_err(|_e| {
            PyRuntimeError::new_err(format!("Lock poisoned"))
        })?;

        // Setting None on an already-None guard is harmless.
        *guard = None;
        Ok(())
    }

    // ── Write path ─────────────────────────────────────────────────
    //
    // All writes hold the GIL and mutate the HeadState in-place.
    // Each returns a SyncResult dict (via pythonize) describing the delta.
    //
    // ARCHITECTURE: HEAD is mutated in place; snapshots are independent
    // frozen databases that never block or are blocked by writes.  Every
    // write advances the application revision, records content in the
    // generation, and publishes the new generation so a reader observing
    // revision R sees consistent content (§3.3.3).

    /// Overlay `path` with in-memory `text` (no disk write). Returns a
    /// SyncResult dict with the new revision and the affected paths.
    fn edit<'py>(&self, py: Python<'py>, path: &str, text: &str) -> PyResult<Bound<'py, PyAny>> {
        let dto = {
            let mut guard = lock_state(&self.inner, "edit")?;
            let head = guard.as_mut().unwrap();
            let abs = resolve_sync_path(&head.root, path);
            // Classify against the pre-edit content BEFORE the staged mutation.
            let event = classify_overlay_edit(&head.system, &head.db, &abs);
            let path_str = abs.as_str().to_string();
            let (created, changed) = match &event {
                ChangeEvent::Created { .. } => (vec![path_str], vec![]),
                _ => (vec![], vec![path_str]),
            };
            let changes = vec![crate::content::Change::Insert {
                path: abs.clone(),
                text: Arc::from(text),
            }];
            let mutation = Mutation::Overlay {
                changes,
                events: vec![event],
                created,
                changed,
                unsaved: vec![abs],
            };
            commit(head, mutation)?
        };
        commit_dto_to_py(py, dto)
    }

    /// Overlay many files atomically (one publish, one `apply_changes`, one
    /// published revision).
    fn edit_many<'py>(
        &self,
        py: Python<'py>,
        edits: std::collections::HashMap<String, String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let dto = {
            let mut guard = lock_state(&self.inner, "edit_many")?;
            let head = guard.as_mut().unwrap();

            // Build one Vec<Change> and one Vec<ChangeEvent> classified against
            // the pre-edit content — one revision for the entire multi-edit.
            let mut changes = Vec::with_capacity(edits.len());
            let mut events = Vec::with_capacity(edits.len());
            let (mut created, mut changed) = (Vec::new(), Vec::new());
            let mut unsaved = Vec::with_capacity(edits.len());
            for (path, text) in edits {
                let abs = resolve_sync_path(&head.root, &path);
                let event = classify_overlay_edit(&head.system, &head.db, &abs);
                changes.push(crate::content::Change::Insert {
                    path: abs.clone(),
                    text: Arc::from(text),
                });
                match &event {
                    ChangeEvent::Created { .. } => created.push(abs.as_str().to_string()),
                    _ => changed.push(abs.as_str().to_string()),
                }
                unsaved.push(abs);
                events.push(event);
            }
            let mutation = Mutation::Overlay { changes, events, created, changed, unsaved };
            commit(head, mutation)?
        };
        commit_dto_to_py(py, dto)
    }

    /// Overlay a virtual/unsaved buffer (e.g. "untitled:1"). No disk involvement.
    fn edit_virtual<'py>(
        &self,
        py: Python<'py>,
        uri: &str,
        text: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let dto = {
            let mut guard = lock_state(&self.inner, "edit_virtual")?;
            let head = guard.as_mut().unwrap();

            let vpath = SystemVirtualPathBuf::from(uri.to_string());
            let is_new = head.db.files().try_virtual_file(&vpath).is_none();
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
            let changes = vec![crate::content::Change::InsertVirtual {
                path: vpath,
                text: Arc::from(text),
            }];
            // Virtual buffers are never watched, so they do not join the
            // unsaved-overlay (buffer-wins) set.
            let mutation = Mutation::Overlay {
                changes,
                events: vec![event],
                created,
                changed,
                unsaved: vec![],
            };
            commit(head, mutation)?
        };
        commit_dto_to_py(py, dto)
    }

    /// Author (write) an authored value for `(layer, durable_id)`.
    ///
    /// A real revision-producing commit funnelled through `commit`: it validates
    /// the layer/value/id, stages the copy-on-write `AuthoredStore` and serialises
    /// the record, persists it crash-safely as a staged step, and publishes the
    /// revision LAST (§5.3 stage → persist → publish). A persistence failure rolls
    /// the whole commit back — head included — via the staged-commit machinery; no
    /// in-memory-only `prior` rollback remains.
    fn author<'py>(
        &self,
        py: Python<'py>,
        layer: &str,
        id: &str,
        value_json: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let dto = {
            let mut guard = lock_state(&self.inner, "author")?;
            let head = guard.as_mut().unwrap();
            // Parse value JSON early (before any staging); layer/id validation
            // happens in `build_plan`, still before any state moves.
            let value: serde_json::Value = serde_json::from_str(value_json).map_err(|e| {
                PyValueError::new_err(format!("invalid authored value JSON: {e}"))
            })?;
            let mutation = Mutation::Author {
                layer: layer.to_string(),
                id: id.to_string(),
                value,
            };
            commit(head, mutation)?
        };
        commit_dto_to_py(py, dto)
    }

    /// Ingest a disk change for `path`: drop any overlay for it and re-read disk.
    fn sync_path<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let dto = {
            let mut guard = lock_state(&self.inner, "sync_path")?;
            let head = guard.as_mut().unwrap();
            let abs = resolve_sync_path(&head.root, path);
            commit(head, Mutation::SyncPath { abs })?
        };
        commit_dto_to_py(py, dto)
    }

    /// Drop the overlay buffer for `path`, reverting to disk. Same semantics as
    /// `sync_path` but named for intent.
    fn discard<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let dto = {
            let mut guard = lock_state(&self.inner, "discard")?;
            let head = guard.as_mut().unwrap();
            let abs = resolve_sync_path(&head.root, path);
            commit(head, Mutation::SyncPath { abs })?
        };
        commit_dto_to_py(py, dto)
    }

    /// Re-ingest the whole project from disk (one rescan revision). New/changed
    /// disk files are discovered (Phase 5 carry-over of the Phase 1 `sync_all`
    /// gap); unsaved overlay buffers are preserved.
    fn sync_all<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let dto = {
            let mut guard = lock_state(&self.inner, "sync_all")?;
            let head = guard.as_mut().unwrap();
            commit(head, Mutation::SyncAll)?
        };
        commit_dto_to_py(py, dto)
    }

    /// TEST-ONLY (§0.4): arm a one-shot commit fault at `stage`. The very next
    /// commit fires a typed error at that staged boundary — after the stage's
    /// work, before the publish tail — and rolls the commit back to R−1. Inert
    /// when unarmed (a `None` cell is a pure no-op). Stage names match the
    /// rollback test's `_FAULT_STAGES`: "code_layer", "identity_persist",
    /// "authored_persist". This is a deliberate, documented test seam; it never
    /// fires in normal use.
    fn _fault_inject(&self, stage: &str) -> PyResult<()> {
        let mut guard = lock_state(&self.inner, "_fault_inject")?;
        guard.as_mut().unwrap().armed_fault = Some(stage.to_string());
        Ok(())
    }

    // ── File watching (Phase 8) ────────────────────────────────────────

    /// Start observing the filesystem. Registers ty's ProjectWatcher over the
    /// head db's watched paths (project root + module search paths + config).
    /// Observed changes are debounced by ty and queued; call `poll_changes()`
    /// to fold them into HEAD. Idempotent: calling watch() again replaces the
    /// watcher.
    fn watch(&self) -> PyResult<()> {
        // Build the handler first — it only needs the queue, not the head.
        let pending = Arc::clone(&self.pending);
        let handler = move |changes: Vec<ChangeEvent>| {
            if let Ok(mut q) = pending.lock() {
                q.extend(changes);
            }
            // A poisoned queue mutex means a prior drain panicked; dropping the
            // batch is acceptable (the next Rescan/sync_all re-syncs). Never
            // panic on the watcher thread.
        };

        let raw_watcher = directory_watcher(handler)
            .map_err(|e| PyRuntimeError::new_err(format!("failed to start file watcher: {e}")))?;

        // ProjectWatcher::new needs &db to derive the watched paths.
        let project_watcher = {
            let guard = lock_state(&self.inner, "watch")?;
            let head = guard.as_ref().unwrap();
            ProjectWatcher::new(raw_watcher, &head.db)
            // guard dropped here
        };

        let mut w = self
            .watcher
            .lock()
            .map_err(|e| PyRuntimeError::new_err(format!("watcher lock poisoned: {e}")))?;
        // Replace any existing watcher; the old one's threads stop on drop.
        *w = Some(project_watcher);
        Ok(())
    }

    /// Stop observing the filesystem. Pending unpolled events are discarded
    /// along with the watcher threads. No-op if not watching.
    fn unwatch(&self) -> PyResult<()> {
        let mut w = self
            .watcher
            .lock()
            .map_err(|e| PyRuntimeError::new_err(format!("watcher lock poisoned: {e}")))?;
        if let Some(pw) = w.take() {
            pw.stop();
        }
        Ok(())
    }

    /// Force the debouncer to emit any pending batch now (still asynchronous —
    /// the handler runs on the watcher thread). Tests call this before
    /// poll_changes to shorten the wait; production code rarely needs it.
    fn flush_watch(&self) -> PyResult<()> {
        let w = self
            .watcher
            .lock()
            .map_err(|e| PyRuntimeError::new_err(format!("watcher lock poisoned: {e}")))?;
        if let Some(pw) = w.as_ref() {
            pw.flush();
        }
        Ok(())
    }

    /// Drain all events the watcher has observed and fold them into HEAD as one
    /// revision. Returns a SyncResult dict, or None if nothing was pending (or
    /// everything was filtered out as overlaid). Single-writer: holds the head
    /// lock for the apply, exactly like edit()/sync_path().
    fn poll_changes<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyAny>>> {
        // 1. Drain the queue (separate lock; released immediately).
        let events: Vec<ChangeEvent> = {
            let mut q = self
                .pending
                .lock()
                .map_err(|e| PyRuntimeError::new_err(format!("watch queue poisoned: {e}")))?;
            std::mem::take(&mut *q)
        };

        if events.is_empty() {
            return Ok(None);
        }

        // 2. Apply under the head lock (the single-writer section) through the
        //    one commit funnel. An empty/all-filtered batch returns None — a
        //    no-op, never a published empty revision (§5.3).
        let dto = {
            let mut guard = lock_state(&self.inner, "poll_changes")?;
            let head = guard.as_mut().unwrap();
            commit(head, Mutation::Poll { events })?
            // guard dropped here
        };

        match dto {
            None => Ok(None),
            Some(dto) => {
                let obj = pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
                Ok(Some(obj))
            }
        }
    }

    /// Test/diagnostic seam: enqueue events as if the watcher had observed
    /// them. Lets the Python suite exercise poll_changes deterministically
    /// (no real FS timing). Paths are resolved like sync_path (relative →
    /// joined onto root).
    #[pyo3(signature = (changes))]
    fn _inject_changes(&self, changes: Vec<(String, String)>) -> PyResult<()> {
        let guard = lock_state(&self.inner, "_inject_changes")?;
        let root = guard.as_ref().unwrap().root.clone();
        drop(guard);

        let mut events = Vec::with_capacity(changes.len());
        for (kind, path) in changes {
            let abs = resolve_sync_path(&root, &path);
            let event = match kind.as_str() {
                "created" => ChangeEvent::Created { path: abs, kind: CreatedKind::File },
                "changed" => ChangeEvent::file_content_changed(abs),
                "deleted" => ChangeEvent::Deleted { path: abs, kind: DeletedKind::Any },
                "rescan"  => ChangeEvent::Rescan,
                other => return Err(PyValueError::new_err(format!("unknown change kind {other:?}"))),
            };
            events.push(event);
        }
        let mut q = self
            .pending
            .lock()
            .map_err(|e| PyRuntimeError::new_err(format!("watch queue poisoned: {e}")))?;
        q.extend(events);
        Ok(())
    }

    /// The current application revision.
    #[getter]
    fn head(&self) -> PyResult<u64> {
        let guard = lock_state(&self.inner, "head")?;
        Ok(guard.as_ref().unwrap().store.revision().0)
    }

    /// Phase 1 test seam: the number of project-content files read from
    /// disk by ingest helpers.  Increments once per file actually read;
    /// snapshot construction (after Phase 1.4) adds zero reads, proving
    /// O(1) capture structurally.
    fn project_content_disk_reads(&self) -> PyResult<u64> {
        let guard = lock_state(&self.inner, "project_content_disk_reads")?;
        Ok(guard.as_ref().unwrap().store.disk_read_count())
    }

    // ── Snapshot ─────────────────────────────────────────────────────

    /// Pin a revision-isolated MVCC snapshot. `at=None` pins the current head
    /// revision; `at=r` time-travels to a still-retained revision (else an error).
    ///
    /// The returned snapshot owns an INDEPENDENT `ProjectDatabase` (its own
    /// `Zalsa`), so holding it across HEAD edits neither blocks the writer nor
    /// risks cancellation (architecture §0). No eager materialisation: content
    /// is pinned by the captured `Generation` + read-once disk capture.
    #[pyo3(signature = (at=None))]
    fn snapshot(&self, at: Option<u64>) -> PyResult<PySnapshot> {
        let guard = lock_state(&self.inner, "snapshot")?;
        let head = guard.as_ref().unwrap();
        let root = head.root.clone();

        let is_head = at.is_none();
        let registry = head.registry.clone();
        let authored = Some(head.authored.clone());
        let config = head.config.clone();
        let hash_policies = head.hash_policies.clone();
        let default_hash_profile = head.default_hash_profile.clone();
        let (generation, rev) = match at {
            None => (head.store.capture(), head.store.revision()),
            Some(r) => {
                let rev = Revision(r);
                let gen = head.store.generation_at(rev).ok_or_else(|| {
                    RevisionEvictedError::new_err(format!(
                        "revision {} is no longer retained (oldest retained: {})",
                        r,
                        head.store.oldest_retained().0
                    ))
                })?;
                (gen, rev)
            }
        };
        drop(guard); // release the head lock BEFORE the (cold) db build

        let mut state = build_frozen(root, generation, rev, hash_policies, default_hash_profile);
        state.registry = Some(registry);
        state.authored = authored;
        Ok(PySnapshot {
            inner: Mutex::new(Some(state)),
            revision: rev.0,
            config,
            is_head,
        })
    }

    // ── Floating warm fast path (Phase 9) ─────────────────────────────

    /// A floating, warm view of the live HEAD (architecture §6 "floating latest
    /// reads"). Each read reflects the newest revision, reuses the writer's
    /// memos, and is internally retried on salsa cancellation. For pinned,
    /// isolated reads use `snapshot()` instead.
    fn head_view(&self) -> PyResult<PyHeadView> {
        // Validate the project is open, then share the head handle.
        drop(lock_state(&self.inner, "head_view")?);
        Ok(PyHeadView {
            inner: Arc::clone(&self.inner),
        })
    }

    // ── Identity: id_for, locate (Gate 2 Step 6) ──────────────────────

    // ── Native code delta (Phase 2, parity-only) ─────────────────

    /// Produce a **full** (cold-start) native code delta for the current head
    /// state — every node upserted, every edge added, `rescan = true` — against
    /// an empty previous layer. The parity oracle applies this to a fresh
    /// `CodeGraph` and compares it to the legacy read-surface build.
    ///
    /// This is a pure read of the head state (it does not mutate the head or its
    /// stored code layer); the in-commit producer is what maintains the layer.
    fn full_code_delta<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let (state, revision) = {
            let guard = lock_state(&self.inner, "full_code_delta")?;
            let head = guard.as_ref().unwrap();
            (head.read_clone(), head.store.revision().0)
        };
        let empty = crate::code_layer::CodeLayer::new();
        let delta = py.detach(move || {
            let (_next, delta) =
                crate::code_layer::produce_code_delta(&state, &empty, revision, true, None);
            delta
        });
        pythonize(py, &delta).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    /// Resolve the DurableId of the entity at `(path, line, col)`.
    ///
    /// Finds the enclosing symbol at the position, builds its qualified_path,
    /// and looks up the DurableId in the identity registry. Returns None if
    /// no entity was found at the position or no identity is registered.
    fn id_for(&self, path: &str, line: u32, col: u32) -> PyResult<Option<String>> {
        let guard = lock_state(&self.inner, "id_for")?;
        let head = guard.as_ref().unwrap();

        let state = TyProjectState {
            db: head.db.clone(),
            root: head.root.clone(),
            registry: Some(head.registry.clone()),
            hash_policy: head.hash_policy,
            hash_policies: head.hash_policies.clone(),
            default_hash_profile: head.default_hash_profile.clone(),
            authored: None,
        };

        // Resolve the file.
        let file = crate::files::resolve_file(
            &state.db,
            state.root.as_std_path(),
            path,
        ).map_err(|e| PathResolutionError::new_err(e.to_string()))?;

        let file_path = file.path(&state.db).as_str().to_string();

        // Get document symbols.
        let flat_symbols = ty_ide::document_symbols(&state.db, file);
        let hierarchical = flat_symbols.to_hierarchical();

        let src = ruff_db::source::source_text(&state.db, file);
        let source_str = src.as_str();
        let line_index = ruff_source_file::LineIndex::from_source_text(source_str);

        // Convert position to offset.
        let offset = crate::coordinates::position_to_offset_with_index(
            source_str, &line_index, line, col,
        ).map_err(|e| PositionError::new_err(e.to_string()))?;

        // Find all symbols that contain this offset, pick the innermost.
        let mut best: Option<(String, u32)> = None; // (qualified_path, range_size)

        // Walk all symbols via iter() and figure out containment + nesting.
        for (id, info) in hierarchical.iter() {
            if info.full_range.contains(offset) {
                // Build qualified_path by walking up the hierarchy.
                let qp = build_qualified_path(&hierarchical, id, &file_path);
                let size = info.full_range.len().to_u32();
                match &best {
                    None => best = Some((qp, size)),
                    Some((_, prev_size)) if size < *prev_size => {
                        best = Some((qp, size));
                    }
                    _ => {}
                }
            }
        }

        // Look up in the registry.
        match best {
            Some((qp, _)) => Ok(head.registry.by_path(&qp).map(|id| id.0.clone())),
            None => Ok(None),
        }
    }

    /// Locate the current file+qualified_path for a DurableId.
    ///
    /// Returns None if the id is not in the registry (e.g. retired, or
    /// from another session).
    fn locate(&self, durable_id: &str) -> PyResult<Option<String>> {
        let guard = lock_state(&self.inner, "locate")?;
        let head = guard.as_ref().unwrap();
        let id = DurableId(durable_id.to_string());
        Ok(head.registry.get(&id).map(|a| a.qualified_path.clone()))
    }

    /// List DurableIds currently flagged as NeedsReview.
    fn needs_review(&self) -> PyResult<Vec<String>> {
        let guard = lock_state(&self.inner, "needs_review")?;
        let head = guard.as_ref().unwrap();
        Ok(head.registry.iter()
            .filter(|a| a.status == crate::identity::IdentityStatus::NeedsReview)
            .map(|a| a.id.0.clone())
            .collect())
    }

    /// List DurableIds currently flagged as Orphaned.
    fn orphaned(&self) -> PyResult<Vec<String>> {
        let guard = lock_state(&self.inner, "orphaned")?;
        let head = guard.as_ref().unwrap();
        Ok(head.registry.iter()
            .filter(|a| a.status == crate::identity::IdentityStatus::Orphaned)
            .map(|a| a.id.0.clone())
            .collect())
    }
}

// ── Snapshot: immutable, revision-pinned, thread-shareable read view ──────────
//
// A `PySnapshot` owns its own clone of the project database, pinned to the
// revision at `snapshot()` time. Each read clones that frozen db under a brief
// lock and runs the analysis with the GIL released, so many threads can share
// one snapshot and run reads in parallel.

#[pyclass(name = "TySnapshot", module = "tyo3._native_impl", frozen)]
pub struct PySnapshot {
    inner: Mutex<Option<TyProjectState>>,
    /// The application revision this snapshot is pinned to (immutable).
    revision: u64,
    /// Validated config needed for derived status computation.
    config: ValidatedConfig,
    /// True if this snapshot was taken at the current HEAD (not time-travel).
    /// When true, authored reads may use the current (latest) value as a
    /// fallback after value_at — the session revision counter may not align
    /// with stored revisions across close/reopen cycles.
    is_head: bool,
}

#[pymethods]
impl PySnapshot {
    /// The revision this snapshot is pinned to.
    #[getter]
    fn revision(&self) -> u64 {
        self.revision
    }

    // ── Files ────────────────────────────────────────────────────

    /// List all source files in the project.
    fn files(&self, py: Python<'_>) -> PyResult<Vec<String>> {
        let state = clone_locked_state(&self.inner, "files")?;
        Ok(py.detach(move || compute_files(&state)))
    }

    // ── Native code delta (Phase 4.4) ────────────────────────────

    /// Produce a **full** (`rescan = true`, scope = all) native code delta for
    /// this snapshot's pinned revision, computed over the snapshot's **own
    /// frozen database** and pinned identity registry — the snapshot analogue of
    /// `PyTyProject::full_code_delta`.
    ///
    /// `Snapshot.graph()` applies this to a fresh `CodeGraph`, so the pinned
    /// graph is a pure projection of *this* revision's content (§5.2 / §5.9). It
    /// reads only the frozen generation (no disk, no live head) and mutates no
    /// session state. It reuses the exact `produce_code_delta` path the head
    /// accessor uses, so a snapshot graph and a head graph at the same revision
    /// agree structurally (parity).
    fn full_code_delta<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "full_code_delta")?;
        let revision = self.revision;
        let empty = crate::code_layer::CodeLayer::new();
        let delta = py.detach(move || {
            let (_next, delta) =
                crate::code_layer::produce_code_delta(&state, &empty, revision, true, None);
            delta
        });
        pythonize(py, &delta).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Check ────────────────────────────────────────────────────

    /// Run the type checker on the snapshot's pinned revision (GIL released).
    fn check<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "check")?;
        let check_result = py.detach(move || compute_check(&state));
        pythonize(py, &check_result)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    /// Run the type checker and return diagnostics for a single file.
    fn check_file<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "check_file")?;
        let path = path.to_owned();
        let dto = py.detach(move || compute_check_file(&state, &path))
            .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dto)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Document Symbols ─────────────────────────────────────────

    /// Get document symbols for a file in the project.
    fn document_symbols<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "document_symbols")?;
        let path = path.to_owned();
        let dtos = py.detach(move || compute_document_symbols(&state, &path))
            .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Workspace Symbols ────────────────────────────────────────

    /// Search for symbols matching a query across all workspace files.
    fn workspace_symbols<'py>(&self, py: Python<'py>, query: &str) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "workspace_symbols")?;
        let query = query.to_owned();
        let dtos = py.detach(move || compute_workspace_symbols(&state, &query));
        pythonize(py, &dtos)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Goto Definition ──────────────────────────────────────────

    /// Navigate to the definition of the symbol at the given position.
    fn goto_definition<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        line: u32,
        column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "goto_definition")?;
        let path = path.to_owned();
        let dtos = py.detach(move || {
            compute_navigate(&state, &path, line, column, ty_ide::goto_definition)
        })
        .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Goto Declaration ─────────────────────────────────────────

    /// Navigate to the declaration of the symbol at the given position.
    fn goto_declaration<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        line: u32,
        column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "goto_declaration")?;
        let path = path.to_owned();
        let dtos = py.detach(move || {
            compute_navigate(&state, &path, line, column, ty_ide::goto_declaration)
        })
        .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Goto Type Definition ─────────────────────────────────────

    /// Navigate to the type definition of the symbol at the given position.
    fn goto_type_definition<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        line: u32,
        column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "goto_type_definition")?;
        let path = path.to_owned();
        let dtos = py.detach(move || {
            compute_navigate(&state, &path, line, column, ty_ide::goto_type_definition)
        })
        .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Find References ──────────────────────────────────────────

    /// Find all references to the symbol at the given position.
    fn find_references<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        line: u32,
        column: u32,
        include_declaration: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "find_references")?;
        let path = path.to_owned();
        let dtos = py.detach(move || {
            compute_find_references(&state, &path, line, column, include_declaration)
        })
        .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Semantic Tokens ─────────────────────────────────────

    /// Return semantic tokens for a file, optionally scoped to a range.
    #[pyo3(signature = (path, *, start_line = None, start_col = None, end_line = None, end_col = None))]
    fn semantic_tokens<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        start_line: Option<u32>,
        start_col: Option<u32>,
        end_line: Option<u32>,
        end_col: Option<u32>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "semantic_tokens")?;
        let path = path.to_owned();
        let dtos = py.detach(move || {
            compute_semantic_tokens(&state, &path, start_line, start_col, end_line, end_col)
        })
        .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Inlay Hints ──────────────────────────────────────────

    /// Return inlay hints for a file.
    fn inlay_hints<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "inlay_hints")?;
        let path = path.to_owned();
        let dtos = py.detach(move || compute_inlay_hints(&state, &path))
            .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Hints ────────────────────────────────────────────────

    /// Return hints (unused bindings, unreachable code) for a file.
    fn hints<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "hints")?;
        let path = path.to_owned();
        let dtos = py.detach(move || compute_hints(&state, &path))
            .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Code Actions ─────────────────────────────────────────

    /// Get quick fixes for a diagnostic at a range.
    fn code_actions<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        start_line: u32,
        start_col: u32,
        end_line: u32,
        end_col: u32,
        diagnostic_id: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "code_actions")?;
        let path = path.to_owned();
        let diagnostic_id = diagnostic_id.to_owned();
        let dtos = py.detach(move || {
            compute_code_actions(&state, &path, start_line, start_col, end_line, end_col, &diagnostic_id)
        })
        .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Selection Ranges ────────────────────────────────────

    /// Compute selection ranges at the given position.
    fn selection_ranges<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        line: u32,
        column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "selection_ranges")?;
        let path = path.to_owned();
        let dtos = py.detach(move || compute_selection_ranges(&state, &path, line, column))
            .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Folding Ranges ───────────────────────────────────────

    /// Return folding ranges for a file.
    fn folding_ranges<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "folding_ranges")?;
        let path = path.to_owned();
        let dtos = py.detach(move || compute_folding_ranges(&state, &path))
            .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Signature Help ───────────────────────────────────────

    /// Get signature help at the given position.
    fn signature_help<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        line: u32,
        column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "signature_help")?;
        let path = path.to_owned();
        match py.detach(move || compute_signature_help(&state, &path, line, column))
            .map_err(AnalysisError::into_pyerr)?
        {
            None => Ok(py.None().bind(py).clone()),
            Some(dto) => pythonize(py, &dto)
                .map_err(|e| PyRuntimeError::new_err(e.to_string())),
        }
    }

    // ── Completion ───────────────────────────────────────────

    /// Get completion suggestions at the given position.
    #[pyo3(signature = (path, line, column, *, auto_import = true))]
    fn completions<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        line: u32,
        column: u32,
        auto_import: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "completions")?;
        let path = path.to_owned();
        let dtos = py.detach(move || compute_completions(&state, &path, line, column, auto_import))
            .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── File Occurrences ─────────────────────────────────────

    /// Batch-resolve all name occurrences in a file.
    fn file_occurrences<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "file_occurrences")?;
        let path = path.to_owned();
        let dtos = py.detach(move || compute_file_occurrences(&state, &path))
            .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Type Hierarchy ──────────────────────────────────────

    /// Query type hierarchy at a position.
    fn type_hierarchy<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        line: u32,
        column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "type_hierarchy")?;
        let path = path.to_owned();
        match py.detach(move || compute_type_hierarchy(&state, &path, line, column))
            .map_err(AnalysisError::into_pyerr)?
        {
            None => Ok(py.None().bind(py).clone()),
            Some(dto) => pythonize(py, &dto)
                .map_err(|e| PyRuntimeError::new_err(e.to_string())),
        }
    }

    /// Direct supertypes (base classes) of the class at a position.
    ///
    /// Lean alternative to `type_hierarchy` for callers (notably CodeGraph
    /// build) that only need base classes for INHERITS edges. Skips the
    /// expensive project-wide subtype scan. Returns an empty list when the
    /// position is not on a class.
    fn class_supertypes<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        line: u32,
        column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "class_supertypes")?;
        let path = path.to_owned();
        let items = py.detach(move || compute_supertypes(&state, &path, line, column))
            .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &items)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Document Highlights ─────────────────────────────────

    /// Highlight all in-file occurrences of the symbol at the given position.
    fn document_highlights<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        line: u32,
        column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "document_highlights")?;
        let path = path.to_owned();
        let refs = py.detach(move || compute_document_highlights(&state, &path, line, column))
            .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &refs)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Rename ───────────────────────────────────────────────

    /// Check if the symbol at the given position can be renamed.
    fn can_rename<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        line: u32,
        column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "can_rename")?;
        let path = path.to_owned();
        match py.detach(move || compute_can_rename(&state, &path, line, column))
            .map_err(AnalysisError::into_pyerr)?
        {
            None => Ok(py.None().bind(py).clone()),
            Some(range) => pythonize(py, &range)
                .map_err(|e| PyRuntimeError::new_err(e.to_string())),
        }
    }

    /// Rename the symbol at the given position.
    fn rename<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        line: u32,
        column: u32,
        new_name: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "rename")?;
        let path = path.to_owned();
        let new_name = new_name.to_owned();
        match py.detach(move || compute_rename(&state, &path, line, column, &new_name))
            .map_err(AnalysisError::into_pyerr)?
        {
            None => Ok(py.None().bind(py).clone()),
            Some(dto) => pythonize(py, &dto)
                .map_err(|e| PyRuntimeError::new_err(e.to_string())),
        }
    }

    // ── Hover ────────────────────────────────────────────────

    /// Get hover information for the symbol at the given position.
    fn hover<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        line: u32,
        column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "hover")?;
        let path = path.to_owned();
        match py.detach(move || compute_hover(&state, &path, line, column))
            .map_err(AnalysisError::into_pyerr)?
        {
            None => Ok(py.None().bind(py).clone()),
            Some(dto) => pythonize(py, &dto)
                .map_err(|e| PyRuntimeError::new_err(e.to_string())),
        }
    }

    // ── Authored reads ──────────────────────────────────────

    /// Resolve an authored value for `(layer, durable_id)` at this snapshot's
    /// pinned revision.
    ///
    /// Status is derived from the snapshot-captured identity registry:
    /// - `present` if the entity is Active, or the layer has `review_on_change = false`.
    /// - `needs_review` if the entity's anchor status is `NeedsReview`.
    /// - `orphaned` if the entity's anchor status is `Orphaned`.
    /// - `absent` if no authored record for this (layer, id) with `revision <= R`.
    fn authored<'py>(&self, py: Python<'py>, layer: &str, id: &str) -> PyResult<Bound<'py, PyAny>> {
        let guard = self.inner.lock().map_err(|e| {
            PyRuntimeError::new_err(format!("Lock poisoned: {e}"))
        })?;
        let state = guard.as_ref().ok_or_else(|| {
            PyRuntimeError::new_err("Snapshot is closed")
        })?;

        let authored_store = state.authored.as_ref();
        let registry = state.registry.as_ref();

        let (value, rev, status_str) = match authored_store {
            Some(store) => {
                // For time-travel reads, use value_at strictly.
                // For HEAD reads, fall back to value() because the session
                // revision counter may not align with stored revisions
                // (e.g., after close/reopen).
                let version_opt = if self.is_head {
                    store
                        .value_at(layer, id, self.revision)
                        .or_else(|| store.value(layer, id))
                } else {
                    store.value_at(layer, id, self.revision)
                };
                match version_opt {
                    Some(version) => {
                        let rev = version.revision;
                        let status_str = derive_authored_status(
                            &self.config,
                            registry,
                            layer,
                            id,
                        );
                        (Some(version.value.clone()), rev, status_str)
                    }
                    None => (None, self.revision, "absent".to_string()),
                }
            }
            None => (None, self.revision, "absent".to_string()),
        };

        let dto = dto::AuthoredValueDto {
            layer: layer.to_string(),
            durable_id: id.to_string(),
            value,
            status: status_str,
            revision: rev,
        };
        drop(guard);
        pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    /// Return the full version history for an authored record.
    ///
    /// Returns all versions (history + current) with `revision <= self.revision`,
    /// ordered by revision ascending.  Empty if the record doesn't exist.
    fn authored_history<'py>(
        &self,
        py: Python<'py>,
        layer: &str,
        id: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let guard = self.inner.lock().map_err(|e| {
            PyRuntimeError::new_err(format!("Lock poisoned: {e}"))
        })?;
        let state = guard.as_ref().ok_or_else(|| {
            PyRuntimeError::new_err("Snapshot is closed")
        })?;

        let pinned_rev = self.revision;
        let versions: Vec<dto::AuthoredVersionDto> = match state.authored.as_ref() {
            Some(store) => match store.records_get(layer, id) {
                Some(rec) => {
                    let mut out: Vec<dto::AuthoredVersionDto> = rec
                        .history
                        .iter()
                        .filter(|v| self.is_head || v.revision <= pinned_rev)
                        .map(|v| dto::AuthoredVersionDto {
                            value: v.value.clone(),
                            revision: v.revision,
                        })
                        .collect();
                    if self.is_head || rec.current.revision <= pinned_rev {
                        out.push(dto::AuthoredVersionDto {
                            value: rec.current.value.clone(),
                            revision: rec.current.revision,
                        });
                    }
                    out
                }
                None => Vec::new(),
            },
            None => Vec::new(),
        };
        drop(guard);
        pythonize(py, &versions).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Close ────────────────────────────────────────────────

    /// Release the pinned revision early, freeing its database. Idempotent.
    fn close(&self) -> PyResult<()> {
        let mut guard = self.inner.lock().map_err(|e| {
            PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
        })?;
        *guard = None;
        Ok(())
    }
}

// ── Phase 9: Floating warm fast path ─────────────────────────────────────

/// Max attempts before giving up. Each cancellation corresponds to a completed
/// write, so in practice 0–1 retries; the bound only guards a pathological,
/// never-quiescent writer.
const MAX_HEAD_RETRIES: u32 = 200;

/// Run a read `f` against a fresh clone of the LIVE HEAD db, catching salsa
/// cancellation and retrying on a new clone. The clone shares HEAD's Zalsa, so
/// the read is warm but cancellable by a concurrent apply_changes (architecture §0/§7).
///
/// `f` is a pure, GIL-free analysis closure (a `compute_*` call). Runs inside
/// py.detach so a concurrent write (which holds the GIL) can proceed; on
/// cancellation the clone is dropped (unblocking the writer) and the read retries
/// on a now-warmer clone. Returns the analysis DTO, or an error if retries are
/// exhausted / the project is closed.
fn read_head_with_retry<T: Send>(
    py: Python<'_>,
    inner: &Arc<Mutex<Option<HeadState>>>,
    op: &str,
    f: impl Fn(&TyProjectState) -> T + Send,
) -> PyResult<T> {
    py.detach(move || {
        let mut attempts: u32 = 0;
        loop {
            // Fresh HEAD clone each attempt (shares HEAD Zalsa). No GIL needed.
            let state = clone_locked_state(inner, op)?;
            match salsa::Cancelled::catch(std::panic::AssertUnwindSafe(|| f(&state))) {
                Ok(value) => return Ok(value),
                Err(_cancelled) => {
                    drop(state); // release the clone so the writer's cancel_others can proceed
                    attempts += 1;
                    if attempts >= MAX_HEAD_RETRIES {
                        return Err(PyRuntimeError::new_err(format!(
                            "{op}() on session.latest was cancelled by concurrent writes \
                             {MAX_HEAD_RETRIES} times; HEAD never quiesced. Use \
                             session.snapshot() for a pinned read."
                        )));
                    }
                    // loop: re-clone (warmer) and retry
                }
            }
        }
    })
}

/// A floating, warm view of the LIVE HEAD. Unlike PySnapshot (pinned, independent,
/// never cancelled), each read here clones the live HEAD db (sharing its Zalsa)
/// and retries on salsa cancellation. Reflects the newest HEAD revision; warm
/// because it reuses the writer's memos. The ONLY place a read can be cancelled
/// (and the only place a read can briefly delay a write) — architecture §7.
#[pyclass(name = "TyHeadView", module = "tyo3._native_impl", frozen)]
pub struct PyHeadView {
    inner: Arc<Mutex<Option<HeadState>>>,
}

#[pymethods]
impl PyHeadView {
    // ── Files ────────────────────────────────────────────────────

    fn files(&self, py: Python<'_>) -> PyResult<Vec<String>> {
        read_head_with_retry(py, &self.inner, "files", |s| compute_files(s))
    }

    // ── Check ────────────────────────────────────────────────────

    fn check<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let dto = read_head_with_retry(py, &self.inner, "check", |s| compute_check(s))?;
        pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    fn check_file<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let result = read_head_with_retry(py, &self.inner, "check_file", move |s| {
            compute_check_file(s, &path)
        })?;
        let dto = result.map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Document Symbols ─────────────────────────────────────────

    fn document_symbols<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let result = read_head_with_retry(py, &self.inner, "document_symbols", move |s| {
            compute_document_symbols(s, &path)
        })?;
        let dtos = result.map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Workspace Symbols ────────────────────────────────────────

    fn workspace_symbols<'py>(&self, py: Python<'py>, query: &str) -> PyResult<Bound<'py, PyAny>> {
        let query = query.to_owned();
        let dtos = read_head_with_retry(py, &self.inner, "workspace_symbols", move |s| {
            compute_workspace_symbols(s, &query)
        })?;
        pythonize(py, &dtos).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Goto Definition ──────────────────────────────────────────

    fn goto_definition<'py>(
        &self, py: Python<'py>, path: &str, line: u32, column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let dtos = read_head_with_retry(py, &self.inner, "goto_definition", move |s| {
            compute_navigate(s, &path, line, column, ty_ide::goto_definition)
        })?.map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Goto Declaration ─────────────────────────────────────────

    fn goto_declaration<'py>(
        &self, py: Python<'py>, path: &str, line: u32, column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let dtos = read_head_with_retry(py, &self.inner, "goto_declaration", move |s| {
            compute_navigate(s, &path, line, column, ty_ide::goto_declaration)
        })?.map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Goto Type Definition ─────────────────────────────────────

    fn goto_type_definition<'py>(
        &self, py: Python<'py>, path: &str, line: u32, column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let dtos = read_head_with_retry(py, &self.inner, "goto_type_definition", move |s| {
            compute_navigate(s, &path, line, column, ty_ide::goto_type_definition)
        })?.map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Find References ──────────────────────────────────────────

    fn find_references<'py>(
        &self, py: Python<'py>, path: &str, line: u32, column: u32,
        include_declaration: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let dtos = read_head_with_retry(py, &self.inner, "find_references", move |s| {
            compute_find_references(s, &path, line, column, include_declaration)
        })?.map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Semantic Tokens ─────────────────────────────────────────

    #[pyo3(signature = (path, *, start_line = None, start_col = None, end_line = None, end_col = None))]
    fn semantic_tokens<'py>(
        &self, py: Python<'py>, path: &str,
        start_line: Option<u32>, start_col: Option<u32>,
        end_line: Option<u32>, end_col: Option<u32>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let dtos = read_head_with_retry(py, &self.inner, "semantic_tokens", move |s| {
            compute_semantic_tokens(s, &path, start_line, start_col, end_line, end_col)
        })?.map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Inlay Hints ──────────────────────────────────────────────

    fn inlay_hints<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let dtos = read_head_with_retry(py, &self.inner, "inlay_hints", move |s| {
            compute_inlay_hints(s, &path)
        })?.map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Hints ────────────────────────────────────────────────────

    fn hints<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let dtos = read_head_with_retry(py, &self.inner, "hints", move |s| {
            compute_hints(s, &path)
        })?.map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Code Actions ─────────────────────────────────────────────

    fn code_actions<'py>(
        &self, py: Python<'py>, path: &str,
        start_line: u32, start_col: u32, end_line: u32, end_col: u32,
        diagnostic_id: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let diagnostic_id = diagnostic_id.to_owned();
        let dtos = read_head_with_retry(py, &self.inner, "code_actions", move |s| {
            compute_code_actions(s, &path, start_line, start_col, end_line, end_col, &diagnostic_id)
        })?.map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Selection Ranges ────────────────────────────────────────

    fn selection_ranges<'py>(
        &self, py: Python<'py>, path: &str, line: u32, column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let dtos = read_head_with_retry(py, &self.inner, "selection_ranges", move |s| {
            compute_selection_ranges(s, &path, line, column)
        })?.map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Folding Ranges ───────────────────────────────────────────

    fn folding_ranges<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let dtos = read_head_with_retry(py, &self.inner, "folding_ranges", move |s| {
            compute_folding_ranges(s, &path)
        })?.map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Signature Help ───────────────────────────────────────────

    fn signature_help<'py>(
        &self, py: Python<'py>, path: &str, line: u32, column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let result = read_head_with_retry(py, &self.inner, "signature_help", move |s| {
            compute_signature_help(s, &path, line, column)
        })?;
        match result.map_err(AnalysisError::into_pyerr)? {
            None => Ok(py.None().bind(py).clone()),
            Some(dto) => pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string())),
        }
    }

    // ── Completion ───────────────────────────────────────────────

    #[pyo3(signature = (path, line, column, *, auto_import = true))]
    fn completions<'py>(
        &self, py: Python<'py>, path: &str, line: u32, column: u32, auto_import: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let dtos = read_head_with_retry(py, &self.inner, "completions", move |s| {
            compute_completions(s, &path, line, column, auto_import)
        })?.map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── File Occurrences ─────────────────────────────────────────

    fn file_occurrences<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let dtos = read_head_with_retry(py, &self.inner, "file_occurrences", move |s| {
            compute_file_occurrences(s, &path)
        })?.map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Type Hierarchy ──────────────────────────────────────────

    fn type_hierarchy<'py>(
        &self, py: Python<'py>, path: &str, line: u32, column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let result = read_head_with_retry(py, &self.inner, "type_hierarchy", move |s| {
            compute_type_hierarchy(s, &path, line, column)
        })?;
        match result.map_err(AnalysisError::into_pyerr)? {
            None => Ok(py.None().bind(py).clone()),
            Some(dto) => pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string())),
        }
    }

    fn class_supertypes<'py>(
        &self, py: Python<'py>, path: &str, line: u32, column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let items = read_head_with_retry(py, &self.inner, "class_supertypes", move |s| {
            compute_supertypes(s, &path, line, column)
        })?.map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &items).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Document Highlights ─────────────────────────────────────

    fn document_highlights<'py>(
        &self, py: Python<'py>, path: &str, line: u32, column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let refs = read_head_with_retry(py, &self.inner, "document_highlights", move |s| {
            compute_document_highlights(s, &path, line, column)
        })?.map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &refs).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Rename ───────────────────────────────────────────────────

    fn can_rename<'py>(
        &self, py: Python<'py>, path: &str, line: u32, column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let result = read_head_with_retry(py, &self.inner, "can_rename", move |s| {
            compute_can_rename(s, &path, line, column)
        })?;
        match result.map_err(AnalysisError::into_pyerr)? {
            None => Ok(py.None().bind(py).clone()),
            Some(range) => pythonize(py, &range).map_err(|e| PyRuntimeError::new_err(e.to_string())),
        }
    }

    fn rename<'py>(
        &self, py: Python<'py>, path: &str, line: u32, column: u32, new_name: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let new_name = new_name.to_owned();
        let result = read_head_with_retry(py, &self.inner, "rename", move |s| {
            compute_rename(s, &path, line, column, &new_name)
        })?;
        match result.map_err(AnalysisError::into_pyerr)? {
            None => Ok(py.None().bind(py).clone()),
            Some(dto) => pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string())),
        }
    }

    // ── Hover ────────────────────────────────────────────────────

    fn hover<'py>(
        &self, py: Python<'py>, path: &str, line: u32, column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let path = path.to_owned();
        let result = read_head_with_retry(py, &self.inner, "hover", move |s| {
            compute_hover(s, &path, line, column)
        })?;
        match result.map_err(AnalysisError::into_pyerr)? {
            None => Ok(py.None().bind(py).clone()),
            Some(dto) => pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string())),
        }
    }
}

// ── Phase 2 tests ────────────────────────────────────────────────────────

/// Build a qualified_path for a SymbolId by walking up the hierarchy.
fn build_qualified_path(
    hierarchical: &ty_ide::HierarchicalSymbols,
    target_id: ty_ide::SymbolId,
    file_path: &str,
) -> String {
    // Walk the hierarchy upward: collect name segments.
    let mut segments: Vec<String> = Vec::new();

    // Get the symbol info for the target.
    for (id, info) in hierarchical.iter() {
        if id == target_id {
            segments.push(info.name.clone().into_owned());
            let mut current_id = id;

            // Walk up: for each symbol, check if any id has it as a child.
            // This is O(n²) but fine for a single query.
            loop {
                let mut found_parent = false;
                for (pid, pinfo) in hierarchical.iter() {
                    for (cid, _) in hierarchical.children(pid) {
                        if cid == current_id {
                            segments.push(pinfo.name.clone().into_owned());
                            current_id = pid;
                            found_parent = true;
                            break;
                        }
                    }
                    if found_parent {
                        break;
                    }
                }
                if !found_parent {
                    break;
                }
            }
            break;
        }
    }

    segments.reverse();
    let qualified = segments.join("::");
    format!("{}::{}", file_path, qualified)
}

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
        let gen = store.capture();
        gen
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
        let gen_r0 = pre_populate(&mut head.store, a.clone(), "X = 1\n");
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
