# TyO3 — Refined Architecture

> The complete technical design of the TyO3 substrate: a real-time,
> multi-agent, multi-layer code intelligence backbone.
>
> Companion to `REFINED_CONCEPT.md` (the *what* and *why*). This document is the
> *how*: components, data structures, algorithms, concurrency, persistence, and
> the public API.

---

## 0. System shape at a glance

```
┌──────────────────────────────────────────────────────────────────────────┐
│                                 TyO3 SPINE                                  │
│                                                                            │
│  Identity ───────── Revision / Content ───────── Delta + Reverse-Dep       │
│  (durable ids,      (immutable content store,    (created/changed/deleted, │
│   reconciliation)    snapshot isolation)          who-depends-on-what)     │
└───────────┬───────────────────┬────────────────────────┬──────────────────┘
            │ links by id        │ pins revision           │ notifies
            ▼                    ▼                          ▼
   ┌─────────────────┐  ┌──────────────────┐     ┌────────────────────────┐
   │ LAYERS           │  │ SNAPSHOTS        │     │ COORDINATION            │
   │ • code (L0)      │  │ consistent view  │     │ • single write lock     │
   │ • embeddings     │  │ across ALL layers│     │ • delta subscription bus│
   │ • docstrings     │  │ at one revision  │     │ • derivation DAG        │
   │ • descriptions   │  └──────────────────┘     └────────────────────────┘
   │ • authored intent│
   └────────┬─────────┘
            │ durable state
            ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ .tyo3/  sidecar:  identity registry · authored layers · derived cache   │
   └──────────────────────────────────────────────────────────────────────┘
```

The system divides cleanly into:

- **The spine** (§2–§4): identity, revisioned content, the delta. Small, owned.
- **The layers** (§5): code and all knowledge-about-code, linked by identity.
- **The sidecar** (§6): durable, committable state.
- **Coordination** (§7): the write lock, the subscription bus, the derivation DAG.
- **Persistence** (§8) and the **public API** (§9).

---

## 1. Foundations and the one hard constraint

TyO3 is a Python library with a Rust core (PyO3/maturin). Semantic analysis is
delegated to the **ty** analysis engine and **ruff** primitives; the analysis
engine memoises queries in a **salsa** database. Graph algorithms are delegated to
**rustworkx**. Vector search is delegated to an external store. The persistent
content map uses a **HAMT** (`rpds`).

One property of the analysis engine dictates the entire concurrency design:

> On any mutation, the salsa storage sets a cancellation flag for in-flight
> queries and then **blocks until it is the sole live handle** to its internal
> state. Any clone of the live database shares that state and that handle count.

Consequence: a reader holding a clone of the *live* database does not merely risk
cancellation — it **blocks the writer indefinitely**. Shared-storage reuse and
non-blocking writes cannot coexist on one database.

Therefore: **each pinned revision is its own independent database over an
immutable content view.** The writer's database is never shared with readers, so
its mutations never block; readers are never cancelled. The cost — each snapshot
warms its own memoised computation lazily — is accepted as the price of true
isolation. This is the non-negotiable shape from which everything else follows.

---

## 2. The content store and revisions

### 2.1 Immutable, revisioned content

All in-flight content (developer edits, agent buffers, ingested disk changes,
authored-layer edits) flows through a single immutable, revisioned content store.

```rust
type Generation = Arc<ContentMap>;   // O(1) to clone; structurally shared

struct ContentMap {
    system:   HashTrieMapSync<SystemPathBuf, Document>,         // disk-path docs
    virtual:  HashTrieMapSync<SystemVirtualPathBuf, Document>,  // scratch buffers
}

enum Document {
    Text { text: Arc<str>, hash: ContentHash, version: u64 },
    Deleted { version: u64 },                                   // tombstone
}

struct ContentStore {
    head:      Generation,                       // current content
    revision:  Revision,                         // monotonic application counter
    retained:  BTreeMap<Revision, Generation>,   // bounded time-travel window
}
```

Because the map is persistent (HAMT), producing the next generation shares
structure with the previous one (O(log n) insert), and **capturing a generation
for a snapshot is an O(1) `Arc` clone**. A bounded `retained` window keeps recent
revisions addressable for time-travel; a snapshot holding its own generation pins
it independently of eviction.

