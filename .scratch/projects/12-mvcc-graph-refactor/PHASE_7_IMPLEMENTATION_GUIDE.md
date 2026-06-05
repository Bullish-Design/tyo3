# Phase 7 Implementation Guide — Graph: pinned snapshots (`Snapshot.graph()` + `graph.diff`)

> Audience: an engineer who has finished **Phase 1**
> (`PHASE_1_IMPLEMENTATION_GUIDE.md`, `ContentStore` + `OverlaySystem`), **Phase 2**
> (`PHASE_2_IMPLEMENTATION_GUIDE.md`, the HEAD db over the overlay via real
> discovery), **Phase 3** (`PHASE_3_IMPLEMENTATION_GUIDE.md`, the write path →
> `SyncResult`), **Phase 4** (`PHASE_4_IMPLEMENTATION_GUIDE.md`, independent MVCC
> snapshots), **Phase 5** (`PHASE_5_IMPLEMENTATION_GUIDE.md`, the concurrency
> proof), and **Phase 6** (`PHASE_6_IMPLEMENTATION_GUIDE.md`, the HEAD graph as an
> incremental `apply_delta` layer), and is now implementing **Phase 7** of
> `MVCC_SUBSTRATE_ARCHITECTURE.md`.
>
> Goal of Phase 7: give a `Snapshot` a **pinned, immutable graph** that describes
> *the same revision R its db reads* (architecture §5.2, §6). Today a `Snapshot`
> offers a consistent semantic surface (`snap.check()`, `snap.document_symbols(...)`
> — Phase 4) but **no graph**; the only graph is the live, mutable HEAD graph
> (Phase 6). Phase 7 closes the pair: `snap.graph()` returns a `CodeGraph` frozen at
> `snap.revision`, so `snap.check()` and `snap.graph()` describe one revision. With
> two pinned graphs in hand, `after.graph().diff(before.graph())` answers the
> first-class agent question **"what did my edit change?"** — meaningful precisely
> because Phase 6 kept node identity content-addressed (`identity.py`).
>
> Three things land together because they are one capability:
>
> 1. **`Snapshot.graph()`** — a lazily-materialised, cached, immutable `CodeGraph`
>    pinned at the snapshot's revision, via **copy-on-pin** of the HEAD graph when it
>    is revision-matched (the C-level fast path, architecture §5.2 "Recommended"),
>    falling back to a **self-build** over the snapshot's own frozen db when it is
>    not (always correct).
> 2. **`CodeGraph.diff(other)`** — a pure structural, content-addressed diff
>    (`GraphDiff`: added / removed / changed nodes, added / removed edges) over the
>    stable `symbol_id` identity, usable on any two `CodeGraph`s (pinned or live).
> 3. **Graph immutability** — a pinned graph is frozen (`_frozen`): structural
>    mutators (`apply_delta`, `rebuild`, the `_add_*` helpers) raise rather than
>    silently corrupting a held pin.
>
> **Still out of scope:** the file watcher as a change source feeding deltas
> (**Phase 8**); the floating warm cancel-retry `session.check()` fast path
> (**Phase 9**); benchmarks (**Phase 10**). Phase 7 adds a **read-only graph view**
> on the existing snapshot surface and a pure diff; it introduces **no new mutation
> path** and (like Phase 6) **no Rust**.
>
> When you finish: the project compiles, the **entire existing Python suite still
> passes**, and a new suite proves the load-bearing invariants — `snap.graph()` is
> (a) **structurally equal to `CodeGraph.build` over the snapshot's revision**
> (consistency with `snap.check()`), (b) **pinned**: it does not change when HEAD
> edits land and the HEAD graph advances, (c) **copy-on-pin == self-build** (the two
> construction strategies agree), and `diff` reports exactly the node/edge set
> changes a known edit produced.

---

## 0. Mental model (read this first)

### 0.1 Why a snapshot needs its *own* graph, and why it can be cheap

A `Snapshot` (Phase 4) is an independent `ProjectDatabase` pinned at revision R: its
`check()` / `document_symbols()` / `file_occurrences()` all read R's content and
never drift (architecture §0, §3.3). The graph must mirror that: a graph queried
*through* a snapshot must describe **the same R**, or `snap.check()` and
`snap.graph()` disagree and every cross-revision diff is meaningless.

There are two honest ways to produce that pinned graph (architecture §5.2):

| Strategy | How | Cost | When |
|---|---|---|---|
| **copy-on-pin** | `PyDiGraph.copy()` the HEAD graph + rebuild secondary indexes | C-level structural copy + O(V+E) in-memory index rebuild — **fast**, no FFI | the HEAD graph is materialised **and** already reflects R (the common `with session.snapshot() as snap: snap.graph()` path) |
| **self-build** | `CodeGraph.build(snap)` over the snapshot's frozen db | full six-pass build: FFI reads + cold snapshot memos — **slow but always correct** | no HEAD graph exists, or it has drifted off R (time-travel `snapshot(at=r0)`, or edits landed between `snapshot()` and `graph()`) |

`PyDiGraph.copy()` is already used in this codebase (`graph.py:1115`, `_semantic_subgraph`)
and **preserves node indices** (that helper's own docstring relies on it,
`graph.py:1106`). Preserved indices are what make copy-on-pin cheap: the copied rx
graph's node indices match the source, so the secondary-index dicts can be rebuilt
from the copy with a single `_rebuild_indexes()` pass (Phase 6 `graph.py`) — no
FFI, no re-analysis. **Both** strategies yield an ordinary, full-speed-queryable
`CodeGraph`; they differ only in *construction* cost. Copy-on-pin is the
optimisation the architecture recommends; self-build is the correctness floor.

### 0.2 The pinning correctness rule (the one subtle move)

Copy-on-pin is only correct **if the HEAD graph reflects exactly R at the instant of
the copy.** The HEAD graph floats — Phase 6's `apply_delta` advances it to the
latest head revision on every write. So `snap.graph()` must **gate the copy on a
revision match** and bail to self-build otherwise:

```text
snap.graph():
  if cached pin exists: return it
  R    = snap.revision
  head = the session's live HEAD CodeGraph, or None
  if head is not None and head._revision == R:
      pinned = head._pin_at(R)        # copy-on-pin; returns None if R raced away
  else:
      pinned = None
  if pinned is None:
      pinned = CodeGraph.build(snap)  # self-build over the frozen db @ R (always R)
  pinned._frozen = True
  cache and return pinned
```

