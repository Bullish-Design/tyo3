# Phase 6 Implementation Guide — Graph: HEAD incremental (`apply_delta`)

> Audience: an engineer who has finished **Phase 1**
> (`PHASE_1_IMPLEMENTATION_GUIDE.md`, `ContentStore` + `OverlaySystem`), **Phase 2**
> (`PHASE_2_IMPLEMENTATION_GUIDE.md`, the HEAD db over the overlay via real
> discovery), **Phase 3** (`PHASE_3_IMPLEMENTATION_GUIDE.md`, the write path →
> `SyncResult`), **Phase 4** (`PHASE_4_IMPLEMENTATION_GUIDE.md`, independent MVCC
> snapshots), and **Phase 5** (`PHASE_5_IMPLEMENTATION_GUIDE.md`, the concurrency
> proof), and is now implementing **Phase 6** of `MVCC_SUBSTRATE_ARCHITECTURE.md`.
>
> Goal of Phase 6: make the **HEAD graph an incremental layer driven by
> `SyncResult`**, mirroring the db's two-track model (architecture §5.1). Today
> `CodeGraph.rebuild(session, path)` (`graph.py:1373`) ignores `path` and
> full-rebuilds via `CodeGraph.build`. Phase 6 adds `CodeGraph.apply_delta(source,
> delta)`: given the `SyncResult` a write produced (Phase 3) and a consistent read
> surface (ideally the `Snapshot` pinned at that revision — Phase 4), it
> **surgically** drops nodes for changed/deleted files, re-indexes created/changed
> files, and revalidates inbound cross-file edges into the changed set — using the
> content-addressed node identity (`identity.py`) and a new **reverse-dependency
> index**, delegating every graph operation to rustworkx.
>
> Three things land together because they are one change:
>
> 1. **`apply_delta`** — the delta-driven update entry point (the new public API).
> 2. **A reverse-dependency index** (`importers_of[file]`) so inbound edge
>    revalidation touches only the files that import the changed set — no global
>    rescan.
> 3. **A per-file indexing pass** (`_index_files`) factored out of `build`'s six
>    passes, reused by both `build` and `apply_delta` so there is one symbol→node→
>    edge implementation, not two.
>
> **Still out of scope:** pinned graph snapshots (`Snapshot.graph()` copy-on-pin,
> cross-revision `graph.diff` — **Phase 7**); the file watcher as a change source
> (**Phase 8**); the floating warm `session.check()` fast path (**Phase 9**);
> benchmarks (**Phase 10**). Phase 6 is the **HEAD** (mutable, live) graph only.
>
> When you finish: the project compiles, the **entire existing Python suite still
> passes** (`rebuild` keeps working as the rescan fallback), and a new suite proves
> the load-bearing invariant — **`apply_delta` produces a graph structurally equal
> to a fresh `CodeGraph.build` over the same post-edit revision** for the changed /
> created / deleted / cross-file-edge cases (architecture §11 "Graph delta ==
> rebuild").

---

## 0. Mental model (read this first)

### 0.1 What the graph is, and why it can be incremental

`CodeGraph` (`graph.py:85`) is a `rx.PyDiGraph` of `SymbolNode`s plus secondary
indexes. `build` (`graph.py:117`) constructs it in six deterministic passes:

1. collect symbols per file (`_collect_symbols_for_file`, `:220`)
2. materialize **all** project nodes (`_materialize_file_nodes`, `:247`)
3. structural edges + range caches (`_add_containment_edges_for_file` `:294`,
   `_build_range_cache_for_file` `:305`)
4. semantic references (`_resolve_references_via_occurrences`, `:411`)
5. inheritance / overrides (`_resolve_inheritance`, `:755`)
6. diagnostics — one project-wide `check()` distributed per file
   (`_collect_all_diagnostics`, `:327`)

The reason a delta update is possible **and produces the same graph** is the
ownership index `_file_to_nodes` (`graph.py:93`): every node records its `file`, so
"the nodes owned by file F" is O(1) to find, and rustworkx's `remove_nodes_from`
auto-removes incident edges. Re-running passes 1–6 for *only* the changed files,
plus a narrow inbound-edge fix-up, reconstructs exactly the subgraph that changed.

### 0.2 The algorithm (architecture §5.1, made precise)

```text
apply_delta(source, delta):
  if delta.rescan:                      # sync_all ⇒ delta unknown
      self._replace_with(build(source)); return

  dirty   = changed ∪ deleted           # files whose nodes must be dropped
  index   = created ∪ changed           # files to (re-)extract nodes+out-edges for

  # 0. BEFORE removal: who imports the dirty set? (the edges are about to vanish)
  importers = self._importers_of(dirty) − index

  # 1. drop nodes owned by dirty files (incident edges go too, incl. inbound)
  remove_nodes_from( nodes owned by dirty )
  self._rebuild_indexes()               # swap-and-pop invalidates indices

  # 2. re-extract created+changed (sub-passes, so inter-dirty refs resolve)
  self._index_files(index)

  # 3. revalidate INBOUND edges: re-resolve each importer's refs INTO dirty only
  for importer in importers:
      self._reresolve_inbound(importer, into=dirty)

  # 4. diagnostics: one check(), redistribute (cross-file diags can change)
  self._refresh_diagnostics(source)
```

The single subtle move is **step 0 before step 1**: an edge `X::<module> →
C::<module>` (file X imports changed file C) is *incident to a node owned by C*. So
`remove_nodes_from(C's nodes)` in step 1 **already deletes every importer→dirty
edge**. That is good — it means step 3 never has to *remove* anything, only re-add
the importer's edges into the dirty set (with `restrict_targets=dirty` so it does
not duplicate the importer's surviving edges into *non*-dirty files). But it also
means the reverse-dependency information is gone after step 1, so we must snapshot
`importers` *before* removal.

### 0.3 Content-addressed identity = minimal, meaningful deltas

`symbol_id_from_symbol` (`identity.py:13`) keys a node as `file::qualified_name`
(or `file::name@line` when there is no qualified name). For the qualified-name
case, **a symbol that did not change keeps its node id across revisions**, so
re-indexing a changed file re-creates the *same* node ids for unchanged symbols and
only the genuinely-changed symbols differ. This is what makes "what edges did my
edit add/remove?" a meaningful question later (Phase 7 `graph.diff`).

Caveat to internalise now: `name@line` ids are **not** content-stable — inserting a
blank line shifts every following `@line`. Phase 6 does not try to fix this (it is
inherent to anonymous symbols); `apply_delta` is still *correct* because it drops
and re-creates the whole changed file, so a shifted `@line` id is simply a
remove+add, exactly as a rebuild would produce. Delta *minimality* is best-effort;
delta *correctness* (== rebuild) is the gate.

### 0.4 Paths: the delta speaks absolute, the graph speaks relative

`SyncResult.created / changed / deleted` (`models/analysis.py:82`) are **absolute
native path strings** (Phase 3 resolves every edit path to an absolute
`SystemPathBuf` and reports `abs.as_str()`). The graph works in **project-relative
POSIX** graph paths (`_to_relative`, `graph.py:30`) but calls session/snapshot read
methods with **native (absolute)** paths (`build` threads a `native_by_graph` map,
`graph.py:144`). `apply_delta` must do the same conversion both directions:

- `graph_path = _to_relative(self._root, abs)` — for graph bookkeeping.
- native path for read calls = the absolute string itself (for created/changed
  files we have it directly from the delta; for importers, look it up in a
  `native_by_graph` rebuilt from `source.files()`).

`self._root` does not exist on the graph today — Phase 6 stores it at `build` time
(§2.1), because `apply_delta`'s `source` may be a `Snapshot`, which has no `.root`.

### 0.5 What `source` is, and why a Snapshot is the right one

`apply_delta(source, delta)` reads symbols/occurrences/hierarchy/diagnostics for
the changed files. Those reads **must be mutually consistent** and pinned at
`delta.revision`, or the graph mixes revisions. The clean caller is:

```python
sync = session.edit("a.py", new_text)        # head now at sync.revision
with session.snapshot() as snap:             # independent db pinned at sync.revision
    graph.apply_delta(snap, sync)            # every read inside sees one revision
```

`source` only needs the `_ReadOps` surface (`document_symbols`, `file_occurrences`,
`type_hierarchy`, `semantic_tokens`, `goto_definition`, `check`) plus `files()` —
both `TyO3Session` and `Snapshot` provide all of these (`session.py:85`,
`session.py:772`). So `apply_delta` is duck-typed on the read surface; pass a
`Snapshot` for true MVCC consistency, or a `TyO3Session` for "latest" (acceptable
because nothing mutates HEAD *during* a single `apply_delta` call). Recommend the
snapshot in docs and tests.

---

## 1. Prerequisite check

Phase 6 assumes Phases 1–5 are merged/working. Run the baseline first (always via
devenv — see `MEMORY.md`, never bare pytest):

```bash
devenv shell -- check-rust
devenv shell -- test-rust
devenv shell -- tests
```

You rely on, and will extend (all in `src/tyo3/graph/graph.py` unless noted):

- `CodeGraph.build` (`:117`) and its six pass helpers — the single node/edge
  implementation Phase 6 factors and reuses.
- `_file_to_nodes` (`:93`), `_file_to_edges` (`:94`), `_id_to_index` (`:92`),
  `_name_prefix_index` (`:103`), `_file_node_ranges` (`:99`),
  `_semantic_subgraph_cache` (`:113`) — the secondary indexes.
- `_rebuild_indexes` (`:925`) — reconstructs every secondary index from graph
  state after a bulk `remove_nodes_from`; Phase 6 extends it to also rebuild the
  reverse-dependency index.
- `_add_import_edge` (`:524`) — the single site IMPORTS edges are created; Phase 6
  makes it maintain the reverse index incrementally.
- `_resolve_references_via_occurrences` (`:411`) — gains an optional
  `restrict_targets` filter for inbound revalidation.
- `rebuild` (`:1373`) — kept as the full-rebuild fallback (regression guard for
  `test_graph_update.py`); `apply_delta` is the new incremental path.
- `SyncResult` (`models/analysis.py:82`) — the delta DTO (`revision`, `created`,
  `changed`, `deleted`, `project_changed`, `custom_stdlib_changed`, `rescan`).
- `Snapshot` / `TyO3Session` read surface (`session.py`) — `apply_delta`'s `source`.

`test_graph_update.py` calls `graph.rebuild(session, path)` — **leave `rebuild`
intact**. Phase 6 adds `apply_delta` and its own tests; it does not delete
`rebuild` (architecture §10.6 "retire the full-rebuild stub" means *stop using it
as the incremental path*, not *delete the method that the rescan branch and
existing tests still need*).

---

## 2. Graph state additions (`graph/graph.py`)

### 2.1 Store the root and the reverse-dependency index

Add two fields in `__init__` (`graph.py:88`). The reverse index maps a **graph
path** to the set of graph paths that import it:

```python
def __init__(self) -> None:
    self._graph: rx.PyDiGraph = rx.PyDiGraph()

    # Secondary indexes
    self._id_to_index: dict[str, int] = {}
    self._file_to_nodes: dict[str, list[int]] = defaultdict(list)
    self._file_to_edges: dict[str, list[int]] = defaultdict(list)

    # Reverse-dependency index (Phase 6): target_file -> {source_files that import it}.
    # Maintained in _add_import_edge (incremental) and rebuilt authoritatively in
    # _rebuild_indexes. Drives inbound edge revalidation in apply_delta without a
    # global rescan (architecture §5.1).
    self._file_importers: dict[str, set[str]] = defaultdict(set)

    # The resolved project root, set at build() time. apply_delta needs it to map
    # the delta's absolute paths to project-relative graph paths, and `source` may
    # be a Snapshot (no .root). None until build() runs.
    self._root: Path | None = None

    # ... rest unchanged (range cache, name prefix index, diagnostics, caches) ...
```

> `defaultdict(set)` mirrors the existing `_file_to_nodes` style. Keep it a plain
> dict-of-sets; do not over-engineer.

### 2.2 Record the root in `build`

In `build` (`graph.py:139`), `root_resolved` is already computed (`:141`). Store it
on the instance so incremental updates can reuse it:

```python
graph = cls()
root = session.root
root_resolved = root.resolve()
graph._root = root_resolved          # ← add: apply_delta reads this later
native_paths = [str(p) for p in session.files()]
# ... unchanged ...
```

That is the only change to `build`. Everything else (the six passes) is reused
verbatim by `apply_delta` through the factored `_index_files` (§4).

### 2.3 Maintain `_file_importers` in `_add_import_edge`

`_add_import_edge` (`graph.py:524`) is the *single* place IMPORTS edges are made.
After it successfully adds the edge, record the reverse dependency. Both arguments
are already graph paths (`source_file`, `target_file`):

```python
def _add_import_edge(self, source_file, target_file, range, project_files) -> None:
    # ... existing body that resolves source_module / target_module and calls
    #     self._add_edge(source_module, target_module, EdgeData(IMPORTS...), source_file)
    #     unchanged ...

    # Phase 6: reverse-dependency bookkeeping. Only record project→project imports;
    # external targets (package::<module>) are never queried as dirty files.
    if target_file in project_files and target_file != source_file:
        self._file_importers[target_file].add(source_file)
```

> Record it keyed by `target_file` (the imported file) → `source_file` (the
> importer). Guard on `target_file in project_files` so external-package targets
> (which get a `package::<module>` stub) do not pollute the index — we only ever
> ask "which project files import dirty project file C?".

### 2.4 Rebuild `_file_importers` in `_rebuild_indexes`

`_rebuild_indexes` (`graph.py:925`) already reconstructs every secondary index
after a bulk removal. Extend it to authoritatively rebuild the reverse index from
the surviving IMPORTS edges, so the index is correct after `remove_nodes_from`:

```python
def _rebuild_indexes(self) -> None:
    self._id_to_index.clear()
    self._file_to_nodes.clear()
    self._file_to_edges.clear()
    self._file_node_ranges.clear()
    self._name_prefix_index.clear()
    self._semantic_subgraph_cache.clear()
    self._file_importers.clear()          # ← add
    # ... existing node loop (rebuild _id_to_index / _file_to_nodes / name index) ...
    # ... existing edge loop (rebuild _file_to_edges) ...
    #     extend the edge loop to also rebuild _file_importers from IMPORTS edges:
    for edge_idx in self._graph.edge_indices():
        data = self._graph.get_edge_data_by_index(edge_idx)
        if data is None:
            continue
        if data.file is not None:
            self._file_to_edges[data.file].append(edge_idx)
        if data.kind == EdgeKind.IMPORTS:
            src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
            src_file = self._graph[src].file
            tgt_file = self._graph[tgt].file
            if (
                tgt_file != "<external>"
                and src_file != "<external>"
                and tgt_file != src_file
            ):
                self._file_importers[tgt_file].add(src_file)
    # ... existing range-cache rebuild loop unchanged ...
```

> Merge this into the *existing* `_file_to_edges` edge loop rather than adding a
> second pass over `edge_indices()`. The module-node `file` attribute is the graph
> path (e.g. `"app.py"`), so `src_file` / `tgt_file` are exactly the keys
> `_importers_of` expects. External stub modules have `file == "<external>"` and are
> excluded.

### 2.5 The reverse-dependency query

```python
def _importers_of(self, files: set[str]) -> set[str]:
    """Graph paths of project files that import any file in *files*.

    Reads the reverse-dependency index built from IMPORTS edges. Used by
    apply_delta to find inbound cross-file edges that must be revalidated when
    *files* (the dirty set) change. Excludes the dirty files themselves — a file's
    own out-edges are rebuilt by re-indexing it, not by inbound revalidation.
    """
    importers: set[str] = set()
    for f in files:
        importers |= self._file_importers.get(f, set())
    return importers - files
```

---

## 3. `restrict_targets` on reference resolution (`graph/graph.py`)

Inbound revalidation re-runs an importer's reference resolution but must add **only
the edges whose target is in the dirty set** (the importer's surviving edges into
non-dirty files were not removed and must not be duplicated). Add an optional
`restrict_targets` filter to `_resolve_references_via_occurrences` (`graph.py:411`).
It is `None` for the normal full pass (build / re-index) and a set of graph paths
for inbound revalidation.

Locate the per-occurrence loop body (`graph.py:459` onward). After `target_file` is
normalized (`graph.py:464`) and **before** any node-creation / edge-adding work,
short-circuit when restricting:

```python
def _resolve_references_via_occurrences(
    self,
    session,
    file_str,
    project_files,
    *,
    report=None,
    root=None,
    native_by_graph=None,
    restrict_targets: set[str] | None = None,   # ← add
) -> None:
    # ... unchanged setup + file_occurrences() call + except/fallback ...
    for occ in occurrences:
        if occ.target_file is None or occ.target_name is None:
            continue
        target_file_raw = occ.target_file
        target_file = (
            _normalize_result_path(root, target_file_raw, project_files)
            if root is not None else target_file_raw
        )
        # Phase 6 inbound revalidation: only (re-)add edges INTO the dirty set.
        if restrict_targets is not None and target_file not in restrict_targets:
            continue
        # ... rest of the loop UNCHANGED (build target_sid, ensure node,
        #     find enclosing symbol, add REFERENCES edge, add_import_edge) ...
```

> Place the `restrict_targets` guard right after `target_file` is computed, so it
> covers both the REFERENCES edge and the `_add_import_edge` call at the bottom of
> the loop body (`graph.py:521`). One guard, both edge kinds filtered.

The fallback path `_resolve_references_via_tokens` (`graph.py:599`) does **not**
need the filter for Phase 6: inbound revalidation always goes through the
occurrences API (the importer file exists and is analysable; the token fallback is
only for files where `file_occurrences` itself throws, which is already logged and
rare). If you want symmetry you may thread `restrict_targets` through it too, but it
is optional — note the asymmetry in the PR rather than over-building.

---

## 4. The per-file indexing pass `_index_files` (`graph/graph.py`)

`build` runs passes 1–5 globally so that all nodes exist before any reference
resolves (cross-file targets must be present). For incremental, re-indexing **N
dirty files that may reference each other** has the same requirement, so we cannot
just loop a monolithic "index one file" helper — we must keep the *sub-pass*
ordering across the set. Factor it once:

```python
def _index_files(
    self,
    session,
    graph_paths: list[str],
    project_files: set[str],
    *,
    root: Path,
    native_by_graph: dict[str, str],
    report: GraphBuildReport | None = None,
) -> None:
    """Run passes 1–5 over *graph_paths* (a subset of the project).

    Mirrors CodeGraph.build's pass ordering for a subset: collect symbols for all
    of them, materialize all their nodes, then structural edges + range caches,
    then references, then inheritance. Keeping the sub-pass split (rather than a
    per-file loop) is what lets two simultaneously-changed files that reference
    each other resolve correctly — every dirty node exists before any reference is
    resolved. Targets in *non*-dirty files already exist in the graph (they were
    never removed), so cross-file references out of the dirty set resolve too.
    """
    symbols_by_file: dict[str, list[Symbol]] = {}
    for graph_path in graph_paths:
        native_path = native_by_graph.get(graph_path, graph_path)
        symbols = self._collect_symbols_for_file(session, native_path, report=report)
        if symbols is not None:
            symbols_by_file[graph_path] = symbols

    for file_str, symbols in symbols_by_file.items():
        self._materialize_file_nodes(file_str, symbols)

    for file_str, symbols in symbols_by_file.items():
        self._add_containment_edges_for_file(file_str, symbols)
        self._build_range_cache_for_file(file_str)

    for file_str in symbols_by_file:
        self._resolve_references_via_occurrences(
            session, file_str, project_files,
            report=report, root=root, native_by_graph=native_by_graph,
        )

    for file_str, symbols in symbols_by_file.items():
        self._resolve_inheritance(
            session, file_str, symbols,
            report=report, root=root, native_by_graph=native_by_graph,
            project_files=project_files,
        )
```

This is a *pure extraction* of `build`'s passes 1–5 (`graph.py:150-191`) over a
subset. **Optional, recommended refactor:** have `build` call `_index_files(graph,
graph_paths, ...)` for passes 1–5 so there is literally one implementation; if that
ripples too much for one PR, leaving `build`'s inline passes and `_index_files` as a
faithful copy is acceptable — but then a parity test (§6) is doubly important to
catch drift. State which you chose in the PR.

> Pass 6 (diagnostics) is deliberately **not** in `_index_files`: diagnostics come
> from a single project-wide `check()` and are handled separately by
> `_refresh_diagnostics` (§5.3), because a change in one file can alter diagnostics
> in *other* files.

---

## 5. `apply_delta` (`graph/graph.py`)

The new public entry point, placed in the "Incremental updates" section beside
`rebuild` (`graph.py:1371`).

### 5.1 The method

```python
def apply_delta(
    self,
    source,
    delta: SyncResult,
    *,
    report: GraphBuildReport | None = None,
) -> None:
    """Incrementally update the HEAD graph from a write's SyncResult.

    *source* is a consistent read surface for ``delta.revision`` — pass the
    ``Snapshot`` pinned at that revision (``session.snapshot()`` right after the
    edit) for true MVCC consistency; a ``TyO3Session`` also works ("latest", fine
    because nothing mutates HEAD during one apply_delta).

    Drops nodes owned by changed+deleted files, re-indexes created+changed files,
    and revalidates inbound cross-file edges into the changed set. On a rescan
    (``delta.rescan``) the delta is unknown, so it full-rebuilds. The result is
    structurally equal to ``CodeGraph.build(source)`` over the same revision.

    Requires ``self._root`` (set by build). Build the graph once with
    ``CodeGraph.build`` before applying deltas.
    """
    if self._root is None:
        raise RuntimeError("apply_delta requires a graph built via CodeGraph.build()")

    # Rescan: delta unknown ⇒ rebuild wholesale (architecture §5.1).
    if delta.rescan:
        self._replace_with(CodeGraph.build(source, report=report))
        return

    root = self._root
    # Project-wide path maps from the CURRENT file set (includes created files,
    # excludes deleted ones). Native (absolute) paths feed read calls; graph paths
    # are project-relative. Mirrors build (graph.py:142-146).
    native_paths = [str(p) for p in source.files()]
    native_by_graph = {_to_relative(root, p): p for p in native_paths}
    project_files = set(native_by_graph.keys())

    created = {_to_relative(root, p) for p in delta.created}
    changed = {_to_relative(root, p) for p in delta.changed}
    deleted = {_to_relative(root, p) for p in delta.deleted}

    dirty = changed | deleted               # nodes to drop
    to_index = sorted(created | changed)    # files to (re-)extract

    # 0. Snapshot inbound dependencies BEFORE removal — step 1 deletes the edges
    #    that encode them (they are incident to dirty nodes).
    importers = self._importers_of(dirty)
    importers -= set(to_index)              # re-indexed files rebuild their own out-edges
    importers &= project_files              # only files that still exist

    # 1. Drop every node owned by a dirty file. rustworkx removes incident edges
    #    (including inbound cross-file edges) automatically.
    doomed = [idx for f in dirty for idx in self._file_to_nodes.get(f, [])]
    if doomed:
        self._graph.remove_nodes_from(doomed)
    # swap-and-pop invalidated stored indices: rebuild every secondary index
    # (incl. _file_importers) from the surviving graph.
    self._rebuild_indexes()

    # 2. Re-extract created + changed files (passes 1–5).
    self._index_files(
        source, to_index, project_files,
        root=root, native_by_graph=native_by_graph, report=report,
    )

    # 3. Revalidate INBOUND edges: re-resolve each importer's references INTO the
    #    dirty set only (its edges into non-dirty files survived step 1 untouched).
    for importer in sorted(importers):
        self._resolve_references_via_occurrences(
            source, importer, project_files,
            report=report, root=root, native_by_graph=native_by_graph,
            restrict_targets=dirty,
        )

    # 4. Diagnostics: one project-wide check(), redistributed. A change in one file
    #    can alter diagnostics in others, so refresh wholesale (architecture §5 the
    #    snapshot's check() is the same revision as the structural update).
    self._refresh_diagnostics(source, root=root, project_files=project_files)

    # Drop stale memoized subgraphs (defensive; _add_* already clear it).
    self._semantic_subgraph_cache.clear()
```

> `delta` is the validated `SyncResult` pydantic model (what `session.edit(...)`
> returns), so `delta.created` etc. are `list[str]`. If a caller hands the raw
> native dict instead, `SyncResult.model_validate(...)` it first — keep
> `apply_delta` typed on the model.

### 5.2 `_replace_with`

`rebuild` (`graph.py:1380`) already does the in-place swap; extract it so both
`rebuild` and the rescan branch share it:

```python
def _replace_with(self, fresh: CodeGraph) -> None:
    """Replace all internal state with *fresh*'s (used by rescan / rebuild)."""
    self.__dict__.update(fresh.__dict__)
```

Then simplify `rebuild` to use it (behaviour unchanged — full rebuild, `path`
ignored, existing tests still green):

```python
def rebuild(self, session: TyO3Session, path: str) -> None:
    """Full rebuild (the rescan fallback). *path* is accepted for API
    compatibility but unused; prefer apply_delta for incremental updates."""
    self._replace_with(CodeGraph.build(session))
```

### 5.3 `_refresh_diagnostics`

`_collect_all_diagnostics` (`graph.py:327`) appends into `self._diagnostics`.
Wrap it so apply_delta gets a clean redistribution (clear, then collect), without
changing build's call:

```python
def _refresh_diagnostics(
    self,
    session,
    *,
    root: Path | None = None,
    project_files: set[str] | None = None,
) -> None:
    """Clear and recompute all diagnostics from one project-wide check().

    apply_delta uses this instead of touching only dirty files: a change in one
    file can add/remove diagnostics in importers and dependents, so a partial
    update would be incorrect. One check() over the pinned source is consistent
    with the structural update's revision.
    """
    self._diagnostics.clear()
    self._collect_all_diagnostics(
        session, root=root, project_files=project_files
    )
```

> If you want to avoid the full `check()` cost on every tiny edit, that is a Phase
> 10 optimisation (e.g. diff diagnostics by revision). For Phase 6, correctness ==
> rebuild is the gate, and `check()` is one FFI call the snapshot needs anyway.

---

## 6. Optional: a live `session.graph` (recommended, but separable)

Architecture §6 lists `session.graph` as "the live HEAD graph (updates
incrementally as syncs land)". A minimal, honest version wires `apply_delta` to the
session's write methods. **This is optional for Phase 6** — the load-bearing
deliverable is `apply_delta` itself. If you include it, keep it thin and lazy:

