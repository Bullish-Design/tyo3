# KICKOFF — Daemon `derived`-notification e2e test + async-serve demo scene

You are working in **TyO3**. The spine-extensibility build (Phase A/B/C through **AB3**)
and the Neovim integration are **all merged to `main`** (`709b52c`). AB3 added
**async serve for slow producers**: a `serving="stale"` layer serves last-good/`absent`
at read and recomputes **off the actor**, then publishes a `DerivedFresh` → the daemon
emits a `derived` JSON-RPC notification → the editor re-pulls. This task closes the one
coverage boundary AB3 left and turns the feature into a visible demo beat.

**Two deliverables, one mechanism:**
1. **A daemon e2e test** that proves a `derived` notification actually arrives over the
   wire (the way `test_end_to_end.py` already proves it for `delta` / `refinement`).
2. **A demo recording scene** showing the stale→fresh "pop-in": land on an entity, the
   card shows the slow value as *computing/absent*, then a moment later it fills in
   fresh — "the cursor path never blocks."

Both are driven by **the same single addition**: a slow `serving="stale"` derived layer
in the synthetic "shop" project.

---

## The key realization (why this is small, not a few hours)

I earlier flagged this as expensive because a *registered* `Producer` object can't cross
the process boundary into the spawned daemon. **But a `config.toml` layer whose generator
is a `type = "python"` dotted callable IS resolved by import inside the subprocess** — so
a slow python generator declared in the shop config needs no entry-point plugin. The
daemon opens the project, reads the config, imports the callable, and the whole
async-serve path runs for real. One slow layer in the shop config unlocks both the e2e
test and the demo.

---

## State on entry / read first
1. `src/tyo3/demo/tour.py` — `_build_project(proj)` writes the shop sources **and** its
   `.tyo3/config.toml` (the `[layers.summary]` / `[layers.embed]` blocks, now
   `serving = "block"`; `summary_generator` / `embedding_generator` are the existing
   in-process `generate(inputs)->list[bytes]` callables). This is where the slow layer
   goes — it is the **single source** the CLI tour, the daemon `shop_project` fixture,
   and the vhs demo all build from.
2. `src/tyo3/daemon/tests/test_end_to_end.py` — the e2e harness: `_spawn_daemon` (real
   `python -m tyo3.daemon` subprocess), `_SocketClient` (`request` + `wait_notification`),
   the `shop_project` fixture (`daemon/tests/conftest.py` → `tour._build_project`). It
   already asserts `wait_notification("delta", …)` and `wait_notification("refinement", …)`.
3. `src/tyo3/daemon/bus_pump.py` `_emit_derived` — emits the `derived` notification
   `{revision, layer, durable_id}`. The `BusPump` subscribes `Interest.ALL` at start, so
   a subscriber always exists → the worker **does** publish (`has_subscribers()` is true).
4. `editors/tyo3.nvim/demo/default/tour.tape` (6 scenes today: open/decorate, inspect
   card, author note, identity-preserving move, edit→affected→narrow, affected picker)
   + `demo/setup.sh` (builds the project via `tour._build_project`, resolves a pristine
   `$TYO3_NVIM`). `devenv shell -- demo-record` renders `tour.gif` + `tour.cast`.
5. `editors/tyo3.nvim/lua/tyo3/init.lua` `handle_notification` — already routes
   `"derived"` → `panel.on_derived` + re-decorate (AB3). The Lua side is **done**; the
   demo just needs a slow layer to make it fire visibly.
6. Memories: `nvim-integration-pr`, `nvim-context-panel-demos`, `nvim-demo-pristine-binary`,
   `spine-extensibility-implementation` (AB3 entry), `devenv-test-entrypoints`,
   `test-run-timeouts`, `commit-no-ai-attribution`.

---

## Task 1 — add a slow `serving="stale"` layer to the shop project (`demo/tour.py`)
- Add a python generator callable next to `summary_generator`, e.g.:
  ```python
  def blurb_generator(inputs):
      import time
      time.sleep(1.5)  # simulate an LLM/HTTP call
      return [f"blurb<{(i.source or '').splitlines()[0].strip()}>".encode() for i in inputs]
  ```
  Tune the sleep so the gif clearly shows stale→fresh (~1.2–1.8s) without dragging.
- Add a `[layers.blurb]` block to the `_build_project` config.toml: `origin="derived"`,
  `depends_on=["code"]`, `generator="blurb_gen"`, `serving = "stale"`, `key_locality =
  "local"`, a store, and **`entity_kinds = ["class"]`** (scope it narrowly — see the
  card-assertion gotcha) — plus the `[generators.blurb_gen]` (`type="python"`,
  `callable="tyo3.demo.tour:blurb_generator"`) and `[stores.kv_blurb]` blocks.
- **Gotcha — `entity_at` card layer set.** The card reads every effective layer, so the
  daemon `test_entity_at_card_is_multilayer_and_consistent` / decorate tests may assert a
  specific layer set or "all fresh". Scoping `blurb` to `class` keeps it off the
  function-keyed cards those tests inspect; still, grep the daemon tests for hard-coded
  layer assertions and update any that now legitimately see `blurb` as `absent`/`stale`
  on first read. Run `pytest src/tyo3/daemon/tests --no-cov` after.

