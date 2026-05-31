use pyo3::prelude::*;

mod dto;
mod project;
mod coordinates;
mod files;

// NOTE: The PyO3 module name MUST match the maturin module-name in pyproject.toml
// and the Python import path used in rust_project.py.
// All three must agree: "rust_backend"
#[pymodule]
#[pyo3(name = "rust_backend")]
fn rust_backend(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<project::PyTyProject>()?;
    Ok(())
}
