# Neovim plugin — build-to-completion kickoff

> **Execution guide for a fresh session.** Pair this with `OVERVIEW.md` (the
> design) in this same directory — read **both** before starting. This file is
> the *how*; `OVERVIEW.md` is the *what/why*. Goal: ship the **full, complete,
> usable** TyO3 Neovim plugin — daemon + Lua plugin, tested and documented — not
> an MVP stub.

---

## 0. Mission

Build, end to end:
1. **`tyo3d`** — a Python daemon wrapping one `TyO3Session`, serving JSON-RPC over
   a unix socket and pumping the delta bus to clients as notifications.
2. **`tyo3.nvim`** — a Lua plugin that drives `tyo3d`: buffer↔overlay sync,
   durable-id-anchored virtual-text notes/summaries, a floating entity inspector,
   an affected-set panel, Telescope pickers, and user commands.

"Usable" = a developer can `:packadd`/lazy-load it, open a Python project, and
see live identity/notes/affected-set intelligence as they edit. Deliver all four
phases in `OVERVIEW.md` §7 (A–D), not just A.

---

## 1. Working rules (this repo)

- **Branch:** create `nvim-plugin` off `main` (which is at `v0.2.0`). Do all work
  there. Commit logically; **no AI attribution** in commits/PRs/docs (house rule).
- **Everything through `devenv shell --`** (Nix). Never bare `cargo`/`ruff`/`pytest`.
  Key scripts: `build` (maturin develop — needed before pytest if Rust changes),
  `test-fast` (parallel, no-cov), `tests` (full), `ruff check`, `ruff format`.
  Suites are slow (~10–15 min); run full suites in the background.
- **No Rust changes should be needed.** The engine is complete; the daemon only
  *wraps* the existing `TyO3Session`. If you think you need a core change, stop and
  reconsider — it almost certainly belongs in the daemon.
- **Gate before committing daemon code:** `devenv shell -- ruff check src` and
  `ruff format --check src` clean; daemon pytest green.

---

## 2. Where things live (create these)

```
src/tyo3/daemon/
  __init__.py
  __main__.py        -- `python -m tyo3.daemon` → serve
  server.py          -- socket server + JSON-RPC dispatch loop
  session_actor.py   -- single-threaded owner of the TyO3Session (serialize writes)
  bus_pump.py        -- subscribe to the bus, push delta/refinement notifications
  protocol.py        -- request/response/notification dataclasses + (de)serialisation
  handlers.py        -- one function per RPC method (open, sync_buffer, entity_at, …)
  tests/
    test_protocol.py
    test_handlers.py        -- drive handlers against a real TyO3Session in tmp_path
    test_end_to_end.py      -- spawn the daemon, connect a socket client, full flow

editors/tyo3.nvim/            -- the Lua plugin (in-repo so it lands on the branch)
  README.md
  plugin/tyo3.lua             -- commands: :TyO3Inspect :TyO3Note :TyO3Affected :TyO3Diff :TyO3Start/Stop
  lua/tyo3/
    init.lua  config.lua  rpc.lua  daemon.lua  decorate.lua  inspect.lua
    panel.lua  notes.lua  telescope.lua  health.lua
  tests/                      -- plenary/busted headless specs (see §6)
```

Add a console entry point in `pyproject.toml` `[project.scripts]` next to
`tyo3-demo`:
```
tyo3-daemon = "tyo3.daemon.__main__:main"
```

---

## 3. The TyO3 API the daemon wraps — read these first

**Do not transcribe signatures from memory — read the source and the two worked
examples.** They are the canonical, tested API:

- `src/tyo3/demo/tour.py` — every call you need, exercised in sequence:
  `sync_all`, `edit`/`edit_many`, `snapshot(at=)`, `subscribe(Interest.ALL)`,
  `sub.poll(timeout)`, `sub.poll_refinement(timeout)`, `id_for`, `locate`,
  `author`, `authored`, `snap.derived(layer,id)`, `snap.graph()`, `snap.diff(snap0)`.
- `src/tyo3/tests/test_final_acceptance.py` — the same surface with assertions on
  every field (the contract).
- `src/tyo3/session/` (facade `session.py`, `read_ops.py`, `views.py`) — exact
  signatures + the `Snapshot` read surface.
- `src/tyo3/bus/` (`subscription.py`, `delta.py`, `interest.py`) — `Delta` fields
  (`revision`, `affected`, `changed`, `rescan`), the refinement shape
  (`revision`, `narrowed`, `added`), and `Interest.ALL` / `Interest.files_of`.
- `src/tyo3/models/delta.py` — `CommitDelta` fields (`revision`, `created_ids`,
  `changed_ids`, `deleted_ids`, `moved`, `affected_ids`, `touched_files`, …).
