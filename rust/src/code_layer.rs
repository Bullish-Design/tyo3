//! The authoritative structural code layer (Gate 3N).
//!
//! `CodeLayer` is the "derived structural state" SPEC §3 step 5 names: small
//! per-node payloads + typed edges + a reverse-dependency index (§6.2.2,
//! §6.2.3, §4.3.4). It is held in `HeadState` and mutated only by the writer
//! under the `inner` Mutex.
//!
//! CONCURRENCY: every method here runs inside `commit_head` under the head
//! lock. There is no interior locking and no GIL interaction.
//!
//! The producer (`produce_*`) lives in `project.rs`, where it has access to the
//! engine queries (occurrences, supertypes) and the identity registry; this
//! module owns the data model, the diff, and the `CodeDelta` assembly.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use crate::dto::{CodeDelta, EdgeDto, RangeDto, SymbolNodeDto};
use crate::entity::SymbolKind;

/// Serialise a `SymbolKind` to the lowercase Python `SymbolKind` value
/// (`class_`, `import_`, …) the replica validates against.
pub fn kind_str(kind: SymbolKind) -> &'static str {
    match kind {
        SymbolKind::Module => "module",
        SymbolKind::Class => "class_",
        SymbolKind::Function => "function",
        SymbolKind::Method => "method",
        SymbolKind::Constructor => "constructor",
        SymbolKind::Variable => "variable",
        SymbolKind::Constant => "constant",
        SymbolKind::Field => "field",
        SymbolKind::Parameter => "parameter",
        SymbolKind::Property => "property",
        SymbolKind::TypeParameter => "type_parameter",
        SymbolKind::Import => "import_",
    }
}

// ── Node + edge data model ────────────────────────────────────────────────

/// One materialised structural node. Mirrors `SymbolNodeDto`.
#[derive(Debug, Clone, PartialEq)]
pub struct NodeData {
    pub durable_id: String,
    pub kind: String,
    pub qualified_name: String,
    pub file: String,
    pub range: RangeDto,
    pub content_hash: Option<String>,
}

impl NodeData {
    pub fn to_dto(&self) -> SymbolNodeDto {
        SymbolNodeDto {
            durable_id: self.durable_id.clone(),
            kind: self.kind.clone(),
            qualified_name: self.qualified_name.clone(),
            file: self.file.clone(),
            range: self.range.clone(),
            content_hash: self.content_hash.clone(),
        }
    }

    /// Identity-and-payload equality used to decide upsert vs unchanged.
    /// Range is excluded: a cosmetic move is reported via `nodes_moved`, never
    /// a remove+add (§5.5.1 / §6.2.2).
    fn payload_eq(&self, other: &NodeData) -> bool {
        self.kind == other.kind
            && self.qualified_name == other.qualified_name
            && self.file == other.file
            && self.content_hash == other.content_hash
    }
}

/// One typed edge (by `DurableId`). Eq/Hash include `file`/`range` so parallel
/// reference/import edges (same src/dst/kind but different occurrence location)
/// are distinct, as in the rustworkx multigraph. `Ord` is implemented over a
/// primitive key because `RangeDto` is not `Ord`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Edge {
    pub src: String,
    pub dst: String,
    pub kind: String,
    pub role: Option<String>,
    pub file: Option<String>,
    pub range: Option<RangeDto>,
}

impl Edge {
    pub fn to_dto(&self) -> EdgeDto {
        EdgeDto {
            src_id: self.src.clone(),
            dst_id: self.dst.clone(),
            kind: self.kind.clone(),
            role: self.role.clone(),
            file: self.file.clone(),
            range: self.range.clone(),
        }
    }

    /// A total-order key for deterministic emission. `RangeDto` is flattened to
    /// its 1-based coordinates so the whole key is `Ord`.
    fn sort_key(&self) -> (&str, &str, &str, Option<&str>, Option<&str>, Option<(u32, u32, u32, u32)>) {
        (
            self.src.as_str(),
            self.dst.as_str(),
            self.kind.as_str(),
            self.role.as_deref(),
            self.file.as_deref(),
            self.range.as_ref().map(|r| {
                (r.start.line, r.start.column, r.end.line, r.end.column)
            }),
        )
    }
}

impl PartialOrd for Edge {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Edge {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.sort_key().cmp(&other.sort_key())
    }
}

// ── CodeLayer ─────────────────────────────────────────────────────────────

/// Authoritative structural code state. Rebuilt from source on open
/// (REFINED_ARCH §8 — the code layer is not persisted).
#[derive(Debug, Default)]
pub struct CodeLayer {
    /// durable_id → node payload.
    nodes: HashMap<String, NodeData>,
    /// All typed edges.
    edges: HashSet<Edge>,
    /// Inverse of reference/import edges: dst_id → {src_id} (§4.3.4). Drives
    /// inbound revalidation and the Gate 8 subscription scope.
    reverse_deps: HashMap<String, HashSet<String>>,
    revision: u64,
}

impl CodeLayer {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn revision(&self) -> u64 {
        self.revision
    }

    /// Files a set of ids are referenced/imported by — the inbound closure used
    /// by incremental revalidation and the delta scope (§4.3.3).
    pub fn importers_of(&self, ids: &HashSet<String>) -> HashSet<String> {
        let mut out = HashSet::new();
        for id in ids {
            if let Some(srcs) = self.reverse_deps.get(id) {
                out.extend(srcs.iter().cloned());
            }
        }
        out
    }

