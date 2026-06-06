# Gate 3N — Quick Implementation Report

> **Status:** partial, **unbuilt and unvalidated**. This was a time-boxed
> "maximum code, defer hardening" pass against
> `GATE_3N_NATIVE_CODE_DELTA_GUIDE.md`. No `devenv shell -- build`, no tests run.
> Treat every Rust line as needing a compile pass and every parity claim as
> unproven. This document is the handoff for the review/harden phase.

## What this gate is (one line)

Move code-layer **delta production** (node/edge materialisation for the dirty
set) out of the post-commit, lock-free Python read-surface pass and into the
**native commit**, under the single write lock, emitting a `CodeDelta` that the
Python `CodeGraph` applies **purely**. rustworkx + all graph *algorithms* stay
in Python; only structural state + delta move to Rust.

---

## Implemented

### Step 0 — wire contract + pure Python applier  ✅ (code written)

| Artifact | File | Notes |
|---|---|---|
| Rust DTO | `rust/src/dto/code_delta.rs` (new) | `SymbolNodeDto`, `EdgeDto`, `CodeDelta` (serde). Field names mirror the Python Pydantic models 1:1 so pythonize round-trips by name. |
| DTO registration | `rust/src/dto/mod.rs` | `mod code_delta; pub use code_delta::*;` |
| SyncResult field | `rust/src/dto/sync.rs` | `#[serde(default)] pub code_delta: Option<CodeDelta>` — old callers/tests unaffected. |
| All initializers | `rust/src/project.rs` | 5 `SyncResultDto { … }` sites set `code_delta: None` (then the commit path overrides). |
| Python mirrors | `src/tyo3/models/analysis.py` | `SymbolNodeDelta`, `EdgeDelta`, `CodeDelta`; `SyncResult.code_delta: CodeDelta | None`. Added to `__all__`. |
| Pure applier | `src/tyo3/graph/graph.py` | `CodeGraph.apply_code_delta()` + helpers. |
| Unit tests | `src/tyo3/graph/tests/test_code_delta_apply.py` (new) | Round-trip a hand-built delta; moved-entity-no-edge-churn. |

**`apply_code_delta` semantics (the function every later step feeds):**
- `rescan == True` → `_clear_all()` then apply as a full build.
- `nodes_removed` → `remove_nodes_from` (rustworkx drops incident edges) +
  `_rebuild_indexes()`.
- `edges_removed` → remove matching `(src, dst, kind)` via
  `remove_edge_from_index`, fix `_file_importers`.
- `nodes_upserted` → add new, or replace payload in place at the stable index
  (`_upsert_delta_node`), moving the node between `_file_to_nodes` buckets if the
  file changed.
- `nodes_moved` → `_move_delta_node`: `model_copy(update={file, range})` only —
  **no re-add, no edge churn** (§5.5.1 / §6.6).
- `edges_added` → `_add_delta_edge`, mapping the delta kind string to `EdgeKind`
  (`containment` → DEFINES from a module node else CONTAINS, matching
  `_add_containment_edges_for_file`), updating the reverse-dep (`_file_importers`)
  for IMPORTS edges.
- Sets `self._revision = delta.revision`.
- **Purity:** reads nothing from session/snapshot. This is the property Step 6/7
  depend on.

### Steps 1–4 — native CodeLayer + producer  ✅ (code written)

| Artifact | File | Notes |
|---|---|---|
| `CodeLayer` | `rust/src/code_layer.rs` (new; `mod code_layer;` in `lib.rs`) | `nodes: HashMap<DurableId,NodeData>`, `edges: HashSet<Edge>`, `reverse_deps`, `revision`. Methods: `importers_of`, `full_build`, `incremental`, `full_delta`, `kind_str`. Deterministic emission via `BTreeMap`/`BTreeSet` keyed `(file, qualified_name, kind)` for nodes and `Ord` for edges. |
| Entity payload | `rust/src/entity.rs` | `Entity` gains `file: String` + `range: RangeDto` (computed via `coordinates::range_to_dto_with_index`) so a node payload is built without a second pass. |
| Producer | `rust/src/project.rs::produce_full_code_delta` | Runs inside `commit_head`, in-lock, **after** identity reconciliation (so every `DurableId` is already reconciled in `head.registry`). |
| HeadState | `rust/src/project.rs` | `HeadState.code_layer: CodeLayer`, constructed in `build_head_with_config`. |

**Producer pipeline (full build):**
1. **Nodes (Step 1):** `extract_entities(state)`; per entity look up its
   reconciled `DurableId` via `registry.by_path(qualified_path)`; emit one
   **module** node per file (`<module>{rel}`, kind `module`, `content_hash =
   None`) and one entity node keyed by ULID. Paths are project-relative
   (`strip_prefix(root)`).
2. **Containment (Step 2):** top-level entity → module node; nested → enclosing
   entity's `DurableId` (via `registry.by_path(container)`); edge kind
   `containment`.