```python
# session.py — on TyO3Session
@property
def graph(self) -> CodeGraph:
    """The live HEAD CodeGraph, built lazily on first access and updated
    incrementally as edits land. Reads through a snapshot pinned at the current
    revision for consistency."""
    self._check_open()
    if self._graph is None:
        from tyo3.graph import CodeGraph
        with self.snapshot() as snap:
            self._graph = CodeGraph.build(snap)
    return self._graph

def _apply_graph_delta(self, sync: SyncResult) -> None:
    """If a graph has been materialized, fold the write's delta into it."""
    if self._graph is None:
        return
    with self.snapshot() as snap:          # pinned at sync.revision
        self._graph.apply_delta(snap, sync)
```

Then have each write method, **after** producing its `SyncResult` and **after**
`_invalidate_head_snap()`, call `self._apply_graph_delta(result)` before returning.
Initialise `self._graph = None` in `__init__`.

> Two cautions if you wire this:
> 1. **Import cycle.** `graph/graph.py` imports `from tyo3.session import
>    TyO3Session` (`graph.py:25`). Import `CodeGraph` lazily *inside* the property
>    (as above) to avoid a circular import at module load.
> 2. **Cost.** Building the graph on first `.graph` access is O(project); each
>    subsequent edit pays one `apply_delta` (incremental) + one snapshot build. If a
>    user never touches `.graph`, they pay nothing (lazy). Document that `.graph` is
>    opt-in.
>
> If wiring `session.graph` risks ballooning the PR or destabilising the write-path
> tests, **defer it** and ship `apply_delta` as a standalone graph method that
> callers drive explicitly (as the tests in §7 do). The architecture's `session.graph`
> is then a trivial follow-up. Decide and state it in the PR; do not half-wire it.

