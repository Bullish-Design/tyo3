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

// ── State ────────────────────────────────────────────────────────────────

/// Internal mutable state of a TyO3 project session.
///
/// NOTE: `ProjectDatabase` (Salsa 0.26) is `!Send + !Sync` because it
/// internally uses `RefCell<salsa::active_query::QueryStack>` and
/// `UnsafeCell<HashMap<...>>`.  This means `&TyProjectState` is not `Send`,
/// so we cannot wrap heavy analysis calls in `py.detach(|| ...)` to
/// release the GIL.  All Rust analysis therefore runs with the GIL held.
/// This is acceptable for v0.1; future Salsa versions may become `Sync`.
struct TyProjectState {
    db: ProjectDatabase,
    root: SystemPathBuf,
}

/// Python-facing wrapper.  The inner `Option` is `None` after `close()`;
/// every operation checks this first and raises if the project is closed.
#[pyclass(name = "TyProject", module = "tyo3._native_impl")]
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

/// GIL-free core of `check`: runs the project type-check and builds the DTO.
/// Touches no Python state, so it is safe to call inside `py.detach(...)`.
fn compute_check(state: &TyProjectState) -> dto::CheckResultDto {
    let result = state.db.check();
    let diagnostics = convert::diagnostics::convert_diagnostics(&state.db, &result);
    dto::CheckResultDto {
        diagnostics,
        files_checked: None,
        elapsed_ms: None,
    }
}

/// Resolve a file handle and return its source text as a String.
fn resolve_file_and_source(
    state: &TyProjectState,
    path: &str,
) -> PyResult<(File, String)> {
    let file = file_resolver::resolve_file(
        &state.db,
        state.root.as_std_path(),
        path,
    )
    .map_err(|e| PathResolutionError::new_err(e))?;

    let src = source_text(&state.db, file);
    let source_str = src.as_str().to_string();

    Ok((file, source_str))
}

