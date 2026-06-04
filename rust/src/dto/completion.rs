/// Completion kind enum — serialized to snake_case to match Python StrEnum.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CompletionKindDto {
    Text,
    Method,
    Function,
    Constructor,
    Field,
    Variable,
    Class,
    Interface,
    Module,
    Property,
    Unit,
    Value,
    Enum,
    Keyword,
    Snippet,
    Color,
    File,
    Reference,
    Folder,
    EnumMember,
    Constant,
    Struct,
    Event,
    Operator,
    TypeParameter,
}

/// A single completion suggestion — all fields are owned Strings.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct CompletionDto {
    pub name: String,
    pub qualified_name: Option<String>,
    pub insert_text: Option<String>,
    #[serde(rename = "type_")]
    pub type_: Option<String>,
    pub kind: Option<CompletionKindDto>,
    pub module_name: Option<String>,
}
