use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pythonize::pythonize;

use crate::{ProjectClosedError, PathResolutionError, PositionError};

use ruff_db::files::File;
use ruff_db::source::source_text;
use ruff_db::system::{OsSystem, SystemPathBuf};
use ruff_source_file::LineIndex;

use ty_project::Db;
use ty_project::{ProjectDatabase, ProjectMetadata};

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

/// Internal mutable state of a TyO3 project session.
///
/// CONCURRENCY: `ProjectDatabase` (salsa 0.26) is `Send + Clone` but `!Sync`
/// — its `salsa::Storage` holds a per-thread `ZalsaLocal` (`RefCell`/`UnsafeCell`).
/// You cannot share `&db` across threads, but you CAN move an owned clone, which is
/// how ty itself parallelizes (`ty_project::Project::check` clones the db per rayon
/// worker). Read methods take a `db.clone()` snapshot via `clone_locked_state` and
/// run inside `py.detach(move || …)` to release the GIL. Because `#[pyclass]` only
/// requires `Send` (not `Sync`), the `Mutex` below is what makes concurrent `&self`
/// access sound once the GIL is released. The canonical db is only ever swapped
/// (never mutated in place) — see `reload` — so outstanding clones stay isolated.
struct TyProjectState {
    db: ProjectDatabase,
    root: SystemPathBuf,
}

/// Python-facing wrapper.  The inner `Option` is `None` after `close()`;
/// every operation checks this first and raises if the project is closed.
#[pyclass(name = "TyProject", module = "tyo3._native_impl", frozen)]
pub struct PyTyProject {
    inner: Mutex<Option<TyProjectState>>,
}

// ── Internal helpers ─────────────────────────────────────────────────────

