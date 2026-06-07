# Phase 2 — Native Code Layer + Code Delta (parity-only)

> A step-by-step execution guide for **Phase 2** of
> `REFINED_IMPLEMENTATION_PLAN.md`. Read that plan's "Phase 2" section and §5.3 /
> §5.4 / §5.6 of `REFINED_IMPLEMENTATION_CONCEPT.md` once before starting — this
> guide assumes that vocabulary (Entity, DurableId, ContentHash, the code layer,
> the code delta, the parity oracle) and turns it into concrete edits against the
> code as it exists today.
>
> **Phase 2 depends on Phase 1.** Phase 1 made committed generations complete and
> reconciled identity at open. Phase 2 builds the native code layer *on top of*
> that complete content and that populated registry. If Phase 1's milestone gate
> is not green, stop and finish Phase 1 first.

---

## 0. The dev environment — read this first, it governs every command below

This repository is a **devenv.sh (Nix-managed) dev environment**. The toolchain
(the right `rustc`, `cargo`, `maturin`, `python`, `pytest`, `ruff`, and the
project's custom scripts) only exists *inside* the devenv shell. A `cargo` or
`pytest` run from a bare login shell is either missing or the wrong version and
will give you misleading results.

**The rule for the entire phase: every in-repo operation runs through the devenv
shell.** Two equivalent forms:

- One-shot (preferred in this guide, copy-pasteable):
  ```bash
  devenv shell -- cargo test --manifest-path rust/Cargo.toml code_layer
  ```
- Interactive (if you are iterating rapidly):
  ```bash
  devenv shell        # drop into the environment once
  # …then run cargo / pytest / the project scripts directly inside it…
  ```

The project defines custom scripts inside `devenv.nix` — `build`, `tests`,
`test-rust`, `test-quick`, `clean`, `status`, etc. When this guide says "rebuild
the extension," it means `devenv shell -- build` (which runs maturin so Python
sees the freshly compiled Rust). **A pure `cargo test` does not refresh the
compiled extension Python imports** — Phase 2 is the first phase whose Rust work
Python must observe (the parity tests call into the native producer), so after
*every* Rust change that the Python parity suite must see, run
`devenv shell -- build` before the pytest gate. Forgetting this is the single
most common way to chase a phantom parity failure.

The suites are slow. Allow **~10–15 minutes** for a full run; launch full suites
in the background and give them time rather than assuming a hang.

> Throughout the rest of this document, **assume every `cargo`, `pytest`,
> `maturin`, `ruff`, and project-script invocation is prefixed with
> `devenv shell --`**, even where a line is abbreviated for readability.

---

## 1. What Phase 2 changes, and why

**Goal (from the plan):** Rust maintains the authoritative code-graph *structure*
and produces a **code delta** inside the commit. In this phase the delta is
emitted *alongside* the existing Python read-surface build and checked with the
parity oracle — it is **not yet authoritative**. The cutover (making it
authoritative and deleting the Python build) is Phase 4. This is the high-risk
core: go slowly and lean on parity.

**The situation today** (confirmed in the current code):

- The code graph is built **entirely in Python**, in `CodeGraph.build`
  (`src/tyo3/graph/graph.py:158`), by reading the analysis "read surface" over
  FFI: per-file `document_symbols`, `file_occurrences`, `class_supertypes`, etc.
  It is a deterministic six-pass build (`graph.py:199-253`): collect symbols →
  materialise nodes → containment edges + range caches → reference resolution →
  inheritance (two passes: all `INHERITS` then all `OVERRIDES`) → diagnostics.
- Rust already extracts the *entities* reconciliation needs
  (`rust/src/entity.rs:86` `extract_entities`), but the `Entity` record is
  deliberately minimal (`entity.rs:69`): `qualified_path`, `kind`,
  `content_hash`, `container`, `name`. It carries **no file, no ranges** — it was
  built for identity, not for graph production.
- Rust owns the identity registry (`rust/src/identity.rs:84` `IdentityRegistry`)
  with a `by_path` index (`qualified_path → DurableId`, `identity.rs:97`) and a
  `by_hash` index. The producer will reuse this to attach durable ids to nodes.
- There is **no native code layer and no code delta**. The commit
  (`commit_head`, `rust/src/project.rs:1435`) produces only the path-shaped
  `SyncResultDto` (`rust/src/dto/sync.rs`).

**The fix this phase delivers:** a native `CodeLayer` (nodes + edges +
reverse-deps) and a native `CodeDelta` produced *inside the commit*, emitted next
to the legacy build, and proven **structurally identical** to it via the parity
oracle's strict tier (`src/tyo3/tests/parity_oracle.py`), with any cosmetic
divergence surfaced and understood. When the structural tier is green for every
parity case, Phase 4 can safely delete the Python build.

### The parity oracle is the whole safety strategy

The comparator already exists (Phase 0) and is self-tested by
`test_final_parity_oracle.py`. Its shape dictates what Phase 2 must expose:

