mod coordinates;
mod diagnostics;
mod symbols;
mod navigation;
mod hover;
mod hierarchy;
mod occurrences;
mod tokens;
pub use coordinates::*;
pub use diagnostics::*;
pub use hierarchy::*;
pub use occurrences::*;
pub use symbols::*;
pub use navigation::*;
pub use hover::*;
pub use tokens::*;

/// Result of a project check.  Serialized to a Python dict via pythonize.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct CheckResultDto {
    pub diagnostics: Vec<DiagnosticDto>,
    pub files_checked: Option<u32>,
    pub elapsed_ms: Option<u64>,
}