- `src/tyo3/precision/refiner.py` — **the model for the bus pump**: a background
  worker already subscribes to the bus and reacts. Mirror its threading shape.

**Graph node fields** (from `snap.graph()._graph[idx]`): `durable_id`, `name`,
`qualified_name`, `kind`, `file`, `range` (start/end line/col), `content_hash`,
`content_hashes`. Use `node.range` + `node.file` to map entities → buffer ranges
for `decorate`.

---

## 4. Daemon spec (`tyo3d`)

- **Transport.** Unix-domain socket, path derived from project root (e.g.
  `$XDG_RUNTIME_DIR/tyo3/<hash(root)>.sock`). **JSON-RPC 2.0**, newline-delimited
  (one JSON object per line). Requests get responses (matched by `id`); the bus
  pump emits **notifications** (no `id`).
- **Concurrency (important).** Own the `TyO3Session` on **one** thread/loop (an
  "actor"): all session mutations (`edit`, `author`, `sync_all`) are serialized
  through it, so writes never race. Reads can take a `snapshot()` and run off the
  pinned snapshot. The **bus pump** is a separate worker doing
  `sub.poll(timeout)` / `sub.poll_refinement(timeout)` in a loop and writing
  notifications to connected clients — the subscription queue is already
  thread-safe (see `refiner.py`). Validate against `test_concurrency.py` /
  `test_mvcc_concurrency.py` behaviour; **do not** call mutating methods from two
  threads.
- **Methods** (params → result) — implement all from `OVERVIEW.md` §5:
  `open`, `sync_buffer`, `entity_at`, `decorate`, `author`, `locate`, `diff`,
  `derived`, `reindex`, `gc`, `check`. `decorate {path}` returns
  `[{range, durable_id, note?, summary?}]` by walking the head graph for nodes
  whose `file == path` and joining authored("intent") + derived summaries.
- **Notifications:** `delta {revision, changed_ids, affected_ids, moved,
  touched_files}` and `refinement {revision, narrowed}` (only when the project
  config sets `precision = "method"`).
- **`sync_buffer {path, text}`** is the editor write path: overlay the buffer text
  (`session.edit(path, text)`) and return the `CommitDelta`. The bus also fires —
  that's expected; the pump forwards it.
- **Lifecycle.** `python -m tyo3.daemon --root <dir> [--socket <path>]`. Clean
  socket on exit; idempotent `open`; exit when the last client disconnects *only*
  if `--autostop` (otherwise stay resident). Log to stderr (overseer shows it).

---

## 5. Lua plugin spec (`tyo3.nvim`)

- **rpc.lua** — JSON-RPC client over a `vim.uv` pipe: connect, send request +
  await response (by id), dispatch incoming notifications to registered handlers.
- **daemon.lua** — locate/spawn `tyo3-daemon` for the buffer's project root (find
  root by walking up for `pyproject.toml`/`.tyo3`/`.git`); reuse one daemon per
  root. Optionally hand supervision to overseer (see below).
- **autocmds (init.lua):** `BufEnter`/`BufReadPost` → `open` + initial `decorate`;
  `TextChanged`/`TextChangedI` → **debounced (250–400ms)** `sync_buffer`;
  `BufWritePost` → final `sync_buffer`. Route by project root.
- **decorate.lua** — one extmark namespace; render authored notes + derived
  summaries as virtual text/lines on each entity. **Re-anchor from the daemon on
  every commit** (call `decorate {path}`); never trust drifted extmark positions
  across a structural edit.
- **inspect.lua** — `:TyO3Inspect` → `entity_at` at cursor → floating window with
  DurableId, qualified name, kind, authored records, derived artifact, last
  affected revision.
- **panel.lua** — a scratch side buffer subscribed to `delta` notifications:
  "rev N · changed {…} · affects {…}". Ambient, not a popup. `nvim-notify` toast
  as a lighter alternative.
- **notes.lua** — `:TyO3Note {text}` → `author {layer="intent", id=<entity at
  cursor>, value={note=text}}`; refresh decorations.
- **telescope.lua** — pickers: `entities` (all), `affected` (since last edit),
  `authored` (notes); select → `locate {id}` → jump.
- **health.lua** — `:checkhealth tyo3` (daemon reachable, socket, version).
- **config:** `require("tyo3").setup({ auto_start=true, debounce_ms=300,
  precision="method", virtual_text=true, panel="auto" })`.

