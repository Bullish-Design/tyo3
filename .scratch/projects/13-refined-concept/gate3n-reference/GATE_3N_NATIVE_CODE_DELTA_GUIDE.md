# Gate 3N — Native Code-Layer Delta Production: Implementation Guide

> Implements the **in-transaction** half of **REFINED_SPEC.md §3** (§3.3.1 /
> §3.3.3 — every layer, including the code layer, is updated under the single
> write lock with no observable half-applied state) and re-homes **§6** (the code
> layer) so that its delta is *produced* in Rust as part of the commit. It closes
> the Gate 3 §3.3.1 gap left open by the current design, in which the Python
> `CodeGraph` is mutated **outside** the native write lock by
> `TyO3Session._apply_graph_delta`.
>
> This is a **hardening / re-homing** gate, not a new feature. The observable
> behaviour of the code layer MUST NOT change — `incremental == full rebuild`
> parity (§6.3.1) is preserved end-to-end. What changes is *where* the work runs:
> from a post-commit, lock-free, read-surface-driven Python pass to an in-lock,
> Rust-native delta producer whose output Python applies purely.
>
> **Prerequisites:** `gate4-complete` must exist. Two pieces of in-flight Gate-1/3
> remediation MUST be landed (committed) first, because this gate builds on a
> trustworthy frozen read surface and a serialized writer:
>   1. the frozen `walk_directory` isolation fix (refactor guide Step 9 — currently
>      uncommitted in `rust/src/overlay.rs`); and
>   2. the interim session-level write lock (option 1 — a `threading.RLock` around
>      `native edit*` + `_apply_graph_delta` in `session.py`), so the codebase is
>      correct *before* you start moving the boundary. This gate **retires** that
>      lock at Step 6; do not skip landing it first — it is the safety net that
>      keeps `main` correct mid-migration.
>
> **Audience:** an engineer new to the codebase. Same working rules as Gates 1–4:
> `devenv shell -- <script>` for everything (never bare `cargo`/`pytest`); one
> labelled commit per validated step (`gate3n: step N — …`); never skip a
> validation; rebuild the native module with `devenv shell -- build` before any
> Python test sees a Rust change.

## The one hard premise (read first, do not violate)

**rustworkx stays in Python.** The `CodeGraph` query/algorithm surface (cycles,
reachability, topological order, transitive deps, centrality/`hub_symbols`, `diff`,
paths) is rustworkx — the delegation REFINED_ARCHITECTURE §10 and CONCEPT design
principle #6 ("we do not reimplement graph algorithms") mandate. **You are NOT
porting the graph to Rust.** You are moving the *delta production* — symbol/node
materialisation and edge resolution for the dirty set — into the native commit, and
reducing the Python side to a **pure applier** of that delta plus the unchanged
rustworkx query methods.

If at any point a step tempts you to reimplement a graph *algorithm* in Rust
(`is_reachable`, `import_cycles`, …), stop — that is out of scope and is the wrong
direction. The boundary is: **Rust owns structural state + delta; Python owns the
rustworkx replica + queries.**

## The contract you are building toward

After this gate:

- The native commit (`commit_head`, under the `inner` `Mutex`) computes, as its
  last in-lock step, a complete **`CodeDelta`** for the dirty set — materialised
  nodes (by `DurableId`) and typed edges (containment, references, imports,
  inheritance, overrides) — and returns it inside `SyncResult` (§3.3.1, §3.3.3).
- Rust holds the **authoritative structural code layer** in `HeadState`
  (`CodeLayer`: small per-node payloads + edges + reverse-dependency index, §6.2.2,
  §6.2.3, §4.3.4). This is the "derived structural state" SPEC §3 step 5 names.
- The Python `CodeGraph` becomes a **replica**: it applies a `CodeDelta` to its
  rustworkx structure with **no calls back into the session read surface**
  (`document_symbols`, `file_occurrences`, `class_supertypes`, `check`). Its public
  API is byte-for-byte unchanged.
- Application is **ordered by revision** (a cheap revision-gate), so two partitioned
  writer threads can never reorder or tear the replica — **without** a Python write
  lock. The interim lock from the prerequisites is **deleted**.
- The `_prime_identity_registry` call in `TyO3Session.snapshot` and the per-node
  `session.id_for` FFI are **deleted** — the data they worked around is now produced
  by the commit.

