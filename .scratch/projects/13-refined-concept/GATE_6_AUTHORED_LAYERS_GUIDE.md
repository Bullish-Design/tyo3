# Gate 6 — Authored Layers (durable, identity-keyed, transaction-bound): Implementation Guide

> Implements **REFINED_SPEC.md §5.4** (authored layers: durable records keyed by
> `DurableId`, edits are writes, never silently dropped), the **authored half of
> §10.2.2** (a snapshot resolves authored values at its revision), and the
> **authored slice of §11.2/§11.3.2** (the `authored/<layer>/` sidecar layout and
> the persist→exit→reload→reconcile no-loss round-trip). It is **ARCHITECTURE
> phase 9**.
>
> **Prerequisites:** `gate5-complete`. This gate consumes:
> - Gate 1's `ContentStore` + revisioned generations + snapshot capture
>   (`rust/src/project.rs::snapshot`, `rust/src/content.rs`),
> - Gate 2's `IdentityRegistry` and its `IdentityStatus`
>   (`Active`/`NeedsReview`/`Orphaned`) and the per-commit `needs_review`/`orphaned`
>   reconciliation output (`rust/src/identity.rs`, `run_identity_reconciliation`),
> - Gate 4's `Sidecar` (single owner of `.tyo3/` paths + `write_atomic`),
>   `ValidatedConfig` (the declared layers, their `origin`/`history`/
>   `review_on_change`), and the unified `TyO3Error` taxonomy + `FormatVersionError`,
> - Gate 5's `Snapshot.derived(...)` wiring pattern and the `DerivationDAG` (authored
>   layers are **excluded** from it — they are sinks).
>
> **Audience:** an engineer new to the codebase. Same rules as Gates 1–5:
> `devenv shell -- <script>` for everything (never bare `cargo`/`pytest`); one
> labelled commit per validated step (`gate6: step N — …`); never skip a validation.

## How to work in this repo

- Rust unit tests: `devenv shell -- test-rust`. Full suite: `devenv shell -- tests`.
- Rebuild the native module after Rust changes: `devenv shell -- build` (required
  before the Python side sees new PyO3 symbols).
- Property tests (Step 7 round-trip fuzz, if expressed via Hypothesis):
  `devenv shell -- test-property`.
- Branch off `gate5-complete`. Commit after each step's validation passes.

## The cardinal rule (unchanged from Gates 1–5)

A gate tag is a claim that the **entire** suite passes. Before tagging
`gate6-complete`, run `devenv shell -- tests` (and `devenv shell -- test-property`)
and paste the final summary line into the tag annotation. Nothing is skipped to pass.

## The contract you are building toward (read first)

An **authored layer** is the mirror image of a derived layer (Gate 5). Where a
derived artifact is a pure function of code with **no durability obligation** (it is
a regenerable, content-addressed cache), an authored record is **deliberately
written human/agent knowledge with no upstream** that must **never be lost**:

- **Keyed by `DurableId`, not content hash (§5.4).** An authored note attaches to a
  *thing*, so it survives edits, moves, and renames of that thing — exactly the
  property the identity registry (Gate 2) provides. Two records for the same id in
  different layers (`intent`, `descriptions`) are distinct.
- **Editing is a write (§5.4).** `session.author(layer, id, value)` flows through the
  commit transaction, **bumps the revision**, and produces a delta — so authored
  knowledge participates in snapshot isolation and time-travel like any other state.
  (This is the defining difference from a derived recompute, which fills a cache and
  does **not** bump the revision.)
- **Never silently dropped (§5.5.3).** When the described entity changes, the record
  is **flagged `needs_review`**, not deleted. When reconciliation cannot rebind the
  entity at all, the record is **`orphaned`**, not deleted. The record file always
  persists.
- **Durable + committable + non-invasive (§11.2/§11.3).** Records live in
  `authored/<layer>/<durable_id>.json`, one file per record, id-keyed so a VCS merge
  is a content merge (§11.3.7); writes are crash-safe (§11.3.3); the source tree is
  never touched (§11.3.4).
- **Consistent at a snapshot (§10.2.2 authored half).** `snapshot.authored(layer,
  id)` at revision R returns the authored value **as of R**, with a status
  (`present`/`needs_review`/`orphaned`/`absent`) consistent with R.

The defining success property: **`session.author(...)` bumps the revision and
persists durably; a delete-then-reopen keeps the record (orphaned, not lost); a
moved entity keeps its note (rebind via identity); a code change flags the note
`needs_review`; a project with no authored layers declared behaves exactly as Gate
5.**

## Design stance — where authored state lives (read before you start)

