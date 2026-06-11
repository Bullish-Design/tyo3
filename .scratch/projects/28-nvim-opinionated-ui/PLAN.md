# tyo3.nvim — Opinionated UI: Implementation Plan (proj 28)

Companion to [`CONCEPT.md`](CONCEPT.md). File-level, phased, with verify commands.
Conventions (from CLAUDE.md / project memory):

- All project commands through devenv: `devenv shell -- build`, `… test-fast`,
  `… pytest src/tyo3/daemon/tests -q --no-cov`.
- Headless Lua specs:
  ```
  devenv shell -- nvim --headless --clean \
    -u editors/tyo3.nvim/tests/minimal_init.lua \
    -c "luafile editors/tyo3.nvim/tests/<spec>.lua"
  ```
- No AI attribution in commits/PRs/comments. Branch off `main`, commit per phase.
- Each phase independently shippable; commit when green before moving on.

Plugin API note: snacks/edgy/tiny-code-action/treesitter evolve fast. Where this
plan sketches their config, **pin a version and verify the API shape** (the
`require(...).setup{}` keys especially) before coding — flagged inline as ⚠API.

---

## Phase A — Tier 1 LSP bridges (additive, no UI change)

**Goal:** documentSymbol, workspace/symbol, and call hierarchy over the existing
graph data. Pure-additive; ships independently of the UI redesign.

### A.1 `documentSymbol` — no daemon change

`decorate {path}` already returns `[{durable_id, name, qualified_name, kind,
range, note?, summary?}]` for a file. Map it to a **hierarchical**
`DocumentSymbol[]` in Lua (nest methods under their class via the dotted
`qualified_name`).

- `lua/tyo3/lsp.lua`:
  - `CAPS.documentSymbolProvider = true`.
  - Add `SYMBOL_KIND` map: daemon `kind.value` → LSP `SymbolKind`
    (function→12, class→5, method→6, …).
  - Handler `handlers["textDocument/documentSymbol"]`: call `decorate {path}`,
    build a tree keyed by `qualified_name` segments; each node:
    `{ name, kind, range = daemon_range_to_lsp(range), selectionRange = same,
       children = {...} }`. Sort by start line.
- Powers: breadcrumbs/winbar (snacks/aerial), `gO`, outline, `Snacks.picker.lsp_symbols`.

### A.2 `workspace/symbol` — small daemon verb

- `src/tyo3/daemon/handlers.py`: new `symbols(params)` verb — walk the head graph
  for entity nodes (same filter as `decorate`: `is_entity_durable_id`, not
  external), return `[{durable_id, name, qualified_name, kind, path, range}]`.
  Optional `query` substring filter on `qualified_name` (server-side cap, e.g.
  500). Register in `_METHODS`. Mirror `decorate`'s one-snapshot pattern.
- `lua/tyo3/lsp.lua`:
  - `CAPS.workspaceSymbolProvider = true`.
  - Handler `handlers["workspace/symbol"]`: call `symbols {query = params.query}`,
    map to `SymbolInformation[]` `{name, kind, location = {uri = path_to_uri,
    range}}`.
- Powers: `Snacks.picker.lsp_workspace_symbols`, any LSP symbol picker.

### A.3 Call hierarchy — daemon verbs + bridge

The graph has `dependents`/`dependencies` (queries.py) and `find_references`.
Map: **incoming** = callers (references to the entity; each ref's enclosing
entity + the ref range as the call site); **outgoing** = the entity's own calls
(its dependency entities).

- `src/tyo3/daemon/handlers.py`: one verb mirroring `type_hierarchy`'s "return
  everything" shape, or two re-query verbs. Recommend a single
  `call_hierarchy(params{path,line,col})` returning
  `{ item, incoming: [{from: <item>, ranges: [...call sites]}],
     outgoing: [{to: <item>, ranges: [...]}] }`, where `<item>` is
  `{name, kind, path, full_range, selection_range}` (reuse the type-hierarchy
  item builder). Incoming from `find_references` grouped by enclosing entity
  (resolve each ref via `id_for`); outgoing from `dependencies` resolved to entity
  nodes. Register in `_METHODS`.