The defining success property: **the suite (incl. `test_incremental_parity.py` and
`test_inheritance_ordering.py`) stays green at every step, and at the end the HEAD
graph is updated inside the native lock, ordered by revision, with the Python read
surface no longer consulted to build it.**

## Why this is shaped the way it is

The current design (Gate 3 Step 8) puts the HEAD-graph update in Python, after the
native commit returns. That re-reads the world through a freshly pinned snapshot
(slow, O(nodes) FFI), needs the identity registry primed before the snapshot is
captured (the `snapshot` workaround), and — most importantly — runs **outside** the
serialization boundary that ordered the content/identity commit, so concurrent
writers can apply graph deltas out of revision order (§3.3.1 violation). All three
problems share one root: *the code-layer delta is produced in the wrong place.*
Producing it in the commit, where identity reconciliation already runs over the same
entities, fixes all three at once and makes the code layer a true peer of content
and identity under the single writer.

## Target module layout

```
rust/src/
  code_layer.rs   # NEW: CodeLayer (authoritative structural graph) + CodeDelta producer
  dto/
    code_delta.rs # NEW: SymbolNodeDto, EdgeDto, CodeDelta (serde) — the wire contract
  entity.rs       # extend: emit full node payload (range) + nesting/containment
  convert/
    occurrences.rs# reuse: reference/import target resolution (already native)
  project.rs       # wire CodeLayer into HeadState; produce CodeDelta in commit_head;
                   # emit it on SyncResult; snapshot code-layer state
src/tyo3/
  graph/graph.py   # add apply_code_delta (pure); retire read-surface build/apply_delta
  models/analysis.py # SyncResult gains an optional code_delta field (Pydantic mirror)
  session.py        # cut over edit*/sync* to apply_code_delta; delete lock + priming
```

Keep every new Rust type's `// INVARIANT:` / `// CONCURRENCY:` comments accurate, as
in Gates 1–4.

---

## Step 0 — Characterise, lock the wire contract, build the parity oracle

**Goal.** Before moving any logic, (a) pin the `CodeDelta` wire format both sides
will speak, (b) add a Python *pure applier* that consumes it, and (c) stand up a
parity oracle that compares "graph built the OLD way (read surface)" against "graph
built by applying a `CodeDelta`". No behaviour changes yet. This mirrors Gate 3
Step 0: make the target observable before you build it.

**Files.** `rust/src/dto/code_delta.rs` (new), `src/tyo3/models/analysis.py`,
`src/tyo3/graph/graph.py`, `src/tyo3/graph/tests/test_code_delta_apply.py` (new).

**Build.**
- Define the wire contract in Rust (serde; serialised into `SyncResult`). Mirror the
  existing `SymbolNode`/`EdgeData`/`EdgeKind` Python models exactly so the replica is
  a 1:1 application:
```rust
// dto/code_delta.rs
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SymbolNodeDto {
    pub durable_id: String,       // ULID, or "<module>{file}" / "<external>{pkg::name}"
    pub kind: String,             // SymbolKind serialised (lowercase variant)
    pub qualified_name: String,
    pub file: String,             // project-relative key, matches content-store key
    pub range: RangeDto,          // start/end line+col (display/location only)
    pub content_hash: Option<String>, // hex of u128; None for synthetic nodes
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EdgeDto {
    pub src_id: String,
    pub dst_id: String,
    pub kind: String,             // "containment"|"references"|"imports"|"inherits"|"overrides"
    pub role: Option<String>,     // ReferenceRole for reference edges; None otherwise
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct CodeDelta {
    pub revision: u64,
    pub rescan: bool,             // true ⇒ this is a full snapshot, replace the replica
    pub nodes_upserted: Vec<SymbolNodeDto>,
    pub nodes_removed:  Vec<String>,                 // durable_ids
    pub nodes_moved:    Vec<(String, String, RangeDto)>, // (id, new_file, new_range)
    pub edges_added:    Vec<EdgeDto>,
    pub edges_removed:  Vec<EdgeDto>,
}
```
- Add `code_delta: Option<CodeDelta>` to `SyncResultDto` (`#[serde(default)]`, so old
  callers and tests are unaffected). Mirror it on the Python `SyncResult` model as
  `code_delta: CodeDelta | None = None`.
