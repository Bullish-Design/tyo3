use ruff_source_file::{LineIndex, OneIndexed, PositionEncoding, SourceLocation};
use ruff_text_size::TextSize;

use crate::dto::{PositionDto, RangeDto};

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

    // Validate column does not go beyond one-past-the-end of the line.
    // Column line_length + 1 is allowed (position after last char);
    // column line_length + 2 is rejected.
    let line = OneIndexed::from_zero_indexed(line_idx);
    let raw_line = source.split('\n').nth(line_idx).unwrap_or("");
    let line_text = raw_line.strip_suffix('\r').unwrap_or(raw_line);
    let line_char_count = line_text.chars().count();
    if col > line_char_count {
        return Err(format!(
            "Column {} exceeds line {} length ({} characters)",
            column,
            line_idx + 1,
            line_char_count
        ));
    }

    // Use ruff's LineIndex for the offset computation — same encoding as
    // range_to_dto_with_index, so both directions share one implementation.
    let source_location = SourceLocation {
        line,
        character_offset: OneIndexed::from_zero_indexed(col),
    };
    Ok(line_index.offset(source_location, source, PositionEncoding::Utf32))
}

/// Convert a 1-based position to an offset, clamping columns beyond the visible
/// line end to the line-end position. Use for range *end* positions only.
pub fn position_to_offset_clamped_line_end_with_index(
    source: &str,
    line_index: &LineIndex,
    line: u32,
    column: u32,
) -> Result<TextSize, String> {
    match position_to_offset_with_index(source, line_index, line, column) {
        Ok(offset) => Ok(offset),
        Err(_) => {
            let line_idx = line
                .checked_sub(1)
                .ok_or_else(|| "Line must be >= 1".to_string())? as usize;
            if line_idx >= line_index.line_count() {
                return Err(format!(
                    "Line {} exceeds file length ({} lines)",
                    line,
                    line_index.line_count()
                ));
            }

            let raw_line = source.split('\n').nth(line_idx).unwrap_or("");
            let line_text = raw_line.strip_suffix('\r').unwrap_or(raw_line);
            position_to_offset_with_index(
                source,
                line_index,
                line,
                line_text.chars().count() as u32 + 1,
            )
        }
    }
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

#[cfg(test)]
mod tests {
    use super::*;
    use ruff_source_file::LineIndex;

    fn offset(source: &str, line: u32, column: u32) -> Result<u32, String> {
        let index = LineIndex::from_source_text(source);
        position_to_offset_with_index(source, &index, line, column)
            .map(|t| t.to_u32())
    }

    #[test]
    fn rejects_column_beyond_current_line() {
        let source = "x = 1\ny = 2\n";
        assert!(offset(source, 1, 500).is_err());
    }

    #[test]
    fn accepts_end_of_line_position() {
        let source = "x = 1\ny = 2\n";
        assert!(offset(source, 1, 6).is_ok());
    }

    #[test]
    fn rejects_line_past_file() {
        let source = "x = 1\n";
        assert!(offset(source, 99, 1).is_err());
    }

    #[test]
    fn handles_unicode_codepoints() {
        let source = "αβ = 1\n";
        assert!(offset(source, 1, 1).is_ok());
        assert!(offset(source, 1, 2).is_ok());
        assert!(offset(source, 1, 99).is_err());
    }

    #[test]
    fn handles_crlf() {
        let source = "x = 1\r\ny = 2\r\n";
        assert!(offset(source, 1, 6).is_ok());
        assert!(offset(source, 1, 7).is_err());
    }

    #[test]
    fn clamps_range_end_columns() {
        let source = "x = 1\r\ny = 2\r\n";
        let index = LineIndex::from_source_text(source);
        let strict = offset(source, 1, 500);
        let clamped = position_to_offset_clamped_line_end_with_index(source, &index, 1, 500);
        assert!(strict.is_err());
        assert_eq!(clamped.unwrap().to_u32(), offset(source, 1, 6).unwrap());
    }
}
