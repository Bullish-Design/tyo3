use std::path::PathBuf;
use std::sync::Mutex;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

use crate::{ProjectClosedError, PathResolutionError, PositionError};

use ruff_db::files::File;
use ruff_db::source::source_text;
use ruff_db::system::{OsSystem, SystemPathBuf};

use ty_project::Db;
use ty_project::{ProjectDatabase, ProjectMetadata};

use crate::convert;
use crate::coordinates;
use crate::dto;
use crate::files as file_resolver;

// ── State ────────────────────────────────────────────────────────────────

/// Internal mutable state of a TyO3 project session.
struct TyProjectState {
    db: ProjectDatabase,
    root: SystemPathBuf,
}

/// Python-facing wrapper.  The inner `Option` is `None` after `close()`;
/// every operation checks this first and raises if the project is closed.
#[pyclass(name = "TyProject")]
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
/// serialises to JSON.
fn navigate_to_targets(
    inner: &Mutex<Option<TyProjectState>>,
    op_name: &str,
    path: &str,
    line: u32,
    column: u32,
    navigate_fn: fn(
        &dyn Db,
        File,
        ruff_text_size::TextSize,
    ) -> Option<ty_ide::RangedValue<ty_ide::NavigationTargets>>,
) -> PyResult<String> {
    let guard = lock_state(inner, op_name)?;
    let state = guard.as_ref().unwrap();

    let (file, source_str) = resolve_file_and_source(state, path)?;

    let pos = dto::PositionDto { line, column };
    let offset = coordinates::position_to_offset(&source_str, &pos)
        .map_err(|e| PositionError::new_err(e))?;

    let result = navigate_fn(&state.db, file, offset);

    let targets = match result {
        Some(targets) => {
            convert::navigation::convert_navigation_targets(&state.db, &targets)
        }
        None => Vec::new(),
    };

    serde_json::to_string(&targets)
        .map_err(|e| PyRuntimeError::new_err(format!("Serialisation failed: {}", e)))
}

