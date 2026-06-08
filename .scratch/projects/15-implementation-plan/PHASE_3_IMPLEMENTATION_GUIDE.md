# Phase 3 — The id-level commit delta

> A step-by-step execution guide for **Phase 3** of
> `REFINED_IMPLEMENTATION_PLAN.md`. Read that plan's "Phase 3" section and §5.4 /
> §5.5 of `REFINED_IMPLEMENTATION_CONCEPT.md` once before starting — this guide
> assumes that vocabulary (DurableId, ContentHash, reconciliation, the
> reverse-dependency index, the delta's no-miss / no-over-fire invariants) and
> turns it into concrete edits against the code as it exists today.
>
> **Phase 3 depends on Phases 1 and 2.** Phase 1 made committed generations
> complete and reconciled identity at open. Phase 2 added the native `CodeLayer`
> (nodes + edges + **`reverse_deps`**) and the `CodeDeltaDto`, produced inside the
> commit and proven structurally parity-equal to the legacy build (via the parity
> oracle's strict tier). Phase 3 reshapes the
> *public per-write result* from path-shaped to **entity-identity-shaped**, nests
> the Phase 2 code delta inside it, and computes the transitively-affected closure
> from the reverse-dep index Phase 2 built. If either earlier phase's milestone
> gate is not green, stop and finish it first.

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
  devenv shell -- cargo test --manifest-path rust/Cargo.toml identity
  ```

The project defines custom scripts inside `devenv.nix` — `build`, `tests`,
`test-rust`, `test-fast`, `clean`, `status`, etc. When this guide says "rebuild
the extension," it means `devenv shell -- build` (which runs maturin so Python
sees the freshly compiled Rust). **A pure `cargo test` does not refresh the
compiled extension Python imports.** Phase 3 changes the *shape of the value
every write method returns*, and the Python contract test
(`test_final_commit_delta_contract.py`) calls into the native commit through the
session — so after **every** Rust change the Python suite must observe, run
`devenv shell -- build` before the pytest gate. Forgetting this is the single
most common way to chase a phantom failure (the test sees the *old* path-shaped
result because the extension wasn't rebuilt).

The suites are slow. Allow **~10–15 minutes** for a full run; launch full suites
in the background and give them time rather than assuming a hang.

> Throughout the rest of this document, **assume every `cargo`, `pytest`,
> `maturin`, `ruff`, and project-script invocation is prefixed with
> `devenv shell --`**, even where a line is abbreviated for readability.

---

## 1. What Phase 3 changes, and why

**Goal (from the plan):** the public per-write delta becomes
entity-identity-shaped (§5.4, §5.5). One `CommitDelta` per write carries
`created_ids / changed_ids / deleted_ids`, **structured** `moved[{id, old, new}]`,
`authored_ids`, `affected_ids` (the transitive closure under reverse-deps), the
**nested Phase 2 `code_delta`**, `touched_files` (metadata only), and `rescan`.

**The situation today** (confirmed in the current code):

- Every write method returns the **path-shaped** `SyncResultDto`
  (`rust/src/dto/sync.rs:14`). Its `created` / `changed` / `deleted` are **file
  path strings** (`sync.rs:17-19`), `moved` is a vector of **qualified-path
  strings** (`sync.rs:21`), and only `needs_review` / `orphaned` / `authored*`
  are durable ids — all flattened into untyped `Vec<String>` (§6.3 defect #4).
  A single file with two changed entities cannot be distinguished; a move is a
  string, not `{id, old, new}`.
- The Rust commit bodies (`commit_head` `project.rs:1435`; `sync_path_inner`
  `:1478`; the watcher fold `:1548`; authored/discard) all build a
  `SyncResultDto` from `run_identity_reconciliation` (`project.rs:1373`), which
  returns the path-shaped `IdentityDelta` (`project.rs:1396-1428`):
  - `moved` is built by mapping each moved id back to its **new qualified_path**
    string (`project.rs:1396-1400`) — losing the old location and the id;
  - it never computes a precise `changed` set (the caller passes path-level
    `created/changed/deleted` straight through, `commit_head` `:1438-1440`);
  - it never computes an affected closure at all.
- The reconciliation engine (`rust/src/identity.rs`) already classifies each
  entity into a `Binding` (`identity.rs:471`): `Exact` / `Moved { id, old_path }`
  / `Struct` / `Minted`, and tracks `retired` anchors
  (`Reconciliation`, `identity.rs:460`). But:
  - `Reconciliation::changed()` (`identity.rs:523`) returns **all `Exact`
    bindings**, changed or not — its own comment admits the imprecision. This
    violates §5.4 **no-over-fire** (an unchanged entity must not appear in
    `changed`).
  - `Binding::Moved` carries only `old_path` (`identity.rs:475`), not the new
    location or the files, so a structured `{id, old, new}` cannot be built.
  - The pass **rebinds anchors in place** (`reconcile_impl` →
    `registry.rebind`, `identity.rs:149`), overwriting the old content hash
    *before* anyone can compare it to the new one. To classify changed-vs-
    unchanged precisely you must capture the old hash *during* the pass.
- The Python side mirrors the path-shaped result as `SyncResult`
  (`src/tyo3/models/analysis.py:82`), validated by every write method in
  `session.py` (`edit` `:950`, `edit_many` `:965`, `sync_path` `:997`, `discard`
  `:1012`, `sync_all` `:1027`, `poll_changes` `:1088`, `author` `:1239`).

**The fix this phase delivers:** reconciliation emits **ids and structured
moves** with a precise changed-set (hash-based, not path-based); the commit
computes the **affected closure** from the Phase 2 `reverse_deps`; and the public
result becomes a single id-level `CommitDelta` (Rust) mirrored as `CommitDelta`
+ `MovedEntity` (Python), with the Phase 2 `code_delta` nested inside it.

### Why this is the keystone of the spine

Phases 4–8 all consume this delta:

- Phase 4's pure graph applier consumes the **nested `code_delta`**.
- Phase 6's one post-commit path and the bus consume `affected_ids` /
  `touched_files` for scoped, ordered delivery.
- Phase 7's derived invalidation consumes `created_ids` / `changed_ids` /
  `deleted_ids` — the change that "turns the currently-inert invalidation back
  on" (§6.3 defect #5).

So the field names and meanings you fix here are a contract the rest of the
refactor is written against. Get **no-miss** and **no-over-fire** (§5.4) exactly
right; everything downstream inherits their correctness or their bugs.

### What is already in place from Phase 2 (you build on it, don't rebuild it)

- `rust/src/dto/code_delta.rs` — `CodeDeltaDto` (revision, rescan,
  nodes_upserted/removed/moved, edges_added/removed). **Phase 3 nests this as a
  field; it does not change its shape.**
- `rust/src/code_layer.rs` — `CodeLayer { nodes, edges, reverse_deps }` and
  `produce_code_delta(...)`. **Phase 3 reads `reverse_deps` for the affected
  closure; the producer itself is unchanged.**
- `HeadState.code_layer` (added in Phase 2.2) — the per-revision code layer.
- `Entity` now carries `file`, `full_range`, `name_range`, `qualified_name`
  (Phase 2.1) — useful when building structured `moved` (new file/location).

### Files in scope

| File | Role in Phase 3 |
|---|---|
| `rust/src/identity.rs` | **edit** — reconciliation emits ids + structured moves + a precise changed set; capture old hash before rebind (3.2) |
| `rust/src/dto/commit_delta.rs` | **new** — the id-level `CommitDelta` DTO + `MovedEntity` DTO (3.1) |
| `rust/src/dto/mod.rs` | **edit** — register + re-export `commit_delta` |
| `rust/src/code_layer.rs` | **edit** — add an `affected_closure(seeds)` helper over `reverse_deps` (3.3) |
| `rust/src/project.rs` | **edit** — commit bodies build a `CommitDelta`; compute the changed set + affected closure; nest the code delta (3.3) |
| `rust/src/dto/sync.rs` | reference — the old shape being replaced (keep a thin shim only if 3.4 needs it) |
| `src/tyo3/models/analysis.py` (or a new `models/delta.py`) | **edit/new** — `CommitDelta` + `MovedEntity` Python models (3.4) |
| `src/tyo3/session.py` | **edit** — write methods validate/return `CommitDelta` (3.4) |
| `src/tyo3/tests/test_final_commit_delta_contract.py` | the Phase 0 test that must go green this phase (do **not** weaken it) |

---

## 2. Working rules for this phase (non-negotiable)

1. **Write/await the failing test first, then implement until green.** Phase 0
   shipped `test_final_commit_delta_contract.py` under a module-level
   `xfail(strict=True)` (`test_final_commit_delta_contract.py:52-57`) because the
   fields (`changed_ids`, `created_ids`, structured `moved`, …) do not exist yet.
   Your job is to make those fields exist and carry the right values — which flips
   the strict-xfail to a *failure* and forces you to remove the marker.
2. **Honour the two §5.4 invariants literally.**
   - **no-miss**: if an entity's content/membership/existence changed at R, its
     `DurableId` appears in exactly one of created/changed/deleted (or `rescan`
     covers it).
   - **no-over-fire**: an entity whose content hash is unchanged at R **MUST NOT**
     appear in `changed`. (This is the bug in today's `Reconciliation::changed()`.)
3. **`changed` means the content hash changed — never "a path rebind happened,"
   and ids are never inferred from touched files.** Touched files are *metadata*
   (`touched_files`); they must not stand in for entity ids (§5.4).
4. **A move is reported distinctly from create+delete.** A `moved` entry (same id,
   new location, unchanged body) must not also appear in `created_ids` /
   `deleted_ids`, so content-hash-keyed caches stay hit (§5.4, §5.5 INV).
5. **Don't swallow errors.** If a precise per-entity delta cannot be computed,
   set `rescan = true` (the honest "rebuild everything" signal, §5.4) — do not
   silently emit an empty or approximate delta.
6. **Keep the code delta nested, not competing.** The Phase 2 `CodeDeltaDto` is a
   *field* of `CommitDelta`. Do not duplicate node/edge data at the top level.
7. **The legacy graph build is still authoritative** until Phase 4. Phase 3
   reshapes the *result value*; it does **not** flip the post-commit graph path or
   delete read-surface code. (That is Phase 4.) The nested `code_delta` is carried
   for Phase 4 to consume; in Phase 3 the Python applier may ignore it.
8. **Milestone gate after the phase** (both must pass, both via devenv):
   ```bash
   devenv shell -- pytest -q --no-cov
   devenv shell -- cargo test --manifest-path rust/Cargo.toml
   ```

---

## 3. Pre-flight — establish the red baseline

Confirm the starting state before changing anything, so you can prove your work
moved the needle.

```bash
# 1. Phases 1–2 are landed and the Rust core is green (incl. the Phase 2 layer).
devenv shell -- cargo test --manifest-path rust/Cargo.toml identity code_layer code_delta entity

# 2. The id-level delta contract test is RED in exactly the expected way:
#    the whole module is xfail(strict=True) because changed_ids/created_ids/
#    structured moved/affected_ids do not exist yet.
devenv shell -- pytest src/tyo3/tests/test_final_commit_delta_contract.py -q --no-cov -rA

# 3. The parity suite (Phase 2) is still green — your safety net is intact.
devenv shell -- pytest src/tyo3/tests/test_final_parity_oracle.py -q --no-cov
```

Record the output. The module-level strict-xfail on the delta-contract test is
your North Star: it exists precisely because the write result is still
path-shaped. By the end of Phase 3 every case in that file is a real, green
assertion and the marker is gone.

---

## 4. Step-by-step implementation

Do the steps in order. Each step lists **what**, **why**, **where**, and a
**verify** command (devenv-prefixed). Commit at the natural breakpoints noted.

### Step 3.1 — The native commit-delta DTO

**What.** Add a new DTO module `rust/src/dto/commit_delta.rs` with the id-level
delta and a structured moved entry:

```rust
#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct MovedEntityDto {
    pub id: String,                 // DurableId
    pub old_qualified_path: String,
    pub new_qualified_path: String,
    pub old_file: String,
    pub new_file: String,
}

#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct CommitDeltaDto {
    pub revision: u64,
    pub created_ids: Vec<String>,
    pub changed_ids: Vec<String>,          // same id, different content hash
    pub deleted_ids: Vec<String>,
    pub moved: Vec<MovedEntityDto>,
    pub authored_ids: Vec<String>,
    pub affected_ids: Vec<String>,         // closure of changed∪deleted via reverse_deps
    pub code_delta: crate::dto::CodeDeltaDto,   // Phase 2 structural delta, NESTED
    pub touched_files: Vec<String>,        // metadata only (the old path-level set)
    // lifecycle / coarse-change signals carried over from SyncResultDto:
    pub needs_review: Vec<String>,
    pub orphaned: Vec<String>,
    pub authored_needs_review: Vec<String>,
    pub authored_orphaned: Vec<String>,
    pub rescan: bool,
    pub project_changed: bool,
    pub custom_stdlib_changed: bool,
}
```

**Why.** §5.4 / §5.5. This is the single public per-write value. Every field
name matches the Python mirror exactly (3.4) and the
`test_final_commit_delta_contract.py` assertions
(`changed_ids`, `created_ids`, `deleted_ids`, `result.moved` with `.id` /
`old*` / `new*`, `result.rescan`, `result.project_changed`,
`test_final_commit_delta_contract.py:69-161`).

**Where / how.**
- Reuse `CodeDeltaDto` from `dto/code_delta.rs` (Phase 2) as the `code_delta`
  field — do **not** redefine it. This is the "nest, not compete" design Phase 2
  set up (`PHASE_2_IMPLEMENTATION_GUIDE.md` §2.3).
- Carry over `needs_review` / `orphaned` / `authored_needs_review` /
  `authored_orphaned` / `project_changed` / `custom_stdlib_changed` from
  `SyncResultDto` (`sync.rs:22-38`) — they are still meaningful and consumers use
  them. `authored_ids` is the new field for "ids authored this commit" (used by
  the `author` write path, §3.4 of the plan and Phase 6).
- Register the module in `dto/mod.rs` (`mod commit_delta;` +
  `pub use commit_delta::*;`, mirroring how `sync` is registered at
  `dto/mod.rs:15`,`:30` and how Phase 2 registered `code_delta`).
- **Keep `touched_files` strictly metadata.** It is the union of the path-level
  `created/changed/deleted` strings the write methods already produce
  (`commit_head` params, `project.rs:1438-1440`). It exists for file-interest bus
  matching (Phase 6.4) and human readability — never as an id substitute.

**Verify.**
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml commit_delta
```
Unit test: a `CommitDeltaDto` with a nested non-empty `code_delta` round-trips
through serde, and (if you add a `pythonize` smoke test) produces a dict whose
keys exactly equal the Python field names from 3.4.

