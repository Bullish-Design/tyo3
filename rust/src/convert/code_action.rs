use ruff_source_file::LineIndex;
use ruff_text_size::Ranged;

use crate::coordinates;
use crate::dto::{QuickFixDto, TextEditDto};

/// Convert a ty_ide QuickFix to a QuickFixDto.
/// `Edit` is from ruff_diagnostics; we access it via `Ranged` trait and `.content()`.
pub fn convert_quick_fix(
    source_str: &str,
    line_index: &LineIndex,
    file_path: &str,
    fix: &ty_ide::QuickFix,
) -> QuickFixDto {
    let edits: Vec<TextEditDto> = fix
        .edits
        .iter()
        .map(|edit| {
            let range = edit.range();
            TextEditDto {
                path: file_path.to_string(),
                range: coordinates::range_to_dto_with_index(source_str, line_index, range),
                new_text: edit.content().unwrap_or("").to_string(),
            }
        })
        .collect();

    QuickFixDto {
        title: fix.title.clone(),
        edits,
    }
}