/// Recursively collect document symbols from a hierarchical symbol tree.
fn collect_symbols_recursive(
    hierarchical: &ty_ide::HierarchicalSymbols,
    id: ty_ide::SymbolId,
    info: &ty_ide::SymbolInfo,
    source: &str,
    line_index: &ruff_source_file::LineIndex,
    file_path: &str,
    parent_name: Option<&str>,
    symbols: &mut Vec<dto::SymbolDto>,
) {
    let qualified = match parent_name {
        Some(p) => Some(format!("{}.{}", p, info.name)),
        None => None,
    };

    let sym = convert::symbols::convert_symbol(
        source,
        line_index,
        file_path,
        &info.name,
        &info.kind,
        info.deprecated,
        info.name_range,
        info.full_range,
        parent_name,
        qualified.clone(),
    );
    symbols.push(sym);

    let own_name = match &qualified {
        Some(q) => q.as_str(),
        None => &info.name,
    };

    for (child_id, child_info) in hierarchical.children(id) {
        collect_symbols_recursive(
            hierarchical,
            child_id,
            &child_info,
            source,
            line_index,
            file_path,
            Some(own_name),
            symbols,
        );
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
    fn reload(&self) -> PyResult<()> {
        let mut guard = lock_state(&self.inner, "reload")?;
        let root = guard.as_ref().unwrap().root.clone();

        // Drop the old database
        *guard = None;
        drop(guard);

        // Re-create with the same root
        let system = OsSystem::new(root.clone());
        let metadata = ProjectMetadata::new(
            ruff_python_ast::name::Name::new("tyo3-project"),
            root.clone(),
        );
        let db = ProjectDatabase::use_defaults(metadata, system);

        let mut guard = self.inner.lock().map_err(|e| {
            PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
        })?;
        *guard = Some(TyProjectState { db, root });
        Ok(())
    }

    // ── Lifecycle: Close ─────────────────────────────────────────────

    /// Close the project and free all resources.
    /// Subsequent operations will raise an error.
    fn close(&self) -> PyResult<()> {
        let mut guard = self.inner.lock().map_err(|e| {
            PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
        })?;

        if guard.is_none() {
            return Err(ProjectClosedError::new_err("Project is already closed"));
        }

        // Drop the database
        *guard = None;
        Ok(())
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
    fn check(&self) -> PyResult<String> {
        let guard = lock_state(&self.inner, "check")?;
        let state = guard.as_ref().unwrap();

        let result = state.db.check();
        let diagnostics = convert::diagnostics::convert_diagnostics(&result);

        let check_result = dto::CheckResultDto {
            diagnostics,
            files_checked: None,
            elapsed_ms: None,
        };

        serde_json::to_string(&check_result)
            .map_err(|e| PyRuntimeError::new_err(format!("Serialisation failed: {}", e)))
    }

    // ── Document Symbols ─────────────────────────────────────────────

    /// Get document symbols for a file in the project.
    fn document_symbols(&self, path: &str) -> PyResult<String> {
        let guard = lock_state(&self.inner, "document_symbols")?;
        let state = guard.as_ref().unwrap();

        let (file, source_str) = resolve_file_and_source(state, path)?;

        let flat_symbols = ty_ide::document_symbols(&state.db, file);
        let hierarchical = flat_symbols.to_hierarchical();

        let file_path = file.path(&state.db).as_str().to_string();
        let line_index = ruff_source_file::LineIndex::from_source_text(&source_str);

        let mut symbols: Vec<dto::SymbolDto> = Vec::new();
        for (id, info) in hierarchical.iter() {
            collect_symbols_recursive(
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

        serde_json::to_string(&symbols)
            .map_err(|e| PyRuntimeError::new_err(format!("Serialisation failed: {}", e)))
    }

    // ── Workspace Symbols ────────────────────────────────────────────

    /// Search for symbols matching a query across all workspace files.
    fn workspace_symbols(&self, query: &str) -> PyResult<String> {
        let guard = lock_state(&self.inner, "workspace_symbols")?;
        let state = guard.as_ref().unwrap();

        let results = ty_ide::workspace_symbols(&state.db, query);

        let mut symbols: Vec<dto::SymbolDto> = Vec::new();
        for ws_info in &results {
            let src = source_text(&state.db, ws_info.file);
            let source_str = src.as_str().to_string();
            let file_path = ws_info.file.path(&state.db).as_str().to_string();

            let line_index = ruff_source_file::LineIndex::from_source_text(&source_str);

            let sym = convert::symbols::convert_symbol(
                &source_str,
                &line_index,
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

        serde_json::to_string(&symbols)
            .map_err(|e| PyRuntimeError::new_err(format!("Serialisation failed: {}", e)))
    }

    // ── Goto Definition ──────────────────────────────────────────────

    /// Navigate to the definition of the symbol at the given position.
    fn goto_definition(
        &self,
        path: &str,
        line: u32,
        column: u32,
    ) -> PyResult<String> {
        navigate_to_targets(
            &self.inner,
            "goto_definition",
            path,
            line,
            column,
            ty_ide::goto_definition,
        )
    }

    // ── Goto Declaration ─────────────────────────────────────────────

    /// Navigate to the declaration of the symbol at the given position.
    fn goto_declaration(
        &self,
        path: &str,
        line: u32,
        column: u32,
    ) -> PyResult<String> {
        navigate_to_targets(
            &self.inner,
            "goto_declaration",
            path,
            line,
            column,
            ty_ide::goto_declaration,
        )
    }

    // ── Goto Type Definition ─────────────────────────────────────────

    /// Navigate to the type definition of the symbol at the given position.
    fn goto_type_definition(
        &self,
        path: &str,
        line: u32,
        column: u32,
    ) -> PyResult<String> {
        navigate_to_targets(
            &self.inner,
            "goto_type_definition",
            path,
            line,
            column,
            ty_ide::goto_type_definition,
        )
    }

    // ── Find References ──────────────────────────────────────────────

    /// Find all references to the symbol at the given position.
    fn find_references(
        &self,
        path: &str,
        line: u32,
        column: u32,
        include_declaration: bool,
    ) -> PyResult<String> {
        let guard = lock_state(&self.inner, "find_references")?;
        let state = guard.as_ref().unwrap();

        let (file, source_str) = resolve_file_and_source(state, path)?;

        let pos = dto::PositionDto { line, column };
        let offset = coordinates::position_to_offset(&source_str, &pos)
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

        serde_json::to_string(&references)
            .map_err(|e| PyRuntimeError::new_err(format!("Serialisation failed: {}", e)))
    }

    // ── Hover ────────────────────────────────────────────────────────

    /// Get hover information for the symbol at the given position.
    ///
    /// Returns a JSON-encoded HoverDto, or None if no hover info is available.
    ///
    /// NOTE: ty_ide does not publicly re-export Hover/HoverContent, so the
    /// entire hover is rendered as Markdown and returned as a single content
    /// item of kind `markdown`.
    fn hover(
        &self,
        path: &str,
        line: u32,
        column: u32,
    ) -> PyResult<Option<String>> {
        let guard = lock_state(&self.inner, "hover")?;
        let state = guard.as_ref().unwrap();

        let (file, source_str) = resolve_file_and_source(state, path)?;

        let pos = dto::PositionDto { line, column };
        let offset = coordinates::position_to_offset(&source_str, &pos)
            .map_err(|e| PositionError::new_err(e))?;

        let result = ty_ide::hover(&state.db, file, offset);

        match result {
            None => Ok(None),
            Some(hover_value) => {
                let file_range = hover_value.file_range();
                let file_path = file.path(&state.db).as_str().to_string();

                // Render the entire hover as Markdown
                let rendered = hover_value
                    .display(&state.db, ty_ide::MarkupKind::Markdown)
                    .to_string();

                let hover_dto = convert::hover::convert_hover_markdown(
                    &source_str,
                    file_path,
                    file_range,
                    rendered,
                );

                let json = serde_json::to_string(&hover_dto)
                    .map_err(|e| {
                        PyRuntimeError::new_err(format!(
                            "Serialisation failed: {}",
                            e
                        ))
                    })?;

                Ok(Some(json))
            }
        }
    }
}