Revisions are **monotonic** (Phase 3 `Revision(n+1)`): once HEAD moves past R it
never returns to R. So a `_revision == R` check that brackets the `.copy()` (read R
before, re-check after — §2.4) is sound: if an edit raced in, the post-copy re-check
fails and we fall to self-build. This is the graph analogue of Phase 4's read-once
capture: pin at first observation, never re-read a moving source.

### 0.3 Content-addressed identity = a meaningful diff (the Phase 6 payoff)

`symbol_id_from_symbol` (`identity.py:13`) keys a node `file::qualified_name`
(stable across content edits) or `file::name@line` (shifts when lines move). Phase 6
preserved this so that re-indexing a changed file re-creates the **same** node ids
for unchanged symbols. Phase 7 cashes that in: `diff` is a **set difference over
`symbol_id`** (nodes) and over **`(source_id, target_id, kind)` triples** (edges).
Because unchanged symbols keep their ids across revisions, the diff of two adjacent
revisions is **small and legible** — it names the symbols and edges your edit
actually added or removed, not a churn of renumbered nodes.

Caveat to carry from Phase 6 §0.3: `name@line` ids are not content-stable, so an
edit that shifts an anonymous symbol's line shows up in the diff as a remove+add of
that node rather than a "change". That is inherent to anonymous symbols and
*correct* (it mirrors what a rebuild produces); diff *legibility* is best-effort,
diff *correctness* (it equals the true set delta between the two graphs) is the gate.

### 0.4 Immutability: a pin must not be corruptible by HEAD

A pinned graph is handed to an agent that may hold it for a long time while HEAD
keeps mutating. Two independent objects already give *value* isolation (the pin is a
separate `CodeGraph` with its own rx graph and index dicts). Phase 7 adds *intent*
isolation: mark the pin `_frozen = True` and make the structural mutators
(`apply_delta`, `rebuild`, `_add_node`, `_add_edge`, `_index_files`, `_rebuild_indexes`)
**raise** on a frozen graph. This catches the bug where code accidentally treats a
pin as the live graph. Pure *read* queries (`symbol`, `dependencies`,
`references_to`, `diff`, the rustworkx algorithms) are unaffected — a frozen graph
is fully queryable, just not writable.

> Beware the one self-mutating *read*: `_semantic_subgraph` (`graph.py:1103`)
> memoises into `self._semantic_subgraph_cache`. That is a cache write, not a
> structural mutation, and must stay allowed on a frozen graph (§2.3). Freeze guards
> structural edits only.

### 0.5 Where the pieces live, and the coupling that copy-on-pin needs

`Snapshot.graph()` needs to *reach* the session's live HEAD graph to copy it. That
is a back-reference from the (otherwise independent) snapshot to the session. Keep
it minimal and optional:

- `TyO3Session.snapshot(at)` passes a **zero-arg head-graph provider** (a bound
  callable returning the session's live HEAD `CodeGraph` or `None`) plus the
  session's **root** into the `Snapshot` constructor.
- A `Snapshot` constructed **without** a provider (the bare
  `Snapshot(native_snapshot)` path, used in some tests) always self-builds — it is
  fully independent, exactly as Phase 4 left it.
- The provider is only ever *read* under the GIL for a `.copy()`; the snapshot never
  mutates the HEAD graph. So this coupling does not compromise snapshot independence
  or thread-safety (§7.3).

This means **Phase 7 depends on Phase 6's optional `session.graph`** (Phase 6 §6).
If Phase 6 deferred wiring `session.graph`, Phase 7 wires it (§4.1) — it is the
source copy-on-pin copies from, and without it `snap.graph()` still works via the
self-build floor, just never warm.

---

## 1. Prerequisite check

Phase 7 assumes Phases 1–6 are merged/working. Run the baseline first (always via
devenv — see `MEMORY.md`, never bare pytest):

```bash
devenv shell -- check-rust
devenv shell -- test-rust
devenv shell -- tests
devenv shell -- pytest src/tyo3/tests/test_graph_incremental.py -q   # Phase 6 parity
```

You rely on, and will extend (all in `src/tyo3/graph/graph.py` unless noted):

- `CodeGraph.build` (`graph.py:117`) — the self-build path; Phase 7 calls it with a
  `Snapshot` as `source` (duck-typed read surface, Phase 6 §0.5). Phase 7 records
  the source's revision on the built graph (§2.2).
- `CodeGraph._graph` (`graph.py:89`), `_id_to_index`, `_file_to_nodes`,
  `_file_to_edges`, `_file_node_ranges`, `_name_prefix_index`, `_diagnostics`,
  `_semantic_subgraph_cache` — the state copy-on-pin must reproduce on the pin.
- `_rebuild_indexes` (Phase 6, `graph.py` ~`:925`) — reconstructs **every** secondary
  index from graph state; copy-on-pin reuses it on the copied rx graph (§2.4).
- `CodeGraph._root` and `CodeGraph._file_importers` (Phase 6 §2.1) — also carried
  onto the pin by `_rebuild_indexes` + an explicit `_root` copy.
- `apply_delta` / `rebuild` / `_add_node` / `_add_edge` / `_index_files` (Phase 6) —
  the structural mutators Phase 7 guards with the freeze check (§2.3).
- The `graph` property (`graph.py:1030`), `symbol`/`symbols_in_file`/etc. read
  surface — unaffected; they must keep working on a frozen graph.
- `TyO3Session` (`session.py:567`): `_root` (`:594`), `head` (`:603`), `snapshot`
  (`:632`), `_graph` (Phase 6 §6, if wired). `Snapshot` (`session.py:772`):
  `_inner`, `revision` (`:790`), `_native` (`:785`), `close`/`__enter__`/`__exit__`.
- `SymbolNode` (`graph/models.py`), `EdgeData` / `EdgeKind` (`graph/models.py`) — the
  payloads `diff` compares; Phase 7 adds `GraphDiff` / `EdgeRef` / `NodeChange` beside
  them.

`PyDiGraph.copy()` (rustworkx 0.17.1, `pyproject.toml` `rustworkx>=0.16.0`) preserves
node indices (`graph.py:1106` relies on this). `get_edge_data_by_index` /
`get_edge_endpoints_by_index` / `edge_indices` / `node_indices` are the rx accessors
`diff` and `_rebuild_indexes` already use.

> **Phase 6 surface dependency.** Phase 7's copy-on-pin reads `graph._revision`,
> `graph._root`, and `graph._file_importers`, and reuses `_rebuild_indexes`. The
> first does not exist until Phase 7 adds it (§2.2); the rest are Phase 6's. If you
> are building Phase 6 and Phase 7 in one stretch, add `_revision` in Phase 6's
> `__init__`/`build`/`apply_delta` and Phase 7 only consumes it — note the choice in
> the PR.

---

## 2. `CodeGraph`: revision tag, freeze, and `_pin_at` (`graph/graph.py`)

### 2.1 Two new fields in `__init__`

Add to `CodeGraph.__init__` (`graph.py:88`), beside Phase 6's `_root` /
`_file_importers`:

```python
def __init__(self) -> None:
    self._graph: rx.PyDiGraph = rx.PyDiGraph()

    # ... existing secondary indexes, Phase 6 _root / _file_importers ...

    # The revision this graph reflects. Set by build() (to the source's revision)
    # and advanced by apply_delta() (to delta.revision). None until built.
    # Phase 7: Snapshot.graph() compares it to snap.revision to decide whether the
    # live HEAD graph is a valid copy-on-pin source.
    self._revision: int | None = None

    # Phase 7: a pinned (snapshot) graph is immutable. When True, structural
    # mutators raise (see _check_mutable). Pure reads / queries are always allowed.
    self._frozen: bool = False
```

> Keep both on `__init__` so every `CodeGraph` has them (a fresh head graph is
> mutable with `_revision = None` until `build` sets it). Do not default `_frozen`
> per-construction-site; it flips to `True` exactly once, when a graph becomes a pin
> (§4.2).

### 2.2 Record the revision in `build` and `apply_delta`

In `build` (`graph.py:139`), after constructing `graph` and resolving the source's
revision, store it. The source is duck-typed (`TyO3Session` exposes `head`;
`Snapshot` exposes `revision` — Phase 6 §0.5), so resolve via a tiny helper:

```python
def _source_revision(source: Any) -> int | None:
    """The application revision a build/delta source reflects.

    Snapshot exposes `.revision`; TyO3Session exposes `.head`. Returns None if
    neither (e.g. a bare duck-typed stub in a unit test) — the graph is then
    untagged and never serves as a copy-on-pin source (Snapshot.graph self-builds).
    """
    rev = getattr(source, "revision", None)
    if rev is None:
        rev = getattr(source, "head", None)
    return rev
```

Then in `build` (right after `graph = cls()` / setting `graph._root`, `graph.py:139`):

```python
graph = cls()
root = session.root
root_resolved = root.resolve()
graph._root = root_resolved              # Phase 6
graph._revision = _source_revision(session)   # ← Phase 7
```

And at the **end of Phase 6's `apply_delta`** (after the structural update, before
return), advance the tag to the revision the delta produced:

