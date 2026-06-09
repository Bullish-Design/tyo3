# Implementation brief — cursor-driven CONTEXT panel (`context = "cursor"`)

> Hand this file to a fresh session as its sole brief. It is self-contained:
> read the files it points at, then implement. **This increment is plugin-only
> (Lua under `editors/tyo3.nvim/`). Do not touch the Rust/Python engine; you do
> not need to touch the daemon either** — the data you need already exists on the
> wire.

## Mission

Add an opt-in mode where, as the cursor moves through a Python buffer in a TyO3
project, a **CONTEXT** section in the side panel auto-updates to show the durable
entity under the cursor and its linked records — **notes** (authored `intent`
layer) and **docs/summary** (derived `summary` layer) — plus its kind, location,
and the last revision that affected it.

The update must fire **only when the enclosing code node changes** (not on every
cursor wiggle), gated by Treesitter, debounced, and resilient to stale
responses. It must be **default-off** so the existing demo and behaviour are
untouched.

This is "increment 1" of a larger design discussed with the maintainer. Increments
2–3 (active-node buffer highlight; a `references`/`neighbors` daemon RPC to surface
linked *tests/callers*) are **out of scope here** — see the last section.

## Ground rules (non-negotiable)

- **Run everything through devenv**: `devenv shell -- <cmd>`. Never call `pytest`,
  `nvim`, etc. raw. Example: `devenv shell -- bash -c '<nvim headless invocation>'`.
- **No AI attribution** anywhere — commits, PR text, code comments, docs. Omit all
  `Co-Authored-By` / "Generated with" trailers.
