use ruff_db::files::{self, File};
use ruff_db::Db;
use std::path::PathBuf;

/// Resolve a Python path to a ruff File. Accepts absolute paths
/// and project-relative paths.
pub fn resolve_file(
    db: &dyn Db,
    root: &std::path::Path,
    path_str: &str,
) -> Result<File, String> {
    let path = PathBuf::from(path_str);
    let absolute = if path.is_absolute() {
        path
    } else {
        root.join(&path)
    };

    // Normalise
    let canonical = absolute
        .canonicalize()
        .map_err(|e| format!("Cannot resolve path '{}': {}", path_str, e))?;

    // Convert to a SystemPath for ruff's API
    let system_path = ruff_db::system::SystemPath::from_std_path(&canonical)
        .ok_or_else(|| format!("Path contains non-UTF-8 characters: {}", path_str))?;

    // Use ruff's system_path_to_file to look up or register the file
    files::system_path_to_file(db, system_path)
        .map_err(|e| format!("File not in project: {} ({})", path_str, e))
}
