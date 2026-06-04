use ty_project::Db;

use crate::dto::{CompletionDto, CompletionKindDto};

/// Convert a single ty_ide Completion to an owned CompletionDto.
fn convert_completion(db: &dyn Db, c: &ty_ide::Completion<'_>) -> CompletionDto {
    let type_display = c.ty.as_ref().map(|ty| ty.display(db).to_string());
    let kind = c.kind.as_ref().map(convert_completion_kind);
    let module_name = c.module_name.map(|m| m.as_str().to_string());
    CompletionDto {
        name: c.name.as_str().to_string(),
        qualified_name: c.qualified.as_ref().map(|q| q.as_str().to_string()),
        insert_text: c.insert.as_ref().map(|i| i.as_str().to_string()),
        type_: type_display,
        kind,
        module_name,
    }
}

/// Convert a ty_ide CompletionKind to CompletionKindDto.
fn convert_completion_kind(kind: &ty_ide::CompletionKind) -> CompletionKindDto {
    match kind {
        ty_ide::CompletionKind::Text => CompletionKindDto::Text,
        ty_ide::CompletionKind::Method => CompletionKindDto::Method,
        ty_ide::CompletionKind::Function => CompletionKindDto::Function,
        ty_ide::CompletionKind::Constructor => CompletionKindDto::Constructor,
        ty_ide::CompletionKind::Field => CompletionKindDto::Field,
        ty_ide::CompletionKind::Variable => CompletionKindDto::Variable,
        ty_ide::CompletionKind::Class => CompletionKindDto::Class,
        ty_ide::CompletionKind::Interface => CompletionKindDto::Interface,
        ty_ide::CompletionKind::Module => CompletionKindDto::Module,
        ty_ide::CompletionKind::Property => CompletionKindDto::Property,
        ty_ide::CompletionKind::Unit => CompletionKindDto::Unit,
        ty_ide::CompletionKind::Value => CompletionKindDto::Value,
        ty_ide::CompletionKind::Enum => CompletionKindDto::Enum,
        ty_ide::CompletionKind::Keyword => CompletionKindDto::Keyword,
        ty_ide::CompletionKind::Snippet => CompletionKindDto::Snippet,
        ty_ide::CompletionKind::Color => CompletionKindDto::Color,
        ty_ide::CompletionKind::File => CompletionKindDto::File,
        ty_ide::CompletionKind::Reference => CompletionKindDto::Reference,
        ty_ide::CompletionKind::Folder => CompletionKindDto::Folder,
        ty_ide::CompletionKind::EnumMember => CompletionKindDto::EnumMember,
        ty_ide::CompletionKind::Constant => CompletionKindDto::Constant,
        ty_ide::CompletionKind::Struct => CompletionKindDto::Struct,
        ty_ide::CompletionKind::Event => CompletionKindDto::Event,
        ty_ide::CompletionKind::Operator => CompletionKindDto::Operator,
        ty_ide::CompletionKind::TypeParameter => CompletionKindDto::TypeParameter,
    }
}

/// Convert a Vec of ty_ide Completion to a Vec of owned CompletionDto.
pub fn convert_completions(
    db: &dyn Db,
    completions: &[ty_ide::Completion<'_>],
) -> Vec<CompletionDto> {
    completions
        .iter()
        .map(|c| convert_completion(db, c))
        .collect()
}
