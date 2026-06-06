# TyO3 — Refined Design

A real-time, multi-agent, multi-layer code intelligence substrate. A developer and
any number of AI agents operate on the same codebase at once — writing code,
generating tests, maintaining docs and embeddings — over one consistent, linked,
revision-versioned model.

This directory is the clean-slate design for the library as a whole. It is written
as the design we are building toward; it references rejected *alternatives* and
their rationale, not prior states.

## Documents (read in order)

1. **[REFINED_CONCEPT.md](./REFINED_CONCEPT.md)** — *what and why*.
   The vision, the problems it solves, the core concepts (spine, layers, identity,
   the `.tyo3/` sidecar), design principles, and the alternatives we rejected.
   Start here.

2. **[REFINED_ARCHITECTURE.md](./REFINED_ARCHITECTURE.md)** — *how*.
   The system shape: the content store, snapshot isolation, identity and
   reconciliation, the delta, the layer model, the sidecar, concurrency and
   coordination, persistence, the public API, the thinness inventory, costs, the
   phased build order, and the verification strategy.

3. **[REFINED_SPEC.md](./REFINED_SPEC.md)** — *must-hold requirements + tests*.
   Normative specifications (RFC-2119 MUST/SHOULD, `[INV]` invariants, `[GATE]`
   markers) for every high-risk component — both the foundations we preserve and
   the new pieces — with failure modes and acceptance tests. The contract
   implementation is held to.

## Implementation guides (per-gate, with per-step validations)

Each `GATE_*_GUIDE.md` walks an engineer through one gate, step by step, with a
validation that must pass before moving on. `CONFIG_SCHEMA.md` is the normative
config schema; `GATES_1-3_REFACTORING_GUIDE.md` is the remediation pass over the
first three gates.

- **GATE_1_CONTENT_STORE_GUIDE.md** — authoritative per-revision content store (§1).
- **GATE_2_IDENTITY_GUIDE.md** — durable ids, content hashing, reconciliation (§5, §7).
- **GATE_3_CODE_LAYER_GUIDE.md** — DurableId-keyed incremental code layer (§6).
- **GATE_3N_NATIVE_CODE_DELTA_GUIDE.md** — re-homes the code-layer delta into the
  native commit so the graph updates in-lock and ordered (§3.3.1/§3.3.3); retires the
  Python write lock and the snapshot identity-priming workaround. Do before Gate 8.
- **GATE_4_CONFIG_SIDECAR_GUIDE.md** — `.tyo3/` sidecar, config, store interface (§11).
- **GATE_5–8** — derived layers, authored layers, read surface, subscription bus.

## The one-paragraph model

TyO3 is a **spine** — stable identity, revisioned snapshot-isolated content, and an
incremental change **delta** — over which multiple **layers** (code, embeddings,
descriptions, docstrings, authored intent) are linked by identity and kept
consistent as code changes. Code is layer 0, not the owner. Durable, non-source
state (the identity registry and authored knowledge) plus a regenerable derived
cache live in a committable **`.tyo3/` sidecar**, never in the source. Many readers
work in parallel on pinned revisions; coordinated, partitioned writers commit
through one cheap transaction and broadcast precise deltas to subscribers.

## Foundational gates (build these first)

Two components are gates — nothing above them is trustworthy until they hold:

- **Authoritative per-revision content** (SPEC §1): any two reads of revision R —
  by any snapshot, any time — observe identical content and directory membership.
- **Identity + reconciliation** (SPEC §5): minted `DurableId`s that survive edits,
  moves, renames, and restarts, distinct from the `ContentHash` that keys derived
  caches; reconciliation never silently drops authored knowledge.

The phased build order is in ARCHITECTURE §12; the per-component contracts and
their tests are in SPEC §1–§14.

## Confirmed design decisions

Settled and not to be re-litigated without cause:

- **Sidecar over in-source annotations.** Durable state lives in `.tyo3/`; the
  source is never polluted. Accepts a reconciliation step as the cost.
- **Linked layers over one fat graph.** Layers are separate, linked by identity;
  large artifacts (vectors, long text) stay out of the graph.
- **Rebuild-plus-sidecar over a durable graph database.** Code-derived state is
  rebuilt from source; only authored knowledge and a regenerable cache persist.
- **Partitioned writers over concurrent-edit merge.** One cheap write transaction
  serialises all writers; no CRDT/OT. Safety comes from partitioning.
- **Independent per-revision storage over shared, reused storage.** Each snapshot
  is its own analysis database, so writers never block and readers are never
  cancelled. Cold per-snapshot warmup is the accepted price.
- **Minted DurableId, not content-derived id.** Identity must survive content
  edits; the content hash is the secondary key for cache reuse and reconciliation
  matching, not the identity itself.

Confirmed policy commitments (defaults, overridable per layer):

- **Authored records are never silently dropped** — ambiguous re-binds are
  `needs-review`; lost entities are `orphaned` (SPEC §5.5.3).
- **Stale derived artifacts are served tagged `stale` by default**, recomputed
  asynchronously (SPEC §8.2.5).
- **The subscription bus may coalesce** multiple revisions into one notification
  only if it delivers the union of their affected sets, in revision order (SPEC
  §12.2.2).

## Scope boundary

We own: identity + reconciliation, revisions + snapshot isolation, the delta +
reverse-dependency index, the layer model, the `.tyo3/` sidecar, and coordination
(write transaction, subscription bus, derivation DAG).

We delegate: Python type analysis and semantic queries, parsing, graph algorithms,
file watching, vector storage and nearest-neighbour search, and embedding/
description *generation* (we orchestrate generators; we do not host models).
Full breakdown in ARCHITECTURE §10.
