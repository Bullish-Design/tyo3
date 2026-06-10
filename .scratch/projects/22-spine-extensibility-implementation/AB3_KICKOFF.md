# KICKOFF — PR9 = AB3: Async serve for slow producers

You are continuing the **TyO3 spine-extensibility build**. Phase A (4 QW PRs),
all of Phase B (AB1/AB5/AB7), and the first Phase-C PR (**AB2** producer protocol +
traced read-sets) are **landed and MERGED into `nvim-plugin`**. AB2 is at PR #10,
fast-forwarded into `nvim-plugin` @ **`5524f60`**. AB3 is the **second Phase-C PR**:
it makes a *slow* (LLM/HTTP/embedding) producer layer stop blocking the cursor
path. Start coding at Task 1.

This is a **pure-Python PR (no Rust, no `build`)**. It builds directly on AB2 —
the produce path AB2 shipped (`dag.produce_artifact` / `dag.derived_traced` /
`dag._produce_code`) is already **reentrant and enqueue-able** (takes
`(layer, snapshot, durable_id)`, runs over any pinned snapshot). AB3 moves the
*execution* off the actor for `serving="stale"` layers; it does **not** rewrite the
produce logic.

---

## State on entry (verify, do NOT redo)

- **AB2 MERGED.** `nvim-plugin` @ `5524f60` contains AB1+AB5+AB7+AB2. Umbrella
  **PR #2 (nvim-plugin → main) is still open — do not touch it.**
- **Branch:** `git checkout nvim-plugin && git pull && git checkout -b
  spine-extend-ab3`. Open the PR with **base `nvim-plugin`**.
- **The produce path is reentrant** (AB2, `derive/dag.py`): `produce_artifact(layer,
  snapshot, durable_id, gen_input)` (override/layer-derived), `derived_traced(layer,
  snapshot, durable_id) -> DerivedValue` (traced, produce-then-key with a cheap
  self-heal check), `_produce_code(layer, snapshot, durable_id) -> (bytes|None,
  read_set)`. Reuse these from the worker — **don't duplicate produce logic.**

## Read first (in order)
1. `21-.../IMPLEMENTATION_GUIDE.md` → **"TASK AB3"** (`:517`) — the 3-step spec.
   (Its line anchors drift; trust the "exact shape" below, grounded in post-AB2
   source @ `5524f60`.)
2. `src/tyo3/precision/refiner.py` (`:53-121`) — **the worker pattern to mirror**:
   a `PrecisionRefiner` with a daemon thread + `queue.Queue` + `start`/`stop`/
   `feed`, lazily started, drained off the commit hot path, publishing on the bus
   only when `bus.has_subscribers()` (`:125-138`). AB3's recompute worker is the
   same shape, fed `(layer, id, revision)` instead of `(rev, changed, affected)`.
3. The `phase9-precision-refinement-done` memory (the refiner's async/graceful-
   degradation contract you are mirroring).
4. `SPIKE_FINDINGS.md` §5.2b / API_DESIGN §5.2b (the LLM callsite-summary example —
   exactly the slow expensive-reverse layer AB3 unblocks) and the
   `spine-extensibility-implementation` memory (chain state, golden rules, the AB2
   "Known boundary" note).

---

## The core gap

Both AB2 read paths produce **synchronously at read** on a cache miss:
- override layers → `scheduler.recompute_now(...)` (`session/views.py:346`);
- traced layers → `dag.derived_traced(...)` produce-then-key (`views.py:325`).

The daemon serves reads **on the single actor thread** (`actor.submit`), so a slow
LLM/HTTP/embedding producer stalls the actor for seconds on the cursor path —
every other read queues behind it. Today `serving="stale"` and `serving="block"`
**both** recompute synchronously (`views.py` comment at the miss seam). AB3 makes
`serving="stale"` mean what it says: serve last-good/absent *now*, recompute in the
background, publish when ready.

---

## The exact shape (grounded in post-AB2 source @ 5524f60)

### Task 1 — the async recompute worker (`derive/async_recompute.py`, new)
Implement a `DerivedRecomputeWorker` mirroring `PrecisionRefiner`
(`refiner.py:53-121`): a daemon `threading.Thread` draining a `queue.Queue`, with
`start()` (idempotent, lazy), `stop()` (drain + join, called from
`session.close()`), and `enqueue(layer_name, durable_id, revision)`. It holds a
session ref. `_run` drains items and for each:
1. **Dedup** by `(layer_name, durable_id, revision)` — a set of in-flight keys so
   repeated reads before the first completes don't stampede the producer.
2. Pin `snap = session.snapshot(at=revision)`; on failure (revision evicted) skip
   gracefully — the coarse last-good stands (mirror `refiner._compute_narrowed`
   returning `None`).
3. Recompute through the **existing** DAG seam: for a traced layer
   `dag.derived_traced(L, snap, id)` (it caches + binds as a side effect); for an
   override layer `gen_input, h = dag.resolve_input(L, snap, id)` →
   `dag.produce_artifact(...)` → `cache.put` + `L.bind(...)` (mirror
   `scheduler.recompute_now`). Reuse, don't reimplement.
