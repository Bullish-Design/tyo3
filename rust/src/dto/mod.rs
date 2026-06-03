mod coordinates;
mod diagnostics;
mod symbols;
mod navigation;
mod hover;
mod hierarchy;
mod occurrences;
mod tokens;
pub use coordinates::*;
pub use diagnostics::*;
pub use hierarchy::*;
pub use occurrences::*;
pub use symbols::*;
pub use navigation::*;
pub use hover::*;
pub use tokens::*;

use pyo3::prelude::*;
use tyo3_derive::PyFields;

#[pyclass(name = "NativeCheckResult", frozen, from_py_object, module = "tyo3._native_impl")]
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PyFields)]
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
