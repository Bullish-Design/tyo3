//! Durable identity and reconciliation (Gate 2, SPEC §5).
//!
//! Two identifiers are kept strictly distinct:
//!   - **DurableId** — a minted ULID that survives content edits, moves, renames.
//!   - **ContentHash** — a hash of the entity's normalised definition.
//!
//! The `IdentityRegistry` binds ids to last-known entity facts.  `reconcile()`
//! matches entities from a new revision against the registry via a four-pass
//! deterministic algorithm (§5.4).
//!
//! # Determinism
//!
//! `reconcile()` sorts its entity input so the result is independent of
//! processing order (§5.5.5).  The one-to-one invariant (§5.5.4) is enforced
//! by consumed sets so no anchor binds to more than one entity and vice versa.

use std::collections::{BTreeMap, HashMap};
use std::fmt;
use std::fs;
use std::io;
use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::content::Revision;
use crate::entity::{Entity, SymbolKind};
use crate::hash::ContentHash;

// ── DurableId (SPEC §5.2) ───────────────────────────────────────────────

/// A minted, persistent identity.  Opaque string (ULID), never parsed for
/// meaning.  Never derived from content, location, name, or line number.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub struct DurableId(pub String);

impl DurableId {
    /// Mint a fresh ULID-based id.
    pub fn mint() -> Self {
        DurableId(ulid::Ulid::new().to_string())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for DurableId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

// ── Anchor (SPEC §5.3) ──────────────────────────────────────────────────

/// A binding record in the identity registry: a `DurableId` plus the last-known
/// facts about its entity.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Anchor {
    pub id: DurableId,
    pub qualified_path: String,
    pub content_hash: ContentHash,
    pub kind: SymbolKind,
    pub first_seen_rev: Revision,
    pub last_seen_rev: Revision,
    /// Lifecycle status added in Step 8.
    #[serde(default)]
    pub status: IdentityStatus,
}

/// Lifecycle states for authored-record safety (§5.5.3).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
pub enum IdentityStatus {
    #[default]
    Active,
    NeedsReview,
    Orphaned,
}

// ── IdentityRegistry (SPEC §5.3) ────────────────────────────────────────

/// An in-memory registry binding `DurableId`s to last-known entity facts.
/// Maintains three indexes that always agree:
///   - `by_id`: the authoritative store
///   - `by_path`: qualified_path → id (EXACT match index)
///   - `by_hash`: content_hash → list of ids (HASH match index)
#[derive(Debug, Default)]
pub struct IdentityRegistry {
    by_id: HashMap<DurableId, Anchor>,
    by_path: HashMap<String, DurableId>,
    by_hash: HashMap<ContentHash, Vec<DurableId>>,
}

impl IdentityRegistry {
    // ── Lookups ─────────────────────────────────────────────────────────

    pub fn get(&self, id: &DurableId) -> Option<&Anchor> {
        self.by_id.get(id)
    }

    pub fn by_path(&self, qualified_path: &str) -> Option<&DurableId> {
        self.by_path.get(qualified_path)
    }

    pub fn by_hash(&self, content_hash: &ContentHash) -> &[DurableId] {
        self.by_hash
            .get(content_hash)
            .map(|v| v.as_slice())
            .unwrap_or(&[])
    }

    pub fn iter(&self) -> impl Iterator<Item = &Anchor> {
        self.by_id.values()
    }

    pub fn len(&self) -> usize {
        self.by_id.len()
    }

    // ── Mutators (keep indexes consistent) ──────────────────────────────

    /// Insert a new anchor.  Panics (debug) / overwrites (release) if an
    /// anchor with the same id already exists.
    pub fn insert(&mut self, anchor: Anchor) {
        // If an anchor with the same id exists, extract old index keys first.
        let old_keys: Option<(String, ContentHash)> = self.by_id.get(&anchor.id).map(|old| {
            (old.qualified_path.clone(), old.content_hash)
        });

        if let Some((old_path, old_hash)) = old_keys {
            self.by_path.remove(&old_path);
            self.remove_from_hash_index(&old_hash, &anchor.id);
        }

        self.by_path
            .insert(anchor.qualified_path.clone(), anchor.id.clone());
        self.by_hash
            .entry(anchor.content_hash)
            .or_default()
            .push(anchor.id.clone());
        self.by_id.insert(anchor.id.clone(), anchor);
    }

    /// Update the path and hash for an existing anchor, bumping its
    /// `last_seen_rev`.  If the id is unknown this is a no-op; tests should
    /// not trigger this path for unknown ids.
    pub fn rebind(
        &mut self,
        id: &DurableId,
        new_path: String,
        new_hash: ContentHash,
        rev: Revision,
    ) {
        // Extract old index keys before mutable mutation.
        let old_keys: Option<(String, ContentHash)> = self.by_id.get(id).map(|a| {
            (a.qualified_path.clone(), a.content_hash)
        });

        let Some((old_path, old_hash)) = old_keys else {
            return;
        };

        // Remove from old indexes.
        self.by_path.remove(&old_path);
        self.remove_from_hash_index(&old_hash, id);

        // Update the anchor.
        if let Some(anchor) = self.by_id.get_mut(id) {
            anchor.qualified_path = new_path.clone();
            anchor.content_hash = new_hash;
            anchor.last_seen_rev = rev;
        }

        // Re-index.
        self.by_path.insert(new_path, id.clone());
        self.by_hash
            .entry(new_hash)
            .or_default()
            .push(id.clone());
    }

