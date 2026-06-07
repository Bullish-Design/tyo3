//! Authored record format and in-memory store (Gate 6, SPEC §5.4, §11.2–§11.3).
//!
//! Two halves, split cleanly:
//!   - `AuthoredRecordDoc` / `AuthoredVersion`: the versioned, serde-friendly on-disk
//!     format.  Owns only its serialized form — no paths, no I/O.
//!   - `AuthoredMap` / `AuthoredStore`: the structurally-shared (rpds) in-memory
//!     store, captured into snapshots in O(1) for Snapshot isolation + time-travel.
//!
//! Status is **derived** from the identity registry, never persisted on records.

use std::fmt;

use serde::{Deserialize, Serialize};

const AUTHORED_FORMAT_VERSION: u32 = 1;

// ── On-disk record format ──────────────────────────────────────────────

/// A single revision-stamped version of an authored value.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AuthoredVersion {
    /// Arbitrary authored payload (JSON).
    pub value: serde_json::Value,
    /// The application revision this version was written at.
    pub revision: u64,
}

/// The on-disk document for one (layer, durable_id) authored record.
///
/// Stored at `authored/<layer>/<durable_id>.json` via `Sidecar::write_atomic`.
/// Pretty-printed with sorted keys for stable VCS diffs (§11.3.7).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuthoredRecordDoc {
    pub format_version: u32,
    pub layer: String,
    pub durable_id: String,
    pub current: AuthoredVersion,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub history: Vec<AuthoredVersion>,
}

impl AuthoredRecordDoc {
    /// Serialise to pretty JSON bytes with sorted keys for stable diffs.
    /// Appends a trailing newline.
    pub fn to_bytes(&self) -> serde_json::Result<Vec<u8>> {
        let mut buf = serde_json::to_vec_pretty(self)?;
        buf.push(b'\n');
        Ok(buf)
    }

    /// Deserialise from bytes, rejecting newer format versions.
    ///
    /// Returns `AuthoredLoadError` if the format version is unsupported
    /// or the JSON is malformed — never silently misread (§11.3.5).
    /// The caller converts to a Python `FormatVersionError` at the boundary.
    pub fn from_bytes(bytes: &[u8]) -> Result<Self, AuthoredLoadError> {
        let doc: AuthoredRecordDoc =
            serde_json::from_slice(bytes).map_err(|e| {
                AuthoredLoadError::Json(e.to_string())
            })?;
        if doc.format_version > AUTHORED_FORMAT_VERSION {
            return Err(AuthoredLoadError::UnknownVersion(doc.format_version));
        }
        Ok(doc)
    }
}

// ── Authored load error ────────────────────────────────────────────────

/// Errors that can occur when loading an authored record from disk.
/// Converted to a Python `FormatVersionError` at the PyO3 boundary.
#[derive(Debug)]
pub enum AuthoredLoadError {
    Json(String),
    UnknownVersion(u32),
}

impl fmt::Display for AuthoredLoadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            AuthoredLoadError::Json(s) => write!(f, "JSON error: {s}"),
            AuthoredLoadError::UnknownVersion(v) => {
                write!(f, "unknown authored record format version: {v}")
            }
        }
    }
}

// ── In-memory store (rpds-backed, COW, captured O(1)) ─────────────────

use rpds::HashTrieMapSync;
use std::sync::Arc;

/// A persistent, structurally-shared record store.
///
/// Keyed by `(layer, durable_id)`.  Clone is O(1) structural share — a
/// snapshot holding an old `Arc<AuthoredMap>` is **never** mutated by a later
/// author edit.
#[derive(Debug, Clone, Default)]
pub struct AuthoredMap {
    records: HashTrieMapSync<(String, String), Arc<AuthoredRecord>>,
}

/// A single in-memory authored record — current version plus optional history.
///
/// `history` is retained newest-last; only preserved when the layer is
/// configured with `history = true`.
#[derive(Debug, Clone)]
pub struct AuthoredRecord {
    pub current: AuthoredVersion,
    pub history: Vec<AuthoredVersion>,
}