- Branch is **`nvim-plugin`** (PR #2 to `main`, stays open). Commit/push only when
  the work is green and the maintainer expects it; ask if unsure.
- Match the surrounding Lua style (2-space indent, `local M = {}` modules,
  `require` at top or lazily inside functions as the existing files do). There is
  **no stylua/luacheck** configured — consistency is by eye.
- Repo root: `/home/andrew/Documents/Projects/tyo3`. Plugin root:
  `editors/tyo3.nvim/`.

## The one fact that makes this small

`entity_at(path, line, col)` (1-based line/col) already returns the **entire
cross-layer card** for whatever is under the cursor. From
`src/tyo3/daemon/handlers.py` (`entity_at` at ~L129, assembled by `_entity_dict`
at ~L304), the JSON shape is:

```
{
  "durable_id": "...",
  "location":   "rel/path.py::qualified::name",
  "name":            "checkout",
  "qualified_name":  "checkout",
  "kind":            "function",          -- node.kind.value
  "file":            "store.py",
  "range":  { "start": {"line":N,"column":M}, "end": {...} },   -- 1-based
  "content_hash":    "....",
  "authored": { "intent": {"value": {"note":"..."}, "status":"present", "revision":R}, ... },
  "derived":  { "summary": {"artifact":"summary<...>", "status":"present"}, ... },
  "last_affected_revision": R    -- int, or null (arrives as vim.NIL in Lua)
}
```

- Returns **`null`** (→ `vim.NIL` in Lua) when there is no entity under the cursor.
- The note layer is conventionally **`intent`**; a note value is `{note = "text"}`.
  The summary layer is conventionally **`summary`** (artifact is a string).
- `editors/tyo3.nvim/lua/tyo3/inspect.lua` (`build_lines`, L17) already renders
  this card for the `:TyO3Inspect` float. **Reuse that rendering** rather than
  re-deriving it (see step 2).

Because the card is already on the wire, **no daemon/protocol change is needed.**

## Current architecture you build on (read these)

- `lua/tyo3/config.lua` — defaults table + `setup`/`get`. Add the new flag here.
- `lua/tyo3/init.lua` — orchestration:
  - `M.rpc(bufnr, method, params, cb)` → `cb(err, result)`. Use this to call
    `entity_at`.
  - `M.root_for_buf(bufnr)` → project root or nil (cached).
  - `M.on_text_changed` (L96) — **the debounce pattern to copy**: a per-buffer
    `vim.uv` timer, stop/close the existing one, `start(ms, 0, schedule_wrap(...))`.
  - Filetype guard used throughout: `vim.bo[bufnr].filetype ~= "python"`.
  - `M.handle_notification` (L140) routes daemon `delta`/`refinement` to the panel.
- `lua/tyo3/panel.lua` — the side panel. One scratch buffer `TyO3://affected`,
  a `botright vsplit` at width 42 (`open`, L51), an `M._lines` history rendered by
  `render()` (L83), appended by `append()` (L96), driven by `on_delta` (L106) and
  `on_refinement` (L134). `M._rev_index` maps revision → line index for refinement
  annotation. **You will refactor this to host two sections** (see step 3).
- `lua/tyo3/decorate.lua` — extmark decorations + `M.name_cache` + `M.name_for`.
  Not central here, but note it already refreshes on every commit via
  `handle_notification`; don't duplicate or fight that.
- `plugin/tyo3.lua` — user commands + autocmds in augroup `tyo3`. Existing autocmds
  key on `pattern = "*.py"`. **Add the CursorMoved autocmd and a `:TyO3Context`
  toggle command here.**
- `tests/minimal_init.lua` + `tests/smoke.lua` — the headless test harness (see
  Verification). `minimal_init.lua` does `filetype plugin indent off` before adding
  the plugin to rtp — that is what keeps the test nvim clean.

## Design

### Config (`config.lua`)
Add to `M.defaults`:
```lua
-- Cursor-context section in the panel: "cursor" (auto-update from the entity
-- under the cursor) or "off". Default off so existing behaviour is unchanged.
context = "off",
-- Debounce (ms) before the cursor-context lookup fires.
context_debounce_ms = 150,
```

### Trigger (`plugin/tyo3.lua` + a new `lua/tyo3/context.lua`)
- New autocmd `{ "CursorMoved", "CursorMovedI" }`, `pattern = "*.py"`, augroup
  `tyo3`, calling `require("tyo3.context").on_cursor(ev.buf)`.
- **Do NOT use `CursorHold`** — it is gated by `updatetime` (default 4000ms) and
  feels exactly like the "lags then lingers" problem we are fixing. Use
  `CursorMoved` + a manual `vim.uv` debounce (copy `init.lua:on_text_changed`).
- A `:TyO3Context` command that toggles `context` between `"cursor"`/`"off"` at
  runtime (flip `require("tyo3.config").get().context` and open/clear the section).

### `lua/tyo3/context.lua` (new module)
Responsibilities:
1. **Gate cheaply with Treesitter.** On `on_cursor(bufnr)`:
   - bail unless `config.get().context == "cursor"`, buffer is `python`, and in a
     TyO3 project (`require("tyo3").root_for_buf(bufnr)`).
   - Compute the **enclosing-node key**:
     ```lua
     local ok, node = pcall(vim.treesitter.get_node, { bufnr = bufnr })
     -- climb to nearest definition node
     while node and not node:type():match("function_definition")
                and not node:type():match("class_definition") do
       node = node:parent()
     end
     local key = node and table.concat({ node:range() }, ":") or ("line:" .. cursorline)
     ```
   - **Fallback** when no parser/parse fails (`ok == false` or `node == nil`):
     key by cursor **line** so the feature still works (just coarser). Both the
     wrapped and the pristine `--clean` nvim here *do* ship the Python parser
     (verified), but degrade gracefully anyway.
   - If `key == M._last_key[bufnr]`, **return early** (dedupe — this is the
     "only when the highlighted node changes" behaviour).
2. **Debounce** the actual RPC (per-buffer `vim.uv` timer, `context_debounce_ms`).
3. On fire, call `entity_at` with the **node start** (or cursor if fallback),
   capturing `key` at request time. In the callback, **drop the response if the
   current `M._last_key[bufnr]` no longer equals the captured `key`** (cursor moved
   on — stale). Otherwise hand the card to the panel's context renderer.
4. Empty/`vim.NIL` card → render an empty/"no entity" context section.

The `SessionActor` is **single-threaded** (`handlers.py` runs every session call
through one owner thread); the dedupe+debounce is what keeps you from flooding it.
Reads run over a **frozen committed snapshot**, so the context reflects the last
commit (saved/synced state), which is the desired semantics.

### Panel sections (`panel.lua` refactor)
Split the single `M._lines` into two independently-rendered parts, composed at
render time:
- `M._context_lines` — the CONTEXT section (replaced wholesale on each update).
- `M._affected_lines` — the existing affected-set log (was `M._lines`).
- `render()` composes: `CONTEXT header + M._context_lines + blank + AFFECTED header
  + M._affected_lines`, then sets the buffer.
- **`M._rev_index` must index into `M._affected_lines`** (relative), and
  `on_refinement` edits `M._affected_lines[idx]`. Keep refinement working — there is
  an existing bug where the appended `→ narrowed {…}` is invisible because the
  panel is 42-col `wrap=false`; this refactor is a fine time to either widen the
  panel or set `wrap = true` so both `affects {…}` and the narrowing show. Confirm
  the affected-log behaviour is unchanged when `context = "off"`.
- New API on the panel, e.g. `M.set_context(card)` and `M.clear_context()`, called
  by `context.lua`. When `context == "cursor"` and the panel is closed, open it
  (mirror the `panel == "auto"` open-on-first-delta logic at L127).

Suggested CONTEXT rendering (compact, reuse `inspect.build_lines` logic — consider
extracting the card→lines formatter into a small shared function so the float and
the panel stay consistent):
```
▌ CONTEXT ──────────────
  checkout · function
  🏷 load-bearing checkout path
  ⟢ summary<def checkout() -> str:>
  affected@ rev 14
```
(Notes: iterate every authored layer with a present record, not just `intent`;
every derived layer with a present artifact, not just `summary` — `_entity_dict`
already returns all of them. Truncate long lines to the panel width.)

### Keep the float
Leave `:TyO3Inspect` as-is (on-demand focused view). The maintainer's "popup stays
open too long" concern is addressed by the *passive* path moving to the panel; the
float is event-dismissed already (`inspect.lua:101`, closes on CursorMoved). No
timer needed.

## Implementation steps (suggested order)

1. `config.lua`: add `context` + `context_debounce_ms` defaults.
2. Extract the card→lines formatter: pull the body of `inspect.lua:build_lines`
   into a reusable function (either keep it in `inspect.lua` and `require` it from
   the panel, or a tiny `lua/tyo3/card.lua`). Have the float and the panel CONTEXT
   section both render from it (panel may render a compact subset).
3. `panel.lua`: refactor to `_context_lines` + `_affected_lines`; add
   `set_context`/`clear_context`; fix `_rev_index` to be affected-relative; make
   refinement visible (widen or wrap).
4. `context.lua` (new): the TS-gated, debounced, stale-dropping cursor handler with
   line-based fallback.
5. `plugin/tyo3.lua`: `CursorMoved`/`CursorMovedI` autocmd + `:TyO3Context` command.
6. Tests (next section).

## Verification loop (the maintainer explicitly asked for this)

Iterate: **edit → run the headless fixture(s) → read PASS/FAIL → fix → repeat**,
all under `devenv shell --`. Treat the tests as a tight inner loop.

### Gate A — existing smoke must stay green (regression guard)
```
devenv shell -- bash -c 'nvim --headless --clean -u editors/tyo3.nvim/tests/minimal_init.lua -c "luafile editors/tyo3.nvim/tests/smoke.lua"'
```
Expect `tyo3.nvim smoke: 12 checks, 0 failed`. This uses the PATH (wrapped) nvim,
but `--clean` + `minimal_init.lua`'s `filetype plugin indent off` keep it clean.

### Gate B — new headless test for the context feature
Add `tests/context.lua`, modelled exactly on `smoke.lua` (build shop project via
`tyo3.demo.tour._build_project`, `setup{ daemon_cmd = {"python","-m","tyo3.daemon"},
context = "cursor" }`, open `store.py`, drive with `with_client`, and a
`vim.wait(30000, ...)` loop; print `[PASS]/[FAIL]`; `cquit 1` on failure). Assert:
1. **Treesitter gate works**: with the cursor inside `checkout`, the computed
   enclosing-node key resolves (Python parser present) and equals the
   `function_definition` range; moving within the same function does *not* change
   the key; moving to another def *does*.
2. **Entity resolves + renders**: after triggering `context.on_cursor` (or moving
   the cursor and waiting past `context_debounce_ms`), the panel buffer
   (`require("tyo3.panel")` buffer lines) contains `checkout` and `function`.
3. **Notes/docs surface**: author an `intent` note on `checkout` (reuse the
   `author` RPC as smoke.lua does), re-trigger, assert the panel CONTEXT lines
   contain the note text and (if present) the summary.
4. **Stale-drop**: simulate two rapid `on_cursor` calls for different keys; assert
   only the latest key's card is rendered (e.g. by checking the rendered entity
   matches the final cursor position).
