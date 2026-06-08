# TyO3 — Implementation Concept V2

> **Supersedes `REFINED_IMPLEMENTATION_CONCEPT.md` from Phase 6 onward.** V1
> remains the authoritative description of Phases 0–5 (content gate, native code
> layer, id-level delta, pure-applier cutover, native commit). It is still worth
> reading for the system framing (§1–§5 of V1), the salsa constraint (§3), and
> the normative rules. **This V2 document changes the *destination*** — it folds
> in what we learned auditing the deferred code-delta producer and probing the
> analysis engine's dependency resolution, and it re-sequences the remaining work
> around a single keystone the V1 plan had assumed already landed.
>
> Read V1 §1–§5 once for vocabulary and the hard salsa constraint. Then read this.

---

## 0. Why a V2

The V1 plan was written front-to-back as if the **native in-commit code-delta
producer** (the thing that maintains the reverse-dependency index and emits a
transitive `affected` set) already existed. It does not. It was deferred at
Phase 3, re-deferred at Phase 4, and carried at Phase 5 — each time for sound
risk reasons, but the surrounding phases kept consuming a capability that was
never built. The result was *accidental* complexity: a path→id graph-walk bridge
in the bus, an "affected is seeds-only" caveat threaded through every phase, and
a standing tension between "Rust owns reverse-deps" (the plan) and "Python's
graph walk computes affected" (the code).

V2 removes that gap by **building the producer first**, then deleting every
bridge and scaffold the deferral forced into existence. It also incorporates two
findings that sharpen — and simplify — the target:

1. **Computing `affected` is cheap.** It is a graph walk over an
   incrementally-maintained reverse-dep index, *not* re-type-checking the blast
   radius. The only per-commit analysis cost is re-analyzing the changed scope.
   The ~100× `open()` regression that motivated the deferral came from the
   *full, every-file* producer, never from the incremental one.

2. **The analysis engine does not expose inference-flow dependencies as edges.**
   A member access through an inferred type (`w = make_widget(); w.draw()`)
   produces no `render → draw` edge. So a method-level affected closure has
   holes. But coverage is preserved at **container granularity** by two facts —
   a container's `content_hash` subsumes its members' bodies, and the container
   is reachable from consumers via the *named* reference chain. This makes the
   synchronous `affected` set **container-granular, nominally-complete, and
   never-miss**, with method-level precision available as an *optional, additive,
   asynchronous* refinement layer.

Those two findings produce the central design idea of V2:

> **`affected` is sound-and-coarse synchronously, and precise-and-optional
> asynchronously.** The system is fully correct with zero async; the async layer
> only narrows. Correctness never depends on it.

---

## 1. The refined principle

V1's principle stands, with one clause added (italic):

> One revision = one native transaction, under one lock, over complete content,
> producing one id-level `CommitDelta` that already includes the code-layer
> structural change *and a transitively-complete, container-granular `affected`
> set computed by an incrementally-maintained reverse-dependency index*. Python
> holds read-only projections of published revisions, integrations (generators,
> stores, bus queues, models), *and one optional asynchronous layer that refines
> `affected` from container to method precision without ever being required for
> correctness*.

---

## 2. Vocabulary additions

| Term | Meaning |
|---|---|
| **Structural edge** | A `DurableId→DurableId` dependency edge the producer derives from a **named** reference/import/inheritance/override occurrence. The reverse-dep index is built from these. |
| **Container-granular affected** | The synchronous `affected` set: the transitive closure of `changed ∪ deleted` over the structural reverse-dep index. Never misses a *nominal* dependent; may over-fire (a whole class's referrers when one method changed). |
| **Nominal completeness** | The guarantee that any dependency whose type is reachable by a named chain to its container is covered by `affected`. Non-nominal flows (duck typing, `Any`, protocols) are the residual, explicitly-bounded miss class. |
| **Affected refinement** | A revision-stamped, after-the-fact message that *narrows* (or optionally *expands*) a published revision's `affected` set. Produced asynchronously in Python over a frozen snapshot. Delivered on the bus's refinement channel. |
| **Refinement channel** | The bus contract that permits an affected-refinement for revision R to arrive after R's primary delta (and possibly after R+1's). |
| **Precision mode** | Config knob `code_graph.precision ∈ {container, method}` selecting whether the async refinement layer runs. |

