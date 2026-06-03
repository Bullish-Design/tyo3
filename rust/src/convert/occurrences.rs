use ruff_db::files::File;
use ruff_db::parsed::parsed_module;
use ruff_python_ast::AnyNodeRef;
use ruff_python_ast::find_node::covering_node;
use ruff_source_file::LineIndex;

use ty_ide::{SemanticTokenType, semantic_tokens};
use ty_project::Db;
use ty_python_semantic::{ImportAliasResolution, SemanticModel};
use ty_python_semantic::types::ide_support::definition_for_name;

use crate::coordinates;
use crate::dto::{NameOccurrenceDto, ReferenceRoleDto};

/// Name-like semantic token types.
const NAME_TOKEN_TYPES: &[SemanticTokenType] = &[
    SemanticTokenType::Namespace,
    SemanticTokenType::Class,
    SemanticTokenType::Parameter,
    SemanticTokenType::SelfParameter,
    SemanticTokenType::ClsParameter,
    SemanticTokenType::Variable,
    SemanticTokenType::Property,
    SemanticTokenType::Function,
    SemanticTokenType::Method,
    SemanticTokenType::Decorator,
    SemanticTokenType::BuiltinConstant,
    SemanticTokenType::TypeParameter,
];

/// Batch-resolve all name occurrences in a file.
pub fn convert_file_occurrences(
    db: &dyn Db,
    file: File,
    source: &str,
    line_index: &LineIndex,
) -> Vec<NameOccurrenceDto> {
    let tokens = semantic_tokens(db, file, None);
    let parsed = parsed_module(db, file).load(db);
    let model = SemanticModel::new(db, file);

    let mut occurrences = Vec::with_capacity(tokens.len());

    for token in tokens.iter() {
        if !is_name_token(&token.token_type) {
            continue;
        }

        let covering = covering_node(parsed.syntax().into(), token.range);

        let name_ref = match name_ref_from_node(covering.node()) {
            Some(n) => n,
            None => continue,
        };

        let role = classify_role(&token.token_type, token.modifiers);

        let (target_file, target_name) = resolve_definition(db, &model, &name_ref);

        occurrences.push(NameOccurrenceDto {
            range: coordinates::range_to_dto_with_index(source, line_index, token.range),
            target_file,
            target_name,
            role,
        });
    }

    occurrences
}

// ── Helpers ───────────────────────────────────────────────────────────

fn is_name_token(tt: &SemanticTokenType) -> bool {
    NAME_TOKEN_TYPES.contains(tt)
}

fn name_ref_from_node(node: AnyNodeRef<'_>) -> Option<ruff_python_ast::ExprName> {
    if let AnyNodeRef::ExprName(name) = node {
        return Some(name.clone());
    }
    if let AnyNodeRef::Identifier(id) = node {
        return Some(ruff_python_ast::ExprName {
            node_index: ruff_python_ast::AtomicNodeIndex::NONE,
            range: id.range,
            id: id.id.clone(),
            ctx: ruff_python_ast::ExprContext::Load,
        });
    }
    None
}

fn classify_role(
    token_type: &SemanticTokenType,
    modifiers: ty_ide::SemanticTokenModifier,
) -> ReferenceRoleDto {
    if modifiers.contains(ty_ide::SemanticTokenModifier::DEFINITION) {
        ReferenceRoleDto::Definition
    } else if matches!(token_type, SemanticTokenType::Namespace) {
        ReferenceRoleDto::Import
    } else {
        ReferenceRoleDto::Read
    }
}

fn resolve_definition(
    db: &dyn Db,
    model: &SemanticModel<'_>,
    name: &ruff_python_ast::ExprName,
) -> (Option<String>, Option<String>) {
    let def = definition_for_name(model, name, ImportAliasResolution::ResolveAliases);

    match def {
        Some(definition) => {
            let file = definition.file(db);
            let path = file.path(db).as_str().to_string();
            let sym_name = definition.name(db);
            (Some(path), sym_name)
        }
        None => (None, None),
    }
}
