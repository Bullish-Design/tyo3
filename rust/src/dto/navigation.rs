use crate::dto::{RangeDto, SymbolDto};

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct DefinitionTargetDto {
    pub path: String,
    pub range: RangeDto,
    pub selection_range: Option<RangeDto>,
    pub symbol: Option<SymbolDto>,
    pub module_name: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ReferenceDto {
    pub path: String,
    pub range: RangeDto,
    /// "read" | "write" | "other"
    pub kind: String,
}
