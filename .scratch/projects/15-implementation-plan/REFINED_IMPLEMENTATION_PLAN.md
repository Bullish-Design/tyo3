# TyO3 — Implementation Plan (Spine Refactor)

> A step-by-step plan to refactor TyO3 so that **Rust owns committed truth** and
> **Python is a read-only projection plus integrations**. Read the companion
> `REFINED_IMPLEMENTATION_CONCEPT.md` once before starting — it defines the system,
> the vocabulary, the rules the implementation must satisfy (referenced below as
> §5.x), the shape of the code today, and the target architecture. You need nothing
> else: not the rest of the project's documentation, examples, or history.
>
> Section references of the form **§5.3** point at rules in the concept document.

---

## The principle this plan enforces

Every defect this refactor removes is a symptom of one root cause: ownership of
"what revision R contains" is split across the Rust lock and unsynchronised Python.
The fix is one rule, applied everywhere:

> **One revision = one native transaction, under one lock, over _complete_ content,
> producing _one_ id-level commit delta that already includes the code-layer
> structural change. Python holds only read-only projections of _published_
> revisions, plus integrations.**

---

## Working rules

1. Do the whole refactor on one branch (suggested name `spine-refactor`). Land it as
   one reviewed series; do not ship it half-done — intermediate phases deliberately
   leave the system in a transitional shape.
2. For each phase, **write the failing test first**, then implement until green.
3. Never weaken a test to make progress. Never swallow an exception unless the
   contract says absence is the correct outcome — and then leave a one-line comment
   saying why.
4. Run everything through the project's dev environment: prefix commands with
   `devenv shell --`. The suites are slow (allow ~10–15 minutes); run full suites in
   the background and give them time.
5. **Milestone gate after every phase:**
   - `devenv shell -- pytest -q --no-cov`
   - `devenv shell -- cargo test --manifest-path rust/Cargo.toml`
6. **Final gate (from the cleanup phases onward):**
   - `devenv shell -- cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings`
   - `devenv shell -- ruff check src`
   - `devenv shell -- ruff format --check src`

   If any command is missing from the dev environment, add it as part of the work and
   note it in the change description.

---

## The safety net: the parity oracle

The high-risk core of this refactor is moving code-graph production from Python into
Rust. The technique that makes it safe:

> While both implementations exist, **emit the new native code delta alongside the
> existing Python read-surface build, and compare the two graphs**. The comparison is
> **tiered**: a *structural* tier (the node set, the load-bearing per-node fields, and
> the edge *relation* set) is compared **strictly** and a mismatch is always fatal; a
> *cosmetic* tier (incidental node fields and the full edge multiset — reference
> occurrence ranges, roles, parallel multiplicities) is **reported but downgradeable**.
> The native delta does not become authoritative until the structural tier is green
> everywhere and any cosmetic divergence is a known, accepted one.

This lets the suite stay green at every commit and turns "did I reproduce the old
behaviour where it matters?" into a mechanical check instead of a judgement call. Build
the comparator once (Phase 0) and reuse it through the cutover (Phase 4).

**Why tiered, not flatly byte-for-byte.** Demanding byte-equality on *every* payload
field forces the Rust producer to reproduce the old Python builder's quirks bug-for-bug
on fields no graph query consumes (reference-edge occurrence ranges, parallel-edge
multiplicities, incidental node metadata) — the single hardest, lowest-payoff part of
the cutover. The structural tier keeps the mechanical-check safety net exactly where a
consumer would notice a regression; the cosmetic tier keeps a known-acceptable
divergence from blocking the whole refactor. Two corollaries:

- **The oracle proves `native == legacy`, not `native == correct`.** Both halves lean
  on the same analysis read surface for resolution, so they can share a blind spot and
  agree while both wrong. It is a regression net pinned to legacy behaviour — which is
  what a cutover wants — not a correctness proof.
- **Its value is dominated by input breadth, not field tightness.** Byte-equality over
  three tiny fixtures proves little. Feed it diamonds, re-exports, decorators,
  overloads, moves, and deletes; that is where parity actually buys safety. Spend effort
  on fixtures, not on tightening cosmetic fields.