## Task 2 — the e2e test (`daemon/tests/test_end_to_end.py`)
A new test (mirror `test_full_flow_over_socket`'s shape):
1. `open` the project; `decorate`/`entity_at` to resolve a **class** id (e.g. `Item`).
2. Fire `derived {layer:"blurb", durable_id:<class id>}`. Assert it returns **promptly**
   (status `absent`/`stale`) — it did **not** block on the 1.5s producer (timing assert:
   the RPC round-trip is far under the sleep).
3. `wait_notification("derived", lambda p: p["layer"]=="blurb" and p["durable_id"]==id,
   timeout=10)` — the off-actor worker finished and the notification crossed the wire.
4. Re-fire `derived` → now `fresh` with the produced artifact (`blurb<…>`).
5. Bonus (actor-not-blocked over the socket): fire the slow `derived` then immediately a
   fast `entity_at`/`ping`; assert the fast one returns without waiting ~1.5s.

## Task 3 — the demo scene (`demo/default/tour.tape`, + `context/` if relevant)
Add a scene (after the inspect-card scene, or as a new Scene): land the cursor on a class
(e.g. `Item` in `catalog.py`), open the card (`:TyO3Inspect`) — the `blurb` row shows
*computing/absent/stale* — then `Sleep 2s` (past the producer + notification), and the
card/decoration **updates in place** to the fresh `blurb<…>` (the `derived` notification
drove `panel.on_derived`'s re-pull). Keep the determinism discipline the tape already
documents (generous sleeps past every async beat). Re-render with `devenv shell --
demo-record`; confirm `tour.gif` shows the pop-in. (If the panel renderer needs a visible
"stale"/"computing" affordance for derived rows, add it in `panel.lua`/`card.lua` — a
small honest-status cue, mirroring the existing `needs_review` ⚠.)

---

## Ground rules / DoD
- **Everything through devenv.** Pure-Python + Lua + config; **no `build`** (no Rust).
- Verify: `pytest src/tyo3/daemon/tests --no-cov` green (update any layer-set assertion
  Task 1 legitimately changed); the new e2e test stable across repeated runs (it has a
  real subprocess + sleep — give it a generous timeout, don't make it flaky); Lua smoke
  12/12; `test-fast` green (baseline only — note: adding `blurb` to the shop config may
  touch `src/tyo3/tests` that build the shop project too, e.g. anything importing
  `tour._build_project`; grep and update); `ruff check` on touched files.
- Re-record the demo (`demo-record`); eyeball `tour.gif` for the pop-in; commit the
  regenerated `tour.gif`/`tour.cast` (they are tracked artifacts).
- **No AI attribution** anywhere.
- Branch off `main`; open a PR to `main` (the nvim-plugin→main umbrella is now merged, so
  `main` is the base for follow-ups). Mark it a docs/test/demo polish PR.
- **Do NOT start AB4 (read concurrency) or AB8 (native rename)** — both remain behind the
  human-review gate.

## Why scope `blurb` to `class`
It keeps the new async layer off the function-keyed cards the existing daemon/demo scenes
inspect (so they stay deterministic), while giving the demo a clean, single place (a class
header) to show the stale→fresh transition. If you'd rather show it on a function, expect
to update more existing assertions — weigh it.

---

## OPTIONAL stretch — the full "spine-story" demo arc (decide before starting)

The tight scope above (e2e test + async pop-in) is the **committed core** — small,
deterministic, no new daemon machinery. Everything below is an *optional* richer storyline
that shows the whole spine-extensibility thesis in one ~15s arc. **Read the cost note
first — it is bigger than it looks, and it is fine to ship the core and defer this.**

**The arc (3 connected beats on one entity):**
1. **Attach anything (AB1/AB6)** — a *custom* layer (not in `config.toml`) shows up on the
   card + inline, proving "register from Python, ride identity like a built-in."
2. **Async pop-in (AB3)** — that layer is slow/`stale`: card shows computing → fills in
   fresh (the core scene above, reused).
3. **Correct by construction (AB2)** — add a caller in another file, re-read → the value
   self-heals to reflect the new caller (the traced read-set / reverse-dependency wow).

**Cost note (the honest part).** Beats 1 and 3 are **not** config.toml-expressible:
- The spawned daemon discovers programmatically-registered layers **only** via
  `tyo3.plugins` entry points (`extend.load_plugins()` at `TyO3Session.__init__`,
  `extend.py:267`). The demo/devenv installs **none**. So beat 1 needs a real
  **entry-point plugin fixture** — a tiny installable package (or a `pyproject` entry in
  the devenv) exposing `[project.entry-points."tyo3.plugins"]` that calls `register_layer`
  — present in the daemon subprocess's environment. That is genuine packaging/devenv work,
  not a tape edit.
- A **true traced read-set** layer (beat 3, AB2) is the recording-`Producer` path — also
  the registration API, so it rides the same entry-point fixture. A *cheaper approximation*
  of the self-heal beat is a **`key_locality = "reverse-semantic"`** config.toml layer (no
  plugin): it recomputes when a *direct* caller changes, which demos the "add a caller →
  value updates" beat without the full traced machinery. Use that if you want beat 3
  without the plugin fixture, and say so in the narration (it's an honest, simpler variant).

**Recommendation.** Ship the **core** (e2e test + config.toml `blurb` async pop-in) as the
PR. If you want the arc, do it as a **follow-up** PR that adds the `tyo3.plugins` entry-point
demo fixture once, then layers beats 1+3 onto the recording — so the packaging cost is
isolated and reviewable on its own. Don't bundle the fixture work into the e2e/pop-in PR.
