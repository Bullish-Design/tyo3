# Phase E implementation guide — edgy accordion sidebar (proj 28)

Companion to [`CONCEPT.md`](CONCEPT.md) §2 (observe) / §5.1 (the accordion spike)
and [`PLAN.md`](PLAN.md) §"Phase E". File-level build spec, written against the
**actual post-Phase-D code** (branch `proj28-phase-d-astnav`) and the **actual
installed edgy** (`edgy.nvim` v1.10.2, tag `stable`). Where this guide and PLAN
differ, this guide wins.

> **One-line goal:** replace `panel.lua`'s hand-rolled split + monolithic render
> with **edgy-managed, vertically stacked, one-category-per-view section windows**
> (IDENTITY / NOTES / DOCS / SUMMARY / AFFECTED), fed by the *same* events the panel
> consumed, with a focus-driven accordion (expand the entered section, collapse the
> rest to title height). Delete `panel.lua` + `inspect.lua`. This is the
> highest-risk phase — **most of the work is deletion + re-pointing, gated behind a
> spike that is already resolved below (§3.1).**

---

## 0. Where Phase D left things (read this first)

The dock today is `lua/tyo3/panel.lua`: a single `botright vsplit` window with a
nofile buffer, rendering all sections as collapsible *text* (toggled with `<Tab>`),
driven by the cursor-context + bus events. It is **observe-only** since Phase C (no
ACTIONS pane). Phase E swaps its *hosting* (one hand-rolled window → N edgy windows)
without changing where the data comes from.

**Every consumer of the dock (the re-point checklist — §3.4):**

| Caller | Today | Phase E |
|---|---|---|
| `context.lua:67` | `panel.set_context(card, bufnr)` | `sidebar.set_context(card, bufnr)` |
| `init.lua:178/196/204` | `panel.on_delta/on_derived/on_refinement` | `sidebar.on_*` |
| `entitydoc.lua:69` | `panel.reload()` (after doc save) | `sidebar.reload()` |
| `picker.lua:125` | reads `panel.last_affected_ids` | `sidebar.last_affected_ids` |
| `plugin/tyo3.lua:52` | `:TyO3Panel` → `panel.toggle()` | `:TyO3Sidebar` → `sidebar.toggle()` |
| `plugin/tyo3.lua:15` | `:TyO3Inspect` → `inspect.inspect()` | focus the IDENTITY view (§3.5) |

**Panel's public surface to reproduce on `sidebar`:** `set_context(card, src_buf)`,
`clear_context()`, `reload()`, `on_delta(root,p)`, `on_derived(root,p)`,
`on_refinement(root,p)`, `toggle()`/`open()`/`close()`, and the field
`last_affected_ids`. The AFFECTED-log state (`_affected_lines`, `_rev_index`,
`_rev_narrowed`) moves with `on_delta`/`on_refinement`.

**Reusables that stay:** `lua/tyo3/card.lua` (`build_lines(card)` — the IDENTITY
renderer reuses it), `lua/tyo3/decorate.lua` (`name_for(id)` for AFFECTED names),
`lua/tyo3/docs.lua` (the DOCS reference links + `section_doc`).

**`lua/tyo3/deps.lua`** has the `setup_edgy(opts)` stub (`pcall(require,"edgy")` →
`record_missing` + a `TODO(Phase E)`) called from `M.setup` under `manage ~= false`.
**`bind_ast_keymaps`** (Phase D) is the per-buffer keymap model; the sidebar is set
up *once* (not per-buffer), so it hangs off `deps.setup_edgy`, not `on_buf_enter`.

**Branch:** `git checkout proj28-phase-d-astnav && git checkout -b
proj28-phase-e-sidebar` (if D has since merged to `main`, branch off `main`).

---

## 1. The target

```
lua/tyo3/sidebar/
  init.lua        -- lifecycle, the panel-compatible public API, accordion controller
  render.lua      -- table-driven section renderers (card → lines), moved from panel.lua
plugin/tyo3.lua   -- :TyO3Panel → :TyO3Sidebar; :TyO3Inspect → focus IDENTITY
deps.lua          -- setup_edgy: laststatus/splitkeep + register the 5 views
DELETE: panel.lua, inspect.lua
```

