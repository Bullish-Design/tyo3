use crate::dto::RangeDto;

/// Diagnostic severity.  Serde `rename_all = "snake_case"` matches
/// Python-side `DiagnosticSeverity` StrEnum values.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SeverityDto {
    Fatal,
    Error,
    Warning,
    Information,
    Hint,
}

/// A type-checking diagnostic.  Serialized to Python dict via pythonize,
/// validated by `Diagnostic.model_validate()` on the Python side.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct DiagnosticDto {
    pub file: Option<String>,
    pub range: Option<RangeDto>,
    pub severity: SeverityDto,
    pub code: Option<String>,
    pub message: String,
    pub details: Vec<String>,
}
