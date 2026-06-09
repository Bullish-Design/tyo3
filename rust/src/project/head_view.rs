//! `PyHeadView`: floating warm fast-path view of live HEAD (Phase 9).
//!
//! Split from `project.rs` (Phase 13). Pulls shared imports + state
//! types from the parent module via `use super::*`.

use super::*;

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
pub(crate) fn read_head_with_retry<T: Send>(
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
    pub(crate) inner: Arc<Mutex<Option<HeadState>>>,
}

#[pymethods]
impl PyHeadView {
    // ── Files ────────────────────────────────────────────────────

    fn files(&self, py: Python<'_>) -> PyResult<Vec<String>> {
        read_head_with_retry(py, &self.inner, "files", compute_files)
    }

    // ── Check ────────────────────────────────────────────────────

    fn check<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let dto = read_head_with_retry(py, &self.inner, "check", compute_check)?;
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

    // PyO3 #[pymethod]: this positional signature is the Python-facing API, so
    // the range/diagnostic params can't be bundled without changing the binding.
    #[allow(clippy::too_many_arguments)]
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