5. **Fallback path**: force the no-parser branch (e.g. call the internal key
   function on a buffer whose filetype TS can't parse, or stub) and assert it keys
   by line and does not error.
6. **Default-off**: with `context = "off"`, `on_cursor` is a no-op and the panel
   shows no CONTEXT section.

Run it the same way:
```
devenv shell -- bash -c 'nvim --headless --clean -u editors/tyo3.nvim/tests/minimal_init.lua -c "luafile editors/tyo3.nvim/tests/context.lua"'
```
You may need a `vim.treesitter.start(bufnr,"python")` / `get_parser():parse()` to
ensure the tree is built in headless mode before reading nodes.

### Gate C — daemon tests + ruff unaffected (sanity; this change is Lua-only)
```
devenv shell -- pytest src/tyo3/daemon/tests --no-cov -q
devenv shell -- ruff check src
```
Both should remain green/clean (you changed no Python).

### Optional manual visual check
For a real-terminal feel, you can drive the pristine nvim (see
`editors/tyo3.nvim/demo/setup.sh` `_resolve_nvim` → `$TYO3_NVIM`, and the memory
note **nvim-demo-pristine-binary** — the PATH `nvim` is a home-manager wrapper that
injects user config even under `--clean`; resolve `neovim-unwrapped`). Open a shop
buffer with `context = "cursor"` and move between `Item.price`, `checkout`,
`show_label` to watch the CONTEXT section track. **Do not regress or re-record the
committed demo** as part of this increment unless asked — keep `context` default-off
so `demo/init.lua` is unaffected.

## Acceptance criteria

- [ ] `context` defaults to `"off"`; with it off, behaviour and the smoke result
      (12/12) are byte-for-byte unchanged.
- [ ] With `context = "cursor"`, moving the cursor between entities updates a
      CONTEXT section showing `name · kind`, authored notes, derived summary, and
      `affected@ rev N` when present.
- [ ] `entity_at` fires **only on enclosing-node change** (TS-gated), debounced by
      `context_debounce_ms`; rapid motion within one function triggers at most one
      lookup.
- [ ] Stale responses are dropped; no flicker to a previous entity.
- [ ] Graceful when no TS parser (line-based fallback), no errors.
- [ ] The affected-set log still works (and refinement narrowing is now visible).
- [ ] New `tests/context.lua` green; Gates A & C green.
- [ ] No engine/daemon/protocol changes; no AI attribution; Lua matches house style.

## Out of scope (future increments — do not build now)

- **Active-node highlight** in the buffer (extmark over the enclosing node range)
  to tie buffer ↔ panel.
- **Linked tests/callers**: there is no graph-neighbour query on the wire today
  (reverse-deps were deferred per project memory; `affected_ids` is computed at edit
  time, not as a standing lookup). Surfacing "which tests/callers reference this
  entity" needs a new `references(durable_id)` / `neighbors(durable_id)` daemon RPC
  over reverse-deps — that crosses into engine surface and needs maintainer sign-off.
  "Tests" could alternatively be an explicit authored `tests` layer in the meantime
  (works today via the `author` RPC).