- Add **`CodeGraph.apply_code_delta(self, delta: CodeDelta) -> None`**: a pure
  function of `(self, delta)` that mutates the rustworkx structure and the
  `_id_to_index` / `_file_to_nodes` / `_file_importers` indices from the payload
  ONLY. It MUST NOT touch `session`/`snapshot`. Semantics:
  - `rescan == True` → clear the graph and apply as a full build.
  - `nodes_removed` → remove nodes (rustworkx auto-removes incident edges); drop
    their index + reverse-dep entries.
  - `nodes_upserted` → add or update node payloads (stable index via `_id_to_index`).
  - `nodes_moved` → update `file`/`range` payload **only**; never re-add, never churn
    edges (§5.5.1 / §6.6 moved handling).
  - `edges_removed` then `edges_added` → update edges and the `_file_importers`
    reverse-dep index.
  - set `self.revision = delta.revision`.
- This is the function every later step feeds; write it once, correctly.

**Validate.**
- Unit test (`test_code_delta_apply.py`): hand-construct a small `CodeDelta`
  (two nodes, one containment + one reference edge), apply it to an empty
  `CodeGraph`, and assert `node_count`/`edge_count`/`references_to` match a
  hand-asserted expectation.
- Unit test: applying a `nodes_moved` entry changes `symbol(id).file` but leaves
  `references_from(id)` identical (no edge churn).
- The existing suite is untouched and green (nothing is wired to *emit* a delta yet).
- **Acceptance gate:** the applier round-trips a hand-built delta; suite green.
  Commit (`gate3n: step 0 — CodeDelta wire contract + pure Python applier`).

> The parity oracle pattern used from Step 1 on: in tests, build graph **G_old** via
> the current read-surface path and **G_new** by applying the emitted `CodeDelta`,
> then `assert_graphs_equal(G_old, G_new)` using the strengthened comparator from
> `test_incremental_parity.py`. Keep the old path alive as the oracle until Step 6.

---

## Step 1 — Native `CodeLayer` + node materialisation (full build)

**Goal.** Introduce the authoritative structural code layer in Rust and have the
commit emit a full-build `CodeDelta` containing every **node** (no edges yet).
Python applies it; parity is asserted on the node set and payloads.

**Files.** `rust/src/code_layer.rs` (new; `mod code_layer;` in `lib.rs`),
`rust/src/entity.rs`, `rust/src/project.rs`.

**Build.**
- `code_layer.rs` — the authoritative small-payload structural state (§6.2.2):
```rust
pub struct NodeData {
    pub durable_id: DurableId,
    pub kind: SymbolKind,
    pub qualified_name: String,
    pub file: String,
    pub range: TextRange,
    pub content_hash: Option<ContentHash>,
}
// CONCURRENCY: only ever mutated by the writer under the inner Mutex (HeadState).
pub struct CodeLayer {
    nodes: HashMap<DurableId, NodeData>,
    // edges + reverse_deps added in Steps 2–4:
    // edges: HashSet<Edge>, reverse_deps: HashMap<DurableId, HashSet<DurableId>>,
    revision: Revision,
}
```
- Extend entity extraction (`entity.rs`) to carry the node payload fields the DTO
  needs. `Entity` already has `qualified_path`, `kind`, `content_hash`, `container`,
  `name`; add the `range` (location) so a `NodeData`/`SymbolNodeDto` can be built
  without a second pass. Reuse `extract_entities_for(state, files)` for the dirty set
  and `extract_entities(state)` for a full build / rescan.
- Synthetic nodes: the producer MUST emit **module** nodes (`"<module>{file}"`, kind
  `module`, `content_hash = None`) and **external stub** nodes
  (`"<external>{pkg}::{name}"`) the same way the Python builder does today
  (`graph/identity.py::make_module_durable_id`, the `<external>` prefix). Step 1 emits
  module nodes (one per file) and entity nodes; external stubs arrive with edges in
  Step 3.
- Node keying: key each entity node by its **reconciled `DurableId`** from the
  registry (the commit just reconciled it — read it from `head.registry`, do not call
  `id_for`). Each entity already has a unique ULID, so the Python compound-key scheme
  (`durable_id::qualified_name` in `identity.py`) is unnecessary — key directly by the
  ULID. (See escalation note on nested-entity keys.)
