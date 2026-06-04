use ruff_source_file::LineIndex;

use crate::coordinates;
use crate::dto::{HintDto, HintKindDto};

/// Convert a ty_ide Hint to an owned HintDto.
pub fn convert_hint(
    source_str: &str,
    line_index: &LineIndex,
    hint: &ty_ide::Hint,
) -> HintDto {
    let kind = match hint.kind {
        ty_ide::HintKind::UnusedBinding(_) => HintKindDto::Unused,
        ty_ide::HintKind::UnreachableCode(_) => HintKindDto::Unreachable,
    };
    HintDto {
        message: hint.message(),
        kind,
        range: Some(coordinates::range_to_dto_with_index(
            source_str,
            line_index,
            hint.range,
        )),
    }
}