```python
# Phase 7: the HEAD graph now reflects delta.revision.
self._revision = delta.revision
```

> `build` takes `session` (its parameter name) but the argument may be a `Snapshot` —
> `_source_revision` reads `.revision` first precisely for that case. The rescan
> branch of `apply_delta` calls `self._replace_with(CodeGraph.build(source, ...))`,
> which already sets `_revision` via `build`; the explicit assignment above covers
> the **incremental** branch.

### 2.3 The freeze guard on structural mutators

Add a one-line guard and call it at the top of every method that mutates graph
*structure*:

```python
def _check_mutable(self) -> None:
    """Raise if this graph is a frozen (pinned) snapshot graph.

    Pinned graphs returned by Snapshot.graph() are immutable: an agent may hold one
    for a long time while HEAD keeps changing. Structural mutation of a pin is
    always a bug (the caller meant the live HEAD graph). Pure reads are unaffected.
    """
    if self._frozen:
        raise RuntimeError(
            "this CodeGraph is a frozen snapshot pin (Snapshot.graph()); "
            "it is immutable. Mutate the live session graph instead."
        )
```

Call `self._check_mutable()` as the first statement of the **structural** mutators:

- `apply_delta` (Phase 6)
- `rebuild` (`graph.py:1373`)
- `_replace_with` (Phase 6 §5.2)
- `_index_files` (Phase 6 §4)
- `_rebuild_indexes` (Phase 6 §2.4) — **but see the exception below**
- the node/edge primitives `_add_node` (`graph.py:893`) and `_add_edge`
  (`graph.py` — the single edge-creation site), if you prefer defence-in-depth at the
  lowest level

> **`_rebuild_indexes` exception.** Copy-on-pin (§2.4) calls `_rebuild_indexes` on
> the pin *while building it* — i.e. **before** `_frozen` is set. So either (a) set
> `_frozen = True` strictly *after* `_pin_at` finishes (recommended — §2.4 / §4.2 do
> exactly this), and you may guard `_rebuild_indexes` freely; or (b) leave
> `_rebuild_indexes` unguarded. Pick (a): freeze last, guard structural entry points.
> Do **not** guard `_semantic_subgraph` / `_semantic_subgraph_cache` writes — those
> are read-time memoisation and must work on a frozen graph (§0.4).

### 2.4 `_pin_at` — the copy-on-pin primitive

A method that returns an **immutable copy of this graph iff it currently reflects
`revision`**, else `None` (signalling the caller to self-build). It brackets the
structural `.copy()` with a monotonic revision re-check (§0.2):

```python
def _pin_at(self, revision: int) -> CodeGraph | None:
    """Copy-on-pin: return an immutable CodeGraph copy of this (HEAD) graph IF it
    currently reflects *revision*, else None.

    Used by Snapshot.graph() as the fast path. The rx-graph .copy() is a C-level
    structural copy that preserves node indices, so the pin's secondary indexes are
    reconstructed in-memory by _rebuild_indexes — no FFI, no re-analysis. Correct
    only when this graph reflects *revision*; the monotonic re-check around .copy()
    rejects a graph that an edit advanced mid-copy (the caller then self-builds).

    The returned graph is NOT yet frozen — the caller sets _frozen after caching it
    (so _rebuild_indexes here is not blocked by the freeze guard).
    """
    if self._revision != revision:
        return None
    copied = self._graph.copy()              # atomic C call; preserves node indices
    if self._revision != revision:           # an edit raced in during/after copy
        return None
    pin = CodeGraph()
    pin._graph = copied
    pin._root = self._root
    pin._revision = revision
    pin._diagnostics = {k: list(v) for k, v in self._diagnostics.items()}
    pin._dependency_cache = dict(self._dependency_cache)
    pin._rebuild_indexes()                   # rebuild ALL secondary indexes from `copied`
    return pin
```

Notes:

- **Why `_rebuild_indexes` and not copy the dicts.** The HEAD graph's index values
  are mutated *in place* by `apply_delta` (`_rebuild_indexes` does
  `self._file_to_nodes.clear()` etc., Phase 6 §2.4 — clearing the **same list/dict
  objects**). Sharing them with the pin would let a later HEAD edit corrupt the pin.
  Re-deriving from the copied rx graph gives the pin its own independent indexes and
  reuses Phase 6's single index-builder — no drift risk. It is O(V+E) in-memory,
  far cheaper than `CodeGraph.build`'s FFI passes.
