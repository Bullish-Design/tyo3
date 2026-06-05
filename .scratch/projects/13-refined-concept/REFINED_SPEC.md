# TyO3 — Refined Specification of High-Risk Components

> Normative specifications and requirements for the components on which TyO3's
> correctness depends. Companion to `REFINED_CONCEPT.md` (the *what/why*) and
> `REFINED_ARCHITECTURE.md` (the *how*).
>
> This document is written as the specification for the library as a whole, not
> for any one change. It covers both newly-introduced components and the
> foundational components that must be preserved exactly. Each section states
> normative requirements, the data it operates on, the invariants it must uphold,
> its failure modes, and the acceptance tests that prove it.

## Conventions

Requirement keywords — **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, **MAY**
— are used in the RFC 2119 sense. A requirement labelled **[INV]** is an invariant
that must hold at every observable point, not merely on a happy path. A
requirement labelled **[GATE]** blocks dependent components: nothing that relies on
it is trustworthy until it is satisfied.

## Shared vocabulary

| Term | Definition |
|---|---|
| **Revision** | Monotonic `u64` application-level counter. Each committed write produces exactly one new revision. |
| **Generation** | An immutable, structurally-shared map of all content (system + virtual paths → `Document`) at one revision. `Arc<ContentMap>`. |
| **ContentHash** | A 128-bit hash of an entity's *normalised* definition. Stable across cosmetic edits; changes on meaningful edits. |
| **DurableId** | A minted, persistent identifier (ULID) for an entity. Stable across content edits, moves, renames, and restarts. |
| **Snapshot** | An independent, revision-pinned read surface over all layers. |
| **Delta** | The precise, durable-id-level description of what a revision changed. |
| **Entity** | A code symbol (or other addressable unit) that layers annotate; identified by a `DurableId`. |

---

# Tier 1 — Foundational (silent-corruption risk)

These components, if wrong, corrupt state invisibly. They are gates for everything
above them.

---

## 1. Authoritative revisioned content store **[GATE]**

### 1.1 Purpose
Be the single system of record for what every revision contains, so that any two
reads of the same revision — by any snapshot, at any time — observe identical
content. All other correctness (snapshot isolation, derived-layer invalidation,
identity reconciliation) depends on this.

### 1.2 Data
```rust
type Generation = Arc<ContentMap>;

struct ContentMap {
    system:  HashTrieMapSync<SystemPathBuf, Document>,
    virtual: HashTrieMapSync<SystemVirtualPathBuf, Document>,
}

enum Document {
    Text    { text: Arc<str>, hash: ContentHash, version: u64 },
    Deleted { version: u64 },              // tombstone: path/dir known-absent at R
}

struct ContentStore {
    head:     Generation,
    revision: Revision,
    retained: BTreeMap<Revision, Generation>,   // bounded time-travel window
    retain_cap: usize,                           // default 256, configurable
}
```

### 1.3 Requirements
- **[INV] 1.3.1** For a fixed revision R, the `Generation` returned for R **MUST**
  be byte-for-byte identical no matter when or by whom it is read. Disk **MUST
  NOT** be consulted to satisfy a read at a committed revision.
- **1.3.2** Every revision-producing event **MUST** write its resulting content
  into the generation for that revision before the revision is published:
  - a text edit writes `Document::Text`;
  - a delete writes `Document::Deleted` (a tombstone);
  - an ingested disk change (sync or watcher) **MUST** read disk **once**, at
    commit time, and intern the result (`Text` or `Deleted`) into the new
    generation. Ingest **MUST NOT** defer the disk read to snapshot time.
- **[INV] 1.3.3** Directory membership **MUST** be pinned at a revision: a snapshot
  enumerating a directory **MUST** observe exactly the set of entries present at
  its revision, including the absence of paths tombstoned at that revision.
- **1.3.4** Generation publication **MUST** be O(1) (an `Arc` swap of a
  structurally-shared map). A write **MUST NOT** copy O(files).
- **1.3.5** `capture(R)` **MUST** be O(1). A captured generation **MUST** remain
  valid and immutable for the lifetime of its holder regardless of `retain_cap`
  eviction.