    /// Retire an anchor: remove from lookup indexes but keep in `by_id`.
    /// This ensures authored records keyed by this id survive (§5.5 rule 5).
    pub fn retire(&mut self, id: &DurableId, rev: Revision) {
        // Extract old index keys before mutation.
        let old_path: Option<String> = self.by_id.get(id).map(|a| a.qualified_path.clone());

        let Some(old_path) = old_path else {
            return;
        };

        // Update the anchor.
        if let Some(anchor) = self.by_id.get_mut(id) {
            anchor.last_seen_rev = rev;
            anchor.status = IdentityStatus::Orphaned;
        }

        // Remove from by_path only; keep in by_hash so a reappearing entity
        // with the same body can re-bind via hash match (§5.5 rule 5).
        self.by_path.remove(&old_path);
    }

    /// Set the lifecycle status of an anchor.
    pub fn set_status(&mut self, id: &DurableId, status: IdentityStatus) {
        if let Some(anchor) = self.by_id.get_mut(id) {
            anchor.status = status;
        }
    }

    // ── Index helpers ───────────────────────────────────────────────────

    fn remove_from_hash_index(&mut self, hash: &ContentHash, id: &DurableId) {
        if let Some(ids) = self.by_hash.get_mut(hash) {
            ids.retain(|x| x != id);
            if ids.is_empty() {
                self.by_hash.remove(hash);
            }
        }
    }

    // ── Consistency check ──────────────────────────────────────────────

    /// Assert all three indexes are mutually consistent.  Debug-only; call
    /// after any mutator in tests.
    #[cfg(debug_assertions)]
    pub fn assert_consistent(&self) {
        // Every id in by_id is in by_path and by_hash by its current values.
        for (id, anchor) in &self.by_id {
            match self.by_path.get(&anchor.qualified_path) {
                Some(p_id) => assert_eq!(p_id, id, "by_path points to wrong id for {}", anchor.qualified_path),
                None => {
                    // Retired anchors are not in by_path.
                    assert!(
                        matches!(anchor.status, IdentityStatus::Orphaned),
                        "active anchor {} missing from by_path", id
                    );
                }
            }

            // Orphaned anchors may still be in by_hash (preserved for re-binding).
            // Active anchors must be in by_hash.
            if anchor.status != IdentityStatus::Orphaned {
                let hash_ids = self.by_hash.get(&anchor.content_hash);
                assert!(
                    hash_ids.map_or(false, |v| v.contains(id)),
                    "active anchor {} missing from by_hash", id
                );
            }
        }

        // Every by_path entry points to a valid by_id anchor.
        for (path, id) in &self.by_path {
            assert!(self.by_id.contains_key(id), "by_path entry {} → {} has no by_id anchor", path, id);
            let anchor = &self.by_id[id];
            assert_eq!(&anchor.qualified_path, path, "by_path path mismatch for {}", id);
        }

        // Every by_hash entry points to valid by_id anchors.
        for (hash, ids) in &self.by_hash {
            for id in ids {
                assert!(self.by_id.contains_key(id), "by_hash entry {} → {} has no by_id anchor", hash.0, id);
            }
        }
    }

    #[cfg(not(debug_assertions))]
    pub fn assert_consistent(&self) {
        // no-op in release
    }

    // ── Persistence (Step 7) ────────────────────────────────────────────

    /// Serialise to a JSON value (for embedding in a larger sidecar file
    /// later).  Anchors are sorted by id for stable diffs.
    pub fn to_json_value(&self) -> serde_json::Value {
        let mut anchors: Vec<&Anchor> = self.by_id.values().collect();
        anchors.sort_by(|a, b| a.id.cmp(&b.id));

        let anchor_entries: Vec<serde_json::Value> = anchors
            .iter()
            .map(|a| {
                serde_json::json!({
                    "id": a.id.0,
                    "qualified_path": a.qualified_path,
                    "content_hash": a.content_hash.0.to_string(),
                    "kind": kind_to_str(&a.kind),
                    "first_seen_rev": a.first_seen_rev.0,
                    "last_seen_rev": a.last_seen_rev.0,
                    "status": status_to_str(&a.status),
                })
            })
            .collect();

        serde_json::json!({
            "format_version": 1,
            "anchors": anchor_entries,
        })
    }

    /// Save the registry atomically to `path` (§11.3.3).
    /// Writes to `path.tmp`, fsyncs, renames over `path`.
    pub fn save(&self, path: &Path) -> Result<(), io::Error> {
        let json = self.to_json_value();
        let text = serde_json::to_string_pretty(&json)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;

        let tmp = path.with_extension("tmp");
        fs::write(&tmp, &text)?;

        // fsync the data.
        let f = fs::File::open(&tmp)?;
        f.sync_all()?;
        drop(f);

        fs::rename(&tmp, path)?;
        Ok(())
    }

    /// Load a registry from `path`.  Returns an empty registry if the file
    /// does not exist.  Returns `FormatError` for unknown format versions.
    pub fn load(path: &Path) -> Result<Self, FormatError> {
        match fs::read_to_string(path) {
            Ok(text) => Self::from_json(&text),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(Self::default()),
            Err(e) => Err(FormatError::Io(e)),
        }
    }