**The five views** (one edgy right-edge view each; `ft` is the join key edgy uses
to claim our scratch buffer):

| View | ft | Source (from the cached card / bus) | Renderer (from panel.lua) |
|---|---|---|---|
| IDENTITY | `tyo3_identity` | name·kind, id, location | `card.build_lines` / `identity_rows` |
| NOTES | `tyo3_notes` | authored layers + needs_review ⚠ | `note_rows` |
| DOCS | `tyo3_docs` | the `docs` markdown + reference links | `doc_rows` |
| SUMMARY | `tyo3_summary` | derived artifacts | `summary_rows` |
| AFFECTED | `tyo3_affected` | the blast-radius log | `M._affected_lines` |

(REVIEW from PLAN's table is folded into NOTES' ⚠ + the existing `tyo3-layer`
`vim.diagnostic` namespace — don't add a sixth view.)

---

## 2. Build order

### E.0 — Spike (already resolved; verify in 10 min, then build)

The accordion question (CONCEPT §5.1) is **answered in §3.1** by reading edgy
v1.10.2's source. Before building, sanity-check it live: `require("edgy").setup{
right = { {ft="tyo3_a",title="A",pinned=true,open=fn}, {ft="tyo3_b",…} } }` with
`laststatus=3`, enter view A → it expands, B stays as-is (edgy does NOT collapse B).
That confirms you need the §3.1 controller. If anything diverges from §3.1, **pause
and flag it** (per the kickoff) — otherwise execute.

### E.1 — `deps.setup_edgy`: globals + register the views

```lua
function M.setup_edgy(_opts)
  local ok, edgy = pcall(require, "edgy")
  if not ok then
    record_missing("edgy.nvim", "accordion sidebar (observe surface)")
    return
  end
  -- edgy can only fully collapse views to title height with the GLOBAL
  -- statusline; splitkeep avoids scroll jumps as views resize (README §setup).
  vim.opt.laststatus = 3
  vim.opt.splitkeep = "screen"
  require("tyo3.sidebar").setup(edgy) -- builds the buffers + the edgy view specs