3. **References/imports (Step 3):** `convert::occurrences::convert_file_occurrences`
   per file (already-native resolver with alias resolution). Source = smallest
   enclosing entity range (`enclosing_id`) else module. Target resolved
   (`resolve_target`) by exact `registry.by_path` then a `(file_rel, leaf)` name
   index; unresolved → `<external>{file_rel}::{name}` stub node + edge.
   `reverse_deps` maintained for reference/import edges.
4. **Inheritance (Step 4, two-pass):** Pass I builds INHERITS for every class via
   `compute_supertypes` (mapping base `(file, name)` to a class `DurableId` or an
   external stub) and records an adjacency map. Pass II walks that adjacency via
   BFS to collect ancestor methods by name, then emits OVERRIDES for child
   methods whose name matches — only after Pass I is complete for the whole set.

The result is installed with `head.code_layer.full_build(nodes, edges, revision)`
and returned as the `CodeDelta`.

### Step 6 — apply path cutover  ✅ (partial; see deferred)

`src/tyo3/session.py::_apply_graph_delta` now consumes `result.code_delta`:
- `rescan` → apply wholesale (no ordering constraint).
- otherwise a **revision gate**: `delta.revision == g.revision + 1` or else drop
  `self._head_graph = None` (defensive lazy rebuild). The native commit is
  serialized so revisions are monotonic — concurrent partitioned writers either
  apply in order or trip the gate; **no Python write lock** is needed.
- `test_native_code_delta.py` (new): commit emits a `code_delta`; a graph driven
  by `apply_code_delta` equals a fresh `CodeGraph.build` rebuild
  (reuses the strengthened comparator `assert_graphs_equal`).

**Note on the "retire the lock" wording in the guide:** the interim
`threading.RLock` prerequisite was **never actually added** to this branch (the
uncommitted `session.py` change was the `_prime_identity_registry` priming
workaround, not a lock). So there was no lock to retire — the revision-gated
apply is the intended end state and is in place.

---

## Deferred (NOT done) — read before continuing

### Step 5 — true incremental producer  ⛔
`commit_head` currently calls `produce_full_code_delta` → emits a **full build as
`rescan` on every write**. This is *correct* (the replica rebuilds each commit)
but not the bounded incremental of §6.3:
- `CodeLayer::incremental(removed_ids, new_nodes, new_edges, moved, revision)` is
  **written but never called**.
- The dirty-set scoping, inbound revalidation via `importers_of`, and
  `nodes_moved` emission still need to be wired from the commit's
  created/changed/deleted/moved sets.
- Until then, every commit re-extracts and re-resolves the whole project — O(all
  files) per write. Functionally fine, performance-wrong.

### Step 7 — native snapshot code-layer + delete read-surface builder  ⛔
- Cold-start (`session.graph` first access) and `Snapshot.graph()` **still use the
  Python read-surface `CodeGraph.build`**. No `code_delta_full()` native accessor
  was added.
- The ~1,200–1,500 lines of dead read-surface code in `graph.py`
  (`_collect_symbols_for_file`, `_materialize_file_nodes`,
  `_resolve_references_via_occurrences`, `_inherits_pass_I`/`_overrides_pass_II`,
  `rebuild`, old `apply_delta`/`build`) are **still present and still used** —
  they remain the oracle, exactly as the guide intends until Step 6/7 close.
- **`_prime_identity_registry` is intentionally LEFT in place** (in
  `CodeGraph.build` and in `TyO3Session.snapshot`). Deleting it now — while the
  read-surface builder is still the cold/snapshot path — would starve that
  builder of `DurableId`s and break snapshot graphs. It can only go once Step 7
  makes snapshots native.

### Step 8 — determinism proof + cleanup + gate re-validation  ⛔
No byte-identical-delta test, no logging-facade/typed-error audit of
`code_layer.rs`, no README doc-list update, no `gate3n-complete` tag.

### Prerequisites — partial
- Frozen `walk_directory` isolation fix (`overlay.rs`): present but **still
  uncommitted** on the branch.
- Interim session write lock: **never existed** (see Step 6 note).

---

## Risk register (most-likely-to-bite first)

1. **Nothing compiled.** `produce_full_code_delta` is large and unbuilt. Expect
   borrow-checker and type fixups: the `did_of` / `rel` / `resolve_target`
   closure borrows of `head` and `root`, the double `let mut abs_files` shadow
   (HashSet→Vec via `drain`), and `enclosing_id`'s `u64` span subtraction (relies
   on `end >= start` to avoid debug underflow). **Build first, in isolation.**

