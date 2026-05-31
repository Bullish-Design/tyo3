use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

use ruff_db::source::source_text;
use ruff_db::system::{OsSystem, SystemPathBuf};

use ty_project::{Db as TyProjectDb, ProjectDatabase, ProjectMetadata};

use crate::coordinates;
use crate::dto;
use crate::files as file_resolver;

/// Internal mutable state of a TyO3 project session.
struct TyProjectState {
    db: ProjectDatabase,
    root: SystemPathBuf,
}

/// Python-facing wrapper around TyProjectState.
#[pyclass(name = "TyProject")]
pub struct PyTyProject {
    inner: Arc<Mutex<TyProjectState>>,
}

#[pymethods]
impl PyTyProject {
    /// Open a project at the given root directory.
    #[staticmethod]
    fn open(root: &str) -> PyResult<Self> {
        let root_path = PathBuf::from(root);
        let absolute = root_path.canonicalize().map_err(|e| {
            PyRuntimeError::new_err(format!("Cannot resolve root '{}': {}", root, e))
        })?;
        let s = absolute.to_str().ok_or_else(|| {
            PyRuntimeError::new_err(format!(
                "Path '{}' contains non-UTF-8 characters",
                absolute.display()
            ))
        })?;
        let system_root = SystemPathBuf::from(s);

        // Create the OS system and project metadata
        let system = OsSystem::new(system_root.clone());
        let metadata = ProjectMetadata::new(
            ruff_python_ast::name::Name::new("tyo3-project"),
            system_root.clone(),
        );

        // Create the database using the default configuration
        let db = ProjectDatabase::use_defaults(metadata, system);

        Ok(PyTyProject {
            inner: Arc::new(Mutex::new(TyProjectState {
                db,
                root: system_root,
            })),
        })
    }

    /// List all source files in the project.
    fn files(&self) -> PyResult<Vec<String>> {
        let state = self.inner.lock().map_err(|e| {
            PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
        })?;

        let project = state.db.project();
        let indexed = project.files(&state.db);
        let paths: Vec<String> = indexed
            .iter()
            .map(|f: &ruff_db::files::File| f.path(&state.db).as_str().to_string())
            .collect();

        Ok(paths)
    }

    /// Run the type checker on the entire project.
    fn check(&self) -> PyResult<String> {
        let state = self.inner.lock().map_err(|e| {
            PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
        })?;

        // Run ty's checker
        let result = state.db.check();

        // Convert diagnostics to DTOs
        let diagnostics: Vec<dto::DiagnosticDto> = result
            .iter()
            .map(|d| {
                let severity = match d.severity() {
                    ruff_db::diagnostic::Severity::Info => "information",
                    ruff_db::diagnostic::Severity::Warning => "warning",
                    ruff_db::diagnostic::Severity::Error => "error",
                    ruff_db::diagnostic::Severity::Fatal => "fatal",
                };

                dto::DiagnosticDto {
                    file: None,
                    range: None,
                    severity: severity.to_string(),
                    code: None,
                    message: d.primary_message().to_string(),
                    details: vec![],
                }
            })
            .collect();

        let check_result = dto::CheckResultDto {
            diagnostics,
            files_checked: None,
            elapsed_ms: None,
        };

        serde_json::to_string(&check_result)
            .map_err(|e| PyRuntimeError::new_err(format!("Serialisation failed: {}", e)))
    }

    /// Get document symbols for a file in the project.
    fn document_symbols(&self, path: &str) -> PyResult<String> {
        let state = self.inner.lock().map_err(|e| {
            PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
        })?;

        let file = file_resolver::resolve_file(
            &state.db,
            state.root.as_std_path(),
            path,
        )
        .map_err(|e| PyRuntimeError::new_err(e))?;

        // Get source text for coordinate conversion
        let src = source_text(&state.db, file);
        let source_str = src.as_str().to_string();

        // Call ty_ide::document_symbols
        let flat_symbols = ty_ide::document_symbols(&state.db, file);
        let hierarchical = flat_symbols.to_hierarchical();

        let mut symbols: Vec<dto::SymbolDto> = Vec::new();
        for (id, info) in hierarchical.iter() {
            let kind = info.kind.to_string().to_lowercase();

            let name_range = coordinates::range_to_dto(&source_str, info.name_range);
            let full_range = coordinates::range_to_dto(&source_str, info.full_range);

            let file_path = file.path(&state.db).as_str().to_string();

            // Add the parent symbol
            symbols.push(dto::SymbolDto {
                name: info.name.to_string(),
                qualified_name: None,
                kind,
                location: dto::FileRangeDto {
                    path: file_path.clone(),
                    range: name_range,
                },
                selection_range: Some(full_range),
                container_name: None,
                deprecated: info.deprecated,
            });

            // Add child symbols
            for (_child_id, child_info) in hierarchical.children(id) {
                let child_kind = child_info.kind.to_string().to_lowercase();
                let child_name_range =
                    coordinates::range_to_dto(&source_str, child_info.name_range);
                let child_full_range =
                    coordinates::range_to_dto(&source_str, child_info.full_range);

                symbols.push(dto::SymbolDto {
                    name: child_info.name.to_string(),
                    qualified_name: Some(format!("{}.{}", info.name, child_info.name)),
                    kind: child_kind,
                    location: dto::FileRangeDto {
                        path: file_path.clone(),
                        range: child_name_range,
                    },
                    selection_range: Some(child_full_range),
                    container_name: Some(info.name.to_string()),
                    deprecated: child_info.deprecated,
                });
            }
        }

        serde_json::to_string(&symbols)
            .map_err(|e| PyRuntimeError::new_err(format!("Serialisation failed: {}", e)))
    }
}
