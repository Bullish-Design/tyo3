# TyO3 — Implementation Concept

> The background a contributor needs to understand and execute the companion
> `REFINED_IMPLEMENTATION_PLAN.md`. This document is self-contained: it defines the
> system, its vocabulary, the rules the implementation must satisfy, the shape of
> the code as it exists today, and the architecture we are moving it to. You do not
> need any other document, prior knowledge of the project, or its history.

---

## 1. What TyO3 is

TyO3 is a **real-time, multi-agent, multi-layer code-intelligence substrate**: a
library that maintains a live, queryable model of a Python codebase and the
knowledge layered on top of it (the code graph, embeddings, extracted docstrings,
generated descriptions, and human/agent-authored notes), so that many agents can
read consistent views in parallel while the code changes underneath them.

It is a **Python library with a Rust core**, built with PyO3 and maturin. The Rust
core is in `rust/src/`; the Python package is in `src/tyo3/`. A compiled extension
module bridges them.

TyO3 owns only a small, focused spine and delegates everything hard:

| Concern | Delegated to |
|---|---|
| Python semantic analysis, type queries | the **ty** analysis engine |
| Parsing, source/notebook handling | **ruff** primitives |
| Incremental file/dir/config change application, file watching | the analysis engine |
| Memoisation & cancellation of analysis | **salsa** (the engine's query database) |
| Graph algorithms (reachability, cycles, centrality, topo order) | **rustworkx** (Python) |
| Vector storage + nearest-neighbour search | an external vector store |
| Embedding/description *generation* | external models (TyO3 only orchestrates) |
| Persistent (structurally-shared) map | **rpds** (a HAMT) |

What TyO3 *owns*: the revisioned content store, identity (durable ids + content
hashing + reconciliation), the change delta and reverse-dependency index, the layer
model, the on-disk sidecar format, and coordination (the single writer, the
subscription bus, the derivation DAG).

---

## 2. Vocabulary

These terms are used precisely throughout the plan.

| Term | Meaning |
|---|---|
| **Revision** | A monotonic `u64` application-level counter. Each committed write produces exactly one new revision. |
| **Generation** | An immutable, structurally-shared map of all content at one revision: every relevant path → `Document`. Held as `Arc<ContentMap>`, so cloning it (capturing it for a reader) is O(1) and producing the next one shares structure with the previous (O(log n) insert). |
| **ContentHash** | A ≥128-bit hash of an entity's *normalised* definition. Stable across cosmetic edits (whitespace, formatting); changes on meaningful edits. The key for derived caches and the matcher for reconciliation. |
| **DurableId** | A minted, persistent identifier (a ULID) for an entity. Stable across content edits, moves, renames, and process restarts. The node identity in every layer and the key authored notes attach to. |
| **Snapshot** | An independent, revision-pinned read surface over all layers. Reading the same revision through any snapshot, at any time, yields identical results. |
| **Entity** | An addressable code unit a layer annotates (a function, class, method, module-level binding, …), identified by a `DurableId`. |
| **Delta** | The precise, `DurableId`-level description of what one revision changed (created / changed / deleted / moved), plus the transitively-affected set. |
| **Layer** | A set of per-entity records keyed by `DurableId`. *Derived* layers are pure functions of code content (embeddings, docstrings); *authored* layers have no upstream (human/agent intent). |

---

## 3. The one hard constraint (it dictates the whole concurrency design)

The analysis engine memoises queries in a salsa database. Salsa has a property that
shapes everything:

> On any mutation, the salsa storage sets a cancellation flag for in-flight queries
> and then **blocks until it is the sole live handle** to its internal state. Any
> clone of the live database shares that state and that handle count.

Consequence: a reader holding a clone of the *live* database does not merely risk
having its query cancelled — it **blocks the writer indefinitely**, because the
writer cannot proceed until it is the sole handle. Shared-storage reuse and
non-blocking writes cannot coexist on one database.

Therefore: **each pinned revision gets its own independent analysis database over an
immutable content view.** The writer's database is never shared with readers, so its
mutations never block; readers are never cancelled. The accepted cost is that each
snapshot warms its own memoised computation lazily (cold reads). This is the price
of true isolation and must not be "optimised" by sharing storage.

---

## 4. System shape

```
┌──────────────────────────────────────────────────────────────────────────┐
│                                 TyO3 SPINE                                  │
│  Identity ───────── Revision / Content ───────── Delta + Reverse-Dep       │
│  (durable ids,      (immutable content store,    (created/changed/deleted, │
│   reconciliation)    snapshot isolation)          who-depends-on-what)     │
└───────────┬───────────────────┬────────────────────────┬──────────────────┘
            │ links by id       │ pins revision          │ notifies
            ▼                   ▼                        ▼
   ┌─────────────────┐  ┌──────────────────┐     ┌────────────────────────┐
   │ LAYERS          │  │ SNAPSHOTS        │     │ COORDINATION           │
   │ • code (L0)     │  │ consistent view  │     │ • single write lock    │
   │ • embeddings    │  │ across ALL layers│     │ • delta subscription   │
   │ • docstrings    │  │ at one revision  │     │   bus                  │
   │ • descriptions  │  └──────────────────┘     │ • derivation DAG       │
   │ • authored      │                           └────────────────────────┘
   └────────┬────────┘
            │ durable state
            ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ .tyo3/ sidecar:  identity registry · authored layers · derived cache   │
   └──────────────────────────────────────────────────────────────────────┘
```

- **The spine**: identity, revisioned content, the delta. Small, fully owned.
- **The layers**: code (the L0 graph) and all knowledge-about-code, linked by id.
- **The snapshots**: the one read surface; every layer at one pinned revision.
- **The sidecar**: durable, committable, on-disk state under `.tyo3/`.
- **Coordination**: the single write lock, the subscription bus, the derivation DAG.

---

## 5. The rules the implementation must satisfy

These are the normative requirements. Keywords **MUST / MUST NOT / SHOULD** are
used in their usual strong sense. A rule marked **[INV]** is an invariant: it must
hold at every observable point, not merely on a happy path.

### 5.1 Authoritative revisioned content
The content store is the single system of record for what each revision contains.

- **[INV]** For a fixed revision R, the `Generation` returned for R **MUST** be
  identical no matter when or by whom it is read. Disk **MUST NOT** be consulted to
  satisfy a read at a committed revision.
- Every revision-producing event **MUST** write its resulting content into the
  generation for that revision *before* the revision is published: a text edit writes
  a text document; a delete writes a tombstone; an ingested disk change reads disk
  **once**, at commit time, and interns the result. Ingest **MUST NOT** defer the
  disk read to snapshot time.
- **[INV]** Directory membership **MUST** be pinned at a revision: enumerating a
  directory in a snapshot observes exactly the entries present at its revision,
  including the absence of paths tombstoned there.
- Publishing a revision **MUST** be O(1) (an `Arc` swap of a structurally-shared
  map); capturing a generation for a snapshot **MUST** be O(1) and remain valid for
  the holder's lifetime regardless of eviction.
- A bounded retained window evicts oldest-first. A request to pin an evicted
  revision **MUST** fail with a typed, catchable `RevisionEvicted` error, never an
  approximate generation.
- Each text document **MUST** carry the `ContentHash` of its content, computed once
  at intern time, so the hash is available wherever content is, without recomputation.

### 5.2 Snapshot isolation
- **[INV]** A held snapshot **MUST NOT** be able to block a write; a writer
  committing while N snapshots are held **MUST** complete without waiting on any.
- **[INV]** A snapshot read **MUST NOT** be cancellable by a concurrent write, and
  **MUST NOT** surface analysis-cancellation errors.
- A snapshot reads through a frozen overlay: a miss is "absent at R," never a
  live-disk read.
- Snapshot lifetime **MUST** be explicit (context manager / close / drop). Holding
  it pins its generation; closing it releases the generation for reclamation.
- Building a snapshot **MUST NOT** mutate or warm the live head database.
- Analysis inside a snapshot **SHOULD** run with the GIL released so N snapshots
  compute in true parallel.

### 5.3 The single-writer commit transaction
Every committed revision is a single, fully-consistent state across content,
identity, and all layers, with no window in which any participant observes a
half-applied commit. A write performs, **atomically under one lock, in order**:

```
acquire write lock
  1. mutate content store           → new Generation (one revision bump)
  2. apply change to head analysis  → incremental engine update
  3. reconcile identity             → durable-id map for the new revision
  4. update the code layer          → graph delta applied
  5. mark dependent derived state stale
  6. compute the Delta              → durable-id-level created/changed/deleted/moved
  7. publish generation + revision  → readers may now observe R
release write lock
→ enqueue the Delta to the subscription bus, outside the lock
```

- **[INV]** Steps 1–7 **MUST** execute under a single mutual-exclusion boundary. No
  layer state (content, identity, code graph, derived structural state) **MUST** be
  observable in a partially-updated form. Layer mutations **MUST NOT** occur outside
  the lock that serialised the content mutation.
- **[INV]** Publication (step 7) **MUST** be the last in-lock step, so a reader that
  observes revision R also observes the fully-updated layers for R.
- The revision counter **MUST** advance exactly once per committed write. A batched
  write advances it exactly once and retains exactly one generation; no intermediate
  per-file revision is observable.
- Delta delivery to the bus happens after publication and **MAY** be outside the
  lock, but delivery **MUST NOT** be required for the write to be considered
  committed, and **MUST NOT** block the writer.
- A panic or error in steps 1–6 **MUST** leave committed state at the prior revision
  (no torn publish): either the whole transaction publishes R or it rolls back to
  R−1. This includes durable side effects — a commit that succeeds in memory but
  fails to persist its sidecar is not committed.
- Writers are *partitioned* (different agents touch different entities). The library
  serialises all writers through this transaction; it does **not** do concurrent-edit
  merge/conflict resolution.

### 5.4 The delta and reverse-dependency index
```
Delta {
  revision: Revision,
  created:  Vec<DurableId>,
  changed:  Vec<DurableId>,                 // content hash changed
  deleted:  Vec<DurableId>,
  moved:    Vec<{ id, old_location, new_location }>,
  affected: Vec<DurableId>,                 // transitive closure under reverse-deps
  rescan:   bool,                           // true ⇒ delta unknown ⇒ full rebuild
}
ReverseDeps: Map<DurableId, Set<DurableId>> // target → sources that reference it
```
- **[INV] no miss**: if an entity's content, membership, or existence changed at R,
  its `DurableId` **MUST** appear in exactly one of created/changed/deleted (or be
  covered by `rescan`).
- **[INV] no over-fire**: an entity whose content hash is unchanged at R **MUST
  NOT** appear in `changed`.
- A `moved` entry (same `DurableId`, new location, unchanged body) **MUST** be
  reported distinctly from created+deleted, so caches keyed by content hash stay hit.
- The transitively-affected set for a changed set S **MUST** be computable from the
  reverse-dependency index (the closure of S under inbound edges), without a global
  scan. The index **MUST** be maintained incrementally inside the commit and be
  correct after every revision.
- Delta fields are `DurableId`-level. File paths **MAY** accompany the delta as
  metadata but **MUST NOT** stand in for entity ids.

### 5.5 Identity and reconciliation
Every entity has two distinct identifiers and conflating them is the classic
mistake to avoid:
- the **DurableId** (minted once, never derived from content/location/name/line),
- the **ContentHash** (of the normalised body; changes with meaning).

A persisted registry maps `DurableId → { qualified_path, content_hash, kind,
first_seen_rev, last_seen_rev }`. Reconciliation runs *inside* the commit, over the
entities of the freshly-updated code layer. For each new-state entity, bind a
`DurableId` by the first matching rule:

1. **Exact** — an anchor with the same qualified path → bind (high confidence; the
   common "nothing moved" case).
2. **Hash** — an anchor with the same content hash at a *different* path → bind and
   record a **move** (high confidence; derived caches keyed by this hash stay hit).
3. **Structural** — an unbound anchor with the same (name, kind, container) in a
   moved/renamed file, or nearest by signature → bind (low confidence; mark
   dependent authored records `needs-review`).
4. **Mint** — no match → mint a new `DurableId`.

After binding all entities: any anchor left unbound whose entity is gone is
**retired** — its authored records are marked `orphaned` (**NEVER deleted**); the
anchor is kept for possible later return.

- **[INV]** A moved-but-unchanged entity keeps its `DurableId` and its cache hits.
- **[INV]** A changed-but-unambiguous entity keeps its `DurableId`; its content
  hash updates.
- **[INV]** Authored records are **NEVER** silently dropped by reconciliation.
- Binding is one-to-one within a pass. Reconciliation is deterministic given the
  same prior registry and the same entity set (no dependence on iteration order);
  where rule 3 could match several anchors, selection uses a defined total order.

### 5.6 Content hashing (normalisation)
- The hash **MUST** be computed over a **canonical rendering of the entity's AST
  subtree**, not raw source bytes.
- Default policy: insensitive to surrounding whitespace and trailing-comma style;
  **sensitive to identifiers, literals (including the exact text inside string
  literals), and structure**. Docstring and comment sensitivity is configurable per
  layer (an embeddings layer may want docstrings; a structure layer may not).
- **[INV]** Two entities with identical normalised form produce identical hashes;
  two entities differing in meaning differ in hash (modulo cryptographic collision).
- Hashing occurs once at intern time; the hash is stored on the document/node. Width
  ≥128 bits.

### 5.7 Derived-layer cache and invalidation
```
derived[layer][content_hash] -> artifact   // immutable per (content_hash, generator_version)
```
- Derived artifacts **MUST** be keyed by `ContentHash` (plus a `generator_version`),
  **not** by revision or `DurableId`. A given key maps to a single immutable
  artifact, so the store needs no revision awareness.
- On a delta, for each affected entity compare the new content hash to the cached
  binding: unchanged ⇒ reuse (no recompute); changed/missing ⇒ mark stale and
  schedule recompute.
- A `generator_version` bump invalidates a layer's artifacts logically (new
  key-space) without touching other layers; prior keys remain for rollback.