- **1.3.6** The `retained` window **MUST** evict oldest-first beyond `retain_cap`.
  A `snapshot(at=R)` for an evicted R **MUST** fail with a typed, catchable error
  (`RevisionEvicted`), never return an approximate generation.
- **1.3.7** Each `Document::Text` **MUST** carry the `ContentHash` of its content,
  computed once at intern time (§7), so the hash is available wherever content is
  without recomputation.

### 1.4 Failure modes to defend against
- **Per-reader disk capture.** Capturing disk content into per-snapshot private
  state (rather than the revision-owned generation) re-opens 1.3.1 and **MUST NOT**
  occur. Lazy disk reads, if used, **MUST** intern into the revision-owned
  generation shared by all readers of that revision.
- **Silent membership drift.** Delegating directory enumeration to live disk for a
  pinned revision violates 1.3.3.

### 1.5 Acceptance tests
- Two snapshots pinned at the same R, with disk mutated between their creation,
  return identical content and identical directory listings for every path.
- A snapshot pinned before a path is created on disk does not observe that path.
- A tombstoned path is absent from both reads and directory enumeration at R.
- `snapshot(at=evicted_R)` raises `RevisionEvicted`.
- Publishing a revision over a 10k-file project is O(1) (no per-file work).

---

## 2. Snapshot isolation over independent storage **[GATE]**

### 2.1 Purpose
Let many readers hold consistent, pinned views while a writer mutates, such that
the writer never blocks and readers are never cancelled.

### 2.2 The hard constraint (non-negotiable)
The analysis engine's storage, on mutation, signals cancellation to in-flight
queries and then blocks until it is the sole live handle to its internal state.
**Therefore a snapshot MUST NOT share storage with the live writer database.** Each
snapshot **MUST** be built as an independent database over a frozen generation,
with its own storage handle.

### 2.3 Requirements
- **[INV] 2.3.1** A held snapshot **MUST NOT** be able to block a write. A writer
  committing while N snapshots are held **MUST** complete without waiting on any
  snapshot.
- **[INV] 2.3.2** A snapshot read **MUST NOT** be cancellable by a concurrent
  write. Snapshot reads **MUST NOT** surface analysis-cancellation errors.
- **2.3.3** A snapshot **MUST** read through a frozen overlay (`frozen = Some(R)`):
  a miss is "absent at R," never a live-disk read (consistent with 1.3.1).
- **2.3.4** Snapshot lifetime **MUST** be explicit (context manager / `close()` /
  drop). Holding a snapshot pins its generation; closing it **MUST** release the
  generation for GC.
- **2.3.5** Analysis inside a snapshot **MUST** run with the GIL released so that N
  snapshots compute in true parallel.
- **2.3.6** Building a snapshot **MUST NOT** mutate or warm the head database.

### 2.4 Costs accepted
- Each snapshot warms its own memoised computation lazily (cold reads). This is
  inherent to isolation and **MUST NOT** be "optimised" by sharing storage.
