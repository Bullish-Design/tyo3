use crate::dto::RangeDto;
use pyo3::prelude::*;

/// Reference role classification matching Python-side ReferenceRole.
#[pyclass(eq, name = "NativeReferenceRole", module = "tyo3._native_impl")]
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub enum ReferenceRoleDto {
    Read,
    Write,
    Import,
    Definition,
    Other,
}

#[pymethods]
impl ReferenceRoleDto {
    fn __str__(&self) -> &'static str {
        match self {
            ReferenceRoleDto::Read => "Read",
            ReferenceRoleDto::Write => "Write",
            ReferenceRoleDto::Import => "Import",
            ReferenceRoleDto::Definition => "Definition",
            ReferenceRoleDto::Other => "Other",
        }
    }

    fn __repr__(&self) -> String {
        format!("NativeReferenceRole.{}", self.__str__())
    }
}

/// A single resolved name occurrence in a file.
///
/// Each occurrence records where a name appears (range), what symbol it
/// resolves to (target_file / target_name), and the reference role
/// (read, write, import, or definition).
#[pyclass(name = "NativeNameOccurrence", frozen, module = "tyo3._native_impl")]
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct NameOccurrenceDto {
    /// The source range where this name appears.
    #[pyo3(get)]
    pub range: RangeDto,
    /// The file path of the symbol this name refers to (None if unresolved).
    #[pyo3(get)]
    pub target_file: Option<String>,
    /// The short name of the referenced symbol (None if unresolved).
    #[pyo3(get)]
    pub target_name: Option<String>,
    /// How this name is used: read, write, import, definition, or other.
    #[pyo3(get)]
    pub role: ReferenceRoleDto,
}
