# Gate 7 — The Unified Read Surface: cross-layer views, cross-revision diff, floating-latest: Implementation Guide

> Implements **REFINED_SPEC.md §10** in full (cross-layer consistency at a snapshot —
> the *combined* join, not the per-layer halves the earlier gates proved
> individually; §10.2.4 no-lock reads; and the §10.3 time-travel-diff acceptance) and
> **ARCHITECTURE phase 11** (cross-revision diff, per layer and combined, plus the
> floating "latest" fast path). It realises the §5.1 `Layer.at(snapshot) -> LayerView`
> protocol uniformly across code, derived, and authored layers.
>
> **Prerequisites:** `gate6-complete`. This gate is a **read-only consolidation**: it
> adds no write type, no generation, no change to the commit transaction. It consumes
> what the previous gates already pin at a revision:
> - Gate 3 step 9 — `Snapshot.graph()` (immutable code graph at R; id-join consistent),
> - Gate 5 step 7 — `Snapshot.derived(layer, id)` (content-hash resolved at R; honest
>   staleness) and the per-profile `content_hashes` on graph nodes (Gate 5 step 1),
> - Gate 6 step 6/8 — `Snapshot.authored(layer, id)` (value + derived status at R),
> - Gate 1/2 — the snapshot capture that pins content + identity registry at R
>   (`project.rs::snapshot`), and the floating `LatestView`/`head_view()` skeleton
>   (`src/tyo3/session.py:LatestView`, `project.rs::head_view`).
>
> **Audience:** an engineer new to the codebase. Same rules as Gates 1–6:
> `devenv shell -- <script>` for everything; one labelled commit per validated step
> (`gate7: step N — …`); never skip a validation; the suite is fully green before the
> tag.

## How to work in this repo

- Rust unit tests: `devenv shell -- test-rust`. Full suite: `devenv shell -- tests`.
- Rebuild after Rust changes: `devenv shell -- build`.
- Property tests (the Step 6 diff-parity fuzz): `devenv shell -- test-property`.
- Branch off `gate6-complete`; commit per validated step.

## The contract you are building toward (read first)

Three deliverables, all read-only:

1. **The cross-layer join (§10.2.2, as a unit).** For one `DurableId` at one pinned
   revision R, `snap.code(id)`, `snap.derived(layer, id)`, and `snap.authored(layer,
   id)` **all describe R**. The earlier gates proved each accessor reads-at-R *in
   isolation*; this gate proves they **agree with each other** for the same id across
   many head writes, and packages that as a single `EntityView` (`snap.entity(id)`).
   A `stale`/`needs_review` value inside the join is reported honestly (§10.2.3),
   never silently presented as fresh.
2. **Cross-revision diff (phase 11, §10.3).** Given two pinned snapshots `before` and
   `after`, a per-layer diff (`after.code.diff(before.code)`,
   `after.embedding_drift(before)`, authored diff) and a **combined** `SnapshotDiff`
   (`after.diff(before)`), keyed by `DurableId`, that says exactly which entities'
   code changed, which derived artifacts drifted, and which authored records changed —
   computed **only from the two pinned snapshots** and **matching an independent
   rebuild at R0 and R1** (the §10.3 acceptance).
3. **The floating "latest" fast path (phase 11).** Complete `LatestView` so the same
   per-layer reads are available *warm* over the live head for one-off glances,
   reusing the writer's memos — with an **honest boundary**: cross-layer consistency
   (§10) and diff are **snapshot** operations (they need a pinned R); `latest` serves
   warm single-layer glances and never promises an isolated multi-layer join.

The defining success property: **at a pinned R every layer agrees for any id; a diff
between two snapshots equals a diff between two fresh rebuilds at those revisions;
reads take no write lock (§10.2.4); a project with only the code layer behaves exactly
as Gate 3.**

## Design stance

