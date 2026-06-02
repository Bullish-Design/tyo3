use crate::dto::{FileRangeDto, RangeDto};

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SymbolKindDto {
    Module,
    #[serde(rename = "class_")]
    Class,
    Function,
    Method,
    Constructor,
    Variable,
    Constant,
    Field,
    Parameter,
    Property,
    TypeParameter,
    #[serde(rename = "import_")]
    Import,
    Unknown,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SymbolDto {
    pub name: String,
    pub qualified_name: Option<String>,
    pub kind: SymbolKindDto,
    pub location: FileRangeDto,
    pub selection_range: Option<RangeDto>,
    pub container_name: Option<String>,
    pub deprecated: bool,
}