- **Diagnostics** are not in the rx graph, so copy `_diagnostics` explicitly. A
  shallow `{k: list(v)}` is enough: `Diagnostic` payloads are immutable Pydantic
  models, and HEAD's `_refresh_diagnostics` (Phase 6 §5.3) `clear()`s its **own**
  dict and builds fresh lists — it never mutates the lists the pin now owns.
- **`_dependency_cache`** (`graph.py:109`) is an external-package cache; copy the dict
  reference map so the pin can resolve externals without re-reaching the head.
- **Do not set `_frozen` here.** The caller (`Snapshot.graph()`) sets it after the pin
  is cached, so `_rebuild_indexes` above is not rejected by the guard (§2.3).

> Confirm `_rebuild_indexes` rebuilds `_root`-independent indexes only from graph
> state (it does — it iterates `node_indices` / `edge_indices`). `_root` is not
> derivable from the graph, so copy it explicitly (above). If Phase 6's
> `_rebuild_indexes` does **not** rebuild `_file_node_ranges` / `_name_prefix_index`
> (it should — Phase 6 §2.4 clears them), verify and extend it; the pin must be a
> faithful, fully-indexed `CodeGraph` or its range/name queries break.

---

## 3. `CodeGraph.diff` + the diff models

### 3.1 The result models (`graph/models.py`)

Add beside `EdgeData` / `EdgeKind`:

```python
class EdgeRef(BaseModel):
    """An identity-stable reference to an edge: (source symbol, target symbol, kind).

    Edges have no intrinsic id, so a diff identifies them by their endpoints'
    content-addressed symbol_ids plus the edge kind — stable across revisions
    exactly when the endpoint symbols are (identity.py).
    """

    model_config = ConfigDict(frozen=True)

    source_id: str
    target_id: str
    kind: EdgeKind


class NodeChange(BaseModel):
    """A symbol present in both graphs under the same symbol_id but with a changed
    payload (e.g. signature, range, documentation, or kind)."""

    model_config = ConfigDict(frozen=True)

    symbol_id: str
    before: SymbolNode
    after: SymbolNode


class GraphDiff(BaseModel):
    """Structural difference between two CodeGraphs, content-addressed by symbol_id.

    Produced by ``after.diff(before)`` — it describes how to transform *before*
    (the `other` argument) into *after* (the receiver / `self`):

      * added_nodes   — symbol_ids in `after` but not `before`
      * removed_nodes — symbol_ids in `before` but not `after`
      * changed_nodes — symbol_ids in both whose payload differs
      * added_edges   — (src,tgt,kind) in `after` but not `before`
      * removed_edges — (src,tgt,kind) in `before` but not `after`

    For two adjacent-revision snapshot pins this is the "what did my edit change?"
    answer; it is small and legible because unchanged symbols keep their ids
    (architecture §5.1, §6; Phase 6 §0.3).
    """

    model_config = ConfigDict(frozen=True)

    added_nodes: list[SymbolNode] = Field(default_factory=list)
    removed_nodes: list[SymbolNode] = Field(default_factory=list)
    changed_nodes: list[NodeChange] = Field(default_factory=list)
    added_edges: list[EdgeRef] = Field(default_factory=list)
    removed_edges: list[EdgeRef] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when the two graphs are structurally identical."""
        return not (
            self.added_nodes
            or self.removed_nodes
            or self.changed_nodes
            or self.added_edges
            or self.removed_edges
        )
```

Export `EdgeRef`, `NodeChange`, `GraphDiff` from `graph/__init__.py`'s `__all__`
(beside `EdgeData` etc.).

### 3.2 `CodeGraph.diff` (`graph/graph.py`)

A pure read (no `_check_mutable`), placed near the query surface (beside
`coupling_between`, `graph.py:1386`, or in a new "Cross-revision diff" section):

```python
def diff(self, other: CodeGraph) -> GraphDiff:
    """Structural diff of this graph against *other*, content-addressed by symbol_id.

    ``after.diff(before)`` describes the transformation *before → after*: nodes /
    edges added in `self`, removed from `other`, and nodes whose payload changed.
    Pure read; valid on frozen pins or live graphs. O(V+E) over both graphs.

    Typical use (architecture §6)::

        before = session.snapshot(at=r0)
        after  = session.snapshot(at=r1)
        delta  = after.graph().diff(before.graph())   # what the edit changed
    """
    self_nodes = self._nodes_by_id()
    other_nodes = other._nodes_by_id()
    self_ids = self_nodes.keys()
    other_ids = other_nodes.keys()

    added_nodes = [self_nodes[i] for i in self_ids - other_ids]
    removed_nodes = [other_nodes[i] for i in other_ids - self_ids]
    changed_nodes = [
        NodeChange(symbol_id=i, before=other_nodes[i], after=self_nodes[i])
        for i in self_ids & other_ids
        if self_nodes[i] != other_nodes[i]      # frozen SymbolNode == is field-wise
    ]

    self_edges = self._edge_refs()
    other_edges = other._edge_refs()
    added_edges = sorted(self_edges - other_edges, key=_edge_ref_key)
    removed_edges = sorted(other_edges - self_edges, key=_edge_ref_key)

    return GraphDiff(
        added_nodes=added_nodes,
        removed_nodes=removed_nodes,
        changed_nodes=changed_nodes,
        added_edges=added_edges,
        removed_edges=removed_edges,
    )
```

With two small private helpers and a sort key:

```python
def _nodes_by_id(self) -> dict[str, SymbolNode]:
    """symbol_id -> node payload for every node in the graph."""
    return {self._graph[i].symbol_id: self._graph[i] for i in self._graph.node_indices()}

def _edge_refs(self) -> set[EdgeRef]:
    """The set of (source_id, target_id, kind) triples for every edge."""
    refs: set[EdgeRef] = set()
    g = self._graph
    for ei in g.edge_indices():
        data = g.get_edge_data_by_index(ei)
        if data is None:
            continue
        src, tgt = g.get_edge_endpoints_by_index(ei)
        refs.add(EdgeRef(source_id=g[src].symbol_id, target_id=g[tgt].symbol_id, kind=data.kind))
    return refs
```

```python
def _edge_ref_key(e: EdgeRef) -> tuple[str, str, str]:
    return (e.source_id, e.target_id, str(e.kind))
```

Notes:

- **`SymbolNode` equality.** It is a frozen Pydantic model (`graph/models.py`), so
  `==` is field-wise — exactly the "payload changed" predicate `changed_nodes` needs
  (range / signature / documentation / kind all participate). No custom `__eq__`.