- In `commit_head`, after reconciliation, build/update the `CodeLayer` and produce a
  `CodeDelta`:
  - For Step 1, the simplest correct producer is: re-extract nodes for the affected
    file set, diff against the prior `CodeLayer.nodes` (added/changed → `upserted`,
    `moved` ids from the reconciliation's `moved` set → `nodes_moved`, retired ids →
    `nodes_removed`), update the `CodeLayer`, and attach the `CodeDelta` to the DTO.
  - Full build / `rescan`: emit every node with `rescan = true`.
- The producer runs **inside the existing lock** (it is part of `commit_head`); no new
  lock, no GIL juggling.

**Validate.**
- Parity (nodes only): for a multi-file fixture, `G_old` (read-surface build) and
  `G_new` (apply emitted `CodeDelta`) have the **same node `DurableId` set** and the
  same per-node `(kind, qualified_name, file, content_hash)`. (Edges will differ —
  ignore edges this step by comparing node payloads only.)
- Determinism: same content built twice → identical `nodes_upserted` ordering and ids
  (sort the emission with a total key: `(file, qualified_name, kind)`).
- Cosmetic edit: a blank-line edit above a method emits that node in `nodes_moved` or
  unchanged-`upserted`, never a remove+add, and its `content_hash` is unchanged
  (ties §5.5.1 / §6.2.2).
- **Acceptance gate:** node parity + determinism green. Commit
  (`gate3n: step 1 — native CodeLayer + node materialisation`).

---

## Step 2 — Containment edges (native)

**Goal.** Produce module→class→method containment edges in the native delta from
symbol nesting; retire the Python containment derivation. (§6.2.3 containment.)

**Files.** `rust/src/code_layer.rs`, `rust/src/entity.rs`.

**Build.**
- During extraction you already know each entity's `container` (enclosing qualified
  name) and its file. Emit a `containment` `EdgeDto` from the container node (module
  node for top-level entities; the enclosing class/function node otherwise) to each
  child. Maintain these in `CodeLayer.edges`.
- Mirror the exact parent resolution the Python builder uses today
  (`graph.py::_materialize_file_nodes` containment block, ~line 397): top-level
  entity → module node; nested → innermost enclosing entity node.

**Validate.**
- Parity now includes containment edges: `G_old` vs `G_new` equal on the
  `(src_id, dst_id, "containment")` edge set for the fixture.
- `CodeGraph.children(module_id)` / `parent(method_id)` over `G_new` match `G_old`.
- **Acceptance gate:** containment parity green. Commit
  (`gate3n: step 2 — native containment edges`).

---

## Step 3 — Reference/import edges + reverse-dependency index (native)

**Goal.** Resolve cross-entity reference and import edges natively and maintain the
reverse-dependency index in `CodeLayer` (§6.2.3, §4.3.4). This is the highest-value
move — it deletes the per-file `file_occurrences` FFI and the empty-stream class of
bugs the refactor guide fought.

**Files.** `rust/src/code_layer.rs`, `rust/src/convert/occurrences.rs`,
`rust/src/project.rs`.

**Build.**
- The occurrence/target resolver is **already native** (`convert/occurrences.rs`,
  using `definitions_for_name`/`definitions_for_attribute` with alias resolution —
  this is what the refactor guide Step 1 fixed). Call it directly from the producer
  for each file in the affected set, instead of round-tripping through Python.
- For each resolved occurrence, add a `references`/`imports` `EdgeDto`:
  - `role` carries the `ReferenceRole` (matches the Python `EdgeData.role`).
  - Project-local target → edge to the target's `DurableId` node. The target node
    exists because all nodes for the affected set were materialised in Step 1 and
    non-dirty nodes were never removed (the §6.3.2 phased-pass guarantee — references
    resolve only after node materialisation).
  - Unresolved/external target → create the `<external>{pkg}::{name}` stub node
    (emit it in `nodes_upserted`) and point the edge at it. Never key by line.
- Maintain `CodeLayer.reverse_deps: DurableId -> {DurableId}` (the inverse of
  reference/import edges). Provide `importers_of(ids) -> set` for Step 5's inbound
  revalidation and the delta closure (§4.3.3). When an edge is removed, remove its
  reverse entry (no stale reverse edges).
- Emit `edges_removed` for reference/import edges that no longer hold (a call deleted),
  so the replica drops them.