Gate 5 put derived layers **in Python** because a derived cache is revision-unaware,
content-addressed, and disposable — it needs nothing from the spine but a hash. **An
authored layer is the opposite**, and its properties are precisely those the spine
already implements for the identity registry:

| Property | Mechanism that already exists |
|---|---|
| durable, single-writer-owned, crash-safe persisted | `Sidecar::write_atomic`, persisted inside `commit_*` under the write lock |
| captured into a snapshot, isolated, time-travellable | `head.registry.clone()` into the snapshot at capture (`project.rs::snapshot`) |
| identity-keyed; `needs_review`/`orphaned` lifecycle | `IdentityRegistry` + `IdentityStatus`, recomputed every reconciliation |

So this gate hosts the authored **store in Rust, alongside the identity registry**,
and keeps Python a thin façade (a config-derived `AuthoredLayer` view, an
`AuthoredValue` model, and `session.author` / `snapshot.authored` methods over native
data) — exactly as the code graph (Gate 3) is a Python API over native symbols. This
buys snapshot isolation and time-travel **for free** from the registry's existing
capture path, puts the authored write **inside the single transaction** (§3.3.1,
§11.3.3) with content and identity, and **co-locates the record with the registry
status it reports** so there is one source of truth for `needs_review`/`orphaned`.

> **Alternative considered (Python host).** A Python-side authored store under the
> Gate-3 session-level write lock would keep the gate "all Python" like Gate 5. It
> was rejected because (a) snapshot isolation/time-travel for the *value* would need
> a new per-revision capture mechanism that the registry capture already gives us in
> Rust, and (b) crash-safe persistence inside the native write transaction (§11.3.3)
> is cleaner where the `Sidecar` and lock already are. If the team prefers the Python
> host, the algorithm below is identical; only the host changes — record the decision
> as Gate 2 did for identity.

## The one decision that drives everything: status is *derived*, not stored

Do **not** persist a status on each record. The authored record on disk holds only
**value + (optional) history**. The review status of a record at revision R is
**derived** from the (snapshot-captured) identity registry's anchor status for that
id, gated by the layer's `review_on_change`:

```
status_at(layer, id, R) =
    absent                       if no authored record for (layer, id) with rev ≤ R
    present                      if layer.review_on_change == false
    map(registry@R .anchor(id).status)    otherwise
        Active      → present
        NeedsReview → needs_review
        Orphaned    → orphaned
```

This is why "never silently dropped" is **structural** (the file persists regardless
of status) and why time-travel status is automatically correct (the snapshot already
captures the registry — `project.rs::snapshot` sets `state.registry = Some(registry)`).
It removes a whole class of status bookkeeping, dual-write, and reconcile-on-load
status drift. The registry is the single source of truth for the lifecycle; the
authored store is the single source of truth for the value.

## Target module layout

```
rust/src/
  authored.rs    # NEW: AuthoredRecord, AuthoredStore (rpds-backed), record format (Steps 1–2)
  project.rs     # HeadState owns AuthoredStore; load on open; author() transaction;
                 #   capture into snapshot; reconcile-status surfacing (Steps 3–5)
  sidecar.rs     # add record_path / history_dir if Gate 4 step 7 did not (Step 1)
  identity.rs    # expose anchor status by id for the snapshot read (Step 3)
  dto/           # SyncResultDto gains `authored`; AuthoredValueDto (Steps 4, 6)
  lib.rs         # mod authored; register nothing new (reuse FormatVersionError) (Step 1)
src/tyo3/
  models/
    authored.py  # NEW: AuthoredValue { value, status, revision, layer } (Step 6)
  authored/      # NEW: thin Python façade
    __init__.py
    layer.py     # AuthoredLayer.from_config (Step 6)
  session.py     # session.author / authored / authored_needs_review; Snapshot.authored (Step 6)
  models/analysis.py   # SyncResult gains `authored` (Step 4)
src/tyo3/tests/
  test_gate6_authored.py   # Step 0 + final acceptance
```

No new crate or pip dependencies: `rpds` (content store), `serde`/`serde_json`,
`ulid` are already present.

---

## Step 0 — Pin the authored API with a failing acceptance test FIRST

**Goal.** Freeze the public API and the four behaviours that define the gate before
building anything (mirrors Gate 3/5 Step 0). It `xfail`s until Step 6/7.

**Files.** `src/tyo3/tests/test_gate6_authored.py` (new).

**Build the fixture & test.** A small multi-file project with a config declaring one
authored layer:

```toml
schema_version = 1
[hashing.profiles.structure]
[layers.intent]
origin           = "authored"
history          = true
review_on_change = true
```

Write four tests against the *intended* API:

