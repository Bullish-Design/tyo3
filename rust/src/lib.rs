use pyo3::prelude::*;
use pyo3::create_exception;

// ── Custom Exception Types ───────────────────────────────────────────────
// These become real Python exception classes importable from tyo3._native_impl.
// They permit Python-side error handling via `except _NativeClosedError` instead
// of fragile string-matching on exception messages.
create_exception!(tyo3._native_impl, ProjectClosedError, pyo3::exceptions::PyRuntimeError);
create_exception!(tyo3._native_impl, PathResolutionError, pyo3::exceptions::PyRuntimeError);
create_exception!(tyo3._native_impl, PositionError, pyo3::exceptions::PyRuntimeError);
create_exception!(tyo3._native_impl, AnalysisError, pyo3::exceptions::PyRuntimeError);

mod convert;
mod dto;
mod project;
mod coordinates;
mod files;

// NOTE: The PyO3 module name determines the Python init symbol (PyInit__native_impl).
// The compiled .so is placed inside the tyo3 Python package as _native_impl.cpython-....so.
#[pymodule]
#[pyo3(name = "_native_impl")]
fn native_impl(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // TyProject (the main project handle)
    m.add_class::<project::PyTyProject>()?;

    // DTO classes — native object transport layer
    m.add_class::<dto::PositionDto>()?;
    m.add_class::<dto::RangeDto>()?;
    m.add_class::<dto::FileRangeDto>()?;
    m.add_class::<dto::SymbolKindDto>()?;
    m.add_class::<dto::SymbolDto>()?;
    m.add_class::<dto::SeverityDto>()?;
    m.add_class::<dto::DiagnosticDto>()?;
    m.add_class::<dto::DefinitionTargetDto>()?;
    m.add_class::<dto::ReferenceKindDto>()?;
    m.add_class::<dto::ReferenceDto>()?;
    m.add_class::<dto::HoverContentKindDto>()?;
    m.add_class::<dto::HoverContentDto>()?;
    m.add_class::<dto::HoverDto>()?;
    m.add_class::<dto::CheckResultDto>()?;

    // Exception types
    m.add("ProjectClosedError", m.py().get_type::<ProjectClosedError>())?;
    m.add("PathResolutionError", m.py().get_type::<PathResolutionError>())?;
    m.add("PositionError", m.py().get_type::<PositionError>())?;
    m.add("AnalysisError", m.py().get_type::<AnalysisError>())?;
    Ok(())
}
