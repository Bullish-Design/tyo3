use crate::dto::{FileRangeDto, RangeDto};

/// Symbol kind classification.  Serde `rename_all = "snake_case"` produces
/// values like `"function"`, `"class_"`, `"unknown"` matching Python-side
/// `SymbolKind` StrEnum values exactly.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
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

/// A code symbol.  Serialized to Python dict via pythonize, validated
/// by `Symbol.model_validate()` on the Python side.
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
