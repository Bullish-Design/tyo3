# Gate 3 — Code Layer (DurableId-keyed, incremental): Implementation Guide

> Implements **REFINED_SPEC.md §6** (code-layer structure, incremental update,
> phased passes, two-pass inheritance) and the code-layer half of **§10**
> (cross-layer consistency via pinned graph snapshots). It is the first *layer*
> built on the two foundational gates.
>
> **Prerequisites:** `gate1-complete` and `gate2-complete` tags must exist. The
> code layer keys its nodes on the `DurableId`s Gate 2 produces and updates from the
> `Delta` Gate 1's commit transaction carries. Do not start this gate until both
> are tagged.
>
> **Audience:** an engineer new to the codebase. Same rules as Gates 1–2:
> `devenv shell -- <script>` for everything; one labelled commit per validated step
> (`gate3: step N — ...`); never skip a validation.

## The contract you are building toward (read first)

The code layer is a graph of code entities (nodes) and their relationships (edges),
where:

- **node identity is the `DurableId`** (SPEC §6.2.1) — so a node survives moves and
  renames and links every other layer to it;
- **payloads are small** (§6.2.2) — id, kind, location, content hash, structural
  fields only; no vectors or large text;
- **edges are engine-derived** (§6.2.3) — containment, references, inheritance,
  overrides, imports come from the analysis engine, never from heuristics;
- **incremental update equals a full rebuild** (§6.3.1) — the parity invariant that
  defines correctness;
- **dirty batches are processed in phases** (§6.3.2) and **inheritance is two-pass**
  (§6.4) — the two ordering rules that make multi-entity and multi-level cases
  correct regardless of processing order.

The two hardest, highest-risk requirements — and the reason this guide exists — are
**§6.4 (two-pass inheritance)** and **§6.3.1 (incremental == rebuild parity)**. Both
get their own dedicated steps and test suites.

## Where the code layer lives

The code layer is the rustworkx graph in `src/tyo3/graph/` (primarily
`graph.py`), consuming:

- the read surface of a `Snapshot` (Gate 1) for symbols/references/supertypes;
- `DurableId`s and the `Delta` (Gate 2 / commit transaction) for identity and the
  changed set.

Today the graph keys nodes on a `symbol_id` string. This gate **re-keys nodes onto
`DurableId`** and restructures inheritance into two passes. Expect to touch:
node construction, the build passes, `_index_files`, `_resolve_inheritance`,
`apply_delta`, the reverse-dependency index, and `Snapshot.graph()`.

## Target module layout

```
src/tyo3/graph/
  graph.py        # CodeGraph: nodes (DurableId-keyed), build passes, apply_delta
  identity.py     # node-id derivation now sources DurableId (replaces local scheme)
  inheritance.py  # OPTIONAL: extract the two-pass inheritance resolver for clarity
  tests/
    test_inheritance_ordering.py   # Step 0 + Step 4
    test_incremental_parity.py     # Step 7
```

---

## Step 0 — Reproduce (or refute) the inheritance ordering hazard FIRST

**Goal.** Before changing anything, write the test that proves whether single-pass
inheritance is a *live* bug or a *latent* one. This decides nothing about whether we
do the two-pass fix (SPEC §6.4 mandates it regardless), but it tells the team what
risk they are closing and gives a regression test that must stay green forever.

**Files.** `src/tyo3/graph/tests/test_inheritance_ordering.py` (new).

**Build the fixture.** Three files, a three-level chain, the override defined on the
*top* of the chain, all three dirty in one batch:
```python
# c.py
class C:
    def greet(self): ...        # defines greet on the top ancestor
# b.py
from c import C
class B(C): ...                 # intermediate, defines nothing
# a.py
from b import B
class A(B):
    def greet(self): ...        # OVERRIDES C.greet, two levels up
```

**Write the test.**
- Build the graph over the three files (full build), then assert the
  `A.greet --OVERRIDES--> C.greet` edge exists.