The comparator (`src/tyo3/tests/parity_oracle.py`) already implements this:
`compare_graphs` returns a tiered report; `assert_graphs_equal(..., cosmetic="warn" |
"strict" | "ignore")` raises on any structural diff and, per mode, surfaces or enforces
cosmetic ones. `STRUCTURAL_NODE_FIELDS` / `COSMETIC_NODE_FIELDS` record the split.

---

## Phase map (dependency order)

```
Phase 0   invariant tests + parity oracle harness            (tests go red first)
Phase 1   content gate: make committed generations complete  (foundation)
Phase 2   native code layer + code delta (parity-only)
Phase 3   id-level commit delta (ids + structured moves + affected closure)
Phase 4   cutover: Python graph becomes a pure applier
Phase 5   single native commit() with staging + rollback
Phase 6   one Python post-commit path; non-blocking bus
Phase 7   derived layers: id-level invalidation, typed stores
Phase 8   read surface: eager / owned-lifetime convenience APIs
Phase 9   AST-canonical hashing
Phase 10  single config source
Phase 11  split the monolith files
Phase 12  zero warnings, typing, exception hygiene
Phase 13  end-to-end acceptance suite
```

Phases 1–4 are the spine and must be done in order. Phases 5–10 are largely
independent once the id-level delta exists. Phases 11–13 are cleanup and come last so
that file movement is easy to review.

---

## Phase 0 — Invariant tests first, plus the parity harness

Write these before changing any implementation. They encode the target contracts and
should fail (or `xfail-strict`) on the current code; each turns green in the phase
noted. Tag each test with its target phase in a comment.

### 0.1 Content authority — `test_final_content_spine.py` (→ Phase 1)
1. A snapshot pinned at R does not observe a file created on disk after R.
2. A snapshot at R keeps R's content for a file even after disk changes later.
3. Two snapshots pinned at the same R are identical even if disk changes between
   their construction.
4. Snapshot construction performs no project-content disk read. Prove this with a
   test seam (a counter or fault flag on the disk-read path) rather than timing.
5. Pinning an evicted revision raises a typed `RevisionEvicted` error.

### 0.2 Reads do not write — `test_final_no_read_side_writes.py` (→ Phase 4)
1. Reading `session.graph` does not change `session.head`.
2. `session.snapshot().graph()` does not change `session.head`.
3. The floating "latest" read surface's `check()` does not change `session.head`.

### 0.3 Delta contract — `test_final_commit_delta_contract.py` (→ Phases 2–3)
1. Editing one function returns that function's `DurableId` in `changed_ids`, not a
   file path.
2. Editing two functions in one file reports two distinct ids.
3. A whitespace-only edit returns empty `changed_ids`.
4. A pure move returns one `moved` entry `{id, old_location, new_location}` and does
   not report created+deleted for it.
5. Deleting an entity reports its id in `deleted_ids`; its authored records become
   orphaned, not deleted.
6. A coarse change sets `rescan = True`.

### 0.4 Transaction atomicity — `test_final_transaction_rollback.py` (→ Phase 5)
1. A forced identity-persistence failure leaves head, the registry, and the sidecar
   unchanged, and publishes no bus delta.
2. A forced authored-record persistence failure leaves head, the authored store, and
   the sidecar unchanged, and publishes no bus delta.
3. A forced code-layer update failure publishes no partial revision.
   - Use a native test-only fault-injection flag, not filesystem permission tricks;
     the goal is to prove rollback semantics, not OS behaviour.

### 0.5 Bus contract — `test_final_bus_contract.py` (→ Phase 6)
1. Each of `edit`, `edit_many`, `edit_virtual`, `sync_path`, `discard`, `sync_all`,
   `author`, and `poll_changes` publishes exactly one relevant bus delta when it
   commits.
2. Bus deltas are id-level and arrive in revision order.
3. A deliberately slow subscriber does not block the writer.
4. Config validation rejects a writer-blocking overflow policy.

### 0.6 Derived contract — `test_final_derived_contract.py` (→ Phase 7)
1. Derived invalidation receives `DurableId`s.
2. A move with unchanged hash reuses the cached artifact (cache hit).
3. A body change recomputes only the affected ids.
4. An eager recompute closes the snapshot it opened (no leak).
5. A generator failure leaves the last-good artifact intact and reports `failed`.

