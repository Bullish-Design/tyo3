use ruff_db::Db;

use crate::coordinates;
use crate::dto::{DefinitionTargetDto, ReferenceDto};

/// Convert a ty_ide NavigationTarget into a DefinitionTargetDto.
pub fn convert_navigation_target(
    db: &dyn Db,
    target: &ty_ide::NavigationTarget,
) -> DefinitionTargetDto {
    let file = target.file();
    let source = ruff_db::source::source_text(db, file);
    let source_str = source.as_str();
    let file_path = file.path(db).as_str().to_string();

    DefinitionTargetDto {
        path: file_path,
        range: coordinates::range_to_dto(source_str, target.focus_range()),
        selection_range: Some(coordinates::range_to_dto(
            source_str,
            target.full_range(),
        )),
        symbol: None,
        module_name: None,
    }
}

/// Convert a ty_ide NavigationTargets collection into a Vec of DefinitionTargetDto.
///
/// Uses IntoIterator (for loop) instead of the private `.iter()` method.
pub fn convert_navigation_targets(
    db: &dyn Db,
    targets: &ty_ide::NavigationTargets,
) -> Vec<DefinitionTargetDto> {
    let mut result = Vec::new();
    for target in targets {
        result.push(convert_navigation_target(db, target));
    }
    result
}

/// Convert a ty_ide ReferenceTarget into a ReferenceDto.
pub fn convert_reference(
    db: &dyn Db,
    reference: &ty_ide::ReferenceTarget,
) -> ReferenceDto {
    let file = reference.file();
    let source = ruff_db::source::source_text(db, file);
    let source_str = source.as_str();
    let file_path = file.path(db).as_str().to_string();

    ReferenceDto {
        path: file_path,
        range: coordinates::range_to_dto(source_str, reference.range()),
        kind: convert_reference_kind(reference.kind()),
    }
}

/// Convert a Vec of ty_ide ReferenceTarget into a Vec of ReferenceDto.
pub fn convert_references(
    db: &dyn Db,
    references: &[ty_ide::ReferenceTarget],
) -> Vec<ReferenceDto> {
    references
        .iter()
        .map(|r| convert_reference(db, r))
        .collect()
}

/// Map ty_ide ReferenceKind to our string DTO.
fn convert_reference_kind(kind: ty_ide::ReferenceKind) -> String {
    match kind {
        ty_ide::ReferenceKind::Read => "read".to_string(),
        ty_ide::ReferenceKind::Write => "write".to_string(),
        ty_ide::ReferenceKind::Other => "other".to_string(),
    }
}
