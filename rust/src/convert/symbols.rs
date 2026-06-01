use crate::coordinates;
use crate::dto::{FileRangeDto, SymbolDto};

/// Convert a ty_ide SymbolInfo into a stable SymbolDto.
///
/// The caller is responsible for providing the source text and file path
/// so we can convert TextRange into 1-based line/column RangeDto.
pub fn convert_symbol(
    source: &str,
    file_path: &str,
    name: &str,
    kind: &ty_ide::SymbolKind,
    deprecated: bool,
    name_range: ruff_text_size::TextRange,
    full_range: ruff_text_size::TextRange,
    container_name: Option<&str>,
    qualified_name: Option<String>,
) -> SymbolDto {
    let name_range_dto = coordinates::range_to_dto(source, name_range);
    let full_range_dto = coordinates::range_to_dto(source, full_range);

    SymbolDto {
        name: name.to_string(),
        qualified_name,
        kind: kind.to_string().to_string(),
        location: FileRangeDto {
            path: file_path.to_string(),
            range: name_range_dto,
        },
        selection_range: Some(full_range_dto),
        container_name: container_name.map(|s| s.to_string()),
        deprecated,
    }
}