### 0.7 Hashing — `test_final_hash_ast.py` (→ Phase 9)
1. Formatting-only variations hash the same.
2. **Changing whitespace inside a string literal hashes differently.**
3. Identifier, literal, and control-flow changes hash differently.
4. Multi-line docstrings respect the include-docstrings policy; string statements
   that are *not* docstrings are never stripped.
5. Decorators, annotations, default values, overloads, nested definitions, and class
   bases are all part of the canonical hash.

### 0.8 The parity oracle harness (→ Phases 2–4)
Build a comparator that, given a project state, produces both (a) the legacy
Python read-surface graph build and (b) a fresh graph with the native code delta
applied, and compares them in two tiers:
- **Structural (strict, always fatal):** the node set keyed by `durable_id`, the
  load-bearing per-node fields (`durable_id, name, qualified_name, kind, file, range,
  content_hash, external`), and the edge *relation* set (the deduplicated
  `(source, target, kind)` triples).
- **Cosmetic (downgradeable):** incidental per-node fields (`selection_range,
  content_hashes, package`) and the full edge *multiset* (occurrence ranges, roles,
  originating files, parallel multiplicities) for relations present on both sides.

Expose it as a test helper the Phase 2–4 tests call: `compare_graphs` returns the
tiered report, and `assert_graphs_equal(..., cosmetic=...)` / `assert_parity(...,
cosmetic=...)` raise on any structural diff and surface (`"warn"`, the default),
enforce (`"strict"`), or ignore (`"ignore"`) cosmetic ones. An unclassified payload
field defaults into the *structural* tier, so nothing silently escapes the strict
check.

**Exit:** all Phase 0 tests are committed and failing/xfail-strict, each tagged with
its target phase.

---

## Phase 1 — Make committed generations complete (the content gate)

**Goal:** committed generations are the complete, authoritative record of every
revision, so snapshots never read disk (§5.1, §5.2).

Work in `rust/src/project.rs`, `rust/src/content.rs`, `rust/src/overlay.rs`.

### 1.1 Define "project-relevant content" once
There is already a predicate that decides whether a path matters (Python sources plus
the project config files `pyproject.toml`, `ty.toml`, `setup.cfg`, `setup.py`). Make
it the single authority for what belongs in a generation. Be explicit that the TyO3
sidecar's own `config.toml` is *not* analysed content and stays out of the analysis
generation.

### 1.2 Add disk ingest to the content store
Add native helpers so disk is read once, at commit time, into the revision-owned
generation:
- `ingest_project(root, filter) -> Revision` — walk the root once, read each relevant
  file once, intern a text document (carrying its content hash, §5.1); a relevant
  absent path becomes a tombstone.
- `apply_disk_batch(paths) -> Revision` and `apply_overlay_batch(changes) ->
  Revision` — one batch is exactly one revision; the retained generation for that
  revision is complete for all relevant content.

### 1.3 Seed content when the project opens
On open: load and validate config → create the content store → `ingest_project` into
the initial revision → build the live analysis database over that generation →
reconcile identity against it. Document whether the initial state is revision 0 or 1,
and make tests assert it. Opening must fully populate identity so that no later read
needs to trigger a write to populate it.

### 1.4 Delete snapshot disk pre-population
Remove the routine that walks the live filesystem during snapshot construction
(`pre_populate_generation`) and its call inside `build_frozen`. `build_frozen` now
receives an already-complete generation and builds the frozen overlay directly.
Snapshot capture returns to O(1) in content.

### 1.5 Keep the frozen overlay strict
The frozen overlay must never fall through to disk on a miss — a miss is "absent at
R." It may synthesise directory membership from the generation's keys, because that
derives membership from the generation, not from disk.

**Acceptance:**
- `devenv shell -- cargo test --manifest-path rust/Cargo.toml content overlay project`
- `devenv shell -- pytest src/tyo3/tests/test_final_content_spine.py src/tyo3/tests/test_mvcc_snapshots.py src/tyo3/tests/test_mvcc_concurrency.py -q --no-cov`

**Exit:** snapshot construction reads no project content from disk; the same revision
yields identical content and directory listings; capture is O(1).

---

## Phase 2 — Native code layer + code delta (parity-only)