---

## 7. Tests (`src/tyo3/tests/test_graph_incremental.py`)

The graph is pure Python, so all Phase 6 tests are Python. The **load-bearing**
test is structural parity: `apply_delta` == `build` over the post-edit revision.
Use the existing fixtures (`fixtures/`, used by `test_graph_update.py`) and helpers
(`tyo3/tests/graph_helpers.py` — `find_one`, `edges_of_kind`).

### 7.1 Parity helpers

```python
from __future__ import annotations

from tyo3.graph import CodeGraph, EdgeKind
from tyo3.tests.graph_helpers import edges_of_kind


def _node_ids(g: CodeGraph) -> set[str]:
    return {g.graph[i].symbol_id for i in g.graph.node_indices()}


def _edge_triples(g: CodeGraph) -> set[tuple[str, str, str]]:
    """(source_id, target_id, edge_kind) for every edge — order-independent."""
    out: set[tuple[str, str, str]] = set()
    for ei in g.graph.edge_indices():
        data = g.graph.get_edge_data_by_index(ei)
        s, t = g.graph.get_edge_endpoints_by_index(ei)
        out.add((g.graph[s].symbol_id, g.graph[t].symbol_id, str(data.kind)))
    return out


def _assert_structurally_equal(a: CodeGraph, b: CodeGraph) -> None:
    assert _node_ids(a) == _node_ids(b), (
        f"node id mismatch\nonly in delta: {_node_ids(a) - _node_ids(b)}\n"
        f"only in rebuild: {_node_ids(b) - _node_ids(a)}"
    )
    assert _edge_triples(a) == _edge_triples(b), (
        f"edge mismatch\nonly in delta: {_edge_triples(a) - _edge_triples(b)}\n"
        f"only in rebuild: {_edge_triples(b) - _edge_triples(a)}"
    )
```