end
```

### E.2 — `lua/tyo3/sidebar/` (the package)

**`sidebar/render.lua`** — lift `identity_rows`/`note_rows`/`doc_rows`/`summary_rows`
out of `panel.lua` verbatim (they already take a `card` and return `{ text, meta? }`
rows; drop the `meta`/interaction bits — the sidebar is read-only). Add an
`affected_lines` passthrough. Each returns `string[]` for its buffer.

**`sidebar/init.lua`:**
- `M.setup(edgy)` — create five persistent scratch buffers (`nvim_create_buf(false,
  true)`, `buftype=nofile`, `bufhidden=hide`, `swapfile=false`, and `filetype` =
  the view's `ft`). Stash them in `M.bufs = { identity=…, notes=…, … }`. Then
  register the edgy views (see §3.2 for the `open`/`pinned` pattern), and install
  the accordion controller (§3.1). Idempotent (guard with `M._did_setup`).
- `M.set_context(card, src_buf)` — cache `M._entity`/`M._source_buf`; for each
  section, render rows into its buffer (`nvim_buf_set_lines`, toggling
  `modifiable`); then **drive the content-accordion** (§3.1): expand sections with
  content (IDENTITY always; NOTES/DOCS/SUMMARY when non-empty), collapse the empty
  ones. Open the sidebar on the first entity if closed (mirror panel's auto-open).
- `M.clear_context()` / `M.reload()` — as panel's (reload re-`entity_at`s the
  cached position and re-`set_context`s).
- `M.on_delta/on_derived/on_refinement` — move panel's bodies in; they append to
  `M._affected_lines` (+ `_rev_index`/`_rev_narrowed`), render the AFFECTED buffer,
  set `M.last_affected_ids`, and auto-open. `on_derived` re-pulls the card via
  `reload`.
- `M.toggle()/open()/close()` — delegate to `require("edgy").toggle/open/close("right")`.
- `M.last_affected_ids = {}` — the field `picker.affected` reads.

**Re-point the callers** (§3.4): `context.lua`, `init.lua` (×3), `entitydoc.lua`,
`picker.lua`.

### E.3 — Delete + commands

- `git rm lua/tyo3/panel.lua lua/tyo3/inspect.lua`.
- `plugin/tyo3.lua`: rename `:TyO3Panel` → `:TyO3Sidebar` (`sidebar.toggle()`);
  re-point `:TyO3Inspect` to **focus the IDENTITY view** (§3.5) — or retire it.
  Keep `card.lua` (IDENTITY uses it).
- Sweep: `rg 'require\("tyo3\.panel"\)|require\("tyo3\.inspect"\)' lua plugin` empty.

### E.4 — Tests (split: dep-light renderers + edgy-gated windows)

- **Dep-light (primary): `tests/sidebar.lua` renderer purity.** The section
  renderers are pure `card → string[]`; build the shop card (via `entity_at` like
  smoke.lua) and assert `render.identity_rows(card)` contains the name/kind,
  `render.note_rows` reflects an authored note, etc. No edgy needed.
- **edgy-gated (skip-if-absent): window/ft + context wiring.** If `pcall(require,
  "edgy")`, call `sidebar.setup(require("edgy"))`, assert the five buffers exist
  with the right `filetype`, then `sidebar.set_context(card)` populates the IDENTITY
  + NOTES buffers and `sidebar.on_delta(...)` appends an AFFECTED line +
  `last_affected_ids`. Resolve edgy via `demo/pack.lua` for a local run (skips in
  the dep-light sweep, like ast_nav's behavioural block).

---

## 3. Critical facts & decisions

### 3.1 — The accordion: NOT native; here's the lever + the controller (DECIDED)

**Spike result (edgy v1.10.2, read from source):** edgy stacks titled views in an
edgebar and sizes them; a `collapsed` view renders at title height. The edgebar's
`on_win_enter` autocmd **expands the entered collapsed view** (`win:show()`) **but
never collapses the siblings** — so "expand focused / collapse others" is *not*
built in (exactly CONCEPT §5.1's prediction).

**The lever** (the one piece of edgy-internal reach; verify it in E.0):
```lua
local function views()  -- the right edgebar's Edgy.View list, or {}
  local bar = require("edgy.config").layout["right"]
  return bar and bar.views or {}
end
local function set_collapsed(view, collapsed)
  local win = view.wins[1] or view.pinned_win
  if win then win:show(not collapsed) end       -- Window:show(visible)