**Goal:** Rust maintains the authoritative code-graph structure and produces a code
delta inside the commit. It is emitted *alongside* the existing Python build and
checked with the parity oracle; it is **not yet authoritative**. This is the
high-risk core — go slowly and lean on parity.

Write these modules fresh against the current types (do not resurrect any earlier
attempt).

### 2.1 Restore the structural fields the producer needs on `Entity`
The code producer needs each entity's file, full range, **name range** (the range of
the symbol's *name*, distinct from the full definition range), and qualified name.
Add these to `rust/src/entity.rs`. The name range matters: inheritance queries must
position the cursor on the class/def *name*, never on the `class`/`def` keyword or a
decorator, or supertype resolution silently returns nothing.

### 2.2 Add the native code layer (`rust/src/code_layer.rs`)
A `CodeLayer` holding:
- `nodes: Map<DurableId, NodeData>` where `NodeData` is small (§5.4 / hashing): id,
  kind, qualified name, file, range, content hash — **never** vectors or large text.
- `edges: Set<Edge>` with a deterministic ordering (derive `Ord`) so emission is
  reproducible.
- `reverse_deps: Map<DurableId, Set<DurableId>>` — target → sources that reference,
  import, or inherit from it.

Register the module and give the per-revision head state a `CodeLayer` field
alongside the existing authored-store field.

### 2.3 Add the code-delta wire contract (`rust/src/dto/code_delta.rs`)
DTOs that pythonize cleanly (field names match the Python mirror exactly):
- a node DTO: durable id, kind (lowercased), qualified name, file, range, optional
  content-hash hex (none for synthetic module/external nodes);
- an edge DTO: source id, destination id, kind
  (`containment`/`references`/`imports`/`inherits`/`overrides`), optional role,
  optional originating file and occurrence range for reference edges;
- a `CodeDelta`: revision, `rescan`, `nodes_upserted`, `nodes_removed`,
  `nodes_moved` (id + new location only, no edge churn), `edges_added`,
  `edges_removed`.

This contract will be folded into the Phase 3 commit delta — design it so it nests,
not so it competes.

### 2.4 The producer passes (all native, inside the commit, correctly phased)
Order matters; resolving edges before all nodes exist, or computing overrides before
the full inheritance chain is known, makes results depend on processing order.
1. Collect entities for the dirty scope and **materialise all nodes first**, before
   any edge resolution.
2. Containment edges.
3. Reference and import edges (from analysis occurrences), maintaining
   `reverse_deps`.
4. Inheritance in **two passes**: add *all* `inherits` edges for the dirty set first,
   then compute *all* `overrides` edges. A single combined per-entity pass is
   forbidden — override correctness would depend on the order entities are processed
   when an intermediate ancestor is also dirty.
5. Diff the freshly-produced full set against the current `CodeLayer` to emit a
   minimal incremental delta; provide a full-delta path for cold start and `rescan`.

### 2.5 Emit alongside and assert parity
Have the commit attach the produced code delta to its result. Parity tests apply it
to a fresh graph and compare against the legacy read-surface build with the Phase 0.8
comparator. The legacy build remains authoritative until Phase 4.

**Watch for (these are the real failure modes):**
- *Qualified-name format.* The analysis engine names entities in dotted form
  (`User.save`); the identity registry uses a `::`-joined form with a file prefix.
  The producer must emit the exact form the Python replica and registry expect, or
  edges resolve to the wrong nodes.
- *Reference target resolution* must match the legacy resolver case-for-case.
- *Inheritance cursor position* (see 2.1).
- *Borrow-checker friction* on the producer's captures of the head state and root.

**Acceptance:** `devenv shell -- cargo test --manifest-path rust/Cargo.toml
code_layer code_delta entity` plus the parity suite, all green, with the legacy build
still authoritative.

---

## Phase 3 — The id-level commit delta

**Goal:** the public per-write delta is entity-identity-shaped (§5.4, §5.5).

### 3.1 The native commit-delta DTO
Replace the path-shaped result with one structured delta:
```
CommitDelta {
    revision,
    created_ids:  [DurableId],
    changed_ids:  [DurableId],         // same id, different content hash
    deleted_ids:  [DurableId],
    moved:        [{ id, old_qualified_path, new_qualified_path, old_file, new_file }],
    authored_ids: [DurableId],
    affected_ids: [DurableId],         // transitive closure of changed∪deleted via reverse-deps
    code_delta,                        // the Phase 2 structural delta, nested
    touched_files: [path],             // metadata only
    rescan, project_changed, custom_stdlib_changed,
}
```