**Content is authoritative.** Every revision-producing event writes the resulting
content (or a `Deleted` tombstone, including directory membership) into the
generation for that revision. Disk reads are interned into the revision-owned
generation the first time they are seen, so **two snapshots pinned at the same
revision always observe identical content** — the store, not live disk, is the
system of record for what a revision contains. This is the invariant that makes
derived-layer invalidation trustworthy (§5.3, §7.3).

Every `Text` document carries a **content hash** (§3.2), computed once at intern
time. The hash is the key that links derived layers to content and drives
reconciliation; storing it on the document means it is available everywhere
content is, without recomputation.

### 2.2 The overlay system (one impl, two roles)

A single `System` implementation serves both the live head and frozen snapshots,
distinguished by one field:

```rust
struct OverlaySystem {
    content: ArcSwap<Generation>,   // lock-free reads; head republishes, frozen never does
    native:  Arc<dyn System>,       // disk source, consulted only to intern-on-first-read
    frozen:  Option<Revision>,      // None = live head; Some(R) = pinned, isolated view
}
```

- **Live head** (`frozen = None`): reads fall through to disk on a miss, and the
  result is interned into the head generation so it becomes part of the record.
- **Frozen view** (`frozen = Some(R)`): serves only the pinned generation for R.
  A miss is a genuine "absent at R," never a live-disk read, so a frozen view is
  isolated from all later change — including disk changes that bypass TyO3.

Reads are lock-free (`ArcSwap` RCU). Directory enumeration and metadata are served
from the generation so that *membership*, not just file content, is pinned at a
revision.

### 2.3 Snapshots (the isolated read surface)

```
snapshot(at: Option<Revision>) -> Snapshot
```

A snapshot:

- captures the generation for revision R (`Arc` clone, O(1)),
- builds a **fresh, independent** analysis database over the frozen overlay at R —
  its own salsa storage, so it can neither be cancelled by nor block the writer,
- exposes the consistent read API across all layers (§9),
- has explicit lifetime (context manager / `close()` / drop); holding it pins its
  generation, dropping it releases the content for GC.

`at = None` pins current head; `at = R` time-travels to any retained revision, so
an agent can diff "before/after my edit" across every layer.

---

## 3. Identity

Identity is the linchpin: every layer links through it, and preserving it across
change is the system's first responsibility.

### 3.1 Two kinds of identifier

Each entity carries two distinct identifiers, and conflating them is the classic
mistake we avoid:

| Identifier | Stable across… | Purpose |
|---|---|---|
| **Durable ID** | content edits, line moves, renames, restarts | the anchor authored layers attach to; the node identity in the graph |
| **Content hash** | …changes whenever the definition body changes | keys derived caches; drives reconciliation matching |

- The **durable ID** is minted once (a ULID/UUID) when an entity is first seen and
  persisted in the sidecar identity registry. It never changes while the entity
  exists, even as its body, name, and location change.
- The **content hash** is a hash of the entity's normalised definition text (§3.2).
  It changes with every meaningful edit. An *unchanged* body keeps the *same* hash
  across revisions — which is what lets derived artifacts (embeddings) be reused
  for free.

This separation gives the exact lifecycle the concept demands: editing a symbol
keeps its durable ID (so authored notes stay attached, flagged for review) while
changing its content hash (so its embedding is recomputed).

### 3.2 Content hashing

The content hash is computed over a **normalised** form of the entity's definition
— the AST subtree rendered canonically (whitespace/comment-insensitive where
appropriate, configurable per layer), not the raw bytes — so that cosmetic edits
do not needlessly invalidate derived artifacts. Hashing happens once, at content
intern time, and is stored on the `Document`/node.

### 3.3 The identity registry

The durable-ID ↔ entity binding lives in the sidecar:

```
identity.db   (durable_id) → {
    qualified_path:  "pkg/models.py::User.save",   # last known location
    content_hash:    <hash>,                        # last known body hash
    kind:            Method,
    first_seen_rev, last_seen_rev,
}
```

It is the persistent backbone that lets identity — and therefore every authored
annotation — survive restarts and out-of-band edits.

### 3.4 Reconciliation

Because anchors live in the sidecar, not in source, identity is **reconciled**
against the freshly-built code layer on load and after any externally-applied
change. For each entity in the new code layer, bind it to a durable ID by the
first matching rule:

1. **Exact match** — an anchor's `qualified_path` resolves to this entity →
   rebind, high confidence. (The common case: nothing moved.)
2. **Hash match** — an anchor's `content_hash` equals this entity's hash, at a
   different path → the entity *moved or was renamed* with an unchanged body →
   rebind the durable ID to the new location; **derived caches hit for free**
   (same hash). High confidence.