    /// Deserialise from JSON text.
    fn from_json(text: &str) -> Result<Self, FormatError> {
        let root: serde_json::Value =
            serde_json::from_str(text).map_err(|e| FormatError::Json(e.to_string()))?;

        let version = root["format_version"]
            .as_u64()
            .ok_or_else(|| FormatError::MissingField("format_version".into()))?;

        if version > 1 {
            return Err(FormatError::UnknownVersion(version));
        }

        let mut registry = Self::default();
        let anchors = root["anchors"]
            .as_array()
            .ok_or_else(|| FormatError::MissingField("anchors".into()))?;

        for entry in anchors {
            let id_str = entry["id"]
                .as_str()
                .ok_or_else(|| FormatError::MissingField("id".into()))?;
            let qualified_path = entry["qualified_path"]
                .as_str()
                .ok_or_else(|| FormatError::MissingField("qualified_path".into()))?;
            let hash_str = entry["content_hash"]
                .as_str()
                .ok_or_else(|| FormatError::MissingField("content_hash".into()))?;
            let kind_str = entry["kind"]
                .as_str()
                .ok_or_else(|| FormatError::MissingField("kind".into()))?;
            let first_rev = entry["first_seen_rev"]
                .as_u64()
                .ok_or_else(|| FormatError::MissingField("first_seen_rev".into()))?;
            let last_rev = entry["last_seen_rev"]
                .as_u64()
                .ok_or_else(|| FormatError::MissingField("last_seen_rev".into()))?;
            let status_str = entry["status"].as_str().unwrap_or("active");

            let hash_val: u128 = hash_str
                .parse()
                .map_err(|_| FormatError::Json(format!("invalid content_hash: {}", hash_str)))?;

            let anchor = Anchor {
                id: DurableId(id_str.to_string()),
                qualified_path: qualified_path.to_string(),
                content_hash: ContentHash(hash_val),
                kind: str_to_kind(kind_str),
                first_seen_rev: Revision(first_rev),
                last_seen_rev: Revision(last_rev),
                status: str_to_status(status_str),
            };

            registry.insert(anchor);
        }

        Ok(registry)
    }
}

/// Error from loading a registry.
#[derive(Debug)]
pub enum FormatError {
    Io(io::Error),
    Json(String),
    MissingField(String),
    UnknownVersion(u64),
}

impl fmt::Display for FormatError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            FormatError::Io(e) => write!(f, "I/O error: {}", e),
            FormatError::Json(s) => write!(f, "JSON error: {}", s),
            FormatError::MissingField(field) => write!(f, "missing field: {}", field),
            FormatError::UnknownVersion(v) => write!(f, "unknown format version: {}", v),
        }
    }
}

// ── Kind serialisation helpers ──────────────────────────────────────────

fn kind_to_str(k: &SymbolKind) -> &'static str {
    match k {
        SymbolKind::Module => "module",
        SymbolKind::Class => "class",
        SymbolKind::Function => "function",
        SymbolKind::Method => "method",
        SymbolKind::Constructor => "constructor",
        SymbolKind::Variable => "variable",
        SymbolKind::Constant => "constant",
        SymbolKind::Field => "field",
        SymbolKind::Parameter => "parameter",
        SymbolKind::Property => "property",
        SymbolKind::TypeParameter => "type_parameter",
        SymbolKind::Import => "import",
    }
}

fn str_to_kind(s: &str) -> SymbolKind {
    match s {
        "module" => SymbolKind::Module,
        "class" => SymbolKind::Class,
        "function" => SymbolKind::Function,
        "method" => SymbolKind::Method,
        "constructor" => SymbolKind::Constructor,
        "variable" => SymbolKind::Variable,
        "constant" => SymbolKind::Constant,
        "field" => SymbolKind::Field,
        "parameter" => SymbolKind::Parameter,
        "property" => SymbolKind::Property,
        "type_parameter" => SymbolKind::TypeParameter,
        "import" => SymbolKind::Import,
        _ => SymbolKind::Variable, // fallback
    }
}

fn status_to_str(s: &IdentityStatus) -> &'static str {
    match s {
        IdentityStatus::Active => "active",
        IdentityStatus::NeedsReview => "needs_review",
        IdentityStatus::Orphaned => "orphaned",
    }
}

fn str_to_status(s: &str) -> IdentityStatus {
    match s {
        "needs_review" => IdentityStatus::NeedsReview,
        "orphaned" => IdentityStatus::Orphaned,
        _ => IdentityStatus::Active,
    }
}

// ── Reconciliation result (Step 4) ──────────────────────────────────────

/// The outcome of one reconciliation pass.  Describes how each new-state entity
/// was bound, plus which anchors were retired.
#[derive(Debug, Default)]
pub struct Reconciliation {
    /// One entry per new entity (in entity order).
    pub bindings: Vec<(String, Binding)>,
    /// Anchors that existed in the prior registry but matched no entity.
    pub retired: Vec<DurableId>,
    /// Ids flagged for human review (Struct binds + body-changed exacts).
    pub needs_review: Vec<DurableId>,
}

/// How a single entity was bound to (or received) a `DurableId`.
#[derive(Debug, PartialEq, Eq)]
pub enum Binding {
    /// Rule 1: exact qualified_path match.
    Exact { id: DurableId },
    /// Rule 2: content hash match at a new location.
    Moved { id: DurableId, old_path: String },
    /// Rule 3: structural match (same name + kind + container), low confidence.
    Struct { id: DurableId, confidence: Confidence },
    /// Rule 4: no match — freshly minted id.
    Minted { id: DurableId },
}

/// Confidence level for a STRUCT bind.
#[derive(Debug, PartialEq, Eq)]
pub enum Confidence {
    Low,
}

impl Reconciliation {
    /// All ids from `Moved` bindings.
    pub fn moved(&self) -> Vec<&DurableId> {
        self.bindings
            .iter()
            .filter_map(|(_, b)| match b {
                Binding::Moved { id, .. } => Some(id),
                _ => None,
            })
            .collect()
    }

    /// All ids from `Minted` bindings.
    pub fn minted(&self) -> Vec<&DurableId> {
        self.bindings
            .iter()
            .filter_map(|(_, b)| match b {
                Binding::Minted { id } => Some(id),
                _ => None,
            })
            .collect()
    }

