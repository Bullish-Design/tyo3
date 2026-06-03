mod coordinates;
mod diagnostics;
mod symbols;
mod navigation;
mod hover;
pub use coordinates::*;
pub use diagnostics::*;
pub use symbols::*;
pub use navigation::*;
pub use hover::*;

use pyo3::prelude::*;

// Spec-anticipation: not yet wired to a Python-accessible endpoint.
// Kept for the spec shape; will be promoted when a backend_info() method exists.
#[allow(dead_code)]
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct BackendInfoDto {
    pub tyo3_version: String,
    pub ty_version: Option<String>,
    pub ty_commit: Option<String>,
    pub ruff_submodule_commit: Option<String>,
    pub backend_source: Option<String>,
}

#[pyclass(name = "NativeCheckResult", frozen, module = "tyo3._native_impl")]
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct CheckResultDto {
    #[pyo3(get)]
    pub diagnostics: Vec<DiagnosticDto>,
    #[pyo3(get)]
    pub files_checked: Option<u32>,
    #[pyo3(get)]
    pub elapsed_ms: Option<u64>,
}

#[pymethods]
impl CheckResultDto {
    #[new]
    #[pyo3(signature = (diagnostics, files_checked=None, elapsed_ms=None))]
    fn new(
        diagnostics: Vec<DiagnosticDto>,
        files_checked: Option<u32>,
        elapsed_ms: Option<u64>,
    ) -> Self {
        CheckResultDto {
            diagnostics,
            files_checked,
            elapsed_ms,
        }
    }

    fn __repr__(&self) -> String {
        format!(
            "NativeCheckResult(diagnostics={} items, files_checked={:?}, elapsed_ms={:?})",
            self.diagnostics.len(),
            self.files_checked,
            self.elapsed_ms
        )
    }
}