- `lua/tyo3/lsp.lua`:
  - `CAPS.callHierarchyProvider = true`. ⚠API verify whether nvim 0.12 gates the
    follow-ups on literal capability keys like type hierarchy did
    (`["callHierarchy/incomingCalls"]`); add them if so.
  - `handlers["textDocument/prepareCallHierarchy"]` → `call_hierarchy`, return
    `[lsp_call_item(item)]` (CallHierarchyItem: name, kind, uri, range,
    selectionRange).
  - `handlers["callHierarchy/incomingCalls"]` / `["callHierarchy/outgoingCalls"]`
    → re-query `call_hierarchy` off the incoming item's `selectionRange` (same
    pattern as `type_hierarchy_relatives`); map to
    `CallHierarchyIncomingCall[]` `{from, fromRanges}` /
    `CallHierarchyOutgoingCall[]` `{to, fromRanges}`.

### A.4 Tests

- `src/tyo3/daemon/tests/test_handlers.py`: `symbols` returns checkout +
  Item.price across files; `call_hierarchy` on `usd` lists `checkout` as an
  incoming caller (and a call-site range), `checkout` outgoing includes `usd`/`Book`.
- New `editors/tyo3.nvim/tests/lsp_symbols.lua`: attach the bridge, then
  - `textDocument/documentSymbol` on store.py → checkout + show_label with kinds;
  - `workspace/symbol {query="checkout"}` → a result with a location uri/range;
  - `prepareCallHierarchy` on usd → item; `incomingCalls` includes checkout.
  (Use the existing `lsp_codeaction.lua`/`lsp_nav.lua` harness scaffolding.)

### A.5 Verify
```
devenv shell -- build
devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov
devenv shell -- nvim --headless --clean -u editors/tyo3.nvim/tests/minimal_init.lua \
  -c "luafile editors/tyo3.nvim/tests/lsp_symbols.lua"
```
Commit: `feat: documentSymbol + workspace/symbol + call hierarchy bridges`.

---

## Phase B — Single-path conversion + dependency adoption

**Goal:** declare the stack, make the bridge always-on, route pickers/prompts
through snacks, delete the UI fallbacks. Mostly deletion + slimming.

### B.1 Dependency ownership — `lua/tyo3/deps.lua` (new)

Called from `M.setup` when `opts.manage ~= false`. Each integration is a small
`setup_*` function. **No fallback**: if a hard dep is missing, record a clear
error for `:checkhealth` (single path — the dep is required).

```lua
-- lua/tyo3/deps.lua (sketch)
local M = {}
local function have(mod) return pcall(require, mod) end
M.missing = {}

function M.setup(opts)
  if opts.manage == false then return end
  M.setup_snacks(opts)            -- picker + input + vim.ui overrides
  M.setup_tiny_code_action(opts)  -- buffer picker, hotkeys
  M.setup_edgy(opts)              -- register right-edge views (Phase E)
  M.setup_treesitter(opts)        -- textobjects (Phase D)
  M.setup_treewalker(opts)        -- motion keymaps (Phase D)
end
```

- ⚠API `setup_snacks`: ensure `require("snacks").setup({ picker = { enabled =
  true }, input = { enabled = true } })` (or detect an already-configured snacks
  and only enable the modules TyO3 needs). snacks' `vim.ui.select`/`vim.ui.input`
  overrides become the single prompt path.
- ⚠API `setup_tiny_code_action`: `require("tiny-code-action").setup({ picker = {
  "buffer", opts = { hotkeys = true } } })` (the buffer-picker-with-hotkeys mode
  the user asked for).
