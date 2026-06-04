use std::collections::HashMap;

use ruff_db::files::File;
use ruff_db::Db;
use ruff_source_file::LineIndex;

use crate::coordinates;
use crate::dto::{RenameEditDto, WorkspaceEditDto};

/// Convert a Vec of ty_ide ReferenceTarget into a WorkspaceEditDto.
///
/// Caches `(source_text, LineIndex)` per file to avoid recomputing indices.
pub fn convert_rename_edits(
    db: &dyn Db,
    references: &[ty_ide::ReferenceTarget],
    new_name: &str,
) -> WorkspaceEditDto {
    let mut file_cache: HashMap<File, (String, LineIndex)> = HashMap::new();
    let edits: Vec<RenameEditDto> = references
        .iter()
        .map(|r| {
            let file = r.file();
            let (source_str, line_index) = file_cache
                .entry(file)
                .or_insert_with(|| {
                    let src = ruff_db::source::source_text(db, file);
                    let s = src.as_str().to_string();
                    let idx = LineIndex::from_source_text(&s);
                    (s, idx)
                });
            RenameEditDto {
                path: file.path(db).as_str().to_string(),
                range: coordinates::range_to_dto_with_index(
                    source_str,
                    line_index,
                    r.range(),
                ),
            }
        })
        .collect();

    WorkspaceEditDto {
        new_name: new_name.to_string(),
        edits,
    }
}
