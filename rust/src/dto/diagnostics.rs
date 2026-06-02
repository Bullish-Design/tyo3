use crate::dto::RangeDto;

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SeverityDto {
    Fatal,
    Error,
    Warning,
    Information,
    Hint,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct DiagnosticDto {
    pub file: Option<String>,
    pub range: Option<RangeDto>,
    pub severity: SeverityDto,
    pub code: Option<String>,
    pub message: String,
    pub details: Vec<String>,
}
