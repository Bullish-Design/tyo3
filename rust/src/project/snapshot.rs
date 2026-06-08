//! `PySnapshot`: immutable, revision-pinned, thread-shareable read view.
//!
//! Split from `project.rs` (Phase 13). Pulls shared imports + state
//! types from the parent module via `use super::*`.

use super::*;

// ── Snapshot: immutable, revision-pinned, thread-shareable read view ──────────
//
// A `PySnapshot` owns its own clone of the project database, pinned to the
// revision at `snapshot()` time. Each read clones that frozen db under a brief
// lock and runs the analysis with the GIL released, so many threads can share
// one snapshot and run reads in parallel.

#[pyclass(name = "TySnapshot", module = "tyo3._native_impl", frozen)]
pub struct PySnapshot {
    pub(crate) inner: Mutex<Option<TyProjectState>>,
    /// The application revision this snapshot is pinned to (immutable).
    pub(crate) revision: u64,
    /// Validated config needed for derived status computation.
    pub(crate) config: ValidatedConfig,
    /// True if this snapshot was taken at the current HEAD (not time-travel).
    /// When true, authored reads may use the current (latest) value as a
    /// fallback after value_at — the session revision counter may not align
    /// with stored revisions across close/reopen cycles.
    pub(crate) is_head: bool,
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