- Memory per snapshot is its memos; content is shared (Arc'd). Bounded by
  lifecycle + `retain_cap`.

### 2.5 Acceptance tests
- 32 held snapshots + a hot writer doing 100 edits: writer never blocks (completes
  under a fixed wall-clock bound); no snapshot read is cancelled.
- A snapshot pinned at R returns R's results after many subsequent head edits.
- Closing all snapshots allows superseded generations to be reclaimed.

---

## 3. The single-writer commit transaction **[GATE]**

### 3.1 Purpose
Guarantee that every committed revision is a single, fully-consistent state across
content, identity, and all layers — with no window in which any participant
observes a half-applied commit.

### 3.2 The transaction
A write commits the following **atomically under one lock**, in order:

```
acquire write lock
  1. mutate content store           → new Generation (one revision bump)
  2. apply change to head analysis  → incremental engine update
  3. reconcile identity (§5)        → durable-id map for the new revision
  4. update code layer (§6)         → graph delta applied
  5. update other derived layers' structural state as required (§8 marks stale)
  6. compute Delta (§4)             → durable-id-level created/changed/deleted
  7. publish generation + revision  → readers may now observe R
release write lock
→ enqueue Delta to the subscription bus (§12), outside the lock
```

### 3.3 Requirements
- **[INV] 3.3.1** Steps 1–7 **MUST** execute under a single mutual-exclusion
  boundary. No layer state (content, identity, code graph, derived structural
  state) **MUST** be observable in a partially-updated form. In particular, layer
  mutations **MUST NOT** occur outside the lock that serialised the content
  mutation.
- **3.3.2** The revision counter **MUST** advance exactly once per committed write
  (see §6 atomicity).
- **[INV] 3.3.3** Publication (step 7) **MUST** be the last in-lock step, so a
  reader that observes revision R also observes the fully-updated layers for R.
- **3.3.4** Delta delivery (bus enqueue) **MUST** happen after publication and
  **MAY** be outside the lock; delivery **MUST NOT** be required for the write to
  be considered committed.
- **3.3.5** Writes are expected to be *partitioned* across participants. The
  library **MUST NOT** require concurrent-edit conflict resolution; it **MUST**
  serialize all writers through this transaction.
- **3.3.6** A panic/error in steps 2–6 **MUST** leave the committed state at the
  prior revision (no torn publish). Either the whole transaction publishes R or it
  rolls back to R−1.

### 3.4 Failure modes to defend against
- **Layer mutation outside the lock.** Updating a code graph or derived layer after
  releasing the write lock allows two writers' layer updates to interleave or apply
  out of revision order. This **MUST NOT** happen (3.3.1).
- **Publish-before-update.** Publishing the revision before layers are updated lets
  a reader see new content with stale layers. Forbidden by 3.3.3.

### 3.5 Acceptance tests
- Under concurrent writers, no snapshot ever observes content at R with code-layer
  state at R−1 (or vice versa).
- A forced error mid-transaction leaves `head == R−1` and all layers consistent at
  R−1.

---

## 4. The delta and reverse-dependency index **[GATE for invalidation/coordination]**

### 4.1 Purpose
Produce, for every revision, the precise set of affected entities so that derived
knowledge self-heals exactly and agents react precisely.

### 4.2 Data
```rust
struct Delta {
    revision: Revision,
    created:  Vec<DurableId>,
    changed:  Vec<DurableId>,
    deleted:  Vec<DurableId>,
    moved:    Vec<Moved>,        // (id, old_path, new_path) from reconciliation
    rescan:   bool,              // true ⇒ delta is unknown ⇒ full rebuild required
}

// reverse-dependency index: target → sources that reference/import/inherit it
ReverseDeps: Map<DurableId, Set<DurableId>>
```

### 4.3 Requirements
- **[INV] 4.3.1 (no miss)** If an entity's content (or its membership/existence)
  changed at R, its `DurableId` **MUST** appear in exactly one of
  created/changed/deleted (or be covered by `rescan = true`).
- **[INV] 4.3.2 (no over-fire)** An entity whose content hash is unchanged at R
  **MUST NOT** appear in `changed`. (Cosmetic edits that don't alter the normalised
  hash do not mark an entity changed — see §7.)
- **4.3.3** The transitive affected set for a changed set S **MUST** be computable
  from the reverse-dependency index without a global scan, as the closure of S
  under inbound dependency edges.
- **4.3.4** The reverse-dependency index **MUST** be maintained incrementally as
  part of the commit transaction (§3) and **MUST** be correct after every revision.
- **4.3.5** A coarse change the engine cannot delta (project/config rescan) **MUST**
  set `rescan = true`; consumers **MUST** treat `rescan` as "rebuild everything."
- **4.3.6** `moved` entries (same `DurableId`, new location) **MUST** be reported
  distinctly from changed/created so consumers can preserve content-hash cache hits
  on moves (§7, §8).

### 4.4 Acceptance tests
- A single-entity edit reports exactly that entity in `changed` and nothing else
  (no over-fire); its importers appear only when their resolution actually changes.
- A pure move (body identical, location changed) appears in `moved`, not in
  created+deleted.
- The transitive affected set equals an independently-computed import/reference
  closure for a multi-file fixture.
- A watcher-ingested disk change produces the same delta precision as an explicit
  edit of the same content.

---

## 5. Identity and reconciliation **[GATE]**

### 5.1 Purpose
Give every entity an identifier that survives content edits, moves, renames, and
restarts, so that authored knowledge attaches to a thing rather than a location,
and derived caches reuse work when content is unchanged.

### 5.2 Two identifiers (MUST be kept distinct)
- **DurableId** — minted ULID; stable while the entity exists; the node identity in
  every layer and the key for authored records.
- **ContentHash** — hash of normalised definition (§7); the key for derived caches;
  the secondary matcher for reconciliation.

- **5.2.1** A `DurableId` **MUST NOT** be derived from content, location, name, or
  line number. (Any of those changes; the id must not.)
- **5.2.2** Entity addressing through the API **MUST** be by `DurableId`. Location
  (path/line) **MAY** be used to *look up* an id but **MUST NOT** be the id.

### 5.3 The identity registry (persisted, §11)
```
identity.db:  DurableId → {
    qualified_path: str,     // last known "pkg/mod.py::Qualified.Name"
    content_hash:   ContentHash,
    kind:           SymbolKind,
    first_seen_rev, last_seen_rev: Revision,
}
```

### 5.4 Reconciliation algorithm
Run **inside** the commit transaction (§3 step 3), over the entities of the
newly-built/updated code layer. For each new-state entity E, bind a `DurableId` by
the first matching rule:

```
1. EXACT:   an anchor A with A.qualified_path == E.qualified_path exists
            → bind E to A.id; confidence = high
2. HASH:    an anchor A with A.content_hash == E.content_hash exists at a
            different path (and A not already bound this pass)
            → bind E to A.id; record a MOVE; confidence = high
            → derived caches keyed by this hash remain valid (cache hit)
3. STRUCT:  an unbound anchor A with same (name, kind, container) in a
            moved/renamed file, or nearest by signature
            → bind E to A.id; confidence = low; mark dependent authored
              records needs-review
4. MINT:    no match
            → mint a new DurableId for E
```
After binding all entities:
```
5. RETIRE:  any anchor unbound this pass whose entity is gone
            → mark its authored records orphaned (NEVER delete them);
              set last_seen_rev; keep the anchor for possible later return
```

### 5.5 Requirements
- **[INV] 5.5.1** An entity whose body is unchanged but whose location moved **MUST**
  keep its `DurableId` (rule 2) and **MUST** preserve its derived-cache hits.
- **[INV] 5.5.2** An entity whose body changed but whose identity is unambiguous
  **MUST** keep its `DurableId` (rule 1) and its content hash **MUST** update.
- **[INV] 5.5.3** Authored records **MUST NEVER** be silently dropped by
  reconciliation. Ambiguous re-binds mark `needs-review`; lost entities mark
  `orphaned`.
- **5.5.4** Binding **MUST** be one-to-one within a pass: an anchor binds to at most
  one entity, an entity to at most one anchor.
- **5.5.5** Reconciliation **MUST** be deterministic given the same prior registry
  and same new-state entity set (no dependence on iteration order). Where rule 3
  could match multiple anchors, the selection **MUST** use a defined total order
  (e.g. smallest edit distance, then lexicographic id) so results are reproducible.
- **5.5.6** Reconciliation cost in the incremental case **MUST** be bounded by the
  changed/affected set, not the whole project.

### 5.6 Failure modes to defend against
- **Line/position-addressed identity.** Falling back to `name@line` (or any
  location-derived id) breaks every layer's link on an unrelated edit above the
  entity, and **MUST NOT** be used. Unqualified/anonymous entities **MUST** be
  identified by content/structure, then anchored to a minted `DurableId`.
- **Greedy mis-binding.** Rule 3 must be conservative and flagged, never silent.

### 5.7 Acceptance tests
- Insert a blank line above a method → its `DurableId` is unchanged; its content
  hash is unchanged; no authored record is flagged.
- Move a class to a new file unchanged → `DurableId` preserved, reported as
  `moved`, embedding cache hits.
- Rename a method, body changed → `DurableId` preserved (rule 1 on container path
  if qualified path tracks, else rule 3 flagged), authored record `needs-review`.
- Delete a class → authored records become `orphaned`, not deleted; restoring the
  class later can re-bind to the retained anchor.
- Same registry + same entities, shuffled processing order → identical bindings.

---

# Tier 2 — Derived-state correctness

---

## 6. Write-path semantics and code-layer incremental update

### 6.1 Write atomicity
- **[INV] 6.1.1** A batched write (`edit_many`) **MUST** advance the revision
  **exactly once** and retain **exactly one** generation. No intermediate
  per-file generation **MUST** be observable via `snapshot(at=...)`.
- **6.1.2** The content store **MUST** expose a batch mutation that applies all map
  changes within a single copy-on-write step (one revision bump, one retained
  generation). Per-file content `version` counters **MAY** still increment per
  file; the *application revision* **MUST** advance once.
- **6.1.3** A single `edit` advances the revision exactly once.

### 6.2 Code layer (L0) structure
- **6.2.1** Node identity **MUST** be the entity's `DurableId` (§5). The graph
  **MUST NOT** key nodes by location.
- **6.2.2** Node payloads **MUST** be small: id, kind, location, content hash, and
  structural fields only. Vectors and large text **MUST NOT** be stored on nodes.
- **6.2.3** Edge kinds (containment, references, inheritance, overrides, imports)
  **MUST** be derived via the analysis engine, not heuristically.

### 6.3 Incremental update (from the Delta)
```
non-rescan:
  drop nodes for changed ∪ deleted entities         # engine auto-removes incident edges
  re-extract nodes + intra/outbound edges for created ∪ changed
  revalidate inbound cross-entity edges from ReverseDeps(changed ∪ deleted)
rescan:
  rebuild the layer from the snapshot
```
- **[INV] 6.3.1** The incremental result **MUST** be identical to a full rebuild
  over the same content (parity).
- **6.3.2** Multi-entity dirty batches **MUST** be processed in phases so that
  cross-references resolve regardless of intra-batch order: **collect → materialise
  all nodes → structural edges → references → inheritance**. References/edges
  **MUST NOT** be resolved before all dirty nodes in the batch exist.

### 6.4 Inheritance / override resolution (multi-level, cross-file)
- **[INV] 6.4.1** Override edges depend on the *complete* inheritance chain. The
  resolver **MUST** add all `INHERITS` edges for the entire dirty set **before**
  computing any `OVERRIDES` edges. A single per-entity pass that computes
  `INHERITS` and `OVERRIDES` together **MUST NOT** be used, because it makes
  override correctness depend on processing order when an intermediate ancestor is
  also dirty.
- **6.4.2** For a chain A → B → C all dirty in one batch, A's overrides of a method
  defined on C **MUST** be resolved correctly.
- **6.4.3** Inheritance resolution that walks the chain via the in-progress graph
  **MUST** only do so after 6.4.1's `INHERITS` pass; alternatively it **MAY**
  resolve transitive ancestry directly from the snapshot.

### 6.5 Acceptance tests
- `edit_many({a, b})` advances revision by 1; `snapshot(at=mid)` is impossible
  (no such revision exists).
- Incremental update == full rebuild for: changed, created, deleted, cross-file
  reference, cross-file inheritance, and override-through-multi-level-chain cases.
- A → B → C across three files edited in one batch yields the A-overrides-C edge,
  independent of file processing order.
- A blank-line edit above a symbol does not churn its node id (depends on §5).

---

## 7. Content hashing (normalisation)

### 7.1 Purpose
Provide a hash that changes when an entity's *meaning* changes and is stable across
cosmetic edits, so derived caches and the delta are precise.

### 7.2 Requirements
- **7.2.1** The hash **MUST** be computed over a canonical rendering of the entity's
  AST subtree, not raw bytes.
- **7.2.2** Normalisation policy **MUST** be explicit and configurable per layer.
  Default: insensitive to surrounding whitespace and trailing-comma style;
  sensitive to identifiers, literals, and structure. Comment/docstring sensitivity
  **MUST** be configurable (an embeddings layer may want docstrings included; a
  pure-structure layer may not).
- **[INV] 7.2.3** Two entities with identical normalised form **MUST** produce
  identical hashes (so moves/duplications hit caches); two entities differing in
  meaning **MUST** (modulo cryptographic collision probability) differ.
- **7.2.4** Hashing **MUST** occur once at content intern time and be stored on the
  document/node (1.3.7).
- **7.2.5** Hash width **MUST** be ≥ 128 bits to make accidental collision
  negligible at repo scale.

### 7.3 Acceptance tests
- Reformatting (whitespace only) does not change the hash; renaming an identifier
  does; changing a literal does.
- Two identical helper functions in different files share a hash (and an embedding,
  if the layer dedupes by hash).

---

## 8. Derived-layer cache and invalidation

### 8.1 Purpose
Make derived knowledge (embeddings, extracted docstrings, summaries) self-healing
and cheap by caching per content hash and invalidating from the delta.

### 8.2 Model
```
derived[layer][content_hash] -> artifact        # immutable per hash
```
- **8.2.1** Derived artifacts **MUST** be keyed by `ContentHash` (plus a
  `generator_version`), **not** by revision or `DurableId`.
- **[INV] 8.2.2** A given `(content_hash, generator_version)` **MUST** map to a
  single immutable artifact, so the store needs no revision awareness and is
  trivially consistent across revisions and agents.
- **8.2.3** On a delta, for each affected entity the new content hash is compared
  to the cached binding:
  - **hash unchanged** → artifact valid; **MUST** be reused (no recompute).
  - **hash changed/missing** → mark stale; schedule recompute via the DAG (§9).
- **8.2.4** A `generator_version` bump (model/prompt change) **MUST** invalidate the
  whole layer's artifacts logically (new key space), without touching code or other
  layers.
- **8.2.5** Serving policy on staleness **MUST** be explicit per layer: either
  serve the previous artifact tagged `stale`, or block until recompute. Default:
  serve `stale` for reads, recompute asynchronously.
- **8.2.6** Vector storage and nearest-neighbour search **MUST** be delegated to an
  external store; TyO3 holds the hash↔artifact keys and the linkage, not the ANN
  implementation.

### 8.3 Acceptance tests
- Edit entity X; only X's (and genuinely-affected dependents') artifacts recompute;
  unrelated artifacts are untouched (no over-fire).
