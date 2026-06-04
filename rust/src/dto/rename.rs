use crate::dto::RangeDto;

/// A single rename edit for one file location.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct RenameEditDto {
    pub path: String,
    pub range: RangeDto,
}

/// The complete workspace edit for a rename operation.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct WorkspaceEditDto {
    pub new_name: String,
    pub edits: Vec<RenameEditDto>,
}