This gate is **entirely Python** and **entirely read-only**. Every fact it needs is
already pinned in a `Snapshot` by Gates 1–6: the code graph (with `content_hash` and
per-profile `content_hashes` on nodes), the captured identity registry (status +
location), the content-addressed derived cache, and the captured authored store. A
diff is therefore a pure function of two `Snapshot`s — no head involvement, no native
changes beyond at most a couple of small enumeration accessors (Step 4/5). Keep it
that way: **if a diff or a join needs to touch the head, it is wrong** — that would
break §10.2.4 (no write lock) and §10.3 (diff must equal a rebuild from the pinned
revisions alone).

The one rigorous boundary to hold: **consistency is a property of a pinned revision,
not of the live head.** The cross-layer `EntityView` and every diff operate on
snapshots. `LatestView` is warm-but-floating: a single read is internally
cancellation-retried and correct, but two successive `latest` reads may straddle a
write, so `latest` does **not** offer the atomic multi-layer join. Session
convenience reads (`session.entity(id)`, `session.diff(...)`) are sugar over the
**internally-held head snapshot** (§9), which *is* consistent — not over `latest`.

## Target module layout

```
src/tyo3/
  layers/                    # NEW: the uniform LayerView surface (§5.1)
    __init__.py
    base.py                  # LayerView protocol + diff result types (Step 1)
    code.py                  # CodeLayerView over Snapshot.graph()      (Steps 1, 3)
    derived.py               # DerivedLayerView over Snapshot.derived() (Steps 1, 4)
    authored.py              # AuthoredLayerView over Snapshot.authored()(Steps 1, 5)
  models/
    view.py                  # NEW: EntityView (the cross-layer join)   (Step 2)
    diff.py                  # NEW: LayerDiff, CodeDiff, SnapshotDiff    (Steps 3–6)
  session.py                 # Snapshot.entity/.code/.layer/.diff;
                             #   LatestView per-layer warm reads;
                             #   session convenience sugar               (Steps 2–7)
src/tyo3/tests/
  test_gate7_read_surface.py # Step 0 + final acceptance
```

No Rust changes are expected; Steps 4–5 may add **small** native enumeration helpers
(`snapshot.authored_ids(layer)`, `snapshot.entity_ids()`) if the Python side cannot
already enumerate them from the pinned graph and authored store — prefer deriving from
the pinned graph where possible.

---

## Step 0 — Pin the read-surface + diff API with a failing test FIRST

**Goal.** Freeze the public API before building (mirrors Gate 3/5/6 Step 0). It
`xfail`s until Steps 2/6/7.

**Files.** `src/tyo3/tests/test_gate7_read_surface.py` (new).

**Build the fixture & test.** A small multi-file project with a config declaring one
derived layer (a deterministic in-process `python` generator, as in Gate 5 Step 0) and
one authored layer (`intent`, as in Gate 6 Step 0). Write three tests against the
intended API:

- **Cross-layer join.** `with session.snapshot() as snap:` → `ev = snap.entity(id)`
  exposes `ev.code` (the graph node), `ev.content_hash`, `ev.derived["upper"]`
  (a `DerivedValue`), `ev.authored["intent"]` (an `AuthoredValue` or None),
  `ev.status` — all at `snap.revision`.
- **Combined diff.** Capture `r0 = session.head`; edit one entity's body and author a
  note on another; capture `r1 = session.head`. With `b = session.snapshot(at=r0)` and
  `a = session.snapshot(at=r1)`: `d = a.diff(b)` reports the edited id under
  `d.code.changed`, the noted id under `d.authored["intent"].changed`, and the edited
  id under `d.derived["upper"].drifted`; unrelated ids appear nowhere.
- **Latest is warm, snapshot is consistent.** `session.latest.derived("upper", id)`
  returns a value warm; `session.entity(id)` (sugar over the head snapshot) returns a
  consistent `EntityView`.

**Validate.**
- `devenv shell -- tests -k gate7_read_surface` runs; record status (expected:
  failing/xfail).
