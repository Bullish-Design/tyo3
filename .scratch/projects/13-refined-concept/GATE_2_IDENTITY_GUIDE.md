# Gate 2 — Identity & Reconciliation: Implementation Guide

> Implements **REFINED_SPEC.md §5** (and §7 content-hash normalisation, on which
> identity depends). This is the second foundational **gate**. It requires Gate 1
> to be complete and tagged `gate1-complete` — reconciliation runs inside the
> commit transaction whose content authority Gate 1 established.
>
> **Audience:** an engineer new to the codebase. Same working rules as Gate 1:
> `devenv shell -- <script>` for everything, one labelled commit per validated
> step (`gate2: step N — ...`), never skip a validation.

## The contract you are building toward (read first)

Two identifiers, kept strictly distinct (SPEC §5.2):

- **DurableId** — a minted, persistent id that survives content edits, moves,
  renames, and restarts. The node identity in every layer and the key authored
  knowledge attaches to. **Never derived from content, location, name, or line.**
- **ContentHash** — a hash of the entity's *normalised* definition. Changes on
  meaningful edits, stable across cosmetic ones. Keys derived caches and is the
  secondary matcher for reconciliation.

The defining success invariants:

- An entity that **moves with an unchanged body keeps its DurableId** and its
  derived-cache hits (§5.5.1).
- An entity whose **body changes but identity is unambiguous keeps its DurableId**;
  its content hash updates (§5.5.2).
- **Authored records are never silently dropped** (§5.5.3).
- Reconciliation is **deterministic** regardless of processing order (§5.5.5).

## Why this depends on Gate 1

Reconciliation compares the prior identity registry against the entities of the
freshly-built code layer *at a specific revision*. That revision's content must be
authoritative and stable (Gate 1 §1.3.1), or the entity set you reconcile against
is itself nondeterministic. Do not attempt Gate 2 on a pre-Gate-1 store.

## Target module layout

```
rust/src/
  hash.rs        # extend with AST-normalised entity hashing (Step 1)
  entity.rs      # Entity extraction from the code layer at a revision (Step 2)
  identity.rs    # DurableId, IdentityRegistry, reconciliation (Steps 3–5)
  project.rs     # wire reconciliation into the commit transaction (Step 6)
src/tyo3/
  identity/      # Python: registry persistence (JSON) + authored linkage (Steps 7–8)
```

> **Note on Python vs Rust split.** Durable identity and reconciliation may live in
> Rust (preferred, single-writer-owned) or Python. This guide implements the
> registry and reconciliation in **Rust**, exposed to Python, so it sits inside the
> native write lock with no GIL-only serialization assumptions. If the team
> chooses Python, the algorithm (Step 5) is identical; only the host changes.

---

## Step 1 — AST-normalised entity content hashing

**Goal.** Replace whole-file byte hashing (Gate 1 Step 1) with a hash over an
entity's *normalised* definition, so cosmetic edits don't churn identity or caches
(SPEC §7).

**Files.** `rust/src/hash.rs`.

**Build.**
- Add `pub fn hash_entity(normalised: &NormalForm) -> ContentHash` where
  `NormalForm` is the canonical rendering of an entity's AST subtree.
- Implement normalisation with a configurable policy:
```rust
pub struct HashPolicy {
    pub ignore_whitespace: bool,     // default true
    pub ignore_trailing_comma: bool, // default true
    pub include_docstrings: bool,    // default false (per-layer override later)
    pub include_comments: bool,      // default false
}
```
- Derive the normal form from ty's/ruff's parsed AST for the entity's range:
  render tokens/structure canonically per policy (identifiers, literals, and
  structure are always significant). Do **not** hash raw source bytes.
- Hashing still funnels through the 128-bit `hash_bytes` from Gate 1.

**Validate.** Unit tests in `hash.rs`:
- Reformatting (whitespace/newlines only) → hash unchanged.
- Adding/removing a blank line *outside* the entity → hash unchanged (this is
  also exercised end-to-end in Step 6).