---

## 3. The hard constraints (unchanged, now first-class)

Two constraints are *anticipated*, not discovered — they shape the design and
must be treated as load-bearing facts, never as bugs to fix:

1. **Salsa cannot be shared with readers or rolled back** (V1 §3). The writer's
   database is never cloned to a reader; on a failed commit the database is left
   *benignly ahead* (it re-reads restored content and recomputes equal). This is
   why the commit uses **Strategy B+ deferred-publish**, and why true
   stage-a-next-db-and-swap (Strategy A) is impossible. B+ satisfies every
   observable invariant in §5.3; it is the clean ceiling, not a compromise to
   remove later.

2. **The analysis engine emits only named structural edges** (this document §0,
   finding 2). Inference-flow dependencies are not edges. Synchronous `affected`
   is therefore container-granular by construction; method precision is the
   async layer's job.

---

## 4. The architecture (target end-state)

```
┌───────────────────────────── RUST: committed truth ─────────────────────────────┐
│ Content store ── Identity ── AST-canonical hash ── Native CodeLayer ── Commit(B+) │
│ (immutable        (durable    (container hash       (nodes + edges +   (publish-  │
│  generations,      ids,        subsumes members)     reverse_deps,      last,     │
│  O(1) capture)     reconcile)                        maintained         rollback) │
│                                                      in-commit)                   │
│                                                          │                        │
│                          one id-level CommitDelta ◄──────┘                        │
│   created/changed/deleted/moved · affected (transitive, container-granular) ·     │
│   code_delta · touched_files · rescan                                             │
└───────────────────────────────────────┬──────────────────────────────────────────┘
              one post-commit path        │ publish-last, then enqueue to bus
   ┌──────────────────────────────────────┼───────────────────────────────────────┐
   ▼                    ▼                  ▼                      ▼                  ▼
┌────────┐      ┌──────────────┐   ┌──────────────┐     ┌─────────────────┐  ┌──────────────┐
│CodeGraph│     │  Snapshots   │   │   Derived    │     │      Bus        │  │ Async        │
│(pure    │     │ (revision-   │   │  (per-layer  │     │ (pure projection│  │ precision    │
│ applier,│     │  pinned,     │   │   key        │     │  of CommitDelta,│  │ refinement   │
│ rustworkx│    │  O(1), frozen,│  │   locality:  │     │  id-level,      │  │ (narrow      │
│ for algos)│    │  GIL-released)│  │   local vs   │     │  ordered,       │  │  container→  │
│         │     │              │   │   semantic)  │     │  non-blocking)  │  │  method over │
└────────┘      └──────────────┘   └──────────────┘     │  + refinement   │  │  snapshots)  │
                                                         │    channel ─────┼──┘             │
                                                         └─────────────────┘  optional, gated│
                                                                              graceful-degrade┘
```

### 4.1 Rust owns committed truth (unchanged from V1, plus the finished producer)

- **Content store**, **identity**, **commit transaction (B+)**: as V1, landed.
- **Native CodeLayer** `{ nodes, edges, reverse_deps }`, **maintained
  incrementally inside the commit** (the keystone, Phase 6). Structural edges
  (references/imports/inherits/overrides) come from the engine's *named*
  occurrence output. `reverse_deps` is updated edge-by-edge as the dirty scope is
  re-analyzed; `affected = affected_closure(changed ∪ deleted)` is a graph walk.
- **The id-level `CommitDelta`** now carries a genuinely transitive
  `affected` (container-granular). `code_delta` is the minimal incremental delta
  from `diff_from(prev_layer)`. The full-rescan-every-commit fallback is retired
  except for genuine `rescan` (cold start, `sync_all`).

### 4.2 Python holds projections + integrations + one async refinement

- **CodeGraph**: a pure applier of `code_delta` into rustworkx. rustworkx is used
  for *algorithms only* (reachability, cycles, centrality, diff). The legacy
  read-surface builder is **deleted** (Phase 7).