- **Edge multiplicity.** `_edge_refs` is a **set**, so two parallel edges of the same
  `(src,tgt,kind)` collapse — `diff` reports *kinds present*, not counts. That matches
  Phase 6's parity helper (`_edge_triples`, Phase 6 §7.1) and is the right altitude
  for "what changed". If a caller ever needs multiplicity, a `Counter`-based variant
  is a later addition; do not build it now.
- **Direction.** `after.diff(before)` ⇒ `self=after`, `other=before`. `added_*` =
  in after / not before (what the edit introduced); `removed_*` = in before / not
  after (what it deleted). Document this clearly — the argument order is the whole
  API.

---

## 4. `Snapshot.graph()` + session wiring (`src/tyo3/session.py`)

### 4.1 Ensure the session exposes a live HEAD graph (Phase 6 §6, made required)

Copy-on-pin needs a source: the session's live HEAD `CodeGraph`. Phase 6 left
`session.graph` optional; Phase 7 requires it (or the self-build floor is the only
path — correct but never warm). Wire it thinly and lazily, with a lazy import to
dodge the `graph ↔ session` cycle (Phase 6 §6 caution 1, `graph.py:25` imports
`TyO3Session`):

```python
# TyO3Session.__init__ — add beside self._head_snap (session.py:596)
self._graph: Any = None          # live HEAD CodeGraph, built lazily on first access

# ── HEAD graph (Phase 6/7) ────────────────────────────────────────
@property
def graph(self) -> Any:
    """The live HEAD CodeGraph, built lazily on first access and updated
    incrementally as edits land (Phase 6 apply_delta). Reads through a snapshot
    pinned at the current revision for consistency."""
    self._check_open()
    if self._graph is None:
        from tyo3.graph import CodeGraph
        with self.snapshot() as snap:
            self._graph = CodeGraph.build(snap)
    return self._graph

def _head_graph_or_none(self) -> Any:
    """The materialised HEAD graph, or None if it was never built. Used as the
    copy-on-pin source for Snapshot.graph(); never triggers a build (a snapshot
    must not force the head graph into existence just to copy it)."""
    return self._graph

def _apply_graph_delta(self, sync: SyncResult) -> None:
    """If a HEAD graph has been materialised, fold the write's delta into it so the
    pin source stays at the head revision. No-op until .graph is first accessed."""
    if self._graph is None:
        return
    with self.snapshot() as snap:        # pinned at sync.revision
        self._graph.apply_delta(snap, sync)
```

If Phase 6 already wired `session.graph` + `_apply_graph_delta`, **reuse it** and add
only `_head_graph_or_none`. Either way, every write method
(`edit`/`edit_many`/`edit_virtual`/`sync_path`/`discard`/`sync_all`, `session.py:646-720`)
must call `self._apply_graph_delta(result)` **after** producing its `SyncResult` and
after `_invalidate_head_snap()` — Phase 6 §6 already specifies this; verify it, since
Phase 7's copy-on-pin fast path is only ever *taken* when the head graph tracks the
head revision.

> Honest cost note (Phase 6 §6, repeated): once `.graph` is touched, every write pays
> one `apply_delta` + one head snapshot build. Users who never touch `.graph` pay
> nothing (`_apply_graph_delta` is a no-op while `_graph is None`), and their
> `snap.graph()` self-builds. Document `.graph`/`snap.graph()` as opt-in.

### 4.2 `Snapshot.graph()` + constructor wiring

Extend the `Snapshot` constructor (`session.py:781`) to accept the optional
head-graph provider and the project root, and add the cached, lazy `graph()`:

```python
class Snapshot(_ReadOps):
    def __init__(
        self,
        native_snapshot: Any,
        *,
        root: StdPath | None = None,
        head_graph_provider: Any = None,   # zero-arg callable -> live HEAD CodeGraph | None
    ) -> None:
        self._inner = native_snapshot
        self._closed = False
        self._root = root                       # for the self-build fallback
        self._head_graph_provider = head_graph_provider
        self._pinned_graph: Any = None          # cached pin (built once, on first graph())

    def _native(self) -> Any:
        self._check_open()
        return self._inner

    @property
    def revision(self) -> int:
        self._check_open()
        return self._inner.revision

    @property
    def root(self) -> StdPath | None:
        """The project root, if this snapshot was created by a session. Needed by
        CodeGraph.build for the self-build path; None for a bare snapshot."""
        return self._root

    def graph(self) -> Any:
        """An immutable CodeGraph pinned at this snapshot's revision.

        Consistent with this snapshot's other reads (check / symbols / occurrences):
        snap.check() and snap.graph() describe the same revision R.

        Fast path (copy-on-pin): if the session's live HEAD graph reflects exactly R,
        copy it (C-level). Otherwise self-build over this snapshot's frozen db
        (always correct, colder). Cached after the first call.
        """
        self._check_open()
        if self._pinned_graph is not None:
            return self._pinned_graph

        from tyo3.graph import CodeGraph

        r = self.revision
        pin = None
        provider = self._head_graph_provider
        if provider is not None:
            head = provider()
            if head is not None:
                pin = head._pin_at(r)            # None unless head reflects exactly R

        if pin is None:
            if self._root is None:
                raise InternalTyError(
                    "Snapshot.graph() requires a session-created snapshot (no root "
                    "for the self-build fallback)."
                )
            pin = CodeGraph.build(self)          # self-build over the frozen db @ R

        pin._frozen = True                       # freeze AFTER construction (§2.3/§2.4)
        self._pinned_graph = pin
        return pin

    # close / __enter__ / __exit__ / __del__ — unchanged, except drop the pin:
    def close(self) -> None:
        if self._closed:
            return
        self._pinned_graph = None                # release the pinned graph
        self._inner.close()
        self._closed = True
```

And have `TyO3Session.snapshot` pass the wiring (`session.py:632`):

```python
def snapshot(self, at: int | None = None) -> Snapshot:
    self._check_open()
    try:
        native_snapshot = self._inner.snapshot(at)
    except _NativeClosedError as e:
        raise ProjectClosedError(str(e)) from e
    except Exception as e:
        raise InternalTyError(f"Unexpected error in snapshot(): {e}") from e
    return Snapshot(
        native_snapshot,
        root=self._root,
        head_graph_provider=self._head_graph_or_none,
    )
```

Notes:

