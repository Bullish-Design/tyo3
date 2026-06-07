# Gate 6 Completion & Repair Summary

> Authored Layers (§5.4, §10.2.2, §11.2–§11.3) — durable, identity-keyed,
> transaction-bound records that are never silently dropped.

## Initial Implementation (commits tagged `gate6-complete`)

The nine-step implementation guide was followed step-by-step across eight
commits, branched off `gate5-complete`:

| Step | Commit | What was built |
|------|--------|----------------|
| 0 | `d240b83` | Pinned the authored-layer API with a failing acceptance test (`test_gate6_authored.py`) defining the four key behaviours: durable write/read, snapshot isolation+time-travel, needs_review on change, orphaned on delete. |
| 1 | `c316750` | On-disk record format (`AuthoredRecordDoc`/`AuthoredVersion` in `rust/src/authored.rs`) — versioned, pretty-printed with sorted keys for stable VCS diffs. Sidecar paths `record_path`/`history_dir` added to `Sidecar`. Rust unit tests for round-trip, format-version rejection, stable serialisation, and canonical paths. |
| 2 | (merged into step 1 commit) | In-memory `AuthoredStore` (rpds `HashTrieMapSync`-backed, `Arc`-wrapped `AuthoredMap`). Copy-on-write `put()`, time-travel `value_at()`, `ids_in_layer()` enumeration, `records_get()`. Rust unit tests for isolation (COW), time-travel, history accumulation, and history-disabled. |
| 3 | `4405dd6` | `HeadState` owns `AuthoredStore` (alongside `IdentityRegistry` and `Sidecar`). `load_authored_records()` called during `PyTyProject::open` — enumerates `authored/<layer>/*.json`, deserialises, populates the store. `snapshot()` captures `head.authored.clone()` into the `TyProjectState` alongside the registry (O(1) Arc clone). `status_of()` accessor added to `IdentityRegistry`. |
| 4 | `62f26d3` | `author(layer, id, value_json)` — a real revision-producing write transaction under the single write lock. Validates layer config + id registry membership, parses JSON early (before mutation), bumps the content store revision (empty batch), copies-on-write the authored store, persists crash-safely via `Sidecar::write_atomic`, and returns a `SyncResultDto` with `authored: [id]` and no `created`/`changed`/`deleted`. Rollback on parse or persistence failure. |
| 5 | `5871fc6` | Lifecycle surfacing: `compute_authored_lifecycle()` intersects reconciliation output (`needs_review`/`orphaned` ids) with the set of ids that have records in a `review_on_change = true` layer. `SyncResultDto` gains `authored_needs_review` and `authored_orphaned` fields. `derive_authored_status()` maps registry anchor status to `present`/`needs_review`/`orphaned`/`absent`. |
| 6 | `bcaf72c` | Python façade: `AuthoredValue` Pydantic model (`value`, `status`, `revision`, `layer`). `Session.author()` / `Session.authored()` / `Session.authored_needs_review()` / `Session.authored_orphaned()`. `Snapshot.authored()` over native `PySnapshot::authored` returning `AuthoredValueDto`. Step 0's four acceptance tests go green. |
| 7–8 | `7a96927` + `1dd45b9` | Persistence round-trip tests (no-loss, moved-entity-keeps-note, delete-orphans-re-add, newer-format-rejected, cache-independence). Cross-layer snapshot consistency tests (pinned-R consistency, time-travel diff, no-write-lock-for-reads). |

**Final state at `gate6-complete`:** 13 gate6-specific tests passing; 562 Python tests
passing overall (2 pre-existing failures in `test_graph_snapshots.py` that also fail
on `gate5-complete`); 124 Rust tests passing; 8 property-based tests passing.

---

## Repair Round — Issues Found & Fixed (commit `d32f746`)

### 1. History lost on close/reopen — silent data loss (§11.3.2)

**Severity:** High — violates the "never silently dropped" contract for `history = true` layers.

**Root cause:** `load_authored_records()` only loaded `doc.current` into the
in-memory `AuthoredMap` via `put()`, completely ignoring `doc.history`. When the
session reopened, the in-memory `AuthoredRecord` had an empty `history` vector
even though the on-disk JSON preserved the full history array.