**Validate.**
- Parity on reference + import edges incl. `role`: `G_old` vs `G_new` equal for the
  two-file `from models import User; User().save()` fixture (the canonical refactor
  fixture) and for a `src/`-layout real repo fixture (e.g. `fixtures/demo_repos/...`).
- `importers_of({b.foo})` (native) returns the calling entity; removing the call and
  re-committing empties the reverse entry.
- No `file_occurrences` call remains in `graph.py`'s build/apply path (grep).
- **Acceptance gate:** reference/import parity + reverse-dep green. Commit
  (`gate3n: step 3 — native references, imports, reverse-dep index`).

---

## Step 4 — Two-pass inheritance (native, §6.4)

**Goal.** Resolve `INHERITS` then `OVERRIDES` natively, two-pass, for the whole
affected set — INHERITS for every class **before** any OVERRIDES — so multi-level
cross-file chains are order-independent (§6.4). This is the gate's silent-hazard step.

**Files.** `rust/src/code_layer.rs`, `rust/src/project.rs`.

**Build — port `graph.py::_inherits_pass_I` / `_overrides_pass_II` verbatim in logic:**
```
# Pass I — for ALL classes in the affected set, before any OVERRIDES:
for class in affected_classes:
    for supertype in class_supertypes(class):   # native; direct bases
        add INHERITS edge class_id -> supertype_id   # external base -> stub node
# Pass II — only after Pass I has completed for the whole set:
for class in affected_classes:
    ancestor_methods = {}
    for ancestor in bfs over the NOW-COMPLETE INHERITS edges in CodeLayer:
        for m in methods(ancestor): ancestor_methods.setdefault(m.name, m.id)
    for child_method in methods(class):
        if child_method.name in ancestor_methods:
            add OVERRIDES edge child_method.id -> ancestor_methods[name]
```
- The `class_supertypes` resolution is already native (PyO3 method
  `class_supertypes`, `project.rs:2342`); call its underlying engine query directly
  from the producer.
- **Pass II MUST NOT run for any class until Pass I has run for the entire affected
  set** — exactly the §6.4 rule. BFS walks `CodeLayer` INHERITS edges (built in Pass
  I), not `class_supertypes` directly (which returns only direct bases).
- Incremental subtlety: when a class is dirty but an ancestor is not, the ancestor's
  INHERITS edges already exist in `CodeLayer` from a prior commit — Pass I only
  (re)adds edges for the affected set, and the BFS still reaches non-dirty ancestors
  through the persisted edges. Preserve this; it is why `CodeLayer` is authoritative
  state, not recomputed from scratch each commit.

**Validate.**
- `test_inheritance_ordering.py` (three-file `a→b→c`, both processing orders, full +
  incremental) passes against `G_new`. Run it parameterised exactly as today.
- Parity: diamond (A→B, A→C, B→D, C→D) and external-base cases equal `G_old`.
- **Acceptance gate:** §6.4 test green in all orders against native delta; parity
  green. Commit (`gate3n: step 4 — native two-pass inheritance (§6.4)`).

---

## Step 5 — Incremental delta production + inbound revalidation (under the lock)

**Goal.** Wire Steps 1–4 into a single incremental producer inside `commit_head`
that, given the dirty set, drops changed/deleted nodes, re-extracts created/changed,
resolves their edges, and **revalidates inbound cross-entity edges** from the
reverse-dep index — emitting one coherent `CodeDelta` per revision, all under the
`inner` lock (§3.3.1, §6.3, §6.3.2).

**Files.** `rust/src/code_layer.rs`, `rust/src/project.rs`.

