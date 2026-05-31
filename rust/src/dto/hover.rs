use crate::dto::FileRangeDto;

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HoverContentKindDto {
    Type,
    Signature,
    Docstring,
    TypedDictKey,
    Markdown,
    PlainText,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct HoverContentDto {
    pub kind: HoverContentKindDto,
    pub value: String,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct HoverDto {
    pub location: FileRangeDto,
    pub contents: Vec<HoverContentDto>,
}