- **Durable write + read.** `r1 = session.author("intent", id, {"note": "compat shim"})`
  returns a new revision (`r1 > session.head_before`); `session.authored("intent", id).value
  == {"note": "compat shim"}`.
- **Snapshot isolation + time-travel.** Open `snap0 = session.snapshot()` *before* the
  author; after the author, `snap0.authored("intent", id).status == "absent"` while a
  fresh `session.snapshot().authored("intent", id).status == "present"`. A
  `session.snapshot(at=r1).authored("intent", id).value` round-trips the value at r1.
- **needs_review on change, not dropped.** Edit the *body* of `id`'s entity (a code
  write); `session.authored("intent", id).status == "needs_review"`; the value is
  still readable.
- **Orphaned on delete, not dropped.** Delete `id`'s entity; reopen the project;
  `session.authored("intent", id).status == "orphaned"` and `.value` is intact (the
  §5.5.3 / §11.3.2 no-loss proof).

**Validate.**
- `devenv shell -- tests -k gate6_authored` runs; record status (expected: failing/
  xfail — the API does not exist yet).
- **Acceptance gate:** the test exists and names the intended API
  (`session.author`, `session.authored`, `snap.authored`, `snapshot(at=r)`); status
  recorded. Commit (`gate6: step 0 — pin authored-layer API, status=XFAIL`).

> Do not implement anything in Step 0. Its only job is to fix the contract.

---

## Step 1 — The on-disk record format + sidecar paths

**Goal.** A versioned, crash-safe, VCS-merge-friendly record format, and the canonical
sidecar paths for it (SPEC §11.2, §11.3.3, §11.3.5, §11.3.7). Split *format* from
*I/O* exactly as Gate 4 did for `identity.db`.

**Files.** `rust/src/authored.rs` (new; `mod authored;` in `lib.rs`),
`rust/src/sidecar.rs`.

**Build.**

- **Sidecar paths.** Gate 4 step 7 specified `record_path`/`history_dir`; if they are
  not already on `Sidecar`, add them next to `authored_dir` (the only place these
  strings are spelled):
```rust
pub fn record_path(&self, layer: &str, durable_id: &str) -> PathBuf {
    self.authored_dir(layer).join(format!("{durable_id}.json"))
}
pub fn history_dir(&self, layer: &str, durable_id: &str) -> PathBuf {
    self.authored_dir(layer).join(format!("{durable_id}.history"))
}
```
- **Record doc (format).** In `authored.rs`, a serde model that owns *only* its
  serialized form — no I/O, no paths:
```rust
const AUTHORED_FORMAT_VERSION: u32 = 1;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuthoredVersion {
    pub value: serde_json::Value,   // arbitrary authored payload
    pub revision: u64,              // the revision this version was written at
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuthoredRecordDoc {
    pub format_version: u32,
    pub layer: String,
    pub durable_id: String,
    pub current: AuthoredVersion,
    #[serde(default)] pub history: Vec<AuthoredVersion>,  // empty unless layer.history
}

impl AuthoredRecordDoc {
    pub fn to_bytes(&self) -> serde_json::Result<Vec<u8>> {
        // pretty + sorted keys for stable diffs (§11.3.7); value uses serde's
        // canonical ordering. Append a trailing newline.
    }
    pub fn from_bytes(bytes: &[u8]) -> Result<Self, FormatVersionError> {
        // parse; if format_version > AUTHORED_FORMAT_VERSION → FormatVersionError
        // (the SAME unified type Gate 4 raises for identity.db/config) — never
        // silently misread (§11.3.5).
    }
}
```
- **No new exception type.** Reuse Gate 4's `FormatVersionError`; a newer authored
  record format is "newer format than I understand," the same catchable type as a
  newer `config.toml`/`identity.db`.

**Validate.** Rust unit tests in `authored.rs`:
- `to_bytes` → `from_bytes` round-trips a record with and without history.
- A doc with `format_version = 2` → `from_bytes` returns `FormatVersionError`.
- Two docs differing only in field insertion order serialise byte-identically (stable
  diff).
- `record_path("intent", "01KT…")` / `history_dir(...)` produce the documented paths.
- **Acceptance gate:** all pass. Commit (`gate6: step 1 — authored record format + sidecar paths`).

---

## Step 2 — The in-memory `AuthoredStore` (revisioned, captured, copy-on-write)

**Goal.** A structurally-shared store of authored records that captures into a
snapshot in O(1) and gives correct time-travel + isolation **by the same mechanism
the content store uses** (Gate 1). This is the heart of the gate.

**Files.** `rust/src/authored.rs`.