> Compare **node ids and (src,tgt,kind) edge triples**, not rustworkx node indices
> (swap-and-pop makes indices implementation details). Parallel edges of different
> kinds are distinct triples; identical-kind parallel edges collapse in a set — if
> the suite cares about parallel-edge multiplicity, compare a `Counter` of triples
> instead, but start with the set (matches how `edges_of_kind` reasons).

### 7.2 The core parity tests

Each test: build a graph, perform a write through the session, `apply_delta` the
returned `SyncResult` via a pinned snapshot, then assert structural equality with a
fresh `build` over the *same* post-edit revision.

```python
import textwrap
from tyo3 import TyO3Session


def _build(session):
    with session.snapshot() as snap:
        return CodeGraph.build(snap)


def test_apply_delta_changed_file_equals_rebuild(tmp_path):
    (tmp_path / "models.py").write_text("class User:\n    def save(self): ...\n")
    (tmp_path / "app.py").write_text(
        "from models import User\n\n\ndef run():\n    return User().save()\n"
    )
    with TyO3Session(str(tmp_path)) as s:
        g = _build(s)
        # change models.py: add a method (new node + edges; importers unaffected
        # structurally except their refs still resolve).
        sync = s.edit("models.py", "class User:\n    def save(self): ...\n    def load(self): ...\n")
        with s.snapshot() as snap:
            g.apply_delta(snap, sync)
            rebuilt = CodeGraph.build(snap)
        _assert_structurally_equal(g, rebuilt)


def test_apply_delta_created_file_equals_rebuild(tmp_path):
    (tmp_path / "app.py").write_text("X = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        g = _build(s)
        sync = s.edit("helpers.py", "def helper():\n    return 42\n")  # Created
        assert sync.created, "expected a created file in the delta"
        with s.snapshot() as snap:
            g.apply_delta(snap, sync)
            rebuilt = CodeGraph.build(snap)
        _assert_structurally_equal(g, rebuilt)


def test_apply_delta_deleted_file_equals_rebuild(tmp_path):
    (tmp_path / "models.py").write_text("class User: ...\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = _build(s)
        # delete app.py from disk, then ingest the deletion.
        (tmp_path / "app.py").unlink()
        sync = s.sync_path("app.py")
        assert sync.deleted, "expected a deleted file in the delta"
        with s.snapshot() as snap:
            g.apply_delta(snap, sync)
            rebuilt = CodeGraph.build(snap)
        _assert_structurally_equal(g, rebuilt)


def test_apply_delta_revalidates_inbound_cross_file_edges(tmp_path):
    """Changing models.py must keep app.py's references INTO models.py correct."""
    (tmp_path / "models.py").write_text("class User:\n    def save(self): ...\n")
    (tmp_path / "app.py").write_text(
        "from models import User\n\n\ndef run():\n    return User().save()\n"
    )
    with TyO3Session(str(tmp_path)) as s:
        g = _build(s)
        sync = s.edit("models.py", "class User:\n    def save(self): ...\n    def extra(self): ...\n")
        with s.snapshot() as snap:
            g.apply_delta(snap, sync)
            rebuilt = CodeGraph.build(snap)
        _assert_structurally_equal(g, rebuilt)
        # explicit: app.py still imports models.py after the change
        assert ("app.py", "models.py") in {
            (a.split("::")[0], b.split("::")[0]) for a, b in edges_of_kind(g, EdgeKind.IMPORTS)
        }


def test_apply_delta_rescan_equals_rebuild(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        g = _build(s)
        sync = s.sync_all()
        assert sync.rescan
        with s.snapshot() as snap:
            g.apply_delta(snap, sync)
            rebuilt = CodeGraph.build(snap)
        _assert_structurally_equal(g, rebuilt)
```

