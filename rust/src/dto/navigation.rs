use crate::dto::{RangeDto, SymbolDto};

/// A navigation target produced by goto-definition-like operations.
/// Serialized to Python dict via pythonize, validated by
/// `DefinitionTarget.model_validate()` on the Python side.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct DefinitionTargetDto {
    pub path: String,
    pub range: RangeDto,
    pub selection_range: Option<RangeDto>,
    pub symbol: Option<SymbolDto>,
    pub module_name: Option<String>,
}

/// Reference kind classification.  Serde `rename_all = "snake_case"` matches
/// Python-side `ReferenceKind` StrEnum values.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReferenceKindDto {
    Read,
    Write,
    Other,
}

/// A reference occurrence within a project file.
/// Serialized to Python dict via pythonize.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ReferenceDto {
    pub path: String,
    pub range: RangeDto,
    pub kind: ReferenceKindDto,
}
