use crate::dto::RangeDto;

/// Reference role classification matching Python-side `ReferenceRole` StrEnum.
/// Serde `rename_all = "snake_case"` makes `Definition` → `"definition"` etc.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReferenceRoleDto {
    Read,
    Write,
    Import,
    Definition,
    Other,
}

/// A single resolved name occurrence in a file.
///
/// Each occurrence records where a name appears (range), the source token
/// text (name), what symbol it resolves to (target_file / target_name /
/// target_qualified_name), and the reference role (read, write, import,
/// or definition).
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct NameOccurrenceDto {
    pub range: RangeDto,
    /// The source token text (e.g. "User", "save", "create_user").
    /// Never None for a real token.
    pub name: Option<String>,
    pub target_file: Option<String>,
    pub target_name: Option<String>,
    pub target_qualified_name: Option<String>,
    pub role: ReferenceRoleDto,
}
