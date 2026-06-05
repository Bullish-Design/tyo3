# TyO3 — Refined Concept

> A real-time, multi-agent, multi-layer code intelligence substrate.
>
> This document describes *what* TyO3 is and *why* it is shaped the way it is.
> The companion `REFINED_ARCHITECTURE.md` describes *how* it is built.

---

## 1. Vision

TyO3 is the **backbone for real-time agentic code intelligence**. A developer and
any number of AI agents operate on the same codebase simultaneously — one writing
a class, another generating its tests, a third updating documentation, a fourth
maintaining semantic embeddings — and every participant sees a consistent,
queryable, *linked* picture of the code and everything known about it.

The codebase is never just text. It is a set of **layers** that describe the same
underlying entities from different angles:

- the **code** itself (symbols, types, references, call/inheritance structure),
- **natural-language descriptions** of what each entity is,
- **"what does this do"** semantic embeddings for similarity and retrieval,
- **docstrings** and extracted API contracts,
- **authored intent** — design rationale a human or agent records deliberately,

…all bound together by a single, stable notion of identity, and all kept mutually
consistent as the code changes underneath them.

TyO3's job is to be the **spine** that holds these layers in alignment in real
time: it owns *identity*, *revisions*, and the *change delta*, and it lets many
readers and coordinated writers work in parallel without stepping on each other.

---

## 2. The problem we are solving

Tools that index code for AI today are built around a request/response model:
embed the repo, answer a query, re-index periodically. This breaks down the moment
work becomes **live and collaborative**:

- **Staleness.** Embeddings and descriptions drift the instant code changes. Most
  systems re-index on a timer and serve stale data in between.
- **No identity.** When code moves or is renamed, naive indexers treat it as
  delete-then-add, losing every annotation attached to it.
- **No isolation.** When several agents read while one writes, they see torn,
  half-applied states — or the writer is blocked by the readers.
- **No coordination.** Agents poll for changes and re-derive everything, because
  nothing tells them *precisely* what changed and *who* is affected.