end
-- after toggling any set:
require("edgy.layout").update()
```

**Two accordion drivers, same lever:**
1. **Content-accordion (default, glanceable):** on `set_context`, expand sections
   that have content for the current entity, collapse the empty ones. This is the
   "the relevant section expands as you move" feel without the user ever leaving the
   code buffer. Match views to `M.bufs` by `ft`.
2. **Focus-accordion (when the user enters the dock):** a `WinEnter` autocmd — if
   the entered window is one of our `tyo3_*` views, expand it and collapse the other
   `tyo3_*` views. Pairs with edgy's built-in `]w`/`[w` view nav.

**Fallback (keep the phase un-stuck):** if the lever proves fragile across resizes,
ship **all sections open with edgy-distributed `size` + an empty section rendering
`(none)`** — a clean stacked dock, no accordion. The DoD (§4) is met either way;
the accordion is the polish, the stacked edgy views are the substance.

### 3.2 — Each view is a window+buffer: the `pinned`/`open` pattern (DECIDED)

edgy claims a window into the edgebar by its buffer's `ft`. Our buffers are
**persistent** (created once in `setup`), so each view is `pinned = true` with an
`open` that *displays our existing buffer* in a new window:

```lua
{ title = "IDENTITY", ft = "tyo3_identity", pinned = true,
  open = function() vim.cmd("vertical sbuffer " .. M.bufs.identity) end },
-- …one per section. `collapsed = true` on NOTES/DOCS/SUMMARY (start empty).
```

`pinned = true` keeps the view in the edgebar even when its window is closed (so the
dock is stable); `open` is how edgy re-creates the window on toggle. Confirm
`sbuffer` lands the buffer in an edgy-managed window in E.0 (alt: `nvim_open_win` +
`set_buf`; sbuffer is simplest and matches edgy's command-string examples).

### 3.3 — The dock is observe-only + cursor-driven, not focus-driven (DECIDED)

The dock is a read-only knowledge surface; the user stays in the *code* buffer and
it updates from `context.on_cursor`. So the **content-accordion is the primary
mechanism** (§3.1.1) — "focused section" means *the section with data for the entity
under the cursor*, not a focused window. The focus-accordion (§3.1.2) is a secondary
affordance for when the user deliberately jumps into the dock to read. Don't invert
this: a purely focus-driven accordion would never expand anything during normal
navigation. (This is also why the sidebar is set up *once*, not per-buffer.)

### 3.4 — Re-point ALL six consumers; keep `last_affected_ids` (DECIDED)

Work the §0 table top to bottom. The two easy-to-miss ones: **`picker.lua:125`**
reads `panel.last_affected_ids` (the `:TyO3Affected` picker breaks silently if the
field moves without re-pointing), and **`entitydoc.lua:69`** calls `panel.reload()`
after a doc save (so the DOCS view refreshes). Grep both panel and inspect to zero
before committing E.3.

### 3.5 — `:TyO3Inspect` → the IDENTITY view; delete the float (DECIDED)

CONCEPT §4: "`inspect.lua` float → the edgy IDENTITY section." So `inspect.lua` is
deleted and `:TyO3Inspect` becomes "open + focus the IDENTITY view"
(`sidebar.open(); require("edgy").select("right", …)` or focus the identity window).
`card.build_lines` survives as the IDENTITY renderer. If focusing a specific edgy
view is awkward, retiring `:TyO3Inspect` entirely is acceptable (the always-on dock
supersedes the on-demand float).

### 3.6 — `manage = false` ⇒ no sidebar (guardrail)

`setup_edgy` only runs under `manage ~= false`, so a managed-off user gets no edgy
sidebar. `sidebar.set_context` (called unconditionally from `context.on_cursor`)
must **no-op safely when `setup` never ran** (guard on `M._did_setup` / `M.bufs`
being nil) — don't error for the manage=false user. Decorations + the
`vim.diagnostic` layer namespace still give them an observe surface.

### 3.7 — edgy is a global window manager (accepted tradeoff)

Turning edgy on sets `laststatus=3` + `splitkeep=screen` and makes edgy arbitrate
*all* window layout. That's the opinionated-distribution bet (CONCEPT §0); it's
gated by `manage`. Note it in the commit / README stub so it's not a surprise.

---

## 4. Definition of done

- `deps.setup_edgy` sets `laststatus=3`/`splitkeep=screen` and registers five
  right-edge views (IDENTITY/NOTES/DOCS/SUMMARY/AFFECTED), each backed by a
  persistent `tyo3_*` scratch buffer; records edgy absent in `M.missing`.
- `lua/tyo3/sidebar/` renders each section from the cached card / bus into its
  buffer, exposes the panel-compatible API (`set_context`/`clear_context`/`reload`/
  `on_delta`/`on_derived`/`on_refinement`/`toggle`/`last_affected_ids`), and drives
  the content-accordion (+ the focus-accordion or the documented §3.1 fallback).
- All six consumers re-pointed; `panel.lua` + `inspect.lua` deleted; the grep is
  empty; `:TyO3Sidebar` toggles the dock; `:TyO3Inspect` focuses IDENTITY (or retired).
- `manage = false` ⇒ no sidebar and no error from `set_context`.
- **Dep-light sweep stays green**; `tests/sidebar.lua` renderer-purity asserts pass;
  the edgy-gated window/ft block passes locally (skips where edgy is absent). A
  `demo/sidebar/` GIF (§8) shows the dock tracking the cursor with the accordion.

---

## 5. Verify

```
PRISTINE=$(echo /nix/store/*neovim-unwrapped-0.12.2/bin/nvim | tr ' ' '\n' | head -1)
GRAMMARS=$(for d in /nix/store/*nvim-treesitter-grammars; do [ -e "$d/parser/python.so" ] && echo "$d" && break; done)
for t in smoke context lsp lsp_nav lsp_codeaction review_dedup lsp_symbols ast_nav sidebar; do
  devenv shell -- timeout 240 "$PRISTINE" --headless --clean --cmd "set rtp^=$GRAMMARS" \
    -u editors/tyo3.nvim/tests/minimal_init.lua \
    -c "luafile editors/tyo3.nvim/tests/$t.lua" 2>&1 | grep -E "checks, [0-9]+ failed|^\[FAIL\]"
done
# Daemon sanity (unchanged by E): devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov
```

⚠**`context.lua` test (`context.lua`/`tests/context.lua`) asserts the panel text.**
It will need updating for the new sidebar surface (or re-pointing its assertions to
`sidebar` buffers) — that's expected churn, not a regression. Update it as part of E.

---

## 6. Files you'll touch

- **Add:** `lua/tyo3/sidebar/init.lua`, `lua/tyo3/sidebar/render.lua`,
  `tests/sidebar.lua`, `demo/sidebar/` (+ `demo-record-sidebar` in `devenv.nix`).
- **Edit:** `lua/tyo3/deps.lua` (setup_edgy body), `lua/tyo3/context.lua`,
  `lua/tyo3/init.lua` (3 notification calls), `lua/tyo3/entitydoc.lua`,
  `lua/tyo3/picker.lua`, `plugin/tyo3.lua` (commands), `tests/context.lua`
  (re-point assertions).
- **Delete:** `lua/tyo3/panel.lua`, `lua/tyo3/inspect.lua`.
- **Reference (don't change):** `lua/tyo3/card.lua`, `lua/tyo3/decorate.lua`,
  `lua/tyo3/docs.lua`, `demo/pack.lua`.

---

## 7. Out of scope (later phase — do NOT start)

- **Phase F** — UI-dep CI test infra (hermetic edgy/stack provisioning so
  `sidebar.lua`'s window block + the demo run in CI), hero re-record, README,
  release. The `demo/pack.lua` + nix path is local-only until then.
- No new *data* surfaces (no REVIEW view — fold into NOTES ⚠). No actions in the
  dock (Phase C made it observe-only; keep it that way).

---

## 8. Verification of the sidebar UI

- **Primary gate (no stack): renderer purity** in `tests/sidebar.lua` (§E.4). The
  section renderers are pure `card → string[]`; this proves the data→view mapping
  without edgy and runs in the dep-light sweep.
- **edgy-gated block (skip-if-absent):** the five `tyo3_*` buffers exist with the
  right `filetype`; `set_context`/`on_delta` populate them; `last_affected_ids` is
  set. Resolve edgy via `demo/pack.lua` locally.
- **UI recording (local): a `demo/sidebar/` GIF.** Model it on `demo/codeaction/`;
  `pack.add{ "edgy.nvim", "snacks.nvim" }` (+ the python parser via `pack.grammars()`
  if you want treesitter context tracking, as `demo/astnav/` does). Set
  `laststatus=3` in the demo init. Beats: move the cursor across `checkout` →
  `show_label` and show the dock's IDENTITY/NOTES/SUMMARY updating and the relevant
  section accordion-expanding; author a note (via `gra` → Author) and show NOTES
  refresh; edit a dependency and show AFFECTED log a revision. Follow
  `.agents/skills/nvim-demo-record` + `…/nvim-demo-review`.
- **CI portability stays Phase F.**

> Mid-build, the thing worth pausing to flag is if the §3.1 collapse lever
> (`view.wins[1]:show()` + `edgy.layout.update()`) does NOT cleanly collapse/expand
> across resizes in E.0 — that's the one genuinely uncertain edgy behaviour. If it
> misbehaves, take the §3.1 fallback (all-open) and note it, rather than fighting
> edgy internals. Everything else — the view table, the `pinned`/`open` pattern, the
> consumer re-point, the observe-only reframe — is decided here; execute it.
