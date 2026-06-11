# Phase D implementation guide — AST-native navigation (proj 28)

Companion to [`CONCEPT.md`](CONCEPT.md) §2 (navigate/observe/act) and
[`PLAN.md`](PLAN.md) §"Phase D". This guide is the file-level build spec, written
against the **actual post-Phase-C code** (branch `proj28-phase-c-actionhub`) and
the **actual installed plugin versions** (both nvim-treesitter and
nvim-treesitter-textobjects are on `main`, not `master` — that matters, see §3.1).
Where this guide and PLAN's Phase D table differ, this guide wins.

> **One-line goal:** make AST motion the default feel — treesitter-textobjects
> (`af`/`if`, `ac`/`ic`, `aa`/`ia` selections) + treewalker (parent/child/sibling
> motion) bound **buffer-local on project python buffers**, so "you're navigating
> the syntax tree" is the default and a textobject selection feeds straight into
> the entity-resolving code-action path. Navigate stays *raw AST*; the dock does
> the semantic naming. This is a pure-addition phase (no deletion).

---

## 0. Where Phase C left things (read this first)

- **`lua/tyo3/deps.lua`** owns the curated stack on `setup` (opt out via
  `manage = false`). It already has the two stubs you fill in:
  - `M.setup_treesitter(opts)` — currently `pcall(require,"nvim-treesitter")` →
    `record_missing(...)` + a `TODO(Phase D)`.
  - `M.setup_treewalker(opts)` — currently `pcall(require,"treewalker")` →
    `record_missing(...)` + a `TODO(Phase D)`.
  Both are called from `M.setup(opts)` after the snacks/tiny-code-action setup.
  `M.missing` (a list of `{mod,role}`) feeds `:checkhealth tyo3`.
- **`lua/tyo3/lsp.lua`** already has the model for **buffer-local keymap binding**:
  `bind_code_action_keymap(bufnr)` reads `config.keymaps.code_action` (default
  `gra`), honours `false`/string, and is called from `M.attach(bufnr,root)`. Copy
  this shape (lazy-require the plugin in the callback; notify if absent).
- **`lua/tyo3/init.lua`** `M.on_buf_enter(bufnr)` is the per-buffer entry point:
  it guards `filetype == "python"` + a resolved `root`, opens/syncs the daemon,
  calls `require("tyo3.lsp").attach(bufnr, root)` (which binds `gra`) and
  `refresh_layer_diagnostics`. **This is where you call the new keymap binder.**
- **`lua/tyo3/config.lua`** `keymaps = { code_action = "gra" }`. You extend it
  with `textobjects` + `treewalker` sub-tables (§1).
- **`lua/tyo3/context.lua`** already debounces `CursorMoved` → resolves the
  enclosing entity → updates the dock (`config.context_debounce_ms`, default 150).
  **treewalker/textobject motion fires `CursorMoved` for free — no new wiring** to
  make the dock follow you through the tree (§2 D.3).
- **The selection→entity path already exists:** the `textDocument/codeAction`
  handler resolves `range.start` up to its enclosing durable entity via the
  daemon. So `vif` (select function inner) then `gra` already acts on exactly that
  function — Phase D just makes the *selection* ergonomic. Nothing to add here.
- **The curated stack IS installed locally** (`~/.local/share/nvim/site/pack/core/opt/`):
  `nvim-treesitter` (`main`), `nvim-treesitter-textobjects` (`main`),
  `treewalker.nvim` — all resolvable via `editors/tyo3.nvim/demo/pack.lua`. The
  python **textobjects queries ship with the textobjects plugin**
  (`queries/python/textobjects.scm`); they're found via runtimepath once the
  plugin dir is on it.

**Branch:** `git checkout proj28-phase-c-actionhub && git checkout -b
proj28-phase-d-astnav` (if C has since merged to `main`, branch off `main`).

---

## 1. The keymaps (defaults → `config.keymaps`)

Extend `config.lua`'s `keymaps` table. Each leaf is a string (rebind) or `false`
(opt out); a whole sub-table set to `false` opts the feature out. Buffer-local,
project-python only.

```lua
keymaps = {
  code_action = "gra",                       -- (Phase C, unchanged)
  -- treesitter-textobjects SELECT (operator-pending + visual: {"x","o"}).
  textobjects = {
    function_outer  = "af", function_inner  = "if",
    class_outer     = "ac", class_inner     = "ic",
    parameter_outer = "aa", parameter_inner = "ia",
  },
  -- treewalker MOTION (normal + visual: {"n","x"}). NOTE the window-nav clash
  -- (§3.4): these shadow <C-hjkl> window moves on project python buffers only.
  treewalker = {
    up = "<C-k>", down = "<C-j>",   -- previous / next neighbour (sibling-ish)
    parent = "<C-h>", child = "<C-l>", -- ancestor (out) / into child (in)
  },
}
```