- **`CodeGraph.build(self)` needs `self.root` and `self.files()`.** The `Snapshot`
  now has both: `root` (passed from the session, §4.2) and `files()` (the `_ReadOps`
  surface, `session.py:108`). `build` also calls the read methods
  (`document_symbols`, `file_occurrences`, …) which dispatch through `_native()` to
  the frozen db — all pinned at R, so the self-build is internally consistent. This
  is the duck-typing Phase 6 §0.5 established; Phase 7 just supplies the missing
  `.root`.
- **Pin lifetime = snapshot lifetime.** The pin is cached on the snapshot and dropped
  in `close()`. Holding the pin keeps its (independent) rx graph alive; closing the
  snapshot releases it. Time-travel snapshots and head snapshots behave identically.
- **Bare `Snapshot(native)` still works.** With no provider and no root,
  `graph()` raises a clear error rather than silently returning a wrong-revision
  graph. Session-created snapshots (the only ones with a graph need) always carry
  both.

### 4.3 `.pyi` and exports

- `graph/__init__.py`: add `EdgeRef`, `GraphDiff`, `NodeChange` to imports + `__all__`.
- No `_native_impl.pyi` change — `Snapshot.graph()` is pure Python on the `Snapshot`
  wrapper, not on the native `TySnapshot`. (The native snapshot is unchanged; Phase 7
  adds **no Rust**.)
- If the project ships a typed `Snapshot` stub or re-exports `Snapshot` publicly,
  annotate `graph(self) -> CodeGraph` and `root` there; otherwise the inline
  annotations suffice.

---

## 5. Tests (`src/tyo3/tests/test_graph_snapshots.py`)

Pure Python (the graph is Python; Phase 7 adds no Rust). Reuse the Phase 6 parity
helpers — copy `_node_ids` / `_edge_triples` / `_assert_structurally_equal` from
`test_graph_incremental.py` (Phase 6 §7.1) or import them if that module exposes
them. The **load-bearing** tests are: pin == build (consistency), pin is isolated
(pinned across edits), copy-on-pin == self-build (strategies agree), and diff
reports the true delta.

```python
from __future__ import annotations

import textwrap
from pathlib import Path as StdPath

from tyo3 import TyO3Session
from tyo3.graph import CodeGraph, EdgeKind


def _node_ids(g: CodeGraph) -> set[str]:
    return {g.graph[i].symbol_id for i in g.graph.node_indices()}


def _edge_triples(g: CodeGraph) -> set[tuple[str, str, str]]:
    out: set[tuple[str, str, str]] = set()
    for ei in g.graph.edge_indices():
        data = g.graph.get_edge_data_by_index(ei)
        s, t = g.graph.get_edge_endpoints_by_index(ei)
        out.add((g.graph[s].symbol_id, g.graph[t].symbol_id, str(data.kind)))
    return out


def _assert_structurally_equal(a: CodeGraph, b: CodeGraph) -> None:
    assert _node_ids(a) == _node_ids(b), (
        f"node mismatch\nonly in a: {_node_ids(a) - _node_ids(b)}\n"
        f"only in b: {_node_ids(b) - _node_ids(a)}"
    )
    assert _edge_triples(a) == _edge_triples(b), (
        f"edge mismatch\nonly in a: {_edge_triples(a) - _edge_triples(b)}\n"
        f"only in b: {_edge_triples(b) - _edge_triples(a)}"
    )
```

### 5.1 Consistency: `snap.graph()` == `build` over the snapshot's revision

```python
def test_snapshot_graph_equals_build_at_revision(tmp_path):
    (tmp_path / "models.py").write_text("class User:\n    def save(self): ...\n")
    (tmp_path / "app.py").write_text(
        "from models import User\n\n\ndef run():\n    return User().save()\n"
    )
    with TyO3Session(str(tmp_path)) as s:
        with s.snapshot() as snap:
            pinned = snap.graph()
            fresh = CodeGraph.build(snap)            # independent self-build @ R
        _assert_structurally_equal(pinned, fresh)
        assert pinned._revision == snap.revision     # tagged at R
```

### 5.2 The pin is immutable and isolated from later HEAD edits

```python
def test_snapshot_graph_is_pinned_across_edits(tmp_path):
    (tmp_path / "m.py").write_text("class User:\n    def save(self): ...\n")
    with TyO3Session(str(tmp_path)) as s:
        _ = s.graph                                  # materialise HEAD graph
        with s.snapshot() as snap:
            pin = snap.graph()
            before_nodes = _node_ids(pin)
            # land several HEAD edits; the HEAD graph advances, the pin must not.
            for extra in ("a", "b", "c"):
                s.edit("m.py", f"class User:\n    def save(self): ...\n    def {extra}(self): ...\n")
            assert _node_ids(pin) == before_nodes     # pin frozen at R
            assert _node_ids(s.graph) != before_nodes # HEAD moved on


def test_pinned_graph_rejects_mutation(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        with s.snapshot() as snap:
            pin = snap.graph()
            assert pin._frozen
            import pytest
            with pytest.raises(RuntimeError):
                pin.rebuild(s, "a.py")               # structural mutation blocked
```

### 5.3 Copy-on-pin and self-build agree (strategy parity)

```python
def test_copy_on_pin_equals_self_build(tmp_path):
    (tmp_path / "models.py").write_text("class User: ...\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\n")
    with TyO3Session(str(tmp_path)) as s:
        _ = s.graph                                  # HEAD graph at current revision
        with s.snapshot() as snap:
            # fast path: HEAD graph reflects snap.revision -> copy-on-pin
            assert s.graph._revision == snap.revision
            via_copy = snap.graph()
        # force self-build by snapshotting with no head-graph source
        from tyo3.session import Snapshot
        native = s._inner.snapshot(None)
        bare = Snapshot(native, root=s.root, head_graph_provider=None)
        try:
            via_build = bare.graph()
        finally:
            bare.close()
        _assert_structurally_equal(via_copy, via_build)


def test_time_travel_snapshot_self_builds(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        _ = s.graph
        r0 = s.head
        s.edit("a.py", "x = 2\ny = 3\n")             # HEAD graph now past r0
        with s.snapshot(at=r0) as old:
            pin = old.graph()                        # head != r0 -> self-build @ r0
            assert pin._revision == r0
            # reflects r0's single binding, not the edited two
            assert any(n.endswith("::x") or "x@" in n for n in _node_ids(pin))
```

### 5.4 `diff` reports the true delta

