# The `SourceAnalysis` seam — what stack-graphs would (and would not) replace

> Companion to `ADR-001-stack-graphs-as-core.md`. This doc is **descriptive, not
> a build plan.** It pins the exact boundary where stack-graphs *could* slot in,
> so the §7 "multi-language" condition in the ADR has a concrete target if it
> ever fires. It also makes the impedance mismatches undeniable by drawing them
> at the level of real types and functions.

## 1. The seam today (grounded in the code)

The `SourceAnalysis` contract (`tyo3-spine.allium`) is realised by three engine
calls, consumed by the scoped producer in `rust/src/code_layer.rs`:

```
ty-based engine                        native L0 producer (Rust)
─────────────────────────────────      ───────────────────────────────────────
compute_document_symbols(file)  ──►  Vec<SymbolDto>      ─┐
                                      { durable_id?,       │  Builder::build_scoped(seed_dirty)
                                        container_name?,   │    • upsert nodes (identity attached)
                                        range, kind,       │    • add_containment_edges  (Defines/Contains)
                                        content_hash }     │
compute_file_occurrences(file)  ──►  Vec<NameOccurrence>  ─┤    • resolve_references     (References/Imports)
                                      { name, range,       │       └─ try_add_edge(source→target, kind)
                                        role }             │          └─ CodeLayer::add_edge
compute_supertypes(class)       ──►  supertype list       ─┘             └─ maintains reverse_deps[target] ∋ source
                                                              │
                                                              ▼
                                                        diff_from(prev) ► CodeDeltaDto
                                                        affected_closure_with_deleted(changed ∪ deleted)
                                                              │
                                                              ▼
                                                        CommitDelta { code_delta, affected, … }
```

Two distinct jobs are happening here, and only one of them is "name resolution":

- **Identity + structure** (`compute_document_symbols` → `SymbolDto` carrying a
  `durable_id` and `content_hash`; node upsert; containment edges). This is
  tyo3's spine work. **Stack-graphs has no part in this.**
- **Reference resolution** (`compute_file_occurrences` → `NameOccurrenceDto` →
  `References`/`Imports` edges; `compute_supertypes` → `Inherits`/`Overrides`).
  This is the name-binding job. **This is the only place stack-graphs fits.**

## 2. What stack-graphs would replace

Exactly the *reference-resolution* arrow: turning a reference occurrence into a
resolved `source_id → target_id` structural edge. Concretely it would supply the
inputs to `try_add_edge` for the `References`/`Imports`/`Inherits`/`Overrides`
kinds, replacing the `resolve_references` / `compute_supertypes` half of the
engine — *and nothing else.*

```
stack-graphs L0 resolver (hypothetical)
───────────────────────────────────────
per file (isolated):                          query time (stitch):
  TSG construction → partial paths    ──►        stitch through root
  stored keyed by file                           ► resolved (ref → def) pairs
                                                 ► map def node ⇒ tyo3 DurableId   ◄── NOT PROVIDED
                                                 ► emit References/Imports edge
```

## 3. What stack-graphs would NOT replace (the rest of the system)

- **Identity minting & reconciliation** (`rust/src/identity.rs`) — durable ids,
  ambiguous re-bind / vanished / reappeared lifecycle.
- **Content hashing** (`hash.rs`, `EntityHashing` contract) — normalised,
  cosmetic-stable `ContentHash` keying derived caches.
- **Revisions, snapshots, B+ commit** (`overlay.rs`, `content.rs`,
  coordination) — MVCC, frozen read surface, atomicity.
- **Derived & authored layers** (`authored.rs`, `tyo3-layers.allium`) —
  embeddings/docs/intent + review/orphan records.
- **The bus, the DAG, the refinement channel** (`tyo3-coordination.allium`).
- **`reverse_deps` maintenance + `affected_closure`** — stack-graphs would *feed*
  this with edges, but the eager index and the synchronous closure remain tyo3's.
- **`compute_document_symbols`** — node discovery + identity attachment stays;
  stack-graphs' "definition nodes" are not tyo3 entities and carry no id.

## 4. The three impedance mismatches (why even the seam is awkward)

1. **No durable identity → a mapping layer is mandatory.** Stack-graphs resolves
   to an intra-file *definition node*, recomputed on every edit. tyo3 edges are
   `DurableId → DurableId`. Every resolved pair must be translated def-node ⇒
   `DurableId` via tyo3's identity index — work stack-graphs cannot do and does
   not model. The translation is exactly where renames/moves are absorbed, and
   it is tyo3-native. So the "clean" resolver still needs tyo3's identity layer
   wrapped around every output.

2. **Lazy stitch-at-query vs. eager affected.** Stack-graphs' incrementality is
   "store partial paths per file, stitch at query." tyo3 must produce `affected`
   *synchronously inside the commit*. We would have to drive stitching eagerly
   for the dirty scope at commit time — using the formalism against its grain and
   discarding the laziness that makes it elegant. `produce_code_delta_scoped`
   already does eager, scoped resolution; stack-graphs offers no improvement on
   that, only a different shape with a mismatch tax.

3. **Syntactic-only vs. type-directed.** `compute_file_occurrences` /
   `compute_supertypes` ride on `ty`'s analysis. Stack-graphs is purely
   syntactic. For `w = make_widget(); w.draw()` neither resolves the
   inference-flow edge today — but a real type checker *can be extended to*,
   whereas stack-graphs structurally cannot. Swapping in stack-graphs converts a
   recoverable limitation into a permanent one. (This is ADR §5.2.)

## 5. Minimal adapter interface (if §7 ever fires)

If the multi-language condition is met, the clean insertion is a single trait
behind the existing producer — *not* a rewrite. Sketch:

```rust
/// Supplies resolved, name-bound structural edges for one file, in DurableId
/// space. Implemented today by the `ty` reference resolver; could be implemented
/// by a stack-graphs backend that stitches the dirty scope and maps def-nodes to
/// DurableIds via tyo3's identity index.
trait ReferenceResolver {
    /// Resolve every reference/import occurrence in `file` to its target
    /// entity, already translated into DurableId space. The producer turns each
    /// into a References/Imports edge via `try_add_edge` and maintains reverse_deps.
    fn resolve_edges(
        &self,
        state: &TyProjectState,
        file: &str,
        identity: &IdentityIndex,   // def-node ⇒ DurableId mapping lives here, tyo3-native
    ) -> Vec<ResolvedEdge>;         // { source: DurableId, target: DurableId, kind: EdgeKind, role/range }
}
```

Everything above the trait (nodes, identity, hashing, reverse_deps, affected,
commit, layers, bus) is unchanged. Everything stack-graphs brings sits *below*
it, behind the `IdentityIndex` translation. That is the precise, honest size of
the opportunity: **one swappable resolver behind a tyo3-owned identity boundary,
buying language reach at the cost of type-directed precision.**

## 6. Bottom line

The seam exists and is clean to express, but it is small, it sits *inside* L0,
and crossing it *loses* precision while *gaining* only language-agnosticism tyo3
does not currently want. Hence ADR-001: not the core; a conditional,
below-the-identity-line resolver swap, gated on the multi-language goal.