- Renaming an identifier inside the entity → hash changes.
- Changing a literal → hash changes.
- Two textually-identical functions in different files → identical hash.
- Docstring edit with `include_docstrings=false` → unchanged; with `true` →
  changed.
- **Acceptance gate:** all pass; the Gate-1 whole-file hash tests still pass
  (entity hashing is additive, not a replacement of file hashing). Commit.

---

## Step 2 — Entity extraction at a revision

**Goal.** From the code layer at revision R, produce the set of `Entity` records
reconciliation operates on (SPEC §5.4 input).

**Files.** `rust/src/entity.rs` (new); `mod entity;` in `lib.rs`.

**Build.**
```rust
pub struct Entity {
    pub qualified_path: String,   // "pkg/mod.py::Outer.method" — file::qualified_name
    pub kind: SymbolKind,
    pub content_hash: ContentHash,
    pub container: Option<String>, // enclosing qualified name, for STRUCT matching
    pub name: String,
}

/// Extract every addressable entity from a snapshot/state at one revision.
pub fn extract_entities(state: &TyProjectState) -> Vec<Entity>;
```
- Walk the project's symbols via the analysis engine (the same source the code
  layer uses). For each symbol: build `qualified_path` as `file_path::qualified_name`;
  compute `content_hash = hash_entity(normal_form(symbol))` using Step 1.
- For symbols the engine does not give a qualified name, derive a **structural**
  name from container + ordinal + kind (e.g. `Outer.<lambda#2>`), **not** a line
  number. (Line numbers are forbidden as identity input — §5.6.)

**Validate.**
- Unit/integration test over a fixture project: `extract_entities` yields the
  expected qualified paths and kinds; counts match the symbols.
- Test: editing whitespace above a method does not change that method's
  `content_hash` (depends on Step 1).
- Test: no `Entity.qualified_path` or derived name contains a line number.
- **Acceptance gate:** all pass. Commit.

---

## Step 3 — `DurableId` and the in-memory registry

**Goal.** The minted-id type and the registry that binds ids to last-known entity
facts (SPEC §5.2, §5.3).

**Files.** `rust/src/identity.rs` (new); `mod identity;` in `lib.rs`. Add a ULID
crate (e.g. `ulid`) to `Cargo.toml`.

**Build.**
```rust
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct DurableId(String);   // ULID string; opaque, never parsed for meaning

impl DurableId {
    pub fn mint() -> Self { DurableId(ulid::Ulid::new().to_string()) }
}

#[derive(Debug, Clone)]
pub struct Anchor {
    pub id: DurableId,
    pub qualified_path: String,
    pub content_hash: ContentHash,
    pub kind: SymbolKind,
    pub first_seen_rev: Revision,
    pub last_seen_rev: Revision,
}

#[derive(Debug, Default)]
pub struct IdentityRegistry {
    by_id:   HashMap<DurableId, Anchor>,
    by_path: HashMap<String, DurableId>,        // qualified_path → id (EXACT index)
    by_hash: HashMap<ContentHash, Vec<DurableId>>, // content_hash → ids (HASH index)
}
```
- Implement registry mutators that keep all three indexes consistent:
  `insert(anchor)`, `rebind(id, new_path, new_hash, rev)`, `retire(id, rev)`,
  and lookups `get(id)`, `by_path(path)`, `by_hash(hash)`.
- **Invariant to enforce in code:** `by_path` and `by_hash` always reflect `by_id`.
  Add a debug-only `assert_consistent()` that the tests call.

**Validate.**
- Unit tests: insert → all three indexes resolve; rebind updates `by_path`/`by_hash`
  and leaves no stale keys; retire removes from `by_path`/`by_hash` but **keeps**
  the anchor in `by_id` (retired, not deleted — §5.5 rule 5).
- `assert_consistent()` holds after each mutator in a randomized sequence test.
- **Acceptance gate:** all pass. Commit.

