use ruff_source_file::LineIndex;
use ruff_text_size::Ranged;

use crate::coordinates;
use crate::dto::{FileRangeDto, HoverContentDto, HoverContentKindDto, HoverDto};

/// Build a HoverDto from a single rendered Markdown string,
/// using a pre-computed [`LineIndex`] to avoid recomputing line/column
/// positions when the caller already has one.
pub fn convert_hover_markdown_with_index(
    source: &str,
    line_index: &LineIndex,
    path: String,
    range: ruff_db::files::FileRange,
    markdown: String,
) -> HoverDto {
    HoverDto {
        location: FileRangeDto {
            path,
            range: coordinates::range_to_dto_with_index(source, line_index, range.range()),
        },
        contents: vec![HoverContentDto {
            kind: HoverContentKindDto::Markdown,
            value: markdown,
        }],
    }
}

/// Build a HoverDto from a single rendered Markdown string.
///
/// Convenience wrapper around [`convert_hover_markdown_with_index`] that
/// computes a `LineIndex` internally.
pub fn convert_hover_markdown(
    source: &str,
    path: String,
    range: ruff_db::files::FileRange,
    markdown: String,
) -> HoverDto {
    let line_index = LineIndex::from_source_text(source);
    convert_hover_markdown_with_index(source, &line_index, path, range, markdown)
}
