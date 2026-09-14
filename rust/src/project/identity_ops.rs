//! Identity reads shared by the head and by pinned snapshots.
//!
//! Identity is a property of a **revision**, not of the head: the entity that
//! `id_for` resolves, and the location `locate` reports, are whatever the
//! registry said at the revision being read. Both `PyTyProject` (head) and
//! `PySnapshot` (pinned) therefore answer these questions, and both must answer
//! them the same way — so the logic lives here once and each caller supplies
//! its own state.
//!
//! Every state carries a registry. An empty registry means "no identity
//! registered", which is the same answer as "not found": `None` or an empty
//! list, never an error.

use super::*;

/// Resolve the DurableId of the entity enclosing `(path, line, col)`.
///
/// Finds the innermost symbol containing the position, builds its
/// qualified_path, and looks it up in `state`'s registry. Returns `None` when
/// no symbol encloses the position or no id is registered for it.
pub(crate) fn id_for(
    state: &TyProjectState,
    path: &str,
    line: u32,
    col: u32,
) -> PyResult<Option<String>> {
    let registry = &state.registry;

    let file = crate::files::resolve_file(&state.db, state.root.as_std_path(), path)
        .map_err(|e| PathResolutionError::new_err(e.to_string()))?;
    let file_path = file.path(&state.db).as_str().to_string();

    let hierarchical = ty_ide::document_symbols(&state.db, file).to_hierarchical();

    let src = ruff_db::source::source_text(&state.db, file);
    let source_str = src.as_str();
    let line_index = ruff_source_file::LineIndex::from_source_text(source_str);
    let offset =
        crate::coordinates::position_to_offset_with_index(source_str, &line_index, line, col)
            .map_err(|e| PositionError::new_err(e.to_string()))?;

    // The innermost enclosing symbol wins: the smallest range that contains the
    // offset is the most specific entity at that position.
    let mut best: Option<(String, u32)> = None; // (qualified_path, range_size)
    for (id, info) in hierarchical.iter() {
        if !info.full_range.contains(offset) {
            continue;
        }
        let size = info.full_range.len().to_u32();
        if best.as_ref().is_none_or(|(_, prev)| size < *prev) {
            best = Some((
                super::methods::build_qualified_path(&hierarchical, id, &file_path),
                size,
            ));
        }
    }

    Ok(best.and_then(|(qp, _)| registry.by_path(&qp).map(|id| id.0.clone())))
}

/// The `file::qualified_path` currently bound to `durable_id`.
///
/// `None` if the id is not in this registry — retired, from another session,
/// or not yet created at this revision.
pub(crate) fn locate(registry: &IdentityRegistry, durable_id: &str) -> Option<String> {
    let id = DurableId(durable_id.to_string());
    registry.get(&id).map(|a| a.qualified_path.clone())
}

/// DurableIds whose body now differs from the hash captured when their note was
/// authored — the durable, level-triggered `needs_review` set.
///
/// This is not the transient registry `NeedsReview` status (an edge signal that
/// auto-clears on the next reconcile): it survives saves, same-file edits, and
/// restart, and unflags on revert.
pub(crate) fn needs_review(
    config: &ValidatedConfig,
    authored: Option<&AuthoredStore>,
    registry: &IdentityRegistry,
) -> Vec<String> {
    authored.map_or_else(Vec::new, |authored| {
        super::commit::needs_review_ids(config, authored, registry)
    })
}

/// DurableIds currently flagged `Orphaned` in this registry.
pub(crate) fn orphaned(registry: &IdentityRegistry) -> Vec<String> {
    registry
        .iter()
        .filter(|a| a.status == crate::identity::IdentityStatus::Orphaned)
        .map(|a| a.id.0.clone())
        .collect()
}