> Commit here: `feat(dto): id-level CommitDelta DTO (+ structured MovedEntity), nesting code_delta`.

---

### Step 3.2 — Make reconciliation emit ids and structured moves

**What.** Teach `rust/src/identity.rs` to return, per the plan: minted → created
ids; retired → deleted ids; same id with a **different content hash** → changed
id; same id at a new location with the **same** hash → a structured `moved`
entry. **`changed` must mean "content hash changed," never "an exact-path rebind
happened."**

**Why.** §5.4 no-over-fire and §5.5. Today `Reconciliation::changed()`
(`identity.rs:523`) returns every `Exact` binding regardless of hash, and
`Binding::Moved` (`identity.rs:475`) drops the new location. Both must be fixed at
the source — and the fix belongs in `identity.rs` so `cargo test identity` covers
it (the phase's Rust acceptance).

**Where / how — the key obstacle and the recommended design.**

The obstacle: `reconcile_impl` **rebinds anchors in place** (via
`registry.rebind`, `identity.rs:149`), which overwrites each anchor's
`content_hash` with the new entity's hash. By the time the caller inspects the
registry, the *old* hash is gone — so changed-vs-unchanged cannot be recovered
after the fact. You must capture the comparison **during** the pass.

Recommended design — make the `Reconciliation` result self-describing so the
commit needs no second registry read:

1. **Carry the old hash on the binding.** Extend the relevant `Binding` variants
   so each non-mint binding records what it needs to be classified:
   ```rust
   pub enum Binding {
       Exact  { id: DurableId, old_hash: ContentHash },
       Moved  { id: DurableId, old_path: String, old_hash: ContentHash },
       Struct { id: DurableId, confidence: Confidence, old_hash: ContentHash },
       Minted { id: DurableId },
   }
   ```
   Populate `old_hash` from the anchor **before** `rebind` overwrites it (the old
   anchor is in scope at the rebind site in `reconcile_impl`).
2. **Add a precise classifier** that pairs each binding with its entity (bindings
   are already `Vec<(String, Binding)>` keyed by the entity's qualified_path, in
   entity order, `identity.rs:462`). Have it take the entity slice so it can read
   each entity's **new** `content_hash`, `qualified_path`, and `file` (Phase 2.1
   added `file`):
   ```rust
   pub struct ReconcileClasses {
       pub created: Vec<DurableId>,                 // Minted
       pub changed: Vec<DurableId>,                 // Exact/Struct where new_hash != old_hash
       pub deleted: Vec<DurableId>,                 // == self.retired
       pub moved:   Vec<MovedBinding>,              // structured: id + old/new path + old/new file
   }
   pub struct MovedBinding {
       pub id: DurableId,
       pub old_qualified_path: String,
       pub new_qualified_path: String,
       pub old_file: String,
       pub new_file: String,
   }
   impl Reconciliation {
       pub fn classify(&self, entities: &[Entity]) -> ReconcileClasses { … }
   }
   ```
   - **created** = all `Minted` ids.
   - **changed** = `Exact`/`Struct` bindings whose paired entity's
     `content_hash != old_hash`. An `Exact` with an unchanged hash is **omitted**
     (no-over-fire). *Decision to record:* a `Struct` bind is a low-confidence
     re-bind; treat it as `changed` (its body almost always differs) **and** keep
     it in `needs_review` (it already is, `identity.rs:466`).
   - **moved** = `Moved` bindings → `{ id, old_qualified_path: binding.old_path,
     new_qualified_path: entity.qualified_path, old_file, new_file }`.
     - `new_file` = the entity's `file` (Phase 2.1).
     - `old_file` = the file component of `old_path`. The registry stores
       `qualified_path` as `file::qualified_name` (see `entity.rs` qualified_path
       construction, referenced in `PHASE_2_IMPLEMENTATION_GUIDE.md` §2.4); split
       on the first `::` to recover the old file. Add a tiny helper +
       unit test so the split is the single authority (don't open-code it twice).
     - A `Moved` binding is the §5.5 rule-2 case (same hash, different path), so by
       construction its hash is unchanged — **it does not also go in `changed`**.
3. **deleted** = the existing `retired` vector (`identity.rs:464`) — already the
   set of anchors that matched no entity. (Their authored records are marked
   `orphaned`, **never deleted**, by the existing lifecycle path — that behaviour
   stays; see `compute_authored_lifecycle`, `project.rs:1283`/`:1406`. The
   delta-contract test asserts exactly this, `test_final_commit_delta_contract.py
   :129-149`.)

> **Determinism (§5.12).** Keep the output ordered: bindings are already produced
> in a sorted, deterministic pass (`identity.rs:542` pre-sort). Preserve that
> order in `ReconcileClasses` so the delta is reproducible across runs/machines.

> **Backward compatibility within the crate.** The old `.changed()` /
> `.moved()` / `.minted()` helpers (`identity.rs:489-533`) are now superseded by
> `classify`. Either keep them as thin wrappers over `classify` for any remaining
> internal callers, or migrate callers and delete them. Do not leave two
> definitions of "changed" with different meanings.

**Verify.**
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml identity
```
Add `#[cfg(test)]` unit tests in `identity.rs` (reuse the existing reconcile test
harness, e.g. the fixtures around `identity.rs:987`,`:1376`):
- **edit one function** → its id in `changed`, not in `created`/`deleted`/`moved`;
- **whitespace-only edit** (same hash) → `changed` is **empty** (no-over-fire);
- **two functions edited in one file** → two distinct ids in `changed`;
- **pure move** (same hash, new path) → exactly one `MovedBinding` with correct
  `old_file`/`new_file`; the id is in neither `created` nor `deleted`;