3. **Structural match** — same name + kind + container in a renamed/moved file, or
   nearest by signature → rebind, **low confidence**; mark dependent authored
   views `needs-review`.
4. **No match** — mint a new durable ID.

Anchors with no surviving entity are retired; their authored views are marked
`orphaned/needs-review` and never silently dropped.

Reconciliation is a spine operation, run inside the write path so that every
committed revision has a fully reconciled identity map before any layer or
subscriber observes it.

---

## 4. The delta and reverse-dependency index

Every write produces a delta — the product of the write and the input to every
reaction:

```rust
struct Delta {
    revision:   Revision,
    created:    Vec<DurableId>,   // and the files/entities involved
    changed:    Vec<DurableId>,
    deleted:    Vec<DurableId>,
    moved:      Vec<(DurableId, OldPath, NewPath)>,   // from reconciliation
    rescan:     bool,             // coarse change ⇒ delta unknown ⇒ full rebuild
}
```

The spine maintains a **reverse-dependency index** (`who imports / references /
inherits-from whom`), so that given a changed set, it can compute the *transitive
affected set* without a global rescan. This index serves two consumers:

- **Invalidation** — which derived artifacts are now stale (§5.3).
- **Coordination** — which subscribers must be notified, scoped to their interest
  (§7.2).

The change *vocabulary* (created/changed/deleted, file vs directory vs config) and
the incremental work of applying a change to the analysis database are delegated
to the analysis engine; TyO3 synthesises the precise events (it always knows which
paths it touched) and owns the durable-ID-level delta on top.

---

## 5. Layers

### 5.1 The layer model

A **layer** is a set of per-entity records keyed by durable ID, plus optional
intra-layer edges. Every layer implements a common interface:

```python
class Layer(Protocol):
    name: str
    origin: Literal["derived", "authored"]

    def value(self, snapshot, durable_id) -> Any | None: ...
    def at(self, snapshot) -> LayerView: ...          # revision-consistent view

    # derived layers only:
    def dependencies(self) -> set[LayerRef]: ...      # what this layer derives from
    def recompute(self, snapshot, durable_ids) -> None: ...

    # authored layers only:
    def edit(self, durable_id, value) -> Revision: ...
```

Layers are **linked by identity**: the code node, embedding, description, and
docstring for one entity all share its durable ID. Cross-layer reads are an
identity join, performed against a single pinned revision so the result is
consistent.

Layers are **typed by origin**, which fixes their lifecycle.

### 5.2 Code — layer 0

The most structured layer: symbols (nodes) and their relationships (containment,
references, inheritance, overrides) as a rustworkx graph, with node identity =
durable ID. It is **derived** from source via the analysis engine. Graph algorithms
(reachability, cycles, centrality, components, shortest paths) are rustworkx
built-ins; TyO3 orchestrates, it does not implement graph theory.

The code layer is updated incrementally from the delta:

```
on delta (non-rescan):
    drop nodes owned by changed/deleted entities      # rustworkx auto-removes edges
    re-extract nodes + intra/out edges for created+changed entities
    revalidate inbound cross-entity edges from the reverse-dep set
on delta (rescan):
    rebuild the layer from the snapshot
```

Heavy payloads (vectors, long text) are **never** stored on graph nodes — only
durable ID, kind, location, and content hash — so snapshot copies stay cheap.

### 5.3 Derived layers (embeddings, docstrings, descriptions, summaries)

A derived layer is a pure function of `(entity content @ R, generator_version)`.
Each record is keyed and cached by **content hash**, not by revision:

```
embedding[ content_hash ] -> vector        # in the external vector store
```

Lifecycle:

1. The delta yields changed/affected durable IDs.
2. For each, the entity's *new* content hash is compared to the cached one.
3. **Hash unchanged** → the cached artifact is still valid (reused across revisions
   and agents — the central efficiency win).
4. **Hash changed / missing** → schedule recomputation via the derivation DAG
   (§7.4); serve the previous value as `stale` or block per layer policy.

Because derived artifacts are content-addressed and immutable per hash, the vector
store needs no revision awareness: a given hash always maps to the same artifact,
so cross-revision and cross-agent consistency are automatic.

### 5.4 Authored layers (intent, curated descriptions, review notes)

