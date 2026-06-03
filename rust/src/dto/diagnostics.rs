use crate::dto::RangeDto;
use pyo3::prelude::*;

#[pyclass(eq, name = "NativeSeverity", module = "tyo3._native_impl")]
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SeverityDto {
    Fatal,
    Error,
    Warning,
    Information,
    Hint,
}

#[pymethods]
impl SeverityDto {
    fn __str__(&self) -> &'static str {
        match self {
            SeverityDto::Fatal => "fatal",
            SeverityDto::Error => "error",
            SeverityDto::Warning => "warning",
            SeverityDto::Information => "information",
            SeverityDto::Hint => "hint",
        }
    }

    fn __repr__(&self) -> String {
        format!("NativeSeverity.{}", self.__str__())
    }
}

#[pyclass(name = "NativeDiagnostic", frozen, module = "tyo3._native_impl")]
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct DiagnosticDto {
    #[pyo3(get)]
    pub file: Option<String>,
    #[pyo3(get)]
    pub range: Option<RangeDto>,
    #[pyo3(get)]
    pub severity: SeverityDto,
    #[pyo3(get)]
    pub code: Option<String>,
    #[pyo3(get)]
    pub message: String,
    #[pyo3(get)]
    pub details: Vec<String>,
}

#[pymethods]
impl DiagnosticDto {
    #[new]
    #[pyo3(signature = (severity, message, file=None, range=None, code=None, details=None))]
    fn new(
        severity: SeverityDto,
        message: String,
        file: Option<String>,
        range: Option<RangeDto>,
        code: Option<String>,
        details: Option<Vec<String>>,
    ) -> Self {
        DiagnosticDto {
            file,
            range,
            severity,
            code,
            message,
            details: details.unwrap_or_default(),
        }
    }

    fn __repr__(&self) -> String {
        if let Some(ref code) = self.code {
            format!(
                "NativeDiagnostic(severity={}, code='{}', message='{}')",
                self.severity.__str__(),
                code,
                self.message
            )
        } else {
            format!(
                "NativeDiagnostic(severity={}, message='{}')",
                self.severity.__str__(),
                self.message
            )
        }
    }
}