- **No durable knowledge.** Hand-authored insight ("this module is a compatibility
  shim, do not extend it") has nowhere to live that survives a refactor.

TyO3 solves these by making **identity, revision-consistency, and an incremental
change delta** first-class, and by treating every kind of knowledge about the code
as a layer linked through that spine.

---

## 3. Core concepts

### 3.1 The spine

The spine is the small, universal core every layer depends on. It provides three
primitives, none of which is specific to code:

1. **Stable identity.** Every entity has a durable id that survives line moves,
   renames, refactors, and restarts. Annotations attach to the id, not to a
   location.
2. **Revisions & snapshot isolation.** Every change produces a new revision. A
   reader can pin a revision and see a fully consistent view of *all* layers as of
   that revision, unaffected by concurrent writes and never interrupted by them.
3. **The change delta.** Every revision carries a precise description of what
   changed (`created` / `changed` / `deleted`) plus a reverse-dependency index
   ("who relies on this"). The delta is both the **invalidation signal** for
   derived knowledge and the **coordination bus** for agents.

### 3.2 Layers

Everything known about the code lives in a **layer**. Layers are linked to each
other by shared identity — the embedding for `User.save`, its description, its
docstring, and its code node all share one durable id.

Layers are typed by **origin**, which determines their lifecycle:

- **Derived layers** are pure functions of the code at a revision (embeddings,
  extracted docstrings, computed summaries). They are never authoritative, are
  always reproducible, and are invalidated automatically by the delta.
- **Authored layers** are written deliberately and have no upstream (design
  intent, curated descriptions, review notes). They are durable, survive code
  edits, and are *flagged for review* rather than silently dropped when the thing
  they describe changes.

Code is simply **layer 0** — the first and most structured layer, but not the
owner of the system.

### 3.3 The `.tyo3/` sidecar

All durable, non-source state lives in a committable **`.tyo3/` sidecar
directory** beside the project — never inside the source files themselves.

The sidecar holds the identity registry, authored layers, and the cache of
derived artifacts. Because it is an ordinary directory:

- it travels with the repository through version control, so authored knowledge is
  shared across machines and agents automatically;
- it is **invisible to collaborators who do not use TyO3** — a directory they can
  ignore — so the source code is never polluted with tool-specific markers that a
  teammate might strip or mangle.

The deliberate tradeoff: because identity anchors live *beside* the code rather
than *inside* it, identity must be **reconciled** against the source whenever the
code may have changed outside TyO3. That reconciliation step is the price of a
clean codebase, and it is exactly what the delta and content-hashing machinery are
built to perform.

### 3.4 Real-time, multi-agent operation

Many participants act at once. The model is:

- **Many concurrent readers**, each on its own pinned revision, fully isolated.
- **Coordinated writers** whose mutations are serialized through a single cheap
  lock and are typically *partitioned* — the human edits one file, the test agent
  another, the doc agent a third.
- **Event-driven reactions.** Agents subscribe to the delta, scoped to the
  entities they care about, and react to precise changes instead of polling and
  re-deriving the world.

---

## 4. The mental model for users

```
   Developer ─┐
   Test agent ─┤  writes (serialized, partitioned)      reads (parallel, isolated)
   Doc agent  ─┤────────────────────────────► TyO3 ◄──────────── Agent A @ rev 41
   Embed agent ┘                              spine               Agent B @ rev 39
                                                │                 Agent C @ rev 41
                                          delta │ subscriptions
                                                ▼
                              reactive agents recompute only what changed
```

A participant:

1. **Reads** by pinning a revision (`snapshot`) and querying any layer — code
   structure, a symbol's embedding neighbours, its description, its docstring —
   all consistent as of that revision.
2. **Writes** by editing code or an authored layer; the write produces a new
   revision and a delta.
3. **Reacts** by subscribing to the delta and recomputing or re-authoring only the
   affected entities.

The defining guarantee: at any revision R, **every layer agrees**. A snapshot's
code graph, its embeddings, and its authored notes all describe the same R.

---

## 5. Design principles

1. **Identity is sacred.** Everything links through stable identity; preserving it
   across change is the system's first responsibility.
2. **The source is the truth; everything else is reconstructable.** Code-derived
   state is rebuilt from source on demand; only authored knowledge and expensive
   caches are persisted, and even those are keyed so they can be regenerated.
3. **Consistency by construction.** Readers never see torn state and never block
   writers; a revision is internally consistent across all layers.
4. **Change is a first-class object.** The delta is not an afterthought — it is the
   product of every write and the input to every reaction.
5. **Don't pollute the code.** Tool state lives in the sidecar, not in source.
6. **Delegate everything we can.** We own identity, revisions, layering, and
   coordination. We do not reimplement type analysis, parsing, graph algorithms,
   file watching, or vector search.
7. **Partition over merge.** We make concurrent work safe by separating what agents
   touch, not by building conflict resolution.

---

## 6. Key decisions and the alternatives we rejected

**Sidecar over in-source annotations.**
We considered embedding identity anchors and authored notes directly in the source
(structured comments, docstring tags). In-source markers move atomically with the
code and need no reconciliation — but they pollute the codebase, can be altered or
removed by collaborators who don't use TyO3, and cannot hold binary artifacts like
embedding vectors. We chose the sidecar and accept a reconciliation step instead.

**Linked layers over one fat graph.**
We considered attaching all knowledge (vectors, long text) as payloads on a single
code-graph node. That gives the simplest mental model but makes consistent
snapshots expensive to copy and forces unrelated lifecycles onto one entity. We
keep layers separate and linked by identity, and keep large artifacts out of the
graph.

**Rebuild-plus-sidecar over a durable graph database.**
We considered persisting the entire graph and analysis state to disk for instant
startup. That effectively means building and maintaining a versioned graph
database, and the analysis state must re-warm on load regardless. We rebuild
code-derived state from source and persist only authored knowledge and a
regenerable cache.

**Partitioned writers over concurrent-edit merge.**
We considered allowing agents to edit the same entities concurrently with CRDT/OT
merge. Snapshot isolation gives consistent *reads*, not write *merge*, and merge
machinery is a large, error-prone subsystem. We partition work so agents write
different entities, and serialize writes through one cheap lock.

**Independent per-revision storage over shared, reused storage.**
We considered sharing one analysis database across readers to reuse computation.
The underlying analysis engine cancels in-flight work and blocks mutation while any
shared handle is alive, which would let a reader stall every writer. We give each
pinned revision its own independent storage so writers never block and readers are
never cancelled, accepting that each snapshot warms its own computation lazily.

---

## 7. Scope and non-goals

**In scope:** identity and reconciliation; revisions and snapshot isolation; the
change delta and subscription bus; the layer model and its derived/authored
typing; the `.tyo3/` sidecar; orchestration of derivations; the read/write API for
developers and agents.

**Explicitly delegated (not ours to build):** Python type analysis and semantic
queries; parsing and source handling; graph algorithms; file-system watching;
vector storage and nearest-neighbour search; embedding/description *generation*
(we orchestrate generators; we are not a model host).

**Non-goals:** concurrent-edit conflict resolution on the same entity; being a
general-purpose database; hosting models; serving as the source of truth for code
(the repository is).

---

## 8. Why this is the right shape

The combination of **stable identity + snapshot isolation + an incremental delta**
is rare, and it is exactly what a live, linked, multi-view system needs:

- Identity lets annotations *stick* through refactors.
- Snapshot isolation lets many agents read in parallel while the world changes.
- The delta makes derived knowledge *self-healing* and agent reactions *precise*.
- The sidecar makes authored knowledge *durable and shareable* without touching the
  code.

Together they turn a codebase from a pile of text that gets re-indexed into a
living, multi-layered model that many minds — human and artificial — can work in at
once.