**Build — port `graph.py::apply_delta` (line 1922) + `rebuild`/inbound revalidation
(~1812, ~1883) into the native producer:**
```
fn produce_code_delta(head, dirty_changed, dirty_deleted, created, moved, rescan):
    if rescan: return full_build_delta(head)            # Steps 1–4 over all files
    dirty = changed ∪ deleted (by DurableId)
    1. remove CodeLayer nodes for `dirty` (drops incident edges) → nodes_removed,
       edges_removed; drop their reverse-dep entries.
    2. files = files_for(created ∪ changed)
       run the SHARED passes over `files`:
         materialise nodes (Step 1) → containment (Step 2)
         references/imports (Step 3) → INHERITS pass I → OVERRIDES pass II (Step 4)
       → nodes_upserted, edges_added, reverse-dep updates.
    3. for importer in reverse_deps.importers_of(dirty):
         re-resolve importer's outbound edges that point into `dirty`
         (drop dangling, re-point resolved) → edges_added/edges_removed.
    4. apply `moved` as location-only node updates → nodes_moved (no edge churn).
    set CodeLayer.revision = head.store.revision()
    return the assembled CodeDelta
```
- The producer is the natural home for SPEC §3 step 5 ("update other derived layers'
  structural state"). It runs after identity reconciliation (§3 step 3) and before
  the DTO is built — i.e. fully inside the lock that ordered the content mutation.
- Determinism: assemble `nodes_*`/`edges_*` in a total order before emitting.

**Validate.**
- Full parity suite against native delta (lift every scenario from
  `test_incremental_parity.py`): single-file change; create; delete; cross-file ref
  added/removed; cross-file inheritance; the §6.4 chain edit; moved entity; `rescan`;
  the randomised sequence (fixed seed). Each: a graph driven by native `CodeDelta`s
  equals a fresh full build. **Do not weaken the comparator.**
- Delete a file: its nodes/edges gone; importers' dangling edges revalidated.
- **Acceptance gate:** every parity scenario equal under the strengthened comparator;
  `devenv shell -- test-property` green. Commit
  (`gate3n: step 5 — native incremental producer + inbound revalidation (§6.3.1)`).

---

## Step 6 — Cut over the HEAD graph; order by revision; retire the lock & priming

**Goal.** Switch `session.edit*/sync*` to apply the native `CodeDelta` from
`SyncResult` via `apply_code_delta`, enforce revision-ordered application, and
**delete** the interim Python write lock and the `snapshot` priming workaround
(§3.3.1, §3.3.3).

**Files.** `src/tyo3/session.py`, `src/tyo3/graph/graph.py`.

**Build.**
- `_apply_graph_delta(result)` becomes:
```python
def _apply_graph_delta(self, result: SyncResult) -> None:
    if self._head_graph is None or result.code_delta is None:
        return
    g = self._head_graph
    # Revision gate: deltas MUST apply in order. The native commit is serialized,
    # so revisions are monotonic; a gap means a missed delta → rebuild defensively.
    if g.revision is not None and result.code_delta.revision != g.revision + 1:
        self._head_graph = None          # force lazy rebuild on next access
        return
    g.apply_code_delta(result.code_delta)
```
- **Remove the interim `threading.RLock`** added in the prerequisites: ordering is now
  guaranteed by (a) the native commit being serialized (monotonic revisions) and (b)
  the revision gate above. Concurrent partitioned writers either apply in order or
  trip the gate and rebuild — never tear the replica. Document this in the method.
- **Delete `_prime_identity_registry` and its call in `TyO3Session.snapshot`**: the
  registry is reconciled by the commit, and the snapshot's code-layer state is now
  produced natively (Step 7). Snapshots no longer require a pre-capture priming pass.
- `CodeGraph.build(session)` for the **cold** path: request a full `CodeDelta` for the
  current head from the native side (a `rescan`-style full build) and apply it, rather
  than walking the read surface. Add a native `code_delta_full()` accessor (produces a
  full `CodeDelta` from current head/`CodeLayer`) for this. (If you prefer a smaller
  Step 6, keep the old read-surface `build` for cold start and cut it in Step 7 — but
  prefer one path now.)

**Validate.**
- After any `edit`/`edit_many`/`sync_*` returns revision R, `session.graph.revision
  == R` immediately, and the graph equals a rebuild at R (no window where content is R
  but graph is R−1) — the §3.3.3 check, now structurally true.
- Concurrency test: two threads each do partitioned `edit` + read `session.graph`;
  the graph is never observed behind its paired content and ends at the latest
  revision (lift the Gate 3 Step 8 concurrency test; it should pass **without** a
  Python write lock).
- `grep` shows no `threading.RLock`/`_prime_identity_registry` in `session.py`.
- Full suite green.
- **Acceptance gate:** in-order, in-lock-equivalent updates with no Python lock; suite
  green. Commit (`gate3n: step 6 — cutover, revision-gated apply, retire lock+priming`).

---

## Step 7 — Snapshot code-layer from native; retire the read-surface builder

**Goal.** A `Snapshot` exposes its code graph by applying a native `CodeDelta`
produced over the snapshot's frozen DB at its revision, so cross-layer consistency
(§10.2.2) holds without the Python read-surface builder. Delete the now-dead Python
extraction/edge code.

**Files.** `rust/src/project.rs` (snapshot code-layer), `src/tyo3/graph/graph.py`,
`src/tyo3/session.py`.

**Build.**
- Snapshot path: keep the **copy-on-pin** fast path (`head_graph._pin_at(R)` when the
  head graph is already at R — `session.py:1063-1071`). Otherwise, build the snapshot
  graph by asking the native side for a full `CodeDelta` computed over the snapshot's
  **own frozen `ProjectDatabase`** at R (the snapshot already owns an independent DB —
  Gate 1 §2.3), and apply it to a fresh replica. No live-head reads; no priming.
- Delete the read-surface graph code in `graph.py` that is now unused: the
  `_collect_symbols_for_file`/`_materialize_file_nodes`/
  `_resolve_references_via_occurrences`/`_inherits_pass_I`/`_overrides_pass_II`/
  `rebuild`/old `apply_delta`/old `build` internals (≈1,200–1,500 lines). Keep all
  query/algorithm methods and `apply_code_delta`. Update `_native_impl.pyi`.
- Keep the `_frozen` guard on a pinned graph (§10.2.2): mutators assert `not _frozen`.

**Validate.**
- `snapshot.graph()` at R, then many head edits → pinned graph unchanged; mutating a
  pinned graph raises; `snapshot.graph().revision == snapshot.revision`.
- Cross-layer seam: for a node id in `snapshot.graph()`, `session.locate(id)` at R
  agrees with the node location (id-join consistent, §10.2.2).
- `grep` confirms `graph.py` build/apply path no longer calls `session.*`/`snapshot.*`
  read methods.
- **Acceptance gate:** snapshot graph native-built; dead code removed; suite green.
  Commit (`gate3n: step 7 — native snapshot code-layer; retire read-surface builder`).

---

## Step 8 — Determinism, cleanup, gate re-validation

**Goal.** Prove determinism (§13.2), finish the logging-facade/typed-error discipline,
and re-validate the gate tags on a fully green suite.

**Files.** `rust/src/code_layer.rs`, tests, README doc list.

**Build / audit.**
- Determinism test (§13.2): same content + same sidecar → byte-identical `CodeDelta`
  (serialise and compare) across two runs; and identical final graph across machines
  (CI). The producer must emit all `nodes_*`/`edges_*` in a total order.
- Error model (§13.1): any producer failure surfaces as a typed error, never a panic
  escaping the commit; no `eprintln!`/`println!` in `code_layer.rs` (use `log::`).
- Remove the `code_delta: Option<...>` "optional" hedge if every commit now emits one
  (keep `Option` only if cold/no-graph commits legitimately omit it — document which).
- Update `.scratch/projects/13-refined-concept/README.md` document list to reference
  this guide.

**Validate.**
- `devenv shell -- tests` and `devenv shell -- test-property` fully green; paste the
  summary line into the commit (the cardinal rule from the refactor guide).
- **Acceptance gate:** determinism proven; suite green; README updated. Commit
  (`gate3n: step 8 — determinism, cleanup, gate re-validation`).

---

## Gate 3N — Final acceptance (must all pass)

Run `devenv shell -- tests` **and** `devenv shell -- test-property` with the
dedicated modules proving:

1. **In-lock code-layer update (§3.3.1/§3.3.3).** The HEAD graph is updated from a
   `CodeDelta` produced inside the native commit; a reader observing R sees the graph
   at R, never R−1. No Python write lock is required or present.
2. **Revision-ordered application.** Concurrent partitioned writers never reorder or
   tear the replica; an out-of-order/missing delta triggers a clean rebuild, not
   corruption.
3. **Parity preserved (§6.3.1).** Every `test_incremental_parity.py` scenario —
   including the randomised sequence — equals a full rebuild, under the strengthened
   comparator, with the graph driven by native deltas.
4. **Two-pass inheritance (§6.4).** The three-file cross-file chain passes in both
   processing orders, full build and incremental, against the native producer.
5. **Workarounds retired.** `_prime_identity_registry` and the per-node `id_for` FFI
   are gone; `graph.py`'s build/apply path makes no `session.*`/`snapshot.*` read
   calls.
6. **rustworkx + public API intact.** The `CodeGraph` query/algorithm surface is
   unchanged; no graph algorithm was reimplemented in Rust.
7. **Determinism (§13.2).** Same content → byte-identical `CodeDelta` and identical
   final graph across runs/machines.

When all pass on a clean `devenv shell -- tests`, tag the commit `gate3n-complete`
and record the passing summary in the tag annotation.

## Carrying forward (MUST read before Gate 8)

- **The subscription bus (Gate 8) consumes this `CodeDelta`.** Gate 8 delivers
  precise, revision-stamped change notifications scoped by the reverse-dep index
  (§12). That index now lives authoritatively in `CodeLayer` (Step 3) and the delta is
  produced in-lock (Step 5) — exactly the inputs the bus needs. Gate 8 MUST consume
  the existing `CodeDelta` + reverse-dep, not recompute them. Do this gate **before**
  Gate 8 so the bus plugs into a finished contract.
- **Derived layers (Gate 5) key off the same affected set.** The `CodeDelta`'s
  `nodes_*` carry `content_hash`; Gate 5's in-lock invalidation hook (its §3 step 5
  insertion) reads the same affected ids and hashes the producer already computed —
  reuse them, do not re-extract.