- Move X unchanged; its artifact is reused (cache hit via hash; §5 rule 2).
- Bump `generator_version`; subsequent reads recompute; prior keys remain for
  rollback.
- `snapshot.nearest(q, k)` at revision R returns neighbours consistent with R.

---

## 9. Derivation DAG orchestrator

### 9.1 Purpose
Schedule recomputation of derived layers in dependency order, on the precise
affected set, without infinite cascades.

### 9.2 Requirements
- **9.2.1** Each derived layer **MUST** declare its dependency layers, forming a DAG
  rooted at code (L0).
- **[INV] 9.2.2** The dependency graph **MUST** be acyclic; layer registration
  **MUST** reject a cycle.
- **9.2.3** On a delta the orchestrator **MUST**: compute the affected entity set
  (§4 closure), then walk the DAG in topological order marking dependent artifacts
  stale, then schedule recompute (lazy or eager per layer policy).
- **9.2.4** Authored layers **MUST NOT** be automatic derivation sources (they are
  sinks for the cascade), so reactions always terminate.
- **9.2.5** Recompute scheduling **MUST** be idempotent: scheduling the same
  `(layer, content_hash)` twice **MUST** compute it at most once.
- **9.2.6** A recompute failure **MUST** leave the prior artifact intact and the
  target marked `stale/failed`, never a partial artifact.