- **Snapshots**: revision-pinned, O(1) capture, frozen overlay, no disk reads,
  GIL-released parallel analysis. Each snapshot warms its own cold database — the
  accepted price of isolation.
- **Derived layers**: one affected-driven invalidation loop; per-layer key
  locality decides whether `content_hash` alone or `content_hash +
  dependency-closure fingerprint` is the cache key (§5 below).
- **Bus**: a pure projection of the `CommitDelta`, id-level, scoped,
  revision-ordered, non-blocking, plus the **refinement channel**.
- **Async precision refinement**: a background worker that narrows
  container-granular `affected` to method precision over frozen snapshots and
  publishes refinements. Gated by `precision = method`. Purely additive;
  removing it changes nothing about correctness.

---

## 5. The affected-set model (the crux of V2)

This is the part V1 got structurally wrong by deferral, and the part our analysis
re-derived. State it precisely.

### 5.1 Synchronous, in-commit (always on)

- The commit re-analyzes the **dirty scope** = changed files ∪ their one-hop
  importers (the latter so newly-resolvable references re-bind). Structural edges
  of *unchanged* files do not change when another file's body changes, so they
  are not re-derived — this is what keeps the cost proportional to the edit, not
  the project.
- `reverse_deps` is maintained edge-by-edge during this re-analysis.
- `affected_ids = affected_closure(changed_ids ∪ deleted_ids)` — a BFS over
  `reverse_deps`. Microseconds, even for a thousand-entity fan-out.
- **Guarantee:** never-miss for nominal dependencies; container-granular
  precision; may over-fire (acceptable — §5.4 constrains `changed`, not
  `affected`).

### 5.2 Why coverage holds at container granularity (the load-bearing facts)

A method-body edit to `Widget.draw`:
1. moves `draw`'s `content_hash` **and** `Widget`'s `content_hash` (the container
   hash subsumes member bodies — **a named, tested invariant**, Phase 10);
2. so `changed_ids ⊇ {draw, Widget}`;
3. `Widget` is reachable from consumers by the *named* chain
   (`render → make_widget → Widget`), so `affected_closure` reaches `render`.

The dependent is caught **through the class hash + the named chain**, not through
a (non-existent) `render → draw` edge. Both facts are guarded by regression tests
(`test_inference_flow_coverage.py`).

### 5.3 The residual miss class (explicitly bounded)

When a dependency's type is obtained with **no named chain to its container**
(duck typing, `Any`, structural protocols, dynamically-obtained types), the
container hash still moves but `reverse_deps` never reaches the consumer → a true
miss. This is the *only* miss class, it is documented, and it is addressed (if a
use case needs it) by the optional async *expansion* refinement (§5.5), accepting
eventual consistency on exactly this class.

### 5.4 Asynchronous precision refinement (optional, additive)

- A background worker, for committed revision R, opens a **frozen snapshot at R**
  (never the live head — salsa §3), resolves member-access targets via the
  engine, and **narrows** the container-granular `affected` to the entities that
  actually depend on the changed *members*. It publishes an affected-refinement
  for R on the bus's refinement channel.
- **Safe by construction:** the synchronous set is a sound superset, so narrowing
  can never introduce a miss. If the worker lags, fails, or never runs, the coarse
  set remains correct. **Graceful degradation.**
- **Optional expansion** mode additionally discovers non-nominal dependents
  (§5.3) and adds them — accepting eventual consistency on that miss class. Off
  by default.

### 5.5 Derived invalidation, unified

One mechanism resolves the long-standing tension between "content-hash keying"
(V1 §5.7) and "transitive affected" (V1 §5.4):

- Each derived layer declares a **key locality**:
  - **local** (raw-source embeddings, extracted docstrings): cache key =
    `content_hash`.
  - **semantic** (type-aware descriptions, inherited docstrings): cache key =
    `content_hash + dependency-closure fingerprint`.
- On a commit, for each id in `affected`, recompute the layer's key; if it
  changed, mark stale and schedule recompute (lazy/eager per policy).
- Local layers' keys move only when their own text changes — so
  `affected`-without-`changed` ids are correct no-ops (no over-recompute).
  Semantic layers' keys move when any dependency changed — so they recompute.
