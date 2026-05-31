use crate::dto::{FileRangeDto, RangeDto};

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SymbolDto {
    pub name: String,
    pub qualified_name: Option<String>,
    /// Matches SymbolKind enum in Python
    pub kind: String,
    pub location: FileRangeDto,
    pub selection_range: Option<RangeDto>,
    pub container_name: Option<String>,
    pub deprecated: bool,
}