### 9.3 Acceptance tests
- Editing code marks embeddings + docstrings + descriptions stale (topo order); a
  description-embedding layer (depending on descriptions) recomputes only after
  descriptions.
- A registration that would introduce a cycle is rejected.
- Two rapid deltas affecting the same entity schedule one recompute, not two.

---

# Tier 3 — Durability and coordination

---

## 10. Cross-layer consistency at a snapshot

### 10.1 Purpose
Guarantee that every layer read from one snapshot describes the same revision.

### 10.2 Requirements
- **[INV] 10.2.1** A snapshot pins: the content generation, the reconciled identity
  map, and therefore each entity's content hash — all at its revision R.
- **[INV] 10.2.2** For any `DurableId`, `snapshot.code(id)`,
  `snapshot.embedding(id)`, `snapshot.docstring(id)`, and `snapshot.authored(layer,
  id)` **MUST** all resolve against R: code from the frozen analysis DB; derived
  artifacts via R's content hash for that id; authored values via R's authored
  state for that id.
- **10.2.3** Resolving a derived artifact that is `stale` at R **MUST** be reported
  as such (the artifact is the last-good for that hash), never silently presented as
  fresh.
- **10.2.4** Cross-layer reads **MUST NOT** require taking the write lock.

### 10.3 Acceptance tests
- At a pinned R, an entity's code node, embedding (by hash), and authored intent are
  mutually consistent even after many head writes.