**Build.**
```rust
use rpds::HashTrieMapSync;
use std::sync::Arc;

/// (layer, durable_id) → record. Persistent map: clone is O(1) structural share.
#[derive(Debug, Clone, Default)]
pub struct AuthoredMap {
    records: HashTrieMapSync<(String, String), Arc<AuthoredRecord>>,
}

#[derive(Debug, Clone)]
pub struct AuthoredRecord {
    pub current: AuthoredVersion,
    pub history: Vec<AuthoredVersion>,   // newest-last; only retained if layer.history
}

pub type AuthoredStore = Arc<AuthoredMap>;   // capture(&self) = Arc::clone
```
- **Update is copy-on-write at the record level.** `author` builds a *new*
  `AuthoredRecord` (Arc), then `records.insert(key, new)` returns a *new map* sharing
  structure with the old. A snapshot holding the old `Arc<AuthoredMap>` keeps the old
  `Arc<AuthoredRecord>` — so a held snapshot is **never** mutated by a later author
  edit. **Never mutate an `Arc<AuthoredRecord>` in place.**
```rust
impl AuthoredMap {
    pub fn put(&self, layer: &str, id: &str, version: AuthoredVersion, keep_history: bool) -> Self {
        let key = (layer.to_string(), id.to_string());
        let history = match self.records.get(&key) {
            Some(prev) if keep_history => {
                let mut h = prev.history.clone();
                h.push(prev.current.clone());
                h
            }
            _ => Vec::new(),
        };
        let rec = Arc::new(AuthoredRecord { current: version, history });
        AuthoredMap { records: self.records.insert(key, rec) }
    }

    /// Time-travel read: the value with the greatest revision ≤ `at`, or None
    /// (the record did not exist at `at`). Checks `current` then `history`.
    pub fn value_at(&self, layer: &str, id: &str, at: u64) -> Option<&AuthoredVersion> { … }

    pub fn ids_in_layer(&self, layer: &str) -> impl Iterator<Item = &str> { … }
}
```
- **Why `value_at` and not just `current`:** a snapshot pinned at an *old* R reads the
  live map (captured at its own revision, which for `snapshot(at=old_R)` is the head
  map) and must return the version that was current at `old_R`. Filtering `current ∪
  history` by `revision ≤ at` gives exact time-travel from the rev-stamped versions —
  the authored analogue of the content store's `retained` window. (Old versions are
  retained as long as their record is in memory; this is bounded by the same project
  lifecycle as the registry.)

**Validate.** Rust unit tests:
- Isolation/COW: capture the map; `put` a new version; the captured clone still
  returns the old `current` (assert via `Arc::ptr_eq` on the record or value equality).
- Time-travel: `put` value A at rev 3, value B at rev 5; `value_at(_, _, 4) == A`,
  `value_at(_, _, 5) == B`, `value_at(_, _, 2) == None`.
- History: with `keep_history = true`, after two `put`s the record's `history` holds
  the first version; with `false`, `history` is empty.
- **Acceptance gate:** all pass. Commit (`gate6: step 2 — AuthoredStore (rpds, COW, time-travel)`).

---

## Step 3 — Own the store in `HeadState`, load on open, capture into snapshots

**Goal.** The head owns one live `AuthoredStore`; opening a project loads every
declared authored layer's records; every snapshot captures the store alongside the
registry so authored reads are revision-consistent (SPEC §11.3.2 load,
§10.2.2 capture).

**Files.** `rust/src/project.rs`, `rust/src/identity.rs`.

**Build.**
- **`HeadState`** (around `rust/src/project.rs:83`) gains `authored: AuthoredStore`
  next to `registry` and `sidecar`.
- **Load on open.** In `PyTyProject::open` (where `identity.db` is loaded, ~`:1422`),
  after the registry is loaded, read authored records: for each layer in
  `validated_config` with `origin == "authored"`, enumerate
  `sidecar.authored_dir(layer)` (if it exists), `from_bytes` each `*.json` into the
  map (skip `*.history` dirs and `.tmp`). A `FormatVersionError` propagates (the
  session does not open — §11.3.5). A missing `authored/` dir yields an empty store
  (first run). **Reconcile-on-load is not skipped** (§11.4): the registry is already
  loaded and reconciled on the first commit/`sync_all`, and because status is
  *derived* from the registry (see the key decision above), the loaded records inherit
  the correct `needs_review`/`orphaned` status with no per-record reconciliation.
- **Capture into the snapshot.** In `PyTyProject::snapshot` (~`:1800`), where
  `state.registry = Some(head.registry.clone())` is set, also capture the authored
  store: `state.authored = Some(head.authored.clone())` (an `Arc` clone — O(1),
  §1.3.5). The frozen state now carries both halves of identity-linked durable state
  at R.