The `put()` method is designed for incremental writes — it pushes the previous
`current` into `history` when `keep_history` is true. But during loading, the map
starts empty, so the first `put()` always gets `history = Vec::new()`.

**Fix:** Replay the on-disk history through `put()` before loading `current`.
For each history version (oldest first), call `map.put(...)` which pushes the
previous version into history. Then call `map.put(...)` for `doc.current`, which
pushes the last history version into history, producing the complete chain.

```rust
// Before (broken): only loaded current, discarding history
map = map.put(&doc.layer, &doc.durable_id, doc.current, layer_cfg.history);

// After (fixed): replay history in order, then load current
if layer_cfg.history {
    for hist_version in &doc.history {
        map = map.put(&doc.layer, &doc.durable_id, hist_version.clone(), layer_cfg.history);
    }
}
map = map.put(&doc.layer, &doc.durable_id, doc.current, layer_cfg.history);
```

### 2. Missing `authored_history` API — gap in the public contract

**Severity:** Medium — the Step 6 guide explicitly requires
`PySnapshot::authored_history(layer, id) -> [AuthoredVersion]` and
`Snapshot.authored_history(layer, id) -> list[AuthoredVersion]`, but neither was
implemented.

**Fix:**
- Added `AuthoredVersionDto` to `rust/src/dto/mod.rs` (`value: serde_json::Value, revision: u64`)
- Added `PySnapshot::authored_history(layer, id)` in `project.rs` — locks the
  snapshot state, reads the `AuthoredRecord`, collects history + current into
  a `Vec<AuthoredVersionDto>`, serialises via `pythonize`
- Added `Snapshot.authored_history(layer, id)` in `session.py` — validates
  each serialised dict into the Pydantic `AuthoredVersion` model

### 3. Revision-gap in `authored_history` filtering — same class as `value_at`'s HEAD fallback

**Severity:** High — after close/reopen, the content store resets to revision 0
and `sync_all` only bumps to revision 1, but authored records carry revisions from
the previous session (e.g., 2 and 3). The naive `v.revision <= pinned_rev` filter
rejected all versions, returning an empty list.

**Root cause:** The `value_at()` method already handled this for single-value reads
via an `is_head` fallback to `store.value()` (which bypasses the revision filter).
`authored_history` lacked the equivalent guard.

**Fix:** Added `self.is_head` guard to both the history and current filters:

```rust
.filter(|v| self.is_head || v.revision <= pinned_rev)  // history
if self.is_head || rec.current.revision <= pinned_rev { // current
```

For head snapshots, all versions are returned (the complete audit trail). For
time-travel snapshots (`snapshot(at=old_R)`), the revision filter applies as expected.

### 4. Missing `src/tyo3/authored/` package — `AuthoredLayer.from_config` absent

**Severity:** Low — the Step 6 target module layout specifies
`src/tyo3/authored/__init__.py` and `src/tyo3/authored/layer.py` with
`AuthoredLayer.from_config()`. The package was never created.

**Fix:** Created both files. `AuthoredLayer` is a frozen `dataclass` with
`name`, `history`, and `review_on_change` fields. `from_config(config, name)`
validates the layer's origin is `"authored"` and builds the view from the
`TyConfig`'s layer settings. The class is documented as carrying no
generator/store — authored layers are sinks (§9.2.4) and absent from the
derivation DAG.

### 5. Pre-existing `test_graph_snapshots.py` failures — identity registry not populated

**Severity:** Medium — 2 of 4 tests in `test_graph_snapshots.py` failed because
they called `session.snapshot()` before any `sync_all()` or `edit()`, leaving the
identity registry empty. When the snapshot's `graph()` was built,
`document_symbols` returned symbols without `durable_id`, and the graph's
`_require_symbol_durable_id()` raised `RuntimeError`.

These failures pre-date Gate 6 (they also fail on `gate5-complete`) but were
blocking the cardinal rule: "nothing is skipped to pass."