/// The stored form carried by `HeadState` and captured into snapshots.
pub type AuthoredStore = Arc<AuthoredMap>;

impl AuthoredMap {
    /// Copy-on-write insert (or replace) of a record.
    ///
    /// Builds a new `AuthoredRecord` (new `Arc` — never mutate in place),
    /// then inserts into the map, returning a new structurally-shared map.
    pub fn put(
        &self,
        layer: &str,
        id: &str,
        version: AuthoredVersion,
        keep_history: bool,
    ) -> Self {
        let key = (layer.to_string(), id.to_string());
        let history = match self.records.get(&key) {
            Some(prev) if keep_history => {
                let mut h = prev.history.clone();
                h.push(prev.current.clone());
                h
            }
            _ => Vec::new(),
        };
        let rec = Arc::new(AuthoredRecord {
            current: version,
            history,
        });
        AuthoredMap {
            records: self.records.insert(key, rec),
        }
    }

    /// Time-travel read: the version with the greatest revision ≤ `at`, or
    /// `None` if the record did not exist at `at`.
    ///
    /// Checks `current` first (the fastest path — the latest version), then
    /// walks `history` (newest-last, so we iterate in reverse).
    pub fn value_at(
        &self,
        layer: &str,
        id: &str,
        at: u64,
    ) -> Option<&AuthoredVersion> {
        let key = (layer.to_string(), id.to_string());
        let rec = self.records.get(&key)?;
        if rec.current.revision <= at {
            return Some(&rec.current);
        }
        // Walk history newest-first (history is newest-last).
        for v in rec.history.iter().rev() {
            if v.revision <= at {
                return Some(v);
            }
        }
        None
    }

    /// Get the full record (current + history) for a (layer, id) pair.
    pub fn records_get(&self, layer: &str, id: &str) -> Option<&Arc<AuthoredRecord>> {
        let key = (layer.to_string(), id.to_string());
        self.records.get(&key)
    }

    /// Iterate over durable ids in a layer.
    pub fn ids_in_layer<'a>(&'a self, layer: &'a str) -> impl Iterator<Item = &str> + 'a {
        self.records
            .keys()
            .filter(move |(l, _)| l == layer)
            .map(|(_, id)| id.as_str())
    }

    /// Number of records in the store.
    pub fn len(&self) -> usize {
        self.records.size()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_version(val: serde_json::Value, rev: u64) -> AuthoredVersion {
        AuthoredVersion {
            value: val,
            revision: rev,
        }
    }

    fn make_version_str(val: &str, rev: u64) -> AuthoredVersion {
        AuthoredVersion {
            value: serde_json::Value::String(val.to_string()),
            revision: rev,
        }
    }

    fn make_doc(
        layer: &str,
        id: &str,
        value: serde_json::Value,
        rev: u64,
        history: Vec<AuthoredVersion>,
    ) -> AuthoredRecordDoc {
        AuthoredRecordDoc {
            format_version: AUTHORED_FORMAT_VERSION,
            layer: layer.to_string(),
            durable_id: id.to_string(),
            current: make_version(value, rev),
            history,
        }
    }

    // ── Step 1: Record format ────────────────────────────────────────

    #[test]
    fn round_trip_without_history() {
        let value = serde_json::json!({"note": "shim"});
        let doc = make_doc("intent", "01ABC", value.clone(), 1, vec![]);
        let bytes = doc.to_bytes().unwrap();
        let doc2 = AuthoredRecordDoc::from_bytes(&bytes).unwrap();
        assert_eq!(doc2.format_version, 1);
        assert_eq!(doc2.layer, "intent");
        assert_eq!(doc2.durable_id, "01ABC");
        assert_eq!(doc2.current.value, value);
        assert_eq!(doc2.current.revision, 1);
        assert!(doc2.history.is_empty());
    }

    #[test]
    fn round_trip_with_history() {
        let doc = make_doc(
            "intent",
            "01ABC",
            serde_json::json!({"note": "v2"}),
            5,
            vec![make_version(serde_json::json!({"note": "v1"}), 3)],
        );
        let bytes = doc.to_bytes().unwrap();
        let doc2 = AuthoredRecordDoc::from_bytes(&bytes).unwrap();
        assert_eq!(doc2.current.revision, 5);
        assert_eq!(doc2.history.len(), 1);
        assert_eq!(doc2.history[0].revision, 3);
    }

    #[test]
    fn format_version_error() {
        let doc_str = r#"{"format_version":2,"layer":"intent","durable_id":"01ABC","current":{"value":"v","revision":1},"history":[]}"#;
        let result = AuthoredRecordDoc::from_bytes(doc_str.as_bytes());
        assert!(result.is_err());
        let err = result.unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("unknown") || msg.contains("2") || msg.contains("format"));
    }