2. **Reference target resolution is the top parity hazard.** `resolve_target`
   maps an occurrence's `(target_file, target_qualified_name|target_name)` to a
   `DurableId` via `registry.by_path` then a `(file_rel, leaf)` name index. The
   engine's `target_qualified_name` likely uses dotted form (`User.save`) while
   the registry key is `::`-joined and **absolute**-file-prefixed
   (`/abs/models.py::User::save`). These probably **do not align**, so many
   project-local targets may fall through to `<external>` stubs — a real parity
   divergence under the strengthened comparator. Verify the exact
   `target_qualified_name` format and the registry key format and reconcile them
   (build a `(file, engine_qualified_name) → DurableId` index, not just leaf).

3. **`qualified_name` rewriting is approximate.** The producer rewrites the
   absolute-file prefix to the relative one
   (`qualified_path.replacen(&e.file, &file_rel, 1)`) and keeps `::` separators.
   The read-surface graph stores the **engine's** `qualified_name` (e.g.
   `User.save`). The comparator includes `qualified_name`, so this is a likely
   mismatch. Decide one canonical form and make both paths emit it. (Cleanest:
   have the producer emit the engine's `qualified_name`, carried on the entity,
   rather than deriving from `qualified_path`.)

4. **Inheritance may silently under-produce.** `compute_supertypes` is called at
   each class's `range.start` (the `class` keyword position), but
   `prepare_type_hierarchy` expects the cursor on the **class name**. If it
   returns empty there, INHERITS (and therefore OVERRIDES) edges are missing.
   Add the class's `selection_range` to `Entity` and query at the name position;
   add a parity test on the diamond + external-base cases.

5. **`content_hash` is decimal, not hex.** The guide says "hex of u128"; the
   existing Symbol DTO uses `anchor.content_hash.0.to_string()` (**decimal**).
   The producer matches the **codebase** (decimal) so the replica's hashes equal
   the read-surface graph's. If you later switch to hex, switch *both* sides at
   once or parity breaks.

6. **`DEFINES` vs `CONTAINS` round-trips through one wire kind.** The wire has a
   single `containment` kind; the applier re-derives DEFINES (module→child) vs
   CONTAINS (nested) from the source node's kind. This matches `children()` /
   `parent()` (which query both), but any code that distinguishes the two on the
   replica will see the reconstructed value, not the original.

7. **External-stub node shape is guessed.** Stubs are emitted with
   `kind = "class_"`, `file = "<external>"`, `content_hash = None`. The
   read-surface path may classify or key external nodes differently
   (`_ensure_target_node_simple`, `_infer_package`). Cross-check against the
   read-surface external nodes; `external_symbols()` / `resolve_external()` and
   the comparator will flag any mismatch.

8. **Path/key consistency across layers (deferred Step 7 seam).** Producer emits
   project-relative files; `session.locate(id)` returns native locations. The
   id-join consistency check (§10.2.2) is untested and only matters once Step 7
   makes snapshots native — but the relative/absolute split is a latent trap.

9. **Full-rebuild-per-commit cost (Step 5 deferred).** Correctness is fine;
   throughput on multi-file projects is not. Don't benchmark until Step 5 lands.

---

## Suggested validation order (when resuming)

1. `devenv shell -- build` — fix `produce_full_code_delta` compile errors in
   isolation; nothing else depends on it compiling to run the pure tests.
2. `test_code_delta_apply.py` — **pure Python, no producer dependency.** Proves
   the applier in isolation. Should pass independent of all Rust risk above.
3. `test_native_code_delta.py::test_commit_emits_code_delta` — cheapest proof the
   producer runs and stamps a revision.
4. `test_native_code_delta.py::test_apply_code_delta_matches_rebuild` — first real
   parity signal. Failures here are almost certainly risks **2/3/4** above (target
   resolution, `qualified_name` form, inheritance) — fix the **producer**, never
   the comparator (§6.3.1).
5. Then lift the full `test_incremental_parity.py` scenarios against the native
   delta and only after green, do Steps 5 → 7 → 8.

## File-change index

```
rust/src/dto/code_delta.rs    NEW  wire contract
rust/src/dto/mod.rs           mod + re-export
rust/src/dto/sync.rs          SyncResultDto.code_delta
rust/src/code_layer.rs        NEW  CodeLayer + delta assembly
rust/src/lib.rs               mod code_layer
rust/src/entity.rs            Entity.file + Entity.range
rust/src/project.rs           HeadState.code_layer; produce_full_code_delta;
                              enclosing_id; resolve_target; file_source_for;
                              pos_le; commit_head wiring; SymbolKind import;
                              code_delta: None on the 5 initializers
src/tyo3/models/analysis.py   SymbolNodeDelta/EdgeDelta/CodeDelta + SyncResult field
src/tyo3/graph/graph.py       apply_code_delta + helpers; CodeDelta import
src/tyo3/session.py           _apply_graph_delta revision-gated native apply
src/tyo3/graph/tests/test_code_delta_apply.py     NEW
src/tyo3/graph/tests/test_native_code_delta.py    NEW
```
