mod coordinates;
mod diagnostics;
mod symbols;
mod navigation;
mod hover;
mod hierarchy;
mod occurrences;
mod tokens;
mod rename;
mod folding;
mod signature;
mod completion;
mod hints;
mod code_action;
mod sync;
pub use coordinates::*;
pub use diagnostics::*;
pub use hierarchy::*;
pub use occurrences::*;
pub use symbols::*;
pub use navigation::*;
pub use hover::*;
pub use tokens::*;
pub use rename::*;
pub use folding::*;
pub use signature::*;
pub use completion::*;
pub use hints::*;
pub use code_action::*;
pub use sync::*;

/// Result of a project check.  Serialized to a Python dict via pythonize.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct CheckResultDto {
    pub diagnostics: Vec<DiagnosticDto>,
    pub files_checked: Option<u32>,
    pub elapsed_ms: Option<u64>,
}