- README: a `lazy.nvim` spec with `dependencies = { "folke/snacks.nvim",
  "folke/edgy.nvim", "rachartier/tiny-code-action.nvim", "nvim-treesitter/
  nvim-treesitter", "nvim-treesitter/nvim-treesitter-textobjects",
  "aaronik/treewalker.nvim" }`.

### B.2 LSP bridge always-on — `lua/tyo3/lsp.lua`, `plugin/tyo3.lua`, `init.lua`

- Remove the `lsp` flag default-off behavior: attach the in-process server on
  every project python `BufEnter` unconditionally (`init.on_buf_enter` → attach).
- `layer_diagnostics` is always on (drop the `layer_diagnostics_enabled()`
  indirection or hardwire true).
- Delete `M.toggle` and the `:TyO3Lsp` command. Delete the `config.get().lsp`
  guards in `init.handle_notification` (push diagnostics always run).

### B.3 snacks pickers — `telescope.lua` → `lua/tyo3/picker.lua` (rewrite)

Rewrite the three pickers over `Snacks.picker`; delete `has_telescope` /
`select_fallback` / `vim.ui.select`. Keep the precise `jump_to` (decorate-fresh
range, from proj 27).

- ⚠API `Snacks.picker` custom-list source: `Snacks.picker({ items = entries,
  format = function(item) ... end, confirm = function(picker, item)
  picker:close(); jump_to(bufnr, item.durable_id) end })`. Verify the exact
  custom-source signature for the pinned snacks version.
- `entities()`, `affected()`, `authored(layer)` → snacks pickers (same entry
  data as today: durable_id + label).
- Update callers: `plugin/tyo3.lua` (`:TyO3Affected/Entities/Authored`),
  `actions.lua`'s "Affected set (picker)" entry (which moves into the action
  registry in Phase C).
- Note: project-wide symbol jump is now better served by `workspace/symbol`
  (A.2) via `Snacks.picker.lsp_workspace_symbols`; `entities()` can stay as the
  TyO3-flavored variant or be retired — decide in Phase C.

### B.4 prompts through snacks — `notes.lua`, `actions.lua`, `docs.lua`

- snacks' `vim.ui` overrides mean existing `vim.ui.input`/`vim.ui.select` calls
  now route through snacks automatically (single path, no code change beyond
  enabling the override). Leave call sites as `vim.ui.*`; they're snacks-backed.
- Remove the comment claims about "fallback when telescope absent".

### B.5 config slimming — `lua/tyo3/config.lua`

- **Remove:** `lsp`, `auto_start`, `layer_diagnostics`, `panel`, `context`
  (always-on now).
- **Keep:** `debounce_ms`, `context_debounce_ms`, `precision`, `root_markers`,
  `daemon_cmd`, `request_timeout_ms`, `log_level`, `overseer`, `virtual_text`
  (a display preference, not a path).
- **Add:** `manage = true` (dep ownership opt-out), `keymaps = {…}` (Phase C/D
  defaults), and an `ai`/`explain` note that AI actions are project-config-driven
  (the `explain` layer must be declared) — AI stays optional.
- Delete the `auto_start = false` autocmd asymmetry: the cursor/textchanged
  autocmds always run (always-start).

