use ruff_source_file::LineIndex;

use crate::coordinates;
use crate::dto::{FileRangeDto, SymbolDto, SymbolKindDto};

/// Map a ty_ide SymbolKind to a SymbolKindDto.
fn symbol_kind_to_dto(kind: &ty_ide::SymbolKind) -> SymbolKindDto {
    match kind {
        ty_ide::SymbolKind::Module => SymbolKindDto::Module,
        ty_ide::SymbolKind::Class => SymbolKindDto::Class,
        ty_ide::SymbolKind::Function => SymbolKindDto::Function,
        ty_ide::SymbolKind::Method => SymbolKindDto::Method,
        ty_ide::SymbolKind::Constructor => SymbolKindDto::Constructor,
        ty_ide::SymbolKind::Variable => SymbolKindDto::Variable,
        ty_ide::SymbolKind::Constant => SymbolKindDto::Constant,
        ty_ide::SymbolKind::Field => SymbolKindDto::Field,
        ty_ide::SymbolKind::Parameter => SymbolKindDto::Parameter,
        ty_ide::SymbolKind::Property => SymbolKindDto::Property,
        ty_ide::SymbolKind::TypeParameter => SymbolKindDto::TypeParameter,
        ty_ide::SymbolKind::Import => SymbolKindDto::Import,
    }
}

/// Convert a ty_ide SymbolInfo into a stable SymbolDto.
///
/// The caller is responsible for providing the source text, a precomputed
/// `LineIndex`, and file path so we can convert TextRange into 1-based
/// line/column RangeDto.
pub fn convert_symbol(
    source: &str,
    line_index: &LineIndex,
    file_path: &str,
    name: &str,
    kind: &ty_ide::SymbolKind,
    deprecated: bool,
    name_range: ruff_text_size::TextRange,
    full_range: ruff_text_size::TextRange,
    container_name: Option<&str>,
    qualified_name: Option<String>,
) -> SymbolDto {
    let name_range_dto = coordinates::range_to_dto_with_index(source, line_index, name_range);
    let full_range_dto = coordinates::range_to_dto_with_index(source, line_index, full_range);

    SymbolDto {
        name: name.to_string(),
        qualified_name,
        kind: symbol_kind_to_dto(kind),
        location: FileRangeDto {
            path: file_path.to_string(),
            range: name_range_dto,
        },
        selection_range: Some(full_range_dto),
        container_name: container_name.map(|s| s.to_string()),
        deprecated,
    }
}
