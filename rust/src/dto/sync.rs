/// The delta produced by a write to the head, surfaced to Python as a dict.
///
/// `created` / `changed` / `deleted` are the project-relative-or-absolute path
/// strings we synthesised events for (the same paths the caller passed, resolved).
/// `revision` is the new application revision. `rescan` is true for `sync_all`,
/// where the delta is unknown and a full graph rebuild is required (Phase 6).
///
/// Identity fields (Gate 2):
/// - `moved`: qualified_paths that moved (Moved bindings)
/// - `needs_review`: DurableIds flagged for human review
/// - `orphaned`: DurableIds that disappeared this revision
#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct SyncResultDto {
    pub revision: u64,
    pub created: Vec<String>,
    pub changed: Vec<String>,
    pub deleted: Vec<String>,
    #[serde(default)]
    pub moved: Vec<String>,
    #[serde(default)]
    pub needs_review: Vec<String>,
    #[serde(default)]
    pub orphaned: Vec<String>,
    pub project_changed: bool,
    pub custom_stdlib_changed: bool,
    pub rescan: bool,
}