- **Acceptance gate:** the test exists and names the intended API (`snap.entity`,
  `a.diff(b)`, `a.code.diff(b.code)`, `a.embedding_drift(b)`, `session.latest.*`,
  `session.entity`/`session.diff`); status recorded. Commit
  (`gate7: step 0 — pin read-surface + diff API, status=XFAIL`).

> Do not implement anything in Step 0. Its only job is to fix the contract.

---

## Step 1 — The uniform `LayerView` surface (§5.1)

**Goal.** Realise the §5.1 `Layer.at(snapshot) -> LayerView` protocol as one interface
that code, derived, and authored layers all implement, so the join (Step 2) and the
combined diff (Step 6) iterate layers uniformly instead of special-casing each.

**Files.** `src/tyo3/layers/base.py`, `code.py`, `derived.py`, `authored.py`.

**Build.**
```python
class LayerView(Protocol):
    """A single layer's revision-pinned view (§5.1). Bound to one Snapshot."""
    name: str
    origin: Literal["code", "derived", "authored"]
    def ids(self) -> Iterable[str]: ...                 # DurableIds present at R
    def value(self, durable_id: str) -> Any | None: ... # the per-id record at R
    def diff(self, other: "LayerView") -> "LayerDiff": ...  # self=after, other=before
```
- **`CodeLayerView`** wraps `snapshot.graph()`. `ids()` = node DurableIds (entities,
  excluding `<module>`/`<external>` synthetics per the layer's policy); `value(id)` =
  the `SymbolNode` (kind, qualified_name, file, range, `content_hash`,
  `content_hashes`).
- **`DerivedLayerView`** wraps `snapshot.derived(name, id)` for one declared derived
  layer. `ids()` = the entity ids the layer `applies_to` at R (from the pinned graph,
  filtered by `entity_kinds`); `value(id)` = a `DerivedValue` (Gate 5).
- **`AuthoredLayerView`** wraps `snapshot.authored(name, id)` for one declared authored
  layer. `ids()` = ids with a record `present`/`needs_review`/`orphaned` at R (the
  authored store enumerated at R); `value(id)` = an `AuthoredValue` (Gate 6).
- Each view is constructed lazily from a `Snapshot` and the layer's `session.config`
  entry. Add `Snapshot.code` (a `CodeLayerView` property) and `Snapshot.layer(name)`
  (dispatch to the right view by `config` origin) — these are the handles Step 3–6
  diff against.

**Validate.**
- Test: `snap.code.ids()` equals the graph's entity ids; `snap.code.value(id)` is the
  node.
- Test: `snap.layer("upper").value(id)` returns a `DerivedValue`;
  `snap.layer("intent").value(id)` returns an `AuthoredValue`.
- Test: `snap.layer("code").origin == "code"`, etc.; `layer("nope")` raises a typed
  error naming the undeclared layer.
- **Acceptance gate:** all pass. Commit (`gate7: step 1 — uniform LayerView surface (§5.1)`).

---

## Step 2 — `EntityView`: the cross-layer join (§10.2.2 as a unit)

**Goal.** Package the per-id, all-layer join at R into one object, and prove the layers
**agree with each other** for the same id (not just each reads-at-R in isolation), with
honest status (§10.2.2, §10.2.3).

**Files.** `src/tyo3/models/view.py`, `src/tyo3/session.py` (`Snapshot.entity`).

**Build.**
```python
@dataclass(frozen=True)
class EntityView:
    durable_id: str
    revision: int
    code: SymbolNode | None                 # None iff the entity is absent at R
    content_hash: str | None
    location: str | None                    # file::qualified_name at R (locate@R)
    status: Literal["active", "needs_review", "orphaned", "absent"]   # from registry@R
    derived: dict[str, DerivedValue]        # layer name → value (only configured layers)
    authored: dict[str, AuthoredValue]      # layer name → value (present/needs_review/orphaned)
```
- `Snapshot.entity(durable_id) -> EntityView`:
  1. `code = snap.code.value(id)`; `content_hash` and `location` from the node /
     `locate(id)` resolved against the snapshot's captured registry (§10.2.1 — **not**
     the head; the snapshot pins the registry).
  2. `status` from the captured registry anchor status (the same source Gate 6 derives
     authored status from — one source of truth).
  3. `derived` = `{L.name: snap.derived(L.name, id) for L in configured derived layers
     that apply_to(kind)}`.
  4. `authored` = `{L.name: snap.authored(L.name, id) for L in configured authored
     layers}` (omit `absent`).
  - Every sub-read resolves against `snap.revision` — there is **no head access**, so
    the whole view is one consistent revision by construction.
