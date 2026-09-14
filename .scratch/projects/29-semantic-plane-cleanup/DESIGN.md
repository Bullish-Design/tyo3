# DESIGN — Semantic-plane cleanup (project 29)

Verified against `main` at commit `e447986` on 2026-09-13. Every claim below
carries a `file:line` anchor or a reproduction.

---

## 1. The v2 concept document, reviewed

`tyo3_v2_refactoring_concept.md` proposes eight refactors in ten phases. The
architectural position is sound and its non-goals (no graph DB, no Salsa
replacement, no multi-language core, no revision DAG, no semantic merge engine)
are correct and worth keeping. Three of its priorities are **already shipped**,
and its Appendix A grounding is stale on each:

| Doc claim | Reality |
|---|---|
| Phase 5: `ProduceContext` read tracing "designed but not yet wired" | Wired. `_RecordingContext` records every typed read (`src/tyo3/session/views.py:20`); `DerivationDAG.derived_traced` (`src/tyo3/derive/dag.py:469`); `_traced_fingerprint` (`dag.py:404`). |
| Phase 6: content-address derived artifacts, drop revision from the key | Done. Store key is `"<input_hash>:<generator_version>"`; no revision participates. |
| Phase 3: make RustworkX a strict projection | Done, and further. `src/tyo3/graph/projection.py:5`: "a pure projection of `full_code_delta()` — not a transaction participant." |

**One outright conflict.** §7.1's `SemanticDelta` carries `edges_added` /
`edges_removed`, and §8.2–8.3 assume an incrementally maintained projection with
a cold-rebuild parity invariant. Both were deliberately retired:

- `rust/src/dto/commit_delta.rs:20` — "The structural `CodeDeltaDto` is **not**
  carried here … graph consumers build on demand from `full_code_delta()`
  (Project 31, #2)."
- `src/tyo3/graph/applier.py:88` — "The incremental machinery (revision-gating,
  node/edge removals, in-place re-emit, moves) was retired with the per-commit
  incremental path (#1)."

The document never mentions this decision, so it never argues against it. **Do
not adopt §7.1 or §8.2–8.3 on trust.**

---

## 2. What `IdentityRegistry` and `CodeLayer` really are

The document's central premise is that these are "two externally separate
authoritative planes" describing "the same semantic entities." They are not.

### 2.1 The coupling is a one-way pipeline

```
state.registry
    → collect_symbols_recursive          (rust/src/project/analysis.rs:128)
    → SymbolDto.durable_id               (rust/src/convert/symbols.rs:94)
    → Builder                            (rust/src/code_layer.rs:489)
    → CodeLayer
```

`code_layer.rs` never reads the registry. Grep for `registry` in that file
returns only test-setup lines (`code_layer.rs:1433-1435`). The boundary is
already clean and already one-directional.

### 2.2 Four asymmetries

|  | `IdentityRegistry` | `CodeLayer` |
|---|---|---|
| **Key set** | reconciled entities only | + synthetic `<module>` nodes, + external stubs |
| **Also holds** | `Orphaned` anchors whose node is gone | — |
| **Lifetime** | cloned into every read clone and snapshot (`rust/src/project.rs:84`) | **HEAD only** (`project.rs:135`) |
| **Persistence** | sidecar, every commit (`commit.rs:553` `persist_identity`) | never |
| **Semantics** | *last-known* facts across revisions | *current* facts at one revision |

The last row is load-bearing. Reconciliation works **because** the two disagree:
`Reconciliation::classify` reads the old hashes captured on the bindings before
the in-pass rebind overwrote them (`commit.rs:380`). One merged `EntityRecord`
per `DurableId` deletes that difference.

### 2.3 Consequence — Phase 1 as written is a net negative

`SemanticState { registry, code }` puts two fields with different lifetimes and
clone costs in one struct. `TyProjectState` (the cheap read clone, `project.rs:81`)
would then carry either:

- `code: Option<CodeLayer>` — reinstating the exact `Option` asymmetry the façade
  was meant to remove; or
- a full `CodeLayer` clone per read — a real cost on the read path.

Neither is an improvement. **Rejected as specified.** A corrected version is
deferred to project 31 (§5).

---

## 3. Phase 4 (one `EntityRecord`) — investigated and rejected

### 3.1 The actual overlap is three fields

```
Anchor    (identity.rs:51)  id, qualified_path, content_hash, kind,
                            first_seen_rev, last_seen_rev, status
NodeData  (code_layer.rs:120) name, qualified_name, kind, file, range,
                            name_range, content_hash, content_hashes,
                            external, package
```

Shared: `kind`, `content_hash`, and `qualified_path` ≈ `file` + `"::"` +
`qualified_name`. Everything else is disjoint.

### 3.2 Unifying those three fields does not work cleanly

- **`kind` cannot be unified.** `Anchor.kind` is `SymbolKind`
  (`entity.rs:29`), which has **no `Unknown` variant**. `NodeData.kind` is a
  lowercased string and holds `"unknown"` for external reference stubs
  (`code_layer.rs:884` via `add_stub_node(…, "unknown", …)`). It also holds
  `"class_"`, not `"class"`. Unifying means adding an `Unknown` variant to
  `SymbolKind`, which ripples into the `ty_ide` conversion (`entity.rs:44`), the
  DTO, and the Python `SymbolKind` StrEnum.
- **`content_hash` cannot be unified cheaply.** `ContentHash` is
  `ContentHash(pub u128)` (`hash.rs:63`) and non-optional on `Anchor`.
  `NodeData.content_hash` is `Option<String>` (hex) and is `None` for every stub.
  Unifying means changing `NodeData`'s representation and re-deriving the exact
  hex at `to_dto()` time.
- **`qualified_path` is already small.** Only three split sites, all in
  `identity.rs` (`:592`, `:847`, `:897`), and one build site (`entity.rs:229`).
  A `QualifiedPath` newtype is tidy but near-zero payoff.

### 3.3 The blocking risk

`CodeNodeDto.kind`, `.content_hash`, `.file`, and `.qualified_name` are all in
the parity oracle's **strict structural tier** (`tests/parity_oracle.py`,
`STRUCTURAL_NODE_FIELDS`). Any representation change must reproduce the wire
shape byte-for-byte. That is a high-risk change for a three-field cleanup with no
functional payoff.