/// Lock and access the state.  Returns an error if the project is closed
/// or the mutex is poisoned.
fn lock_state<'a>(
    inner: &'a Mutex<Option<TyProjectState>>,
    op_name: &str,
) -> PyResult<std::sync::MutexGuard<'a, Option<TyProjectState>>> {
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
fn clone_locked_state(
    inner: &Mutex<Option<TyProjectState>>,
    op_name: &str,
) -> PyResult<TyProjectState> {
    let guard = lock_state(inner, op_name)?;
    let s = guard.as_ref().unwrap();
    Ok(TyProjectState {
        db: s.db.clone(),
        root: s.root.clone(),
    })
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

/// Return semantic tokens for a file.
fn compute_semantic_tokens(
    state: &TyProjectState,
    path: &str,
) -> Result<Vec<dto::SemanticTokenDto>, AnalysisError> {
    let (file, source_str) = resolve_file_and_source(state, path)?;
    let line_index = LineIndex::from_source_text(&source_str);

    let tokens = ty_ide::semantic_tokens(&state.db, file, None);

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

        let system = OsSystem::new(system_root.clone());
        let metadata = ProjectMetadata::new(
            ruff_python_ast::name::Name::new("tyo3-project"),
            system_root.clone(),
        );

        let db = ProjectDatabase::use_defaults(metadata, system);

        Ok(PyTyProject {
            inner: Mutex::new(Some(TyProjectState {
                db,
                root: system_root,
            })),
        })
    }

    // ── Lifecycle: Reload ────────────────────────────────────────────

    /// Reload the project: drop the current database and re-create it.
    /// Clears all cached diagnostics and symbol data.
    ///
    /// Holds the lock throughout — no window where concurrent callers
    /// see a closed project.
    fn reload(&self) -> PyResult<()> {
        let mut guard = lock_state(&self.inner, "reload")?;
        let root = guard.as_ref().unwrap().root.clone();

        // Create the new database while still holding the lock.
        // This is CPU-bound work with no lock-contention risk.
        let system = OsSystem::new(root.clone());
        let metadata = ProjectMetadata::new(
            ruff_python_ast::name::Name::new("tyo3-project"),
            root.clone(),
        );
        let db = ProjectDatabase::use_defaults(metadata, system);

        // Atomically swap — old database drops when guard's previous value drops
        *guard = Some(TyProjectState { db, root });
        Ok(())
    }

    // ── Lifecycle: Close ─────────────────────────────────────────────

    /// Close the project and free all resources.
    /// Idempotent — closing an already-closed project is a no-op.
    fn close(&self) -> PyResult<()> {
        let mut guard = self.inner.lock().map_err(|e| {
            PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
        })?;

        // Setting None on an already-None guard is harmless.
        *guard = None;
        Ok(())
    }

    // ── Snapshot ─────────────────────────────────────────────────────

    /// Take a cheap, read-only, revision-pinned snapshot of the current project
    /// state. The returned snapshot is safe to share across threads and is
    /// isolated from later `reload()` calls (which swap in a fresh database).
    fn snapshot(&self) -> PyResult<PySnapshot> {
        let state = clone_locked_state(&self.inner, "snapshot")?;
        Ok(PySnapshot {
            inner: Mutex::new(Some(state)),
        })
    }

    // ── Files ────────────────────────────────────────────────────────

    /// List all source files in the project.
    fn files(&self, py: Python<'_>) -> PyResult<Vec<String>> {
        let state = clone_locked_state(&self.inner, "files")?;
        Ok(py.detach(move || compute_files(&state)))
    }

    // ── Check ────────────────────────────────────────────────────────

    /// Run the type checker on the entire project.
    ///
    /// Clones the database under a brief lock, then runs the analysis with the
    /// GIL released (`py.detach`), so other Python threads make progress during
    /// the scan. See the `TyProjectState` concurrency note.
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

    // ── Document Symbols ─────────────────────────────────────────────

    /// Get document symbols for a file in the project.
    fn document_symbols<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "document_symbols")?;
        let path = path.to_owned();
        let dtos = py.detach(move || compute_document_symbols(&state, &path))
            .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Workspace Symbols ────────────────────────────────────────────

    /// Search for symbols matching a query across all workspace files.
    fn workspace_symbols<'py>(&self, py: Python<'py>, query: &str) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "workspace_symbols")?;
        let query = query.to_owned();
        let dtos = py.detach(move || compute_workspace_symbols(&state, &query));
        pythonize(py, &dtos)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Goto Definition ──────────────────────────────────────────────

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

    // ── Goto Declaration ─────────────────────────────────────────────

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

    // ── Goto Type Definition ─────────────────────────────────────────

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

    // ── Find References ──────────────────────────────────────────────

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

    // ── Semantic Tokens ─────────────────────────────────────────

    /// Return semantic tokens for a file.
    fn semantic_tokens<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "semantic_tokens")?;
        let path = path.to_owned();
        let dtos = py.detach(move || compute_semantic_tokens(&state, &path))
            .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── File Occurrences ─────────────────────────────────────────

    /// Batch-resolve all name occurrences in a file.
    ///
    /// Returns a list describing every name-like token in the file,
    /// including its location, the symbol it resolves to (target file +
    /// target name), and the reference role.
    ///
    /// This replaces the per-token `goto_definition` approach with a
    /// single Rust call per file — O(1) FFI calls instead of O(tokens).
    fn file_occurrences<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "file_occurrences")?;
        let path = path.to_owned();
        let dtos = py.detach(move || compute_file_occurrences(&state, &path))
            .map_err(AnalysisError::into_pyerr)?;
        pythonize(py, &dtos)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Type Hierarchy ──────────────────────────────────────────

    /// Query type hierarchy at a position: returns the item with supertypes and subtypes.
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

    // ── Hover ────────────────────────────────────────────────────────

    /// Get hover information for the symbol at the given position.
    ///
    /// Returns a dict, or None if no hover info is available.
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
}

#[pymethods]
impl PySnapshot {
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

    /// Return semantic tokens for a file.
    fn semantic_tokens<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "semantic_tokens")?;
        let path = path.to_owned();
        let dtos = py.detach(move || compute_semantic_tokens(&state, &path))
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