- A stale derived value carries `status="stale"` (Gate 5), surfaced unchanged in the
  join; a `needs_review`/`orphaned` authored value likewise — §10.2.3 honesty is
  inherited, not re-implemented.

**Validate.**
- Test (the §10.2.2 join): build an entity with code + a derived artifact + an authored
  note; `snap.entity(id)` returns all three at `snap.revision`; do **many** head writes
  (code *and* authored) afterward; re-read the *same held snapshot* → the `EntityView`
  is unchanged (every layer still describes R).
- Test (honest staleness in the join, §10.2.3): edit the entity with a `serving="stale"`
  derived layer; in a fresh snapshot the join's `derived[L].status == "stale"` until the
  scheduler finishes.
- Test (absent): `snap.entity("01-unknown")` → `status="absent"`, `code is None`, empty
  `derived`/`authored`.
- **Acceptance gate:** all pass. Commit (`gate7: step 2 — EntityView cross-layer join (§10.2.2)`).

---

## Step 3 — Code-layer diff (structural)

**Goal.** `after.code.diff(before.code)` reports the structural change between two
pinned code graphs — the "what did my edit do" view (§9 example, §10.3).

**Files.** `src/tyo3/models/diff.py`, `src/tyo3/layers/code.py`.

**Build.**
```python
@dataclass(frozen=True)
class CodeDiff:
    added:   frozenset[str]            # node ids in after, not before
    removed: frozenset[str]            # node ids in before, not after
    changed: frozenset[str]            # in both; content_hash differs
    moved:   frozenset[str]            # in both; location differs, content_hash same
    edges_added:   frozenset[tuple]    # (src, dst, kind, role)
    edges_removed: frozenset[tuple]
    def is_empty(self) -> bool: ...
```
- Implement `CodeLayerView.diff(before)` as a **set-delta over the two pinned graphs**,
  reusing the Gate-3 graph-equality comparator's normalisation (the comparator that
  proves incremental==rebuild already canonicalises node payloads and edge tuples — the
  diff is its set-difference form, not a new normalisation). Node identity is the
  `DurableId`; `changed` compares `content_hash`; `moved` compares `(file,
  qualified_name)` with an equal `content_hash` (cross-checks the delta's `moved`).
  - **Reconcile with the existing `CodeGraph.diff` (post-Gate-3N).** `CodeGraph` already
    ships a `diff(before) -> GraphDiff`, but `GraphDiff` is **coarser** — only
    `added_nodes`/`removed_nodes`/`added_edges`/`removed_edges`, no id-level
    `changed`/`moved` split. So Gate 7's `CodeDiff` is a genuine refinement, not a
    duplicate: build it on the same node/edge normalisation, but add the
    `changed` (same id, differing `content_hash`) and `moved` (same id + hash, differing
    location) partition the bus and the §10.3 acceptance need. Either extend `GraphDiff`
    or layer `CodeDiff` over it — do not fork the normalisation.
- Edges: diff the `(src_id, dst_id, kind, role)` tuple sets — **as sets**, sorted with
  a total key, never `repr`.
- **No head access.** The diff is a pure function of `before` and `after` graphs.

**Validate.**
- Test: edit one entity's body → `changed == {id}`, everything else empty.
- Test: add a file → its nodes in `added`; delete a file → `removed`; add a cross-file
  call → the edge in `edges_added`.
- Test: move a class unchanged → its id in `moved`, **not** in `added`+`removed`, no
  spurious edge churn.
- Test (parity, §10.3): the diff between two snapshots taken from a live session equals
  the diff between two snapshots whose graphs were built fresh-from-scratch at the same
  R0/R1 (build via `CodeGraph.build(snapshot(at=R), root=<project root>)` — post-Gate-3N
  `build` derives its delta from `source._inner.code_delta_full()` and a `Snapshot` has
  no `.root`, so the project root MUST be passed explicitly), proving the diff depends
  only on the pinned revisions.
- **Acceptance gate:** all pass. Commit (`gate7: step 3 — code-layer structural diff (§10.3)`).

---

## Step 4 — Derived drift (`embedding_drift` + generic layer diff)

**Goal.** Report which entities' derived artifacts changed between R0 and R1 — the
content-addressed analogue of "which embeddings drifted" (§8.3 as a cross-revision
diff, §9 `embedding_drift`).

**Files.** `src/tyo3/models/diff.py`, `src/tyo3/layers/derived.py`,
`src/tyo3/session.py`.

**Build.**
```python
@dataclass(frozen=True)
class LayerDiff:
    layer: str
    added:   frozenset[str]     # had no artifact key at R0, has one at R1 (new entity)
    removed: frozenset[str]     # had a key at R0, entity absent at R1
    drifted: frozenset[str]     # present at both; cache key (input_hash) differs
```
- `DerivedLayerView.diff(before)` compares, per id, the layer's **cache key at R0 vs
  R1** — i.e. the entity's `content_hashes[layer.hash_profile]` taken from each pinned
  graph node (Gate 5 step 1). A code-derived layer drifts exactly when its profile hash
  changes (so a cosmetic edit that the profile ignores does **not** drift — the §7.3
  payoff at the diff level; a moved-unchanged entity does **not** drift — §5.5.1). A
  **layer-derived** layer (e.g. `description_embeddings`) drifts when its upstream
  artifact key changes — compute from the upstream `LayerDiff`, walked in topo order.
- This reads **only** the two pinned graphs — **no artifact reads, no vector store, no
  head**. Drift means "a recompute would occur," computed without computing anything.
- `Snapshot.embedding_drift(before)` is sugar for `self.layer("embeddings").diff(before)`
  (only if an `embeddings` layer is configured; else a typed error). Provide the
  generic `snap.layer(name).diff(before)` for any derived layer.

**Validate.**
- Test: edit an entity's body → it appears in the derived layer's `drifted`; an
  unrelated entity does not; an *importer* whose own body is unchanged does **not**
  drift (the §8.3 no-over-fire property as a diff).
- Test (profile-aware): with a `structure` profile, a docstring-only edit does **not**
  drift; with a `semantic` profile (include_docstrings) it does.
- Test (move): move an entity unchanged → not in `drifted`.
- Test (layered): changing an entity drifts `descriptions` and, downstream,
  `description_embeddings`.
- Test (parity, §10.3): drift from live snapshots equals drift from fresh rebuilds at
  R0/R1.
- **Acceptance gate:** all pass. Commit (`gate7: step 4 — derived drift / embedding_drift (§8.3/§10.3)`).

---

## Step 5 — Authored diff

**Goal.** Report which authored records changed between R0 and R1 — value, revision, and
review-status transitions (§5.4, §10.3).

**Files.** `src/tyo3/models/diff.py`, `src/tyo3/layers/authored.py`.

**Build.**
```python
@dataclass(frozen=True)
class AuthoredDiff:
    layer: str
    added:    frozenset[str]   # absent at R0, present at R1 (new authored record)
    changed:  frozenset[str]   # present at both; value/revision differs
    review_changed: frozenset[str]   # status differs (e.g. became needs_review/orphaned)
    removed:  frozenset[str]   # present at R0, absent at R1 (rare; record created after R0)
```
- `AuthoredLayerView.diff(before)` compares, per id, `authored.value_at(id, R0)` vs
  `value_at(id, R1)` (via `snap.authored(layer, id)` on each snapshot). `changed` keys
  on the version `revision` (a new authored edit bumps it — Gate 6 step 4) and/or value
  inequality; `review_changed` on the derived status differing between R0 and R1
  (status comes from each snapshot's captured registry — so a body edit between R0 and
  R1 that flagged the note shows up here even though no authored *write* occurred).
- Authored records are never deleted (§5.5.3), so `removed` only ever reflects a record
  that did not yet exist at R0 — keep it for symmetry, expect it usually empty.

**Validate.**
- Test: author a note between R0 and R1 → `added == {id}`.
- Test: re-author the same id with a new value → `changed == {id}`.
- Test: edit the *entity's* body between R0 and R1 (no authored write) → the note's id
  in `review_changed` (status went `present → needs_review`), not in `changed`.
- Test (parity, §10.3): authored diff from live snapshots equals one from fresh reopen
  at R0/R1.
- **Acceptance gate:** all pass. Commit (`gate7: step 5 — authored diff (§5.4/§10.3)`).

---

## Step 6 — The combined `SnapshotDiff` (`after.diff(before)`)

**Goal.** One id-keyed object unifying every layer's diff, so an agent can ask "what
changed between these two revisions, across everything" in a single call (§9 combined
diff, §10.3).

