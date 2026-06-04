use crate::dto::RangeDto;

/// A single item in a type hierarchy (a class in the hierarchy tree).
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct TypeHierarchyItemDto {
    pub name: String,
    pub detail: Option<String>,
    pub path: String,
    pub full_range: RangeDto,
    pub selection_range: RangeDto,
}

/// Result of a type hierarchy query.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct TypeHierarchyDto {
    pub item: TypeHierarchyItemDto,
    pub supertypes: Vec<TypeHierarchyItemDto>,
    pub subtypes: Vec<TypeHierarchyItemDto>,
}
