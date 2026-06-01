/// TyO3 error types for converting Rust errors into Python exceptions.
///
/// These are simple string-based error types that get mapped to Python
/// exceptions in the PyO3 boundary (see `project.rs` methods).

/// Error returned when a path cannot be resolved or is invalid.
#[derive(Debug, Clone)]
pub struct PathError {
    pub message: String,
}

impl std::fmt::Display for PathError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Path error: {}", self.message)
    }
}

impl std::error::Error for PathError {}

/// Error returned when a position (line/column) is invalid.
#[derive(Debug, Clone)]
pub struct PositionError {
    pub message: String,
}

impl std::fmt::Display for PositionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Position error: {}", self.message)
    }
}

impl std::error::Error for PositionError {}

/// Error returned when a project operation fails.
#[derive(Debug, Clone)]
pub struct ProjectError {
    pub message: String,
}

impl std::fmt::Display for ProjectError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Project error: {}", self.message)
    }
}

impl std::error::Error for ProjectError {}