```python
def test_diff_added_symbol(tmp_path):
    (tmp_path / "m.py").write_text("class User:\n    def save(self): ...\n")
    with TyO3Session(str(tmp_path)) as s:
        r0 = s.head
        before = s.snapshot(at=r0)
        s.edit("m.py", "class User:\n    def save(self): ...\n    def load(self): ...\n")
        after = s.snapshot()
        try:
            d = after.graph().diff(before.graph())
            added = {n.symbol_id for n in d.added_nodes}
            assert any(sid.endswith("User.load") for sid in added)
            assert not any(sid.endswith("User.load") for sid in {n.symbol_id for n in d.removed_nodes})
            assert not d.is_empty
        finally:
            before.close(); after.close()


def test_diff_removed_symbol(tmp_path):
    (tmp_path / "m.py").write_text("class User:\n    def save(self): ...\n    def load(self): ...\n")
    with TyO3Session(str(tmp_path)) as s:
        before = s.snapshot()
        s.edit("m.py", "class User:\n    def save(self): ...\n")
        after = s.snapshot()
        try:
            d = after.graph().diff(before.graph())
            removed = {n.symbol_id for n in d.removed_nodes}
            assert any(sid.endswith("User.load") for sid in removed)
        finally:
            before.close(); after.close()


def test_diff_identical_graphs_is_empty(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        a = s.snapshot()
        b = s.snapshot()                              # same revision, no edit between
        try:
            assert a.graph().diff(b.graph()).is_empty
        finally:
            a.close(); b.close()


def test_diff_added_edge_on_new_reference(tmp_path):
    (tmp_path / "models.py").write_text("class User: ...\n")
    (tmp_path / "app.py").write_text("from models import User\n")
    with TyO3Session(str(tmp_path)) as s:
        before = s.snapshot()
        s.edit("app.py", "from models import User\nu = User()\n")   # adds a reference edge
        after = s.snapshot()
        try:
            d = after.graph().diff(before.graph())
            kinds = {e.kind for e in d.added_edges}
            assert EdgeKind.REFERENCES in kinds or EdgeKind.INSTANTIATES in kinds
        finally:
            before.close(); after.close()
```

### 5.5 Consistency pair: `snap.graph()` and `snap.check()` agree on revision

```python
def test_graph_and_check_same_revision(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x: int = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        with s.snapshot() as snap:
            g = snap.graph()
            s.edit("a.py", "x: int = 'bad'\n")        # HEAD now has a type error
            # the pin and the snapshot's check() both still describe R:
            assert g._revision == snap.revision
            # snapshot diagnostics unchanged (Phase 4 isolation), and the graph the
            # snapshot exposes is the same revision as those diagnostics.
            assert snap.revision == g._revision
```

> If a parity test fails, `_assert_structurally_equal` names the exact node/edge
> difference. Common causes: copy-on-pin sharing index lists with HEAD (forgot the
> `{k: list(v)}` / `_rebuild_indexes` independence — §2.4), `_pin_at` not re-checking
> the revision (pinned a drifted graph), or freeze set before `_rebuild_indexes`
> (the pin build trips its own guard — §2.3). See §7.

---

## 6. Build, test, iterate

```bash
devenv shell -- tests          # FULL Python suite — no regressions + new snapshot-graph suite
devenv shell -- pytest src/tyo3/tests/test_graph_snapshots.py -q
devenv shell -- pytest src/tyo3/tests/test_graph_incremental.py -q   # Phase 6 still green
```

No Rust changes in Phase 7 (the graph and snapshot wrapper are Python), so
`check-rust` / `test-rust` should be untouched — run them once to confirm you did not
accidentally edit Rust:

```bash
devenv shell -- check-rust
devenv shell -- test-rust
```

The full suite passing proves `snap.graph()` and `diff` are additive: existing
snapshot tests (Phase 4/5), graph tests (Phase 6), and write-path tests are
unchanged. The new suite proves the four Phase-7 invariants (consistency, pinning,
strategy parity, diff correctness).

---

## 7. Gotchas & decisions (read before you debug)

1. **Gate copy-on-pin on a revision match, and re-check around the copy (§0.2/§2.4).**
   The HEAD graph floats; copying it when `head._revision != snap.revision` pins the
   wrong revision. `_pin_at` returns `None` unless the revision matches both before
   *and* after the `.copy()` — then `graph()` self-builds. Skip the re-check and a
   mid-copy edit silently corrupts the pin.

2. **Re-derive the pin's indexes; never share them with HEAD (§2.4).** `apply_delta`
   mutates the HEAD index dicts/lists in place (`_rebuild_indexes` `clear()`s the same
   objects). A pin that shares them is corrupted by the next HEAD edit. `_pin_at`
   gives the pin its own indexes via `_rebuild_indexes` over the copied rx graph, and
   copies `_diagnostics` as `{k: list(v)}`.

3. **Freeze the pin *after* it is built (§2.3/§2.4).** `_pin_at` calls
   `_rebuild_indexes`, a guarded structural method. Set `_frozen = True` in
   `Snapshot.graph()` *after* caching the pin, not inside `_pin_at`. Otherwise the
   pin trips its own freeze guard mid-construction.

4. **Do not guard `_semantic_subgraph` (§0.4).** It memoises into
   `_semantic_subgraph_cache` on read; that cache write must work on a frozen graph.
   Guard *structural* mutators only (`apply_delta`, `rebuild`, `_index_files`,
   `_add_node`, `_add_edge`).

5. **`Snapshot.graph()` self-build needs `snap.root` (§4.2).** `CodeGraph.build`
   reads `source.root` and `source.files()`. Phase 4's `Snapshot` had neither root;
   Phase 7 passes `root` from the session into the constructor. A bare
   `Snapshot(native)` with no root raises a clear error from `graph()` rather than
   self-building wrongly.

6. **`diff` argument order is the API (§3.2).** `after.diff(before)` ⇒ `added_*` are
   in *after*, `removed_*` are in *before*. Get it backwards and "what my edit added"
   reads as "removed". Lock it with `test_diff_added_symbol` /
   `test_diff_removed_symbol`.

7. **`SymbolNode` equality is field-wise (§3.2).** It is a frozen Pydantic model, so
   `changed_nodes` correctly catches signature/range/doc/kind changes for a stable
   `symbol_id`. Do not add a custom `__eq__` or compare by identity.

8. **`name@line` ids show edits as remove+add, not change (§0.3).** Anonymous symbols
   whose line shifts get a new id, so they appear in `added`/`removed`, not
   `changed`. This is inherent and correct (mirrors a rebuild). Do not try to
   "repair" it in Phase 7.

9. **`_head_graph_or_none` must not build the head graph.** It returns
   `self._graph` (possibly `None`); it never triggers the lazy `.graph` build. A
   snapshot copying the head must not *force* the head graph into existence — if it
   was never built, self-build is the right (and only) source of truth.