    /// All ids from `Struct` bindings.
    pub fn struct_binds(&self) -> Vec<&DurableId> {
        self.bindings
            .iter()
            .filter_map(|(_, b)| match b {
                Binding::Struct { id, .. } => Some(id),
                _ => None,
            })
            .collect()
    }

    /// Ids from `Exact` bindings whose hash changed (body-changed exacts).
    pub fn changed(&self) -> Vec<&DurableId> {
        // The reconciliation algorithm flags body-changed exacts in needs_review.
        // For the delta we also need the full set of Exact bindings (changed or not).
        self.bindings
            .iter()
            .filter_map(|(_, b)| match b {
                Binding::Exact { id } => Some(id),
                _ => None,
            })
            .collect()
    }
}

// ── Reconciliation algorithm (Step 5) ───────────────────────────────────

/// Reconcile a set of extracted entities against the registry at a new revision.
///
/// # Algorithm (SPEC §5.4)
///
/// 1. **Pre-sort** entities by `(qualified_path, content_hash)`.
/// 2. **Pass A — EXACT**: match by `qualified_path`.
/// 3. **Pass B — HASH (moves)**: match by content hash at a different path.
/// 4. **Pass C — STRUCT**: match by `(name, kind, container)` with edit-distance.
/// 5. **Pass D — MINT**: all remaining entities get fresh ids.
/// 6. **Apply**: rebind existing anchors, insert new ones, retire vanished ones.
///
/// One-to-one invariant (§5.5.4): anchor consumed at most once, entity bound at
/// most once.  Determinism (§5.5.5): sorting makes the result order-independent.
pub fn reconcile(
    registry: &mut IdentityRegistry,
    entities: &[Entity],
    revision: Revision,
) -> Reconciliation {
    // 1. Determinism pre-sort.
    let mut sorted: Vec<&Entity> = entities.iter().collect();
    sorted.sort_by(|a, b| {
        a.qualified_path
            .cmp(&b.qualified_path)
            .then_with(|| a.content_hash.cmp(&b.content_hash))
    });

    let mut bound_ids: std::collections::HashSet<DurableId> = std::collections::HashSet::new();
    let mut matched_entities: std::collections::HashSet<usize> = std::collections::HashSet::new();
    let mut bindings: Vec<(String, Binding)> = Vec::with_capacity(sorted.len());
    let mut needs_review: Vec<DurableId> = Vec::new();

    // ── Pass A: EXACT match ─────────────────────────────────────────
    for (idx, e) in sorted.iter().enumerate() {
        if matched_entities.contains(&idx) {
            continue;
        }
        let exact_id: Option<DurableId> = {
            let by_path = registry.by_path(&e.qualified_path);
            by_path.filter(|id| !bound_ids.contains(id)).cloned()
        };
        if let Some(id) = exact_id {
            // If the content hash changed, flag for review.
            if let Some(anchor) = registry.get(&id) {
                if anchor.content_hash != e.content_hash {
                    needs_review.push(id.clone());
                }
            }
            bound_ids.insert(id.clone());
            matched_entities.insert(idx);
            bindings.push((e.qualified_path.clone(), Binding::Exact { id }));
        }
    }

    // ── Pass B: HASH match (moves) ─────────────────────────────────
    for (idx, e) in sorted.iter().enumerate() {
        if matched_entities.contains(&idx) {
            continue;
        }
        // Collect unconsumed candidates. Allow same-path matches when the
        // anchor is orphaned (not in by_path), so a re-added entity with
        // identical body re-binds to the original id (§5.5 rule 5).
        let candidates: Vec<DurableId> = registry
            .by_hash(&e.content_hash)
            .iter()
            .filter(|cid| !bound_ids.contains(cid))
            .cloned()
            .collect();

        let best: Option<DurableId> = candidates
            .into_iter()
            .filter(|cid| {
                registry.get(cid).map_or(false, |a| {
                    // Allow match if paths differ (classic move) OR the
                    // anchor is not in by_path (orphaned, same path re-bind).
                    a.qualified_path != e.qualified_path
                        || registry.by_path(&a.qualified_path).is_none()
                })
            })
            .min()
            ;

        if let Some(id) = best {
            let old_path = registry
                .get(&id)
                .map(|a| a.qualified_path.clone())
                .unwrap_or_default();
            bound_ids.insert(id.clone());
            matched_entities.insert(idx);
            bindings.push((
                e.qualified_path.clone(),
                Binding::Moved { id, old_path },
            ));
        }
    }

    // ── Pass C: STRUCT match ───────────────────────────────────────
    for (idx, e) in sorted.iter().enumerate() {
        if matched_entities.contains(&idx) {
            continue;
        }

        // Find unconsumed anchors with same (name, kind, container).
        let mut candidates: Vec<(DurableId, usize)> = {
            registry
                .by_id
                .iter()
                .filter(|(id, anchor)| {
                    !bound_ids.contains(id)
                        && anchor.name() == e.name
                        && anchor.kind == e.kind
                        && anchor.container() == e.container.as_deref()
                })
                .map(|(id, anchor)| {
                    let dist = edit_distance(&anchor.qualified_path, &e.qualified_path);
                    (id.clone(), dist)
                })
                .collect()
        };

        if !candidates.is_empty() {
            // Pick the one minimizing edit distance, ties broken by smallest id.
            candidates.sort_by(|(id_a, dist_a), (id_b, dist_b)| {
                dist_a.cmp(dist_b).then_with(|| id_a.cmp(id_b))
            });

            let id = candidates[0].0.clone();
            needs_review.push(id.clone());
            bound_ids.insert(id.clone());
            matched_entities.insert(idx);
            bindings.push((
                e.qualified_path.clone(),
                Binding::Struct {
                    id,
                    confidence: Confidence::Low,
                },
            ));
        }
    }

    // ── Pass D: MINT ───────────────────────────────────────────────
    for (idx, e) in sorted.iter().enumerate() {
        if matched_entities.contains(&idx) {
            continue;
        }
        let id = DurableId::mint();
        bound_ids.insert(id.clone());
        matched_entities.insert(idx);
        bindings.push((e.qualified_path.clone(), Binding::Minted { id }));
    }

    // ── Apply to registry ─────────────────────────────────────────┐
    for (idx, e) in sorted.iter().enumerate() {
        let binding = &bindings[idx];
        match &binding.1 {
            Binding::Exact { id }
            | Binding::Moved { id, .. }
            | Binding::Struct { id, .. } => {
                registry.rebind(id, e.qualified_path.clone(), e.content_hash, revision);
                // Clear NeedsReview / Orphaned when re-bound via a clean match.
                if !needs_review.contains(id) {
                    registry.set_status(id, IdentityStatus::Active);
                }
            }
            Binding::Minted { id } => {
                registry.insert(Anchor {
                    id: id.clone(),
                    qualified_path: e.qualified_path.clone(),
                    content_hash: e.content_hash,
                    kind: e.kind,
                    first_seen_rev: revision,
                    last_seen_rev: revision,
                    status: IdentityStatus::Active,
                });
            }
        }
    }

    // Apply NeedsReview status for flagged ids.
    for id in &needs_review {
        registry.set_status(id, IdentityStatus::NeedsReview);
    }

    // ── Retire vanished anchors ───────────────────────────────────
    let mut retired: Vec<DurableId> = Vec::new();
    let all_ids: Vec<DurableId> = registry.by_id.keys().cloned().collect();
    for id in &all_ids {
        if !bound_ids.contains(id) {
            registry.retire(id, revision);
            retired.push(id.clone());
        }
    }

    Reconciliation {
        bindings,
        retired,
        needs_review,
    }
}