| Key (mode) | Action | Wiring |
|---|---|---|
| `af`/`if` (x,o) | select `@function.outer`/`.inner` | `select.select_textobject("@function.outer","textobjects")` |
| `ac`/`ic` (x,o) | select `@class.outer`/`.inner` | `…("@class.outer","textobjects")` |
| `aa`/`ia` (x,o) | select `@parameter.outer`/`.inner` | `…("@parameter.outer","textobjects")` |
| `<C-k>`/`<C-j>` (n,x) | prev/next neighbour | `:Treewalker Up`/`Down` |
| `<C-h>`/`<C-l>` (n,x) | ancestor / into child | `:Treewalker Left`/`Right` |

`@parameter` is the textobjects query name for a function argument/param.

---

## 2. Build order

### D.1 — treesitter-textobjects setup (`deps.setup_treesitter`)

⚠**The installed plugin is on `main`** — the API is the *new* one (§3.1). Do **not**
reach for `require("nvim-treesitter.configs").setup{ textobjects = … }` (that is the
`master` API and does not exist here).

```lua
function M.setup_treesitter(_opts)
  -- Parser-engine presence (nvim-treesitter main installs parsers + queries; we
  -- don't force-install — the user's TS config / Phase F provisions parsers).
  if not pcall(require, "nvim-treesitter") then
    record_missing("nvim-treesitter (main)", "AST parsers/queries for text objects (Phase D)")
  end
  local ok, tso = pcall(require, "nvim-treesitter-textobjects")
  if not ok then
    record_missing("nvim-treesitter-textobjects (main)", "AST text-object selections (Phase D)")
    return
  end
  -- `main` config surface: select.lookahead is the only setting we need; the
  -- per-key maps are bound buffer-local in bind_ast_keymaps (NOT here — `main`
  -- does not own the keymaps like `master`'s module did).
  pcall(tso.setup, { select = { lookahead = true } })
end
```

- **Selections are bound in `bind_ast_keymaps`** (D.3), not in setup — `main`'s
  `setup{}` only configures behaviour; you bind the maps yourself with
  `require("nvim-treesitter-textobjects.select").select_textobject(query, "textobjects")`.
- Don't manage highlighting or parser install — that's the user's nvim-treesitter
  config (and Phase F for hermetic CI). textobjects only needs a *live parser* for
  the buffer, which `vim.treesitter` resolves from the runtimepath.

### D.2 — treewalker setup (`deps.setup_treewalker`)

```lua
function M.setup_treewalker(_opts)
  local ok, tw = pcall(require, "treewalker")
  if not ok then
    record_missing("treewalker.nvim", "AST motion: parent/child/sibling (Phase D)")
    return
  end
  -- Defaults are good; keep the brief post-jump highlight (it reinforces "this
  -- node is selected"). Motion keymaps bind buffer-local in bind_ast_keymaps.
  pcall(tw.setup, {}) -- highlight=true, highlight_duration=250 by default
end
```

- treewalker exposes `:Treewalker Up/Down/Left/Right` (and `move_up/down/out/in`).
  Up=prev neighbour, Down=next neighbour, Left=ancestor (out), Right=into child.
  Bind via `<cmd>Treewalker Up<cr>` so the jumplist/highlight behaviour is the
  plugin's, not ours.

### D.3 — buffer-local keymap binder (`deps.bind_ast_keymaps(bufnr)`)

A new directly-callable function (the model is `lsp.bind_code_action_keymap`).
Bind both feature sets here so there is **one** buffer-local binding site, gated
on `manage ~= false` (these maps require the managed plugins; unlike `gra` there
is no native fallback — a missing plugin should notify + point at `:checkhealth`).

