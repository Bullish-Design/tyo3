mod coordinates;
mod diagnostics;
mod symbols;
mod navigation;
mod hover;
mod tokens;
mod hierarchy;

pub use coordinates::*;
pub use diagnostics::*;
pub use symbols::*;
pub use navigation::*;
pub use hover::*;

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct BackendInfoDto {
    pub tyo3_version: String,
    pub ty_version: Option<String>,
    pub ty_commit: Option<String>,
    pub ruff_submodule_commit: Option<String>,
    pub backend_source: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct CheckResultDto {
    pub diagnostics: Vec<DiagnosticDto>,
    pub files_checked: Option<u32>,
    pub elapsed_ms: Option<u64>,
}