- **Expose anchor status to the read path.** Add to `IdentityRegistry` a small
  accessor `pub fn status_of(&self, id: &DurableId) -> Option<IdentityStatus>` (read
  `by_id`). The snapshot read in Step 6 maps it to the public `AuthoredValue.status`.

**Validate.**
- Rust test: construct a head with two authored records on disk; `open` loads both
  into the store; `value_at(head_rev)` returns them.
- Rust test: a snapshot captures the store; after a later head `author` edit (Step 4),
  the snapshot still reads the pre-edit value (isolation, end-to-end with capture).
- Rust test: `status_of` returns the anchor's status; `None` for an unknown id.
- **Acceptance gate:** all pass. Commit (`gate6: step 3 — HeadState owns + loads + captures authored store`).

---

## Step 4 — The authored write transaction

**Goal.** `author(layer, id, value)` is a real revision-producing commit: it bumps the
revision under the **single write lock**, updates the store copy-on-write, persists the
record crash-safely, and returns a delta — atomically, with rollback on error (SPEC
§5.4, §3.3.1, §3.3.6, §11.3.3).

**Files.** `rust/src/project.rs`, `rust/src/dto`, `src/tyo3/models/analysis.py`.

**Build.**
- **SyncResult gains an `authored` field.** Add `authored: Vec<String>` to
  `dto::SyncResultDto` and the Pydantic `SyncResult` (`src/tyo3/models/analysis.py:82`),
  default empty. An authored edit reports the edited id here. It MUST NOT populate
  `created`/`changed`/`deleted` (those are code-entity deltas) so that **derived
  invalidation (Gate 5) and the head-graph update (Gate 3) are no-ops** for an
  authored write — authored layers are sinks (§9.2.4) and code-derived recompute must
  not fire (verify in Step 6).
- **The native `author` method** (a new PyO3 method on `PyTyProject`):
```rust
fn author(&self, py: Python<'_>, layer: &str, id: &str, value_json: &str)
    -> PyResult<Bound<'_, PyAny>>
{
    let mut guard = lock_state(&self.inner, "author")?;   // the single write lock
    let head = guard.as_mut().unwrap();

    // 1. validate: layer is a declared authored layer (ValidatedConfig in HeadState)
    let lcfg = head.config.authored_layer(layer)
        .ok_or_else(|| ConfigError::new_err(format!("'{layer}' is not a declared authored layer")))?;
    let value: serde_json::Value = serde_json::from_str(value_json)
        .map_err(|e| /* typed error */)?;

    // 2. bump the revision through the content store with an EMPTY content change,
    //    so this authored edit IS a revision with a retained generation
    //    (snapshot(at=R) valid; authored time-travel aligns with content §5.4).
    let revision = head.store.apply_batch(Vec::new());     // one revision, no content
    head.system.publish(head.store.capture());             // keep the publish invariant

    // 3. copy-on-write the authored store
    let version = AuthoredVersion { value, revision: revision.0 };
    let new_map = head.authored.put(layer, id, version.clone(), lcfg.history);
    head.authored = Arc::new(new_map);

    // 4. persist the record crash-safely via the Sidecar (still under the lock,
    //    §11.3.3 — never a torn sidecar)
    let rec = head.authored.records_get(layer, id).unwrap();
    let doc = AuthoredRecordDoc { format_version: 1, layer: layer.into(),
                                  durable_id: id.into(),
                                  current: rec.current.clone(), history: rec.history.clone() };
    head.sidecar.write_atomic(&head.sidecar.record_path(layer, id), &doc.to_bytes()?)?;

    // 5. build the delta
    let dto = dto::SyncResultDto { revision: revision.0, authored: vec![id.into()],
                                   ..Default::default() };
    Ok(pythonize(py, &dto)?)
}
```
- **Atomicity / rollback (§3.3.6).** If `value_json` is invalid or persistence fails,
  return the error **before** mutating `head.authored`, or restore the prior store on
  a persistence failure — the committed state must be all-or-nothing. Order the steps
  so the fallible parse happens first; treat a `write_atomic` failure as a hard error
  that rolls the in-memory `head.authored` back to its prior `Arc` (keep the prior Arc
  in a local before assigning the new one).
- **Validation of `id`.** The id need not currently resolve to a live entity — you can
  author intent about an entity that is temporarily orphaned. But it SHOULD exist in
  the registry's `by_id` (live or retired); reject an id unknown to the registry with
  a typed error so a typo does not create a dangling record.

**Validate.** Rust + Python:
- Test: `author` advances the revision by exactly one; `snapshot(at=that R)` is valid.
- Test: `author` on a non-authored / undeclared layer → `ConfigError`.
- Test: the returned `SyncResult.authored == [id]` and `created/changed/deleted` empty.
- Test: a persistence failure (simulate by pointing the sidecar at an unwritable path)
  leaves the in-memory store at the prior value (rollback).