- **deleted entity** → its id in `deleted` (== `retired`).

> Commit here: `feat(identity): precise hash-based changed set + structured moves`.

---

### Step 3.3 — Compute the changed set and affected closure during the commit

**What.** In each commit body, after reconciliation, build a `CommitDeltaDto`:
classify with `Reconciliation::classify` (3.2), nest the Phase 2 code delta, and
compute `affected_ids` as the transitive closure of `changed ∪ deleted` under the
code layer's `reverse_deps`.

**Why.** §5.3 step 6 ("compute the Delta") and §5.4 (the affected set "MUST be
computable from the reverse-dependency index … without a global scan"). The
closure belongs *inside* the commit, against the just-updated layer, so the delta
is consistent with the revision being published.

**Where / how.**

**(a) Add the closure helper to `code_layer.rs`.** The reverse-dep index Phase 2
built is `reverse_deps: Map<DurableId, Set<DurableId>>` (target → sources that
reference/import/inherit it). Add:
```rust
impl CodeLayer {
    /// Transitive closure of `seeds` under inbound edges (who-depends-on-me),
    /// excluding nothing — the result includes the seeds themselves.
    pub fn affected_closure(&self, seeds: &BTreeSet<DurableId>) -> BTreeSet<DurableId> {
        // BFS over reverse_deps; visit each id once. Deterministic via BTreeSet.
    }
}
```
- BFS/worklist; mark-visited so cycles terminate. Use `BTreeSet` so the output is
  deterministic (§5.12).
- Unit-test it in `code_layer.rs`: a→b→c reverse chain, closure of `{c}` returns
  `{a,b,c}`; closure of an isolated id returns just itself.

**(b) Wire it into `run_identity_reconciliation` / the commit bodies.** Today
`run_identity_reconciliation` (`project.rs:1373`) returns the path-shaped
`IdentityDelta`. Reshape its output (or add a sibling that returns the id-level
pieces) so it surfaces `ReconcileClasses`. Then in `commit_head`
(`project.rs:1435`) and the other commit bodies (`sync_path_inner` `:1478`, the
watcher fold `:1548`, the authored/discard/sync_all paths) assemble the
`CommitDeltaDto`:

```
let classes = recon.classify(&entities);                 // 3.2
let code_delta = produce_code_delta(...);                // Phase 2 (already called here)
let mut seeds: BTreeSet<DurableId> = classes.changed ∪ classes.deleted;
let affected = head.code_layer.affected_closure(&seeds); // (a)
CommitDeltaDto {
    revision: head.store.revision().0,
    created_ids: classes.created,
    changed_ids: classes.changed,
    deleted_ids: classes.deleted,
    moved:        classes.moved (→ MovedEntityDto),
    authored_ids: …,                  // the ids authored this commit (author path); [] otherwise
    affected_ids: affected,
    code_delta,                       // nested
    touched_files: created ∪ changed ∪ deleted (the old path-level strings),
    needs_review, orphaned, authored_needs_review, authored_orphaned,
    rescan, project_changed, custom_stdlib_changed,
}
```

**Closure subtlety — which layer's `reverse_deps`?** Dependents of a **deleted**
id existed in the layer *before* this commit; dependents of a **changed** id
exist in the *new* layer (and usually the old too). The robust choice: compute
the closure over a `reverse_deps` that covers both — i.e. seed from the **old**
layer for deleted ids and the **new** layer for changed ids, or simply union the
reverse-dep neighbourhoods of both layers before the BFS. Phase 2 stores the next
layer back into `head.code_layer` inside the commit
(`PHASE_2_IMPLEMENTATION_GUIDE.md` §2.5(a)); capture the *prior* `head.code_layer`
before that store if you need the old reverse-deps for deleted ids. Add a test
(deleting a base class still reports its subclasses in `affected_ids`).

**Coarse change → `rescan`.** Keep the existing `rescan` plumbing
(`commit_head` param, `project.rs:1441`; `sync_all` sets it). When `rescan` is
true the precise per-id delta is unknown: it is acceptable for `changed_ids`
etc. to be empty and `rescan=true` to mean "rebuild everything" (§5.4). The
contract test asserts a project-config sync sets `rescan` **or**
`project_changed` (`test_final_commit_delta_contract.py:155-164`) — make sure
`sync_path("pyproject.toml")` still routes through the project-changed/rescan
path it does today.

**Replace `SyncResultDto` at the return type.** Change the commit bodies and the
PyO3 write methods to return `CommitDeltaDto` instead of `SyncResultDto`. Search
for every `dto::SyncResultDto {` construction (`project.rs:1453`,`:1513`,`:1562`,
`:1655`,`:2001`,`:2044`) and the `pythonize` of each at the PyO3 boundary, and
convert them. If an internal caller still needs the path-shaped value, prefer
deriving it from `CommitDeltaDto.touched_files` rather than keeping two parallel
results.

**Verify.**
```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml identity code_layer project
```

> Commit here: `feat(commit): build id-level CommitDelta + affected closure inside the commit`.

---

### Step 3.4 — Mirror the delta in Python

**What.** Add the `CommitDelta` and `MovedEntity` pydantic models on the Python
side and have every write method return them.

**Why.** §5.4. The Python contract test consumes these field names directly
(`result.changed_ids`, `result.created_ids`, `result.deleted_ids`,
`result.moved[*].id` / `.old_*` / `.new_*`, `result.rescan`,
`result.project_changed`).

**Where / how.**
- Add the models (a new `src/tyo3/models/delta.py` is cleanest, re-exported from
  `models/__init__.py`; or alongside `SyncResult` in `models/analysis.py:82`):
  ```python
  class MovedEntity(BaseModel):
      model_config = ConfigDict(from_attributes=True)
      id: str
      old_qualified_path: str
      new_qualified_path: str
      old_file: str
      new_file: str

  class CommitDelta(BaseModel):
      model_config = ConfigDict(from_attributes=True)
      revision: int
      created_ids: list[str]       = Field(default_factory=list)
      changed_ids: list[str]       = Field(default_factory=list)
      deleted_ids: list[str]       = Field(default_factory=list)
      moved: list[MovedEntity]     = Field(default_factory=list)
      authored_ids: list[str]      = Field(default_factory=list)
      affected_ids: list[str]      = Field(default_factory=list)
      touched_files: list[str]     = Field(default_factory=list)
      needs_review: list[str]      = Field(default_factory=list)
      orphaned: list[str]          = Field(default_factory=list)
      authored_needs_review: list[str] = Field(default_factory=list)
      authored_orphaned: list[str] = Field(default_factory=list)
      code_delta: dict | None      = None     # Phase 4 will type this; a dict is fine for Phase 3
      rescan: bool = False
      project_changed: bool = False
      custom_stdlib_changed: bool = False
  ```
- **Use a default factory for every list/dict field** (never a shared mutable
  default) — this is called out explicitly in the plan (§3.4) and is the §12
  hygiene rule applied early.
- **Prefer the new model name** (`CommitDelta`) over re-using `SyncResult`, so the
  change in meaning is explicit. Update every write method in `session.py` to
  `CommitDelta.model_validate(native_result)` and the return annotation to
  `CommitDelta`: `edit` (`session.py:950`), `edit_many` (`:965`), `edit_virtual`
  (`:981`), `sync_path` (`:997`), `discard` (`:1012`), `sync_all` (`:1027`),
  `poll_changes` (`:1088`), `author` (`:1239`). Also update the post-commit
  helper signatures that take a `SyncResult` (`_invalidate_derived` `:753`,
  `_publish_delta` `:873`, `_apply_graph_delta` `:1103`) — for Phase 3 they can
  keep working off the same object; Phases 6–7 rewrite their bodies.
- **Compatibility shim — only if external callers require one.** If something
  outside this refactor still imports `SyncResult`, keep a thin, clearly
  `@deprecated` alias (or a `SyncResult` that is constructed from a `CommitDelta`'s
  `touched_files`), and note it in the change description. Do not keep it for
  internal convenience — internal code should move to `CommitDelta`.

**The Phase 2 parity oracle still works.** The oracle probes
`source._inner.full_code_delta()` for the *code* delta
(`parity_oracle.py:233-247`), independent of the commit result shape — so
reshaping the write result does not disturb parity. Re-run it to confirm.

**Verify (the phase's Python acceptance).**
```bash
devenv shell -- build
devenv shell -- pytest src/tyo3/tests/test_final_commit_delta_contract.py -q --no-cov -rA
```
Remove the module-level `@pytest.mark.xfail` (`test_final_commit_delta_contract
.py:52-57`) only once every case passes on its own (an xfail that starts passing
with the marker still on is itself a failure under `xfail-strict`).

> Commit here: `feat(session): mirror CommitDelta/MovedEntity in Python; write methods return it`.

---

## 5. Acceptance — the Phase 3 gate

Run exactly what the plan's Phase 3 "Acceptance" lists, all via devenv:

```bash
devenv shell -- cargo test --manifest-path rust/Cargo.toml identity

devenv shell -- build   # ensure pytest imports the freshly built extension

devenv shell -- pytest src/tyo3/tests/test_final_commit_delta_contract.py -q --no-cov -rA
```

Then the full milestone gate (run the suites in the background; ~10–15 min):

```bash
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml
```

**Exit criteria (all must hold):**
- Reconciliation emits **ids** and **structured moves**, with `changed` meaning
  "content hash changed" (no-over-fire) — proven by the `identity` unit tests and
  by `test_whitespace_only_edit_changes_nothing` / `test_edit_one_function_
  reports_its_id`.
- A two-function edit in one file reports **two** changed ids
  (`test_edit_two_functions_reports_two_ids`).
- A pure move reports **one structured `moved`** entry with old/new locations and
  appears in neither `created_ids` nor `deleted_ids`
  (`test_pure_move_is_structured_and_not_create_delete`).
- Deleting an entity reports its id in `deleted_ids`; its authored record is
  `orphaned`, not dropped (`test_delete_reports_id_and_orphans_authored`).
- A coarse (project-config) change sets `rescan` or `project_changed`
  (`test_coarse_change_sets_rescan`).
- `affected_ids` is the closure of `changed ∪ deleted` under the code layer's
  `reverse_deps` (Rust unit test).
- The public write result is one `CommitDelta` (Rust `CommitDeltaDto`, Python
  `CommitDelta`) with the Phase 2 `code_delta` **nested**, not duplicated.
- **No downstream code treats a path as a durable id**; `touched_files` is the
  only path-shaped field and is metadata.
- The Phase 2 parity suite stays green; the legacy graph build is **still
  authoritative** (cutover is Phase 4).
- Both full suites stay green; the delta-contract test's `xfail` marker is
  removed.

---

## 6. Pitfalls specific to this phase

- **Forgetting `devenv shell -- build` before pytest.** Phase 3 changes the
  returned value's shape; if the contract test still sees path-shaped fields, the
  extension wasn't rebuilt.
- **Over-firing `changed`.** The classic trap: re-using today's
  `Reconciliation::changed()` (all `Exact` bindings). A whitespace-only edit is an
  `Exact` bind with the **same** hash and must produce an **empty** `changed_ids`.
  Compare hashes; do not equate "exact-path rebind" with "changed."
- **Losing the old hash to in-place rebind.** `registry.rebind` overwrites the
  anchor's `content_hash`. Capture `old_hash` on the `Binding` *before* the
  rebind, or you cannot classify changed-vs-unchanged.
- **A move leaking into create/delete.** A `Moved` binding (same hash, new path)
  must be in `moved` only — never also in `created_ids`/`deleted_ids` — or
  content-hash-keyed caches (Phase 7) lose their hits and authored records churn.
- **Reconstructing `old_file` wrong.** It comes from splitting the anchor's
  stored `qualified_path` (`file::qualified_name`) on the first `::`, not from the
  entity. Make the split a single tested helper.
- **Wrong-layer reverse-deps for deletions.** A deleted id's dependents live in
  the *old* layer; seed the closure from the prior `head.code_layer` for deleted
  ids (capture it before Phase 2's commit-time store overwrites it).
- **Path-as-id regressions downstream.** After the reshape, grep the post-commit
  helpers (`_invalidate_derived`, `_publish_delta`, `_apply_graph_delta`) and any
  bus/derive code for assumptions that `created/changed/deleted` are paths. They
  are ids now; paths live in `touched_files`. (Phases 6–7 rewrite these bodies,
  but they must not crash in Phase 3.)
- **Duplicating the code delta.** Nest `CodeDeltaDto`; don't hoist node/edge data
  to the top level of `CommitDelta`.
- **Touching the authoritative graph path.** Do not flip the post-commit graph to
  the nested `code_delta` or delete read-surface code in Phase 3 — that's Phase 4,
  gated by the parity oracle.
- **Mutable default fields in the Python models.** Use `Field(default_factory=…)`
  for every list/dict; a shared `[]`/`{}` default is the §12 bug the plan tells
  you to pre-empt here.

---

## 7. Suggested commit sequence for the phase

1. `feat(dto): id-level CommitDelta DTO (+ structured MovedEntity), nesting code_delta` (3.1)
2. `feat(identity): precise hash-based changed set + structured moves` (3.2)
3. `feat(commit): build id-level CommitDelta + affected closure inside the commit` (3.3)
4. `feat(session): mirror CommitDelta/MovedEntity in Python; write methods return it` (3.4)

This maps to the plan's single commit
`refactor(delta): id-level commit delta with structured moves + affected
closure`; squash on landing if the series is preferred as one reviewed commit.

Every commit passes its focused `cargo`/`pytest` slice; the last one passes the
delta-contract test and both full suites (the milestone gate). Remember: **all of
it through `devenv shell --`.**

---

## 8. What Phase 3 deliberately leaves for later (so you don't over-reach)

- **Cutover** — making the nested `code_delta` authoritative, switching the
  post-commit graph to the pure applier, and deleting the read-surface build /
  priming / read-side writes — is **Phase 4**. Phase 3 only *carries* the code
  delta in the result.
- **The single staged transaction with rollback** (sidecar as a participant; no
  torn publish) is **Phase 5**. Phase 3 places the delta computation in the right
  spot in the commit; Phase 5 stages and rolls back the whole thing.
- **One Python post-commit path + non-blocking bus** is **Phase 6**. Phase 3
  reshapes the value those paths receive; it does not yet unify them or change bus
  delivery.
- **Id-level derived invalidation** is **Phase 7** — it will consume
  `created_ids`/`changed_ids`/`deleted_ids` from the delta you build here.

Keeping these out of Phase 3 is what keeps the suite green at every commit and
keeps the delta-shape change isolated from the cutover risk.