An authored layer has no upstream. Records are keyed by **durable ID** and stored
durably in the sidecar. Editing an authored value is a write: it flows through the
content store, bumps the revision, and produces a delta like any other change, so
authored knowledge participates fully in snapshot isolation and time-travel.

When the entity an authored record describes changes (its content hash moves), the
record is **not** dropped — it is marked `needs-review` and surfaced, so a human or
agent can confirm or update it. When reconciliation cannot rebind the entity at
all, the record becomes `orphaned`. Silent loss of authored knowledge is treated as
a correctness failure.

### 5.5 Cross-layer consistency

A snapshot pins revision R, which pins: the content generation, the reconciled
identity map, and therefore each entity's content hash at R. From those, every
layer resolves deterministically — code from the frozen analysis DB, derived
artifacts by content hash, authored values by durable ID at R. The guarantee:
**`snapshot.code`, `snapshot.embedding(id)`, and `snapshot.authored(id)` all
describe the same revision R.**

---

## 6. The `.tyo3/` sidecar

```
.tyo3/
  config.toml            # layer definitions, generators, policies, retain caps
  identity.db            # durable_id → {qualified_path, content_hash, kind, …}
  authored/              # authored-layer records, keyed by durable_id (+ history)
    intent/
    descriptions/
  cache/
    embeddings/          # content-hash-keyed vectors (vector store data dir)
    docstrings/          # content-hash-keyed extracted text
  revisions/             # optional: retained delta log for cross-restart history
```

Properties:

- **Committable.** Living in version control, authored knowledge and identity
  anchors travel with the repo and are shared across machines and agents.
- **Non-invasive.** Collaborators who do not use TyO3 see one directory they may
  ignore; the source is never modified. (A team may `.gitignore` `cache/` while
  committing `identity.db` and `authored/`.)
- **Reconstructable boundary.** `cache/` is regenerable and disposable.
  `identity.db` and `authored/` are the durable knowledge that cannot be recomputed
  from source.

**Concurrency on the sidecar.** Writes to `identity.db` and authored records go
through the same single writer (§7.1), so the sidecar never sees torn writes from
TyO3 itself. The git-level concern (two people committing divergent
`identity.db`/authored edits) is an ordinary merge, minimised because durable IDs
are stable and content-hash keys are order-independent.

---

## 7. Concurrency and coordination

### 7.1 One writer, many readers

- **Writes** are serialized by a single lock and are cheap: persistent-map insert
  (O(log n)) + incremental analysis update (warm) + reconciliation + delta
  computation + O(1) publish. The lock spans the **entire** commit — content
  mutation, identity reconciliation, **and** all derived-layer code-graph updates —
  so there is exactly one consistent post-commit state and no layer can be observed
  mid-update. Writes are typically *partitioned* (different agents touch different
  entities), so serialization is not a throughput concern.
- **Reads** are concurrent and isolated: each snapshot owns an independent analysis
  database, so the writer's mutation never blocks on readers and readers are never
  cancelled (§1). Analysis runs with the GIL released, so many agents read in true
  parallel.

We deliberately do **not** support concurrent edits to the *same* entity with
merge/CRDT semantics (§ rejected alternatives, concept doc). Snapshot isolation
provides consistent reads, not write merge; safety comes from partitioning.

### 7.2 The delta subscription bus

Agents react to change instead of polling:

```python
sub = session.subscribe(interest=Interest(files={...}) | Interest.ALL)
for delta in sub:            # delivered after each committed revision
    affected = delta.scoped_to(sub.interest)   # via reverse-dep index
    ...                       # recompute / re-author only what matters
```

On each committed revision the bus computes, per subscriber, the intersection of
the transitive affected set with that subscriber's interest, and delivers a delta
only when it is non-empty. Subscriptions are revision-stamped so an agent can pin a
snapshot at exactly the revision it was notified about.

### 7.3 Why authoritative content matters here

Derived-layer invalidation (§5.3) keys off "what changed at revision R." Because
the content store is authoritative for every revision (§2.1) — including disk
changes ingested through the watcher — the affected set is exact: invalidation
neither **misses** (serving a stale embedding as fresh) nor **over-fires**
(recomputing unchanged entities). This precision is what makes a paid embedding
pipeline economical and agent reactions correct.

### 7.4 The derivation DAG

Derived layers declare their dependencies, forming a DAG (e.g. `code → docstrings`,
`code → embeddings`, `code → descriptions`, `descriptions → description-embeddings`).
On a delta the orchestrator:

