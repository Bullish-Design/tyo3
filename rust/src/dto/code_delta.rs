//! The code-layer delta wire contract (Gate 3N).
//!
//! `CodeDelta` is produced inside the native commit (under the `inner` Mutex)
//! and surfaced on `SyncResultDto`. The Python `CodeGraph` applies it purely —
//! it is a 1:1 mirror of the rustworkx node/edge structure, never re-derived
//! from the read surface. Field names match the Python Pydantic mirror in
//! `models/analysis.py` exactly so pythonize round-trips without translation.
//!
//! INVARIANT: every node is keyed by its reconciled `DurableId` (ULID), a
//! synthetic module id `<module>{file}`, or an external stub `<external>{pkg}::{name}`.

use serde::{Deserialize, Serialize};

use crate::dto::RangeDto;

/// One materialised graph node. Small structural payload only (§6.2.2) —
/// id, kind, location, content hash; never vectors or large text.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SymbolNodeDto {
    /// ULID, or `<module>{file}` / `<external>{pkg}::{name}`.
    pub durable_id: String,
    /// `SymbolKind` serialised (lowercase variant, e.g. `class_`, `import_`).
    pub kind: String,
    pub qualified_name: String,
    /// Project-relative key, matches the content-store key.
    pub file: String,
    /// Display/location only.
    pub range: RangeDto,
    /// Hex of the u128 content hash; `None` for synthetic (module/external) nodes.
    pub content_hash: Option<String>,
}

/// One typed edge between two nodes (by `DurableId`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EdgeDto {
    pub src_id: String,
    pub dst_id: String,
    /// `containment` | `references` | `imports` | `inherits` | `overrides`.
    pub kind: String,
    /// `ReferenceRole` for reference edges; `None` otherwise.
    pub role: Option<String>,
    /// The file the edge originates from (the occurrence's file for
    /// reference/import edges). `None` for structural edges (containment,
    /// inherits, overrides), matching the read-surface `EdgeData.file`.
    #[serde(default)]
    pub file: Option<String>,
    /// The occurrence range for reference/import edges; `None` otherwise.
    /// Carried so the replica's edge payload is byte-equal to the read-surface
    /// build under the strengthened parity comparator (§6.3.1).
    #[serde(default)]
    pub range: Option<RangeDto>,
}

/// A coherent, revision-stamped delta for the dirty set. `rescan == true`
/// means a full snapshot: the replica is cleared and rebuilt from it.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct CodeDelta {
    pub revision: u64,
    pub rescan: bool,
    pub nodes_upserted: Vec<SymbolNodeDto>,
    pub nodes_removed: Vec<String>,
    /// `(durable_id, new_file, new_range)` — location-only updates, no edge churn.
    pub nodes_moved: Vec<(String, String, RangeDto)>,
    pub edges_added: Vec<EdgeDto>,
    pub edges_removed: Vec<EdgeDto>,
}
