use crate::dto::RangeDto;

/// Semantic token type classification.
/// Serde `rename_all = "snake_case"` matches Python `SemanticTokenType` StrEnum values.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SemanticTokenTypeDto {
    Namespace,
    #[serde(rename = "class_")]
    Class,
    Parameter,
    #[serde(rename = "self_parameter")]
    SelfParameter,
    #[serde(rename = "cls_parameter")]
    ClsParameter,
    Variable,
    Property,
    Function,
    Method,
    Keyword,
    String,
    Number,
    Decorator,
    #[serde(rename = "builtin_constant")]
    BuiltinConstant,
    TypeParameter,
}

/// Semantic token modifier.
#[derive(Debug, Clone, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SemanticTokenModifierDto {
    Definition,
    Readonly,
    #[serde(rename = "async_")]
    Async,
    Documentation,
}

/// A single classified semantic token.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SemanticTokenDto {
    pub range: RangeDto,
    pub token_type: SemanticTokenTypeDto,
    pub modifiers: Vec<SemanticTokenModifierDto>,
}
