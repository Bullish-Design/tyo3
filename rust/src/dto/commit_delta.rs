//! The id-level commit delta (Phase 3, §5.4 / §5.5).
//!
//! `CommitDeltaDto` is the single public per-write value. Where the legacy
//! `SyncResultDto` carried file-path strings, this carries **entity DurableIds**:
//!   - `created_ids` — minted this revision
//!   - `changed_ids` — same id, **different content hash** (no over-fire: an
//!     entity whose hash is unchanged never appears here)
//!   - `deleted_ids` — retired this revision (their authored records orphan, not drop)
//!   - `moved` — structured `{id, old/new qualified path, old/new file}`; a move is
//!     reported distinctly from create+delete so content-hash-keyed caches stay hit
//!   - `affected_ids` — the closure of `changed ∪ deleted` under the code layer's
//!     reverse-dependency index (Phase 3: seeds-only while the layer is empty; the
//!     live transitive closure lands in Phase 4 when the layer is authoritative)
//!   - `code_delta` — the Phase 2 structural `CodeDeltaDto`, **nested** (not duplicated)
//!
//! `touched_files` and the per-category `created`/`changed`/`deleted` path lists are
//! **metadata only** — the file paths the write synthesised events for. They exist
//! for file-interest bus matching (Phase 6) and human readability, and must never
//! stand in for an entity id (§5.4).

use crate::dto::CodeDeltaDto;

/// A structured move: same `id`, unchanged body, new location.
///
/// `old_file` / `new_file` are the file components of the old / new
/// `qualified_path` (`file::qualified_name`).
#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct MovedEntityDto {
    pub id: String,
    pub old_qualified_path: String,
    pub new_qualified_path: String,
    pub old_file: String,
    pub new_file: String,
}

/// The single public per-write delta (§5.4 / §5.5).
#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct CommitDeltaDto {
    pub revision: u64,

    // ── id-level entity delta (the contract) ────────────────────────────
    /// Minted DurableIds.
    pub created_ids: Vec<String>,
    /// Same id, **different content hash**.
    pub changed_ids: Vec<String>,
    /// Retired DurableIds (authored records orphan, never drop).
    pub deleted_ids: Vec<String>,
    /// Structured moves: same id at a new location, unchanged body.
    pub moved: Vec<MovedEntityDto>,
    /// Ids authored this commit (the `author` write path); `[]` otherwise.
    pub authored_ids: Vec<String>,
    /// Closure of `changed ∪ deleted` under `reverse_deps`. Phase 3: seeds-only
    /// (the head code layer is empty until Phase 4 makes it authoritative).
    pub affected_ids: Vec<String>,

    // ── nested structural delta (Phase 2, carried for Phase 4) ───────────
    pub code_delta: CodeDeltaDto,

    // ── path-shaped metadata (NOT ids — for bus file-interest + readability) ─
    /// Union of the path-level created/changed/deleted strings.
    pub touched_files: Vec<String>,
    /// Per-category touched paths (metadata; consumed by the still-path-shaped
    /// Phase 6/7 post-commit helpers until they are rewritten).
    pub created: Vec<String>,
    pub changed: Vec<String>,
    pub deleted: Vec<String>,

    // ── lifecycle / coarse-change signals (carried from SyncResultDto) ───
    pub needs_review: Vec<String>,
    pub orphaned: Vec<String>,
    pub authored_needs_review: Vec<String>,
    pub authored_orphaned: Vec<String>,
    /// Debug counters proving reconciliation is bounded by the committed scope.
    #[serde(default)]
    pub identity_extracted: usize,
    #[serde(default)]
    pub identity_scope_files: usize,
    pub rescan: bool,
    pub project_changed: bool,
    pub custom_stdlib_changed: bool,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dto::{CodeEdgeDto, CodeNodeDto, PositionDto, RangeDto};

    fn range(sl: u32, sc: u32, el: u32, ec: u32) -> RangeDto {
        RangeDto {
            start: PositionDto { line: sl, column: sc },
            end: PositionDto { line: el, column: ec },
        }
    }

    #[test]
    fn commit_delta_round_trips_with_nested_code_delta_and_python_field_names() {
        let delta = CommitDeltaDto {
            revision: 9,
            created_ids: vec!["01CREATED".into()],
            changed_ids: vec!["01CHANGED".into()],
            deleted_ids: vec!["01DELETED".into()],
            moved: vec![MovedEntityDto {
                id: "01MOVED".into(),
                old_qualified_path: "a.py::helper".into(),
                new_qualified_path: "b.py::helper".into(),
                old_file: "a.py".into(),
                new_file: "b.py".into(),
            }],
            authored_ids: vec![],
            affected_ids: vec!["01CHANGED".into(), "01DELETED".into()],
            code_delta: CodeDeltaDto {
                revision: 9,
                rescan: false,
                nodes_upserted: vec![CodeNodeDto {
                    durable_id: "01CHANGED".into(),
                    name: "foo".into(),
                    qualified_name: "foo".into(),
                    kind: "function".into(),
                    file: "a.py".into(),
                    range: range(1, 1, 2, 1),
                    name_range: Some(range(1, 5, 1, 8)),
                    content_hash: Some("beef".into()),
                    content_hashes: std::collections::HashMap::new(),
                    external: false,
                    package: None,
                }],
                nodes_removed: vec![],
                nodes_moved: vec![],
                edges_added: vec![CodeEdgeDto {
                    source_id: "<module>a.py".into(),
                    destination_id: "01CHANGED".into(),
                    kind: "defines".into(),
                    role: None,
                    file: None,
                    range: None,
                }],
                edges_removed: vec![],
            },
            touched_files: vec!["a.py".into()],
            created: vec![],
            changed: vec!["a.py".into()],
            deleted: vec![],
            needs_review: vec![],
            orphaned: vec![],
            authored_needs_review: vec![],
            authored_orphaned: vec![],
            identity_extracted: 1,
            identity_scope_files: 1,
            rescan: false,
            project_changed: false,
            custom_stdlib_changed: false,
        };

        let json = serde_json::to_value(&delta).unwrap();
        // id-level fields carry ids, never paths.
        assert_eq!(json["changed_ids"][0], "01CHANGED");
        assert_eq!(json["created_ids"][0], "01CREATED");
        assert_eq!(json["deleted_ids"][0], "01DELETED");
        // structured moved exposes id + old/new locations.
        assert_eq!(json["moved"][0]["id"], "01MOVED");
        assert_eq!(json["moved"][0]["old_file"], "a.py");
        assert_eq!(json["moved"][0]["new_file"], "b.py");
        // the code delta is nested, not hoisted.
        assert_eq!(json["code_delta"]["nodes_upserted"][0]["durable_id"], "01CHANGED");
        // touched files are path-shaped metadata, kept separate from ids.
        assert_eq!(json["touched_files"][0], "a.py");
        assert_eq!(json["changed"][0], "a.py");

        let back: CommitDeltaDto = serde_json::from_value(json).unwrap();
        assert_eq!(back.revision, 9);
        assert_eq!(back.changed_ids, vec!["01CHANGED".to_string()]);
        assert_eq!(back.moved.len(), 1);
        assert_eq!(back.code_delta.nodes_upserted.len(), 1);
    }
}