10. **Pin lifetime = snapshot lifetime.** Cache the pin on the snapshot; drop it in
    `close()`. Do not cache pins on the session or in a global — that would defeat the
    retained-revision memory bound (architecture §9) and leak graphs across snapshots.

11. **Edge `EdgeRef` set collapses parallel same-kind edges (§3.2).** `diff` reports
    kinds present between two symbols, not multiplicity — matching Phase 6's parity
    altitude. If you ever need counts, that is a separate `Counter` API, not Phase 7.

12. **Lazy import inside the property/method (§4.1/§4.2).** `graph/graph.py` imports
    `TyO3Session` (`graph.py:25`); import `CodeGraph` lazily inside `session.graph`
    and `Snapshot.graph()` to avoid a circular import at module load.

---

## 8. Explicitly OUT of scope for Phase 7

- **The file watcher** feeding deltas (`ProjectWatcher` → `poll_changes` →
  `sync_path` → `apply_delta`) — **Phase 8**. Phase 7 pins/diffs graphs; it adds no
  change source.
- **The floating warm `session.check()` fast path** with cancel-retry — **Phase 9**.
- **Benchmarks** — copy-on-pin vs self-build timing, `diff` cost, memory across many
  retained snapshot pins — **Phase 10**. Phase 7 proves *correctness* (pin == build,
  copy == self-build, diff == true delta), not speed.
- **Interval-tagged / true graph-level MVCC** (the architecture §5.2 "purist
  alternative"): a single append-only graph with `[r_start, r_end)` payloads and
  revision-filtered views. Explicitly **not chosen** — copy-on-pin gives the same
  observable semantics without an O(V+E) revision filter on every query. Do not build
  it.
- **Notebook / virtual-buffer graph nodes** in pins — the graph indexes system-path
  Python files as today (Phase 6 scope); `edit_virtual` revisions still are not graphed.
- **A `Snapshot.graph()` on the native `TySnapshot`** — Phase 7 is pure Python on the
  `Snapshot` wrapper; do not add Rust or touch `_native_impl.pyi`'s `TySnapshot`.
- **Persisting / serialising pins or diffs** beyond the existing `to_json`/`to_dot`
  exporters (`graph/export.py`) — out of scope.

If you find yourself wiring `ProjectWatcher`, adding a cancel-retry loop, writing
timing assertions, building an interval-tagged graph, or editing Rust, stop — you
have left Phase 7.

---

## 9. Definition of Done

- [ ] `graph/models.py`: `EdgeRef`, `NodeChange`, `GraphDiff` (frozen Pydantic) added;
      exported from `graph/__init__.py` `__all__`.
- [ ] `graph.py`: `CodeGraph.__init__` gains `_revision: int | None` and
      `_frozen: bool`.
- [ ] `graph.py`: `_source_revision(source)` helper; `build` sets
      `_revision = _source_revision(session)`; Phase 6 `apply_delta` sets
      `_revision = delta.revision` at the end of its incremental branch.
- [ ] `graph.py`: `_check_mutable()` added and called at the top of `apply_delta`,
      `rebuild`, `_replace_with`, `_index_files` (and optionally `_add_node`/`_add_edge`);
      `_semantic_subgraph` left unguarded.
- [ ] `graph.py`: `_pin_at(revision)` — revision-gated `.copy()` + `_rebuild_indexes`
      + explicit `_root`/`_revision`/`_diagnostics`/`_dependency_cache` copy; returns
      `None` on revision mismatch (before or after the copy); does **not** set `_frozen`.
- [ ] `graph.py`: `diff(other)` + `_nodes_by_id` / `_edge_refs` / `_edge_ref_key`;
      `after.diff(before)` semantics documented; `GraphDiff.is_empty`.
- [ ] `session.py`: `TyO3Session` exposes `graph` (lazy, Phase 6) + `_head_graph_or_none`
      + `_apply_graph_delta` wired into every write method; `_graph` initialised to
      `None`.
- [ ] `session.py`: `Snapshot.__init__` accepts `root` + `head_graph_provider`; adds
      `root` property, `_pinned_graph`, and `graph()` (copy-on-pin fast path →
      self-build floor, cached, frozen after build); `close()` drops the pin.
- [ ] `session.py`: `TyO3Session.snapshot` passes `root=self._root` and
      `head_graph_provider=self._head_graph_or_none` to `Snapshot`.
- [ ] New suite `test_graph_snapshots.py` passes: pin == build at revision; pin
      isolated across HEAD edits; pin rejects mutation; copy-on-pin == self-build;
      time-travel self-builds at r0; diff added / removed / edge / empty; graph-and-check
      same revision.
- [ ] `devenv shell -- tests` shows **no regressions** (Phase 4/5 snapshot tests,
      Phase 6 incremental tests, write-path tests unchanged).
- [ ] `devenv shell -- check-rust` / `test-rust` unchanged (no Rust edits).
- [ ] PR notes: whether `session.graph` was already wired by Phase 6 or added here;
      whether `_revision` was added in Phase 6 or Phase 7; the `diff` argument-order
      convention; and any deviation in `_rebuild_indexes`'s coverage that `_pin_at`
      had to compensate for.

---

## 10. How this seeds Phase 8+

Phase 7 completes the **two-track symmetry** the architecture promised (§5): the db
has HEAD (mutable) + REVISION (pinned) tracks, and now so does the graph — HEAD via
Phase 6 `apply_delta`, REVISION via Phase 7 `Snapshot.graph()`. A `Snapshot` is a
fully consistent pair: `snap.check()` and `snap.graph()` describe one R, and
`after.graph().diff(before.graph())` is the first-class "what changed?" capability the
product thesis rests on.

- **Phase 8** routes `ProjectWatcher` events through the existing `sync_path` →
  `SyncResult` → `apply_delta` pipeline (Phase 6) — the HEAD graph advances, and any
  snapshot taken afterwards pins/diffs against the new revision with **zero new graph
  code**. The watcher is just another change source feeding the same machinery.
- **Phase 9**'s floating warm `session.check()` is the cancellable "latest" read; it
  composes with `session.graph` (also "latest, mutable") as the warm counterpart to
  the cold, pinned `snap.graph()` — distinct by intent, no new graph surface.
- **Phase 10** benchmarks copy-on-pin vs self-build (the §5.2 tradeoff Phase 7
  deliberately makes), `diff` cost on large revisions, and the memory of K retained
  pins (architecture §9's retained-revision bound), characterising the honest costs.

Keep `_pin_at` revision-gated, the pin's indexes independent of HEAD, the freeze
guard on structural mutators only, and `diff` content-addressed, and Phases 8–10 drop
straight onto this pinned-graph surface.