**Files.** `src/tyo3/models/diff.py`, `src/tyo3/session.py` (`Snapshot.diff`).

**Build.**
```python
@dataclass(frozen=True)
class SnapshotDiff:
    before_revision: int
    after_revision: int
    code: CodeDiff
    derived: dict[str, LayerDiff]      # layer name → drift
    authored: dict[str, AuthoredDiff]  # layer name → authored diff
    def entities(self) -> frozenset[str]: ...   # union of all touched DurableIds
    def is_empty(self) -> bool: ...
```
- `Snapshot.diff(before) -> SnapshotDiff`: run `self.code.diff(before.code)`, then
  every configured derived layer's `diff` **in the config's topological order** (so a
  layer-derived diff can consume its upstream's), then every authored layer's `diff`.
  Assemble. **Pure function of the two snapshots.**
- `entities()` is the union of all per-layer touched ids — the precise set an agent (or
  the Gate-8 bus) reacts to.
- Guard: `before.revision <= after.revision` (or document that the diff is directional
  and `before`/`after` may be swapped). Diffing two snapshots from **different
  sessions/roots** is a typed error.

**Validate.**
- Test: edit one entity body + author a note on another → `code.changed`,
  `derived[*].drifted`, and `authored[*].changed` each carry exactly the right id;
  `entities()` is their union.
- Test: identical revisions → `is_empty()`.
- Test (the headline §10.3 acceptance): `a.diff(b)` over live snapshots equals
  `a2.diff(b2)` where `a2`/`b2` are built fresh-from-scratch at the same revisions —
  for **every** layer pair. Express the randomized version (N random edits, fixed seed)
  and wire it into `devenv shell -- test-property`.
- **Acceptance gate:** all pass. Commit (`gate7: step 6 — combined SnapshotDiff, §10.3 parity`).

---

## Step 7 — The floating "latest" fast path (warm glances, honest boundary)

**Goal.** Complete `LatestView` so the per-layer reads are available *warm* over the
live head for one-off glances, and route the consistent convenience reads through the
held head snapshot — keeping the consistency boundary honest (phase 11; §9; §10.2.4).

**Files.** `src/tyo3/session.py` (`LatestView`, `TyO3Session` convenience).

**Build.**
- **Extend `LatestView`** (currently code/check reads only,
  `src/tyo3/session.py:LatestView`) with the warm single-layer reads:
  - `latest.graph()` — the live HEAD graph (already maintained incrementally by the
    write path, `_apply_graph_delta`); warm, no copy-on-pin.
  - `latest.derived(layer, id)` / `latest.authored(layer, id)` — resolve against the
    **current** head revision, reusing head's warm state, cancellation-retried like the
    existing latest reads. Each *single* call is internally consistent (it reads one
    revision); the view does not pin, so the revision may advance between calls.
  - **Do not** add `latest.entity(...)` or `latest.diff(...)`. A cross-layer join and a
    diff require a single pinned R; offering them on the floating view would invite a
    torn multi-read. State this in the `LatestView` docstring (the boundary is the
    deliverable, not a limitation to paper over).
- **Session convenience reads = sugar over the held head snapshot** (§9): add
  `session.entity(id)`, `session.diff(before)`, `session.code`, `session.layer(name)`
  delegating to `self._native()` (the cached, *consistent* head snapshot — `session.py:
  _native`), **not** to `latest`. So `session.entity(id)` is a consistent join;
  `session.latest.derived(...)` is the explicit warm-glance escape hatch. Two clearly
  distinct intents, as the existing `latest` vs `snapshot` split already establishes.
- Reads take **no write lock** (§10.2.4): snapshot reads run on the snapshot's own
  storage; latest reads run cancellation-retried on head storage and never acquire the
  writer lock.

**Validate.**
- Test: `session.latest.derived("upper", id)` returns the current value warm and
  reflects a subsequent head edit on the next call (it floats); `session.snapshot()`'s
  derived read does not (it pins).
- Test: `session.entity(id)` equals `session.snapshot().entity(id)` for the current
  head (sugar over the held head snapshot, consistent).
- Test (no lock / liveness, §10.2.4): a hot writer doing code+authored edits never
  blocks while many threads call `snap.entity(...)` and `latest.derived(...)`; no read
  is cancelled (reuse `test_mvcc_concurrency.py`).
- Test: `latest` exposes no `entity`/`diff` (the honest boundary — assert
  `not hasattr(session.latest, "entity")` or that it raises a clear error).
- **Acceptance gate:** all pass. Commit (`gate7: step 7 — floating-latest warm reads + convenience sugar`).

---

## Step 8 — §10 final acceptance (cross-layer consistency + time-travel diff)

**Goal.** Prove §10 end-to-end as a unit, including the §10.3 time-travel-diff
acceptance, in one dedicated module.

**Files.** `src/tyo3/tests/test_gate7_read_surface.py` (extend).

**Build & validate** — a dedicated suite proving:
- **All layers describe R (§10.2.2).** At a pinned R, for an entity with code +
  embedding + docstring + authored intent, every member of `snap.entity(id)` describes
  R; after many head writes the held snapshot is unchanged.
- **Honest staleness (§10.2.3).** A stale derived member of the join reports `stale`;
  never silently fresh.
- **Time-travel diff matches rebuilds (§10.3).** Build a fixture, take snapshots at R0
  and R1, diff every layer pair; independently rebuild snapshots from scratch at R0/R1
  and diff again; the results are identical for code, each derived layer, and each
  authored layer.
- **No write lock for reads (§10.2.4).** The liveness/no-cancel stress from Step 7.
- **No-config no-op.** A project with only the code layer: `snap.entity(id)` has empty
  `derived`/`authored`; `snap.diff` carries only `code`; behaviour matches Gate 3.
- **Acceptance gate:** all pass; `devenv shell -- tests` and `devenv shell --
  test-property` green. Commit (`gate7: step 8 — §10 cross-layer + time-travel-diff acceptance`).

---

## Gate 7 — Final acceptance (must all pass before Gate 8)

Run `devenv shell -- tests` (plus `devenv shell -- test-property`) with the dedicated
module proving:

1. **Cross-layer join (§10.2.2).** `snap.entity(id)` resolves code, derived, and
   authored for one id at one revision; the layers agree across many head writes.
2. **Honest status in the join (§10.2.3).** Stale derived and needs_review/orphaned
   authored values are reported as such, never silently fresh.
3. **Per-layer diff.** `after.code.diff(before.code)` (added/removed/changed/moved +
   edges), `after.embedding_drift(before)` (drift, profile-aware, no over-fire),
   authored diff (changed + review_changed).
4. **Combined diff (§10.3).** `after.diff(before)` unifies all layers, keyed by
   `DurableId`; `entities()` is the precise union.
5. **Diff == rebuild (§10.3).** Every diff equals one computed from fresh rebuilds at
   the same two revisions, including a randomized property run.
6. **Pure-snapshot, no head, no lock (§10.2.4).** Joins and diffs touch only pinned
   snapshots; reads never take the write lock or get cancelled.
7. **Floating latest is warm and honest.** `latest.*` serves warm single-layer
   glances; cross-layer joins/diffs are snapshot-only; session convenience reads are
   consistent sugar over the held head snapshot.
8. **No-config no-op.** A code-only project behaves exactly as Gate 3; derived/authored
   diffs are empty.

When all eight pass on a clean `devenv shell -- tests`, tag the commit
`gate7-complete` and paste the passing summary into the annotation. The unified,
consistent, diffable read surface across every layer is complete.

## Carrying forward to Gate 8 (MUST read before the subscription bus)

Gate 7 leaves the bus exactly what it needs:

- **`SnapshotDiff.entities()` is the precise reaction set.** The subscription bus
  (§12) delivers a revision to a subscriber iff the transitive affected set intersects
  its interest; `SnapshotDiff` (or the per-revision `SyncResult` deltas it mirrors) is
  the id-level payload a subscriber resolves with `snapshot(at=delta.revision)` —
  exactly the read surface this gate finalised. The bus does not re-derive diffs; it
  carries the delta and lets subscribers read at the notified revision.
- **The notified read is always a snapshot, never latest.** Because a subscriber
  reacts to a *specific* revision, it must `snapshot(at=R)` (consistent), not read
  `latest` (floating). Gate 7's boundary makes that the obvious, enforced path.
- **`RevisionEvicted` on a lagging subscriber falls back to a rescan.** A subscriber
  that lags past `retain_cap` gets `RevisionEvicted` from `snapshot(at=R)` (Gate 1)
  and rescans by diffing its last-seen snapshot against `session.snapshot()` — using
  this gate's `diff`. The diff *is* the catch-up mechanism.

## Sequencing & escalation notes

- **Never let a diff or a join touch the head.** Both are pure functions of pinned
  snapshots; that is what makes them equal a rebuild (§10.3) and lock-free (§10.2.4).
  If a parity test (Steps 3–6) diverges, the bug is almost always a stale field on the
  pinned graph or an accidental head read — fix the read, not the comparator.
- **Reuse the Gate-3 comparator; don't fork it.** The structural-equality comparator
  that proves incremental==rebuild and the structural *diff* share normalisation. Keep
  one canonicalisation so a node/edge that compares equal there also diffs empty here.
- **Drift is computed, not generated.** Derived drift reads only `content_hashes` on
  pinned graph nodes — never the artifacts, the vector store, or a generator. If you
  find yourself recomputing an artifact to diff, stop; that contradicts the
  content-addressed design (§8.2.2).
- **Hold the consistency boundary.** Resist adding `latest.entity`/`latest.diff`. The
  warmth/consistency trade is the architecture's deliberate choice (§11 costs); the
  consistent path is `snapshot`, the warm path is `latest`, and conflating them
  re-introduces torn multi-layer reads.
- **If enumerating ids at R is awkward in Python** (Step 4/5), add a *small* native
  accessor on the frozen snapshot (`authored_ids(layer)`, `entity_ids()`) rather than
  reaching into the head — keep enumeration pinned at R like every other read here.
