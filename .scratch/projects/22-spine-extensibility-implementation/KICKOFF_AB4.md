# KICKOFF — PR10 = AB4: Serve reads off the actor (read concurrency)

> ⚠️ **HUMAN-REVIEW GATE.** AB4 and AB8 are behind a review gate. Do **not** start
> coding until the maintainer has signed off on AB3 (PR #11) and explicitly green-lit
> AB4. This is the **highest-concurrency-risk** task in the whole build — the failure
> mode is data races / writer cancellation under load, which tests catch only if you
> **write the concurrency test FIRST**. Treat that as a hard rule, not advice.

You are continuing the **TyO3 spine-extensibility build**. Phase A (4 QW PRs), all of
Phase B (AB1/AB5/AB7), and the first two Phase-C PRs (**AB2** producer protocol, **AB3**
async serve) are landed. AB4 is the **third Phase-C PR**: it stops every read RPC from
serialising on the single session-actor thread, so a slow read (or a slow write) no
longer queues every other read behind it.

This is a **daemon-layer PR (pure-Python; no Rust, no `build`)**. It does **not** change
the session/derive/bus internals — it changes *where* the daemon runs reads.

---

## State on entry (verify, do NOT redo)

- **AB3 = PR #11**, branch `spine-extend-ab3` @ `709b52c`, base `nvim-plugin`. Confirm
  it is **merged into `nvim-plugin`** before branching (the Phase-A/B pattern: a linear
  child is fast-forwarded into `nvim-plugin`; GitHub auto-marks the PR MERGED). If #11
  is not yet merged, **stop and ask** — do not branch off an unmerged base.
- **Branch:** `git checkout nvim-plugin && git pull && git checkout -b spine-extend-ab4`.
  Open the PR with **base `nvim-plugin`**. Umbrella **PR #2 (nvim-plugin → main) stays
  open — do not touch it.**
- **AB3 shipped the off-actor precedent you are generalising:** `DerivedRecomputeWorker`
  (`derive/async_recompute.py`) already reads/recomputes **off the actor over a frozen
  pinned snapshot** — proof that a non-actor thread can safely read a `Snapshot` while
  the actor remains the sole writer. AB4 extends that pattern from "one background
  recompute" to "every read RPC."

## Read first (in order)
1. `21-.../IMPLEMENTATION_GUIDE.md` → **"TASK AB4 — Serve reads off the actor"** — the
   3-step spec + landmines. (Trust the "exact shape" below, grounded in post-AB3 source.)
2. `src/tyo3/daemon/session_actor.py` — the **one writer thread**. `submit(fn)` runs
   `fn(session)` on the actor and blocks. Today *every* handler funnels through this.
3. `src/tyo3/daemon/handlers.py` — `_METHODS` (`:707`) maps verb → handler; **every**
   handler currently calls `self._actor.submit(work)`. Sort them into reads vs writes
   (below).
4. `src/tyo3/session/views.py` `Snapshot` docstring (`:166`) — "immutable,
   revision-pinned, **thread-shareable**". This is the property AB4 leans on.
5. `src/tyo3/tests/test_final_no_read_side_writes.py` — the **"no read advances head"**
   invariant (§5.3/§5.9). AB4 must preserve it under concurrency.
6. The `session-reads-via-frozen-snapshot` and `phase9-precision-refinement-done`
   memories (the frozen-snapshot read contract + the off-actor precedent), and the QW4
   gotcha in `spine-extensibility-implementation` (id_for/locate are **live-registry**
   ops, not snapshot ops — load-bearing for Task 2 below).

---

## The core gap

The daemon owns one `TyO3Session` on one thread (salsa/MVCC: the session must never be
mutated from two threads). Today `Handlers.dispatch` runs **every** verb through
`actor.submit` — including pure reads (`entity_at`, `decorate`, `derived`, `references`,
…). So a single slow read (a big `check`, or — pre-AB3 — a slow producer) blocks the
actor and **every other read queues behind it**, even though `Snapshot` is immutable and
thread-shareable. Reads don't need the actor; only **writes** do (one-writer rule #3).

AB4: route **reads** onto a small thread pool that shares a **pinned head snapshot**;
keep **writes** on the actor. The actor serialises writes only.

---

## The exact shape (grounded in post-AB3 source)

### Task 0 — write the concurrency test FIRST (`daemon/tests/`)
Before touching the server, write the failing/asserting test:
- **Parallel reads don't serialise.** Fire N concurrent `entity_at`/`derived` where one
  read is artificially slow (e.g. a layer with a small sleep, or a `check` on a big
  file); assert the *other* reads return well under N×(slow time) — they ran in
  parallel, not queued behind the slow one.
- **Writes stay ordered + single-threaded.** Interleave `sync_buffer` commits with
  parallel reads; assert revisions are strictly increasing and no commit is lost/raced.
- **No read advances head.** Port the assertions from
  `test_final_no_read_side_writes.py` to the daemon: hammer reads on the pool, assert
  `head` is unchanged by reads (only writes move it).
- **A read in flight during a write sees a consistent snapshot** (its pinned revision),
  never a half-applied write.

### Task 1 — a shared, write-invalidated head snapshot on the actor
Add `SessionActor.snapshot()` that returns a **pinned `Snapshot`** taken **on the actor
thread** (so the pin itself is serialised with writes). Cache it; **invalidate it when a
write commits** (the next `snapshot()` re-pins at the new head). The actor is the only
thread that creates/swaps the cached snapshot. A read handler asks the actor for the
current pinned snapshot (cheap — a reference hand-off, not a session call) and then reads
**off the pool**, never touching the live session.

### Task 2 — route reads to a pool, keep writes on the actor (`daemon/server.py`, `handlers.py`)
- **Writes stay `actor.submit`:** `sync_buffer`, `sync_buffers`, `author`, `reindex`,
  `gc`. (Also `open` — it touches session setup.)
- **Reads run on a `ThreadPoolExecutor`** against the shared pinned snapshot:
  `entity_at`, `decorate`, `authored`, `derived`, `diff`, `references`,
  `document_highlights`, `hover`, `type_hierarchy`, `can_rename`, `rename` (edit-compute
  only — no commit), `diagnostics_at`, `check`, `layers`, `layer_ids`, `locate`.
- **⚠ The identity landmine (QW4 gotcha):** `id_for` / `locate` are **live-registry ops
  on `TyO3Session`** (they hit `self._inner`, the live native handle — the *snapshot*
  handle does NOT expose them). `entity_at` today calls `s.id_for(...)` **then** reads
  layers off a snapshot. You **cannot** call `s.id_for` from a pool thread concurrently
  with the actor's writes (that's the exact two-threads-touch-the-session violation).
  Resolve this deliberately — pick ONE and document why:
  - (a) Resolve identity **on the actor** (a tiny `actor.submit(id_for)`), then do the
    heavy layer reads **off-actor** over the pinned snapshot; or
  - (b) Serve `id_for`/`locate` over the **pinned snapshot's** revision if/once that's
    expressible without a live-session call (likely needs native surface → out of scope
    here, defer); or
  - (c) Pin identity into the snapshot at creation time on the actor.
  (a) is the pragmatic, in-scope choice: identity hop on the actor (cheap, serialised),
  layer reads on the pool. **Do not** call any `TyO3Session` method from a pool thread —
  only `Snapshot` methods.