### 7.3 Reverse-dependency index unit tests

```python
def test_importers_index_populated_after_build(tmp_path):
    (tmp_path / "models.py").write_text("class User: ...\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = _build(s)
        assert "app.py" in g._importers_of({"models.py"})
        assert g._importers_of({"models.py"}) == g._file_importers.get("models.py", set())


def test_importers_index_survives_apply_delta(tmp_path):
    (tmp_path / "models.py").write_text("class User: ...\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = _build(s)
        sync = s.edit("models.py", "class User:\n    name: str\n")
        with s.snapshot() as snap:
            g.apply_delta(snap, sync)
        assert "app.py" in g._importers_of({"models.py"})  # rebuilt, not lost
```

### 7.4 Idempotence / sequence

```python
def test_apply_delta_sequence_matches_rebuild(tmp_path):
    (tmp_path / "models.py").write_text("class User: ...\n")
    (tmp_path / "app.py").write_text("from models import User\nu = User()\n")
    with TyO3Session(str(tmp_path)) as s:
        g = _build(s)
        for text in (
            "class User:\n    a: int\n",
            "class User:\n    a: int\n    b: int\n",
            "class User:\n    b: int\n",
        ):
            sync = s.edit("models.py", text)
            with s.snapshot() as snap:
                g.apply_delta(snap, sync)
        with s.snapshot() as snap:
            rebuilt = CodeGraph.build(snap)
        _assert_structurally_equal(g, rebuilt)
```

