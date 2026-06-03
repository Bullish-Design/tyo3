use ruff_source_file::{LineIndex, OneIndexed, PositionEncoding};
use ruff_text_size::TextSize;

use crate::dto::{PositionDto, RangeDto};

/// Convert a 1-based Python Position to a ruff TextSize byte offset.
///
/// Convenience wrapper around [`position_to_offset_with_index`] that
/// computes a `LineIndex` internally.
pub fn position_to_offset(
    source: &str,
    pos: &PositionDto,
) -> Result<TextSize, String> {
    let line_index = LineIndex::from_source_text(source);
    position_to_offset_with_index(source, &line_index, pos.line, pos.column)
}

/// Convert a 1-based Python Position to a ruff TextSize byte offset,
/// using a pre-computed [`LineIndex`] (avoids recomputing for multiple
/// positions in the same file).
///
/// Returns an error if:
/// - line or column is < 1
/// - line exceeds the file's total number of lines
/// - column exceeds the line's length
pub fn position_to_offset_with_index(
    source: &str,
    line_index: &LineIndex,
    line: u32,
    column: u32,
) -> Result<TextSize, String> {
    let line_idx = line
        .checked_sub(1)
        .ok_or_else(|| "Line must be >= 1".to_string())? as usize;
    let col = column
        .checked_sub(1)
        .ok_or_else(|| "Column must be >= 1".to_string())? as usize;

    // Validate line is within file bounds
    let total_lines = line_index.line_count();
    if line_idx >= total_lines {
        return Err(format!(
            "Line {} exceeds file length ({} lines)",
            line, total_lines
        ));
    }

    let line_start = line_index.line_start(OneIndexed::from_zero_indexed(line_idx), source);

    // Convert column (Unicode codepoints) to byte offset
    let line_start_usize: usize = line_start.to_usize();
    let line_text = &source[line_start_usize..];
    let byte_col = char_len_to_byte_offset(line_text, col);

    // Validate column does not exceed the line
    if usize::from(byte_col) > line_text.len() {
        return Err(format!(
            "Column {} exceeds line {} length ({} bytes)",
            column,
            line,
            line_text.len()
        ));
    }

    Ok(line_start + byte_col)
}

/// Count characters from the start of the line text and return the byte offset.
/// For v0.1, this handles ASCII and multi-byte UTF-8.
/// A full implementation should use `unicode-width` for grapheme clusters.
///
/// ASSUMPTION: Input is always 1-based (Python default). If a future
/// `CoordinateMode` ("rust" = 0-based) is added, the caller must convert
/// before calling this function.
fn char_len_to_byte_offset(text: &str, char_offset: usize) -> TextSize {
    let mut byte_pos: usize = 0;
    for (i, c) in text.chars().enumerate() {
        if i >= char_offset {
            break;
        }
        byte_pos += c.len_utf8();
    }
    // Safe: byte_pos is bounded by text.len() (indices from chars in the same text)
    TextSize::try_from(byte_pos).unwrap_or_else(|_| TextSize::from(text.len() as u32))
}

/// Convert a ruff TextRange to a Python-friendly RangeDto using a precomputed LineIndex.
///
/// Use this when converting multiple ranges for the same file — avoids
/// recomputing the line index on every call.
pub fn range_to_dto_with_index(
    source: &str,
    line_index: &LineIndex,
    range: ruff_text_size::TextRange,
) -> RangeDto {
    let start_loc = line_index.source_location(range.start(), source, PositionEncoding::Utf32);
    let end_loc = line_index.source_location(range.end(), source, PositionEncoding::Utf32);

    RangeDto {
        start: PositionDto {
            line: start_loc.line.get() as u32,
            column: (start_loc.character_offset.to_zero_indexed() + 1) as u32,
        },
        end: PositionDto {
            line: end_loc.line.get() as u32,
            column: (end_loc.character_offset.to_zero_indexed() + 1) as u32,
        },
    }
}

/// Convenience wrapper that computes a `LineIndex` internally.
/// Use `range_to_dto_with_index` when converting multiple ranges
/// for the same source.
pub fn range_to_dto(
    source: &str,
    range: ruff_text_size::TextRange,
) -> RangeDto {
    let line_index = LineIndex::from_source_text(source);
    range_to_dto_with_index(source, &line_index, range)
}