1. computes the affected entity set (reverse-dep index),
2. walks the DAG in topological order, marking dependent artifacts stale,
3. schedules recomputation (lazy on first read, or eager per policy).

The DAG is acyclic by construction; authored layers are not auto-derivation
sources, so reactions always terminate. A derived artifact whose entity is renamed
out from under it is caught as stale by the next reconciliation + delta, not left
dangling.

---

## 8. Persistence model

**Rebuild + sidecar + cache** — no durable graph/analysis database.

| State | Strategy | Rationale |
|---|---|---|
| Code layer + analysis | **Rebuilt from source** each session | Source is truth; analysis memos must re-warm on load regardless |
| Identity anchors | **Loaded from `identity.db`, reconciled** against fresh code | Durable knowledge; cannot be recomputed |
| Authored layers | **Loaded from `authored/`**, reconciled | Durable knowledge; cannot be recomputed |
| Derived artifacts | **Served from content-hash cache**, recomputed on miss | Expensive but regenerable |
| Revision history | **Optional retained delta log** in `revisions/` | Cross-restart time-travel if desired |

Startup: build code layer from source → load identity registry → reconcile →
load/attach authored layers → lazily serve derived artifacts from cache. The only
cost relative to a fully-durable design is the rebuild, which is bounded by repo
size and recovers the *correct* state from the *only* real source of truth.

We reject a fully durable graph database: analysis memos are not practically
serialisable (so they re-warm anyway), and persisting/versioning the graph
ourselves means building and maintaining a database — effort better spent on the
spine.

---

## 9. Public API (Python)

```python
session = TyO3Session(root)              # owns the spine, layers, and sidecar

# ── identity ────────────────────────────────────────────────────────────────
session.id_for(path, line, col)          # durable id at a location
session.locate(durable_id)               # current location of an id

# ── write / mutate (each returns the new Revision) ───────────────────────────
session.edit(path, text)                 # developer/agent code buffer edit
session.edit_many({path: text, ...})     # atomic: one revision, one delta
session.edit_virtual(uri, text)          # scratch buffer, never touches disk
session.sync_path(path)                  # ingest an external disk change
session.sync_all()                       # rescan
session.author(layer, durable_id, value) # write an authored-layer record
session.watch(); session.poll_changes()  # file watcher as a change source

session.head                             # current Revision

# ── read: pinned, consistent across ALL layers, never cancelled ──────────────
with session.snapshot() as snap:         # or snapshot(at=r) to time-travel
    snap.check()                         # diagnostics
    snap.code.symbol(durable_id)         # code layer
    snap.code.neighbors(durable_id, kind=...)
    snap.embedding(durable_id)           # derived layer (content-hash resolved)
    snap.nearest(query_vector, k=10)     # ANN over the embedding layer @ R
    snap.docstring(durable_id)
    snap.authored("intent", durable_id)  # authored layer @ R
    snap.revision

# ── cross-revision diff (per layer or combined) ──────────────────────────────
before, after = session.snapshot(at=r0), session.snapshot(at=r1)
after.code.diff(before.code)             # structural change my edit caused
after.embedding_drift(before)            # which embeddings changed

# ── react ────────────────────────────────────────────────────────────────────
sub = session.subscribe(interest=Interest.files({"models.py"}))
for delta in sub:
    with session.snapshot(at=delta.revision) as snap:
        for did in delta.changed:
            regenerate_tests(snap, did)   # an agent reacting to a precise change
```

One read surface, the snapshot, serves every layer; session-level convenience
reads are sugar over an internally-held head snapshot.

---

## 10. Thinness inventory — delegate vs. own

**Delegated (we write ~none of this):**

| Concern | Delegated to |
|---|---|
| Python type analysis, semantic queries | the ty analysis engine |
| Parsing, source handling, notebooks | ruff / ruff_notebook |
| Incremental file/dir/config sync, change vocabulary | the analysis engine's change application |
| Memoisation & durability of analysis | salsa (per independent database) |
| Graph algorithms | rustworkx |
| File watching | the engine's directory watcher |
| Vector storage + nearest-neighbour search | external vector store |
| Embedding / description *generation* | external models (we orchestrate) |
| Persistent map structure | rpds (HAMT) |

**Owned (small, focused):**

- The content store + overlay/frozen system + revision plumbing.
- Identity: durable IDs, content hashing, the registry, **reconciliation**.
- The delta + reverse-dependency index.
- The layer model and its derived/authored typing + linking.
- The `.tyo3/` sidecar format and lifecycle.
- Coordination: the single write lock, the subscription bus, the derivation DAG.

