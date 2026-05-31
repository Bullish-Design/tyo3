use crate::dto::RangeDto;

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct DiagnosticDto {
    pub file: Option<String>,
    pub range: Option<RangeDto>,
    /// "fatal" | "error" | "warning" | "information" | "hint"
    pub severity: String,
    pub code: Option<String>,
    pub message: String,
    pub details: Vec<String>,
}
