use ruff_text_size::Ranged;

use crate::coordinates;
use crate::dto::{FileRangeDto, HoverContentDto, HoverContentKindDto, HoverDto};

/// Build a HoverDto from pre-extracted parts.
///
/// Because ty_ide does not publicly export its `Hover` and `HoverContent`
/// types, the project.rs hover method must extract content items from the
/// `RangedValue<Hover<'_>>` returned by `ty_ide::hover()` and pass them
/// here as `(kind, value)` pairs.
///
/// Content kinds use our own `HoverContentKindDto` enum which maps directly
/// to the upstream variants.
pub fn convert_hover(
    source: &str,
    path: String,
    range: ruff_db::files::FileRange,
    content_items: Vec<(HoverContentKindDto, String)>,
) -> HoverDto {
    let contents: Vec<HoverContentDto> = content_items
        .into_iter()
        .map(|(kind, value)| HoverContentDto { kind, value })
        .collect();

    HoverDto {
        location: FileRangeDto {
            path,
            range: coordinates::range_to_dto(source, range.range()),
        },
        contents,
    }
}

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
