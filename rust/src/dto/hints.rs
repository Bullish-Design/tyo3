use crate::dto::{PositionDto, RangeDto};

/// Kind of inlay hint.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InlayHintKindDto {
    Type,
    CallArgumentName,
}

/// An inlay hint to display in the editor.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct InlayHintDto {
    pub position: PositionDto,
    pub label: String,
    pub kind: InlayHintKindDto,
}

/// Kind of hint (unused binding, unreachable code).
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HintKindDto {
    #[serde(rename = "unused")]
    Unused,
    #[serde(rename = "unreachable")]
    Unreachable,
}

/// A hint about the source code (unused bindings, unreachable code).
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct HintDto {
    pub message: String,
    pub kind: HintKindDto,
    pub range: Option<RangeDto>,
}