- Then exercise the *incremental* path: build with the files, edit all three in one
  batch (`edit_many`), apply the delta, and assert the same edge exists.
- Parameterize the order in which the dirty files are processed (force `a` before
  `b` before `c`, and the reverse) — the test must pass in **both** orders. With the
  current single-pass resolver this is where it should fail (override missed when
  the intermediate ancestor's `INHERITS` edge doesn't exist yet).

**Validate.**
- Run `devenv shell -- tests -k inheritance_ordering`.
- Record the outcome in the commit message: **LIVE** (fails now — confirms the bug)
  or **LATENT** (passes now — e.g. because the engine returns transitive supertypes).
  Either way the test stays as a permanent regression guard.
- **Acceptance gate:** the test exists and runs; its current pass/fail status is
  recorded. Commit (`gate3: step 0 — characterize §6.4, status=LIVE|LATENT`).

> Do not "fix" anything in Step 0. Its only job is to make the hazard observable and
> permanently guarded. Step 4 implements the two-pass resolver that makes this test
> pass in all orders by construction.

---

## Step 1 — Node model keyed by `DurableId`

**Goal.** Re-key graph nodes onto the `DurableId` from Gate 2 and constrain payloads
to small structural fields (SPEC §6.2.1, §6.2.2).

**Files.** `src/tyo3/graph/graph.py`, `src/tyo3/graph/identity.py`.

**Build.**
- Node payload (`SymbolNode`) carries exactly: `durable_id`, `kind`, `qualified_name`
  (display only), `file`, `range` (location), `content_hash`. **Remove** any field
  holding large text or vectors; those belong to other layers keyed by the same id.
- Replace the local node-id derivation with `DurableId` lookup: where a node was
  keyed by `file::qualified_name` (or any `name@line` fallback), it is now keyed by
  the `DurableId` the session resolves for that entity (`session.id_for(...)` or the
  id carried in the symbol DTO once the Rust side attaches it).
- Keep the ownership index (`_file_to_nodes`) and the id index
  (`_id_to_index`) but make them map through `DurableId`.

> **Source of the id:** prefer attaching the `DurableId` to the symbol DTO the Rust
> layer already returns (extend the DTO in `extract_entities`/`document_symbols`),
> so the Python layer never has to re-derive identity. Fall back to
> `session.id_for(path, line, col)` only if extending the DTO is out of scope here —
> and if so, file a follow-up, because per-node `id_for` calls are O(nodes) FFI.

**Validate.**
- Test: build over a fixture; every node's key is a valid `DurableId` (a ULID
  string), never a `file::name` or `name@line` string.
- Test: a blank-line edit above a symbol leaves that node's key unchanged across an
  incremental update (the end-to-end §6.2.1 / Gate-2 §5.5.1 payoff at the graph
  level).
- Test: no node payload contains text longer than a small bound or any vector field
  (assert the payload schema).
- **Acceptance gate:** all pass. Commit.

---

## Step 2 — Symbol collection, node materialisation, structural edges

**Goal.** The first three phased passes that every build and every incremental
update share, ordered so cross-references can resolve later (SPEC §6.3.2).

**Files.** `src/tyo3/graph/graph.py`.

**Build.** Implement (or align) these as discrete, separately-callable passes over a
given set of files (the whole project for a build, the dirty set for an update):
1. **Collect** — for each file, fetch its symbols from the snapshot; build a
   `symbols_by_file` map. Resolve each symbol's `DurableId` here (Step 1).
2. **Materialise nodes** — for *every* file in the set, add all symbol nodes. This
   pass MUST complete for the entire set before any edge pass runs, so that
   references between two files in the set can resolve (§6.3.2).
3. **Structural edges + range cache** — add containment edges (module → class →
   method) and build the per-file range cache used for enclosing-symbol lookups.

- Keep each pass a pure function of `(snapshot, file_set)` plus the graph, with no
  hidden ordering dependence beyond the documented pass order.