---

## Step 4 — The reconciliation result type

**Goal.** A precise, testable description of what reconciliation decided, so it can
be unit-tested in isolation and consumed by the delta and authored layers (SPEC
§5.4, §4.3.6).

**Files.** `rust/src/identity.rs`.

**Build.**
```rust
#[derive(Debug, PartialEq, Eq)]
pub enum Binding {
    Exact  { id: DurableId },                 // rule 1
    Moved  { id: DurableId, old_path: String }, // rule 2 (hash match, new location)
    Struct { id: DurableId, confidence: Low }, // rule 3 (flag authored needs-review)
    Minted { id: DurableId },                 // rule 4
}

#[derive(Debug, Default)]
pub struct Reconciliation {
    pub bindings: Vec<(String /*qualified_path*/, Binding)>, // one per new entity
    pub retired:  Vec<DurableId>,             // rule 5: entity gone (authored → orphaned)
    pub needs_review: Vec<DurableId>,         // Struct binds + body-changed exacts you choose to flag
}
```
- Add helpers: `Reconciliation::moved()`, `::minted()`, `::struct_binds()` returning
  the relevant ids, for the delta builder (Gate-3/§4) and authored layer (Step 8).

**Validate.** Compile + a trivial constructor test. **Acceptance gate:** green. Commit.

---

## Step 5 — The reconciliation algorithm (the core deliverable)

**Goal.** Bind every new-state entity to a `DurableId` by the four-rule algorithm,
retire vanished anchors, and do so **deterministically** (SPEC §5.4, §5.5).

**Files.** `rust/src/identity.rs`.

**Build.** Implement:
```rust
pub fn reconcile(
    registry: &mut IdentityRegistry,
    entities: &[Entity],
    revision: Revision,
) -> Reconciliation;
```

Algorithm — follow exactly; the ordering and one-to-one rules are correctness, not
style:

1. **Determinism pre-sort.** Sort `entities` by a total order
   `(qualified_path, content_hash)` into a working list. Track two "consumed" sets:
   `bound_ids: HashSet<DurableId>` and `matched_entities`.
2. **Pass A — EXACT.** For each entity E in order: if `registry.by_path(E.qualified_path)`
   yields an unconsumed id A, bind `Exact{A}`; if `anchor.content_hash != E.hash`,
   record the anchor's hash will update and add A to `needs_review` only if you flag
   body-changed exacts (policy: flag iff the entity has dependent authored records —
   defer the actual flag to Step 8, just mark the id). Mark E and A consumed.
3. **Pass B — HASH (moves).** For each still-unmatched E in order: look up
   `registry.by_hash(E.content_hash)` for an unconsumed id A whose
   `anchor.qualified_path != E.qualified_path`. If found (choose the smallest id by
   the total order if several), bind `Moved{A, old_path}`; mark consumed.
4. **Pass C — STRUCT.** For each still-unmatched E in order: among unconsumed
   anchors, find candidates with same `(name, kind, container)`; if any, pick the
   one minimizing an edit-distance score on `qualified_path` (ties broken by
   smallest id). Bind `Struct{A, Low}`; add A to `needs_review`; mark consumed.
5. **Pass D — MINT.** Any still-unmatched E gets `Minted{mint()}`.
6. **Apply to registry.** For Exact/Moved/Struct: `registry.rebind(id, E.qualified_path,
   E.hash, revision)`. For Minted: `registry.insert(Anchor{..})`. For every anchor
   **not** consumed this pass whose binding is gone: `registry.retire(id, revision)`
   and add to `Reconciliation.retired`.
7. Return the `Reconciliation`.

> **One-to-one (§5.5.4):** an anchor binds to at most one entity per pass and an
> entity to at most one anchor — the `bound_ids`/`matched_entities` consumed sets
> enforce this. **Conservative STRUCT (§5.6):** Pass C must be the *only* fuzzy
> rule, must be flagged `needs_review`, and must never run before B.

