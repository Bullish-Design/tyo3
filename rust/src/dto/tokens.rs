use crate::dto::RangeDto;
use pyo3::prelude::*;
use tyo3_derive::PyFields;

/// Semantic token type classification.
#[pyclass(eq, name = "NativeSemanticTokenType", from_py_object, module = "tyo3._native_impl")]
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SemanticTokenTypeDto {
    Namespace,
    Class,
    Parameter,
    SelfParameter,
    ClsParameter,
    Variable,
    Property,
    Function,
    Method,
    Keyword,
    String,
    Number,
    Decorator,
    BuiltinConstant,
    TypeParameter,
}

#[pymethods]
impl SemanticTokenTypeDto {
    fn __str__(&self) -> &'static str {
        match self {
            SemanticTokenTypeDto::Namespace => "namespace",
            SemanticTokenTypeDto::Class => "class_",
            SemanticTokenTypeDto::Parameter => "parameter",
            SemanticTokenTypeDto::SelfParameter => "self_parameter",
            SemanticTokenTypeDto::ClsParameter => "cls_parameter",
            SemanticTokenTypeDto::Variable => "variable",
            SemanticTokenTypeDto::Property => "property",
            SemanticTokenTypeDto::Function => "function",
            SemanticTokenTypeDto::Method => "method",
            SemanticTokenTypeDto::Keyword => "keyword",
            SemanticTokenTypeDto::String => "string",
            SemanticTokenTypeDto::Number => "number",
            SemanticTokenTypeDto::Decorator => "decorator",
            SemanticTokenTypeDto::BuiltinConstant => "builtin_constant",
            SemanticTokenTypeDto::TypeParameter => "type_parameter",
        }
    }

    fn __repr__(&self) -> String {
        format!("NativeSemanticTokenType.{}", self.__str__())
    }
}

/// Semantic token modifier.
#[pyclass(eq, name = "NativeSemanticTokenModifier", from_py_object, module = "tyo3._native_impl")]
#[derive(Debug, Clone, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SemanticTokenModifierDto {
    Definition,
    Readonly,
    Async,
    Documentation,
}

#[pymethods]
impl SemanticTokenModifierDto {
    fn __str__(&self) -> &'static str {
        match self {
            SemanticTokenModifierDto::Definition => "definition",
            SemanticTokenModifierDto::Readonly => "readonly",
            SemanticTokenModifierDto::Async => "async_",
            SemanticTokenModifierDto::Documentation => "documentation",
        }
    }

    fn __repr__(&self) -> String {
        format!("NativeSemanticTokenModifier.{}", self.__str__())
    }
}

/// A single classified semantic token.
#[pyclass(name = "NativeSemanticToken", frozen, from_py_object, module = "tyo3._native_impl")]
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PyFields)]
pub struct SemanticTokenDto {
    #[pyo3(get)]
    pub range: RangeDto,
    #[pyo3(get)]
    pub token_type: SemanticTokenTypeDto,
    #[pyo3(get)]
    pub modifiers: Vec<SemanticTokenModifierDto>,
}