- `parity_oracle.native_delta_graph` probes `source._inner` for one of
  `full_code_delta`, `code_delta`, `_code_delta`, then calls
  `graph.apply_code_delta(code_delta)` on a fresh `CodeGraph` and compares it to
  the legacy build with `assert_graphs_equal`.
- The comparison is **tiered** (see `parity_oracle.py`'s module docstring):
  - **Structural (strict, always fatal):** the node set keyed by `durable_id`,
    the load-bearing per-node fields `STRUCTURAL_NODE_FIELDS =
    (durable_id, name, qualified_name, kind, file, range, content_hash,
    external)`, and the edge **relation** set (deduplicated
    `(source_id, target_id, kind)` triples). **This is the tier you must drive to
    zero diffs in Phase 2** — it is what every graph query consumer reads, and
    Phase 4 deletes the legacy build on the strength of it.
  - **Cosmetic (downgradeable):** the incidental per-node fields
    `COSMETIC_NODE_FIELDS = (selection_range, content_hashes, package)` and the
    full edge **multiset** (occurrence ranges, roles, originating files, parallel
    multiplicities). `assert_parity` defaults to `cosmetic="warn"`: cosmetic
    diffs are surfaced (logged) but do not fail. Aim to land these too — a clean
    cosmetic tier is the ideal — but a *known, understood* cosmetic divergence is
    not a blocker, and you should not contort the producer to reproduce an
    incidental field bug-for-bug.
- Any payload field that is neither listed structural nor listed cosmetic falls
  into the **structural** tier by default, so a newly-added field cannot silently
  slip past the strict check.

So Phase 2 must deliver **three** surfaces, not two:

1. the native producer + `CodeDelta` (the bulk of the work),
2. a Python-side `full_code_delta()` accessor on the native handle that returns a
   *full* (cold-start) delta for the current state, and
3. a **minimal `CodeGraph.apply_code_delta` applier** so the oracle can
   materialise the native half and compare.

> **Decision recorded — a small pull-forward from Phase 4.** The plan lists the
> pure applier (`apply_code_delta`) under Phase 4.1, but the parity oracle cannot
> run without it, and Phase 2's own acceptance ("assert parity") requires it.
> Build a *minimal* applier now. It is harmless: the legacy read-surface build
> stays authoritative (the post-commit path still calls `CodeGraph.build`), and
> the applier is only exercised by parity tests until Phase 4 flips the
> post-commit path to use it and deletes the legacy build. Keep the applier pure
> (no FFI, no session/snapshot access) from the start so Phase 4 inherits it
> unchanged.

### Files in scope

| File | Role in Phase 2 |
|---|---|
| `rust/src/entity.rs` | **edit** — add `file`, `full_range`, `name_range`, `qualified_name` to `Entity` (2.1) |
| `rust/src/code_layer.rs` | **new** — the native `CodeLayer` (2.2) |
| `rust/src/dto/code_delta.rs` | **new** — the `CodeDelta` wire DTOs (2.3) |
| `rust/src/dto/mod.rs` | **edit** — register + re-export `code_delta` |
| `rust/src/lib.rs` | **edit** — register the `code_layer` module |
| `rust/src/project.rs` | **edit** — `CodeLayer` field on `HeadState`; produce the delta in the commit; expose `full_code_delta()` on `PyTyProject` (2.4, 2.5) |
| `src/tyo3/graph/graph.py` | **edit** — add the minimal pure `apply_code_delta` applier (2.5) |
| `src/tyo3/graph/models.py` | reference — the parity payload contract (do not change shapes) |
| `src/tyo3/tests/parity_oracle.py` | reference — the comparator; do **not** weaken it |
| `src/tyo3/tests/test_final_parity_oracle.py` | the Phase 0 test that must go green this phase |

---

## 2. Working rules for this phase (non-negotiable)

1. **Write/await the failing test first, then implement until green.** Phase 0
   shipped the parity tests in an xfail/`ParityOracleNotReady` state. Your job is
   to make the native half exist so they flip from xfail to real assertions —
   and then make those assertions pass.
2. **Never weaken the comparator or a test — but understand its tiers.** If the
   *structural* tier fails, the producer is wrong, not the oracle; never narrow
   the structural tier (e.g. to "node ids only") to make progress. The *cosmetic*
   tier is downgradeable **by design** (`cosmetic="warn"`): surfacing a cosmetic
   diff and accepting it as known is legitimate; silently editing
   `STRUCTURAL_NODE_FIELDS`/`COSMETIC_NODE_FIELDS` to *move* a field out of the
   strict tier to dodge a real diff is not. If you believe a field is genuinely
   cosmetic and it is currently structural, justify it in the comparator with a
   comment and a self-test, don't quietly reclassify it.
3. **The legacy build stays authoritative this entire phase.** Do not touch the
   post-commit graph path in `session.py`, and do not delete any read-surface
   code. That is Phase 4. Phase 2 only *adds* the native side and compares.
4. **Don't swallow errors.** A producer that cannot resolve a node/edge must
   surface it (or set `rescan = true` with a one-line reason), never silently
   drop a node — a dropped node is exactly what parity will catch, painfully,
   later instead of now.
5. **Phasing inside the producer is a correctness requirement, not a style
   choice** (see 2.4). Materialise all nodes before any edge; resolve all
   `inherits` before any `overrides`. Out-of-order resolution makes results
   depend on iteration order.
6. **Milestone gate after the phase** (both must pass, both via devenv):
   ```bash
   devenv shell -- pytest -q --no-cov
   devenv shell -- cargo test --manifest-path rust/Cargo.toml
   ```

---

## 3. Pre-flight — establish the red baseline

Confirm the starting state before changing anything, so you can prove your work
moved the needle.

```bash
# 1. Phase 1 is landed and the Rust core is green.
devenv shell -- cargo test --manifest-path rust/Cargo.toml content overlay project entity identity

# 2. The parity comparator self-test passes (the safety net is trustworthy).
devenv shell -- pytest src/tyo3/tests/test_final_parity_oracle.py -q --no-cov -rA

# 3. The legacy graph build still works end-to-end (your parity target).
devenv shell -- pytest src/tyo3/tests/test_graph*.py -q --no-cov
```

Record the output. In step 2, the comparator's *own* tests pass, but any test
that calls `assert_parity` / `native_delta_graph` raises `ParityOracleNotReady`
(no native delta yet) and is xfailed — that absence is your North Star. By the
end of Phase 2 those flip to real, green assertions.

---

## 4. Step-by-step implementation

Do the steps in order. Each step lists **what**, **why**, **where**, and a
**verify** command (devenv-prefixed). Commit at the natural breakpoints noted.

### Step 2.1 — Restore the structural fields the producer needs on `Entity`

**What.** Extend `Entity` (`rust/src/entity.rs:69`) with the fields a graph node
needs that identity did not:

- `file: String` — the source file path (the same string form as
  `File::path(...).as_str()` and the legacy node's `file`, after the legacy
  build's project-relative normalisation; see the qualified-name warning below).
- `full_range` — the entity's full definition range (already available as
  `info.full_range`, `entity.rs:195`; today it is consumed to slice source and
  then discarded).
- `name_range` — the range of the symbol's **name** only, distinct from
  `full_range`. This is the analysis "selection range." It maps to the legacy
  node's `selection_range` (`graph.py:346`) **and** is what inheritance queries
  must position the cursor on.
- `qualified_name: String` — the leaf-relative dotted/`::` qualified name as the
  node carries it, kept *separately* from the combined `qualified_path`
  (`file::qualified_name`) the registry keys on.

**Why.** §5.4 / §5.6: the node payload must be small but *complete*. The producer
cannot synthesise `selection_range` or `file` after the fact without re-reading
the read surface — which is exactly what we are removing. The **name range
matters specifically**: inheritance/supertype resolution must position the cursor
on the class/def *name*, never on the `class`/`def` keyword or a decorator, or
supertype resolution silently returns nothing (the legacy build does this at
`graph.py:1001` using `symbol.selection_range.start`). Get this wrong and
inheritance edges vanish — and parity will fail on the edge multiset.

**Where / how.**
- Add the fields to the struct and populate them in `collect_entities_recursive`
  (`entity.rs:172`). The recursion already has `info.full_range`; obtain the name
  range from the same `SymbolInfo` (the analysis carries a name/selection range
  alongside the full range — confirm the field name on `ty_ide::SymbolInfo` and
  thread it through, mirroring how `full_range` is read at `entity.rs:195`).
- Keep `qualified_path` as-is (it is the registry's EXACT key and identity code
  depends on it). Add `qualified_name` and `file` as *additional* fields; do not
  repurpose the existing ones.
- Update the struct doc-comment (`entity.rs:62-67`) to record what each new field
  means and that `file`/`qualified_name` are the node-facing decomposition of
  `qualified_path`.

**Verify.**
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml entity
```
Add a unit test (in `entity.rs`'s `#[cfg(test)] mod tests`) asserting that for a
`class A: def b(self): ...` source, the `b` method entity has a `name_range` that
covers only `b` (length 1 token), a `full_range` strictly larger, a `file` ending
`m.py`, and `qualified_name` containing `b`.

> Commit here: `feat(entity): structural fields (file, full_range, name_range, qualified_name)`.

---

### Step 2.2 — Add the native code layer (`rust/src/code_layer.rs`)

**What.** A new module holding the authoritative structural state:

```rust
pub struct CodeLayer {
    nodes: BTreeMap<DurableId, NodeData>,
    edges: BTreeSet<Edge>,
    reverse_deps: BTreeMap<DurableId, BTreeSet<DurableId>>,
}

pub struct NodeData {
    pub kind: NodeKind,            // entity kinds + Module + External
    pub qualified_name: String,
    pub file: String,
    pub range: Range,              // full range
    pub name_range: Option<Range>, // selection range; None for synthetics
    pub content_hash: Option<ContentHash>, // None for module/external nodes
    // small structural fields only — NEVER vectors or large text (§5.4)
}

pub struct Edge {
    pub source: DurableId,
    pub target: DurableId,
    pub kind: EdgeKind,            // Containment/References/Imports/Inherits/Overrides
    pub role: Option<ReferenceRole>,
    pub file: Option<String>,      // originating file (reference/import edges)
    pub range: Option<Range>,      // occurrence range (reference/import edges)
}
```

**Why.** §5.4: the code layer is the canonical structural state, and the delta is
diffed against it. `reverse_deps` (target → sources that reference/import/inherit
it) is what makes the Phase 3 `affected_ids` closure computable without a global
scan — build it here, maintain it in the producer, so Phase 3 inherits a correct
index.

**Where / how — important details.**
- **Determinism.** Derive `Ord` on `Edge` (and order `NodeKind`, `EdgeKind`,
  `ReferenceRole`) and use `BTreeMap`/`BTreeSet` throughout, so emission order is
  reproducible across runs and machines (§5.12). This matters: the parity
  comparator folds edges into a multiset, but a stable order makes diffs
  readable and the eventual delta deterministic.
- **Node id space.** Entity nodes use the registry `DurableId`. Module and
  external nodes use the **exact same synthetic id scheme the Python side uses**,
  because parity compares ids directly:
  - module → `make_module_durable_id(file)` = `"<module>" + file`
    (`src/tyo3/graph/identity.py:58`),
  - external → the `<external>…` / `package::name` forms the legacy build mints
    (`graph.py:806`, `:943`). Reproduce these byte-for-byte. (A natural way to
    avoid drift: define the synthetic-id helpers once in Rust and have a unit
    test assert they equal the Python strings for representative inputs.)
- **`NodeKind` ↔ Python kind.** The DTO will lowercase kinds to match the Python
  `SymbolKind` string values; keep the Rust enum aligned with `SymbolKind`
  (`entity.rs:26`) plus `Module` and `External`.
- **Keep `NodeData` small.** §5.4 is explicit: no vectors, no large text. The
  content hash is the only heavy-ish field and it is fixed-width.

Register the module in `rust/src/lib.rs` (`mod code_layer;`) and give the
per-revision head state a `CodeLayer` field — add it to `HeadState`
(`rust/src/project.rs:95`) alongside the existing `registry` and `authored`
fields, initialised empty in `build_head_with_config` (`project.rs:955`,
near the `authored: AuthoredStore::default()` line at `project.rs:1021`).

**Verify.**
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml code_layer
```
Unit tests: an empty `CodeLayer` has no nodes/edges; inserting a node then an
edge updates `reverse_deps`; removing the edge removes the reverse-dep entry;
synthetic id helpers equal the documented Python strings.

> Commit here: `feat(rust): CodeLayer (nodes/edges/reverse_deps) + head-state field`.

---

### Step 2.3 — Add the code-delta wire contract (`rust/src/dto/code_delta.rs`)

**What.** DTOs that pythonize cleanly (field names match the Python mirror
exactly — see the parity contract below):

```rust
pub struct CodeNodeDto {
    pub durable_id: String,
    pub kind: String,                 // lowercased: "class"/"function"/"module"/…
    pub qualified_name: String,
    pub file: String,
    pub range: RangeDto,
    pub name_range: Option<RangeDto>, // → SymbolNode.selection_range
    pub content_hash: Option<String>, // hex; None for synthetic module/external
}

pub struct CodeEdgeDto {
    pub source_id: String,
    pub destination_id: String,
    pub kind: String,                 // containment/references/imports/inherits/overrides
    pub role: Option<String>,
    pub file: Option<String>,         // originating file (reference edges)
    pub range: Option<RangeDto>,      // occurrence range (reference edges)
}

pub struct CodeDeltaDto {
    pub revision: u64,
    pub rescan: bool,
    pub nodes_upserted: Vec<CodeNodeDto>,
    pub nodes_removed: Vec<String>,        // durable ids
    pub nodes_moved: Vec<CodeNodeMovedDto>,// { durable_id, file, range, name_range } — new location only
    pub edges_added: Vec<CodeEdgeDto>,
    pub edges_removed: Vec<CodeEdgeDto>,
}
```

**Why.** §5.4. This is the wire the Python applier consumes. It must carry
**every field the legacy `SymbolNode` and `EdgeData` carry**, because the parity
comparator checks them all (§1).

**The parity field contract (memorise this — it is where parity breaks).** The
**Tier** column tells you which diffs are fatal: `S` (structural) diffs *must* be
zero before Phase 4; `C` (cosmetic) diffs are surfaced and downgradeable, so aim
to land them but do not contort the producer for them.

| Python `SymbolNode` field (`models.py:18`) | Tier | Source in the DTO / applier |
|---|---|---|
| `durable_id` | **S** | `CodeNodeDto.durable_id` |
| `name` | **S** | leaf of `qualified_name` (applier derives) — or carry it explicitly to be safe |
| `qualified_name` | **S** | `CodeNodeDto.qualified_name` (see the format warning in 2.4) |
| `kind` | **S** | `CodeNodeDto.kind` (lowercased to the `SymbolKind` value) |
| `file` | **S** | `CodeNodeDto.file` |
| `range` | **S** | `CodeNodeDto.range` |
| `content_hash` | **S** | `CodeNodeDto.content_hash` (hex; None for module/external) |
| `external` | **S** | `True` only for external stub nodes; `False` otherwise |
| `selection_range` | `C` | `CodeNodeDto.name_range` (None for synthetics) — but see the note: getting it *right* is cheap and unblocks inheritance, so do it |
| `content_hashes` | `C` | the per-profile dict — legacy passes `getattr(symbol, "content_hashes", {})` (`graph.py:348`), i.e. `{}` unless the read surface supplied one. Match what the legacy build produces; if always `{}` today, the applier sets `{}` |
| `package` | `C` | set only for external nodes (the inferred package, `graph.py:954`) |

> **Carrying `name` explicitly is recommended.** `name` is structural. The legacy
> module node sets `name = PurePosixPath(file).stem` (`graph.py:318`) and entity
> nodes set `name = symbol.name` (`graph.py:341`). Deriving "leaf of
> qualified_name" does *not* reproduce the module stem. Rather than special-case
> in the applier, add `name: String` to `CodeNodeDto` and have the producer set it
> the same way the legacy build does. One field now saves a structural parity hunt
> later.

> **`selection_range` is classed cosmetic but don't skip it.** The comparator
> won't *fail* on it, yet `name_range` is the inheritance-cursor input (2.1): get
> it wrong and `inherits`/`overrides` edges vanish — which *is* a structural edge
> diff. So a wrong `name_range` shows up structurally via missing edges even
> though the node field itself is cosmetic. Plumb it correctly.

For `EdgeData` (`models.py:61`): `kind` (lowercased to `EdgeKind` value), `file`,
`range`, `role`. The **structural** edge fact is the `(source, target, kind)`
relation; `file`/`range`/`role` and parallel-edge multiplicity are **cosmetic**.
Containment/inheritance/override edges have `file=None`, `range=None`, `role=None`
(so they contribute only the relation); reference/import edges carry the
originating file, occurrence range, and role in the cosmetic multiset.

**Where / how.**
- Reuse the existing `RangeDto` from `dto/coordinates.rs` (used across the DTO
  layer) so range serialisation matches what Python already validates into
  `Range` (`tyo3.models.analysis.Range`).
- Register the module in `dto/mod.rs` (add `mod code_delta;` and
  `pub use code_delta::*;`, mirroring `sync` at `dto/mod.rs:15`,`:30`).
- Design it to **nest**, not compete: Phase 3 folds `CodeDeltaDto` into the
  larger `CommitDelta` as its `code_delta` field. Keep it self-contained (no
  back-references to `SyncResultDto`).

**Verify.**
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml code_delta
```
Unit test: a `CodeDeltaDto` round-trips through serde and (if you add a pythonize
smoke test) produces a dict whose keys exactly equal the Python field names.

> Commit here: `feat(dto): code-delta wire contract (nodes/edges/moved)`.

---

### Step 2.4 — The producer (all native, inside the commit, correctly phased)

**What.** A function — put it in `code_layer.rs` (e.g.
`produce_code_delta(state: &TyProjectState, registry: &IdentityRegistry,
prev: &CodeLayer, scope: Option<&HashSet<String>>) -> (CodeLayer, CodeDeltaDto)`)
— that builds the full structural set for the dirty scope, diffs it against the
current `CodeLayer`, and returns the next layer plus the minimal delta.

**Why.** §5.3 step 4: the code layer is updated *inside* the commit. Producing it
from the same `TyProjectState`/analysis the entity extractor uses keeps it
consistent with the revision being committed.

**Order matters — this is the correctness core. Do not collapse the passes:**

1. **Materialise all nodes first.** Collect entities for the scope
   (`extract_entities_for`, `entity.rs:109`, now carrying file/ranges from 2.1),
   plus the synthetic module node per touched file. Bind each entity to its
   `DurableId` via `registry.by_path(&entity.qualified_path)` (`identity.rs:97`).
   Insert every node into the fresh layer **before resolving any edge.** (Mirrors
   legacy Pass 2, `graph.py:210`.) Resolving an edge before its endpoint exists
   forces a guess and makes the result order-dependent.
2. **Containment edges.** Parent (enclosing entity, else the module node) →
   child, `kind = Containment`. The parent is the entity whose `qualified_path`
   is this entity's `container` (`entity.rs` `container` field), or the module
   node for top-level entities (legacy `_resolve_parent_id`, `graph.py:483`).
3. **Reference and import edges**, from analysis occurrences. Use the same
   occurrence surface the legacy build uses (`file_occurrences` →
   `NameOccurrenceDto`, fields `target_file`, `target_qualified_name`, `role`,
   `dto/occurrences.rs:22`). For each occurrence resolve the target node, add a
   `References`/`Imports` edge carrying originating file + occurrence range +
   role, and **maintain `reverse_deps`** (target → source). For project→project
   imports record the file-level importer relation too (legacy `_file_importers`,
   `graph.py:725`) — Phase 3 needs it; for now it lives in the producer's output.
4. **Inheritance in two passes.** First add **all** `Inherits` edges for the
   dirty set (legacy `_inherits_pass_I`, `graph.py:232`), positioning the
   supertype query cursor on each class's **name_range start** (from 2.1). *Then*
   compute **all** `Overrides` edges (legacy `_overrides_pass_II`, `graph.py:241`),
   which BFS-walks the now-complete inheritance chains. A single combined
   per-entity pass is **forbidden**: if an intermediate ancestor is also dirty,
   override correctness would depend on the order entities are processed.
5. **Diff against `prev`.** Compare the freshly-produced full set to the current
   `CodeLayer`: new id → `nodes_upserted`; missing id → `nodes_removed`; same id
   with changed content hash → `nodes_upserted` (re-emit); same id, same hash,
   new file/range → `nodes_moved`; edges symmetric-diff into
   `edges_added`/`edges_removed`. Provide a **full-delta path** (everything in
   `nodes_upserted` + all edges in `edges_added`, `rescan = true`) for cold start
   and for any case where a precise diff cannot be computed.

**Watch for (these are the real failure modes — each is a concrete parity break):**

- **Qualified-name format.** The analysis engine names entities in dotted form
  (`User.save`); the identity registry keys on a `::`-joined form with a file
  prefix (`pkg/mod.py::User::save`, `entity.rs:188-191`); the *graph node*
  carries yet another form — for nested entities the legacy build uses
  `symbol.qualified_name or symbol.name` (`graph.py:338`,`:342`), and the
  legacy node `durable_id` for a nested entity is a **compound**
  `registry_id::qualified_name` (`graph/identity.py:53-55`), while top-level
  entities use the registry id directly. **Reproduce exactly:** the producer must
  emit, for each node, the same `durable_id`, `qualified_name`, and `name` the
  legacy build would. This is the most likely first parity failure — when it
  fails, the comparator will report a node id present on one side only. Resolve
  it by aligning to `graph/identity.py` and `graph.py:335-365` case-for-case.
- **File-path normalisation.** The legacy build normalises first-party paths to
  project-relative POSIX (`_to_relative`, used at `graph.py:192`); your node
  `file` must match that normalised form, not the absolute `File::path` string.
  Decide where you normalise (producer vs. applier) and do it once,
  consistently.
- **Reference target resolution must match the legacy resolver case-for-case**
  (`_resolve_references_via_occurrences`, `graph.py:528`), including: the
  short-name vs. qualified-name lookup order (`graph.py:607`), the ambiguous
  short-name fallback (the `""` sentinel, `graph.py:358-364`), import edges that
  attach to the module node vs. the symbol, and external-stub creation. A
  reference that resolves to the wrong node is a parallel-edge multiset
  difference the comparator will flag.
- **Inheritance cursor position** — name_range, not full_range (see 2.1 and step
  4 above).
- **External / module synthetic nodes** must use the exact id, `external`, and
  `package` values the legacy build produces (`graph.py:806`,`:943`,`:954`).
- **Borrow-checker friction** on the producer's captures of `state.db`, the
  registry, and the root. Prefer taking `&TyProjectState` + `&IdentityRegistry`
  by shared reference and returning owned `CodeLayer`/DTO; avoid holding a borrow
  of the registry across a `&mut` of the layer.

**Verify (Rust-side, before any Python parity).**
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml code_layer code_delta
```
Unit tests over a temp-dir `TyProjectState` (reuse the `state_with_source`
harness pattern from `entity.rs:247`): a two-class file with a method override
produces the expected node set, one `Containment` edge per nesting, one
`Inherits` and one `Overrides` edge; a second `produce_code_delta` with no source
change yields an **empty** delta (no over-fire); changing one method body
re-emits exactly that one node in `nodes_upserted`.

> Commit here: `feat(rust): native code-delta producer (phased, parity-targeted)`.

---

### Step 2.5 — Emit alongside, expose to Python, and assert parity

**What.** Wire the producer into the commit, expose a full-delta accessor to
Python, add the minimal applier, and turn the parity tests green.

**(a) Produce inside the commit and store the next layer.** In `commit_head`
(`project.rs:1435`) — and the other commit bodies that build a `SyncResultDto`
(`sync_path_inner` `:1478`, the watcher fold `:1545`, authored/discard) — after
reconciliation (`run_identity_reconciliation`, `project.rs:1373`), call
`produce_code_delta` against the freshly-updated `state`/`registry`, **store the
returned `CodeLayer` back into `head.code_layer`**, and keep the `CodeDeltaDto`.
Do **not** thread it into `SyncResultDto` (that DTO is replaced in Phase 3) — for
this phase the delta only needs to be *obtainable*, which (b) provides. Keep this
strictly additive: the legacy post-commit path in `session.py` is untouched and
remains authoritative.

> Phasing note (§5.3): the code-layer update belongs *inside* the lock, after
> identity reconciliation, before publication. Phase 5 formalises the full
> staged transaction; in Phase 2 you are adding step 4 ("update the code layer")
> next to the existing reconciliation so it is already in the right place when
> Phase 5 stages everything.

**(b) Expose a full-delta accessor on the native handle.** The oracle probes
`source._inner.full_code_delta()` (`parity_oracle.py:242`). Add
`#[pyo3] fn full_code_delta(&self) -> PyResult<Bound<PyAny>>` on `PyTyProject`
(`project.rs:163`) that produces a **full** (`rescan = true`, scope = all)
`CodeDeltaDto` for the current head state against an *empty* `prev` layer, and
returns it pythonized (the same `pythonize`/dict pattern the other PyO3 methods
use for `SyncResultDto`). This gives the oracle a cold-start delta independent of
incremental history — the cleanest thing to compare against a fresh legacy build.

**(c) Add the minimal pure applier to `CodeGraph`.** In
`src/tyo3/graph/graph.py`, add `apply_code_delta(self, code_delta) -> None`:
construct `SymbolNode`s from `nodes_upserted` (mapping `name_range →
selection_range`, lowercased `kind → SymbolKind`, hex `content_hash`, `external`
/ `package` for synthetics), remove `nodes_removed`, apply `nodes_moved` as
in-place location updates, add `edges_added` as `EdgeData`, remove
`edges_removed`, and update `_id_to_index` / `_file_to_nodes` / `_file_importers`
from the delta. It must make **no FFI calls and touch no session or snapshot** —
pure function of the graph and the delta (§5.3 / Phase 4.1). Accept either a
pythonized dict or a typed mirror model; a dict is fine for Phase 2.

> Keep it pure now so Phase 4 adopts it verbatim. The temptation to "just read
> one more thing off the session" to fix a parity gap is the exact coupling
> Phase 4 exists to remove — fix the *producer* instead.

**(d) Turn the parity tests green.** The Phase 0 parity tests
(`test_final_parity_oracle.py` and any Phase 2–4-tagged parity cases) call
`assert_parity(session)` (`parity_oracle.py:250`). With (b) and (c) present they
stop raising `ParityOracleNotReady` and become real assertions. Remove the
`xfail` markers on the parity cases as each goes green (never before — an xfail
that starts passing with the marker still on is itself a failure under
`xfail-strict`).

**Where / how — the iteration loop.**
```bash
# 1. rebuild so Python sees the new native producer + accessor
devenv shell -- build
# 2. run the parity suite verbosely; read the comparator's node/edge diff
devenv shell -- pytest src/tyo3/tests/test_final_parity_oracle.py -q --no-cov -rA
```
The comparator prints a bounded, readable diff split by tier: a `structural
parity mismatch` block ("nodes only in expected/actual", per-node structural
payload diffs, "edge set (relation) mismatch") that **fails the test**, and a
`cosmetic parity mismatch` block (cosmetic node fields, edge multiset `x1 vs x0`)
that under the default `cosmetic="warn"` is **logged, not fatal**. **Drive the
structural block to empty** — each line is a precise instruction: a missing node
means an id-format or extraction gap; a structural payload diff means a
DTO/applier field mismatch (often `file` normalisation, `kind`, or `content_hash`);
an `edge set (relation)` line means a reference/inheritance resolution produced or
dropped a relationship. Fix the *producer*, rebuild, repeat. Then read the cosmetic
warnings: land them where cheap (they're usually `selection_range`,
`content_hashes`, or a reference occurrence-range mismatch), and for any you
consciously accept, note *why* — those are the only diffs allowed to remain.

> Tip: to enforce full byte-for-byte equality temporarily (e.g. to audit the
> cosmetic tier), call `assert_parity(session, cosmetic="strict")`. Keep the
> committed parity tests on the default so a benign cosmetic diff never blocks the
> series.

**Verify (the phase's parity acceptance).**
```bash
devenv shell -- build
devenv shell -- pytest \
  src/tyo3/tests/test_final_parity_oracle.py \
  src/tyo3/tests/test_graph*.py \
  -q --no-cov -rA
```

> Commit here: `feat: emit native code delta alongside legacy build; assert parity`.

---

## 5. Acceptance — the Phase 2 gate

Run exactly what the plan's Phase 2 "Acceptance" lists, all via devenv:

```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml code_layer code_delta entity

devenv shell -- build   # ensure pytest imports the freshly built extension

# the parity suite, green, with the legacy build still authoritative
devenv shell -- pytest src/tyo3/tests/test_final_parity_oracle.py -q --no-cov -rA
```

Then the full milestone gate (run the suites in the background; ~10–15 min):

```bash
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml
```

**Exit criteria (all must hold):**
- A native `CodeLayer` and `CodeDelta` exist; the commit produces the delta
  inside the lock, after reconciliation, and stores the next layer on the head.
- `PyTyProject.full_code_delta()` returns a full, pythonizable code delta for the
  current state.
- `CodeGraph.apply_code_delta` is a **pure** applier (no FFI, no session/snapshot
  access) and the parity oracle's `native_delta_graph` uses it.
- **Structural parity is exact**: for every parity fixture, the native-delta
  graph equals the legacy read-surface graph in the node set, every *structural*
  node field, and the edge *relation* set — `assert_parity` raises no
  `AssertionError` from the structural tier. Any remaining *cosmetic* diff is
  understood and documented (ideally there are none). No `ParityOracleNotReady`
  and no remaining `xfail` on a parity case.
- **The legacy build is still authoritative** — `session.py`'s post-commit path
  still calls `CodeGraph.build`; no read-surface code was deleted. (That is
  Phase 4.)
- Both full suites stay green.

---

## 6. Pitfalls specific to this phase

- **Forgetting `devenv shell -- build` before the parity pytest.** Phase 2 is the
  first phase where Python must observe new Rust. A green `cargo test` does not
  refresh the compiled extension. If parity "won't change no matter what you
  fix," you skipped the rebuild.
- **Weakening the *structural* tier to make progress.** The structural tier
  (node set, structural fields, edge relation set) is the safety net Phase 4
  deletes the legacy build on; never narrow it or move a field out of it to dodge
  a real diff. (The *cosmetic* tier is meant to be downgradeable — that's the
  point of the split — but reclassify a field only deliberately, with a comment
  and a self-test, never silently.)
- **Qualified-name / durable-id format drift.** The three name forms (analysis
  dotted, registry `::`-with-file-prefix, graph-node compound for nested
  entities) are the #1 parity break. Align to `graph/identity.py` and
  `graph.py:335-365` exactly; do not invent a fourth form.
- **`selection_range` left `None`.** The legacy entity node always sets
  `selection_range` (`graph.py:346`); if your `name_range` plumbing (2.1) is
  incomplete, the comparator flags a per-node payload diff and inheritance edges
  also vanish. One missing field, two symptoms.
- **File-path normalisation mismatch** (absolute vs. project-relative POSIX).
  Normalise once; the comparator compares `file` on every node and as part of
  every reference-edge key.
- **Combining the inheritance passes.** Computing overrides per-entity instead of
  after *all* inherits edges makes override edges depend on processing order;
  parity may pass on small fixtures and fail on a multi-level hierarchy. Keep the
  two passes separate (step 2.4.4).
- **Letting the producer read the session/snapshot to "patch" a parity gap.**
  That re-introduces the coupling Phase 4 removes. The producer reads only
  `TyProjectState` + the registry; the applier reads only the graph + the delta.
- **Touching the authoritative path.** Do not switch the post-commit hook to the
  native delta in Phase 2. Emit *alongside*. Cutover is Phase 4, gated by green
  parity.
- **`content_hashes` assumptions.** It is a *cosmetic*-tier field, so a mismatch
  warns rather than fails — but match whatever the legacy build actually emits
  (today `{}` unless the read surface supplies one, `graph.py:348`) anyway. Do not
  invent a richer dict the legacy side never produced; an accepted cosmetic diff
  must be a *deliberate* one, not drift.

---

## 7. Suggested commit sequence for the phase

1. `feat(entity): structural fields (file, full_range, name_range, qualified_name)` (2.1)
2. `feat(rust): CodeLayer (nodes/edges/reverse_deps) + head-state field` (2.2)
3. `feat(dto): code-delta wire contract (nodes/edges/moved)` (2.3)
4. `feat(rust): native code-delta producer (phased, parity-targeted)` (2.4)
5. `feat: emit native code delta alongside legacy build; assert parity` (2.5)

This maps to the plan's single commit
`feat(rust): native code layer + code delta behind the parity oracle`; squash on
landing if the series is preferred as one reviewed commit.

Every commit passes its focused `cargo`/`pytest` slice; the last one passes the
parity suite and both full suites (the milestone gate). Remember: **all of it
through `devenv shell --`.**

---

## 8. What Phase 2 deliberately leaves for later (so you don't over-reach)

- **Cutover** — making the native delta authoritative and deleting the Python
  read-surface build, the priming routine, and read-side writes — is **Phase 4**.
  Phase 2 only proves equivalence.
- **The id-level public delta** (`CommitDelta` with `created_ids/changed_ids/
  deleted_ids/moved/affected_ids`, the nested `code_delta`, `touched_files`) is
  **Phase 3**. Phase 2's `CodeDeltaDto` is designed to *nest* into it; do not
  build the public id-level delta yet.
- **The transactional staging/rollback** of the code-layer update is **Phase 5**.
  Phase 2 places the code-layer update in the right spot in the commit; Phase 5
  makes the whole commit stage-then-publish with rollback.

Keeping these out of Phase 2 is what keeps the suite green at every commit and
keeps the high-risk producer change isolated behind the parity oracle.