### 3.2 Make reconciliation emit ids and structured moves
Identity reconciliation (`rust/src/identity.rs`) returns: minted → created ids;
retired → deleted ids; same id with a different content hash → changed id; same id at
a new location with the *same* hash → a structured `moved` entry. `changed` must mean
"content hash changed," never "an exact-path rebinding happened," and ids must never
be inferred from touched files.

### 3.3 Compute the exact changed set and affected closure during commit
For the affected scope: extract entities → reconcile old vs. new registry → per
durable id compare old vs. new content hash → emit created / changed / moved /
deleted accordingly. Compute `affected_ids` as the closure of `changed ∪ deleted`
under `reverse_deps`. If a precise delta cannot be computed, set `rescan = True` and
let consumers treat it as "rebuild everything."

### 3.4 Mirror the delta in Python
Add the `CommitDelta` and `MovedEntity` models on the Python side. Use a default
factory for every list/dict field (never a shared mutable default). Prefer a new
model name over re-using the old result name, so the change in meaning is explicit;
keep a thin, clearly-deprecated compatibility shim only if external callers require
one.

**Acceptance:** `devenv shell -- pytest
src/tyo3/tests/test_final_commit_delta_contract.py -q --no-cov`;
`devenv shell -- cargo test --manifest-path rust/Cargo.toml identity`. No downstream
code treats a path as a durable id; moves are structured; a two-function edit in one
file reports two changed ids.

---

## Phase 4 — Cutover: the Python graph becomes a pure applier

**Goal:** the native code delta becomes authoritative; the Python graph stops
building from the read surface and stops triggering writes (§5.3, §5.9). This is
where the bulk of the old graph-construction code is deleted.

### 4.1 Add a pure applier to `CodeGraph`
`apply_code_delta(code_delta)`: upsert nodes from the DTO, add/remove edges from the
DTO, update the graph's indices (including its file-level reverse-dependency index)
from the DTO. It must make **no FFI calls, no read-surface reads, and touch no
session or snapshot** — it is a pure function of the graph and the delta.

### 4.2 Make the native delta authoritative and delete the legacy build
Switch the post-commit graph update to call `apply_code_delta` (revision-gated, so an
out-of-order apply is impossible). Then delete the read-surface construction in
`graph/graph.py`: the per-file symbol collection, node materialisation, occurrence-
based reference resolution, the inheritance and override passes, the full rebuild,
and the old delta-application internals.

### 4.3 Remove read-side writes
Delete the identity-priming routine and the flag that guards it. Reading the graph
must never call `sync_all` or otherwise advance the revision. Identity is already
reconciled at open (Phase 1.3) and on every commit (Phase 3), so it is always
populated before a graph read. If a snapshot graph build finds required identity
missing, it raises a typed build failure — it never mutates the session.

### 4.4 Build the snapshot graph from native state
A snapshot's `graph()` applies a native code delta computed over the snapshot's own
frozen database, not a read-surface walk, so the graph is consistent with the
snapshot's pinned revision. It must not mutate session state.

**Acceptance:** `devenv shell -- pytest
src/tyo3/tests/test_final_no_read_side_writes.py src/tyo3/tests/test_graph*.py -q
--no-cov` plus the full parity suite. No read accessor changes head; no priming flag
remains; the legacy read-surface build is gone.

---

## Phase 5 — One native `commit()` with staging and rollback

**Goal:** every write is one native transaction that either fully publishes or fully
rolls back, with the sidecar as a participant (§5.3, §5.10). Once publication is the
last in-lock step, no Python-side write lock is needed.

### 5.1 A single commit entry point
Funnel all write kinds (`edit`, `edit_many`, `edit_virtual`, `sync_path`, `discard`,
`sync_all`, `author`, watcher `poll_changes`) through one native function:
`commit(mutation) -> Result<CommitDelta, CommitError>`.

### 5.2 Stage all state before publishing
Inside the lock: stage the next generation, the analysis change events, the next
identity registry, the next authored state, and the next code layer; compute the
commit delta; write sidecar files to temporaries and fsync+rename them atomically;
apply the analysis change against the staged content; and **publish the revision
last**. If any step fails, return a typed error and leave every piece of public state
at the prior revision — head, retained generations, registry, authored store, and
sidecar all unchanged, and no bus delta enqueued.

