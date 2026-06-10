# KICKOFF — PR7 = AB7: per-layer subscription streaming

You are continuing the **TyO3 spine-extensibility build**. AB1 (registration
keystone) and AB5 (optional per-layer schemas) are **landed**. AB7 is the last
Phase-B PR: let a daemon client **subscribe to a slice** of the delta stream
(a layer, a set of files, or a set of ids) instead of receiving every commit.

This is a **daemon + small-Python PR (no Rust, no `build`)**, plus an *optional*
Lua touch. The bus filtering machinery (`Interest` / `Delta.scoped_to` /
`Bus.publish` matching) **already exists and is unit-tested** — AB7 wires it the
last mile: (1) stamp the *specific* authored layer name onto the published delta,
and (2) give each socket connection its own `Interest` and fan deltas out per
connection. Start coding at Task 1.

---

## State on entry (verify, do NOT redo)

- **AB1 landed** → `spine-extend-ab1` @ `c48f262`, **PR #7** open → `nvim-plugin`.
- **AB5 landed** → `spine-extend-ab5` @ `7077be2`, **PR #8** open → base
  `spine-extend-ab1`. (Stacked chain: `nvim-plugin ← ab1 ← ab5`.)
- **Branch:** create `spine-extend-ab7` **off `spine-extend-ab5`** (`7077be2`):
  `git checkout spine-extend-ab5 && git pull && git checkout -b spine-extend-ab7`.
  Open the PR with **base `spine-extend-ab5`** (clean stacked diff); retarget up
  the chain as #7/#8 merge. (If the chain has already merged into `nvim-plugin`
  when you start, branch off `nvim-plugin` and target it directly.)
- **Why stack on AB5, not AB1:** AB7 edits `session.author` (threads a layer name
  into `_after_commit`) and AB5 already edited the *top* of `session.author`
  (schema validation). Same function → stack to avoid a conflict.
- **The filtering primitives already exist and are green** (`test_gate8_bus.py`
  `TestInterest`): `Interest.layer("intent")`, `Interest.files_of`,
  `Interest.ids_of`, `Interest.ALL`, `Interest.matches(ids, files, layers)`,
  `Interest.__or__`, `Delta.scoped_to(interest)`. `Bus.publish` already fans out
  per-subscriber with exactly these semantics (`bus.py:103-121`). **Do not
  reinvent any of this — reuse it.**

## Read first (in order)
1. `21-.../IMPLEMENTATION_GUIDE.md` → **"TASK AB7"** (`:426`) — the 3-step spec +
   §0 golden rules + §0.1 the devenv build/test loop.