- **Acceptance gate:** all pass. Commit (`gate6: step 4 — authored write transaction (§5.4/§3.3.1)`).

---

## Step 5 — Lifecycle surfacing: needs_review / orphaned after a code commit

**Goal.** When a *code* commit reconciles, an authored record whose entity changed
must surface as `needs_review`, and one whose entity vanished as `orphaned` — driven
by the existing reconciliation output, gated by the layer's `review_on_change`
(§5.5.3). Because status is *derived* (the key decision), this step is mostly
**surfacing**, not new bookkeeping.

**Files.** `rust/src/project.rs` (`commit_head` / `run_identity_reconciliation`,
~`:1126`/`:1176`), `rust/src/dto`.

**Build.**
- The read path (Step 6) already derives a record's status from the captured
  registry, so a `needs_review`/`orphaned` record is **correct on read with no write
  needed**. What this step adds is the **per-commit delta** so agents/the bus (Gate 8)
  can react precisely:
  - In `commit_head`, after `run_identity_reconciliation`, intersect
    `identity.needs_review` and `identity.orphaned` with the set of ids that **have an
    authored record** in a `review_on_change` layer, and add two fields to the
    `SyncResultDto`: `authored_needs_review: Vec<String>` and
    `authored_orphaned: Vec<String>` (each a list of ids, deduplicated, deterministic
    order). Mirror them on the Pydantic `SyncResult`.
  - A layer with `review_on_change = false` contributes nothing here (its records are
    always `present`).
- **Do not delete or rewrite any record.** Orphaning is a *status* derived from the
  registry, not a file operation. The record file is untouched (§5.5.3 no-drop).
- Expose a steady-state query: `session.authored_needs_review(layer)` and
  `session.authored_orphaned(layer)` — the current id lists, computed by intersecting
  the registry status with the layer's record ids (mirrors the existing
  `session.needs_review()`/`orphaned()` at `session.py:942`).

**Validate.**
- Test: author a note on `id`; edit `id`'s body (a code write) → the code commit's
  `SyncResult.authored_needs_review == [id]`; `session.authored("intent", id).status
  == "needs_review"`; the value is intact.
- Test: a record on a `review_on_change = false` layer → never appears in
  `authored_needs_review`; status stays `present` through a body edit.
- Test: delete `id`'s entity → `SyncResult.authored_orphaned == [id]`; status
  `orphaned`; record file still on disk.
- Test: re-add the entity (rebinds via registry rule 2/5) → status returns to
  `present` (registry anchor back to `Active`); no record rewrite needed.
- **Acceptance gate:** all pass. Commit (`gate6: step 5 — needs_review/orphaned surfacing (§5.5.3)`).

---

## Step 6 — The Python façade: `session.author`, `Snapshot.authored`, models

**Goal.** A thin, typed Python surface over the native authored store, following the
Gate-5 `Snapshot.derived` wiring exactly, and making Step 0's tests pass (SPEC §10.2.2
authored half, §10.2.3 honest status).

**Files.** `src/tyo3/models/authored.py`, `src/tyo3/authored/layer.py`,
`src/tyo3/session.py`, `rust/src/dto` (`AuthoredValueDto`).

**Build.**
- **`AuthoredValue` model** (`src/tyo3/models/authored.py`):
```python
class AuthoredValue(BaseModel):
    layer: str
    durable_id: str
    value: Any | None                 # None iff status == "absent"
    status: Literal["present", "needs_review", "orphaned", "absent"]
    revision: int
```
- **Native read.** Add a PyO3 method on the snapshot state (the frozen `PySnapshot`):
  `authored(layer, id) -> AuthoredValueDto` that:
  1. resolves the version via `state.authored.value_at(layer, id, state.revision)`;
  2. derives status from `state.registry.status_of(id)` gated by the layer's
     `review_on_change` (per the key-decision table); `absent` if no version ≤ R;
  3. returns `{ layer, durable_id, value, status, revision }`.
  Add `authored_history(layer, id) -> [AuthoredVersion]` for `history = true` layers.
- **`Snapshot.authored`** (`src/tyo3/session.py`, the `Snapshot` class ~`:1000`),
  mirroring `Snapshot.graph()`/the Gate-5 `derived` accessor:
```python
def authored(self, layer: str, durable_id: str) -> AuthoredValue:
    self._check_open()
    dto = self._inner.authored(layer, durable_id)
    return AuthoredValue.model_validate(dto)
```
- **`session.author`** (the write), following the write-path shape at
  `session.py:734` but **without** `_apply_graph_delta` / derived invalidation (an
  authored edit produces no code/derived delta — assert this stays a no-op):