### 5.3 Fix the authored-write ordering
The authored write currently publishes its revision before persisting the record and
rolls back only its in-memory map. Re-order it to stage → persist → publish, so a
persistence failure rolls the whole commit back.

### 5.4 Typed errors
Surface `SidecarWriteError`, `CommitFailed`, and `ReconcileAmbiguous` (alongside the
existing `RevisionEvicted`, `ProjectClosed`, and format-version errors). Do not map
commit failures onto a generic internal error.

**Acceptance:** `devenv shell -- pytest
src/tyo3/tests/test_final_transaction_rollback.py src/tyo3/tests/test_write_path.py
src/tyo3/tests/test_gate6_authored.py -q --no-cov`;
`devenv shell -- cargo test --manifest-path rust/Cargo.toml project authored
sidecar`. No failed write advances the revision; no sidecar error is swallowed; all
writers share one commit path.

---

## Phase 6 — One Python post-commit path; non-blocking bus

**Goal:** every write runs the same post-commit steps, so none can diverge, and the
bus never blocks the writer (§5.11).

### 6.1 Centralise the post-commit hook
Add one method:
```python
def _after_commit(self, delta: CommitDelta) -> None:
    self._invalidate_head_snap()
    self._apply_graph_delta(delta)     # the pure applier from Phase 4
    self._schedule_derived(delta)      # id-level, from Phase 7
    self._publish_delta(delta)
```
Every write method becomes: call the native op → validate the returned delta → call
`_after_commit(delta)` → return it. The previously hand-copied per-method sequences
collapse into this one path — which is what let one write method silently skip
publishing.

### 6.2 Publish from every write path
Including `discard` and `author`. The bus's revision-order delivery check, which
previously documented a guarantee the code did not hold, now backs a real one
(publication is the commit tail) — promote it from a warning to an assertion.

### 6.3 Remove the writer-blocking overflow policy
Config validation rejects any overflow policy that can block the writer. Supported
policies, all non-blocking:
- `coalesce` — union the affected sets of pending deltas (never hide an intervening
  change),
- `drop_and_mark_lagged` — drop and force the subscriber to rescan,
- `error_and_close` — mark lagged and close the subscription.

### 6.4 Deliver scoped, ordered, id-level deltas
The bus delta is a thin immutable wrapper over the commit delta. Interest matching:
an id interest matches `affected_ids`; a file interest matches `touched_files` plus
the files of affected ids; a layer interest matches touched layers; `rescan` matches
all.

**Acceptance:** `devenv shell -- pytest src/tyo3/tests/test_final_bus_contract.py
src/tyo3/tests/test_gate8_bus.py -q --no-cov`. Every write publishes; slow
subscribers never stall commits; deltas are ordered and id-level.

---

## Phase 7 — Repair derived layers

**Goal:** derived invalidation is precise and id-level, staleness is honest, and
stores report errors instead of hiding them (§5.7, §5.12).

### 7.1 Feed invalidation id-level inputs
Build the dirty set from the commit delta's `created_ids` and `changed_ids`, and the
deleted set from `deleted_ids` — durable ids, not the old path-shaped values. This is
the change that turns the currently-inert invalidation back on.

### 7.2 Prefer read-time staleness over mutable flags
For a derived layer and entity id at a snapshot: resolve the entity's content hash
under the layer's hash profile → form the store key `(input_hash, generator_version)`
→ if the artifact exists, it is fresh; if it is missing but a last-good exists, it is
stale/failed; if nothing exists, it is absent or blocks per policy. This makes
staleness a pure read-time comparison and removes the need for transaction-time
mutable derived state.

### 7.3 Fix the recompute snapshot lifetime
The derivation pass must use **one** pinned snapshot for both invalidation and eager
recompute and close it in a `finally`. Remove the path that opens a second snapshot
and never closes it.

### 7.4 Make store errors typed
A filesystem store returns "missing" only for a genuinely absent file and propagates
all other IO errors. The vector store stops swallowing query/write errors. A missing
optional backend stays a distinct typed "backend unavailable," separable from "not
found" and from "backend broken."