### overseer.nvim (optional, supporting)
Per `OVERVIEW.md` §4: use overseer **only** to supervise the `tyo3-daemon`
process (a template/strategy with its log in a panel) and for `reindex`/`gc`/
`check` task templates. **Do not** route the live notes/affected surface through
overseer. Make it an optional integration (`require("tyo3").setup({ overseer =
true })`), not a hard dependency.

---

## 6. Testing & acceptance

- **Daemon (must be automated — this is the verifiable core):**
  - `test_handlers.py` — call each handler against a real `TyO3Session` in
    `tmp_path` (reuse the synthetic project shape from `tour.py`); assert
    `entity_at`, `decorate`, `sync_buffer` deltas, `author`→`authored`, `diff`.
  - `test_end_to_end.py` — spawn the daemon, connect a raw socket client, run the
    full flow (open → sync_buffer → receive `delta` notification → entity_at →
    author → decorate), assert the notification arrives and is id-level.
  - Run via `devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov`. Green.
  - ruff clean on `src/tyo3/daemon/`.
- **Lua (best-effort but real):** headless specs with plenary/busted that spawn
  the daemon and exercise `rpc.lua` against it (`nvim --headless -c "PlenaryBustedDirectory editors/tyo3.nvim/tests"`).
  At minimum: rpc round-trip, decorate placement, note authoring. If a full
  headless harness is impractical, ship a **manual verification checklist** in the
  plugin README and a `scripts/smoke.lua` that a human runs.
- **Manual checklist (README):** open a sample project, edit a function → panel
  shows the affected set; `:TyO3Note` → virtual text appears; **move the function
  to another file → the note rides along** (the money shot); `:TyO3Inspect` shows
  the entity; Telescope `affected` jumps correctly.

---

## 7. Definition of done

- `tyo3-daemon` entry point serves the socket; all §4 methods + both
  notifications implemented; daemon pytest green; ruff clean.
- `tyo3.nvim` implements all §5 surfaces; loads cleanly; `:checkhealth tyo3`
  passes against a running daemon.
- The README documents install (lazy.nvim spec), config, commands, and the manual
  checklist; the "move the function, the note follows" flow works.
- A **scripted, recorded demo** is checked in: `editors/tyo3.nvim/demo/tour.tape`
  → `tour.gif` + asciinema `tour.cast` (via `vhs`), regenerable headlessly and
  embedded in the README. This is the canonical proof the plugin works — see
  `DEMO_RECORDING.md` (terminal nvim is the real visual UI; vhs drives scripted
  keystrokes; `Sleep` past every async beat for determinism).
- No core Rust/Python engine changes (daemon-only); `v0.2.0` gate still green.
- Branch `nvim-plugin` pushed; PR opened to `main` describing the plugin, with the
  manual checklist in the body. (Open the PR; do not merge — leave for human review.)
- A short progress note in `.scratch/projects/20-neovim-integration/` (a
  `PROGRESS.md`) recording what shipped, the protocol as built, and any deviations
  from this kickoff.

---

## 8. Sequencing (do them in order; each is committable)

1. Daemon protocol + handlers + `test_handlers.py` (no socket yet) — proves the
   session-wrapping logic in isolation.
2. Socket server + bus pump + `test_end_to_end.py` — proves the wire + push.
3. `tyo3-daemon` entry point + lifecycle + `:checkhealth` support.
4. Lua: rpc + daemon spawn + `:TyO3Inspect` (read-only) — first thing visible in nvim.
5. Lua: buffer sync (debounced) + `delta` panel — the reactive core.
6. Lua: notes (virtual text) + derived summaries + Telescope pickers — the payoff.
6.5. Demo: `demo/setup.sh` + minimal `demo/init.lua` + `demo/tour.tape`; package
   `vhs` in devenv; record `tour.gif` + `tour.cast`; embed in the README
   (see `DEMO_RECORDING.md`).
7. overseer templates + README + PROGRESS + PR.

---

## 9. References (all in-repo)
- `.scratch/projects/20-neovim-integration/OVERVIEW.md` — design rationale,
  protocol table, surface mapping, overseer stance, risks.
- `.scratch/projects/20-neovim-integration/DEMO_RECORDING.md` — the scripted
  vhs/asciinema demo spec, the graphical-vs-terminal nuance, and a sample tape.
- `.scratch/projects/19-post-0.2.0-backlog/OVERVIEW.md` — `gc`/`reindex` task
  semantics (overseer templates).
- `src/tyo3/demo/tour.py`, `src/tyo3/tests/test_final_acceptance.py` — the exact,
  tested API the daemon wraps.
- `src/tyo3/precision/refiner.py` — background bus-consumer threading pattern.
- `CLAUDE.md` + the auto-loaded project memory — devenv conventions, house rules.
- Released base: `v0.2.0` (spine refactor complete, full gate green).
