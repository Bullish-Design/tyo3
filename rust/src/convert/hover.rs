use ruff_text_size::Ranged;

use crate::coordinates;
use crate::dto::{FileRangeDto, HoverContentDto, HoverContentKindDto, HoverDto};

/// Build a HoverDto from a single rendered Markdown string.
///
/// Used when the entire hover is rendered as Markdown (via
/// `hover_value.display(db, MarkupKind::Markdown)`) because the upstream
/// `Hover`/`HoverContent` types are not publicly re-exported from ty_ide.
pub fn convert_hover_markdown(
    source: &str,
    path: String,
    range: ruff_db::files::FileRange,
    markdown: String,
) -> HoverDto {
    HoverDto {
        location: FileRangeDto {
            path,
            range: coordinates::range_to_dto(source, range.range()),
        },
        contents: vec![HoverContentDto {
            kind: HoverContentKindDto::Markdown,
            value: markdown,
        }],
    }
}