### 7.5 Complete the self-healing derived test
Un-skip the derived-layer self-healing test so it proves: an unrelated edit does not
recompute; a content change recomputes; a move reuses the artifact; a
generator-version bump uses a new key-space; and a failure keeps the last-good
artifact.

**Acceptance:** `devenv shell -- pytest
src/tyo3/tests/test_final_derived_contract.py src/tyo3/tests/test_gate5_derived.py -q
--no-cov`. Invalidation is id-level; no snapshot leaks; no remaining skip.

---

## Phase 8 — Read surface and convenience APIs

**Goal:** no read returns a view over a closed snapshot, and read paths do not hide
failures (§5.2, §5.12).

### 8.1 Remove closed-snapshot views
The convenience reads (`session.code`, `session.layer`, `session.entity`) currently
open a snapshot, take a lazy view, then close the snapshot before returning — so any
later access fails. Pick one shape and apply it consistently:
- *Preferred:* remove the lazy convenience reads; reads go through an explicit pinned
  snapshot: `with session.snapshot() as snap: snap.code.value(id)`.
- *Alternative:* the returned view owns its snapshot and is itself a context manager
  that closes it on exit.

Never return a lazy view tied to an already-closed snapshot.

### 8.2 Keep the floating "latest" view honest
The floating latest read surface is for warm single-layer reads only: no entity
accessor, no cross-layer diff, no pinned revision, no mutable graph reference. If it
keeps a `graph()` accessor, it returns a clearly non-canonical projection.

### 8.3 Stop swallowing read-surface errors
Layer views stop catching broad exceptions and returning `None`. Distinguish typed
absence from typed backend failure, graph-build failure, and format/config failure.

**Acceptance:** `devenv shell -- pytest src/tyo3/tests/test_gate7_read_surface.py
src/tyo3/tests/test_final_no_read_side_writes.py -q --no-cov`. No convenience API
returns closed lazy state; cross-layer reads are pinned or explicitly floating.

---

## Phase 9 — AST-canonical hashing

**Goal:** the content hash reflects meaning, not formatting (§5.6).

### 9.1 Replace line heuristics with an AST renderer
The current normaliser works on source text line by line and collapses whitespace
even inside string literals, so a meaningful change like `"a  b"` → `"a b"` does not
change the hash. Replace it: parse the entity source with the ruff parser, walk the
AST, and emit canonical tokens for node kind, identifiers, literals (with literal
*content* preserved exactly), signatures, annotations, decorators, bases, control
flow, and assignments. Exclude comments always; include or exclude docstrings per the
hash policy.

### 9.2 Define and test the policy precisely
Whitespace outside literals is ignored; trailing commas are ignored; docstrings are
included or excluded by policy; annotations, decorators, default values, and import
aliases are significant.

### 9.3 Keep width and stability
At least 128 bits, stable across machines, consistently encoded (hex).

**Acceptance:** `devenv shell -- pytest src/tyo3/tests/test_final_hash_ast.py -q
--no-cov`; `devenv shell -- cargo test --manifest-path rust/Cargo.toml hash entity`.
The implementation no longer claims more than it does.

---

## Phase 10 — Single config source

**Goal:** one validated config, surfaced from Rust; no silent Python fallback
(§5.12).

Rust already loads and validates the config. Python must consume the validated config
as JSON from the native side and stop re-reading the config file itself. Delete the
Python routine that parses the config TOML directly and falls back to defaults on any
exception. Expose the coordination settings (bus capacity and overflow, watcher
enabled and debounce) in the native config JSON. Keep any Python sidecar helper only
as a lightweight path facade if it is still needed.

**Acceptance:** invalid coordination config fails loudly at open instead of silently
reverting to defaults; the config and sidecar tests stay green.

---

## Phase 11 — Split the monolith files

Behavioural changes are done; now make the structure reviewable. Each split must be
behaviour-preserving — run the full suite after each.

### 11.1 Split the Rust project file
`rust/src/project.rs` mixes project open, the write transaction, snapshots, the
watcher, authored writes, conversions, and the PyO3 methods. Split into focused
modules: open, head state, commit, snapshot, watch, authored, and the PyO3 method
wrappers (which stay thin — parse arguments, call core logic, convert errors).
Identity and the code layer stay in their own modules.