    #[test]
    fn stable_byte_identical_serialisation() {
        let val = serde_json::json!({"note": "shim"});
        let doc1 = make_doc("intent", "01ABC", val.clone(), 1, vec![]);
        let doc2 = make_doc("intent", "01ABC", val, 1, vec![]);
        let bytes1 = doc1.to_bytes().unwrap();
        let bytes2 = doc2.to_bytes().unwrap();
        assert_eq!(bytes1, bytes2);
    }

    // ── Step 2: AuthoredMap ──────────────────────────────────────────

    #[test]
    fn isolation_cow() {
        let map = AuthoredMap::default();
        let v1 = make_version_str("A", 1);
        let v2 = make_version_str("B", 2);

        let map1 = map.put("intent", "id1", v1.clone(), false);
        let map1_clone = map1.clone();
        // Write through the clone-based map.
        let map2 = map1_clone.put("intent", "id1", v2.clone(), false);

        // Clone still sees old version.
        let old = map1.value_at("intent", "id1", 1).unwrap();
        assert_eq!(old.value, v1.value);
        // New map sees new version.
        let new = map2.value_at("intent", "id1", 2).unwrap();
        assert_eq!(new.value, v2.value);
    }

    #[test]
    fn time_travel_value_at() {
        let map = AuthoredMap::default();
        let map = map.put("intent", "id1", make_version_str("A", 3), true);
        let map = map.put("intent", "id1", make_version_str("B", 5), true);

        assert_eq!(
            map.value_at("intent", "id1", 2).map(|v| v.value.as_str().unwrap()),
            None,
        );
        assert_eq!(
            map.value_at("intent", "id1", 3).map(|v| v.value.as_str().unwrap()),
            Some("A"),
        );
        assert_eq!(
            map.value_at("intent", "id1", 4).map(|v| v.value.as_str().unwrap()),
            Some("A"),
        );
        assert_eq!(
            map.value_at("intent", "id1", 5).map(|v| v.value.as_str().unwrap()),
            Some("B"),
        );
    }

    #[test]
    fn history_accumulation() {
        let map = AuthoredMap::default();
        let map = map.put("intent", "id1", make_version_str("A", 1), true);
        let map = map.put("intent", "id1", make_version_str("B", 2), true);

        let rec = map.records_get("intent", "id1").unwrap();
        assert_eq!(rec.current.value, serde_json::Value::String("B".to_string()));
        assert_eq!(rec.history.len(), 1);
        assert_eq!(rec.history[0].value, serde_json::Value::String("A".to_string()));
    }

    #[test]
    fn no_history_when_disabled() {
        let map = AuthoredMap::default();
        let map = map.put("intent", "id1", make_version_str("A", 1), false);
        let map = map.put("intent", "id1", make_version_str("B", 2), false);

        let rec = map.records_get("intent", "id1").unwrap();
        assert!(rec.history.is_empty());
    }

    #[test]
    fn ids_in_layer_enumeration() {
        let map = AuthoredMap::default();
        let map = map.put("intent", "id1", make_version_str("A", 1), false);
        let map = map.put("intent", "id2", make_version_str("B", 1), false);
        let map = map.put("descriptions", "id3", make_version_str("C", 1), false);

        let mut ids: Vec<&str> = map.ids_in_layer("intent").collect();
        ids.sort();
        assert_eq!(ids, vec!["id1", "id2"]);
    }
}