**Validate.** Unit tests against a hand-built registry + entity set (no engine
needed — pure function):
- **Unchanged move (§5.5.1):** anchor at `a.py::C` hash H; entity at `b.py::C`
  hash H → `Moved`, same id, `old_path = a.py::C`.
- **Body change, same path (§5.5.2):** anchor `a.py::C` hash H1; entity `a.py::C`
  hash H2 → `Exact`, same id, anchor hash updated to H2.
- **Rename + body change:** anchor `a.py::Old` hash H1; entity `a.py::New` hash H2
  with same container/kind → `Struct`, same id, `needs_review` contains it.
- **New entity:** no match → `Minted`, fresh id.
- **Vanished entity:** anchor with no matching entity → `retired`, id retained in
  registry `by_id`.
- **Determinism (§5.5.5):** run `reconcile` twice with the entity slice shuffled;
  assert identical `bindings` (compare as sets keyed by qualified_path) and
  identical minted-vs-reused decisions. Use a fixed RNG seed to shuffle.
- **One-to-one:** two entities that both hash-match one anchor → exactly one binds
  `Moved`/`Exact`, the other `Minted` or `Struct`; the anchor is consumed once.
- **Acceptance gate:** all pass. Commit. This is the highest-value test set in the
  gate — do not advance with any failing.

---

## Step 6 — Wire reconciliation into the commit transaction

**Goal.** Every committed revision has a fully reconciled identity map *before* the
revision is published, and the result feeds the delta (SPEC §3.3, §5.4, §4.3.6).

**Files.** `rust/src/project.rs`.

**Build.**
- Extend `HeadState` to own an `IdentityRegistry`.
- In `commit` (Gate 1 Step 9), after the engine `apply_changes` and before building
  the `SyncResult`, run, **still under the write lock**:
  1. `entities = extract_entities(&head_state_view)` for the new revision;
  2. `recon = reconcile(&mut head.registry, &entities, revision)`;
  3. translate `recon` into the durable-id-level delta fields: `created` =
     `Minted` ids; `changed` = `Exact` ids whose hash changed; `moved` = `Moved`
     entries; `deleted` = `retired` ids.
- Expose `id_for(path, line, col)` and `locate(durable_id)` PyO3 methods:
  `id_for` resolves the enclosing entity at a location, then its current
  `qualified_path`, then `registry.by_path`; `locate` reads the anchor's
  `qualified_path`.
- Reconciliation cost must be bounded by the changed/affected set in the
  incremental case (§5.5.6): only re-extract and reconcile entities in files
  touched by this commit plus their reverse-dep closure — not the whole project.
  (Full-project reconciliation is correct but only acceptable on `rescan`.)

**Validate.** Integration tests through the Python API:
- Open a fixture; `id1 = id_for("models.py", <line of User.save>)`. Insert a blank
  line above `User.save` via `edit`. `id2 = id_for(...)` at the new line. Assert
  `id1 == id2` (id survived a cosmetic edit — the end-to-end §5.5.1/§7 proof).
- Move a class to a new file (two edits in one `edit_many`); assert `locate(id)`
  reports the new path and the commit's delta lists it under `moved`, not
  created+deleted.
- Rename a method with a body change; assert the delta/`needs_review` surfaces it.
- Determinism end-to-end: build the same fixture twice from scratch; assert the
  set of (qualified_path → kind) is identical and that a re-open reuses ids via the
  registry (after Step 7 persistence).
- **Acceptance gate:** all pass; `devenv shell -- tests` green. Commit.

---

## Step 7 — Identity registry persistence (round-trip)

**Goal.** Persist the registry so durable ids survive a restart and reconciliation
on reload re-binds to current code (SPEC §5.3, §11.3.2). This is the identity slice
of the `.tyo3/` sidecar; the full sidecar (authored stores, derived cache) is a
later phase — implement only `identity.db` here.