---

## 11. Inherent costs and tradeoffs (stated honestly)

- **Cold snapshot computation.** Each snapshot warms its own analysis memos for the
  slice it queries. Inherent to true isolation (§1); amortised because snapshots are
  held and queried many times, and a floating "latest" read can serve one-off
  glances warm.
- **Memory per live snapshot × layers.** Each snapshot retains its own analysis
  memos; *content* is shared (Arc'd generations) and large artifacts live outside
  the graph, so the multiplier is memos, not vectors. Bounded by explicit snapshot
  lifecycle and a retained-revision cap.
- **Reconciliation cost** on load and after external edits — the price of a clean,
  unpolluted codebase (sidecar over in-source anchors). Bounded by the changed set
  in the incremental case.
- **Two-system consistency** (spine ↔ vector store) — resolved by content-hash
  keying so the vector store needs no revision awareness.
- **Rebuild on startup** — the price of not maintaining a durable graph database;
  bounded by repo size and always recovers correct state from source.

These are the genuine tradeoffs of a substrate whose defining features are many
concurrent consistent readers, self-healing derived knowledge, durable authored
knowledge, and an unpolluted codebase.

---

## 12. Implementation phases (ordered by architectural dependency)

The two **gates** come first; nothing else is trustworthy without them.

1. **Content store + overlay/frozen system.** Persistent-map content layer;
   authoritative per-revision content (intern-on-read, tombstones, directory
   membership); the `System` impl serving head and frozen roles. *Gate.*
2. **Identity: durable IDs + content hashing + registry + reconciliation.** The
   linchpin every layer links through. *Gate.*
3. **Revisions + write path + delta.** `edit`/`edit_virtual`/`sync_*` → store
   mutation + analysis apply + reconciliation + delta; reverse-dependency index.
4. **Snapshots.** Independent per-revision databases; the consistent read surface;
   session reads as sugar over a head snapshot.
5. **Code layer (L0).** Incremental graph update from the delta; rustworkx
   orchestration.
6. **Concurrency proof.** N reader snapshots + a hot writer: writer never blocks,
   readers never cancelled, results match the pinned revision; whole-commit lock
   verified (no mid-update observation).
7. **The `.tyo3/` sidecar.** Layout, identity registry persistence, authored-layer
   storage, derived cache directory.
8. **Derived layers + derivation DAG.** Content-hash-keyed caching; embedding
   layer over an external vector store; invalidation from the delta.
9. **Authored layers.** Durable records; `needs-review`/`orphaned` lifecycle on
   reconciliation.
10. **Coordination bus.** Delta subscriptions scoped by reverse-dep + interest;
    file watcher as a change source.
11. **Cross-revision diff** (per layer and combined) and the floating "latest"
    fast path.
12. **Benchmarks** (§13).

(1)–(4) deliver the spine; (5)–(6) the code layer and its isolation proof; (7)–(10)
the multi-layer, multi-agent substrate.

---

## 13. Verification strategy

- **Isolation.** A snapshot pinned at R reads R across all layers after many head
  writes (and after disk changes that bypass TyO3).
- **Liveness.** Writer throughput is unaffected by K held snapshots (proves no
  blocking).
- **No cancellation.** Snapshot reads never surface analysis cancellation.
- **Atomicity.** `edit_many` advances exactly one revision; no intermediate
  half-batch revision is observable.
- **Identity.** A symbol that moves with an unchanged body keeps its durable ID and
  hits derived caches; a rename with body change keeps the ID and flags authored
  views; an unmatched anchor orphans (never silently drops) authored knowledge.
- **Delta precision.** The affected set equals the true transitive set — no misses,
  no over-fire — including watcher-ingested disk changes.
- **Cross-layer consistency.** At any R, code, embeddings, docstrings, and authored
  values all describe R.
- **Parity.** Incremental layer state after a sequence == a full rebuild over the
  same content.
- **Reconciliation.** Round-trip through the sidecar (persist → restart → reload →
  reconcile) restores identity and authored layers exactly.
- **Coordination.** A subscriber is notified iff a committed revision affects its
  interest set, at the correct revision.
- **Performance.** Single-entity edit → sync + delta + incremental layer update ≪
  full rebuild; snapshot warm-up characterised; memory bounded across many
  revisions and layers.
```