    fn add_edge_internal(&mut self, e: Edge) {
        if e.kind == "references" || e.kind == "imports" {
            self.reverse_deps
                .entry(e.dst.clone())
                .or_default()
                .insert(e.src.clone());
        }
        self.edges.insert(e);
    }

    fn remove_edge_internal(&mut self, e: &Edge) {
        if e.kind == "references" || e.kind == "imports" {
            if let Some(srcs) = self.reverse_deps.get_mut(&e.dst) {
                srcs.remove(&e.src);
                if srcs.is_empty() {
                    self.reverse_deps.remove(&e.dst);
                }
            }
        }
        self.edges.remove(e);
    }

    /// Replace the whole layer with a freshly produced full build and emit a
    /// `rescan` delta carrying every node and edge (Steps 1–4, full build).
    pub fn full_build(&mut self, nodes: Vec<NodeData>, edges: Vec<Edge>, revision: u64) -> CodeDelta {
        self.nodes.clear();
        self.edges.clear();
        self.reverse_deps.clear();
        self.revision = revision;

        // Deterministic emission order.
        let mut node_map: BTreeMap<(String, String, String), NodeData> = BTreeMap::new();
        for n in nodes {
            let key = (n.file.clone(), n.qualified_name.clone(), n.kind.clone());
            self.nodes.insert(n.durable_id.clone(), n.clone());
            node_map.insert(key, n);
        }
        let mut edge_set: BTreeSet<Edge> = BTreeSet::new();
        for e in edges {
            self.add_edge_internal(e.clone());
            edge_set.insert(e);
        }

        CodeDelta {
            revision,
            rescan: true,
            nodes_upserted: node_map.values().map(NodeData::to_dto).collect(),
            nodes_removed: Vec::new(),
            nodes_moved: Vec::new(),
            edges_added: edge_set.iter().map(Edge::to_dto).collect(),
            edges_removed: Vec::new(),
        }
    }

    /// Apply a freshly produced node/edge set for the *affected* files and emit
    /// an incremental `CodeDelta` (§6.3). `removed_ids` are nodes owned by the
    /// dirty set that no longer exist; their incident edges are dropped too.
    pub fn incremental(
        &mut self,
        removed_ids: HashSet<String>,
        new_nodes: Vec<NodeData>,
        new_edges: Vec<Edge>,
        moved: Vec<(String, String, RangeDto)>,
        revision: u64,
    ) -> CodeDelta {
        let mut nodes_removed: BTreeSet<String> = BTreeSet::new();
        let mut edges_removed: BTreeSet<Edge> = BTreeSet::new();
        let mut nodes_upserted: BTreeMap<(String, String, String), SymbolNodeDto> = BTreeMap::new();
        let mut edges_added: BTreeSet<Edge> = BTreeSet::new();

        // 1. Remove dirty nodes and their incident edges.
        let removed_set: HashSet<String> = removed_ids;
        for id in &removed_set {
            self.nodes.remove(id);
            nodes_removed.insert(id.clone());
        }
        let incident: Vec<Edge> = self
            .edges
            .iter()
            .filter(|e| removed_set.contains(&e.src) || removed_set.contains(&e.dst))
            .cloned()
            .collect();
        for e in incident {
            self.remove_edge_internal(&e);
            edges_removed.insert(e);
        }

        // 2. Upsert re-extracted nodes (skip pure no-ops).
        for n in new_nodes {
            let changed = match self.nodes.get(&n.durable_id) {
                Some(prev) => !prev.payload_eq(&n),
                None => true,
            };
            self.nodes.insert(n.durable_id.clone(), n.clone());
            if changed {
                let key = (n.file.clone(), n.qualified_name.clone(), n.kind.clone());
                nodes_upserted.insert(key, n.to_dto());
            }
        }

        // 3. Add fresh edges (dedup against survivors).
        for e in new_edges {
            if !self.edges.contains(&e) {
                edges_added.insert(e.clone());
            }
            self.add_edge_internal(e);
        }

        self.revision = revision;

        CodeDelta {
            revision,
            rescan: false,
            nodes_upserted: nodes_upserted.into_values().collect(),
            nodes_removed: nodes_removed.into_iter().collect(),
            nodes_moved: moved,
            edges_added: edges_added.iter().map(Edge::to_dto).collect(),
            edges_removed: edges_removed.iter().map(Edge::to_dto).collect(),
        }
    }

    /// Produce a full `CodeDelta` describing the current layer (for cold-start
    /// and snapshot builds — Step 6/7). Marked `rescan` so the replica replaces.
    pub fn full_delta(&self) -> CodeDelta {
        let mut node_map: BTreeMap<(String, String, String), &NodeData> = BTreeMap::new();
        for n in self.nodes.values() {
            node_map.insert((n.file.clone(), n.qualified_name.clone(), n.kind.clone()), n);
        }
        let mut edge_set: BTreeSet<&Edge> = BTreeSet::new();
        for e in &self.edges {
            edge_set.insert(e);
        }
        CodeDelta {
            revision: self.revision,
            rescan: true,
            nodes_upserted: node_map.values().map(|n| n.to_dto()).collect(),
            nodes_removed: Vec::new(),
            nodes_moved: Vec::new(),
            edges_added: edge_set.into_iter().map(Edge::to_dto).collect(),
            edges_removed: Vec::new(),
        }
    }
}