**Files.** `rust/src/identity.rs` (serialize/deserialize); `rust/src/project.rs`
(load on open, persist on commit). Add `serde`/`serde_json` if not present.

**Build.**
- Define a versioned on-disk schema:
```json
{ "format_version": 1,
  "anchors": [ {"id","qualified_path","content_hash","kind","first_seen_rev","last_seen_rev"} ] }
```
- `IdentityRegistry::save(path)` writes atomically: write to `path.tmp`, fsync,
  rename over `path` (crash-safe, §11.3.3). `ContentHash` serialises as its `u128`.
- `IdentityRegistry::load(path) -> Result<Self, FormatError>`: a newer
  `format_version` returns a typed `FormatError` (never silently misread, §11.3.5);
  a missing file yields an empty registry (first run).
- On `TyO3Session`/project open: load `<root>/.tyo3/identity.db` if present.
- After a successful commit, persist the registry (atomically). Persisting **MUST**
  happen under the same write transaction so the on-disk registry never reflects a
  half-applied revision.
- Keys are order-independent (id-keyed anchors), so a VCS merge of two `identity.db`
  files is a content merge (§11.3.7) — verify the JSON is emitted with anchors
  sorted by id for stable diffs.

**Validate.**
- Round-trip test: build a fixture (mints ids) → `save` → new registry `load` →
  assert anchors identical.
- Restart simulation: open project, edit, close; re-open; assert `id_for` on an
  unchanged symbol returns the **same** id as before close (ids persisted, not
  re-minted) — the §11.3.2 no-loss proof for identity.
- Out-of-band edit: persist, mutate a file on disk while "closed", re-open;
  reconciliation re-binds (moved/struct) rather than minting fresh ids for
  unchanged-body symbols.