4. On success **publish** a "layer X id Y at revision R is now fresh" message on the
   bus (Task 3) — but only if `bus.has_subscribers()`. Either way the cache is now
   warm, so the next read is `fresh` even without a notification (in-process use).
5. Close the pinned snapshot in `finally`; swallow producer exceptions and keep the
   worker alive (graceful degradation — a failed recompute is never a miss; the
   last-good already served).

Lifecycle: lazily built + started on first enqueue (mirror `session._get_refiner`,
`session.py:690`); stopped in `session.close()` alongside the refiner
(`session.py` close path, before bus close).

### Task 2 — serve-stale-then-enqueue at the read seam (`session/views.py`, `derive/dag.py`)
The policy is already encoded in `serving` — **no new spec field**:
- **`serving="stale"`** ⇒ on a cache miss, return last-good (`stale`) or `absent`
  **immediately** and `worker.enqueue(layer, id, revision)`. Never block.
- **`serving="block"`** ⇒ keep today's synchronous produce (`recompute_now` /
  inline `derived_traced` produce). Unchanged.

Thread this into **both** miss paths:
- `Snapshot.derived` override branch (`views.py:~340-361`): split the
  `recompute_now` call on `L.serving`.
- `dag.derived_traced` (`dag.py`): the **cheap self-heal check stays on the read
  thread** (it's O(read-set) hashing, not the slow producer). On a cheap-check miss,
  if `serving=="stale"` → serve last-good/absent + enqueue; if `"block"` → produce
  inline as AB2 does today.

The worker runs **off the actor** (golden rule #3 permits this — it's a
reader/recompute over a *frozen pinned snapshot*, never a writer to committed
truth; the actor still serializes the only writes). `PrecisionRefiner` already
takes `session.snapshot(at=rev)` from its daemon thread, so the pattern is proven.

### Task 3 — publish "now fresh" + editor re-pull (`bus/`, `daemon/`, Lua)
- A bus message for "derived value became fresh": add `DerivedFresh(revision, layer,
  durable_id)` (mirror `bus/refinement.py::AffectedRefinement`) and
  `Bus.publish_derived_fresh(...)` (mirror `Bus.publish_refinement`,
  `bus/bus.py:123`), travelling on its own channel (out-of-band, like refinements —
  it may arrive after later revisions' deltas; never participate in the primary
  `revision > last` ordering).
- `daemon/bus_pump.py` drains that channel and emits a JSON-RPC notification (a new
  `derived` notification, or extend the `refinement` route) carrying `{layer, id,
  revision}`. `daemon/server.py` broadcasts it.
- Lua `init.handle_notification`: route the new notification → re-pull
  `entity_at`/`derived` for that `id`+`layer` and refresh the card/decoration
  (mirror the existing `refinement`/`delta` routing).

### Task 4 — tests (`src/tyo3/tests/test_registration.py` + daemon e2e)
- **A sleeping producer** (e.g. `time.sleep(0.4)` in `produce`). Register it with
  `serving="stale"`. Assert: the first `s.derived(...)` returns **promptly**
  (wall-clock < the sleep) with status `stale`/`absent`; after the worker finishes
  (poll/join), a later read returns `fresh` with the produced value. Use the
  `_clean_registries` autouse fixture.
- **Actor-not-blocked** (daemon e2e): fire the slow `derived` then a fast read
  (e.g. `entity_at`) and assert the fast read returns without waiting for the slow
  producer (timing or an ordering probe).
- **`serving="block"` unchanged:** a blocking producer's first read returns `fresh`.
- **Dedup:** two rapid reads of the same missing id enqueue/produce **once**.

---

## Locked decisions (do NOT re-litigate)
- **`serving="stale"` ⇒ async background recompute; `serving="block"` ⇒ synchronous
  (today's behaviour).** No new `DerivedLayerSpec` field — `serving` already encodes
  the policy.
- **The worker runs OFF the actor**, over a frozen pinned snapshot, and is a
  *reader/recompute* — it never writes committed truth (rule #3 holds: one writer,
  the actor). Reuse AB2's reentrant `dag.produce_artifact` / `derived_traced` —
  don't duplicate produce logic.
- **Honest status preserved:** `stale`/`absent` first, `fresh` after the worker
  publishes. The honest-staleness model (`views.py`) is untouched.
- **No Rust.** AB3 is pure-Python over the existing snapshot + bus + daemon surface.

## Non-obvious gotchas
- **The cheap self-heal check stays on the read thread** for traced layers — it's
  cheap (re-fingerprint the stored read-set). Only the *slow producer* is enqueued.
  Don't push the whole `derived_traced` onto the worker; push only the produce.
- **Evicted revision** → `session.snapshot(at=rev)` fails → skip gracefully (mirror
  `refiner._compute_narrowed` → `None`); the last-good already served stands.
- **Off-actor cache writes race actor reads.** The cache is content-addressed (key
  includes `input_hash`), so a background `put` is *additive* (a new key); the
  binding-dict update is a single GIL-atomic assignment. Last-writer-wins is fine;
  document it. Do **not** add locks that could deadlock the actor.
- **Bus overflow must be non-blocking** (mirror the refinement channel's bounded
  queue / drop-newest; `bus_pump.py` / `bus.py`). A full subscriber queue must never
  block the worker or the actor.
- **No subscribers ⇒ still recompute + cache** (so in-process / test use self-heals
  on the next read); only *skip the publish* when `not bus.has_subscribers()`.
- **Dedup is per `(layer, id, revision)`** — a new commit (new revision) is a new
  work item even for the same id.

## Anchor lines (post-AB2, on `nvim-plugin` @ 5524f60)
- `src/tyo3/session/views.py:301` `Snapshot.derived`; `:324-325` traced branch →
  `dag.derived_traced`; `:346` `scheduler.recompute_now` (the sync miss seam to
  split on `serving`); `:351` last-good serve.
- `src/tyo3/derive/dag.py` `derived_traced` / `produce_artifact` / `_produce_code`
  (the reentrant produce seam — reuse from the worker); `resolve_input` (override
  key).
- `src/tyo3/derive/scheduler.py` `recompute_now` (the sync recompute to mirror in
  the worker, now producing via `dag.produce_artifact`).
- `src/tyo3/derive/layer.py:35` `serving`; `:53` `_last_good`; `last_good_store_key`
  / `bind` / `bind_traced`.
- `src/tyo3/precision/refiner.py:53-121` (worker pattern), `:125-138` (publish-if-
  subscribers + graceful degradation).
- `src/tyo3/bus/refinement.py` (`AffectedRefinement`), `src/tyo3/bus/bus.py:123`
  `publish_refinement`, `:150` `has_subscribers`.
- `src/tyo3/daemon/bus_pump.py` (refinement drain + broadcast), `daemon/server.py`
  (broadcast), Lua `editors/tyo3.nvim/lua/tyo3/init.lua` `handle_notification`.
- `src/tyo3/session/session.py:99` `_refiner` field, `:690` `_get_refiner`, `:714`
  `.feed(...)`, `close()` (stop the worker here too), `:104` `_refinement_mode`.
- Config: `LayerConfig.serving` (`config.py`); `DerivedLayer.serving` (`layer.py:35`).

## Ground rules (non-negotiable)
- **Everything through devenv.** Pure-Python ⇒ **no `build`**. Inner loop:
  `devenv shell -- test-fast`. Before the PR (inside devenv): `pytest
  src/tyo3/tests/test_registration.py --no-cov`, `pytest src/tyo3/daemon/tests
  --no-cov`, `test-final`, and `ruff check` on touched files. Suites are slow —
  background, budget ~15 min.
- **Golden invariants:** Rust owns committed truth (AB3 adds **no** native change);
  reads/recompute over a frozen snapshot; **one writer via `SessionActor`** — the
  worker is off-actor but never writes committed truth; don't touch the parity
  oracle or `[profile.dev.package."*"]`; identity rules unchanged.
- **No AI attribution** anywhere (commits, PRs, code, docs).

## Baseline (on `nvim-plugin` @ 5524f60 — all fails are environmental)
`test-fast` → **~702 pass / 1 fail**; the fail is the documented
`test_sidecar_is_sole_path_owner_in_source` (demo/tour.py). An xdist shared-fixture
flake may also appear that **passes serially** (`-p no:xdist`). daemon **49/49**;
Lua smoke **12/12**; `test-final` **43/43**. AB3 only adds passes.

## Definition of done (PR9)
A slow `serving="stale"` producer **never blocks the cursor path**: the read returns
`stale`/`absent` promptly and a later read (after the off-actor worker publishes)
returns `fresh`; `serving="block"` is unchanged (synchronous); the worker runs off
the actor with graceful degradation on evicted revisions + per-`(layer,id,revision)`
dedup; a "now fresh" notification flows through the bus → daemon → editor, which
re-pulls; `test-fast` green (baseline only); daemon + `test_registration.py` +
`test-final` green; ruff clean; `architecture.md` documents the async-serve model
(stale-then-fresh, the new notification, off-actor recompute); `PROGRESS.md` (mark
PR9 landed) + the `spine-extensibility-implementation` memory updated; push
`spine-extend-ab3`, open a PR (base `nvim-plugin`; no AI attribution). Then stop and
report.

> **Note for the planner:** AB4 (read concurrency) and AB8 (native rename) are
> **behind a human-review gate** — do NOT start them. AB3 is the last
> non-gated Phase-C PR.

## Memories to recall
`spine-extensibility-implementation`, `spine-extensibility-review`,
`session-reads-via-frozen-snapshot`, `phase9-precision-refinement-done`,
`phase8-derived-invalidation-done`, `devenv-test-entrypoints`, `test-run-timeouts`,
`commit-no-ai-attribution`.