- Serving policy on staleness is explicit per layer (serve last-good tagged `stale`,
  or block until recompute). A recompute failure leaves the prior artifact intact and
  marks the target `failed`, never a partial artifact.
- Vector storage and nearest-neighbour search are delegated to an external store;
  TyO3 holds only the hash↔artifact keys and the linkage.

### 5.8 Derivation DAG
- Each derived layer declares its dependency layers, forming a DAG rooted at the
  code layer. Registration **MUST** reject a cycle.
- On a delta: compute the affected set, walk the DAG in topological order marking
  dependent artifacts stale, then schedule recompute (lazy or eager per policy).
- Authored layers are **not** automatic derivation sources (they are sinks), so
  reactions always terminate. Scheduling the same `(layer, content_hash)` twice
  computes it at most once.

### 5.9 Cross-layer consistency
- **[INV]** A snapshot pins the content generation, the reconciled identity map, and
  therefore each entity's content hash — all at one revision R. For any `DurableId`,
  the code node, derived artifacts (resolved via R's content hash), and authored
  values **MUST** all resolve against R. Cross-layer reads **MUST NOT** take the
  write lock.

### 5.10 The `.tyo3/` sidecar
```
.tyo3/
  config.toml          # layer defs, generators, policies, retain caps, hash policy
  identity.db          # DurableId → { qualified_path, content_hash, kind, revs }
  authored/<layer>/    # authored records keyed by DurableId (+ history)
  cache/<layer>/       # content-hash-keyed derived artifacts (regenerable)
  revisions/           # OPTIONAL retained delta log
```
- `identity.db` and `authored/` are **durable knowledge** and must be safe to
  commit to version control. `cache/` is **regenerable** and may be deleted/ignored.
- **[INV] no loss**: persist → exit → reload → reconcile restores identity bindings
  and authored records exactly (modulo reconciliation re-binding to current code).
- Sidecar writes go through the single write transaction and **MUST** be crash-safe
  (write-temp-then-rename). Formats are versioned; reading a newer format **MUST**
  fail loudly with a typed error, never silently misread.
- The sidecar never modifies **source files**; it never changes program
  behaviour for collaborators who do not use TyO3.
  - **Clarification (not a violation):** TyO3 **MAY create** the `.tyo3/`
    directory on `open` — e.g. persisting `identity.db` from the identity
    reconciliation run at open is expected and desirable. This is *not* a
    violation of "presence/absence never changes behaviour": `.tyo3/` is
    git-ignored for shared projects, so it never reaches collaborators and never
    affects anyone who does not use TyO3. The invariant that matters is that no
    **source** file is created, deleted, or modified by opening or reading a
    project. Do **not** re-file "opening a sidecar-less project creates `.tyo3/`"
    as a §5.10 violation. (Tests assert only that the *source* tree is
    unchanged, excluding `.tyo3/`.)

### 5.11 The subscription bus
- A subscriber registers an *interest* (a set of files/ids, a layer, or ALL). On each
  committed revision the bus delivers a delta to that subscriber iff the transitive
  affected set intersects its interest, scoped to that intersection.
- **[INV]** Deltas to a single subscriber are delivered **in revision order** and
  are not reordered or coalesced in a way that hides an intervening change (coalescing
  is permitted only if the union of affected sets is delivered).
- Each delivered delta carries its revision, so the subscriber can pin a snapshot at
  exactly the revision it was notified about.
- Delivery occurs after publication and **MUST NOT** block the write transaction. A
  slow or dead subscriber **MUST NOT** stall the writer or other subscribers (each
  subscriber has its own bounded queue with a non-blocking backpressure policy).
- A subscriber that lags past the retained window receives a typed `RevisionEvicted`
  on its next snapshot and falls back to a rescan.

### 5.12 Error model & determinism
- Recoverable conditions surface as typed, catchable errors (`RevisionEvicted`,
  `ProjectClosed`, `PathResolution`, `FormatVersion`, `GeneratorFailed`,
  `ReconcileAmbiguous`, `SidecarWriteError`, `CommitFailed`). Analysis cancellation
  never escapes the snapshot read surface. Library code uses a logging facade, not
  direct stderr writes.
- Given identical content and identical sidecar state, all of identity bindings,
  content hashes, the code layer, and the delta are reproducible across runs and
  machines.

---

## 6. The architecture as it exists today (your starting point)

This section describes the current code so you do not have to reverse-engineer it.
It works for single-threaded use and passes its current suite, but it violates
several rules in §5. The plan exists to close those violations.

### 6.1 The Rust core (`rust/src/`)
- `project.rs` (large): defines `TyProject` and its per-revision `HeadState`; the
  PyO3 write methods (`edit`, `edit_many`, `edit_virtual`, `sync_path`, `discard`,
  `sync_all`, `author`, watcher `poll_changes`); `commit_head` (the write body, run
  under an inner `Mutex`); `build_frozen` and `pre_populate_generation` (snapshot
  construction); `snapshot`; the watcher.
- `content.rs`: the `ContentStore` (immutable `Generation`s, retained window).
- `overlay.rs`: the `OverlaySystem`, one implementation serving two roles — a *live
  head* role that falls through to disk on a miss and interns the result, and a
  *frozen* role pinned to one revision that serves only its generation.
- `identity.rs`: durable-id reconciliation.
- `hash.rs`: content hashing (`HashPolicy`, `normalise_entity_source`).
- `entity.rs`: the `Entity` model extracted from analysis.
- `authored.rs`, `config.rs`, `sidecar.rs`: authored store, config load/validate,
  sidecar paths.
- `dto/`: wire structs surfaced to Python, including `dto/sync.rs`'s `SyncResultDto`.

### 6.2 The Python package (`src/tyo3/`)
- `session.py` (large): `TyO3Session` — the public facade. Holds the write methods
  (each calls the native op, then Python post-commit steps), `snapshot`, the
  convenience read sugar (`code`, `layer`, `entity`, `diff`), the `graph` property,
  the watcher daemon thread, and glue to the bus and derivation DAG.
- `graph/graph.py` (large): `CodeGraph` — a rustworkx graph built and updated in
  Python by reading the analysis "read surface" (document symbols, occurrences,
  supertypes) over FFI; plus queries, diff, diagnostics, a file-level
  reverse-dependency index (`_file_importers`).
- `derive/`: the derivation DAG, recompute scheduler, derived-layer model, cache.
- `bus/`: the bus, per-subscriber subscription queues, the delta wrapper, interest.
- `layers/`, `stores/`, `models/`, `authored/`, `sidecar.py`, `config.py`: layer
  views, artifact stores, pydantic models, authored facade, config mirror.

### 6.3 How a write flows today, and where it breaks §5
```
session.edit(...)                       (Python, NO lock held)
  → self._inner.edit(...)               (Rust: commit_head under Mutex →
                                          mutate content, reconcile identity,
                                          build a path-shaped SyncResult, publish,
                                          RELEASE Mutex, return)
  → self._apply_graph_delta(result)     (Python: rebuild/update rustworkx from the
                                          read surface via many FFI calls)
  → self._publish_delta(result)         (Python: push to the bus)
```
The concrete deviations from §5 you will remove:

1. **Snapshot construction reads live disk.** `build_frozen` calls
   `pre_populate_generation`, which walks the project root with a live OS filesystem
   and interns whatever disk currently says for any relevant file missing from the
   generation. This makes capture O(files) and lets a snapshot at R observe content
   that was never committed at R — violating §5.1 and §5.2.

2. **The transaction is split across the lock boundary.** Steps 4 (code layer) and 5
   (derived) run in Python *after* the Rust `Mutex` is released, and there is **no
   Python-side write lock** at all. A reader can observe content at R with the code
   graph still at R−1; two writer threads can apply graph deltas or publish bus
   deltas out of revision order. This violates §5.3.

3. **A read accessor performs a write.** The `graph` property builds the `CodeGraph`,
   which calls a priming routine that invokes `sync_all()` — a write that advances
   the revision — so reading `session.graph` can change `session.head`. Violates §5.3
   ("reads do not write") and §5.9.

4. **The delta is path-shaped, not id-level.** `SyncResultDto`'s
   `created/changed/deleted` are file-path strings; `moved` carries *qualified-path*
   strings; only `needs_review/orphaned/authored` are durable ids — all flattened
   into untyped string vectors. A single file with two changed entities cannot be
   distinguished; a move is reported as a string, not `{id, old, new}`. Violates §5.4.

5. **Derived invalidation is silently inert.** It is fed the path-shaped
   `created/changed` values as if they were durable ids; the per-entity hash lookup
   raises and is swallowed, so nothing is invalidated. It also opens a second
   snapshot it never closes. Violates §5.7.

6. **One write path forgets to publish.** Because each write method hand-copies the
   post-commit sequence, `discard` applies the graph delta but never publishes to the
   bus — a committed revision no subscriber hears about. Violates §5.11.

7. **Convenience reads return views over closed snapshots.** `session.code`,
   `session.layer`, and `session.entity` open a snapshot, take a lazy view, then
   close the snapshot before returning the view; later access fails. Violates §5.2's
   lifetime contract.

8. **Hashing is text-heuristic, not AST-canonical.** `normalise_entity_source`
   normalises source line-by-line and collapses whitespace *inside string literals*,
   so a meaningful change like `"a  b"` → `"a b"` does not change the hash. Violates
   §5.6.

9. **Config is parsed twice with a silent fallback.** Rust loads and validates the
   config, but Python separately re-reads `config.toml` for coordination settings and
   falls back to defaults on any exception, so invalid config is silently ignored and
   the two languages can disagree. Violates §5.12's "fail loudly" stance.

10. **The three central files are monoliths** (`project.rs`, `session.py`,
    `graph/graph.py`), each mixing many responsibilities, which makes ownership hard
    to reason about and lets side effects spread.

---

## 7. The target architecture

Every deviation in §6.3 is a symptom of one root cause:

> Ownership of "what revision R contains" is split across the Rust lock and
> unsynchronised Python.

The refactor collapses that split into a single principle:

> **One revision = one native transaction, under one lock, over _complete_ content,
> producing _one_ id-level delta that already includes the code-layer structural
> change. Python holds only read-only projections of _published_ revisions, plus
> integrations (generators, artifact stores, bus queues, pydantic models).**

Concretely, the end state is:

- **Rust owns committed truth**: the complete content generations, the revision
  counter, the identity registry, the canonical code layer (nodes + edges +
  reverse-deps), the id-level delta, and sidecar persistence — all updated inside one
  commit, with publication as the strictly-last in-lock step.
- **The code layer becomes native.** Rust maintains the authoritative structural
  state and, on each commit, produces a **code delta**: the precise set of node and
  edge upserts/removals/moves, keyed by `DurableId`. Python's `CodeGraph` stops
  building from the read surface and becomes a **pure applier** of that code delta
  into rustworkx — no FFI during apply, no identity priming, no read-surface walk.
  rustworkx stays in Python for *algorithms*; Rust owns *structural state*.
- **The public delta is id-level.** One `CommitDelta` per write carries
  `created_ids / changed_ids / deleted_ids / moved[{id, old, new}] / affected_ids`,
  the code delta, `touched_files` (metadata only), and `rescan`. Reconciliation
  emits ids and structured moves directly; `changed` means content-hash changed.
- **Snapshots are revision-owned.** Generations are complete at commit time, so a
  snapshot captures content in O(1) and never touches disk. A snapshot's code graph
  is produced from a code delta computed over its own frozen database.
- **One post-commit path.** After a successful native commit, every write funnels
  through a single Python hook that invalidates the head snapshot, applies the code
  delta, schedules derived recompute (id-level), and publishes to the bus — so no
  write path can diverge.
- **The bus is a pure notification layer.** It receives the published id-level delta,
  delivers it scoped and in revision order, and never blocks the writer.

The companion plan (`REFINED_IMPLEMENTATION_PLAN.md`) executes this in dependency
order, gating the risky middle (moving the code layer into Rust) with a parity
technique that keeps the test suite green at every step.