> If a parity test fails, the assertion message names the exact node/edge
> difference — that is the debugging surface. Common causes: forgot
> `_rebuild_indexes` after removal (stale indices), forgot to snapshot `importers`
> before removal (inbound edges lost), or `restrict_targets` duplicating surviving
> edges (missing/incorrect guard). See §9.

---

## 8. Build, test, iterate

```bash
devenv shell -- tests          # FULL Python suite — rebuild still green + new parity suite
devenv shell -- pytest src/tyo3/tests/test_graph_incremental.py -q
```

No Rust changes in Phase 6 (the graph is Python), so `check-rust` / `test-rust`
should be untouched — run them once to confirm you did not accidentally edit Rust:

```bash
devenv shell -- check-rust
devenv shell -- test-rust
```

The full suite passing proves `apply_delta` is additive and `rebuild`'s behaviour
(and `test_graph_update.py`) is unchanged. The new parity suite proves the
incremental path equals the rebuild path — the architecture §11 gate.

---

## 9. Gotchas & decisions (read before you debug)

1. **Snapshot `importers` BEFORE removal (§0.2/§5.1 step 0).** `remove_nodes_from`
   deletes inbound `X→C` edges (they are incident to dirty node C). If you query
   `_importers_of(dirty)` *after* removal, it returns nothing and inbound edges are
   never revalidated. Compute it first.

