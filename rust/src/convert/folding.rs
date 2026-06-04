use ruff_source_file::LineIndex;

use crate::coordinates;
use crate::dto::{FoldingRangeDto, FoldingRangeKindDto};

/// Convert a ty_ide FoldingRange into a FoldingRangeDto.
pub fn convert_folding_range(
    source_str: &str,
    line_index: &LineIndex,
    range: &ty_ide::FoldingRange,
) -> FoldingRangeDto {
    FoldingRangeDto {
        range: coordinates::range_to_dto_with_index(source_str, line_index, range.range),
        kind: range.kind.as_ref().map(|k| match k {
            ty_ide::FoldingRangeKind::Comment => FoldingRangeKindDto::Comment,
            ty_ide::FoldingRangeKind::Imports => FoldingRangeKindDto::Imports,
            ty_ide::FoldingRangeKind::Region => FoldingRangeKindDto::Region,
        }),
    }
}