- Crash safety: write a test that points `save` at a path, simulates an
  interrupted write (write `.tmp` then don't rename), and asserts `load` still
  reads the prior valid file.
- Newer format_version → `FormatError`.
- Stable diff: `save` twice with anchors inserted in different orders → byte-
  identical files.
- **Acceptance gate:** all pass. Commit.

---

## Step 8 — `needs-review` / `orphaned` lifecycle hooks

**Goal.** Guarantee authored knowledge is never silently dropped, by giving the
registry the lifecycle states reconciliation produces, and a stable hook authored
layers (a later phase) consume (SPEC §5.5.3).

**Files.** `rust/src/identity.rs`, `rust/src/project.rs`.

**Build.**
- Add to `Anchor` a `status: IdentityStatus` where:
```rust
pub enum IdentityStatus { Active, NeedsReview, Orphaned }
```
- During Step 6's commit translation:
  - `Struct` binds and (per policy) body-changed `Exact` binds → set anchor
    `NeedsReview`; include the id in `SyncResult.needs_review`.
  - `retired` anchors → set `Orphaned`; include in `SyncResult.orphaned`. **Never**
    remove the anchor from `by_id` (it may return — Step 5 rule 5 / §5.5).
  - A subsequent clean `Exact`/`Moved` bind clears the status back to `Active`.
- Expose read methods: `session.needs_review()` and `session.orphaned()` returning
  the current id lists, so an agent/human can act. These are the *hooks*; the
  authored-record storage that consumes them is the authored-layer phase, out of
  scope here — but the states and the no-drop guarantee are in scope and must hold.

**Validate.**
- Test: rename-with-change → the id is `NeedsReview`; appears in
  `session.needs_review()`; the anchor still exists.
- Test: delete a symbol → its id is `Orphaned`; appears in `session.orphaned()`;
  anchor retained in `by_id`.
- Test: restore the deleted symbol (re-add identical body) → reconciliation
  re-binds to the **retained** id (via hash match), status returns to `Active`.
- Test (no-drop, end-to-end): across a delete then re-add, the id is stable — proving
  an authored record keyed by that id would have survived.
- **Acceptance gate:** all pass; `devenv shell -- tests` green. Commit.

---

## Gate 2 — Final acceptance (must all pass before any layer work)

Run `devenv shell -- tests` with a dedicated identity test module proving:

1. **Cosmetic-edit stability (§5.5.1/§7).** Blank line above a method → same id,
   same content hash.
2. **Unchanged move (§5.5.1).** Class moved to a new file unchanged → same id,
   reported `moved`, content hash unchanged.
3. **Body change keeps id (§5.5.2).** Edit a method body → same id, content hash
   updated.
4. **No authored loss (§5.5.3).** Delete→re-add keeps the id stable; ambiguous
   re-binds are `NeedsReview`, vanished are `Orphaned`, anchors never deleted.
5. **Determinism (§5.5.5).** Shuffled entity order → identical bindings; rebuild
   twice → identical id assignment after reload.
6. **One-to-one (§5.5.4).** Competing matches resolve to a single bind per anchor.
7. **No location-derived identity (§5.6).** Grep the code: no id, qualified_path,
   or structural name incorporates a line/column number.
8. **Persistence no-loss (§11.3.2, identity slice).** save→load and
   close→reopen restore ids exactly; crash-mid-write leaves the prior valid file.
9. **Bounded cost (§5.5.6).** An incremental commit reconciles only the
   touched+closure entity set, not the whole project (assert via instrumentation).

When all nine pass on a clean `devenv shell -- tests`, tag the commit
`gate2-complete`. The two gates are now in place; layer work (code layer
incremental, derived caches, authored layers, the subscription bus) may begin on
this foundation.

## Carrying forward to the code layer (MUST read before Gate 3)

Gate 2 produces `DurableId`s; the code layer is the first consumer of them and is
genuinely downstream of this gate. When the code layer is (re)built over
`DurableId`s — see **`GATE_3_CODE_LAYER_GUIDE.md`** and **SPEC §6** — the following
requirements MUST be enforced. They are easy to miss because nothing fails to
compile if you don't:

- **§6.2.1 — node identity is the `DurableId`.** Graph nodes MUST key on the
  reconciled `DurableId` from this gate, never on location or `file::name`. This is
  what makes a node survive moves/renames and what links all other layers to it.
- **§6.2.2 — small payloads.** Nodes carry id, kind, location, content hash, and
  structural fields only; no vectors or large text.
- **§6.3.2 — phased passes.** A dirty batch MUST be processed as *collect →
  materialise all nodes → structural edges → references → inheritance*; edges MUST
  NOT be resolved before every node in the batch exists.
- **§6.4 — two-pass inheritance (the silent one).** ALL `INHERITS` edges for the
  batch MUST be added **before** any `OVERRIDES` edge is computed. A single
  per-entity pass that does both at once makes override correctness depend on
  processing order whenever an intermediate ancestor is also in the batch (the
  `A → B → C` case). Do not write the intuitive combined loop.

Gate 3 opens by *reproducing* the §6.4 hazard before fixing it, so the team knows
whether it is closing a live bug or hardening against a latent one. Treat the four
points above as acceptance criteria carried into Gate 3, not as background reading.

## Sequencing & escalation notes

- **Order matters:** Steps 1–2 (hashing + extraction) must be solid before Step 5,
  because reconciliation correctness is only as good as the hash's stability. If
  the cosmetic-edit test in Step 1 is flaky, stop — do not build reconciliation on
  an unstable hash.
- **If qualified_path is not unique** (e.g. overloads, conditionals defining the
  same name), make EXACT matching require `(qualified_path, kind)` and let HASH/
  STRUCT disambiguate; document the chosen tie-break. Escalate if the engine cannot
  produce stable qualified names for a construct — that is an identity-input gap to
  resolve at the spec level, not to paper over with line numbers.
- **Rust vs Python host:** if reconciliation is implemented in Python instead,
  ensure it runs inside the same logical write transaction as the native commit
  (do not let it run after the lock is released), preserving §3.3.1.


