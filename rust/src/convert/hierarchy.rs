use ruff_db::Db;
use ruff_db::source::source_text;
use ruff_source_file::LineIndex;

use crate::coordinates;
use crate::dto::TypeHierarchyItemDto;

/// Convert a `ty_ide::TypeHierarchyItem` to a DTO.
pub fn convert_hierarchy_item(
    db: &dyn Db,
    item: &ty_ide::TypeHierarchyItem,
) -> TypeHierarchyItemDto {
    let source = source_text(db, item.file);
    let src = source.as_str();
    let line_index = LineIndex::from_source_text(src);
    let path = item.file.path(db).as_str().to_string();

    TypeHierarchyItemDto {
        name: item.name.as_str().to_string(),
        detail: item.detail.clone(),
        path,
        full_range: coordinates::range_to_dto_with_index(src, &line_index, item.full_range),
        selection_range: coordinates::range_to_dto_with_index(
            src,
            &line_index,
            item.selection_range,
        ),
    }
}

/// Convert a slice of `ty_ide::TypeHierarchyItem` to DTOs.
pub fn convert_hierarchy_items(
    db: &dyn Db,
    items: &[ty_ide::TypeHierarchyItem],
) -> Vec<TypeHierarchyItemDto> {
    items
        .iter()
        .map(|i| convert_hierarchy_item(db, i))
        .collect()
}
