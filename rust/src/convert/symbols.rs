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
// Each argument is a distinct per-symbol field mapped 1:1 into `SymbolDto`;
// bundling them would just mirror the output struct.
#[allow(clippy::too_many_arguments)]
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
///
/// `SymbolWalkCtx` holds the immutable analysis inputs shared by every node in
/// the walk; the remaining arguments are the current cursor and accumulator.
pub(crate) struct SymbolWalkCtx<'a> {
    pub(crate) hierarchical: &'a ty_ide::HierarchicalSymbols,
    pub(crate) source: &'a str,
    pub(crate) stmt_index:
        &'a std::collections::HashMap<ruff_text_size::TextRange, &'a ruff_python_ast::Stmt>,
    pub(crate) line_index: &'a ruff_source_file::LineIndex,
    pub(crate) file_path: &'a str,
    pub(crate) registry: &'a IdentityRegistry,
    pub(crate) hash_policies:
        Option<&'a std::collections::HashMap<String, crate::hash::HashPolicy>>,
    pub(crate) default_profile_name: Option<&'a str>,
}

pub fn collect_symbols_recursive(
    ctx: &SymbolWalkCtx<'_>,
    id: ty_ide::SymbolId,
    info: &ty_ide::SymbolInfo,
    parent_name: Option<&str>,
    parent_identity_path: Option<&str>,
    symbols: &mut Vec<dto::SymbolDto>,
) {
    let qualified = parent_name.map(|p| format!("{}.{}", p, info.name));
    let identity_path = match parent_identity_path {
        Some(p) => format!("{}::{}", p, info.name),
        None => format!("{}::{}", ctx.file_path, info.name),
    };
    let anchor = ctx
        .registry
        .by_path(&identity_path)
        .and_then(|id| ctx.registry.get(id));

    // Compute per-profile content hashes from a canonical rendering of the
    // entity's AST subtree (looked up in `stmt_index` by `full_range`).
    let mut content_hashes: std::collections::HashMap<String, String> = std::collections::HashMap::new();
    let default_hash = if let Some(policies) = ctx.hash_policies {
        for (profile_name, policy) in policies {
            let nf = crate::hash::entity_normal_form(
                ctx.stmt_index,
                info.full_range,
                ctx.source,
                policy,
            );
            let h = crate::hash::hash_entity(&nf);
            content_hashes.insert(profile_name.clone(), h.0.to_string());
        }
        // The default content_hash is the one under the default profile.
        ctx.default_profile_name
            .and_then(|pn| content_hashes.get(pn).cloned())
    } else {
        // No per-profile hashes configured: use the identity anchor hash.
        anchor.map(|a| a.content_hash.0.to_string())
    };

    let sym = convert_symbol(
        ctx.source,
        ctx.line_index,
        ctx.file_path,
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

    for (child_id, child_info) in ctx.hierarchical.children(id) {
        collect_symbols_recursive(
            ctx,
            child_id,
            &child_info,
            Some(own_name),
            Some(&identity_path),
            symbols,
        );
    }
}