```lua
--- Bind the AST navigation keymaps (textobjects select + treewalker motion)
--- buffer-local for *bufnr*. Idempotent (buffer-local; re-binding on re-enter is
--- harmless). No-op when `manage = false` (the user owns the stack + its maps).
function M.bind_ast_keymaps(bufnr)
  local cfg = require("tyo3.config").get()
  if cfg.manage == false then
    return
  end
  local km = cfg.keymaps or {}
  -- textobjects: each key → select_textobject on {x,o}.
  local TO = { function_outer="@function.outer", function_inner="@function.inner",
               class_outer="@class.outer", class_inner="@class.inner",
               parameter_outer="@parameter.outer", parameter_inner="@parameter.inner" }
  local to = km.textobjects
  if to ~= false then
    to = to or {}
    for slot, query in pairs(TO) do
      local key = to[slot]
      if key and key ~= "" then
        vim.keymap.set({ "x", "o" }, key, function()
          local ok, sel = pcall(require, "nvim-treesitter-textobjects.select")
          if ok then sel.select_textobject(query, "textobjects")
          else vim.notify("[tyo3] nvim-treesitter-textobjects missing (:checkhealth tyo3)", vim.log.levels.WARN) end
        end, { buffer = bufnr, silent = true, desc = "tyo3: select " .. query })
      end
    end
  end
  -- treewalker: motion via :Treewalker on {n,x}.
  local TW = { up="Up", down="Down", parent="Left", child="Right" }
  local tw = km.treewalker
  if tw ~= false then
    tw = tw or {}
    for slot, dir in pairs(TW) do
      local key = tw[slot]
      if key and key ~= "" then
        vim.keymap.set({ "n", "x" }, key, "<cmd>Treewalker " .. dir .. "<cr>",
          { buffer = bufnr, silent = true, desc = "tyo3: Treewalker " .. dir })
      end
    end
  end
end
```

Then call it from `init.on_buf_enter`, right after the `lsp.attach` line:

```lua
require("tyo3.lsp").attach(bufnr, root)
require("tyo3.deps").bind_ast_keymaps(bufnr)   -- ← add
require("tyo3.lsp").refresh_layer_diagnostics(bufnr, root)
```

(Putting the binder in `deps` keeps the managed-plugin keymaps with the managed
plugins, and `bind_ast_keymaps(bufnr)` is directly callable from the headless
spec — the same testability split as `lsp.attach` / `bind_code_action_keymap`.)

### D.4 — motion → sidebar (free; one optional tune)

- **Nothing to wire.** `context.on_cursor` (the `CursorMoved`/`CursorMovedI`
  autocmd in `plugin/tyo3.lua`) already debounces → resolves the enclosing entity
  → updates the dock. treewalker/textobject motion moves the cursor, so the dock
  follows for free.
- **Optional tune:** drop `config.defaults.context_debounce_ms` from 150 → ~100
  for a snappier "the dock tracks me through the tree" feel. Low-risk; mention it
  in the commit if you do it. Don't go below ~80 (the resolve is an actor hop).

### D.5 — config defaults

Add the `textobjects`/`treewalker` sub-tables to `config.defaults.keymaps` (§1).
`vim.tbl_deep_extend("force", …)` already merges per-leaf, so a user setting one
key keeps the rest of the defaults, and `false` (leaf or sub-table) opts out.

---

## 3. Critical facts & decisions

### 3.1 — Both plugins are on `main`, not `master` (DECIDED — this is the spike)

`CONCEPT.md` §5.2 flagged "nvim-treesitter `main` vs `master` API split" as a
risk-to-verify. **Verified: the installed `nvim-treesitter` and
`nvim-treesitter-textobjects` are both on `main`.** Consequences you must build to:

- **No `nvim-treesitter.configs`.** The old `require("nvim-treesitter.configs")
  .setup{ ensure_installed, highlight, textobjects = { select = { keymaps = … } } }`
  module system is gone on `main`. textobjects config is
  `require("nvim-treesitter-textobjects").setup{ select = {...}, move = {...} }`
  and **you bind the select keymaps yourself** via
  `require("nvim-treesitter-textobjects.select").select_textobject("@function.outer",
  "textobjects")` (verified against the installed `select.lua`:
  `M.select_textobject(query_string, query_group)`).
- **Parsers are not auto-installed on `main`.** `nvim-treesitter` main installs
  parsers via `:TSInstall`/`require("nvim-treesitter").install()` into a stdpath;
  highlighting is `vim.treesitter.start()`. **Don't** do either in `setup_*` —
  the user's TS config owns parser install; for tests/demo you put the python
  parser on the runtimepath (the nix-store grammars, §5/§8). textobjects only
  needs `vim.treesitter` to find a parser for the buffer.
- **The textobjects queries ship with the textobjects plugin**
  (`queries/python/textobjects.scm`), found via runtimepath once the plugin dir
  is on it (pack.add / the installed opt dir). No separate query install.