## Sequencing & escalation notes

- **Land the prerequisites first.** The frozen `walk_directory` fix and the interim
  session lock must be committed before Step 0, so `main` is correct throughout the
  migration and the lock can be cleanly *retired* (not "never added") at Step 6.
- **Keep the old path as the oracle until Step 6.** Steps 1–5 emit a `CodeDelta`
  *alongside* the still-live read-surface build, and parity tests compare them. Only
  Step 6 makes the native delta authoritative. This is what lets you migrate without a
  red suite.
- **Never weaken the parity comparator (§6.3.1).** A divergence is a real bug in the
  native producer — almost always a missing reverse-dep update (Step 3), an inbound
  revalidation miss (Step 5), or a Pass-II-before-Pass-I ordering slip (Step 4). Fix
  the producer, not the test.
- **Nested-entity keys.** Today `graph/identity.py` builds compound keys
  (`durable_id::qualified_name`) for nested entities. Each entity already has its own
  reconciled ULID, so the native producer SHOULD key directly by ULID and drop the
  compound scheme. If any existing test asserts the compound string form, that test's
  *meaning* changed deliberately — update it and call it out (as Gate 4 did for
  refactors). Escalate only if a construct genuinely lacks a stable per-entity ULID.
- **Where `CodeLayer` state lives across restart.** `CodeLayer` is rebuilt from source
  on open (REFINED_ARCH §8: code layer is rebuilt, not persisted). On open, run a full
  build to populate it; do not attempt to persist it to the sidecar.