2. `ASSESSMENT.md` §6 (why `Interest.layer` exists but the daemon only ever
   subscribes `ALL` and the delta doesn't carry the real layer name).
3. This file's **"The exact shape"** below — it is grounded line-by-line in the
   post-AB5 source; trust it over older design prose where they differ.
4. The `spine-extensibility-implementation` memory (chain state, golden rules).

---

## The core gap (two halves)

**Half 1 — the published delta loses the layer name.** `Delta.from_commit_delta`
is a *pure projection* of `CommitDelta` (`bus/delta.py:100`), and `CommitDelta`
carries `authored_ids` but **not which layer** they were authored into
(`models/delta.py:45`). So `_layers_touched` (`bus/delta.py:129`) can only emit
the generic strings **`"code"`** (any structural change) and **`"authored"`**
(any authored write) — never `"intent"` / `"summary"` / a registered layer name.
A subscriber to `Interest.layer("intent")` therefore never matches. The layer
name **is** known — at `session.author(layer, …)` time — it just isn't threaded
to the publish step. **Half 1 threads it through (Python-only; no Rust, the
native `CommitDelta` is untouched).**

**Half 2 — every client gets every delta.** `BusPump` subscribes once on
`Interest.ALL` (`bus_pump.py:65`) and `DaemonServer.broadcast` writes the *same
pre-encoded* notification to **every** connected socket (`server.py:119`). There
is no per-connection interest. **Half 2 gives each `_Client` its own `Interest`,
adds a `subscribe` RPC, and makes the fan-out per-connection** (mirroring the
exact match/scope semantics `Bus.publish` already uses).

---

## The exact shape (grounded in post-AB5 source)

### Task 1 — stamp the specific authored layer onto the delta (`session/session.py`)

Thread the authored layer name from `author` through the single post-commit hook
to the projection. Three small edits, all default-None so the other ~8 writers
are unchanged:

- **`author` (`:803`, post-AB5):** it already validates (AB5) then
  `self._after_commit(result)` (`:854`). Change that one call to
  `self._after_commit(result, touched_layer_names=(layer,))`.
- **`_after_commit` (`:661`):** add `*, touched_layer_names: tuple[str, ...] = ()`
  and pass it down to `self._publish_delta(delta, touched_layer_names)`. Leave the
  graph-apply / derived-schedule / refine steps untouched.
- **`_publish_delta` (`:349`):** add the same kwarg (default `()`); pass it to
  `Delta.from_commit_delta(result, extra_layers=touched_layer_names)`.
- **`Delta.from_commit_delta` (`bus/delta.py:100`):** add
  `extra_layers: tuple[str, ...] = ()`; union it into the `layers=` argument:
  `layers=_layers_touched(delta) | frozenset(extra_layers)`. Keep `_layers_touched`
  emitting the generic `"code"`/`"authored"` too — **additive, for back-compat**
  (a subscriber to `layer("authored")` still works; `layer("intent")` now also
  works). Update the docstring (it currently says layers are keyed off id fields).

> A code edit still touches only `"code"` (no `extra_layers`). An authored intent
> write now touches `{"authored", "intent"}`. That is the whole behavioural change
> of Half 1.
>
> **Derived layers:** the IMPLEMENTATION_GUIDE mentions "derived layers that
> recomputed." In this engine derivation is **lazy** (`recompute="lazy"`) — nothing
> recomputes *inside* the commit, so there is no derived layer to stamp at publish
> time. Do **not** invent an eager-derive path here; AB7's layer stamping is
> authored-only. Note this in the PR so the deviation from the older prose is
> explicit.

### Task 2 — per-connection interest + `subscribe` RPC + filtered fan-out (`daemon/`)

**`_Client` (`server.py:55`):** add `self.interest: Interest = Interest.ALL`
(import `from tyo3.bus.interest import Interest`). `ALL` is the back-compat default
so an editor that never calls `subscribe` keeps receiving every delta (the e2e
test and the Lua plugin rely on this).

**`subscribe` RPC — handle it at the *server*, not in `Handlers`.** Critical
constraint: `Handlers` is a **single shared instance** dispatched for every
connection (`server.py:104`, `handlers.dispatch` → module-level `_METHODS`,
`handlers.py:704`) — it has **no per-connection identity**. So `subscribe` cannot
live in `_METHODS`. Special-case it in `DaemonServer._handle_line` (`server.py:213`)
*before* `self._handlers.dispatch(...)`: when `req.method == "subscribe"`, build an
`Interest` from `req.params` and assign `client.interest`, then reply with an ack:

```python
if req.method == "subscribe":
    p = req.params or {}
    client.interest = Interest(
        files=frozenset(p.get("files", ())),
        ids=frozenset(p.get("ids", ())),
        layers=frozenset(p.get("layers", ())),
        all=bool(p.get("all", False)),
    )
    client.send(encode_response(req.id, {"ok": True}))
    return
```
(An empty `subscribe {}` ⇒ `Interest()` = matches nothing — a client mutes itself;
that's a legitimate use. `subscribe {"all": true}` restores ALL.) Add `"subscribe"`
to the advertised method list so `ping` reports it — e.g. a small constant the
server contributes, or extend `Handlers.methods`. Keep it discoverable.

**Make the fan-out per-connection.** Today `BusPump._emit_delta` pre-encodes one
string and calls `self._broadcast(line)` (→ `server.broadcast`, sends to all).
Change the delta path so the pump hands the server the **raw `Delta`** and the
server scopes/encodes per client:

- **`BusPump.__init__`:** take a `broadcast_delta: Callable[[Delta], None]`
  (keep the existing string `broadcast` for refinements). Server passes
  `self.broadcast_delta` and `self.broadcast`.
- **`BusPump._emit_delta` (`bus_pump.py:112`):** keep the `self._tracker.record(...)`
  call (delta-level, must run once regardless of clients), then call
  `self._broadcast_delta(delta)` instead of building params + `self._broadcast`.
  Move the `params` construction into the server (it now happens per client).
- **New `DaemonServer.broadcast_delta(delta)`** — mirror `Bus.publish`'s exact
  semantics (`bus.py:103-121`) per client:
  ```python
  def broadcast_delta(self, delta):
      with self._clients_lock:
          clients = list(self._clients)
      dead = []
      for c in clients:
          interest = c.interest
          if interest.all or delta.rescan:
              scoped = delta.scoped_to(interest)
          elif interest.matches(
              affected_ids=delta.affected,
              affected_files=delta.files,
              touched_layers=delta.layers,
          ):
              scoped = delta.scoped_to(interest)
              if scoped.is_empty():
                  continue
          else:
              continue
          line = encode_notification("delta", _delta_params(scoped))
          if not c.send(line):
              dead.append(c)
      if dead:
          with self._clients_lock:
              for c in dead:
                  self._clients.discard(c)
  ```
  where `_delta_params(d)` is the dict currently built in `bus_pump._emit_delta`
  (`revision`/`created_ids`/`changed_ids`/`deleted_ids`/`moved_ids`/`authored_ids`/
  `affected_ids`/`touched_files`/`rescan`). Move that helper to the server (or a
  shared module) since encoding is now per client.

**Refinements stay broadcast-to-all** (back-compat, lowest risk): leave
`BusPump._emit_refinement` → `self._broadcast(line)` → `server.broadcast` exactly
as today. A refinement is keyed by revision; a client that didn't receive that
revision's delta simply ignores it. (Scoping refinements by interest is a clean
follow-up, not required for AB7's acceptance.)

### Task 3 — plugin (OPTIONAL — recommend leaving for green)

`init.lua` already routes `delta`/`refinement` notifications
(`init.lua:140-151`) and relies on receiving every delta. The back-compat default
(`_Client.interest = Interest.ALL`) means **no Lua change is required** and the
smoke test stays 12/12. *Optionally*, send `subscribe {files=<open buffers>}` on
attach to cut noise — but only do this if smoke + e2e stay green; otherwise skip
it and note it as a follow-up. **Do not block the PR on a Lua change.**

### Task 4 — tests

**Bus/Delta unit (`src/tyo3/tests/test_gate8_bus.py`):** add a test that
`Delta.from_commit_delta(cd, extra_layers=("intent",))` yields
`delta.layers ⊇ {"intent", "authored"}` for an authored CommitDelta, and that
`Interest.layer("intent").matches(delta.affected, delta.files, delta.layers)` is
True while `Interest.layer("summary").matches(...)` is False. (Reuse the
`from_commit_delta` construction pattern already in `TestInterest`/the
`from_commit_delta` tests in that file.)

**Daemon e2e (`src/tyo3/daemon/tests/test_end_to_end.py`)** — the acceptance test,
using the existing `DaemonClient` harness (`request` + `wait_notification`):
- Open **two** client connections to the same daemon.
- `clientA.request("subscribe", {"layers": ["intent"]})`,
  `clientB.request("subscribe", {"layers": ["summary"]})`.
- Author an **intent** value on some id
  (`request("author", {"layer": "intent", …})`).
- Assert **A receives** a `delta` notification for that revision (with the id in
  `authored_ids`) and **B does not** (use a short timeout + assert `TimeoutError`,
  or drain B's queue and assert no delta for that revision). Mirror the existing
  `wait_notification("delta", lambda p: p["revision"] == rev)` pattern.
- (Optional) a third default (no-subscribe) client still receives the delta —
  proves `ALL` back-compat.

**Acceptance:** a client receives only deltas matching its interest; un-subscribed
clients still receive everything.

---

## Locked decisions (do NOT re-litigate)
- **Reuse the existing filtering primitives.** `Interest` / `Delta.scoped_to` /
  the match semantics in `Bus.publish` are correct and tested — AB7 only wires
  them to the socket layer and supplies the missing layer name. No new matching
  logic.
- **Layer stamping is Python-side & additive.** Thread the name from
  `session.author`; keep `"code"`/`"authored"` generic strings for back-compat.
  Native `CommitDelta` is **not** changed (no Rust).
- **`Interest.ALL` is the per-connection default.** Back-compat is non-negotiable:
  the e2e/smoke clients never subscribe and must keep getting every delta.
- **`subscribe` is server-level, not a `Handlers` method.** `Handlers` is shared
  across connections and connection-blind. Don't try to give it per-client state.
- **Overflow stays non-blocking; delivery stays off the actor.** Do not touch the
  bus overflow policy (`config.rs:464`) or move fan-out onto the actor thread —
  the pump already runs on its own thread (`bus_pump.py`).
- **Refinements stay broadcast-to-all** this PR (scoping them is a follow-up).

## Non-obvious gotchas
- **`Bus.publish`'s scoped match keys on `delta.affected` only** (`bus.py:114-117`),
  *not* on `authored`/`changed`. For an **authored-only** commit `delta.affected`
  is empty, so an `ids_of({authored_id})` interest will **not** match — but a
  `layer("intent")` interest **will** (it matches on `touched_layers=delta.layers`,
  which Half 1 now populates). The acceptance test is layer-based for exactly this
  reason; don't "fix" it by adding `authored` to the affected match (that would
  change established semantics — out of scope).
- **`scoped_to` keeps all ids for a file/layer-only interest** (`delta.py:63-80`):
  a `layer("intent")` subscriber still sees the full `authored_ids` list (the bus
  already decided the delta is relevant). So `scoped.is_empty()` is False for an
  authored write — it *is* delivered. Good.
- **ALL/rescan always deliver even an empty delta** (`bus.py:109`) so a client can
  pin a snapshot at that revision. Mirror this in `broadcast_delta` — the
  `interest.all or delta.rescan` arm sends without the `is_empty` skip.
- **`tracker.record` must run once per delta**, independent of how many clients
  match — keep it in `BusPump._emit_delta`, before `broadcast_delta`. Don't move it
  into the per-client loop.
- **`has_subscribers()` gate.** `_publish_delta` early-returns if the bus has no
  subscribers (`session.py:357`). The `BusPump` still subscribes `Interest.ALL`
  (so the bus always has ≥1 subscriber while the daemon runs) — keep that ALL
  subscription; per-client filtering happens at the server fan-out, not the bus.
- **Two-connection e2e timing.** Give `wait_notification` a real timeout and assert
  B's *absence* with a bounded wait (the suite is slow; don't busy-spin). The
  harness puts notifications on a `Queue` keyed by method — drain/inspect B's queue
  rather than assuming order.

## Anchor lines (post-AB5, on `spine-extend-ab5` @ 7077be2)
- `src/tyo3/session/session.py:803` `author` (call site `:854` → add
  `touched_layer_names=(layer,)`); `:661` `_after_commit`; `:349` `_publish_delta`.
- `src/tyo3/bus/delta.py:100` `from_commit_delta`; `:129` `_layers_touched`.
- `src/tyo3/bus/interest.py` — `Interest` (reuse as-is); `:60` `matches`.
- `src/tyo3/bus/bus.py:103-121` — the publish fan-out to mirror.
- `src/tyo3/daemon/bus_pump.py:65` ALL subscription; `:112` `_emit_delta` (raw
  Delta hand-off); `:132` `_emit_refinement` (leave as-is).
- `src/tyo3/daemon/server.py:55` `_Client` (+`interest`); `:119` `broadcast`
  (refinements); add `broadcast_delta`; `:213` `_handle_line` (subscribe case);
  `:105` `BusPump(...)` construction (pass `broadcast_delta`).
- `src/tyo3/daemon/handlers.py:704` `_METHODS` (subscribe is NOT here — server-level).
- `src/tyo3/tests/test_gate8_bus.py` — `TestInterest` + `from_commit_delta` tests.
- `src/tyo3/daemon/tests/test_end_to_end.py` — `DaemonClient` harness (`request` /
  `wait_notification`); author-over-the-wire pattern at `:215`.

## Ground rules (non-negotiable)
- **Everything through devenv.** Pure-Python/daemon ⇒ no `build`. Inner loop:
  `devenv shell -- test-fast`. Before the PR (inside devenv): `pytest
  src/tyo3/daemon/tests --no-cov`, `pytest src/tyo3/tests/test_gate8_bus.py
  --no-cov`, `test-final`, and the Lua smoke: `nvim --headless --clean -u
  editors/tyo3.nvim/tests/minimal_init.lua -c "luafile
  editors/tyo3.nvim/tests/smoke.lua"`. Suites are slow — background, budget ~15
  min. Run `ruff check` on touched files.
- **Golden invariants:** Rust owns committed truth (AB7 adds **no** native change);
  reads over a frozen snapshot; one writer via `SessionActor`; revision-order
  publish invariant intact (don't touch `Bus.publish`'s `revision >` assert);
  identity rules unchanged. Don't touch the parity oracle or
  `[profile.dev.package."*"]`.
- **No AI attribution** anywhere (commits, PRs, code, docs).

## Baseline (on `spine-extend-ab5` @ 7077be2 — all fails are environmental)
`test-fast` → **695 pass / 1 fail**; the fail is the documented
`test_sidecar_is_sole_path_owner_in_source` (demo/tour.py). An xdist shared-fixture
flake may also appear (e.g. a `test_rust_integration`/`test_concurrency` case) that
**passes serially** (`-p no:xdist`). daemon **48/48**; Lua smoke **12/12**;
`test-final` **43/43**. AB7 only adds passes (≈1 bus unit + 1 daemon e2e).

## Definition of done (PR7)
`Delta` carries the specific authored layer name (additive); each socket
connection has its own `Interest` (default `ALL`); a `subscribe` RPC sets it; the
delta fan-out is per-connection (mirroring `Bus.publish` semantics); refinements
unchanged; un-subscribed clients still get everything; ~2 new tests (bus unit +
two-connection daemon e2e); `test-fast` green (baseline only); daemon tests green;
`test-final` green; Lua smoke 12/12; `architecture.md` documents the `subscribe`
verb + per-connection delivery (and `delta.layers` carrying real layer names);
`PROGRESS.md` (mark PR7 landed) + the `spine-extensibility-implementation` memory
updated; push `spine-extend-ab7`, open a PR (base `spine-extend-ab5`, retarget up
the chain as #7/#8 merge; no AI attribution). Then stop and report.

## Memories to recall
`spine-extensibility-implementation`, `spine-extensibility-review`,
`session-reads-via-frozen-snapshot`, `phase7-pure-projection-bus-done`,
`phase8-derived-invalidation-done`, `devenv-test-entrypoints`,
`test-run-timeouts`, `commit-no-ai-attribution`, `nvim-integration-pr`.
