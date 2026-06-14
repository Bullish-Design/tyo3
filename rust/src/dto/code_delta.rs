//! The code-delta wire contract (Phase 2, §5.4).
//!
//! These DTOs describe the precise set of node/edge upserts, removals, and moves
//! that reconstruct the native code layer. They pythonize cleanly (field names
//! match the Python `CodeGraph.apply_code_delta` applier exactly). They are
//! produced on demand by `full_code_delta()` for build-on-demand graph
//! construction; they are **not** nested into `CommitDelta` (Project 31, #2).
//!
//! Field tiers (mirrors `parity_oracle.py`): `durable_id`, `name`,
//! `qualified_name`, `kind`, `file`, `range`, `content_hash`, and `external` are
//! *structural* (parity must be exact); `name_range`/`selection_range`,
//! `content_hashes`, and `package` are *cosmetic* (downgradeable).

use std::collections::HashMap;

use crate::dto::RangeDto;

/// A node upsert in the code delta. Carries every field the legacy `SymbolNode`
/// payload carries so the applier reconstructs an identical node.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct CodeNodeDto {
    pub durable_id: String,
    /// The unqualified leaf name (structural). Module nodes use the file stem;
    /// entity nodes use the symbol name.
    pub name: String,
    pub qualified_name: String,
    /// Lowercased `SymbolKind` value: `"class_"` / `"function"` / `"module"` / …
    pub kind: String,
    pub file: String,
    pub range: RangeDto,
    /// The name (selection) range; `None` for synthetic module/external nodes.
    pub name_range: Option<RangeDto>,
    /// Hex content hash; `None` for synthetic module/external nodes.
    pub content_hash: Option<String>,
    /// Per-profile content hashes (cosmetic). Empty unless configured.
    #[serde(default)]
    pub content_hashes: HashMap<String, String>,
    /// `true` only for external stub nodes (structural).
    pub external: bool,
    /// Inferred package for external nodes (cosmetic); `None` otherwise.
    pub package: Option<String>,
}

/// A node move: same `durable_id`, unchanged body, new location only.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct CodeNodeMovedDto {
    pub durable_id: String,
    pub file: String,
    pub range: RangeDto,
    pub name_range: Option<RangeDto>,
}

/// An edge add/remove in the code delta.
///
/// The *structural* fact is the `(source_id, destination_id, kind)` relation;
/// `role` / `file` / `range` are cosmetic (carried only by reference/import
/// edges). Containment/inheritance/override edges leave them `None`.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct CodeEdgeDto {
    pub source_id: String,
    pub destination_id: String,
    /// Lowercased `EdgeKind` value:
    /// `"defines"` / `"contains"` / `"references"` / `"imports"` /
    /// `"inherits"` / `"overrides"`.
    pub kind: String,
    pub role: Option<String>,
    pub file: Option<String>,
    pub range: Option<RangeDto>,
}

/// The code delta produced inside a commit (Phase 2).
///
/// `rescan = true` marks a full (cold-start / unknown-diff) delta: every node is
/// in `nodes_upserted` and every edge in `edges_added`.
#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct CodeDeltaDto {
    pub revision: u64,
    pub rescan: bool,
    pub nodes_upserted: Vec<CodeNodeDto>,
    pub nodes_removed: Vec<String>,
    pub nodes_moved: Vec<CodeNodeMovedDto>,
    pub edges_added: Vec<CodeEdgeDto>,
    pub edges_removed: Vec<CodeEdgeDto>,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn range(sl: u32, sc: u32, el: u32, ec: u32) -> RangeDto {
        RangeDto {
            start: crate::dto::PositionDto { line: sl, column: sc },
            end: crate::dto::PositionDto { line: el, column: ec },
        }
    }

    #[test]
    fn code_delta_round_trips_through_serde_with_python_field_names() {
        let delta = CodeDeltaDto {
            revision: 7,
            rescan: true,
            nodes_upserted: vec![CodeNodeDto {
                durable_id: "01ABC".into(),
                name: "User".into(),
                qualified_name: "User".into(),
                kind: "class_".into(),
                file: "models.py".into(),
                range: range(1, 1, 3, 1),
                name_range: Some(range(1, 7, 1, 11)),
                content_hash: Some("deadbeef".into()),
                content_hashes: HashMap::new(),
                external: false,
                package: None,
            }],
            nodes_removed: vec!["01OLD".into()],
            nodes_moved: vec![CodeNodeMovedDto {
                durable_id: "01MOV".into(),
                file: "new.py".into(),
                range: range(5, 1, 6, 1),
                name_range: None,
            }],
            edges_added: vec![CodeEdgeDto {
                source_id: "<module>models.py".into(),
                destination_id: "01ABC".into(),
                kind: "defines".into(),
                role: None,
                file: None,
                range: None,
            }],
            edges_removed: vec![],
        };

        let json = serde_json::to_value(&delta).unwrap();
        // Field names must match the Python applier's expectations exactly.
        assert!(json["nodes_upserted"][0]["durable_id"].is_string());
        assert!(json["nodes_upserted"][0]["qualified_name"].is_string());
        assert!(json["nodes_upserted"][0]["name_range"]["start"]["line"].is_u64());
        assert_eq!(json["edges_added"][0]["destination_id"], "01ABC");
        assert_eq!(json["nodes_removed"][0], "01OLD");

        let back: CodeDeltaDto = serde_json::from_value(json).unwrap();
        assert_eq!(back.revision, 7);
        assert!(back.rescan);
        assert_eq!(back.nodes_upserted.len(), 1);
        assert_eq!(back.nodes_upserted[0].kind, "class_");
    }
}