**Validate.**
- Test: over a two-file fixture where `a.py` references a symbol defined in `b.py`,
  after passes 1–3 both files' nodes exist (reference resolution itself is Step 3).
- Test: containment edges form the expected module/class/method tree; the range
  cache returns the innermost enclosing node for a position.
- Test: passes are order-independent for node existence — materialising `b` before
  or after `a` yields the same node set.
- **Acceptance gate:** all pass. Commit.

---

## Step 3 — Reference resolution and the reverse-dependency index

**Goal.** Add cross-entity reference/import edges (out-edges) and maintain the
reverse-dependency index that the delta closure and incremental revalidation depend
on (SPEC §6.2.3, §4.3.4).

**Files.** `src/tyo3/graph/graph.py`.

**Build.**
- **Reference pass** — for each file in the set, resolve its outbound references via
  the snapshot's occurrence/definition queries; add edges to the target nodes. Target
  nodes in non-dirty files already exist (they were never removed), so references out
  of the dirty set resolve. Edges MUST be keyed by `DurableId` on both ends.
- **Reverse-dependency index** — maintain `importers_of[target_id] -> {source_id}`
  (the reverse of dependency edges, by `DurableId`). Update it whenever a reference
  edge is added or removed. Provide `importers_of(ids: set) -> set` returning the
  union of sources that reference any id in the set — this is the input to inbound
  revalidation (Step 6) and to the delta's transitive closure (§4.3.3).
- External targets (symbols outside the project) resolve to stub nodes keyed by a
  stable external id (package::name), never by line.

**Validate.**
- Test: `a.py` calls `b.foo` → an edge `a-caller --refs--> b.foo` exists; the
  reverse index has `b.foo` importer-of `a.py`'s caller.
- Test: removing the call and re-resolving removes both the edge and the reverse
  index entry (no stale reverse edges).
- Test: `importers_of({b.foo})` returns the calling entity.
- **Acceptance gate:** all pass. Commit.

---

## Step 4 — Two-pass inheritance (the §6.4 deliverable)

**Goal.** Resolve `INHERITS` and `OVERRIDES` correctly for multi-level, cross-file
chains regardless of processing order, by **adding all `INHERITS` edges for the set
before computing any `OVERRIDES` edge** (SPEC §6.4). This makes Step 0's test pass
in all orders by construction.

**Files.** `src/tyo3/graph/graph.py` (or extract to `inheritance.py`).

**Build — replace the combined per-entity loop with two distinct passes** over the
file set:

```
# Pass I — INHERITS (for ALL classes in the set, before any OVERRIDES):
for file in set:
    for class_symbol in classes(file):
        for supertype in snapshot.class_supertypes(class_symbol):  # engine, direct bases
            add INHERITS edge: class_id --INHERITS--> supertype_id   # stub if external

# Pass II — OVERRIDES (only after Pass I has completed for the whole set):
for file in set:
    for class_symbol in classes(file):
        ancestor_methods = {}            # name -> ancestor method DurableId
        # walk the NOW-COMPLETE INHERITS chain in the graph (BFS), collecting methods
        for ancestor in bfs_inherits_chain(class_id):
            for m in methods(ancestor):
                ancestor_methods.setdefault(m.name, m.id)
        for child_method in methods(class_symbol):
            if child_method.name in ancestor_methods:
                add OVERRIDES edge: child_method.id --OVERRIDES--> ancestor_methods[name]
```

Critical requirements:
- **Pass II MUST NOT run for any class until Pass I has added INHERITS edges for the
  entire set.** This is the whole point: when computing A's overrides, B→C must
  already exist so the BFS reaches C. A combined per-class loop violates this.
- The BFS walks the graph's `INHERITS` edges (not `class_supertypes` directly),
  because `class_supertypes` returns only direct bases; transitivity comes from the
  edges built in Pass I.
- If `class_supertypes` is later found to return *transitive* ancestry, the two-pass
  split is still required and harmless (Pass I simply adds more direct edges); do not
  collapse it.