2. **`_rebuild_indexes` is mandatory after `remove_nodes_from`.** rustworkx
   swap-and-pop renumbers node indices, invalidating every `*_to_index` /
   `_to_nodes` / `_to_edges` entry (the method's own docstring, `graph.py:925`,
   says so). Re-index *before* `_index_files`, or new nodes collide with stale
   indices.

3. **`restrict_targets` filters BOTH edge kinds.** Place the guard right after
   `target_file` is normalized (§3) so it covers the REFERENCES edge *and* the
   `_add_import_edge` call at the bottom of the occurrence loop. Miss it and inbound
   revalidation re-adds the importer's surviving edges into non-dirty files →
   duplicate parallel edges → parity test fails with "only in delta".

4. **Inbound revalidation only re-adds, never removes.** Step 1 already removed all
   importer→dirty edges (incident to dirty nodes). So step 3 with
   `restrict_targets=dirty` purely re-creates them; there is no removal to do and no
   duplication risk for dirty targets. Do not also try to clear the importer's edges
   first — that would wrongly drop its edges into *non*-dirty files.

5. **Paths: delta is absolute, graph is relative (§0.4).** Convert every delta path
   with `_to_relative(self._root, p)`. Read calls (`document_symbols`,
   `file_occurrences`, …) take **native/absolute** paths — use `native_by_graph`
   (rebuilt from `source.files()`), exactly as `build` does.

6. **`self._root` must be set.** Only `build` sets it; `apply_delta` raises if it is
   `None`. Always `CodeGraph.build(...)` first, then `apply_delta`. A bare
   `CodeGraph()` + `apply_delta` is unsupported (the guard makes this explicit).

7. **Created files that satisfy previously-external references are a known
   approximation.** A file `X` that already did `import newmod` resolved `newmod` to
   an *external stub* before `newmod` existed. Creating `newmod` does not, by
   itself, make `apply_delta` re-point X's edge to the new project node — X is not in
   `_importers_of(created)` because the old edge targeted a stub, not the (nonexistent)
   project file. This follows architecture §5.1 (`dirty = changed ∪ deleted` for
   inbound). The common case (edit/delete an existing file) is exact; the rare
   new-module-satisfies-old-import case converges on the next `sync_all`/rebuild.
   Document it; do not special-case it in Phase 6.

8. **Diagnostics refresh is wholesale, on purpose (§5.3).** A change in one file can
   add/remove diagnostics in importers. A per-dirty-file diagnostics update would be
   incorrect. One `check()` + redistribute matches `build` and is consistent with
   the pinned revision. Per-revision diagnostic diffing is a Phase 10 optimisation.

9. **Orphaned external stubs are harmless.** Removing a changed file can leave an
   external stub node with zero edges. Pruning it is *optional* cleanup — but note a
   rebuild also keeps stubs only if something references them, so for strict parity
   you may need to prune zero-degree external stubs after step 3, OR re-create them
   on re-index (they are re-created by `_ensure_target_node*` whenever the changed
   file still references them). In practice the parity test will tell you: if it
   flags an "only in delta" external stub, add a small prune of zero-in-degree
   `external` nodes at the end of `apply_delta`. Keep the prune scoped to external
   nodes only.