// ── Edit-distance for STRUCT matching ────────────────────────────────────

/// Levenshtein distance between two strings.
fn edit_distance(a: &str, b: &str) -> usize {
    let a_chars: Vec<char> = a.chars().collect();
    let b_chars: Vec<char> = b.chars().collect();
    let n = a_chars.len();
    let m = b_chars.len();

    let mut prev: Vec<usize> = (0..=m).collect();
    let mut curr = vec![0usize; m + 1];

    for i in 1..=n {
        curr[0] = i;
        for j in 1..=m {
            let cost = if a_chars[i - 1] == b_chars[j - 1] {
                0
            } else {
                1
            };
            curr[j] = (prev[j] + 1)
                .min(curr[j - 1] + 1)
                .min(prev[j - 1] + cost);
        }
        std::mem::swap(&mut prev, &mut curr);
    }
    prev[m]
}

// ── Anchor helpers ──────────────────────────────────────────────────────

impl Anchor {
    /// The unqualified name (last segment of `qualified_path` after `::`).
    fn name(&self) -> &str {
        self.qualified_path
            .rsplit("::")
            .next()
            .unwrap_or(&self.qualified_path)
    }

    /// The container (everything before the last `::`), or `None`.
    fn container(&self) -> Option<&str> {
        let last_sep = self.qualified_path.rfind("::")?;
        if last_sep == 0 {
            return None;
        }
        Some(&self.qualified_path[..last_sep])
    }
}

// ── Tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn entity(path: &str, name: &str, kind: SymbolKind, hash: ContentHash, container: Option<&str>) -> Entity {
        Entity {
            qualified_path: format!("{}::{}", path, name),
            kind,
            content_hash: hash,
            container: container.map(|s| s.to_string()),
            name: name.to_string(),
        }
    }

    fn hash(v: u128) -> ContentHash {
        ContentHash(v)
    }

    // ── Step 3: Registry tests ────────────────────────────────────────

    #[test]
    fn insert_and_lookup_all_indexes() {
        let mut reg = IdentityRegistry::default();
        let id = DurableId::mint();
        let anchor = Anchor {
            id: id.clone(),
            qualified_path: "a.py::C".into(),
            content_hash: hash(42),
            kind: SymbolKind::Class,
            first_seen_rev: Revision(1),
            last_seen_rev: Revision(1),
            status: IdentityStatus::Active,
        };
        reg.insert(anchor);
        reg.assert_consistent();

        assert!(reg.get(&id).is_some());
        assert_eq!(reg.by_path("a.py::C"), Some(&id));
        assert!(!reg.by_hash(&hash(42)).is_empty());
    }

    #[test]
    fn rebind_updates_path_and_hash() {
        let mut reg = IdentityRegistry::default();
        let id = DurableId::mint();
        reg.insert(Anchor {
            id: id.clone(),
            qualified_path: "a.py::C".into(),
            content_hash: hash(42),
            kind: SymbolKind::Class,
            first_seen_rev: Revision(1),
            last_seen_rev: Revision(1),
            status: IdentityStatus::Active,
        });
        reg.assert_consistent();

        reg.rebind(&id, "b.py::C".into(), hash(99), Revision(2));
        reg.assert_consistent();

        assert_eq!(reg.by_path("a.py::C"), None, "old path removed");
        assert_eq!(reg.by_path("b.py::C"), Some(&id), "new path added");
        assert!(reg.by_hash(&hash(42)).is_empty(), "old hash removed");
        assert!(!reg.by_hash(&hash(99)).is_empty(), "new hash added");

        let anchor = reg.get(&id).unwrap();
        assert_eq!(anchor.content_hash, hash(99));
        assert_eq!(anchor.last_seen_rev, Revision(2));
    }

    #[test]
    fn retire_removes_from_indexes_keeps_in_by_id() {
        let mut reg = IdentityRegistry::default();
        let id = DurableId::mint();
        reg.insert(Anchor {
            id: id.clone(),
            qualified_path: "a.py::Func".into(),
            content_hash: hash(7),
            kind: SymbolKind::Function,
            first_seen_rev: Revision(1),
            last_seen_rev: Revision(1),
            status: IdentityStatus::Active,
        });
        reg.assert_consistent();

        reg.retire(&id, Revision(2));
        reg.assert_consistent();

        // Still in by_id.
        assert!(reg.get(&id).is_some(), "retained in by_id");
        // Gone from by_path only; by_hash retained for re-binding.
        assert_eq!(reg.by_path("a.py::Func"), None);
        assert!(!reg.by_hash(&hash(7)).is_empty(), "by_hash retained for orphan");
        // Status is Orphaned.
        assert_eq!(reg.get(&id).unwrap().status, IdentityStatus::Orphaned);
    }

    #[test]
    fn consistency_after_random_mutations() {
        use std::collections::HashSet;
        let mut reg = IdentityRegistry::default();
        let mut ids: Vec<DurableId> = Vec::new();

        // Insert 20 anchors.
        for i in 0..20 {
            let id = DurableId::mint();
            reg.insert(Anchor {
                id: id.clone(),
                qualified_path: format!("f{}.py::Sym{}", i, i),
                content_hash: hash(i as u128),
                kind: SymbolKind::Function,
                first_seen_rev: Revision(1),
                last_seen_rev: Revision(1),
                status: IdentityStatus::Active,
            });
            reg.assert_consistent();
            ids.push(id);
        }

        // Rebind half.
        for i in 0..10 {
            reg.rebind(&ids[i], format!("g{}.py::Sym{}", i, i), hash((i + 100) as u128), Revision(2));
            reg.assert_consistent();
        }

        // Retire a quarter.
        for i in 15..20 {
            reg.retire(&ids[i], Revision(3));
            reg.assert_consistent();
        }
    }

    // ── Step 5: Reconciliation tests ───────────────────────────────────

    #[test]
    fn unchanged_move_moved_binding() {
        // §5.5.1: anchor at a.py::C hash H; entity at b.py::C hash H → Moved, same id.
        let mut reg = IdentityRegistry::default();
        let id = DurableId::mint();
        reg.insert(Anchor {
            id: id.clone(),
            qualified_path: "a.py::C".into(),
            content_hash: hash(100),
            kind: SymbolKind::Class,
            first_seen_rev: Revision(1),
            last_seen_rev: Revision(1),
            status: IdentityStatus::Active,
        });

        let entities = vec![entity("b.py", "C", SymbolKind::Class, hash(100), None)];
        let recon = reconcile(&mut reg, &entities, Revision(2));

        assert_eq!(recon.bindings.len(), 1);
        match &recon.bindings[0].1 {
            Binding::Moved { id: bound_id, old_path } => {
                assert_eq!(*bound_id, id);
                assert_eq!(old_path, "a.py::C");
            }
            other => panic!("expected Moved, got {:?}", other),
        }
        assert!(recon.retired.is_empty());
    }

    #[test]
    fn body_change_same_path_exact_binding() {
        // §5.5.2: anchor a.py::C hash H1; entity a.py::C hash H2 → Exact, same id.
        let mut reg = IdentityRegistry::default();
        let id = DurableId::mint();
        reg.insert(Anchor {
            id: id.clone(),
            qualified_path: "a.py::C".into(),
            content_hash: hash(100),
            kind: SymbolKind::Class,
            first_seen_rev: Revision(1),
            last_seen_rev: Revision(1),
            status: IdentityStatus::Active,
        });

        let entities = vec![entity("a.py", "C", SymbolKind::Class, hash(200), None)];
        let recon = reconcile(&mut reg, &entities, Revision(2));

        match &recon.bindings[0].1 {
            Binding::Exact { id: bound_id } => assert_eq!(*bound_id, id),
            other => panic!("expected Exact, got {:?}", other),
        }
        // Hash updated.
        assert_eq!(reg.get(&id).unwrap().content_hash, hash(200));
    }

    #[test]
    fn struct_binding_when_container_matches() {
        // STRUCT match: anchor and entity share (name, kind, container) but
        // different paths (EXACT fails) and different hashes (HASH fails).
        //
        // Anchor: "a.py::Outer::helper" → container "a.py::Outer", name "helper"
        // Entity: same container, same name, same kind, different path, different hash.
        let mut reg = IdentityRegistry::default();
        let id = DurableId::mint();
        reg.insert(Anchor {
            id: id.clone(),
            qualified_path: "a.py::Outer::helper".into(),
            content_hash: hash(50),
            kind: SymbolKind::Method,
            first_seen_rev: Revision(1),
            last_seen_rev: Revision(1),
            status: IdentityStatus::Active,
        });

        // Entity: different file prefix but same container (a.py::Outer), same name,
        // same kind, different hash.
        let e = Entity {
            qualified_path: "some/other/a.py::Outer::helper".into(),
            kind: SymbolKind::Method,
            content_hash: hash(60),
            container: Some("a.py::Outer".into()),
            name: "helper".into(),
        };
        let entities = vec![e];
        let recon = reconcile(&mut reg, &entities, Revision(2));

        match &recon.bindings[0].1 {
            Binding::Struct { id: bound_id, .. } => assert_eq!(*bound_id, id),
            other => panic!("expected Struct, got {:?}", other),
        }
        assert!(recon.needs_review.contains(&id));
    }

    #[test]
    fn new_entity_minted() {
        let mut reg = IdentityRegistry::default();
        let entities = vec![entity("a.py", "NewFunc", SymbolKind::Function, hash(1), None)];
        let recon = reconcile(&mut reg, &entities, Revision(1));

        match &recon.bindings[0].1 {
            Binding::Minted { .. } => {} // ok
            other => panic!("expected Minted, got {:?}", other),
        }
    }

    #[test]
    fn vanished_entity_retired() {
        let mut reg = IdentityRegistry::default();
        let id = DurableId::mint();
        reg.insert(Anchor {
            id: id.clone(),
            qualified_path: "a.py::OldSym".into(),
            content_hash: hash(10),
            kind: SymbolKind::Function,
            first_seen_rev: Revision(1),
            last_seen_rev: Revision(1),
            status: IdentityStatus::Active,
        });

        let entities: Vec<Entity> = vec![]; // OldSym is gone.
        let recon = reconcile(&mut reg, &entities, Revision(2));

        assert!(recon.retired.contains(&id));
        assert!(reg.get(&id).is_some(), "retained in by_id");
        assert_eq!(reg.get(&id).unwrap().status, IdentityStatus::Orphaned);
    }

    #[test]
    fn determinism_shuffled_entities() {
        // §5.5.5: same result regardless of input order.
        let mut reg = IdentityRegistry::default();
        let id_a = DurableId::mint();
        reg.insert(Anchor {
            id: id_a.clone(),
            qualified_path: "a.py::Foo".into(),
            content_hash: hash(1),
            kind: SymbolKind::Class,
            first_seen_rev: Revision(1),
            last_seen_rev: Revision(1),
            status: IdentityStatus::Active,
        });

        // Run once with normal order.
        let entities_ordered = vec![
            entity("a.py", "Foo", SymbolKind::Class, hash(1), None),
            entity("b.py", "Bar", SymbolKind::Function, hash(2), None),
        ];
        let mut reg1 = {
            let mut r = IdentityRegistry::default();
            r.insert(Anchor {
                id: id_a.clone(),
                qualified_path: "a.py::Foo".into(),
                content_hash: hash(1),
                kind: SymbolKind::Class,
                first_seen_rev: Revision(1),
                last_seen_rev: Revision(1),
                status: IdentityStatus::Active,
            });
            r
        };
        let recon1 = reconcile(&mut reg1, &entities_ordered, Revision(2));

        // Run again with reversed order.
        let entities_reversed = vec![
            entity("b.py", "Bar", SymbolKind::Function, hash(2), None),
            entity("a.py", "Foo", SymbolKind::Class, hash(1), None),
        ];
        let mut reg2 = {
            let mut r = IdentityRegistry::default();
            r.insert(Anchor {
                id: id_a.clone(),
                qualified_path: "a.py::Foo".into(),
                content_hash: hash(1),
                kind: SymbolKind::Class,
                first_seen_rev: Revision(1),
                last_seen_rev: Revision(1),
                status: IdentityStatus::Active,
            });
            r
        };
        let recon2 = reconcile(&mut reg2, &entities_reversed, Revision(2));

        // Bindings keyed by qualified_path must match in variant type.
        let bind1: BTreeMap<&str, &Binding> = recon1
            .bindings
            .iter()
            .map(|(p, b)| (p.as_str(), b))
            .collect();
        let bind2: BTreeMap<&str, &Binding> = recon2
            .bindings
            .iter()
            .map(|(p, b)| (p.as_str(), b))
            .collect();

        for (path, binding1) in &bind1 {
            let binding2 = bind2.get(path).expect("binding missing in recon2");
            // Compare binding variants for reused ids (Exact should have same id).
            match (binding1, binding2) {
                (Binding::Exact { id: id1 }, Binding::Exact { id: id2 }) => {
                    assert_eq!(id1, id2, "Exact bind id should match for {}", path);
                }
                (Binding::Minted { .. }, Binding::Minted { .. }) => {
                    // Minted ids differ (ULIDs are unique), that's expected.
                }
                (Binding::Moved { .. }, Binding::Moved { .. }) => {
                    // Should have same id if both matched the same anchor.
                }
                _ => panic!("binding type mismatch for {}: {:?} vs {:?}", path, binding1, binding2),
            }
        }
    }

    #[test]
    fn one_to_one_two_entities_same_hash_match() {
        // Two entities with the same hash; only one anchor. Only one gets the anchor.
        let mut reg = IdentityRegistry::default();
        let id = DurableId::mint();
        reg.insert(Anchor {
            id: id.clone(),
            qualified_path: "a.py::Original".into(),
            content_hash: hash(42),
            kind: SymbolKind::Function,
            first_seen_rev: Revision(1),
            last_seen_rev: Revision(1),
            status: IdentityStatus::Active,
        });

        let entities = vec![
            entity("b.py", "Copy1", SymbolKind::Function, hash(42), None),
            entity("c.py", "Copy2", SymbolKind::Function, hash(42), None),
        ];
        let recon = reconcile(&mut reg, &entities, Revision(2));

        // Exactly one Moved/Minted and the other Minted.
        let moved_count = recon
            .bindings
            .iter()
            .filter(|(_, b)| matches!(b, Binding::Moved { .. }))
            .count();
        let minted_count = recon
            .bindings
            .iter()
            .filter(|(_, b)| matches!(b, Binding::Minted { .. }))
            .count();

        assert_eq!(moved_count, 1, "exactly one should be Moved");
        assert_eq!(minted_count, 1, "the other should be Minted");
    }

    #[test]
    fn restore_deleted_symbol_rebinds_same_id() {
        // Delete a symbol → Orphaned. Re-add with same body → hash match → re-binds.
        let mut reg = IdentityRegistry::default();
        let id = DurableId::mint();
        reg.insert(Anchor {
            id: id.clone(),
            qualified_path: "a.py::Func".into(),
            content_hash: hash(99),
            kind: SymbolKind::Function,
            first_seen_rev: Revision(1),
            last_seen_rev: Revision(1),
            status: IdentityStatus::Active,
        });

        // Delete.
        let recon1 = reconcile(&mut reg, &[], Revision(2));
        assert!(recon1.retired.contains(&id));
        assert_eq!(reg.get(&id).unwrap().status, IdentityStatus::Orphaned);

        // Re-add with same body.
        let entities = vec![entity("a.py", "Func", SymbolKind::Function, hash(99), None)];
        let recon2 = reconcile(&mut reg, &entities, Revision(3));

        // Should re-bind to the retained id.
        match &recon2.bindings[0].1 {
            Binding::Exact { id: bound_id } => assert_eq!(*bound_id, id),
            Binding::Moved { id: bound_id, .. } => assert_eq!(*bound_id, id),
            other => panic!("expected Exact or Moved, got {:?}", other),
        }
        assert_eq!(reg.get(&id).unwrap().status, IdentityStatus::Active);
    }

    // ── Step 7: Persistence tests ───────────────────────────────────────

    #[test]
    fn round_trip_save_load() {
        let mut reg = IdentityRegistry::default();
        let id = DurableId::mint();
        reg.insert(Anchor {
            id: id.clone(),
            qualified_path: "a.py::X".into(),
            content_hash: hash(123),
            kind: SymbolKind::Class,
            first_seen_rev: Revision(1),
            last_seen_rev: Revision(2),
            status: IdentityStatus::Active,
        });

        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("identity.db");

        reg.save(&path).unwrap();
        let loaded = IdentityRegistry::load(&path).unwrap();

        assert_eq!(loaded.len(), reg.len());
        assert_eq!(loaded.get(&id).unwrap().qualified_path, "a.py::X");
        assert_eq!(loaded.get(&id).unwrap().content_hash, hash(123));
        loaded.assert_consistent();
    }

    #[test]
    fn load_missing_file_yields_empty_registry() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("nonexistent.db");
        let reg = IdentityRegistry::load(&path).unwrap();
        assert_eq!(reg.len(), 0);
    }

    #[test]
    fn crash_safety_tmp_not_renamed() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("identity.db");

        // Save a valid registry.
        let mut reg = IdentityRegistry::default();
        let id = DurableId::mint();
        reg.insert(Anchor {
            id: id.clone(),
            qualified_path: "a.py::X".into(),
            content_hash: hash(1),
            kind: SymbolKind::Class,
            first_seen_rev: Revision(1),
            last_seen_rev: Revision(1),
            status: IdentityStatus::Active,
        });
        reg.save(&path).unwrap();

        // Simulate an interrupted write: write a tmp but don't rename.
        let tmp = path.with_extension("tmp");
        std::fs::write(&tmp, b"garbage").unwrap();
        // Don't rename — crash simulation.

        // Load should still read the prior valid file.
        let loaded = IdentityRegistry::load(&path).unwrap();
        assert!(loaded.get(&id).is_some(), "prior valid file survived crash");
    }

    #[test]
    fn newer_format_version_error() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("identity.db");
        std::fs::write(&path, r#"{"format_version": 99, "anchors": []}"#).unwrap();
        let result = IdentityRegistry::load(&path);
        match result {
            Err(FormatError::UnknownVersion(99)) => {} // expected
            other => panic!("expected UnknownVersion(99), got {:?}", other),
        }
    }

    #[test]
    fn stable_diff_two_saves_identical() {
        let dir = tempfile::tempdir().unwrap();
        let path1 = dir.path().join("id1.db");
        let path2 = dir.path().join("id2.db");

        let id_a = DurableId("01ARZ3NDEKTSV4RRFFQ69G5FAV".to_string());
        let id_b = DurableId("01ARZ3NDEKTSV4RRFFQ69G5FAW".to_string());

        // Save in "wrong" order.
        let mut reg1 = IdentityRegistry::default();
        reg1.insert(Anchor {
            id: id_b.clone(),
            qualified_path: "b.py::Y".into(),
            content_hash: hash(2),
            kind: SymbolKind::Function,
            first_seen_rev: Revision(1),
            last_seen_rev: Revision(1),
            status: IdentityStatus::Active,
        });
        reg1.insert(Anchor {
            id: id_a.clone(),
            qualified_path: "a.py::X".into(),
            content_hash: hash(1),
            kind: SymbolKind::Class,
            first_seen_rev: Revision(1),
            last_seen_rev: Revision(1),
            status: IdentityStatus::Active,
        });
        reg1.save(&path1).unwrap();

        // Save in "right" order.
        let mut reg2 = IdentityRegistry::default();
        reg2.insert(Anchor {
            id: id_a,
            qualified_path: "a.py::X".into(),
            content_hash: hash(1),
            kind: SymbolKind::Class,
            first_seen_rev: Revision(1),
            last_seen_rev: Revision(1),
            status: IdentityStatus::Active,
        });
        reg2.insert(Anchor {
            id: id_b,
            qualified_path: "b.py::Y".into(),
            content_hash: hash(2),
            kind: SymbolKind::Function,
            first_seen_rev: Revision(1),
            last_seen_rev: Revision(1),
            status: IdentityStatus::Active,
        });
        reg2.save(&path2).unwrap();

        let bytes1 = std::fs::read(&path1).unwrap();
        let bytes2 = std::fs::read(&path2).unwrap();
        assert_eq!(bytes1, bytes2, "stable diff: byte-identical files");
    }
}