**Verdict: do not do Phase 4.** If a narrow version is ever wanted, take only the
`QualifiedPath` newtype; leave `kind` and `content_hash` alone.

---

## 4. The real defects in this area

Four, none of which Phase 1 or Phase 4 fixes. Three become Steps 1–3; the fourth
becomes project 30.

### 4.1 The `Option<IdentityRegistry>` is a lie → **Step 2**

`TyProjectState.registry: Option<IdentityRegistry>` (`project.rs:84`). Every
production path passes `Some`. 25 sites pay for it:

```
rust/src/project.rs:162,178            read_clone impls
rust/src/project/analysis.rs:128       state.registry.as_ref()
rust/src/project/identity_ops.rs:28    let Some(registry) = … else
rust/src/project/snapshot.rs:499,514,527,549
rust/src/project/methods.rs:137,602,624,689,700,708
rust/src/project/head_view.rs:355,364,371
rust/src/project/commit.rs:359,374,375,555,572,696,706,1020
rust/src/code_layer.rs:1425,1435       test setup
```

One site genuinely wants an empty registry: `commit.rs:359`, the
entity-extraction state built *before* reconcile, which must not attach durable
ids. **`Some(&empty)` is equivalent to `None` there**, verified at
`rust/src/convert/symbols.rs:94`:

```rust
let anchor = registry.and_then(|r| r.by_path(&identity_path).and_then(|id| r.get(id)));
```

An empty registry makes `by_path` return `None`, so `and_then` short-circuits
identically. The `Option` therefore carries no information.

### 4.2 The three id populations are implicit — and the guess is wrong → **Step 1**

Three populations, distinguished by string shape:

| Population | Id form | `file` |
|---|---|---|
| Real entity | 26-char ULID (`identity.rs:35`) | real path |
| Synthetic module | `"<module>" + file` (`code_layer.rs:33`) | real path |
| External stub (reference target) | `"{package}::{name}"` (`code_layer.rs:1075`) | `"<external>"` |
| External stub (import target) | `"{package}::<module>"` (`code_layer.rs:1101`) | `"<external>"` |

`src/tyo3/graph/identity.py:68 is_entity_durable_id` classifies by prefix:

```python
if durable_id.startswith(("<module>", "<external>")):
    return False
```

**External stub ids never start with `<external>`.** Only their `file` field does.
So every external stub is classified as a real entity. Reproduced:

```
$ python -c "from tyo3.graph.identity import is_entity_durable_id; ..."
'<module>main.py'            -> False
'requests::Session'          -> True    # WRONG
'requests::<module>'         -> True    # WRONG
'01J0ABCDEFGHJKMNPQRSTVWXYZ' -> True
```

End-to-end, on a one-file project importing `json`:

```
all nodes:
   01M2ER6S3JQ418NNVKPV4FEE2A     | main.py     | external=False
   <module>main.py                | main.py     | external=False
   stdlib/json/__init__.pyi::     | <external>  | external=True
   stdlib/json/__init__.pyi::loads| <external>  | external=True
   unknown::<module>              | <external>  | external=True

code.ids(): ['01M2ER6S3JQ418NNVKPV4FEE2A',
             'stdlib/json/__init__.pyi::',
             'stdlib/json/__init__.pyi::loads',
             'unknown::<module>']
```

`src/tyo3/layers/code.py:56` documents `ids()` as "Excludes `<module>` and
`<external>` synthetics." It leaks three of them. `value()` returns a full
`SymbolNode` for `'stdlib/json/__init__.pyi::loads'` with `external=True`
despite gating on the same predicate.

Blast radius — every `.ids()` consumer:
`src/tyo3/models/diff.py:208-209` (layer diffs), `src/tyo3/daemon/handlers.py:663`
(daemon layer listing), `src/tyo3/layers/derived.py:69-70`,
`src/tyo3/layers/authored.py:85-86`.