- Alternative permitted by §6.4.3: compute ancestry directly from the snapshot
  recursively instead of from the in-progress graph — but only if it does not
  reintroduce an order dependence. The two-pass graph walk above is the recommended
  form.

**Validate.**
- **Step 0's `test_inheritance_ordering` now passes in BOTH processing orders** (full
  build and incremental). This is the gate.
- Test: a diamond (A→B, A→C, B→D, C→D) resolves overrides from D without duplication.
- Test: external supertype (class extends a stdlib/third-party base) produces a stub
  INHERITS target and does not crash override resolution.
- Test: a method that does *not* exist on any ancestor gets no OVERRIDES edge.
- **Acceptance gate:** all pass, including Step 0's test green in all orders. Commit
  (`gate3: step 4 — two-pass inheritance, §6.4 closed`).

---

## Step 5 — Full build orchestration

**Goal.** A clean full build that runs the phased passes over the whole project in
the mandated order (SPEC §6.3.2), establishing the reference result for parity
(Step 7).

**Files.** `src/tyo3/graph/graph.py` (`CodeGraph.build`).

**Build.** `CodeGraph.build(snapshot)` runs, over the full project file set, in
order: collect (1) → materialise nodes (2) → structural edges + range cache (3) →
references + reverse-dep (Step 3) → inheritance Pass I then Pass II (Step 4). Record
`graph.revision = snapshot.revision`.

- The full build and the incremental update MUST share the *same* pass
  implementations (Steps 2–4), differing only in the file set they run over (whole
  project vs dirty set). This shared-implementation rule is what makes the parity
  test (Step 7) meaningful.

**Validate.**
- Test: `build` over a multi-file fixture yields the expected node count, containment
  tree, reference edges, and inheritance/override edges.
- Test: `graph.revision == snapshot.revision`.
- **Acceptance gate:** all pass. Commit.

---

## Step 6 — Incremental update from the `Delta`

**Goal.** `apply_delta` updates only the affected subgraph and yields a result
identical to a full rebuild (SPEC §6.3, §6.3.1).

**Files.** `src/tyo3/graph/graph.py` (`apply_delta`).

**Build.**
```
def apply_delta(self, snapshot, delta):
    if delta.rescan:
        self._replace_with(CodeGraph.build(snapshot)); return
    dirty = set(delta.changed) | set(delta.deleted)        # by DurableId
    # 1. Drop nodes owned by changed/deleted entities (rustworkx removes incident edges)
    self._remove_nodes(self._nodes_for(dirty))
    #    and drop their reverse-dep entries.
    # 2. Re-index created+changed via the SHARED phased passes over the dirty file set:
    files = self._files_for(set(delta.created) | set(delta.changed))
    self._collect(snapshot, files)
    self._materialise_nodes(files)
    self._structural_edges(files)
    self._references(snapshot, files)        # rebuilds reverse-dep for these
    self._inherits_pass_I(snapshot, files)
    self._overrides_pass_II(files)
    # 3. Revalidate INBOUND cross-entity edges into the dirty set:
    for importer in self._importers_of(dirty):
        self._reresolve_out_edges(snapshot, importer, into=dirty)
    self.revision = snapshot.revision
```
- `moved` entities (same `DurableId`, new location) MUST update the node's location
  payload **without** changing its id or churning its edges unnecessarily — handle
  `delta.moved` as a location update, not a drop+re-add (preserves §5.5.1 at the
  graph level and avoids needless edge churn).
- Inheritance for the dirty set MUST use the two-pass form (Step 4) — Pass I across
  the whole dirty set before Pass II.

**Validate.**
- Test: edit one file; only its nodes (and genuinely-affected inbound edges) change;
  unrelated nodes keep their identity and edges.
- Test: `moved` entity keeps its node id and out-edges; only location updates.
- Test: delete a file; its nodes and incident edges are gone; importers' dangling
  edges are revalidated (removed or re-pointed).