10. **Keep `rebuild` (§5.2).** It is the rescan fallback and the subject of
    `test_graph_update.py`. `apply_delta` is *additive*. "Retire the full-rebuild
    stub" (architecture §10.6) means stop using full-rebuild as the *incremental*
    path, not delete the method.

11. **Lazy import to dodge the cycle (§6).** `graph/graph.py` imports
    `TyO3Session`; if you wire `session.graph`, import `CodeGraph` lazily inside the
    property/helper, never at `session.py` module top.

12. **Pass a Snapshot, not a session, in tests.** Snapshots pin one revision so the
    six passes are mutually consistent; a session floats. Both are accepted by
    `apply_delta`, but tests must use the snapshot to make parity deterministic.

---

## 10. Explicitly OUT of scope for Phase 6

- **Pinned graph snapshots** — `Snapshot.graph()` copy-on-pin and cross-revision
  `graph.diff` (**Phase 7**). Phase 6 is the *mutable HEAD* graph only. Do not add a
  `.graph()` method to `PySnapshot`/`Snapshot` or any revision-tagged graph.
- **The file watcher** feeding deltas (`poll_changes` → `apply_delta`) — **Phase 8**.
- **The floating warm `session.check()` fast path** — **Phase 9**.
- **Benchmarks** — single-file-edit `apply_delta` vs full rebuild timing, memory,
  per-revision diagnostic diffing — **Phase 10**. Phase 6 proves *correctness ==
  rebuild*, not speed (though it should be obviously faster for a 1-file edit in a
  large project, do not gate on a timing threshold).
- **Notebook / virtual-buffer graph nodes** — the graph indexes system-path Python
  files as today; `edit_virtual` deltas are not graphed in Phase 6.
- **Fixing `name@line` identity instability** — inherent to anonymous symbols;
  `apply_delta` stays correct via drop+recreate (§0.3).
- **Auto-wiring `session.graph`** is *optional* (§6); if it threatens scope, ship
  `apply_delta` standalone and defer the property.

If you find yourself adding a `graph()` to a snapshot, implementing `graph.diff`,
wiring `ProjectWatcher`, or writing timing assertions, stop — you have left Phase 6.

---

## 11. Definition of Done

- [ ] `graph.py`: `CodeGraph.__init__` gains `_file_importers: dict[str, set[str]]`
      and `_root: Path | None`.
- [ ] `build` sets `self._root = root_resolved` (no other build change required).
- [ ] `_add_import_edge` records `_file_importers[target_file].add(source_file)` for
      project→project imports.
- [ ] `_rebuild_indexes` clears and rebuilds `_file_importers` from surviving
      IMPORTS edges (merged into the existing edge loop).
- [ ] `_importers_of(files)` returns importing project files, minus the input set.
- [ ] `_resolve_references_via_occurrences` gains `restrict_targets: set[str] |
      None`, guarding both the REFERENCES edge and `_add_import_edge`.
- [ ] `_index_files(source, graph_paths, project_files, …)` factored from build's
      passes 1–5 and reused (optionally also by `build`).
- [ ] `_refresh_diagnostics` clears + recomputes diagnostics from one `check()`.
- [ ] `_replace_with(fresh)` added; `rebuild` simplified to call it (behaviour
      unchanged; `test_graph_update.py` still green).
- [ ] `apply_delta(source, delta, *, report=None)` implemented: rescan branch;
      snapshot importers before removal; `remove_nodes_from` + `_rebuild_indexes`;
      `_index_files(created∪changed)`; inbound revalidation with
      `restrict_targets=dirty`; `_refresh_diagnostics`.
- [ ] (Optional, stated in PR) `session.graph` property + `_apply_graph_delta`
      wired into the write methods with a lazy `CodeGraph` import — or explicitly
      deferred.
- [ ] New suite `test_graph_incremental.py` passes: changed / created / deleted /
      inbound-cross-file / rescan parity == rebuild; reverse-index populated and
      survives apply_delta; multi-edit sequence parity.
- [ ] `devenv shell -- tests` shows **no regressions** (existing graph tests,
      write-path tests, snapshot tests unchanged).
- [ ] `devenv shell -- check-rust` / `test-rust` unchanged (no Rust edits).
- [ ] PR notes: whether `build` was refactored onto `_index_files` or kept inline;
      whether `session.graph` was wired or deferred; whether zero-degree external
      stub pruning was needed for parity.

---

## 12. How this seeds Phase 7+

Phase 6 makes the HEAD graph a first-class incremental layer keyed to the same
`SyncResult` the db write path already produces. That is precisely the substrate
Phase 7 builds on:

- **Phase 7** adds `Snapshot.graph()` as a **copy-on-pin** `PyDiGraph.copy()` of the
  HEAD graph as of the snapshot's revision (architecture §5.2), giving a consistent
  `snap.check()` + `snap.graph()` pair. The content-addressed node ids Phase 6
  preserves are what make cross-revision `before.graph().diff(after.graph())`
  meaningful ("what edges did my edit add/remove?"). `apply_delta`'s minimal,
  identity-stable updates are the reason that diff is small and legible.
- **Phase 8** routes watcher events through the same `sync_path` → `SyncResult` →
  `apply_delta` pipeline; no new graph code, just a new change source.
- **Phase 10** benchmarks single-file `apply_delta` vs full rebuild and characterises
  the reverse-dep upkeep cost (architecture §9) — the honest tradeoff Phase 6
  deliberately pays to avoid global rescans.

Keep the delta path identity-stable, the reverse index rebuilt on every structural
change, and `apply_delta == build` enforced in CI, and Phase 7's pinned graph
snapshots drop straight onto this HEAD layer.