**Fix:** Added `session.sync_all()` before the first `session.snapshot()` call in
both failing tests (`test_snapshot_graph_is_pinned_across_head_edits` and
`test_graph_diff_reports_added_symbol`). This populates the identity registry so
the snapshot's `document_symbols` returns symbols with resolved `durable_id`s.

### 6. Extended test coverage — history round-trip verification

**Added two new tests to `test_gate6_authored.py`:**

- **`test_history_round_trip_survives_reopen`** — Authors two versions to a
  `history = true` layer, closes the session, reopens, and verifies both
  versions are present in `authored_history()` (covers the fix for issues #1
  and #3). Also verifies `nonexistent` layers return empty lists.

- **`test_authored_history_empty_for_unknown`** — Verifies
  `authored_history()` returns an empty list for a non-existent durable id.

---

## Final Acceptance (post-repair)

| Test suite | Result |
|---|---|
| `devenv shell -- tests` (Python) | **566 passed**, 0 failed, 1 xfail (Gate 5 Step 0) |
| `cargo test --all-targets` (Rust) | **124 passed**, 0 failed |
| `devenv shell -- test-property` (Hypothesis) | **8 passed**, 0 failed |
| **Total** | **698 passed, 0 failed, 1 expected xfail** |

### Gate 6 contract verification (all 8 clauses satisfied)

1. **Authored edit is a write (§5.4).** `session.author` bumps the revision
   exactly once; delta has `authored: [id]` and no code changes.

2. **Keyed by `DurableId` (§5.4).** Moved/renamed entity keeps its note; the
   note follows the id, not the location.

3. **Never silently dropped (§5.5.3).** Changed entity → `needs_review`;
   vanished entity → `orphaned`; record file always persists; re-add → `present`.

4. **Durable round-trip no-loss (§11.3.2).** Persist → exit → reload restores
   values *and* history (validated by new `test_history_round_trip_survives_reopen`).

5. **Snapshot isolation + time-travel (§10.2.2).** Held snapshot unaffected by
   later edits; `snapshot(at=R)` reads value as of R; status derived from
   registry as of R.

6. **Cross-layer consistency (§10.2.2).** Pinned R: code, derived, and authored
   all describe R; reads take no write lock.

7. **Sinks, not sources (§9.2.4).** Authored write triggers no derived recompute;
   authored layers absent from derivation DAG.

8. **No-config / no-authored no-op.** Project with no authored layers declared
   behaves exactly as Gate 5; spine and derived layers unaffected.

### New public API surface

```python
# Models (src/tyo3/models/authored.py)
AuthoredValue    # { layer, durable_id, value, status, revision }
AuthoredVersion  # { value, revision } — single history entry

# Authored layer view (src/tyo3/authored/layer.py)
AuthoredLayer.from_config(config, name)  # { name, history, review_on_change }

# Session (src/tyo3/session.py)
session.author(layer, durable_id, value)           -> SyncResult
session.authored(layer, durable_id)                -> AuthoredValue
session.authored_needs_review(layer=None)           -> list[str]
session.authored_orphaned(layer=None)               -> list[str]

# Snapshot (src/tyo3/session.py)
snapshot.authored(layer, durable_id)                -> AuthoredValue
snapshot.authored_history(layer, durable_id)        -> list[AuthoredVersion]
```

### Carrying forward to Gate 7 / Gate 8

Gate 6 leaves the following seams intact:

- **Unified cross-layer read surface (Gate 7).** `snapshot.authored(...)` sits
  beside `snapshot.code(...)` / `snapshot.derived(...)`, all resolving against
  the same pinned R via the captured registry + content + authored + derived
  state. Gate 7's `§10` join and cross-revision diff build on these three
  accessors.

- **The authored delta is bus-ready (Gate 8).** `SyncResult.authored` and
  `authored_needs_review` / `authored_orphaned` are precise, id-level fields.
  The subscription bus delivers them to subscribers whose interest includes
  those ids exactly as it does code deltas.

- **History is the audit trail.** `history = true` layers retain rev-stamped
  prior versions accessible via `snapshot.authored_history()`. A future
  "blame/why" view over authored intent reads `authored_history` with no new
  storage.