- Step 0's incremental assertion remains green.
- **Acceptance gate:** all pass. Commit. (Parity vs full rebuild is Step 7.)

---

## Step 7 — Parity: incremental == full rebuild (the correctness gate)

**Goal.** Prove that applying a sequence of deltas yields a graph identical to a full
rebuild over the same final content (SPEC §6.3.1). This is the invariant that makes
incremental update trustworthy; treat it as the central deliverable alongside §6.4.

**Files.** `src/tyo3/graph/tests/test_incremental_parity.py` (new).

**Build a graph-equality comparator.** Two `CodeGraph`s are equal iff:
- the same set of node `DurableId`s exists;
- per node, the same `(kind, qualified_name, file, content_hash)` (location/range
  MAY differ only if both sides agree — normalise);
- the same set of edges as `(src_id, dst_id, edge_kind, role/extra)` tuples — compare
  as **sets**, not sequences; sort with a total key, never `repr`.

**Build the parity suite.** For each scenario, run two graphs to the same final
revision — one via `build`, one via a sequence of `apply_delta` — and assert equal:
- single-file content change;
- file creation; file deletion;
- cross-file reference added, then removed;
- cross-file inheritance added; multi-level chain edit (the §6.4 case);
- a `moved` entity (rename/move with unchanged body);
- a `rescan` delta (must equal a fresh build);
- a randomized sequence: apply N random edits, compare to a rebuild at the end
  (property-style; fixed seed for reproducibility).

**Validate.**
- All parity scenarios pass.
- Run under the property harness too: `devenv shell -- test-property` (if the random
  sequence is expressed there) stays green.
- **Acceptance gate:** every parity scenario equal. Commit
  (`gate3: step 7 — incremental==rebuild parity, §6.3.1`).

> If any scenario diverges, the bug is almost always a missing pass-sharing (Step 5
> rule), a stale reverse-dep entry (Step 3), or an inbound-revalidation miss (Step 6
> phase 3). Do not weaken the comparator to make a test pass.

---

## Step 8 — Wire the code-layer update into the commit transaction

**Goal.** The HEAD code-layer update happens **inside** the single write transaction,
so no reader observes content at R with a graph at R−1, and concurrent writers cannot
apply graph deltas out of revision order (SPEC §3.3.1, §3.3.3).

**Files.** `rust/src/project.rs` (commit transaction) and the Python session glue
that owns the HEAD graph.

**Build.**
- The HEAD graph update (`apply_delta`) MUST be performed as part of the committed
  write, under the same serialization boundary that ordered the content mutation —
  not in a separate, lock-free step after the write returns.
- Concretely: the write path computes the `Delta`, and the HEAD graph's `apply_delta`
  is invoked before the revision is considered observable, in revision order. If the
  graph lives in Python while the lock is native, the session MUST hold a
  session-level write lock spanning *both* the native `edit*`/`sync*` call **and** the
  subsequent `apply_delta`, so two concurrent writers cannot interleave or reorder
  their graph updates. A native-only lock that does not cover the graph mutation is
  insufficient and MUST NOT be relied on.
- Publication ordering (§3.3.3): a snapshot opened at the returned revision MUST see
  a HEAD graph already updated to that revision.

**Validate.**
- Test: after any write returns revision R, `session.graph` reflects R immediately
  (no window where content is R but graph is R−1).
- Concurrency test: two threads each do `edit` + observe; assert the HEAD graph is
  never observed at a revision older than the content it is paired with, and ends at
  the latest revision. (Partitioned writers; no same-node concurrent edits.)
- Test: a forced error during `apply_delta` leaves both content and graph at R−1
  (no torn commit).
- **Acceptance gate:** all pass. Commit.

---

## Step 9 — Pinned graph snapshots (`Snapshot.graph()`) for cross-layer consistency

**Goal.** A snapshot exposes an immutable code graph at exactly its revision, so a
reader's `snapshot.check()` and `snapshot.graph()` describe the same R, and other
layers can join against it (SPEC §10.2.2).

