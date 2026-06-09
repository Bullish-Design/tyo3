# tyo3.nvim

> Durable, identity-anchored code intelligence inside Neovim — notes and
> summaries that **stay glued to a function through a rename, a move, and a
> reformat**, plus a live, id-level affected-set as you type. Powered by the
> [TyO3](../../README.md) incremental engine via a small daemon.

![tyo3.nvim demo](demo/default/tour.gif)

*(asciinema cast: [`demo/default/tour.cast`](demo/default/tour.cast) — regenerate
both with `devenv shell -- demo-record`.)*

---

## The idea

The compelling editor feature is the one no LSP gives you: **knowledge anchored
to a durable identity that survives edits**. TyO3 assigns every entity a
`DurableId` that outlives renames/moves/reformats, tracks a transitive,
id-level *affected closure* per commit, and carries authored notes + derived
artifacts on that anchor. tyo3.nvim surfaces all of it reactively:

- **Author a note on a function, move the function to another file — the note
  rides along.** That single moment is the whole pitch.
- Edit a base method → the **affected-set panel** lights up with the exact
  entities to re-check; with `precision = "method"` an async refinement narrows
  it a beat later.
- A floating **inspector** answers "what is this entity?" — DurableId, kind,
  location, authored records, derived artifacts, last-affected revision.

It is **not** another diagnostics source. It is a reactive, per-entity
intelligence layer.

## Architecture

```
  ┌─────────────┐   buffer text (debounced)     ┌──────────────────────┐
  │  Neovim     │ ───── sync_buffer ──────────▶ │  tyo3d (Python)      │
  │  tyo3.nvim  │                               │   owns TyO3Session   │
  │  (Lua)      │ ◀──── delta / refinement ──── │   + overlay + bus    │
  └─────────────┘       (bus push, async)       └──────────────────────┘
       extmarks · floats · panel · telescope        unix socket: JSON-RPC 2.0
```

- **`tyo3d`** (ships with the `tyo3` Python package as `tyo3-daemon`) owns one
  `TyO3Session` per project root on a single actor thread, serves JSON-RPC 2.0
  over a per-root unix-domain socket, and pumps the session's delta bus to
  clients as notifications.
- **tyo3.nvim** is a thin client: it spawns/locates the daemon for a buffer's
  project, mirrors buffer text into the session overlay (debounced), and renders
  the results as extmarks, floats, a panel, and Telescope pickers.

One daemon + one socket per project root; the plugin routes by the buffer's
root.

## Requirements

- Neovim ≥ 0.10 (`vim.uv`).
- The `tyo3` Python package built and importable (`tyo3-daemon` on `PATH`, or
  `python -m tyo3.daemon` available). In this repo: `devenv shell -- build`.
- A project with a `.tyo3/config.toml` (or `pyproject.toml`/`.git` to mark the
  root). See [`src/tyo3/demo/tour.py`](../../src/tyo3/demo/tour.py) for an
  example config (precision, layers, generators).