### 11.2 Split the Python session file
`src/tyo3/session.py` mixes the public facade, the snapshot and latest views, the
post-commit hook, shared read wrappers, and the public exceptions. Split them, moving
protocols and models out of implementation modules to avoid import cycles. The public
`TyO3Session` stays a thin facade.

### 11.3 Split the Python graph file
`src/tyo3/graph/graph.py` mixes the applier, queries, diff, diagnostics, export, and
models. Split them. Since the graph is now a projection rather than a transaction
participant, name it accordingly (e.g. a snapshot/projection name) so its role is
unambiguous.

**Acceptance:** the full suite stays green after each split; public imports still
work; each module has a single clear responsibility.

---

## Phase 12 — Zero warnings, typing, exception hygiene

- Drive Rust warnings to zero and add `clippy --all-targets -- -D warnings` to the
  gate. Remove unused imports, dead variants, and confusing lifetime syntax; prefix
  intentionally-unused test bindings with `_`; use a narrow `#[allow(...)]` only with
  a one-line reason.
- Reduce untyped values in the session, the native type stubs, and the
  stores/generators; add typed models for native return payloads so wrappers
  validate less by hand.
- Replace mutable default arguments and fields with factories.
- Replace every broad `except Exception: pass` with a typed catch that yields a typed
  absence, logs with context, or re-raises a domain error; each remaining deliberate
  swallow gets a justifying comment.

**Acceptance:** `devenv shell -- ruff check src`,
`devenv shell -- ruff format --check src`, and
`devenv shell -- cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D
warnings` all clean.

---

## Phase 13 — End-to-end acceptance suite

Add `src/tyo3/tests/test_final_acceptance.py` exercising one project end to end:
open → pin an initial snapshot → author intent on an entity → read the code graph
from a pinned snapshot → configure a deterministic derived layer → edit one entity →
move another entity unchanged → delete one entity → read the old and new snapshots →
diff them → verify bus notifications → close and reopen → verify identity and authored
records survive → delete the derived cache and verify recompute → assert no source
files were modified.

Assertions: the same revision yields the same content; durable ids survive a cosmetic
edit and a move; the content hash changes only on a meaningful edit; derived artifacts
are keyed by content hash; authored records are present, needs-review, or orphaned as
appropriate; bus deltas are ordered and id-level; the snapshot diff agrees with an
independent rebuild; and no read accessor advanced head.

---

## Definition of done

- `devenv shell -- pytest -q` passes with no unexpected skips.
- `devenv shell -- cargo test --manifest-path rust/Cargo.toml` passes.
- `clippy -D warnings`, `ruff check`, and `ruff format --check` are all clean.
- No snapshot construction reads live project content from disk.
- No read accessor advances the revision.
- Every write is one native commit returning one id-level commit delta, followed by
  one shared post-commit path.
- A failed write fully rolls back; the sidecar is a commit participant; there is no
  torn publish.
- Bus delivery is ordered, scoped, and non-blocking, and every write path publishes.
- Derived invalidation is id-level and leaks no snapshots.
- Convenience reads return eager values or own their snapshot lifetime.
- Hashing is AST-canonical and treats string-literal content as significant.
- The end-to-end acceptance test proves the whole story.

## Suggested commit series

1. `test: add final invariant tests + parity oracle harness`
2. `refactor(content): complete generations; remove snapshot disk pre-population`
3. `feat(rust): native code layer + code delta behind the parity oracle`
4. `refactor(delta): id-level commit delta with structured moves + affected closure`
5. `refactor(graph): cut over to a pure applier; remove read-surface build + priming`
6. `refactor(commit): single native commit() with staging + rollback`
7. `fix(session/bus): one post-commit path; publish every write; non-blocking overflow`
8. `fix(derived): id-level invalidation; read-time staleness; close snapshots; typed stores`
9. `fix(read): eager / owned-lifetime convenience views; stop swallowing read errors`
10. `refactor(hash): AST-canonical hashing`
11. `refactor(config): single validated config source`
12. `refactor: split project / session / graph monoliths`
13. `chore: zero warnings, tighten typing, exception hygiene`
14. `test: final end-to-end acceptance scenario`

Each commit passes its focused tests; every third commit passes both full suites.
