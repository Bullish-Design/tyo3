use crate::dto::RangeDto;

/// A text edit to be applied to a source file.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct TextEditDto {
    pub path: String,
    pub range: RangeDto,
    pub new_text: String,
}

/// A quick fix suggestion for a diagnostic.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct QuickFixDto {
    pub title: String,
    pub edits: Vec<TextEditDto>,
}
