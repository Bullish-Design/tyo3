# Gate 8 — The Delta Subscription Bus & Watcher-as-a-Change-Source: Implementation Guide

> Implements **REFINED_SPEC.md §12** (the delta subscription bus: scoped, ordered,
> non-blocking delivery) and the **§4.3.3 transitive-affected-set / §4.4 watcher
> precision** consumed by it, and finishes **ARCHITECTURE phase 10** (delta
> subscriptions scoped by reverse-dep + interest; the file watcher as a change
> source). It is the **last functional gate** — after it, the substrate is the
> real-time, multi-agent system the concept describes; only the benchmark/perf
> envelope (§13.3, §14) remains.
>
> **Prerequisites:** `gate7-complete`. This gate is **read-and-coordinate only**: it
> adds no new write type and does not change the commit transaction. It consumes:
> - the per-commit `SyncResult` every write already returns (revision + id-level
>   `created`/`changed`/`deleted`/`moved`/`authored`/`rescan`), produced **after
>   publication** (§3.3.4) so the bus enqueues outside the write lock,
> - the reverse-dependency index already in the code graph (`graph._importers_of`,
>   `graph.transitive_dependents`) for the §4.3.3 closure,
> - Gate 4's `[coordination.bus]` (`queue_capacity`, `overflow`) and
>   `[coordination.watcher]` (`enabled`, `debounce_ms`) config,
> - the **already-built file watcher** (`session.watch/unwatch/flush_watch/
>   poll_changes`, native `ProjectWatcher` + `apply_watch_events`, the `pending`
>   queue),
> - Gate 1's `RevisionEvictedError` and Gate 7's `SnapshotDiff` for the lag/rescan
>   fallback.
>
> **Audience:** an engineer new to the codebase. Same rules as Gates 1–7:
> `devenv shell -- <script>` for everything; one labelled commit per validated step
> (`gate8: step N — …`); never skip a validation; the suite is fully green before the
> tag.

## How to work in this repo

- Rust unit tests: `devenv shell -- test-rust`. Full suite: `devenv shell -- tests`.
- Rebuild after Rust changes: `devenv shell -- build`.
- Concurrency/liveness stress (Steps 4–8): model on `test_mvcc_concurrency.py`.
- Branch off `gate7-complete`; commit per validated step.

## The contract you are building toward (read first)

The bus turns the system from poll-and-re-derive into **react-to-exactly-what-changed**
(§12.1). Its guarantees:

- **Scoped delivery (§12.2.1).** A subscriber registers an `interest` (a set of
  files/ids, a layer, or `ALL`). On each committed revision, the bus delivers a delta
  to that subscriber **iff** the transitive affected set (§4.3.3) intersects its
  interest — **scoped to that intersection**, never the whole delta.
- **Ordered, non-hiding (§12.2.2 [INV]).** Deltas to one subscriber arrive in
  **revision order**. Coalescing multiple revisions into one notification is permitted
  **only if** the union of their affected sets is delivered — a coalesced notification
  never hides an intervening change to the interest.