- Optional: [telescope.nvim](https://github.com/nvim-telescope/telescope.nvim)
  for the pickers (they degrade to `vim.ui.select` without it);
  [overseer.nvim](https://github.com/stevearc/overseer.nvim) for ops tasks.

## Install

### lazy.nvim

```lua
{
  "your-org/tyo3.nvim",            -- or dir = "/path/to/tyo3/editors/tyo3.nvim"
  ft = "python",
  dependencies = { "nvim-telescope/telescope.nvim" }, -- optional
  opts = {
    auto_start = true,
    debounce_ms = 300,
    virtual_text = true,
    panel = "auto",
  },
}
```

### packer.nvim

```lua
use({ "your-org/tyo3.nvim", config = function() require("tyo3").setup({}) end })
```

### Manual

Put `editors/tyo3.nvim` on your `runtimepath` and call `require("tyo3").setup({})`.

If `tyo3-daemon` is not on your `PATH`, point the plugin at the module form:

```lua
require("tyo3").setup({ daemon_cmd = { "python", "-m", "tyo3.daemon" } })
```

## Configuration

`setup{}` accepts (defaults shown):

| Option | Default | Meaning |
|---|---|---|
| `auto_start` | `true` | Spawn/attach the daemon on `BufEnter` of a project `*.py` buffer. |
| `debounce_ms` | `300` | Pause before a `TextChanged` commit fires. One commit per pause. |
| `precision` | `"method"` | Hint surfaced in `:checkhealth` (the daemon owns the real setting via `.tyo3/config.toml`). |
| `virtual_text` | `true` | Render notes + summaries inline as virtual text. |
| `panel` | `"auto"` | Affected-set panel: `"auto"` (open on first delta), `"always"`, `"off"`. |
| `daemon_cmd` | `nil` | Launch command. `nil` ⇒ auto-detect `tyo3-daemon`, else `python -m tyo3.daemon`. |
| `root_markers` | `{".tyo3","pyproject.toml",".git"}` | Upward search for the project root. |
| `overseer` | `false` | Enable the optional overseer.nvim ops integration. |

## Commands

| Command | Does |
|---|---|
| `:TyO3Inspect` | Floating inspector for the entity under the cursor. |
| `:TyO3Note [text]` | Author an intent note on the entity under the cursor (prompts if no text). |
| `:TyO3Move <name> <dest>` | Atomically move an entity to another file — id + note + hash preserved. |
| `:TyO3Affected` | Picker over the last edit's affected set; jump by identity. |
| `:TyO3Entities` | Picker over all known entities. |
| `:TyO3Authored` | Picker over entities carrying an authored note. |
| `:TyO3Diff <from_rev> [to_rev]` | Entity-level snapshot diff (added/removed/changed/moved). |
| `:TyO3Panel` | Toggle the affected-set panel. |
| `:TyO3Start` / `:TyO3Stop` | Attach the daemon for this buffer / stop all session daemons. |
| `:TyO3Reindex` / `:TyO3Gc` / `:TyO3Check` | One-shot daemon ops (rescan / gc orphans / type-check). |
| `:TyO3DaemonLog` | Show the captured daemon stderr log. |

## Health

```
:checkhealth tyo3
```

Reports daemon-command resolution and, from inside a project buffer, pings the
running daemon for liveness + engine version + the available RPC methods.

## Protocol (as built)

Newline-delimited **JSON-RPC 2.0** over a per-root unix socket
(`$XDG_RUNTIME_DIR/tyo3/<hash>.sock`). Positions on the wire are **1-based**
(TyO3 native convention); the plugin converts from Neovim's 0-based columns.

**Requests** (editor → daemon):

| Method | Params | Result |
|---|---|---|
| `ping` | `{}` | `{ok, engine_version, revision, methods, …}` |
| `open` | `{root}` | `{session_id, revision, files, layers, precision}` |
| `sync_buffer` | `{path, text}` | `CommitDelta` `{revision, changed_ids, affected_ids, moved, touched_files, …}` |
| `sync_buffers` | `{edits:{path:text}}` | `CommitDelta` (one atomic commit — the move path) |
| `entity_at` | `{path, line, col}` | entity card or `null` |
| `decorate` | `{path}` | `[{durable_id, name, qualified_name, kind, range, note?, summary?}]` |
| `author` | `{layer, durable_id, value}` | `{revision}` |
| `authored` | `{layer, durable_id}` | `{value, status, revision}` |
| `locate` | `{durable_id}` | `{location}` (`file::qualified_name`) |
| `diff` | `{from_rev, to_rev?}` | `{added, removed, changed, moved, …}` |
| `derived` | `{layer, durable_id}` | `{status, artifact, revision}` |
| `reindex` / `gc` / `check` | `{}` | op result |

**Notifications** (daemon → editor, from the bus pump):

| Method | Params |
|---|---|
| `delta` | `{revision, changed_ids, affected_ids, moved_ids, touched_files, rescan, …}` |
| `refinement` | `{revision, narrowed, added}` (only with `precision = "method"`) |

## Manual verification checklist

A human pass (the scripted recording above is the canonical proof; this is the
fallback). In a project with a `.tyo3/config.toml` (e.g. a copy of the shop
project — see `demo/setup.sh`):

1. **Ambient.** Open `store.py`; `:checkhealth tyo3` is green; the panel shows
   the session is live. Entities are decorated.
2. **Inspect.** Cursor on `checkout`, `:TyO3Inspect` → float with DurableId,
   kind, location, derived summary.
3. **Note.** `:TyO3Note load-bearing checkout path` → a virtual-text note
   appears above `checkout`.
4. **The money shot.** `:TyO3Move checkout checkout.py`, then `:e checkout.py` —
   the note **rides along** to the new file, still on `checkout`.
5. **Affected set.** `:e catalog.py`, change `Item.price`'s `return 100` to
   `250`, `:w` — the panel lights up with `{Item, Book, checkout, …}`; a beat
   later the refinement narrows it (`show_label` drops out).
6. **Navigate.** `:TyO3Affected` → pick an affected entity → it jumps there.

## Regenerating the demo

```
devenv shell -- demo-record           # vhs editors/tyo3.nvim/demo/default/tour.tape
devenv shell -- demo-record-context   # the cursor-CONTEXT demo (demo/context/)
```

Renders `demo/default/tour.gif` + `demo/default/tour.cast` headlessly (no display
required). `demo/setup.sh` (shared) builds the synthetic project fresh and
resolves a pristine nvim; each demo's `init.lua` is a minimal, plugin-only nvim
config so the recording is identical on any machine.

## Development & tests

- The verifiable core is the **daemon pytest** (handlers + a full socket
  end-to-end): `devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov`.
- A headless Lua smoke spec spawns a real daemon and drives `rpc.lua` /
  `daemon.lua`:

  ```
  devenv shell -- nvim --headless --clean \
    -u editors/tyo3.nvim/tests/minimal_init.lua \
    -c "luafile editors/tyo3.nvim/tests/smoke.lua"
  ```

  It exits non-zero on any failed check.

## License

Same as the parent TyO3 project.
