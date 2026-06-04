use crate::dto::RangeDto;

/// Kind of folding range.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FoldingRangeKindDto {
    Comment,
    Imports,
    Region,
}

/// A folding range in the source code.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct FoldingRangeDto {
    pub range: RangeDto,
    /// The kind of folding range. None when the kind is not applicable.
    pub kind: Option<FoldingRangeKindDto>,
}