- **If producing edges for the dirty set needs a non-dirty node that was evicted from
  `CodeLayer`** (shouldn't happen — non-dirty nodes are never removed), that indicates
  a removal-scope bug in Step 5; fix the scope, do not re-add via a read-surface
  fallback.

## Appendix — current code the producer re-homes (file/line index)

| Logic moving into the native producer | Current Python location | Step |
|---|---|---|
| Node payload + module synthetic nodes | `graph.py::_materialize_file_nodes` (~287–397), `graph/identity.py` | 1 |
| Containment edges | `graph.py::_materialize_file_nodes` containment block (~397) | 2 |
| Reference/import resolution | `graph.py::_resolve_references_via_occurrences` (527), `_add_import_edge` (~650) | 3 |
| Reverse-dependency index | `graph.py::_file_importers` (118), `importers_of` (~770) | 3 |
| Two-pass inheritance | `graph.py::_inherits_pass_I` (977), `_overrides_pass_II` (1047) | 4 |
| Incremental orchestration + inbound revalidation | `graph.py::apply_delta` (1922), `rebuild` (1812), inbound revalidation (~1883) | 5 |
| Already native (reuse, do not re-port) | `convert/occurrences.rs`, `class_supertypes` (`project.rs:2342`), `entity.rs` extraction | 3,4 |
| Workarounds to delete | `session.py::_prime_identity_registry` (+ call in `snapshot`, ~734), interim `RLock` | 6 |
| Read-surface build to delete | `graph.py::build`/`_collect_symbols_for_file` (158/276) | 7 |