- Time-travel to R0 and R1 and diff any layer pair; results match independent
  rebuilds at R0 and R1.

---

## 11. The `.tyo3/` sidecar and persistence round-trip

### 11.1 Purpose
Persist durable knowledge (identity, authored layers) and a regenerable cache,
committable to version control, without polluting source.

### 11.2 Layout (normative)
```
.tyo3/
  config.toml          # layer defs, generators, policies, retain caps, hash policy
  identity.db          # DurableId → {qualified_path, content_hash, kind, revs}
  authored/<layer>/    # authored records keyed by DurableId (+ history)
  cache/<layer>/       # content-hash-keyed derived artifacts (regenerable)
  revisions/           # OPTIONAL retained delta log for cross-restart history
```

### 11.3 Requirements
- **11.3.1** `identity.db` and `authored/` are **durable knowledge** and **MUST**
  be safe to commit. `cache/` is **regenerable** and **MUST** be safe to delete and
  **MAY** be `.gitignore`d.
- **[INV] 11.3.2 (no loss)** A persist → process-exit → reload → reconcile
  round-trip **MUST** restore identity bindings and authored records exactly (modulo
  reconciliation re-binding to current code). No authored record **MUST** be lost or
  silently altered.
- **11.3.3** Sidecar writes by TyO3 **MUST** go through the single write
  transaction (§3), so TyO3 never produces a torn sidecar. Writes **MUST** be
  crash-safe (write-temp-then-rename or equivalent) so an interrupted write leaves
  the prior valid state.
