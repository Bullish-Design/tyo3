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
/// - `identity_extracted` / `identity_scope_files`: debug counters used to
///   prove incremental reconciliation is bounded by the committed scope.
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
    #[serde(default)]
    pub identity_extracted: usize,
    #[serde(default)]
    pub identity_scope_files: usize,
    pub project_changed: bool,
    pub custom_stdlib_changed: bool,
    pub rescan: bool,
    /// The code-layer delta produced inside the native commit (Gate 3N). `None`
    /// until the producer is wired (Steps 1–5); `#[serde(default)]` keeps old
    /// callers and tests unaffected while the migration is in flight.
    #[serde(default)]
    pub code_delta: Option<crate::dto::CodeDelta>,
}
