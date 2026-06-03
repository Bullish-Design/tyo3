use crate::dto::{FileRangeDto, RangeDto};
use pyo3::prelude::*;
use tyo3_derive::PyFields;

#[pyclass(eq, name = "NativeSymbolKind", from_py_object, module = "tyo3._native_impl")]
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

#[pymethods]
impl SymbolKindDto {
    fn __str__(&self) -> &'static str {
        match self {
            SymbolKindDto::Module => "module",
            SymbolKindDto::Class => "class_",
            SymbolKindDto::Function => "function",
            SymbolKindDto::Method => "method",
            SymbolKindDto::Constructor => "constructor",
            SymbolKindDto::Variable => "variable",
            SymbolKindDto::Constant => "constant",
            SymbolKindDto::Field => "field",
            SymbolKindDto::Parameter => "parameter",
            SymbolKindDto::Property => "property",
            SymbolKindDto::TypeParameter => "type_parameter",
            SymbolKindDto::Import => "import_",
            SymbolKindDto::Unknown => "unknown",
        }
    }

    fn __repr__(&self) -> String {
        format!("NativeSymbolKind.{}", self.__str__())
    }
}

#[pyclass(name = "NativeSymbol", frozen, from_py_object, module = "tyo3._native_impl")]
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PyFields)]
pub struct SymbolDto {
    #[pyo3(get)]
    pub name: String,
    #[pyo3(get)]
    pub qualified_name: Option<String>,
    #[pyo3(get)]
    pub kind: SymbolKindDto,
    #[pyo3(get)]
    pub location: FileRangeDto,
    #[pyo3(get)]
    pub selection_range: Option<RangeDto>,
    #[pyo3(get)]
    pub container_name: Option<String>,
    #[pyo3(get)]
    pub deprecated: bool,
}

#[pymethods]
impl SymbolDto {
    #[new]
    #[pyo3(signature = (name, kind, location, selection_range=None, qualified_name=None, container_name=None, deprecated=false))]
    fn new(
        name: String,
        kind: SymbolKindDto,
        location: FileRangeDto,
        selection_range: Option<RangeDto>,
        qualified_name: Option<String>,
        container_name: Option<String>,
        deprecated: bool,
    ) -> Self {
        SymbolDto {
            name,
            qualified_name,
            kind,
            location,
            selection_range,
            container_name,
            deprecated,
        }
    }

    fn __repr__(&self) -> String {
        format!(
            "NativeSymbol(name='{}', kind={}, location={:?})",
            self.name,
            self.kind.__str__(),
            self.location
        )
    }
}