- **Revision-stamped (§12.2.3).** Each delivered delta carries its `revision`, so the
  subscriber can `snapshot(at=revision)` and read **exactly** the state it was notified
  about (Gate 7's consistent read surface).
- **Non-blocking (§12.2.4).** Delivery happens after publication and **never blocks the
  write transaction**. A slow or dead subscriber **must not** stall the writer or other
  subscribers — each subscriber has its own bounded queue with a backpressure policy.
- **Lag is survivable (§12.2.5).** The bus does **not** guarantee the notified revision
  is still un-evicted by the time the subscriber reads. A subscriber that lags past
  `retain_cap` gets a typed `RevisionEvicted` on `snapshot(at=R)` and **falls back to a
  rescan** (Gate 7 `diff`).
- **Clean teardown (§12.2.6).** Dropping a subscriber releases its queue and any
  revisions it pinned.

Plus: the **file watcher is a first-class change source** — a watcher-ingested disk
change produces a delta with the **same precision** as an explicit edit (§4.4) and
fires the bus exactly the same way.

The defining success property: **a subscriber interested in `models.py` is notified on
edits to it and to its importers (via reverse-dep), and on nothing else; notifications
arrive in revision order or correctly-unioned; a blocked subscriber never delays the
writer; a project with no subscribers behaves exactly as Gate 7.**

## Design stance — the bus is Python, fed after the commit

The write transaction is native and serialized under one lock (Gates 1/3). The bus has
exactly one job the spine cannot already do: **fan a post-commit delta out to
interested subscribers without touching the transaction.** Everything it needs is
already in Python *after* a write returns — the `SyncResult` (the delta, published) and
the head graph's reverse-dep index (the closure). So the bus lives in **Python**, fed
from the write path **after** the native commit returns (publication already happened —
§3.3.4), exactly as Gate 5's derived invalidation and Gate 6's authored writes plug in.
This keeps §12.2.4 structural: the bus is downstream of publication, so it **cannot**
block the writer.

Two invariants to hold throughout:

1. **Ordering comes from the write lock, not the bus.** Writes are serialized (one
   monotonic revision at a time). Publish to the bus **while still holding the
   session-level write lock** Gate 3 step 8 established (the lock that spans the native
   call + `_apply_graph_delta`), so deltas enter the bus in revision order. The
   *delivery* to each subscriber's queue is then a non-blocking append — the lock is
   held only for the O(subscribers) fan-out, never for subscriber consumption.
2. **The notified read is always a `snapshot(at=R)`, never `latest`.** A subscriber
   reacts to a *specific* revision; it must pin it (Gate 7's consistency boundary). The
   bus hands a revision; the subscriber reads consistently at it.

## Target module layout

```
src/tyo3/
  bus/                       # NEW: the subscription bus (all Python)
    __init__.py
    interest.py              # Interest (files/ids/layer/ALL) + match/scope (Step 1)
    delta.py                 # Delta (the delivered object) + scoped_to (Step 2)
    subscription.py          # Subscription: bounded queue, iterator, teardown (Step 3)
    bus.py                   # Bus: register/publish/unregister, overflow policy (Step 4)
  session.py                 # session.subscribe; publish from the write path;
                             #   watcher auto-poll driven by config              (Steps 5, 7)
src/tyo3/tests/
  test_gate8_bus.py          # Step 0 + final acceptance
```

No Rust changes are expected. The watcher already exists; Step 7 drives it from config
and routes its `poll_changes` delta into the bus. If the head graph cannot yet hand the
bus a transitive closure at the id level, Step 2 adds a thin Python helper over the
existing `_importers_of`/`transitive_dependents` — not a native change.

---

## Step 0 — Pin the bus API with a failing test FIRST

**Goal.** Freeze the public API before building (mirrors every prior gate's Step 0). It
`xfail`s until Step 5.

**Files.** `src/tyo3/tests/test_gate8_bus.py` (new).

**Build the fixture & test.** A two-file project (`models.py` defines `User`; `app.py`
imports and calls it). Write the headline tests against the intended API:

- **Scoped, reverse-dep-aware delivery.** `sub = session.subscribe(Interest.files({"models.py"}))`;
  edit `models.py` → `sub.poll(timeout=...)` yields a `Delta` whose `revision` matches
  the edit and whose `changed` includes `User`. Edit `app.py` (an *importer* of
  `models.py`) → the subscriber **is** notified (reverse-dep). Edit an unrelated third
  file → the subscriber is **not** notified.
- **Revision-stamped read-at-R.** For a delivered `delta`, `with
  session.snapshot(at=delta.revision) as snap:` reads exactly the notified state
  (Gate 7).
- **Clean teardown.** `sub.close()` (or `with session.subscribe(...) as sub:`) stops
  delivery; a later edit is not enqueued to it.

**Validate.**
- `devenv shell -- tests -k gate8_bus` runs; record status (expected: failing/xfail).
- **Acceptance gate:** the test exists and names the intended API
  (`session.subscribe`, `Interest.files/ids/layer/ALL`, `sub.poll`, iteration over
  `sub`, `delta.revision`, `delta.changed`, `delta.scoped_to`); status recorded. Commit
  (`gate8: step 0 — pin bus API, status=XFAIL`).

> Do not implement anything in Step 0. Its only job is to fix the contract.

---

## Step 1 — `Interest` (the subscription filter)

**Goal.** A composable description of what a subscriber cares about, with a cheap
`matches`/`scope` against an affected set (§12.2.1).

**Files.** `src/tyo3/bus/interest.py`.

**Build.**
```python
@dataclass(frozen=True)
class Interest:
    files: frozenset[str] = frozenset()      # project-relative paths
    ids: frozenset[str] = frozenset()        # DurableIds
    layers: frozenset[str] = frozenset()     # layer names (e.g. "embeddings", "intent")
    all: bool = False                         # ALL: every committed revision

    @classmethod
    def files(cls, paths) -> "Interest": ...
    @classmethod
    def ids(cls, ids) -> "Interest": ...
    @classmethod
    def layer(cls, name) -> "Interest": ...
    ALL: ClassVar["Interest"]                 # Interest(all=True)

    def __or__(self, other) -> "Interest": ...   # union (Interest | Interest)

    def matches(self, affected_ids: set[str], affected_files: set[str],
                touched_layers: set[str]) -> bool: ...
    def scope(self, delta) -> "Delta": ...       # delegates to delta.scoped_to(self)
```
- `matches` is `all` OR any non-empty intersection of `ids`/`files`/`layers` with the
  affected sets. **`files` matching uses the affected set's files**, where an entity id
  maps to its defining file via the pinned graph / `locate` (resolve once per delta,
  not per subscriber).
- `layers` matching: a code/derived change touches the implicit `code` layer and any
  derived layer whose entities drifted; an authored edit touches its authored layer
  (from `SyncResult.authored`). Compute `touched_layers` once per delta (Step 2).
- Keep `Interest` immutable and hashable so the bus can key subscribers cheaply.

**Validate.**
- Test: `Interest.files({"a.py"}).matches({...}, {"a.py"}, ...)` is true; disjoint is
  false; `Interest.ALL.matches(...)` always true.
- Test: `Interest.layer("intent").matches(set(), set(), {"intent"})` true.
- Test: `(Interest.files({"a.py"}) | Interest.ids({"01K…"}))` unions correctly.
- **Acceptance gate:** all pass. Commit (`gate8: step 1 — Interest filter`).

---

## Step 2 — The transitive affected set (§4.3.3) and the delivered `Delta`

**Goal.** Compute, for a committed `SyncResult`, the **transitive** affected id set
(the closure of `changed ∪ deleted` under inbound dependency edges) and package the
revision-stamped, scopable `Delta` the bus delivers (§4.3.3, §12.2.3).

**Files.** `src/tyo3/bus/delta.py`, a thin closure helper over the head graph.

**Build.**
```python
@dataclass(frozen=True)
class Delta:
    revision: int
    created: frozenset[str]
    changed: frozenset[str]
    deleted: frozenset[str]
    moved: frozenset[str]
    authored: frozenset[str]          # authored-record ids edited this revision (Gate 6)
    affected: frozenset[str]          # transitive closure of changed∪deleted (§4.3.3)
    rescan: bool
    files: frozenset[str]             # files touched (for Interest.files matching)
    layers: frozenset[str]            # layers touched (for Interest.layer matching)

    def scoped_to(self, interest: Interest) -> "Delta": ...   # intersect every id set with interest
    def is_empty(self) -> bool: ...
```
- **Closure (§4.3.3).** `affected = changed ∪ deleted ∪ transitive_dependents(changed ∪
  deleted)` using the head graph's reverse-dep index — `graph.transitive_dependents`
  for the id-level closure, or `graph._importers_of` mapped through file→ids. **No
  global scan** (§4.3.3): the closure walks only inbound edges from the changed set.
  Compute it from the head graph **as it stands at the committed revision** (the write
  path has already applied `_apply_graph_delta`, so the graph is at R — Gate 3 step 8).
- `scoped_to(interest)` returns a new `Delta` whose `created/changed/deleted/moved/
  authored/affected` are each intersected with the interest's id/file resolution, so a
  subscriber sees only its slice (§12.2.1). A `rescan` delta scopes to "everything" —
  `rescan=True` is delivered to every subscriber (it means "rebuild," §4.3.5).
- `from_sync_result(result, graph)` builds the `Delta`: copy the id sets, resolve
  `files` (each id → defining file) and `layers`, compute `affected`.

**Validate.**
- Test (no over-fire at the bus level, but full closure for coordination): editing
  `models.py::User` yields `changed={User}` and `affected ⊇ {User, app's caller}` (the
  importer is in the closure even though its own hash didn't change — this is the
  coordination closure, distinct from derived invalidation which is hash-precise).
- Test: a pure move appears in `moved`, not `created`+`deleted` (Gate 2/3 carried up).
- Test: `scoped_to(Interest.files({"models.py"}))` drops ids defined in other files.
- Test: a `rescan` delta has `rescan=True` and is treated as "all affected."
- **Acceptance gate:** all pass. Commit (`gate8: step 2 — transitive affected set + Delta (§4.3.3)`).

---

## Step 3 — `Subscription`: bounded queue, iterator, teardown

**Goal.** One subscriber's delivery endpoint: a bounded queue, a blocking iterator and
non-blocking `poll`, and clean teardown that releases everything (§12.2.4, §12.2.6).

**Files.** `src/tyo3/bus/subscription.py`.

**Build.**
```python
class Subscription:
    interest: Interest
    def __init__(self, interest, capacity: int, overflow: Literal["coalesce","block","error"]): ...
    # producer side (called by the Bus, under the write lock):
    def _offer(self, delta: Delta) -> None: ...   # append; apply overflow policy; NEVER block the writer for coalesce/error
    # consumer side (called by the subscriber's own thread):
    def poll(self, timeout: float | None = 0.0) -> Delta | None: ...
    def __iter__(self) -> Iterator[Delta]: ...    # blocking; ends when closed
    def __enter__(self): ...; def __exit__(self, *exc): self.close()
    def close(self) -> None: ...                  # idempotent; unregister from bus; drain/release queue
    @property
    def lagged(self) -> bool: ...                 # set when overflow=="error" dropped a delta
```
- The queue is bounded by `capacity` (config `[coordination.bus].queue_capacity`,
  default 1024). The **overflow policy** decides what `_offer` does when full:
  - **`coalesce`** (default) — merge the incoming delta into the tail delta, delivering
    the **union** of their affected sets in revision order (§12.2.2). Keep the *latest*
    revision as the delta's `revision` but the union of all id sets since the last
    consumed one, so the subscriber reads the newest state and misses nothing. This is
    the non-hiding coalesce the INV permits.
  - **`error`** — drop and set `lagged=True`; the subscriber observes the gap and
    rescans (Step 6). Never blocks the writer.
  - **`block`** — the explicit backpressure opt-in: `_offer` blocks the *producer*.
    Because `_offer` runs under the write lock, **this can stall the writer**, so it is
    off by default and documented as "use only when a subscriber must never lag and you
    accept writer backpressure." Default `coalesce` honours §12.2.4 unconditionally.
- `close()` removes the subscription from the bus (so no further `_offer`), wakes any
  blocked iterator, and releases the queue and any snapshots the subscriber pinned
  (the subscriber owns its snapshots; the queue holds only `Delta`s, which pin
  nothing — §12.2.6).

**Validate.**
- Test: `poll(timeout=0)` returns `None` when empty; returns the delta when offered.
- Test (iterator): a background producer offers 3 deltas; `for d in sub` yields them in
  order, then `close()` ends the loop.
- Test (coalesce): set `capacity=2`; offer 5 deltas without consuming; the queue holds
  ≤ capacity and the coalesced tail's affected set is the **union** of the overflowed
  deltas with the newest revision (§12.2.2 non-hiding).
- Test (error): `overflow="error"`; overflow sets `lagged`; no exception on the
  producer.
- Test (teardown): `close()` is idempotent; after close, `_offer` is a no-op.
- **Acceptance gate:** all pass. Commit (`gate8: step 3 — Subscription queue + overflow + teardown`).

---

## Step 4 — The `Bus`: register, publish, revision-order fan-out

**Goal.** The registry of subscriptions and the `publish` that fans a committed delta
out, scoped and in revision order, non-blocking for the writer (§12.2.1–§12.2.4).

**Files.** `src/tyo3/bus/bus.py`.

**Build.**
```python
class Bus:
    def __init__(self, capacity: int, overflow: str): ...
    def subscribe(self, interest: Interest) -> Subscription: ...   # register + return
    def _unsubscribe(self, sub: Subscription) -> None: ...
    def publish(self, delta: Delta) -> None: ...
    def close(self) -> None: ...   # close all subscriptions
```
- `subscribe` creates a `Subscription` with the configured capacity/overflow (a
  per-subscriber override may be added later) and adds it to the set under a small bus
  lock.
- `publish(delta)`:
  - resolves the delta's id→file→layer mappings **once** (already done in Step 2);
  - for each subscription whose `interest.matches(...)` is true, `_offer` the
    **scoped** delta (`delta.scoped_to(sub.interest)`); skip the rest;
  - is called by the write path **in revision order** (the caller holds the write
    lock — Step 5), so per-subscriber FIFO == revision order (§12.2.2). The bus does
    not re-sort; it relies on the caller's ordering. Assert defensively that
    `delta.revision` is monotonically non-decreasing across `publish` calls and log a
    loud error if not (it would mean the write lock was not held).
- **The bus never reads the head, never takes the write lock itself, never blocks on a
  subscriber** (except the opt-in `block` overflow). Its only shared state is the
  subscription set, guarded by a short lock that is never held across an `_offer` that
  could block — for `block` policy, offer outside the set lock to a snapshot of current
  subscribers.

**Validate.**
- Test: two subscribers with disjoint interests; `publish` delivers to each only its
  scoped slice.
- Test: `Interest.ALL` subscriber receives every published delta.
- Test (order): publish revisions 1..100 from one thread; each subscriber's queue
  drains in 1..100 order.
- Test (isolation): a subscriber whose interest doesn't match receives nothing.
- **Acceptance gate:** all pass. Commit (`gate8: step 4 — Bus register + scoped fan-out (§12.2.1)`).

---

## Step 5 — Wire the bus into the write path (publish after publication)

**Goal.** Every committed revision — from `edit`/`edit_many`/`edit_virtual`/`sync_*`/
`author`/`poll_changes` — produces exactly one `publish`, **after** the native commit
returns and the head graph is updated, **under the session write lock** so deltas are
published in revision order, and **never** blocking the writer (§3.3.4, §12.2.1–4).
This makes Step 0 pass.

**Files.** `src/tyo3/session.py`.

**Build.**
- The session owns a `Bus`, constructed from `session.config.coordination.bus`
  (capacity/overflow). If no subscribers ever register, `publish` is a cheap no-op
  (empty subscription set) — the no-bus path costs nothing.
- `session.subscribe(interest) -> Subscription` delegates to `self._bus.subscribe`.
- Factor the common write tail into one helper invoked by every write method, **inside
  the Gate-3 session write lock** that already spans the native call + `_apply_graph_
  delta` (and Gate 5's `_invalidate_derived`):
```python
def _publish_delta(self, result: SyncResult) -> None:
    if not self._bus.has_subscribers():          # fast no-op
        return
    delta = Delta.from_sync_result(result, self.graph)   # graph is at result.revision here
    self._bus.publish(delta)
```
  Call order in each write method (all under the one session write lock):
  `native commit → _apply_graph_delta (Gate 3) → _invalidate_derived (Gate 5) →
  _publish_delta (this gate)`. The lock guarantees revision order; `_publish_delta`
  only appends to bounded queues, so it does not block the writer (default overflow).
- **Authored writes publish too** (Gate 6): `session.author` produces a `SyncResult`
  with the id in `authored` and a touched authored layer — route it through
  `_publish_delta` so layer-/id-interested subscribers react to authored edits.
- Building the head graph in `_publish_delta` must not be forced when there are no
  subscribers (hence the `has_subscribers()` guard first) — preserve the lazy-graph
  cost model.

**Validate.**
- **Step 0's tests pass.**
- Test: an `edit` with one subscriber produces exactly one delivered delta at the right
  revision; with no subscribers, no graph build is forced beyond what the write already
  did.
- Test (after publication, §3.3.4): the delivered `delta.revision` equals
  `session.head` after the write; `snapshot(at=delta.revision)` reads the new content.
- Test (concurrent writers, ordering): two threads partition-edit different files; a
  shared `ALL` subscriber receives deltas in strictly increasing revision order.
- **Acceptance gate:** all pass; Step 0 green. Commit (`gate8: step 5 — publish from the write path (§12.2.1–4)`).

---

## Step 6 — Lag, eviction, and the rescan fallback (§12.2.5)

**Goal.** A subscriber that falls behind past `retain_cap` must survive: its
`snapshot(at=R)` raises `RevisionEvicted`, and it recovers by rescanning — diffing its
last-seen state against the current head with Gate 7's `diff` (§12.2.5, §4.3.5).

**Files.** `src/tyo3/bus/subscription.py`, `src/tyo3/tests/test_gate8_bus.py`.

**Build.**
- The bus does **not** pin revisions for subscribers (a `Delta` holds only ids, not a
  snapshot). So a notified revision may evict before the subscriber reads it — that is
  by design (§12.2.5). Document this on `Subscription`.
- Provide a recovery helper the subscriber uses when `snapshot(at=delta.revision)`
  raises `RevisionEvictedError` **or** when `sub.lagged` is set (overflow="error"):
```python
def rescan_from(self, session, last_seen_revision: int | None) -> SnapshotDiff:
    """Catch up after eviction/lag: diff the last revision the subscriber
    successfully read against the current head, returning the full affected
    SnapshotDiff (Gate 7). If last_seen is itself evicted, diff against a fresh
    full read (rescan=everything)."""
```
  - If `last_seen_revision` is still retained: `after.diff(before)` over
    `snapshot(at=head)` and `snapshot(at=last_seen)`.
  - If `last_seen` is also evicted: treat as a full rescan — return a `SnapshotDiff`
    against an empty/initial baseline (everything affected), mirroring `rescan=True`.
- The subscriber pattern (documented in the module + a test) is:
```python
for delta in sub:
    try:
        with session.snapshot(at=delta.revision) as snap:
            react(snap, delta)
    except RevisionEvictedError:
        diff = sub.rescan_from(session, last_good)
        react_to_diff(session, diff)
    else:
        last_good = delta.revision
```

**Validate.**
- Test: with `retain_cap=4`, hold a subscriber without consuming while the writer does
  10 edits; consuming an old delta's `snapshot(at=R)` raises `RevisionEvictedError`;
  `rescan_from` returns a `SnapshotDiff` catching up to head.
- Test (`overflow="error"` lag): a dropped delta sets `lagged`; `rescan_from` recovers
  the union of missed changes.
- Test: a `rescan=True` delta delivered to a subscriber triggers the full-rescan branch.
- **Acceptance gate:** all pass. Commit (`gate8: step 6 — lag/eviction rescan fallback (§12.2.5)`).

---

## Step 7 — The watcher as a change source (config-driven, fires the bus)

**Goal.** Make the existing file watcher a first-class, config-driven change source:
auto-start from `[coordination.watcher]`, drain on a debounce, and route its delta
through the same `_publish_delta` so disk changes notify subscribers — with the **same
precision** as an explicit edit (§4.4, ARCHITECTURE phase 10).

**Files.** `src/tyo3/session.py` (watcher lifecycle + auto-poll), reusing the native
`watch`/`flush_watch`/`poll_changes` already present.

**Build.**
- **Config-driven start.** On `session` open, if `config.coordination.watcher.enabled`,
  call `self.watch()` and start a small **auto-poll loop** (a daemon thread) that, every
  `debounce_ms`, calls `poll_changes()` and — if it returns a non-`None` `SyncResult` —
  runs the same write tail (`_apply_graph_delta → _invalidate_derived →
  _publish_delta`) **under the session write lock**, so a watcher revision is
  indistinguishable from an explicit edit to the bus and the graph/derived layers.
  (Disabled by default; manual `watch()`/`poll_changes()` remains available and also
  publishes.)
- **Precision (§4.4).** `poll_changes` already ingests disk content into the generation
  (Gate 1 step 7) and runs the same reconciliation (Gate 2), so its `SyncResult` has
  the same id-level precision as an explicit edit. Verify the bus delta built from it
  matches the delta from an equivalent `edit` of the same content.
- **Overlay wins.** A watcher event for a path with a live overlay buffer is dropped
  (already handled in `apply_watch_events`); confirm such an event produces no spurious
  bus delta.
- **Lifecycle.** `unwatch()` stops the loop and the native watcher; `close()` tears down
  the loop, the watcher, and the bus cleanly (no dangling daemon thread).

**Validate.**
- Test (deterministic seam): use `_inject_changes` to simulate a watcher batch, then
  `poll_changes`; a subscriber interested in the changed file receives a delta at the
  resulting revision (no real filesystem timing needed).
- Test (precision parity, §4.4): a watcher-ingested change to `a.py` and an explicit
  `edit("a.py", same_text)` produce bus deltas with identical `changed`/`affected`.
- Test (overlay wins): a watcher event for an overlaid path yields no bus delta.
- Test (config): `watcher.enabled=false` → no auto-poll thread; `enabled=true` → a
  disk change is observed and published within a bounded poll window.
- Test (teardown): `close()` joins the auto-poll thread; no thread leak.
- **Acceptance gate:** all pass. Commit (`gate8: step 7 — watcher as a config-driven change source (§4.4)`).

---

## Step 8 — Coalescing correctness + non-blocking liveness acceptance (§12.2.2/§12.2.4)

**Goal.** Prove the two hardest INVs end-to-end in one dedicated module: ordered/
non-hiding coalescing, and a slow subscriber never stalling the writer.

**Files.** `src/tyo3/tests/test_gate8_bus.py` (extend).

**Build & validate.**
- **Burst → ordered or unioned (§12.2.2).** Fire a burst of N writes faster than a
  subscriber consumes (small `capacity`, `overflow="coalesce"`); assert the subscriber
  receives either ordered per-revision deltas **or** correctly-unioned coalesced deltas
  whose combined affected set equals the union of all N affected sets — **nothing is
  hidden**, and the final delivered revision is the latest.
- **Blocked subscriber doesn't stall the writer (§12.2.4).** One subscriber never
  consumes (`overflow="coalesce"` or `"error"`); a hot writer does 100 edits; assert
  the writer completes within a fixed wall-clock bound (model on
  `test_mvcc_concurrency.py`) and other subscribers still receive in order.
- **Interest filter precision (§12.3 acceptance).** A subscriber on `models.py` is
  notified on edits to it and its importers (reverse-dep) and **not** on unrelated
  edits.
- **Revision-stamped read (§12.2.3).** Every delivered delta's `snapshot(at=revision)`
  reads exactly the notified state across all layers (Gate 7 join).
- **Clean teardown (§12.2.6).** Closing a subscriber releases its queue; no leaked
  threads/snapshots after `session.close()` (assert via resource counters / no
  ResourceWarning).
- **No-bus no-op.** With no subscribers, the write path is byte-for-byte Gate 7
  behaviour (no graph forced, no delta built).
- **Acceptance gate:** all pass; `devenv shell -- tests` and `devenv shell --
  test-property` green. Commit (`gate8: step 8 — coalescing + liveness acceptance (§12.2.2/§12.2.4)`).

---

## Gate 8 — Final acceptance (must all pass before benchmarks)

Run `devenv shell -- tests` (plus `devenv shell -- test-property`) with the dedicated
module proving:

1. **Scoped delivery (§12.2.1).** A subscriber is notified iff the transitive affected
   set intersects its interest, scoped to that intersection; importers are reached via
   the reverse-dep closure (§4.3.3).
2. **Ordered, non-hiding (§12.2.2).** Per-subscriber FIFO == revision order; coalescing
   delivers the union of affected sets and never hides an intervening change.
3. **Revision-stamped (§12.2.3).** Each delta carries its revision; `snapshot(at=R)`
   reads exactly the notified state across all layers.
4. **Non-blocking (§12.2.4).** Delivery is post-publication; a slow/dead subscriber
   never stalls the writer or other subscribers (default `coalesce`/`error`).
5. **Lag survivable (§12.2.5).** A subscriber past `retain_cap` gets `RevisionEvicted`
   and recovers via `rescan_from` (Gate 7 `diff`); `rescan=True` forces a full catch-up.
6. **Clean teardown (§12.2.6).** Dropping a subscriber releases its queue and pins; no
   thread/snapshot leak.
7. **Watcher is a change source (§4.4).** A watcher-ingested disk change fires the bus
   with the same precision as an explicit edit; overlay buffers win; config drives it.
8. **No-bus no-op.** A project with no subscribers behaves exactly as Gate 7.

When all eight pass on a clean `devenv shell -- tests`, tag the commit
`gate8-complete` and paste the passing summary into the annotation. **The substrate is
now feature-complete:** spine, code layer, derived + authored layers, the unified
diffable read surface, and real-time multi-agent coordination via the bus and the
watcher.

## Carrying forward — what remains after Gate 8

Only the **performance/verification envelope** (ARCHITECTURE phase 12; SPEC §13.3,
§14) is left — it is measurement, not new behaviour:

- **Single-entity edit → commit ≪ full rebuild**, bounded by the affected set, not
  project size (§13.3); the bus fan-out is O(matching subscribers), not O(project).
- **Snapshot warm-up characterised**; **memory bounded** across many revisions/
  snapshots/subscribers (`retain_cap` + live-snapshot count + bounded per-subscriber
  queues).
- The **§14 verification matrix** performance rows, run under the devenv scripts.

A future enhancement (explicitly out of scope here): an **async/await** subscription
interface (`async for delta in sub`) over the same bus, and **cross-process** delivery.
The Python in-process bus is the spec's requirement; both extensions layer on top
without changing the §12 contract.

## Sequencing & escalation notes

- **The bus must never touch the write transaction.** It is fed *after* publication
  with an already-committed `SyncResult`; that is what makes §12.2.4 structural. If you
  find yourself enqueueing to the bus *inside* the native commit or before the head
  graph is at R, stop — publish from the write tail under the session lock, after
  `_apply_graph_delta`.
- **Ordering is the write lock's job.** Do not build a re-sorting buffer in the bus;
  rely on publishing under the serialized write lock and assert monotonic revisions.
- **`block` overflow is a foot-gun by design.** It is the only policy that can apply
  backpressure to the writer (because `_offer` runs under the write lock). Default
  `coalesce`; document `block` as an explicit, eyes-open choice.
- **Coalesce must union, never truncate (§12.2.2).** The easy bug is to keep only the
  latest delta and drop the overflowed ones' affected ids — that hides changes. Always
  union the id sets; keep the latest revision as the read target.
- **Notified reads are snapshots, never latest.** A subscriber reacts to a specific
  revision; route it through `snapshot(at=R)` (Gate 7's consistency boundary), and on
  eviction fall back to `diff` — do not let a reaction read `latest`.
- **If the id-level closure is hard to get from the graph**, compute it from
  `_importers_of` (file-level) mapped through file→ids rather than adding head access in
  the bus; keep the bus a pure consumer of the already-built reverse-dep index.
