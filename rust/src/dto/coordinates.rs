use pyo3::prelude::*;
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};

#[pyclass(name = "NativePosition", frozen, module = "tyo3._native_impl")]
#[derive(Debug, Clone, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub struct PositionDto {
    /// 1-based line number
    #[pyo3(get)]
    pub line: u32,
    /// 1-based column (Unicode codepoints)
    #[pyo3(get)]
    pub column: u32,
}

#[pymethods]
impl PositionDto {
    #[new]
    fn new(line: u32, column: u32) -> Self {
        PositionDto { line, column }
    }

    fn __repr__(&self) -> String {
        format!("NativePosition(line={}, column={})", self.line, self.column)
    }

    fn __eq__(&self, other: PyRef<'_, Self>) -> bool {
        *self == *other
    }

    fn __hash__(&self) -> u64 {
        let mut hasher = DefaultHasher::new();
        self.hash(&mut hasher);
        hasher.finish()
    }
}

#[pyclass(name = "NativeRange", frozen, module = "tyo3._native_impl")]
#[derive(Debug, Clone, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub struct RangeDto {
    #[pyo3(get)]
    pub start: PositionDto,
    #[pyo3(get)]
    pub end: PositionDto,
}

#[pymethods]
impl RangeDto {
    #[new]
    fn new(start: PositionDto, end: PositionDto) -> Self {
        RangeDto { start, end }
    }

    fn __repr__(&self) -> String {
        format!(
            "NativeRange(start=NativePosition(line={}, column={}), end=NativePosition(line={}, column={}))",
            self.start.line, self.start.column, self.end.line, self.end.column
        )
    }

    fn __eq__(&self, other: PyRef<'_, Self>) -> bool {
        *self == *other
    }

    fn __hash__(&self) -> u64 {
        let mut hasher = DefaultHasher::new();
        self.hash(&mut hasher);
        hasher.finish()
    }
}

#[pyclass(name = "NativeFileRange", frozen, module = "tyo3._native_impl")]
#[derive(Debug, Clone, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub struct FileRangeDto {
    #[pyo3(get)]
    pub path: String,
    #[pyo3(get)]
    pub range: RangeDto,
}

#[pymethods]
impl FileRangeDto {
    #[new]
    fn new(path: String, range: RangeDto) -> Self {
        FileRangeDto { path, range }
    }

    fn __repr__(&self) -> String {
        format!("NativeFileRange(path='{}', range={:?})", self.path, self.range)
    }

    fn __eq__(&self, other: PyRef<'_, Self>) -> bool {
        *self == *other
    }

    fn __hash__(&self) -> u64 {
        let mut hasher = DefaultHasher::new();
        self.hash(&mut hasher);
        hasher.finish()
    }
}