**Files.** `src/tyo3/graph/graph.py`, session glue.

**Build.**
- `Snapshot.graph()` returns an immutable code graph pinned at the snapshot's
  revision. Recommended: **copy-on-pin** — if the HEAD graph is already at the
  snapshot's revision, produce a frozen copy (`PyDiGraph.copy()` plus deep-copied
  index structures, `_frozen = True` guard on mutators) and cache it on the snapshot;
  otherwise build the graph from the snapshot directly.
- A pinned graph MUST be fully independent: mutating the HEAD graph afterward MUST NOT
  affect it. Every mutation method MUST assert `not _frozen`.
- The pinned graph's node ids are the same `DurableId`s, so embeddings/docstrings/
  authored layers (later phases) join to it by id at the same revision (§10.2.2).

**Validate.**
- Test: `snapshot.graph()` at R, then many HEAD edits; the pinned graph is unchanged.
- Test: attempting to mutate a pinned graph raises (frozen guard).
- Test: `snapshot.graph().revision == snapshot.revision`; nodes present match the
  entities at R (consistent with `snapshot` reads).
- Test (cross-layer seam): for a node id in `snapshot.graph()`, `session.locate(id)`
  resolved at R agrees with the node's location — proving the id-join is consistent.
- **Acceptance gate:** all pass. Commit.

---

## Gate 3 — Final acceptance (must all pass before further layer work)

Run `devenv shell -- tests` (plus `devenv shell -- test-property` for the randomized
parity) with dedicated modules proving:

1. **DurableId-keyed nodes (§6.2.1).** Every node keys on a `DurableId`; no
   location- or name-derived keys anywhere.
2. **Small payloads (§6.2.2).** No vectors/large text on nodes.
3. **Phased passes (§6.3.2).** Node existence is order-independent; references resolve
   only after all dirty nodes exist.
4. **Two-pass inheritance (§6.4).** Step 0's `A→B→C` test passes in both processing
   orders, full build and incremental; diamond and external-base cases correct.
5. **Incremental == rebuild (§6.3.1).** Every parity scenario equal, including the
   randomized sequence.
6. **Moved entities.** Keep node id; location-only update; no edge churn.
7. **In-transaction update (§3.3.1/§3.3.3).** Graph is never observed behind the
   content it is paired with; concurrent partitioned writers never reorder graph
   updates; forced mid-commit error rolls back cleanly.
8. **Pinned snapshots (§10.2.2).** `snapshot.graph()` is immutable, at the snapshot's
   revision, and id-joins consistently with `snapshot` reads and `session.locate`.

When all eight pass on a clean `devenv shell -- tests`, tag the commit
`gate3-complete`. The spine (Gates 1–2) and its first layer (the code layer) are now
in place; derived layers (embeddings, docstrings), authored layers, the derivation
DAG, and the subscription bus build on this.

## Sequencing & escalation notes

- **Step 0 before Step 4, always.** The reproduction test is what tells you the fix
  worked and guards against regression; writing the two-pass resolver without it
  leaves you unable to prove §6.4 is closed.
- **Never weaken the parity comparator (Step 7).** A failing parity test is a real
  divergence; fix the update, not the test. Common causes are listed under Step 7.
- **If the HEAD graph stays in Python**, the Step 8 session-level lock is mandatory;
  do not assume the native write lock covers the Python graph mutation. If graph
  updates are later moved into Rust under the native lock, the session-level lock can
  be retired — record that decision.
- **If `class_supertypes` semantics are unclear** (direct vs transitive), keep the
  two-pass form regardless (§6.4) and note the engine's actual behaviour in a code
  comment for future maintainers.
- **Performance:** if `apply_delta` opening a fresh snapshot per edit is hot, that is
  a known optimisation target (cache the per-revision snapshot, or build a
  lighter-weight read surface) — raise it as a follow-up; it does not block this gate.


