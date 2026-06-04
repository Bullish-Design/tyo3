use crate::dto::FileRangeDto;

/// Hover content kind.  Serde `rename_all = "snake_case"` matches
/// Python-side `HoverContentKind` StrEnum values.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HoverContentKindDto {
    Type,
    Signature,
    Docstring,
    TypedDictKey,
    Markdown,
    PlainText,
}

/// A single piece of hover content.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct HoverContentDto {
    pub kind: HoverContentKindDto,
    pub value: String,
}

/// Structured hover information for a symbol location.
/// Extends ``FileRangeDto`` with content items.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct HoverDto {
    pub location: FileRangeDto,
    pub contents: Vec<HoverContentDto>,
}