### Task 3 — snapshot lifetime under in-flight reads
A cached head snapshot must not be `close()`d while a pool thread is still reading it.
Guard the lifetime: **refcount** the pinned snapshot (acquire before a read, release
after) and only close a *superseded* snapshot once its readers drain — or keep the last
N snapshots alive until idle. A `Snapshot` close while a read is mid-flight is a
use-after-free-class bug. (Snapshots are independent — holding an old one does **not**
block a new write, `session.py` `_invalidate_head_snap` precedent — so "keep until
drained" is cheap.)

### Task 4 — wire-level + back-compat
- Per-connection response ordering still holds (the per-client send lock in
  `server.py::_Client.send` already serialises frames; a pool reply and a pump
  notification must not interleave — verify).
- A read that *errors* on the pool surfaces as the same `ENGINE_ERROR` frame as today.
- `subscribe` stays a server-level verb (AB7); the bus pump is unchanged.

---

## Locked decisions (do NOT re-litigate)
- **Writes single-threaded via the actor; reads on a bounded pool over a shared pinned
  snapshot.** One writer (rule #3) is non-negotiable.
- **Pool threads touch only `Snapshot`, never `TyO3Session`.** Identity resolution
  (`id_for`/`locate`) hops through the actor (Task 2a) until/unless a native snapshot
  identity surface lands (separate, gated).
- **The pinned head snapshot is created/swapped only on the actor thread**, invalidated
  on commit. No pool thread ever calls `session.snapshot()`.
- **No Rust.** AB4 is pure-Python over the existing daemon + `Snapshot` surface.

## Non-obvious gotchas
- **`id_for`/`locate` are live-session ops** (QW4). The single most likely way to
  introduce a race is to "just move `entity_at` to the pool" — its `s.id_for` call would
  run on a pool thread. Split identity (actor) from layer reads (pool).
- **Snapshot close vs in-flight read** — refcount/drain before close (Task 3).
- **Bounded pool** — an unbounded pool lets a flood of reads exhaust threads; size it
  (e.g. small fixed pool) and let excess reads queue *in the pool*, not on the actor.
- **`check`/`diagnostics_at` are heavier** — they are the reads most worth parallelising
  and the ones most likely to expose lifetime bugs; include them in the stress test.
- **Frozen-walk ordering** (`frozen-walk-sorted-ordering` memory) — snapshot reads
  enumerate in sorted order; don't write an assertion that depends on native readdir
  order.
- **Per-connection frame interleaving** — the pump and a pool reply both write to the
  same socket; the existing `_send_lock` covers it, but confirm under concurrency.

## Anchor lines (post-AB3, on `nvim-plugin` after #11 merges)
- `src/tyo3/daemon/session_actor.py` `submit` (`:127`) / `_run` (`:74`) — the one writer
  thread; add `snapshot()` + cached-head-snapshot + commit-invalidate here.
- `src/tyo3/daemon/handlers.py` `_METHODS` (`:707`); every handler's `self._actor.submit`
  call (the read ones move to the pool). `entity_at` (`:133`) is the identity-split
  exemplar; `derived` (`:266`), `decorate` (`:154`), `check` (`:301`), `references`
  (`:321`).
- `src/tyo3/daemon/server.py` `_handle_line` (`:261`) / `dispatch` — where read-vs-write
  routing lands; `_Client.send` (`:73`) per-connection send lock.
- `src/tyo3/session/views.py` `Snapshot` (`:166`, thread-shareable) — what the pool
  reads; `Snapshot.close` (`:447`) — the lifetime hazard.
- `src/tyo3/session/session.py` `snapshot()` (`:398`), `_invalidate_head_snap` (`:386`,
  the "old snapshot doesn't block a new write" precedent).
- `src/tyo3/derive/async_recompute.py` — the AB3 off-actor-read-over-pinned-snapshot
  precedent to mirror.
- `src/tyo3/tests/test_final_no_read_side_writes.py` — the invariant to port to the daemon.

## Ground rules (non-negotiable)
- **Everything through devenv.** Pure-Python ⇒ **no `build`**. Inner loop:
  `devenv shell -- test-fast`. Before the PR (inside devenv): `pytest
  src/tyo3/daemon/tests --no-cov`, the new concurrency test (run it **many** times / with
  `-p no:xdist` and under load — races are flaky by nature), `test-final`, and `ruff
  check` on touched files. Suites are slow — background, budget ~15 min.
- **Golden invariants:** Rust owns committed truth (AB4 adds **no** native change);
  reads over a frozen snapshot; **one writer via `SessionActor`**; don't touch the parity
  oracle or `[profile.dev.package."*"]`; identity rules unchanged.
- **No AI attribution** anywhere (commits, PRs, code, docs).

## Baseline (on `nvim-plugin` after #11)
`test-fast` → ~707 pass / **1** documented baseline fail
(`test_sidecar_is_sole_path_owner_in_source`, demo/tour.py — unrelated). An xdist
shared-fixture flake in `test_concurrency`/`test_property_based` may appear that **passes
serially** (`-p no:xdist`); treat any such as environmental. daemon **49/49**; Lua smoke
**12/12**; `test-final` **43/43**. AB4 only adds passes.

## Definition of done (PR10)
Parallel read RPCs run concurrently (a slow read no longer queues the others) while
writes stay strictly ordered on the actor; `Snapshot` is the only thing pool threads
touch; identity resolution stays serialised; the pinned head snapshot is swapped only on
the actor and never closed under an in-flight read; "no read advances head" holds under
concurrency; the concurrency test (written FIRST) is green and stable across repeated
runs; `test-fast` green (baseline only); daemon + `test-final` green; ruff clean;
`architecture.md` documents the read-pool / one-writer split; `PROGRESS.md` (mark PR10)
+ the `spine-extensibility-implementation` memory updated; push `spine-extend-ab4`, open
a PR (base `nvim-plugin`; no AI attribution). Then stop and report.

> **After AB4:** only **AB8** (identity-preserving native rename) remains — also
> **behind the human-review gate, pair with a maintainer** (it is the one task that
> touches Rust committed-truth: `commit.rs`/`identity.rs` rebind). Do NOT start it
> without sign-off.

## Memories to recall
`spine-extensibility-implementation`, `spine-extensibility-review`,
`session-reads-via-frozen-snapshot`, `frozen-walk-sorted-ordering`,
`phase9-precision-refinement-done`, `devenv-test-entrypoints`, `test-run-timeouts`,
`commit-no-ai-attribution`.