### B.6 Verify
- Engine/bridge specs must still pass (they don't need the UI deps):
  `smoke`, `lsp`, `lsp_nav`, `lsp_codeaction`, `review_dedup`, `lsp_symbols`.
- `picker.lua` / sidebar specs need the dep stack (Phase F infra).
Commit: `refactor(nvim): single-path UI — snacks pickers, always-on bridge, drop fallbacks`.

---

## Phase C — Action hub via tiny-code-action

**Goal:** one registry of "act on the entity" (the LSP code-action providers),
surfaced through the tiny-code-action buffer picker; delete `actions.lua` and the
panel ACTIONS pane. Navigation stays native.

### C.1 Fold `actions.lua` → code-action providers (`lua/tyo3/lsp.lua`)

For each ACTIONS-pane entry, register an entity-gated code action + a client
command (generalize the existing `tyo3.explain`/`tyo3.run`/`tyo3.ack` pattern):

| Old ACTIONS entry | New code action | Client command | Kind |
|---|---|---|---|
| 📝 Author `<layer>` | per discovered authored layer (via `layers` verb) | `tyo3.author` (prompt via `vim.ui.input`→snacks, then `author`) | `source.tyo3` |
| 📄 Write / edit doc | "Write/edit doc" | `tyo3.doc` → `entitydoc.edit_card` | `source.tyo3` |
| 🤖 Explain (AI) | existing (gate on `explain` layer present) | `tyo3.explain` | `source.tyo3` |
| ✨ Simplify | existing resolvable (P6) | resolve → WorkspaceEdit | `refactor.rewrite` |
| (review flagged) | existing acknowledge | `tyo3.ack` | `quickfix` |
| Move entity | "Move `<name>` to…" | `tyo3.move` (prompt dest, then `move`) | `refactor.move` |
| 🔍 Inspect / ↪ goto / 📞 callers | **drop** — IDENTITY section + native `gd`/`grr` | — | — |

- The `tyo3.author` command discovers writable layers from the `layers` verb
  (origin == "authored", minus `docs`/`explain` which have dedicated actions) and
  prompts per layer — same logic as `actions.author_layer` / `note_author_layers`,
  moved into a client command.
- `executeCommandProvider.commands` gains `tyo3.author`, `tyo3.doc`, `tyo3.move`.

### C.2 tiny-code-action buffer picker + default keymap (`deps.lua`, `plugin/tyo3.lua`)

- `deps.setup_tiny_code_action` configures the buffer picker with hotkeys.
- Bind a default, buffer-local keymap on project python buffers (in the BufEnter
  attach path or `deps`): e.g. `<leader>a` and/or keep `gra` →
  `require("tiny-code-action").code_action()`. Configurable via `config.keymaps`.
- tiny-code-action reads code actions from the attached LSP clients (our bridge),
  so the providers from C.1 flow in automatically; kinds give it icons/grouping.

### C.3 Preview pane (decision from CONCEPT §5.4)

- Simplify already resolves to a diff (P6) → preview shows before/after.
- For informational actions (author/doc/explain), **minimal**: rely on
  titles + kinds. *Optional enrichment:* extend `codeAction/resolve` to attach a
  small preview document (e.g. the entity card, or "will author on layer X") for
  non-edit actions — only if the picker renders it cleanly. Defer unless cheap.

### C.4 Delete

- `lua/tyo3/actions.lua` (folded into the registry).
- The panel ACTIONS pane rendering (it disappears with the edgy refactor, Phase
  E; until then, stop rendering it).
- Keep `:TyO3Note`/`:TyO3Doc`/`:TyO3Move` as **thin wrappers** over the same
  client-command bodies (power-user entry), but the *default* surface is the
  picker. (Single-path governs UI rendering, not the existence of ex-commands;
  they share one implementation so there's still one code path.)

### C.5 Tests

- Extend `lsp_codeaction.lua`: on an entity, the menu includes `tyo3.author`
  (per layer), `tyo3.doc`, and (when project declares it) `tyo3.move`; invoking
  `vim.lsp.commands["tyo3.author"]({...})` authors a note (poll `authored`).
- The tiny-code-action *UI* isn't headless-testable; test the **providers**
  (the code actions exist with the right commands/kinds), not the picker chrome.
Commit: `feat(nvim): unify entity actions into the code-action registry (tiny-code-action hub)`.

---

## Phase D — AST-native navigation

**Goal:** treesitter-textobjects (select) + treewalker (motion), bound by default,
buffer-local — so "you're navigating the AST" is the default feel and selections
feed entity resolution.

### D.1 treesitter-textobjects (`deps.setup_treesitter`)

- ⚠API pin nvim-treesitter (`main` vs `master` have different config surfaces).
  Configure `select` keymaps buffer-local for python:
  `af/if` @function.outer/inner, `ac/ic` @class.outer/inner, `aa/ia`
  @parameter.outer/inner. These selections already feed the code-action
  selection→entity path (`id_for` on `range.start`), so `vif`→`<leader>a` acts on
  exactly that function.
- Keymaps live in `config.keymaps.textobjects` (defaults + opt-out).

### D.2 treewalker motion (`deps.setup_treewalker`)

- ⚠API `require("treewalker").setup{}` + bind parent/child/prev-sibling/
  next-sibling (defaults e.g. `<C-k>/<C-j>` siblings, `<C-h>/<C-l>` parent/child),
  buffer-local on project python buffers, from `config.keymaps.treewalker`.

### D.3 Wire motion → sidebar

- `context.lua`'s `CursorMoved` debounce already resolves the enclosing entity
  and updates the sidebar — treewalker motion triggers it for free. Consider a
  lower `context_debounce_ms` default (e.g. 80–100ms) for a snappy "the dock
  follows me through the tree" feel.
- *Optional polish:* on entity-enter, the LSP `documentHighlight` already
  highlights occurrences; a subtle full-range highlight of the current entity
  could reinforce "this node is selected" — defer.

### D.4 Tests

- Config/keymap, hard to unit-test the motion itself. Spec
  (`tests/ast_nav.lua`): on a project buffer, assert the textobject + treewalker
  keymaps are set (buffer-local), and that selecting a function textobject then
  requesting `textDocument/codeAction` resolves the entity (reuse codeaction
  machinery — proves the selection→entity wiring).
Commit: `feat(nvim): default AST navigation (treesitter-textobjects + treewalker)`.

---

## Phase E — edgy accordion sidebar

**Goal:** replace `panel.lua`'s hand-rolled window with edgy-managed, vertically
stacked, accordion section views. **Spike the accordion first (CONCEPT §5.1).**

### E.0 Spike (do before the rest of E)

Stand up two edgy right-edge views and confirm the "expand focused / collapse
others to title height" behavior via edgy's size API + a `WinEnter` resize hook.
If edgy can't do it cleanly, decide: custom resize controller vs. a simpler
"all-open, fixed sizes" layout. Record the finding in this dir.

### E.1 Section views (`deps.setup_edgy`)

Register one edgy right-edge view per category, each a scratch buffer with a
distinct filetype:

| View | ft | Content (from the entity card / bus) |
|---|---|---|
| IDENTITY | `tyo3_identity` | id, kind, location, content hash, last-affected (reuse `card.build_lines`) |
| NOTES | `tyo3_notes` | authored layers (note/intent), needs_review flag |
| DOCS | `tyo3_docs` | the markdown `docs` layer |
| SUMMARY | `tyo3_summary` | derived summary artifact(s) |
| AFFECTED | `tyo3_affected` | the blast-radius log (current panel AFFECTED) |
| REVIEW | `tyo3_review` | needs_review / orphaned (from `review_state`) — optional |

### E.2 Section renderers (`lua/tyo3/sidebar/` new package)

- `sidebar/init.lua`: lifecycle + the accordion controller (WinEnter → expand
  focused, collapse siblings); section navigation keymaps (up/down between
  sections).
- One module per section (or a table-driven renderer): each renders its slice of
  the cached entity card into its buffer. Reuse `card.lua` helpers for IDENTITY.
- Subscribe to the same events `panel.lua` used:
  - `context.lua` cursor-entity change → update IDENTITY/NOTES/DOCS/SUMMARY/REVIEW.
  - `init.handle_notification` `delta`/`derived`/`refinement` → update
    AFFECTED/REVIEW (move `panel.on_delta`/`on_derived`/`on_refinement` logic in).

### E.3 Delete

- `lua/tyo3/panel.lua` (window mgmt + monolithic render + ACTIONS pane).
- `lua/tyo3/inspect.lua` (IDENTITY section replaces the float).
- `:TyO3Panel` → `:TyO3Sidebar` toggle (edgy open/close), `:TyO3Context` likely
  retired (context always on).

### E.4 Tests

- edgy/headless is awkward; `tests/sidebar.lua` asserts the section buffers exist
  with the right filetypes and that a cursor move populates IDENTITY/NOTES for a
  known entity. Lighter coverage — note it.
Commit: `feat(nvim): edgy accordion sidebar (replaces panel.lua)`.

---

## Phase F — tests, dep CI, demo, README, release

### F.1 Test infra (`tests/minimal_init.lua` + bootstrap)

- Provision the dep stack for UI specs: a `tests/bootstrap.lua` that ensures
  snacks/edgy/tiny-code-action/treesitter(+textobjects)/treewalker are on the
  runtimepath (vendor under `tests/deps/` or clone to a cache dir). ⚠ decide
  vendoring vs. fetch-on-CI.
- Keep engine/bridge specs (`smoke`, `lsp`, `lsp_nav`, `lsp_codeaction`,
  `review_dedup`, `lsp_symbols`) runnable **without** the UI deps; gate UI specs
  (`picker`, `ast_nav`, `sidebar`) on the stack.
- Confirm/ensure the Lua specs run under `test-ci` (CONCEPT §5.3).

### F.2 Re-record the hero demo (`demo/hero/`)

New loop, snacks-flavored: treewalk between functions → the accordion sidebar
expands sections as you move → `<leader>a` opens the tiny-code-action buffer
picker → acknowledge review (warning clears) and/or Simplify (diff preview).
Keep it < ~800KB, ~35s. (For the Simplify diff offline, see CONCEPT §1.2 — AI
optional; the ack beat works offline.)

### F.3 README rewrite (`editors/tyo3.nvim/README.md`)

- New install: the `lazy.nvim` spec with dependencies; nvim 0.12 floor.
- The single-path philosophy + the navigate/observe/act model.
- Default keymaps (textobjects, treewalker, `<leader>a` action picker), the
  `manage`/`keymaps` opts.
- New screenshots/gif; drop the "opt-in/additive/degrades" framing.

### F.4 Release

- Version bump (minor → 0.6.0 or as appropriate), tag, push (per the established
  release flow: bump `pyproject.toml`, `rust/Cargo.toml`, `rust/Cargo.lock`,
  `src/tyo3/__init__.py`; annotated `vX.Y.Z` tag).

---

## Open questions to resolve in-flight (not blockers)

1. **`entities()` picker fate** — retire in favor of `workspace/symbol` via
   `Snacks.picker.lsp_workspace_symbols`, or keep as a TyO3-flavored variant?
2. **tiny-code-action preview enrichment** for non-edit actions (C.3) — minimal
   titles, or `codeAction/resolve` previews?
3. **edgy accordion** mechanism (E.0 spike outcome) — native size API vs. custom
   controller vs. fixed-size fallback layout.
4. **treesitter version** pin (`main` vs `master`) and the matching textobjects
   config shape.
5. **Test-dep provisioning** — vendor under `tests/deps/` vs. fetch-on-CI.

## Cross-cutting checklist

- [ ] A: documentSymbol + workspace/symbol + call hierarchy + tests
- [ ] B: deps.lua ownership, always-on bridge, snacks pickers, config slim, delete fallbacks
- [ ] C: actions.lua → code-action registry, tiny-code-action buffer picker + keymap, delete ACTIONS pane
- [ ] D: textobjects + treewalker default keymaps, motion→sidebar wiring
- [ ] E: edgy accordion sidebar (spike first), delete panel.lua/inspect.lua
- [ ] F: test-dep infra, hero re-record, README rewrite, release
