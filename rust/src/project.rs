use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pythonize::pythonize;

use crate::{ProjectClosedError, PathResolutionError, PositionError};

use ruff_db::files::File;
use ruff_db::source::source_text;
use ruff_db::system::{SystemPath, SystemPathBuf, SystemVirtualPathBuf};
use ruff_db::Db as _; // bring files() etc. into scope
use ruff_source_file::LineIndex;

use ty_project::watch::{ChangeEvent, ChangedKind, CreatedKind, DeletedKind, ExistingPathKind, ProjectWatcher, directory_watcher};
use ty_project::Db;
use ty_project::{ProjectDatabase, ProjectMetadata};

use crate::content::{ContentStore, Generation, Revision};
use crate::overlay::OverlaySystem;

use ruff_python_ast::name::Name;

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
enum AnalysisError {
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
struct TyProjectState {
    db: ProjectDatabase,
    root: SystemPathBuf,
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
        }
    }
}

impl ReadCloneSource for HeadState {
    fn read_clone(&self) -> TyProjectState {
        // Clone *only* db + root. store/system stay in the head; the read clone
        // (and any snapshot built from it) never sees them.
        TyProjectState {
            db: self.db.clone(),
            root: self.root.clone(),
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
fn compute_files(state: &TyProjectState) -> Vec<String> {
    let project = state.db.project();
    let indexed = project.files(&state.db);
    indexed
        .iter()
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
fn compute_document_symbols(
    state: &TyProjectState,
    path: &str,
) -> Result<Vec<dto::SymbolDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;

    let flat_symbols = ty_ide::document_symbols(&state.db, file);
    let hierarchical = flat_symbols.to_hierarchical();

    let file_path = file.path(&state.db).as_str().to_string();
    let line_index = ruff_source_file::LineIndex::from_source_text(&source_str);

    let mut symbols: Vec<dto::SymbolDto> = Vec::new();
    for (id, info) in hierarchical.iter() {
        convert::symbols::collect_symbols_recursive(
            &hierarchical,
            id,
            &info,
            &source_str,
            &line_index,
            &file_path,
            None,
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
fn compute_navigate(
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
            let end = coordinates::position_to_offset_with_index(
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
fn compute_file_occurrences(
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
fn compute_supertypes(
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
fn build_head(root: SystemPathBuf, initial_store: ContentStore) -> HeadState {
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
            eprintln!("WARNING: {err}. Falling back to default project settings.");
            let metadata = ProjectMetadata::new(Name::new("tyo3-project"), root.clone());
            ProjectDatabase::use_defaults(metadata, system.clone())
        }
    };

    HeadState {
        db,
        root,
        store: initial_store,
        system,
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
/// Disk files not in `generation` are captured read-once by the frozen overlay
/// (overlay.rs §2), so the snapshot never races live disk.
fn build_frozen(root: SystemPathBuf, generation: Generation, rev: Revision) -> TyProjectState {
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
            eprintln!("WARNING: {err}. Falling back to default project settings for snapshot.");
            let metadata =
                ProjectMetadata::new(Name::new("tyo3-project"), root.clone());
            ProjectDatabase::use_defaults(metadata, system)
        }
    };

    TyProjectState { db, root }
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

/// Publish the captured store generation, apply `events` to the db, and build the
/// SyncResult. Caller has already mutated `head.store` and bucketed the paths.
///
/// PRECONDITION: `head.store` is already mutated; this captures+publishes it.
fn commit_head(
    head: &mut HeadState,
    events: &[ChangeEvent],
    created: Vec<String>,
    changed: Vec<String>,
    deleted: Vec<String>,
    rescan: bool,
) -> dto::SyncResultDto {
    // 2. publish BEFORE apply so apply_changes re-reads new content.
    head.system.publish(head.store.capture());
    // 3. ty does all incremental work.
    let result = head.db.apply_changes(events, None);
    // 4 + 5.
    dto::SyncResultDto {
        revision: head.store.revision().0,
        created,
        changed,
        deleted,
        project_changed: result.project_changed(),
        custom_stdlib_changed: result.custom_stdlib_changed(),
        rescan,
    }
}

/// Shared body of `sync_path` and `discard`: forget the overlay for `abs`, publish
/// so the overlay falls through to disk, classify, apply, and build the result.
fn sync_path_inner(head: &mut HeadState, abs: SystemPathBuf) -> dto::SyncResultDto {
    head.store.forget(&abs);
    head.system.publish(head.store.capture());
    let event = classify_disk_sync(&head.system, &head.db, &abs);

    let path_str = abs.as_str().to_string();
    let (created, changed, deleted) = match &event {
        ChangeEvent::Created { .. } => (vec![path_str], vec![], vec![]),
        ChangeEvent::Deleted { .. } => (vec![], vec![], vec![path_str]),
        _ => (vec![], vec![path_str], vec![]),
    };
    let result = head.db.apply_changes(std::slice::from_ref(&event), None);
    dto::SyncResultDto {
        revision: head.store.revision().0,
        created,
        changed,
        deleted,
        project_changed: result.project_changed(),
        custom_stdlib_changed: result.custom_stdlib_changed(),
        rescan: false,
    }
}

// ── Phase 8: Watcher drain-and-apply core ───────────────────────────────

/// Fold a batch of watcher-produced ChangeEvents into HEAD as ONE revision.
///
/// Returns `None` if, after filtering, there is nothing to apply (so the caller
/// returns None to Python without bumping the revision). Otherwise publishes,
/// applies, bumps the revision, and returns the SyncResult.
///
/// Rules:
///  * A `Rescan` anywhere in the batch ⇒ rescan wholesale (like sync_all).
///  * Events for a path with a live overlay buffer are DROPPED (the buffer wins).
///  * Virtual events are skipped (the watcher never emits them for real dirs).
///  * The remaining events are applied in one `apply_changes` call.
fn apply_watch_events(
    head: &mut HeadState,
    events: Vec<ChangeEvent>,
) -> Option<dto::SyncResultDto> {
    if events.is_empty() {
        return None;
    }

    // Rescan short-circuit: if ty lost sync, redo everything.
    if events.iter().any(|e| e.is_rescan()) {
        head.system.publish(head.store.capture());
        let result = head.db.apply_changes(&[ChangeEvent::Rescan], None);
        let revision = head.store.bump_revision().0;
        return Some(dto::SyncResultDto {
            revision,
            created: vec![],
            changed: vec![],
            deleted: vec![],
            project_changed: result.project_changed(),
            custom_stdlib_changed: result.custom_stdlib_changed(),
            rescan: true,
        });
    }

    // Filter: keep only real-path events whose path is NOT overlaid.
    let mut kept: Vec<ChangeEvent> = Vec::with_capacity(events.len());
    let (mut created, mut changed, mut deleted) = (Vec::new(), Vec::new(), Vec::new());
    for event in events {
        let Some(path) = event.system_path() else {
            // Virtual or path-less event — not from a directory watcher; skip.
            continue;
        };
        let path = path.to_path_buf();
        if head.store.has_overlay(&path) {
            // Unsaved buffer wins; ignore the disk event.
            continue;
        }
        let path_str = path.as_str().to_string();
        match &event {
            ChangeEvent::Created { .. } => created.push(path_str),
            ChangeEvent::Deleted { .. } => deleted.push(path_str),
            ChangeEvent::Changed { .. } => changed.push(path_str),
            _ => continue, // Opened / virtual variants: ignore
        }
        kept.push(event);
    }

    if kept.is_empty() {
        return None;
    }

    // Publish the (content-unchanged) generation so the revision counter
    // advances, then let ty do the incremental work. HEAD reads disk directly,
    // so no store mutation is needed for non-overlaid disk changes.
    head.system.publish(head.store.capture());
    let result = head.db.apply_changes(&kept, None);
    let revision = head.store.bump_revision().0;

    Some(dto::SyncResultDto {
        revision,
        created,
        changed,
        deleted,
        project_changed: result.project_changed(),
        custom_stdlib_changed: result.custom_stdlib_changed(),
        rescan: false,
    })
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

        let head = build_head(system_root, ContentStore::new());

        Ok(PyTyProject {
            inner: Arc::new(Mutex::new(Some(head))),
            pending: Arc::new(Mutex::new(Vec::new())),
            watcher: Mutex::new(None),
        })
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
        // Preserve overlay content across the rebuild: move the existing store
        // out and re-seed the new head from it. (Empty in Phase 2; meaningful
        // once edits land in Phase 3.)
        let store = std::mem::replace(&mut head.store, ContentStore::new());

        // Rebuild while still holding the lock, then swap atomically.
        *guard = Some(build_head(root, store));
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

    // ── Write path (Phase 3) ───────────────────────────────────────
    //
    // All writes hold the GIL (§7.3) and mutate the HeadState in-place.
    // Each returns a SyncResult dict (via pythonize) describing the delta.
    //
    // IMPORTANT: a live snapshot (`snapshot()`) shares the HEAD Zalsa and
    // will block `apply_changes` forever (architecture §0). Always close()
    // snapshots before calling any write method. Phase 4 fixes this.

    /// Overlay `path` with in-memory `text` (no disk write). Returns a
    /// SyncResult dict with the new revision and the affected paths.
    fn edit<'py>(&self, py: Python<'py>, path: &str, text: &str) -> PyResult<Bound<'py, PyAny>> {
        let mut guard = lock_state(&self.inner, "edit")?;
        let head = guard.as_mut().unwrap();

        let abs = resolve_sync_path(&head.root, path);
        let event = classify_overlay_edit(&head.system, &head.db, &abs); // classify BEFORE mutating
        head.store.insert_text(abs.clone(), text); // 1. mutate

        let path_str = abs.as_str().to_string();
        let (created, changed) = match &event {
            ChangeEvent::Created { .. } => (vec![path_str], vec![]),
            _ => (vec![], vec![path_str]),
        };
        let dto =
            commit_head(head, std::slice::from_ref(&event), created, changed, vec![], false);
        drop(guard);
        pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    /// Overlay many files atomically (one publish, one `apply_changes`, one
    /// published revision).
    fn edit_many<'py>(
        &self,
        py: Python<'py>,
        edits: std::collections::HashMap<String, String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let mut guard = lock_state(&self.inner, "edit_many")?;
        let head = guard.as_mut().unwrap();

        let mut events = Vec::with_capacity(edits.len());
        let (mut created, mut changed) = (Vec::new(), Vec::new());
        for (path, text) in edits {
            let abs = resolve_sync_path(&head.root, &path);
            let event = classify_overlay_edit(&head.system, &head.db, &abs);
            head.store.insert_text(abs.clone(), text);
            match &event {
                ChangeEvent::Created { .. } => created.push(abs.as_str().to_string()),
                _ => changed.push(abs.as_str().to_string()),
            }
            events.push(event);
        }
        let dto = commit_head(head, &events, created, changed, vec![], false);
        drop(guard);
        pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    /// Overlay a virtual/unsaved buffer (e.g. "untitled:1"). No disk involvement.
    fn edit_virtual<'py>(
        &self,
        py: Python<'py>,
        uri: &str,
        text: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let mut guard = lock_state(&self.inner, "edit_virtual")?;
        let head = guard.as_mut().unwrap();

        let vpath = SystemVirtualPathBuf::from(uri.to_string());
        let is_new = head.db.files().try_virtual_file(&vpath).is_none();
        head.store.insert_virtual(vpath.clone(), text);

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
        let dto =
            commit_head(head, std::slice::from_ref(&event), created, changed, vec![], false);
        drop(guard);
        pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    /// Ingest a disk change for `path`: drop any overlay for it and re-read disk.
    fn sync_path<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let mut guard = lock_state(&self.inner, "sync_path")?;
        let head = guard.as_mut().unwrap();
        let abs = resolve_sync_path(&head.root, path);
        let dto = sync_path_inner(head, abs);
        drop(guard);
        pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    /// Drop the overlay buffer for `path`, reverting to disk. Same semantics as
    /// `sync_path` but named for intent.
    fn discard<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let mut guard = lock_state(&self.inner, "discard")?;
        let head = guard.as_mut().unwrap();
        let abs = resolve_sync_path(&head.root, path);
        let dto = sync_path_inner(head, abs);
        drop(guard);
        pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    /// Rescan everything (in-place, via `apply_changes`). Existing overlay
    /// buffers are preserved; ty re-walks and re-reads all files.
    fn sync_all<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let mut guard = lock_state(&self.inner, "sync_all")?;
        let head = guard.as_mut().unwrap();

        head.system.publish(head.store.capture());
        let result = head.db.apply_changes(&[ChangeEvent::Rescan], None);
        let revision = head.store.bump_revision().0;
        let dto = dto::SyncResultDto {
            revision,
            created: vec![],
            changed: vec![],
            deleted: vec![],
            project_changed: result.project_changed(),
            custom_stdlib_changed: result.custom_stdlib_changed(),
            rescan: true,
        };
        drop(guard);
        pythonize(py, &dto).map_err(|e| PyRuntimeError::new_err(e.to_string()))
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

        // 2. Apply under the head lock (the single-writer section).
        let dto = {
            let mut guard = lock_state(&self.inner, "poll_changes")?;
            let head = guard.as_mut().unwrap();
            apply_watch_events(head, events)
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
        drop(guard); // release the head lock BEFORE the (cold) db build

        let state = build_frozen(root, generation, rev);
        Ok(PySnapshot {
            inner: Mutex::new(Some(state)),
            revision: rev.0,
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
        let head = build_head(root.clone(), ContentStore::new());
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
        let head_default = build_head(root_default.clone(), ContentStore::new());
        let diags_default = head_default.db.check();

        // Config forces Python 3.8 → syntax error on PEP 695 generics.
        let (_d2, root_38) = project(Some(toml_38), pep695);
        let head_38 = build_head(root_38.clone(), ContentStore::new());
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
        let head = build_head(root.clone(), ContentStore::new()); // must not panic
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
        let mut head = build_head(root.clone(), ContentStore::new());
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
        let mut head = build_head(root.clone(), ContentStore::new());
        let r0 = head.store.revision().0;
        head.store.insert_text(root.join("a.py"), "X = 2\n");
        assert!(head.store.revision().0 > r0);
    }

    /// A created (previously-absent) file is classified Created and becomes
    /// visible.
    #[test]
    fn edit_creates_new_file() {
        let (_dir, root) = project("X = 1\n");
        let mut head = build_head(root.clone(), ContentStore::new());
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
        let mut head = build_head(root.clone(), ContentStore::new());
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
        let mut head = build_head(root.clone(), ContentStore::new());
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
        assert!(read(&snap0, &a).contains("X = 1")); // capture-once pins it

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

    /// Read-once capture pins disk content even if disk changes out from under a
    /// snapshot AFTER the snapshot first read the file.
    #[test]
    fn read_once_capture_pins_first_read() {
        let (_dir, root) = project("DISK = 1\n");
        let head = build_head(root.clone(), ContentStore::new());
        let a = root.join("a.py");

        let snap = build_frozen(root.clone(), head.store.capture(), head.store.revision());
        assert!(read(&snap, &a).contains("DISK = 1")); // captures "DISK = 1"

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
        let ev = ChangeEvent::file_content_changed(a.clone());
        head.db
            .apply_changes(std::slice::from_ref(&ev), None);

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
        assert!(read(&snap0, &a).contains("X = 1")); // pinned to r0's (disk) content
    }
}

// ── Phase 5 tests: Concurrency proof ──────────────────────────────────────
//
// Prove the core architectural invariant: many reader snapshots held open
// do NOT block the writer (independent Zalsa per snapshot), snapshot reads
// never surface salsa::Cancelled, and frozen read-once disk capture is
// race-safe under concurrent reads.

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
        let mut head = build_head(root.clone(), ContentStore::new());
        let a = root.join("a.py");

        let snapshot_count = stress_count("TYO3_MVCC_STRESS_SNAPSHOTS", 32);
        let edit_count = stress_count("TYO3_MVCC_STRESS_EDITS", 100);

        let snapshots: Vec<TyProjectState> = (0..snapshot_count)
            .map(|_| {
                build_frozen(
                    root.clone(),
                    head.store.capture(),
                    head.store.revision(),
                )
            })
            .collect();

        // Force each snapshot to do real work before the writer starts.
        // This catches both "held but idle clone" and "active snapshot db" bugs.
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
        let mut head = build_head(root.clone(), ContentStore::new());
        let a = root.join("a.py");

        let reader_count = stress_count("TYO3_MVCC_STRESS_READERS", 8);
        let edit_count = stress_count("TYO3_MVCC_STRESS_EDITS", 100);

        let snapshots: Vec<TyProjectState> = (0..reader_count)
            .map(|_| {
                build_frozen(
                    root.clone(),
                    head.store.capture(),
                    head.store.revision(),
                )
            })
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

    // ── 5.3 Concurrent frozen disk capture is race-safe ─────────────────

    /// Phase 4's `capture_disk_file` uses `ArcSwap::rcu` and a shared
    /// `capture_version`. This test makes many clones of one snapshot read
    /// the same uncaptured disk-backed file at once. Every reader must get
    /// the same content and no reader may panic.
    #[test]
    fn frozen_read_once_capture_is_race_safe() {
        let (_dir, root) = project("CAPTURED = 1\n");
        let head = build_head(root.clone(), ContentStore::new());
        let a = root.join("a.py");
        let snap = build_frozen(
            root.clone(),
            head.store.capture(),
            head.store.revision(),
        );

        let reader_count = stress_count("TYO3_MVCC_STRESS_READERS", 16);
        let barrier = Arc::new(Barrier::new(reader_count));
        let (tx, rx) = mpsc::channel();

        for _ in 0..reader_count {
            let snap_clone =
                TyProjectState { db: snap.db.clone(), root: snap.root.clone() };
            let barrier = Arc::clone(&barrier);
            let tx = tx.clone();
            let a = a.clone();
            std::thread::spawn(move || {
                barrier.wait();
                let result =
                    catch_unwind(AssertUnwindSafe(|| read_source(&snap_clone, &a)));
                let _ = tx.send(
                    result.map_err(|_| "panic during frozen capture".to_string()),
                );
            });
        }
        drop(tx);

        let mut texts = Vec::new();
        for _ in 0..reader_count {
            let result = rx
                .recv_timeout(Duration::from_secs(5))
                .expect("frozen capture reader did not finish");
            texts.push(result.expect("frozen capture reader panicked"));
        }

        assert!(texts.iter().all(|t| t.contains("CAPTURED = 1")));
        assert!(
            texts.windows(2).all(|w| w[0] == w[1]),
            "all concurrent first reads should observe identical captured content"
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

    #[test]
    fn empty_batch_is_noop() {
        let (_d, root) = project(&[("a.py", "x = 1\n")]);
        let mut head = build_head(root, ContentStore::new());
        assert!(apply_watch_events(&mut head, vec![]).is_none());
    }

    #[test]
    fn changed_event_matches_explicit_sync_path() {
        // The load-bearing parity: a watcher Changed event yields the same delta
        // shape as an explicit sync_path for the same on-disk change.
        let (_d, root) = project(&[("a.py", "x: int = 1\n")]);
        let a = root.join("a.py");

        // Watcher-driven head.
        let mut head_w = build_head(root.clone(), ContentStore::new());
        let _ = head_w.db.apply_changes(&[ChangeEvent::Rescan], None); // warm discovery
        std::fs::write(a.as_std_path(), b"x: str = 'two'\n").unwrap();
        let via_watch =
            apply_watch_events(&mut head_w, vec![ChangeEvent::file_content_changed(a.clone())])
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
        let mut head = build_head(root, ContentStore::new());

        // Agent overlay buffer (unsaved).
        head.store.insert_text(a.clone(), "BUFFER = 2\n".to_string());
        head.system.publish(head.store.capture());
        head.db.apply_changes(&[ChangeEvent::file_content_changed(a.clone())], None);

        // A disk change underneath the buffer arrives via the watcher.
        std::fs::write(a.as_std_path(), b"DISK = 999\n").unwrap();
        let result = apply_watch_events(&mut head, vec![ChangeEvent::file_content_changed(a.clone())]);

        // Dropped: nothing applied, buffer still wins.
        assert!(result.is_none(), "overlaid path must not be clobbered by a disk event");
        let file = ruff_db::files::system_path_to_file(&head.db, &a).unwrap();
        assert!(source_text(&head.db, file).as_str().contains("BUFFER = 2"));
    }

    #[test]
    fn rescan_event_short_circuits() {
        let (_d, root) = project(&[("a.py", "x = 1\n")]);
        let mut head = build_head(root.clone(), ContentStore::new());
        let a = root.join("a.py");
        let r = apply_watch_events(
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
        let mut head = build_head(root.clone(), ContentStore::new());
        let before = head.store.revision().0;
        std::fs::write(a.as_std_path(), b"x = 2\n").unwrap();
        let r = apply_watch_events(
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