- **11.3.4** The sidecar **MUST NOT** require any modification to source files. (No
  in-source markers; identity lives in `identity.db`, not in code.)
- **11.3.5** Formats **MUST** be versioned; a reader encountering a newer format
  **MUST** fail loudly with a typed error, never silently misread.
- **11.3.6** Collaborators without TyO3 **MUST** be unaffected: the sidecar is inert
  data; its presence or absence **MUST NOT** change program behaviour.
- **11.3.7** `identity.db` keys (`DurableId`) and `cache/` keys (`ContentHash`)
  **MUST** be order-independent so that version-control merges of divergent sidecars
  are content merges, not positional ones.

### 11.4 Failure modes to defend against
- **Cache treated as truth.** Reading a derived artifact whose hash no longer
  matches any entity **MUST NOT** be served as current (it is orphaned cache, GC-able).
- **Reconcile-on-load skipped.** Reloading authored records without reconciling
  against current code **MUST NOT** happen (it would attach notes to wrong entities).

### 11.5 Acceptance tests
- Persist a project with authored intent + embeddings; delete `cache/`; reload →
  identity + authored intact, embeddings recompute, no data loss.
- Persist, edit code out-of-band, reload → reconciliation re-binds; moved entities
  keep authored notes; renamed-with-change entities flagged `needs-review`.
- Simulated crash mid-sidecar-write leaves the previous valid `identity.db`.
- A newer on-disk format version is rejected with a typed error.

---

## 12. The delta subscription bus

### 12.1 Purpose
Deliver precise change notifications to agents so they react to exactly what
matters, instead of polling and re-deriving.

### 12.2 Requirements
- **12.2.1** A subscriber registers an `interest` (a set of files/ids, a layer, or
  ALL). On each committed revision, the bus **MUST** deliver a delta to that
  subscriber iff the transitive affected set (§4.3.3) intersects its interest,
  scoped to that intersection.
- **[INV] 12.2.2** Deltas to a single subscriber **MUST** be delivered in revision
  order and **MUST NOT** be reordered or coalesced in a way that hides an
  intervening change to its interest. (Coalescing multiple revisions into one
  notification is permitted only if the union of affected sets is delivered.)