### 3.2 — Keep navigation raw; do NOT snap motion to entities (DECIDED)

Per CONCEPT §2: treewalker navigates *syntax*; TyO3 names *semantics* in the dock.
**Bind plain treewalker/textobjects — don't add a "jump to next entity" wrapper**
that filters AST nodes to durable entities. The payoff ("syntax under the cursor,
durable identity in the dock") comes precisely from letting motion be raw AST and
letting `context.on_cursor` resolve the enclosing entity. Resisting the wrapper is
the design, not a shortcut.

### 3.3 — One buffer-local binding site, gated on `manage` (DECIDED)

Bind in `deps.bind_ast_keymaps(bufnr)`, called from `on_buf_enter`. **Gate on
`manage ~= false`** (these maps have no native fallback — unlike `gra` — so a
managed-off user owns them). Lazy-require the plugin in each callback and notify
on absence (mirrors `bind_code_action_keymap`), so binding is always safe and the
keymap-presence spec is dep-light (§3.5). Idempotent because buffer-local.

### 3.4 — The `<C-hjkl>` window-nav clash (guardrail)

treewalker's suggested `<C-hjkl>` shadow the common window-move maps **on project
python buffers only** (buffer-local). That's acceptable for an opinionated distro
but is the most likely user complaint. Mitigate: (a) they're buffer-local (window
nav still works everywhere else), (b) fully rebindable via
`config.keymaps.treewalker`, (c) `false` opts out. Document the default + the
clash in the commit / README stub. If you'd rather avoid the clash entirely,
`[[`/`]]`-family defaults are the alternative — but the plan picked `<C-hjkl>`;
keep it unless you have a reason, and just make the opt-out obvious.

### 3.5 — Test what you can dep-light; gate the rest on the stack (DECIDED)

Mirror Phase C's split:
- **Dep-light (primary gate): keymap presence.** Because binding lazy-requires the
  plugins, `bind_ast_keymaps(bufnr)` sets the buffer-local maps **with no plugin
  installed**. A new `tests/ast_nav.lua` builds the shop project, opens a python
  buffer, calls `deps.bind_ast_keymaps(bufnr)`, and asserts via
  `vim.api.nvim_buf_get_keymap(bufnr, "x")` / `"o"` / `"n"` that `af`/`if`/`ac`/
  `ic`/`aa`/`ia` and `<C-k>/<C-j>/<C-h>/<C-l>` are bound buffer-local; plus
  `manage=false` ⇒ none bound, and a `keymaps.treewalker=false` ⇒ motion maps
  absent but textobjects present. **No stack needed** → runs in the existing
  dep-light harness.
- **Behavioural (stack-gated, skip-if-absent): selection → entity.** In the same
  spec, if `demo/pack.lua` resolves the stack AND a python parser is on the rtp,
  select `@function.outer` over a function then request `textDocument/codeAction`
  and assert the entity resolved (reuse the codeaction machinery). Guard the whole
  block on the stack being resolvable so the spec stays green where it isn't.

---

## 4. Definition of done

- `deps.setup_treesitter` / `setup_treewalker` configure the **`main`-API** plugins
  (textobjects `setup{ select = { lookahead = true } }`; treewalker `setup{}`),
  recording each absent plugin in `M.missing` (→ `:checkhealth tyo3`).
- `deps.bind_ast_keymaps(bufnr)` binds the textobject select (`af`/`if`/`ac`/`ic`/
  `aa`/`ia`, modes x+o) and treewalker motion (`<C-k>/<C-j>/<C-h>/<C-l>`, modes
  n+x) **buffer-local**, from `config.keymaps`, no-op under `manage = false`,
  honouring per-leaf/sub-table `false` + string rebinds. Called from
  `init.on_buf_enter` after `lsp.attach`.
- `config.defaults.keymaps` gains the `textobjects` + `treewalker` sub-tables.
- The dock follows treewalker/textobject motion (free, via `context.on_cursor`).
- **Dep-light sweep stays green** and `tests/ast_nav.lua` passes its keymap-presence
  assertions (the behavioural select→entity block may skip where the stack/parser
  is absent). A `demo/astnav/` GIF (§8) shows real motion + select → act, locally.

---

## 5. Verify

