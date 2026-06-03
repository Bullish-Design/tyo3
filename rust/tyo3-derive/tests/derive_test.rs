use pyo3::prelude::*;
use tyo3_derive::PyFields;

#[pyclass(frozen)]
#[derive(PyFields)]
struct TestStruct {
    #[pyo3(get)]
    pub name: String,
    #[pyo3(get)]
    pub count: u32,
    #[pyo3(get)]
    pub tags: Vec<String>,
    #[pyo3(get)]
    pub parent: Option<String>,
    // No #[pyo3(get)] — should be excluded
    pub internal: bool,
}

#[test]
fn test_fields_generated() {
    Python::with_gil(|_py| {
        let fields = TestStruct::__fields__();
        assert_eq!(
            fields,
            vec![
                ("name", "str"),
                ("count", "int"),
                ("tags", "list:str"),
                ("parent", "opt:str"),
            ]
        );
    });
}

#[pyclass(frozen)]
#[derive(PyFields)]
struct AllPrimitives {
    #[pyo3(get)]
    pub s: String,
    #[pyo3(get)]
    pub b: bool,
    #[pyo3(get)]
    pub u8_val: u8,
    #[pyo3(get)]
    pub u16_val: u16,
    #[pyo3(get)]
    pub u32_val: u32,
    #[pyo3(get)]
    pub u64_val: u64,
    #[pyo3(get)]
    pub i32_val: i32,
    #[pyo3(get)]
    pub i64_val: i64,
    #[pyo3(get)]
    pub f32_val: f32,
    #[pyo3(get)]
    pub f64_val: f64,
}

#[test]
fn test_primitives_all_passthrough() {
    Python::with_gil(|_py| {
        let fields = AllPrimitives::__fields__();
        let tags: Vec<&str> = fields.iter().map(|(_, t)| *t).collect();
        assert_eq!(
            tags,
            vec!["str", "bool", "int", "int", "int", "int", "int", "int", "float", "float"]
        );
    });
}