- **One loop over `affected`; the per-layer key definition decides locality.**
  No separate transitive machinery, no deep-hash-everywhere over-fire.

---

## 6. What V2 deletes (because clean beats incremental)

The V1 deferral chain and Phase-6 partial work created scaffolding that V2
removes outright:

- **The bus path→id graph-walk bridge** and its id-keyed helpers
  (`_compute_affected`, `_resolve_files`, the materialised-head-graph dependency
  in `Delta.from_commit_delta`). Once `affected` is transitive at the source the
  bus delta is a *pure projection*. (Phase 7)
- **The legacy read-surface graph builder** in `graph.py` (~1500 lines). The
  native producer is authoritative; the projection only applies deltas. (Phase 7)
- **The parity oracle as a standing fixture.** It was cutover scaffolding; it is
  demoted to a focused native-correctness suite + the inference-flow regression
  tests. (Phase 7)
- **The "affected is seeds-only" caveat** threaded through Phases 3–6 memory and
  docs. (Resolved by Phase 6.)
- **The dual config read** in Python. (Phase 12)

---

## 7. Load-bearing invariants (guard these forever)

1. **Publication is the strictly-last in-lock step** (V1 §5.3). No torn commit.
2. **A container's `content_hash` subsumes its members' bodies** — the coverage
   mechanism for inference-flow dependencies. *Tested* (`test_inference_flow_coverage`).
3. **Every nominal dependency's container is reachable by a named reference
   chain** — what carries container-granular coverage. *Tested.*
4. **The synchronous `affected` set is a sound superset** of the true
   (method-precise) affected set — what makes async narrowing safe.
5. **The salsa database is never shared with readers and never rolled back**
   (V1 §3) — what makes Strategy B+ correct and non-blocking.
6. **`reverse_deps` is maintained incrementally inside the commit and is correct
   after every revision** (V1 §5.4).

---

## 8. Phase map (V2, remaining work)

Phases 0–5 are landed (V1). V2 re-sequences the rest so the keystone lands first
and every downstream phase consumes a capability that actually exists.

| Phase | Title | Keystone outcome |
|---|---|---|
| **6** | The scoped native producer | `affected_ids` transitive at source; container-granular; never-miss |
| **7** | Pure-projection bus; delete scaffolding | Bus = pure projection; legacy builder & bridge deleted; refinement-channel seam added |
| **8** | Unified derived invalidation | One affected-driven loop; per-layer key locality |
| **9** | Async precision refinement | Container→method narrowing over snapshots; `precision` knob; graceful degradation |
| **10** | AST-canonical hashing | Meaning-not-formatting; container-subsumes-members made explicit |
| **11** | Read surface & convenience lifetime | No views over closed snapshots; honest floating-latest |
| **12** | Single config source | Rust-authoritative; precision + coordination knobs surfaced |
| **13** | Split the monoliths | `project.rs` / `session.py` / `graph.py` along clean seams |
| **14** | Warnings/typing/hygiene + acceptance | `-D warnings` clean; end-to-end acceptance test |

Each remaining phase has a detailed `PHASE_<n>_IMPLEMENTATION_GUIDE.md`. The
guides assume V1 §1–§5 vocabulary, the salsa constraint, and this V2 §5 affected
model.

---

## 9. Definition of done (V2)

Everything in V1's definition of done, plus:

- `affected_ids` is genuinely transitive at the source (container-granular,
  never-miss for nominal dependencies); proven by the producer's reverse-dep
  closure tests, not by a Python graph walk.
- The bus delta is a pure projection of the `CommitDelta`; no path→id
  reconstruction, no materialised-head-graph dependency, no Option-B helpers.
- The legacy read-surface builder is deleted; the native producer is the sole
  source of graph structure.
- Derived invalidation runs one affected-driven loop with declared per-layer key
  locality; local layers do not over-recompute on `affected`-without-`changed`.
- The async precision layer (when `precision = method`) narrows correctly and
  degrades gracefully; with `precision = container` it does not run and the
  system is fully correct.
- The container-subsumes-members invariant and the named-chain coverage are
  guarded by regression tests.