/// Shared implementation for goto_definition, goto_declaration,
/// goto_type_definition.  Resolves the path, computes the source offset,
/// calls the provided navigation function, converts the results, and
/// returns a Python list of definition-target dicts via pythonize.
fn navigate_to_targets<'py>(
    inner: &Mutex<Option<TyProjectState>>,
    op_name: &str,
    py: Python<'py>,
    path: &str,
    line: u32,
    column: u32,
    navigate_fn: fn(
        &dyn Db,
        File,
        ruff_text_size::TextSize,
    ) -> Option<ty_ide::RangedValue<ty_ide::NavigationTargets>>,
) -> PyResult<Bound<'py, PyAny>> {
    let guard = lock_state(inner, op_name)?;
    let state = guard.as_ref().unwrap();

    let (file, source_str) = resolve_file_and_source(state, path)?;

    let line_index = LineIndex::from_source_text(&source_str);
    let offset = coordinates::position_to_offset_with_index(
        &source_str, &line_index, line, column,
    )
    .map_err(|e| PositionError::new_err(e))?;

    let result = navigate_fn(&state.db, file, offset);

    let targets = match result {
        Some(targets) => {
            convert::navigation::convert_navigation_targets(&state.db, &targets)
        }
        None => Vec::new(),
    };

    pythonize(py, &targets).map_err(|e| PyRuntimeError::new_err(e.to_string()))
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
    fn files(&self) -> PyResult<Vec<String>> {
        let guard = lock_state(&self.inner, "files")?;
        let state = guard.as_ref().unwrap();

        let project = state.db.project();
        let indexed = project.files(&state.db);
        let paths: Vec<String> = indexed
            .iter()
            .map(|f: &File| f.path(&state.db).as_str().to_string())
            .collect();

        Ok(paths)
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
    ///
    /// Runs a full project check (Salsa-cached if unchanged), then filters
    /// diagnostics in Rust before constructing DTOs — only matching file
    /// diagnostics are converted and returned across the boundary.
    fn check_file<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let guard = lock_state(&self.inner, "check_file")?;
        let state = guard.as_ref().unwrap();

        // Resolve the target file path for comparison
        let (file, _) = resolve_file_and_source(state, path)?;
        let target_path = file.path(&state.db).as_str().to_string();

        // Full project check (Salsa-cached if unchanged)
        let all_diagnostics = state.db.check();

        // Filter in Rust — only convert matching diagnostics to DTOs
        let matching: Vec<_> = all_diagnostics
            .iter()
            .filter(|d| {
                convert::diagnostics::diagnostic_matches_file(
                    &state.db, d, &target_path,
                )
            })
            .collect();

        let diagnostics =
            convert::diagnostics::convert_diagnostic_refs(&state.db, &matching);

        let check_result = dto::CheckResultDto {
            diagnostics,
            files_checked: Some(1),
            elapsed_ms: None,
        };

        pythonize(py, &check_result)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Document Symbols ─────────────────────────────────────────────

    /// Get document symbols for a file in the project.
    fn document_symbols<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let guard = lock_state(&self.inner, "document_symbols")?;
        let state = guard.as_ref().unwrap();

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

        pythonize(py, &symbols)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Workspace Symbols ────────────────────────────────────────────

    /// Search for symbols matching a query across all workspace files.
    fn workspace_symbols<'py>(&self, py: Python<'py>, query: &str) -> PyResult<Bound<'py, PyAny>> {
        let guard = lock_state(&self.inner, "workspace_symbols")?;
        let state = guard.as_ref().unwrap();

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

        pythonize(py, &symbols)
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
        navigate_to_targets(
            &self.inner,
            "goto_definition",
            py,
            path,
            line,
            column,
            ty_ide::goto_definition,
        )
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
        navigate_to_targets(
            &self.inner,
            "goto_declaration",
            py,
            path,
            line,
            column,
            ty_ide::goto_declaration,
        )
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
        navigate_to_targets(
            &self.inner,
            "goto_type_definition",
            py,
            path,
            line,
            column,
            ty_ide::goto_type_definition,
        )
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
        let guard = lock_state(&self.inner, "find_references")?;
        let state = guard.as_ref().unwrap();

        let (file, source_str) = resolve_file_and_source(state, path)?;

        let line_index = LineIndex::from_source_text(&source_str);
        let offset = coordinates::position_to_offset_with_index(
            &source_str, &line_index, line, column,
        )
        .map_err(|e| PositionError::new_err(e))?;

        let result = ty_ide::find_references(
            &state.db,
            file,
            offset,
            include_declaration,
        );

        let references = match result {
            Some(refs) => convert::navigation::convert_references(&state.db, &refs),
            None => Vec::new(),
        };

        pythonize(py, &references)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Semantic Tokens ─────────────────────────────────────────

    /// Return semantic tokens for a file.
    fn semantic_tokens<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyAny>> {
        let guard = lock_state(&self.inner, "semantic_tokens")?;
        let state = guard.as_ref().unwrap();

        let (file, source_str) = resolve_file_and_source(state, path)?;
        let line_index = LineIndex::from_source_text(&source_str);

        let tokens = ty_ide::semantic_tokens(&state.db, file, None);

        let result = convert::tokens::convert_semantic_tokens(
            &source_str,
            &line_index,
            &tokens,
        );

        pythonize(py, &result)
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
        let guard = lock_state(&self.inner, "file_occurrences")?;
        let state = guard.as_ref().unwrap();

        let (file, source_str) = resolve_file_and_source(state, path)?;
        let line_index = LineIndex::from_source_text(&source_str);

        let result = convert::occurrences::convert_file_occurrences(
            &state.db,
            file,
            &source_str,
            &line_index,
        );

        pythonize(py, &result)
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
        let guard = lock_state(&self.inner, "type_hierarchy")?;
        let state = guard.as_ref().unwrap();

        let (file, source_str) = resolve_file_and_source(state, path)?;
        let line_index = LineIndex::from_source_text(&source_str);
        let offset = coordinates::position_to_offset_with_index(
            &source_str, &line_index, line, column,
        )
        .map_err(|e| PositionError::new_err(e))?;

        // Step 1: Prepare the hierarchy item at this position
        let prepared = ty_ide::prepare_type_hierarchy(&state.db, file, offset);
        let item = match prepared {
            Some(item) => item,
            None => {
                return Ok(py.None().bind(py).clone());
            }
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

        let result = dto::TypeHierarchyDto {
            item: item_dto,
            supertypes: supertypes_dto,
            subtypes: subtypes_dto,
        };

        pythonize(py, &result)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    // ── Hover ────────────────────────────────────────────────────────

    /// Get hover information for the symbol at the given position.
    ///
    /// Returns a dict, or None if no hover info is available.
    ///
    /// NOTE: ty_ide does not publicly re-export Hover/HoverContent, so the
    /// entire hover is rendered as Markdown and returned as a single content
    /// item of kind `markdown`.
    fn hover<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        line: u32,
        column: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let guard = lock_state(&self.inner, "hover")?;
        let state = guard.as_ref().unwrap();

        let (file, source_str) = resolve_file_and_source(state, path)?;

        let line_index = LineIndex::from_source_text(&source_str);
        let offset = coordinates::position_to_offset_with_index(
            &source_str, &line_index, line, column,
        )
        .map_err(|e| PositionError::new_err(e))?;

        let result = ty_ide::hover(&state.db, file, offset);

        match result {
            None => Ok(py.None().bind(py).clone()),
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

                pythonize(py, &hover_dto)
                    .map_err(|e| PyRuntimeError::new_err(e.to_string()))
            }
        }
    }
}

// ── Snapshot: immutable, revision-pinned, thread-shareable read view ──────────
//
// A `PySnapshot` owns its own clone of the project database, pinned to the
// revision at `snapshot()` time. Each read clones that frozen db under a brief
// lock and runs the analysis with the GIL released, so many threads can share
// one snapshot and run reads in parallel.

#[pyclass(name = "TySnapshot", module = "tyo3._native_impl")]
pub struct PySnapshot {
    inner: Mutex<Option<TyProjectState>>,
}

#[pymethods]
impl PySnapshot {
    /// Run the type checker on the snapshot's pinned revision (GIL released).
    fn check<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let state = clone_locked_state(&self.inner, "check")?;
        let check_result = py.detach(move || compute_check(&state));
        pythonize(py, &check_result)
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    /// Release the pinned revision early, freeing its database. Idempotent.
    fn close(&self) -> PyResult<()> {
        let mut guard = self.inner.lock().map_err(|e| {
            PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
        })?;
        *guard = None;
        Ok(())
    }
}
