use pyo3::prelude::*;

mod convert;
mod dto;
mod errors;
mod project;
mod coordinates;
mod files;

// NOTE: The PyO3 module name determines the Python init symbol (PyInit__native_impl).
// The compiled .so is placed inside the tyo3 Python package as _native_impl.cpython-....so.
#[pymodule]
#[pyo3(name = "_native_impl")]
fn native_impl(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<project::PyTyProject>()?;
    Ok(())
}