```
# Dep-light regression sweep + the new ast_nav presence gate (0 failed).
PRISTINE=$(echo /nix/store/*neovim-unwrapped-0.12.2/bin/nvim | tr ' ' '\n' | head -1)
GRAMMARS=$(for d in /nix/store/*nvim-treesitter-grammars; do [ -e "$d/parser/python.so" ] && echo "$d" && break; done)
for t in smoke context lsp lsp_nav lsp_codeaction review_dedup lsp_symbols ast_nav; do
  devenv shell -- timeout 240 "$PRISTINE" --headless --clean --cmd "set rtp^=$GRAMMARS" \
    -u editors/tyo3.nvim/tests/minimal_init.lua \
    -c "luafile editors/tyo3.nvim/tests/$t.lua" 2>&1 | grep -E "checks, [0-9]+ failed|^\[FAIL\]"
done
# Daemon sanity (unchanged by D): devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov
```

(The `--cmd "set rtp^=$GRAMMARS"` puts the python parser on the rtp so the
behavioural select→entity block — if the stack resolves — has a live parser; the
keymap-presence assertions don't need it.)

---

## 6. Files you'll touch

- **Edit:** `lua/tyo3/deps.lua` (setup_treesitter / setup_treewalker bodies +
  new `bind_ast_keymaps`), `lua/tyo3/init.lua` (call `bind_ast_keymaps` in
  `on_buf_enter`), `lua/tyo3/config.lua` (`keymaps.textobjects`/`.treewalker`
  defaults; maybe `context_debounce_ms`).
- **Add:** `tests/ast_nav.lua`; `demo/astnav/` (init.lua + tape + gif/txt) and a
  `demo-record-astnav` script in `devenv.nix` (model on `demo/codeaction/`).
- **Reference (don't change):** `lua/tyo3/lsp.lua` (`bind_code_action_keymap` is
  the binder model; the codeAction handler is the select→entity consumer),
  `lua/tyo3/context.lua` (the free motion→dock wiring), `demo/pack.lua`.

---

## 7. Out of scope (later phases — do NOT start)

- **Phase E** — edgy accordion sidebar (replaces `panel.lua`/`inspect.lua`). D
  leaves the dock exactly as C left it; you only rely on `context.on_cursor`.
- **Phase F** — UI-dep CI test infra (hermetic stack + parser provisioning so
  `ast_nav.lua`'s behavioural block + the demo run in CI), hero re-record, README,
  release. The local `demo/pack.lua` + nix-grammars path is local-only until then.
- No "snap motion to entities" wrapper (§3.2). No highlighting/parser management
  (the user's nvim-treesitter owns it).

---

## 8. Verification of the AST-nav UI

- **Primary gate (no stack): `ast_nav.lua` keymap presence** (§3.5). Sufficient
  for D's DoD — it proves the binder wires every default map buffer-local, honours
  `manage`/`false`/rebinds, and doesn't bind under `manage=false`.
- **Behavioural block (stack-gated):** select `@function.outer` → `codeAction`
  resolves the entity. Guard on `demo/pack.lua` resolving
  `nvim-treesitter`/`nvim-treesitter-textobjects` AND a python parser on the rtp;
  skip cleanly otherwise.
- **UI recording (local): a `demo/astnav/` GIF.** Model it on `demo/codeaction/`.
  **The extra wrinkle vs picker/codeaction: AST nav needs a live python parser**,
  which a pristine `--clean` nvim lacks (nvim bundles c/lua/vim/markdown, not
  python). So the astnav `init.lua` must **prepend a python-parser dir to the
  runtimepath** (reuse the nix-store `*nvim-treesitter-grammars` glob the verify
  harness uses, or extend `demo/pack.lua` with a `grammars()` resolver), in
  addition to `pack.add{ "nvim-treesitter", "nvim-treesitter-textobjects",
  "treewalker.nvim", "snacks.nvim" }`. Tape beats: treewalker `<C-j>/<C-k>` to
  move between siblings, `<C-l>` into a child / `<C-h>` out to the parent (the
  dock re-resolves the entity each move — the money shot), then `vif`/`vac` to
  select a node and `gra` to act on it. Follow `.agents/skills/nvim-demo-record`
  (pristine nvim, single devenv sandbox) and `…/nvim-demo-review` (read the `.txt`).
- **CI portability stays Phase F** — provisioning the parser + plugins hermetically
  in devenv. The local GIF + the behavioural block don't block on it.

> Mid-build, the only thing worth pausing to flag is an unexpected **`main`-API
> shape** in the installed textobjects/treewalker vs what §3.1 records (e.g. a
> renamed `select_textobject` or a different `setup` key). Everything else —
> the keymap defaults, the `manage` gate, the test split, the demo parser wrinkle
> — is decided here; execute it.