```python
def author(self, layer: str, durable_id: str, value: Any) -> SyncResult:
    self._check_open()
    self._invalidate_head_snap()
    payload = json.dumps(value)
    try:
        native = self._inner.author(layer, durable_id, payload)
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e
    return SyncResult.model_validate(native)
```
  > **Do not** call `_apply_graph_delta(result)` here: `created/changed/deleted` are
  > empty, so it would be a no-op, but skipping it also avoids forcing the lazy head
  > graph to build. If a session-level write lock was introduced in Gate 3 step 8,
  > take it around the native `author` call too, so an authored write and a code write
  > cannot interleave their revision bumps.
- **Head convenience reads** (sugar over the cached head snapshot, like other session
  reads): `session.authored(layer, id)` → `self._native().authored(...)`;
  `session.authored_needs_review(layer)` / `session.authored_orphaned(layer)` over the
  native steady-state queries from Step 5.
- **`AuthoredLayer.from_config`** (`src/tyo3/authored/layer.py`): a small read-only
  view of an authored layer's config (`name`, `history`, `review_on_change`) built from
  `session.config`, for callers that want to enumerate authored layers. It carries **no
  generator/store** (authored layers have no upstream). It is **not** registered in the
  Gate-5 `DerivationDAG` (sinks).

**Validate.**
- **Step 0's four tests now pass.**
- Test (honest status, §10.2.3): a `needs_review` record reads `status="needs_review"`
  and still returns its value.
- Test (no derived fire): with a derived layer also configured, `session.author(...)`
  does not enqueue any recompute and does not change the head graph revision beyond the
  authored bump (assert the derived generator counter is unchanged).
- Test (history): with `history=true`, author twice; `snap.authored_history(...)` has
  the prior version; with `history=false`, history is empty.
- **Acceptance gate:** all pass; Step 0 green. Commit (`gate6: step 6 — Python authored façade, §10.2.2`).

---

## Step 7 — Persistence round-trip & reconcile-on-load no-loss (§11.3.2)

**Goal.** Prove the durable contract end-to-end: persist → exit → reload → reconcile
restores authored records exactly, modulo identity rebinding (SPEC §11.3.2, §11.4).

**Files.** `src/tyo3/tests/test_gate6_authored.py` (extend).

**Build & validate** — a dedicated suite proving:
- **No-loss round-trip.** Author notes on several ids (one layer `history=true`, one
  `history=false`) → `close()` → reopen → every value (and history where kept) is
  identical.
- **Moved entity keeps its note.** Author on `id`; move its class to a new file
  unchanged (one `edit_many`) → reopen → `authored(id).value` intact, status
  `present` (registry rule 2 rebind; the note followed the id, not the location).
- **Rename-with-change flags review.** Author on `id`; rename + edit the body → reopen
  → value intact, status `needs_review`.
- **Delete orphans, never drops.** Author on `id`; delete the entity → reopen →
  status `orphaned`, value intact; re-add the entity → status returns to `present`.
- **Crash-safety (§11.3.3).** Simulate an interrupted record write (write `<id>.json.tmp`
  but not the rename) → reopen reads the prior valid record (reuse the Gate-4
  `write_atomic` crash test pattern).
- **Newer format rejected (§11.3.5).** Hand-write a record with `format_version = 2`
  → reopen raises `FormatVersionError` (the unified type).
- **Stable diffs / order-independence (§11.3.7).** Author the same set of records in
  two different orders into two sidecars → the per-id files are byte-identical.
- **Cache-independence.** Delete `.tyo3/cache/` (derived) → reopen → authored records
  intact (authored lives under `authored/`, not `cache/`).
- **Acceptance gate:** all pass. Commit (`gate6: step 7 — persistence no-loss round-trip (§11.3.2)`).

---

## Step 8 — Cross-layer consistency at a snapshot (authored half of §10.2.2)

**Goal.** At one pinned revision R, the code node, the derived artifact, **and** the
authored value for an id all describe R; time-travel to old R is exact (SPEC §10.2.2,
§10.2.4).

**Files.** `src/tyo3/tests/test_gate6_authored.py` (extend).

**Build & validate.**
- Test: at a pinned R, for some id, `snap.graph().node(id).content_hash`,
  `snap.derived("embeddings", id)` (if a derived layer is configured), and
  `snap.authored("intent", id)` all reflect R; many subsequent head writes (code *and*
  authored) do not change the pinned reads.
- Test (time-travel diff): author value A at R0, value B at R1; `snapshot(at=R0)`
  reads A, `snapshot(at=R1)` reads B — the authored analogue of the content/derived
  time-travel already proven in Gates 1/5.
