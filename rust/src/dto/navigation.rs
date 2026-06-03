use crate::dto::{RangeDto, SymbolDto};
use pyo3::prelude::*;

#[pyclass(name = "NativeDefinitionTarget", frozen, module = "tyo3._native_impl")]
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct DefinitionTargetDto {
    #[pyo3(get)]
    pub path: String,
    #[pyo3(get)]
    pub range: RangeDto,
    #[pyo3(get)]
    pub selection_range: Option<RangeDto>,
    #[pyo3(get)]
    pub symbol: Option<SymbolDto>,
    #[pyo3(get)]
    pub module_name: Option<String>,
}

#[pymethods]
impl DefinitionTargetDto {
    #[new]
    #[pyo3(signature = (path, range, selection_range=None, symbol=None, module_name=None))]
    fn new(
        path: String,
        range: RangeDto,
        selection_range: Option<RangeDto>,
        symbol: Option<SymbolDto>,
        module_name: Option<String>,
    ) -> Self {
        DefinitionTargetDto {
            path,
            range,
            selection_range,
            symbol,
            module_name,
        }
    }

    fn __repr__(&self) -> String {
        format!(
            "NativeDefinitionTarget(path='{}', range={:?})",
            self.path, self.range
        )
    }
}

#[pyclass(eq, name = "NativeReferenceKind", module = "tyo3._native_impl")]
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReferenceKindDto {
    Read,
    Write,
    Other,
}

#[pymethods]
impl ReferenceKindDto {
    fn __str__(&self) -> &'static str {
        match self {
            ReferenceKindDto::Read => "read",
            ReferenceKindDto::Write => "write",
            ReferenceKindDto::Other => "other",
        }
    }

    fn __repr__(&self) -> String {
        format!("NativeReferenceKind.{}", self.__str__())
    }
}

#[pyclass(name = "NativeReference", frozen, module = "tyo3._native_impl")]
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ReferenceDto {
    #[pyo3(get)]
    pub path: String,
    #[pyo3(get)]
    pub range: RangeDto,
    #[pyo3(get)]
    pub kind: ReferenceKindDto,
}

#[pymethods]
impl ReferenceDto {
    #[new]
    fn new(path: String, range: RangeDto, kind: ReferenceKindDto) -> Self {
        ReferenceDto { path, range, kind }
    }

    fn __repr__(&self) -> String {
        format!(
            "NativeReference(path='{}', kind={}, range={:?})",
            self.path,
            self.kind.__str__(),
            self.range
        )
    }
}
