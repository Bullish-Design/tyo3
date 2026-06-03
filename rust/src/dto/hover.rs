use crate::dto::FileRangeDto;
use pyo3::prelude::*;

#[pyclass(eq, name = "NativeHoverContentKind", module = "tyo3._native_impl")]
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

#[pymethods]
impl HoverContentKindDto {
    fn __str__(&self) -> &'static str {
        match self {
            HoverContentKindDto::Type => "type",
            HoverContentKindDto::Signature => "signature",
            HoverContentKindDto::Docstring => "docstring",
            HoverContentKindDto::TypedDictKey => "typed_dict_key",
            HoverContentKindDto::Markdown => "markdown",
            HoverContentKindDto::PlainText => "plain_text",
        }
    }

    fn __repr__(&self) -> String {
        format!("NativeHoverContentKind.{}", self.__str__())
    }
}

#[pyclass(name = "NativeHoverContent", frozen, module = "tyo3._native_impl")]
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct HoverContentDto {
    #[pyo3(get)]
    pub kind: HoverContentKindDto,
    #[pyo3(get)]
    pub value: String,
}

#[pymethods]
impl HoverContentDto {
    #[new]
    fn new(kind: HoverContentKindDto, value: String) -> Self {
        HoverContentDto { kind, value }
    }

    fn __repr__(&self) -> String {
        format!(
            "NativeHoverContent(kind={}, value='{}')",
            self.kind.__str__(),
            self.value
        )
    }
}

#[pyclass(name = "NativeHover", frozen, module = "tyo3._native_impl")]
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct HoverDto {
    #[pyo3(get)]
    pub location: FileRangeDto,
    #[pyo3(get)]
    pub contents: Vec<HoverContentDto>,
}

#[pymethods]
impl HoverDto {
    #[new]
    fn new(location: FileRangeDto, contents: Vec<HoverContentDto>) -> Self {
        HoverDto { location, contents }
    }

    fn __repr__(&self) -> String {
        format!(
            "NativeHover(location={:?}, contents={} items)",
            self.location,
            self.contents.len()
        )
    }
}
