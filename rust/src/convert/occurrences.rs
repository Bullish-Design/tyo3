use ruff_db::files::File;
use ruff_db::parsed::parsed_module;
use ruff_python_ast::AnyNodeRef;
use ruff_python_ast::find_node::covering_node;
use ruff_source_file::LineIndex;

use ty_ide::{SemanticTokenType, semantic_tokens};
use ty_project::Db;
use ty_python_core::definition::Definition;
use ty_python_core::semantic_index;
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

        let (target_file, target_name, target_qualified_name) =
            resolve_definition(db, &model, &name_ref);

        occurrences.push(NameOccurrenceDto {
            range: coordinates::range_to_dto_with_index(source, line_index, token.range),
            target_file,
            target_name,
            target_qualified_name,
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
) -> (Option<String>, Option<String>, Option<String>) {
    let def = definition_for_name(model, name, ImportAliasResolution::ResolveAliases);

    match def {
        Some(definition) => {
            let file = definition.file(db);
            let path = file.path(db).as_str().to_string();
            let sym_name = definition.name(db);
            let qualified = build_qualified_name(db, &definition);
            (Some(path), sym_name, qualified)
        }
        None => (None, None, None),
    }
}

/// Build a dotted qualified name for a definition by walking its scope chain.
///
/// For a method `save` inside class `User`, returns `Some("User.save")`.
/// For a top-level function `process_data`, returns `None` (the short name
/// suffices as the qualified name).
///
/// The scope chain is walked upward from the definition's scope, collecting
/// names of enclosing class and function scopes.  Module, lambda, and
/// comprehension scopes are skipped (they are not part of a qualified name).
fn build_qualified_name(db: &dyn Db, definition: &Definition) -> Option<String> {
    let short_name = definition.name(db)?;
    let file = definition.file(db);
    let module = parsed_module(db, file).load(db);
    let index = semantic_index(db, file);

    let mut current_scope_id = definition.file_scope(db);
    let mut parent_names: Vec<String> = Vec::new();

    loop {
        let scope = index.scope(current_scope_id);
        match scope.parent() {
            Some(parent_id) => {
                let parent_scope_id = parent_id.to_scope_id(db, file);
                let parent_name = parent_scope_id.name(db, &module);

                // Stop at the module scope — no qualified prefix for module-level symbols.
                if parent_name == "<module>" {
                    break;
                }

                // Skip synthetic scopes: lambda, comprehensions, generator expressions.
                // Class and function scopes contribute their name to the qualified path.
                if !parent_name.starts_with('<') {
                    parent_names.push(parent_name.to_string());
                }

                current_scope_id = parent_id;
            }
            None => break,
        }
    }

    if parent_names.is_empty() {
        // Top-level symbol — short name is sufficient.
        None
    } else {
        parent_names.reverse();
        Some(format!("{}.{}", parent_names.join("."), short_name))
    }
}