- **12.2.3** Each delivered delta **MUST** carry its `revision`, so the subscriber
  can `snapshot(at=revision)` to read exactly the state it was notified about.
- **12.2.4** Delivery **MUST** occur after publication (§3.3.4) and **MUST NOT**
  block the write transaction. A slow/dead subscriber **MUST NOT** stall the writer
  or other subscribers (per-subscriber queue with a bounded/backpressure policy).
- **12.2.5** The bus **MUST NOT** guarantee that the notified revision is still
  un-evicted by the time the subscriber reads; a subscriber that lags past
  `retain_cap` **MUST** receive a typed `RevisionEvicted` on snapshot and fall back
  to a rescan.
- **12.2.6** Subscription teardown **MUST** be clean: dropping a subscriber releases
  its queue and any pinned revisions.

### 12.3 Acceptance tests
- A subscriber interested in `models.py` is notified on edits to it and to its
  importers (via reverse-dep), and not on unrelated edits.
- Notifications arrive in revision order; a burst of writes yields either ordered
  per-revision deltas or a correctly-unioned coalesced delta.
- A blocked subscriber does not delay the writer; on resume it either catches up or
  receives `RevisionEvicted` and rescans.

---

## 13. Cross-cutting requirements

### 13.1 Error model
- **13.1.1** All recoverable conditions **MUST** surface as typed, catchable errors:
  `RevisionEvicted`, `ProjectClosed`, `PathResolution`, `FormatVersion`,
  `GeneratorFailed`, `ReconcileAmbiguous`. Analysis cancellation **MUST NOT** escape
  the snapshot read surface (§2.3.2).
- **13.1.2** Library code **MUST NOT** write to stderr directly for operational
  warnings; it **MUST** use a logging facade so an embedding host controls output.

### 13.2 Determinism
- **13.2.1** Given identical content and identical sidecar state, all of: identity
  bindings (§5.5.5), content hashes (§7), the code layer (§6.3.1), and the delta
  (§4) **MUST** be reproducible across runs and machines.

### 13.3 Performance envelope (targets, measured in §14)
- Single-entity edit → commit (content + reconcile + delta + L0 update) **SHOULD**
  be ≪ a full rebuild and bounded by the affected set, not project size.
- Snapshot creation **SHOULD** be O(1) in content (Arc capture) + the fixed cost of
  building an empty analysis DB; per-query warmup is paid lazily.
- Memory **SHOULD** be bounded across many revisions/snapshots by `retain_cap` +
  live-snapshot count; large artifacts live outside the graph and the generations.

---

## 14. Verification matrix (summary)

| # | Component | Must-prove property | Key test |
|---|---|---|---|
| 1 | Content store | Same R ⇒ same content & membership, always | two snapshots @ R, disk mutated between |
| 2 | Snapshot isolation | Writer never blocks; reads never cancelled | 32 snapshots + hot writer |
| 3 | Commit transaction | No partially-applied revision observable | concurrent writers; forced mid-tx error |
| 4 | Delta + reverse-dep | No miss, no over-fire; exact closure | single edit; move; multi-file closure |
| 5 | Identity/reconcile | Id survives move/rename/restart; no authored loss | move, rename, delete, reload, shuffle |
| 6 | Write path + L0 | edit_many atomic; incremental == rebuild; override chain | A→B→C batch; parity suite |
| 7 | Content hash | Stable to cosmetic, sensitive to meaning | reformat vs rename vs literal change |
| 8 | Derived cache | Keyed by hash; reuse on move; precise invalidation | edit/move/generator-bump |
| 9 | Derivation DAG | Acyclic; topo order; terminates; idempotent | cycle reject; layered recompute |
| 10 | Cross-layer | All layers describe the same R | pinned read after many writes; time-travel diff |
| 11 | Sidecar | Round-trip no loss; crash-safe; non-invasive | persist/exit/reload; delete cache; crash sim |
| 12 | Subscription bus | Scoped, ordered, non-blocking delivery | interest filter; burst; blocked subscriber |

All verification runs under the project's devenv scripts.