The reliable discriminator exists already and only Rust holds it:
`NodeData.external` (`code_layer.rs:133`). Python is guessing at something the
producer knows for certain.

### 4.3 `reverse_deps` has no derivability check → **Step 3**

`CodeLayer.reverse_deps` (`code_layer.rs:173`) is maintained edge-by-edge by
`add_edge` / `remove_edge` (`:197`, `:209`) and, per `code_layer.rs:457`,
"**maintained** edge-by-edge … never rebuilt from scratch — working rule 3."

It is the sole input to `affected_closure_with_deleted` (`:263`), which produces
`affected_ids` (`commit.rs:928`), which drives derived-layer invalidation. If the
index drifts from the canonical edge set, `affected_ids` **under-fires** and
derived artifacts go silently stale. No test catches it:

- `code_layer.rs:1317` checks `add_edge`/`remove_edge` on a hand-built layer.
- `tests/test_affected_closure.py` checks one closure case end-to-end.
- **Nothing** asserts `reverse_deps == derive_from(edges)` after a real scoped
  incremental commit.

`remove_edge`'s pruning rule is the specific risk: it only drops a `reverse_deps`
entry when *no parallel dependency edge of the same `(source, target)` remains*
(`:209-223`). That scan is correct as written, but it is exactly the kind of
invariant that breaks quietly under a future edit.

This is the v2 document's Invariant C, and it is the one invariant from that
document worth adopting immediately.

### 4.4 Every fresh snapshot rebuilds the whole code layer → **project 30**

`Snapshot.full_code_delta` runs `produce_code_delta` against an **empty** prev
(`rust/src/project/snapshot.rs:58-67`), i.e. a full `Builder::build()` over the
entire project. `Snapshot.graph()` memoizes per snapshot
(`src/tyo3/session/views.py:286`), but the first call pays in full.

`_RecordingContext.__init__` calls `snapshot.graph()`
(`src/tyo3/session/views.py:44`). **Every traced derived production on a fresh
snapshot pays a full project rebuild.** For the LLM/embedding producers the doc
is most concerned about, this is the dominant cost and the document never
mentions it.

Fixing it means snapshots start carrying a `CodeLayer`, which changes the memory
profile per pinned snapshot and is the first real argument for a `SemanticState`
boundary. Sized as its own project **30**, after this one.

---

## 5. Deferred: `SemanticState` (project 31)

The document's Phase 1, corrected. Not `SemanticState { registry, code }` on both
sides, but an explicit split:

- **HEAD** owns `semantic: SemanticState { identities, code }` — both live, both
  owned, one publication boundary.
- **Read clone / snapshot** owns identities plus a *lazily produced* layer.

That is what happens today; the value is naming it so the asymmetry is deliberate
rather than accidental. Do it **after** project 30, because caching the layer on
snapshots may reshape the boundary and there is no point naming it twice.

---

## 6. Decided: Jujutsu integration uses `pyjutsu`

Evaluated `../gitman` and `../pyjutsu`.

**`gitman` — no.** It is a workflow policy layer (lanes, `save`/`land`/`push`,
markdown projections, invariants) and a *writer* of repository state. tyo3 needs
a read-only context probe. Wrong layer; it would couple tyo3 to the lane model.

**`pyjutsu` — yes.** In-process PyO3 binding to `jj-lib` 0.44.0. Supplies all
four fields the concept document's §13.3 `JujutsuContext` asks for:

| Field | Call |
|---|---|
| `workspace` | `ws.name` |
| `change_id` | `ws.working_copy().change_id` |
| `commit_id` | `ws.working_copy().commit_id` |
| `operation_id` | `ws.operations(limit=1)[0].id` |

Three properties beat the document's proposed `jj` subprocess adapter:

1. **Reads publish no operation.** `docs/USER_GUIDE.md:23` — "`RepoView` — a
   snapshot of the repo at one operation. No working-copy snapshot, no new
   operation." A subprocess `jj log` auto-snapshots and writes the user's op log.
   tyo3 must never mutate the repo to label a revision.
2. **Frozen Pydantic v2 models, `extra="forbid"`.** A `jj-lib` shape change fails
   loudly; text parsing fails silently.
3. **`requires-python >=3.13`**, matching tyo3.

Constraints to plan around:

- `crate-type = ["cdylib"]`, `publish = false`. tyo3's **Rust** cannot link it.
  The adapter lives in Python at `src/tyo3/integrations/jujutsu.py` — which is
  exactly the document's §13.4 boundary, so nothing is lost.
- Not on PyPI: path or git dependency, under an optional extra (`tyo3[jj]`),
  never a base dependency. Invariant G then falls out for free — no pyjutsu,
  `revision_context.jujutsu = None`.
- Hard pin `jj-lib = "=0.44.0"`: tyo3 inherits pyjutsu's jj cadence. Acceptable
  for an optional extra.

`Workspace.load(path)` handles colocated jj/git, covering §13.5.

Not part of project 29. Own project, any time after this one.
