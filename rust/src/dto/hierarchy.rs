use crate::dto::RangeDto;
use pyo3::prelude::*;
use tyo3_derive::PyFields;

/// A single item in a type hierarchy (a class in the hierarchy tree).
#[pyclass(name = "NativeTypeHierarchyItem", frozen, from_py_object, module = "tyo3._native_impl")]
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PyFields)]
pub struct TypeHierarchyItemDto {
    #[pyo3(get)]
    pub name: String,
    #[pyo3(get)]
    pub detail: Option<String>,
    #[pyo3(get)]
    pub path: String,
    #[pyo3(get)]
    pub full_range: RangeDto,
    #[pyo3(get)]
    pub selection_range: RangeDto,
}

/// Result of a type hierarchy query.
#[pyclass(name = "NativeTypeHierarchy", frozen, from_py_object, module = "tyo3._native_impl")]
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PyFields)]
pub struct TypeHierarchyDto {
    #[pyo3(get)]
    pub item: TypeHierarchyItemDto,
    #[pyo3(get)]
    pub supertypes: Vec<TypeHierarchyItemDto>,
    #[pyo3(get)]
    pub subtypes: Vec<TypeHierarchyItemDto>,
}
