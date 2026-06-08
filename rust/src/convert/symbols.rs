use ruff_source_file::LineIndex;

use crate::coordinates;
use crate::dto::{FileRangeDto, SymbolDto, SymbolKindDto};
use crate::dto;
use crate::identity::IdentityRegistry;

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
    durable_id: Option<String>,
    content_hash: Option<String>,
    content_hashes: std::collections::HashMap<String, String>,
) -> SymbolDto {
    let name_range_dto = coordinates::range_to_dto_with_index(source, line_index, name_range);
    let full_range_dto = coordinates::range_to_dto_with_index(source, line_index, full_range);

    SymbolDto {
        name: name.to_string(),
        qualified_name,
        kind: symbol_kind_to_dto(kind),
        location: FileRangeDto {
            path: file_path.to_string(),
            range: full_range_dto,
        },
        selection_range: Some(name_range_dto),
        container_name: container_name.map(|s| s.to_string()),
        deprecated,
        durable_id,
        content_hash,
        content_hashes,
    }
}

/// Recursively collect document symbols from a hierarchical symbol tree.
pub fn collect_symbols_recursive(
    hierarchical: &ty_ide::HierarchicalSymbols,
    id: ty_ide::SymbolId,
    info: &ty_ide::SymbolInfo,
    source: &str,
    stmt_index: &std::collections::HashMap<ruff_text_size::TextRange, &ruff_python_ast::Stmt>,
    line_index: &ruff_source_file::LineIndex,
    file_path: &str,
    parent_name: Option<&str>,
    parent_identity_path: Option<&str>,
    registry: Option<&IdentityRegistry>,
    hash_policies: Option<&std::collections::HashMap<String, crate::hash::HashPolicy>>,
    default_profile_name: Option<&str>,
    symbols: &mut Vec<dto::SymbolDto>,
) {
    let qualified = match parent_name {
        Some(p) => Some(format!("{}.{}", p, info.name)),
        None => None,
    };
    let identity_path = match parent_identity_path {
        Some(p) => format!("{}::{}", p, info.name),
        None => format!("{}::{}", file_path, info.name),
    };
    let anchor = registry.and_then(|r| r.by_path(&identity_path).and_then(|id| r.get(id)));

    // Compute per-profile content hashes from a canonical rendering of the
    // entity's AST subtree (looked up in `stmt_index` by `full_range`).
    let mut content_hashes: std::collections::HashMap<String, String> = std::collections::HashMap::new();
    let default_hash = if let Some(policies) = hash_policies {
        for (profile_name, policy) in policies {
            let nf = crate::hash::entity_normal_form(stmt_index, info.full_range, source, policy);
            let h = crate::hash::hash_entity(&nf);
            content_hashes.insert(profile_name.clone(), h.0.to_string());
        }
        // The default content_hash is the one under the default profile.
        default_profile_name.and_then(|pn| content_hashes.get(pn).cloned())
    } else {
        // No per-profile hashes configured: use the identity anchor hash.
        anchor.map(|a| a.content_hash.0.to_string())
    };

    let sym = convert_symbol(
        source,
        line_index,
        file_path,
        &info.name,
        &info.kind,
        info.deprecated,
        info.name_range,
        info.full_range,
        parent_name,
        qualified.clone(),
        anchor.map(|a| a.id.0.clone()),
        default_hash,
        content_hashes,
    );
    symbols.push(sym);

    let own_name = match &qualified {
        Some(q) => q.as_str(),
        None => &info.name,
    };

    for (child_id, child_info) in hierarchical.children(id) {
        collect_symbols_recursive(
            hierarchical,
            child_id,
            &child_info,
            source,
            stmt_index,
            line_index,
            file_path,
            Some(own_name),
            Some(&identity_path),
            registry,
            hash_policies,
            default_profile_name,
            symbols,
        );
    }
}