- Test (no write lock for reads, §10.2.4): authored reads on a snapshot succeed while a
  writer hammers head authored+code edits (reuse the `test_mvcc_concurrency.py`
  pattern); no read is blocked or cancelled.
- **Acceptance gate:** all pass; `devenv shell -- tests` green. Commit
  (`gate6: step 8 — cross-layer consistency, authored half §10.2.2`).

---

## Gate 6 — Final acceptance (must all pass before Gate 7)

Run `devenv shell -- tests` (plus `devenv shell -- test-property` for the round-trip
fuzz) with the dedicated module proving:

1. **Authored edit is a write (§5.4).** `session.author` bumps the revision exactly
   once and produces a delta with the id in `authored` and nothing in
   `created/changed/deleted`.
2. **Keyed by `DurableId` (§5.4).** A moved/renamed entity keeps its authored note;
   the note follows the id, not the location.
3. **Never silently dropped (§5.5.3).** A changed entity flags its note
   `needs_review`; a vanished entity's note is `orphaned`; the record file always
   persists; re-adding the entity returns the note to `present`.
4. **Durable round-trip no-loss (§11.3.2).** persist → exit → reload restores values
   and history exactly; crash-mid-write leaves the prior valid record; a newer record
   format raises `FormatVersionError`.
5. **Snapshot isolation + time-travel (§10.2.2 authored half).** A held snapshot is
   unaffected by later authored edits; `snapshot(at=R)` reads the value as of R; status
   at R is derived from the registry as of R.
6. **Cross-layer consistency (§10.2.2).** At a pinned R, code, derived, and authored
   all describe R; reads take no write lock (§10.2.4).
7. **Sinks, not sources (§9.2.4).** An authored write triggers no derived recompute
   and no head-graph change; authored layers are absent from the derivation DAG.
8. **No-config / no-authored no-op.** A project with no authored layers declared
   behaves exactly as Gate 5; the spine and derived layers are unaffected.

When all eight pass on a clean `devenv shell -- tests`, tag the commit
`gate6-complete` and paste the passing summary into the annotation. Durable authored
knowledge is now first-class.

## Carrying forward to Gate 7 / Gate 8 (MUST read)

Gate 6 leaves these seams for the remaining gates:

- **Unified cross-layer read surface (Gate 7).** `snapshot.authored(...)` now sits
  beside `snapshot.code(...)`/`snapshot.derived(...)`, all resolving against the same
  pinned R via the captured registry + content + authored + derived state. Gate 7's
  unified `§10` join and the cross-revision diff (`after.authored_diff(before)`, the
  combined layer diff) build on the three accessors — they do not add a fourth capture
  mechanism.
- **The authored delta is bus-ready (Gate 8).** `SyncResult.authored` and
  `authored_needs_review`/`authored_orphaned` are precise, id-level fields. The
  subscription bus delivers them to subscribers whose interest includes those ids
  exactly as it does code deltas; an authored edit is a normal revision on the bus.
- **History is the audit trail.** `history = true` layers retain rev-stamped prior
  versions; a future "blame/why" view over authored intent reads `authored_history`
  with no new storage.

## Sequencing & escalation notes

- **The empty-content revision bump (Step 4) is load-bearing.** It is what makes an
  authored edit a real revision with a retained generation, so `snapshot(at=R)` works
  and authored time-travel aligns with content/identity time-travel. Do not "optimise"
  it into a side-channel counter that the content store and snapshot path don't know
  about — that re-opens §10.2.1 (a snapshot must pin *one* revision across all state).
- **Status is derived; never dual-write it.** If you find yourself persisting a
  `status` onto a record and keeping it in sync with the registry, stop — that is the
  drift bug the key decision exists to prevent. The registry (captured per snapshot) is
  the single source of truth for `needs_review`/`orphaned`.
- **Copy-on-write at the record level (Step 2) is the isolation guarantee.** Mutating
  an `Arc<AuthoredRecord>` in place would leak a later edit into a held snapshot. Build
  a new record + insert into the persistent map; never mutate in place.
- **Authored ≠ derived in the write path.** Derived recompute fills a cache and does
  not bump the revision; an authored edit bumps the revision and does not recompute
  anything. Keep `author()` out of `_apply_graph_delta` and the Gate-5 derived
  invalidation.
- **If the team chooses the Python host instead** (design stance), the snapshot must
  still capture authored state at R (a Python rev-stamped store read as `value_at(R)`),
  and the persist must run under the Gate-3 session-level write lock via a Python
  `write_atomic` mirror — do not let the record write escape the write transaction
  (§11.3.3). Record the decision in the commit, as Gate 2 did for identity.
