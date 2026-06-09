# Neovim integration — progress

> What shipped on branch `nvim-plugin` (off `main` @ `v0.2.0`), the protocol as
> built, and deviations from `KICKOFF.md`. Daemon-only — no engine changes.

## Status: complete

Both deliverables are built, tested, and documented:

1. **`tyo3d`** — a Python daemon (`src/tyo3/daemon/`, `tyo3-daemon` entry point)
   wrapping one `TyO3Session`, serving newline-delimited JSON-RPC 2.0 over a
   per-root unix socket, pumping the delta bus to clients as notifications.
2. **`tyo3.nvim`** — the Lua plugin (`editors/tyo3.nvim/`): buffer↔overlay sync,
   durable-id-anchored virtual-text notes/summaries, a floating inspector, an
   affected-set panel, Telescope pickers, an atomic-move verb, commands, and
   `:checkhealth`.

A scripted recorded demo (`editors/tyo3.nvim/demo/`) is checked in and embedded
in the README.

## How it's built (the sequence, KICKOFF §8)

1. **Protocol + actor + handlers** (`protocol.py`, `session_actor.py`,
   `handlers.py`, `tracking.py`) + `test_protocol.py` / `test_handlers.py`.
2. **Socket server + bus pump** (`server.py`, `bus_pump.py`) + `test_end_to_end.py`.
3. **Entry point + lifecycle** (`__main__.py`, `tyo3-daemon` script, `ping` for
   health, `--autostop`).
4–6. **Lua**: `rpc` → `daemon` → `init` (autocmds) → `decorate` / `inspect` /
   `panel` / `notes` / `move` / `telescope` / `diff` / `health` / `overseer`,
   plus `plugin/tyo3.lua` and a headless `tests/smoke.lua`.
6.5. **Demo**: `setup.sh` + `init.lua` + `tour.tape` (+ `record_cast.py`), `vhs`
   in devenv, `demo-record` script, README embed.
7. overseer ops integration, README, this PROGRESS, PR.

## The protocol as built

Newline-delimited **JSON-RPC 2.0**, one object per line, over
`$XDG_RUNTIME_DIR/tyo3/<sha>.sock`. Positions are **1-based** (TyO3 native
convention); the Lua client converts from Neovim's 0-based columns. The session
is owned by a single **actor thread**; every read/write is funnelled through it.
The **bus pump** subscribes `Interest.ALL` and forwards each `Delta` /
`AffectedRefinement` as a notification.

Requests: `ping`, `open`, `sync_buffer`, `sync_buffers`, `entity_at`, `decorate`,
`author`, `authored`, `locate`, `diff`, `derived`, `reindex`, `gc`, `check`.
Notifications: `delta`, `refinement`.

`decorate {path}` walks the head graph for entity nodes whose `file == path` and
joins the authored note layer (`intent`) + the derived summary layer (`summary`).
`sync_buffers {edits}` (→ `session.edit_many`) is the **atomic** path that makes
the move bind as a `Moved` so an authored note rides along.

## Tests / gates

- **Daemon pytest (the verifiable core):** `pytest src/tyo3/daemon/tests`
  — `test_protocol.py`, `test_handlers.py` (every handler against a real session
  built from the tour's shop project, incl. the id-preserving atomic move),
  `test_end_to_end.py` (spawns the daemon, drives the full reactive flow over a
  real socket, asserts the id-level `delta` + the `refinement` push, and
  `--autostop`). All green; `ruff check` / `ruff format --check` clean on
  `src/tyo3/daemon/`.
- **Lua headless smoke:** `tests/smoke.lua` spawns a real daemon and drives
  `rpc.lua` / `daemon.lua` (12 checks: connect, ping, decorate placement,
  entity_at, note authoring, sync_buffer delta) — all green via
  `nvim --headless --clean -u tests/minimal_init.lua -c "luafile tests/smoke.lua"`.
- **v0.2.0 engine gate:** unchanged (no engine edits); `test-final` still green.

## Deviations from KICKOFF

1. **asciinema `.cast` not emitted by vhs.** The `vhs` packaged in nixpkgs here
   (0.11.0) does not support an asciinema `.cast` output (its parser rejects the
   extension / routes it to ffmpeg). The **GIF is the canonical recorded demo**
   (DEMO_RECORDING §7 designates the GIF as the canonical artifact), embedded in
   the README and regenerable via `devenv shell -- demo-record`. The `.cast` is
   produced by a small **dependency-free PTY recorder** (`demo/record_cast.py`)
   driving the *same* scripted scenes — a genuine, regenerable asciinema v2 cast
   without needing the asciinema binary. `demo-record` runs both.
2. **`tyo3-daemon` console script vs `python -m`.** The entry point is registered
   in `pyproject.toml`; the plugin auto-detects it and falls back to
   `python -m tyo3.daemon`. The demo + smoke pin the module form so no rebuild is
   needed to install the console script.
3. **`:TyO3Move` added.** KICKOFF/DEMO mention "a `:TyO3Move` or scripted
   yank/paste" for the move. We implemented `:TyO3Move <name> <dest>` as a real
   verb (extract entity lines → atomic `sync_buffers`), so the money shot is one
   command and deterministic in the recording. The destination must be an
   existing project file (an empty stub is fine — `setup.sh` pre-creates
   `checkout.py` with the needed imports).
4. **overseer integration is light.** Per KICKOFF/OVERVIEW §4, overseer is a
   supporting actor, not the live surface. We expose ops (`reindex`/`gc`/`check`)
   and a daemon-log view that work with or without overseer, rather than forcing
   the reactive surface through task windows. The live notes/affected/inspector
   surfaces are extmarks + floats + the bus, as specified.

## No engine changes

The Rust/Python engine is untouched. `git diff main -- src/tyo3` outside
`src/tyo3/daemon/` and `pyproject.toml`'s one added entry point is empty.
