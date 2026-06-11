-- tyo3.nvim — dependency ownership (the "batteries included" seam).
--
-- proj 28 turns tyo3.nvim from an additive toolkit into an opinionated
-- distribution: one curated stack, one path through every interaction. TyO3
-- *owns* the setup of that stack (snacks / tiny-code-action / edgy / treesitter
-- / treewalker) so a fresh install is cohesive out of the box.
--
-- One escape hatch: `setup{ manage = false }` disables all of this and hands the
-- stack back to the user (they configure snacks et al. themselves). TyO3 still
-- works — the bridge attaches, the commands run — it just doesn't touch anyone
-- else's plugins.
--
-- Single path, no silent fallback: a missing *hard* dep is recorded in
-- `M.missing` and surfaced as a clear `:checkhealth tyo3` error, rather than
-- degrading to a lesser UI. (The LLM/AI layer stays optional — see lsp.lua.)

local M = {}

-- Hard deps we couldn't `require`, as { mod = <module>, role = <what it powers> }.
-- Read by health.lua to turn each into a `:checkhealth` error.
M.missing = {}

local function record_missing(mod, role)
  table.insert(M.missing, { mod = mod, role = role })
end

-- ── snacks.nvim — picker + input + the `vim.ui` overrides ─────────────────────
--
-- snacks owns three things for us: `Snacks.picker` (our own pickers, see
-- picker.lua), `vim.ui.select` (via `picker.ui_select`), and `vim.ui.input`
-- (via the `input` module) — so every select/prompt in the plugin (and the rest
-- of the editor) routes through one cohesive UI with no code-site branching.
function M.setup_snacks(_opts)
  local ok, snacks = pcall(require, "snacks")
  if not ok then
    record_missing("snacks.nvim", "picker, input, and vim.ui overrides")
    return
  end
  -- `snacks.setup` errors if called twice (it guards on `did_setup`). If the
  -- user already configured snacks, trust their config and don't fight it —
  -- `ui_select`/`input` default on, which is all we need.
  if snacks.did_setup then
    return
  end
  pcall(snacks.setup, {
    picker = { enabled = true, ui_select = true },
    input = { enabled = true },
  })
end

-- ── tiny-code-action.nvim — the action surface ───────────────────────────────
--
-- The buffer picker (hotkey-driven) is the default "act on the entity" surface;
-- it reads code actions from the attached LSP clients (our in-process bridge),
-- so the providers registered in lsp.lua flow in automatically. The picker chrome
-- is wired here; the providers/keymap are Phase C.
function M.setup_tiny_code_action(_opts)
  local ok, tca = pcall(require, "tiny-code-action")
  if not ok then
    record_missing("tiny-code-action.nvim", "code-action buffer picker (the act-on-entity surface)")
    return
  end
  pcall(tca.setup, {
    picker = { "buffer", opts = { hotkeys = true } },
  })
end

-- ── edgy.nvim — accordion sidebar (Phase E) ──────────────────────────────────
--
-- edgy manages the window/layout for five vertically stacked, right-edge section
-- views (IDENTITY / NOTES / DOCS / SUMMARY / AFFECTED). Each view is a persistent
-- scratch buffer joined by filetype; a content-driven accordion expands sections
-- that have data for the current entity. edgy is a global window manager — it
-- sets laststatus=3 / splitkeep=screen for the whole editor, which is the
-- opinionated-distribution tradeoff (gated by manage).

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

  -- sidebar.setup creates buffers + registers views (handles both pre-setup
  -- and post-setup edgy scenarios).
  require("tyo3.sidebar").setup(edgy)

  -- If edgy hasn't been set up yet (test/demo path), call edgy.setup now
  -- so it picks up the view specs we merged into edgy.config.opts.
  -- If already set up (lazy.nvim path, did_setup guard), this is a no-op.
  if not pcall(function() return require("edgy.config").did_setup end) then
    return -- can't access config at all
  end
  if not require("edgy.config").did_setup then
    local opts = require("edgy.config").opts or {}
    edgy.setup(opts)
  end
end

-- ── treesitter-textobjects — AST node selections (Phase D) ────────────────────
--
-- ⚠The installed nvim-treesitter (+ textobjects) are on the `main` branch, NOT
-- `master`: there is no `nvim-treesitter.configs` module system. textobjects is
-- configured via its own `setup{}` and the *select* keymaps are bound by us,
-- buffer-local, in `bind_ast_keymaps` (so they ride the per-buffer attach and the
-- headless spec can call the binder directly). We don't install parsers or manage
-- highlighting here — the user's nvim-treesitter config (and Phase F for CI) owns
-- parser install; textobjects only needs `vim.treesitter` to resolve a parser for
-- the buffer, and its python queries ship with the plugin (queries/python/…).
function M.setup_treesitter(_opts)
  if not pcall(require, "nvim-treesitter") then
    record_missing("nvim-treesitter (main)", "AST parsers/queries for text objects (Phase D)")
  end
  local ok, tso = pcall(require, "nvim-treesitter-textobjects")
  if not ok then
    record_missing("nvim-treesitter-textobjects (main)", "AST text-object selections (Phase D)")
    return
  end
  -- `main` config surface: lookahead makes a select jump to the next match when
  -- the cursor is before one. The per-key maps are bound in `bind_ast_keymaps`.
  pcall(tso.setup, { select = { lookahead = true } })
end

-- ── treewalker — AST motion (Phase D) ─────────────────────────────────────────
--
-- Parent/child/sibling motion over the syntax tree. We keep the defaults (incl.
-- the brief post-jump highlight, which reinforces "this node is selected"); the
-- motion keymaps bind buffer-local in `bind_ast_keymaps` via `:Treewalker …` so
-- the jumplist/highlight behaviour stays the plugin's.
function M.setup_treewalker(_opts)
  local ok, tw = pcall(require, "treewalker")
  if not ok then
    record_missing("treewalker.nvim", "AST motion: parent/child/sibling (Phase D)")
    return
  end
  pcall(tw.setup, {}) -- highlight = true, highlight_duration = 250 by default
end

-- ── AST navigation keymaps (Phase D) ──────────────────────────────────────────
--
-- One buffer-local binding site for both feature sets, called from
-- `init.on_buf_enter` after the LSP attach (the model is `lsp.bind_code_action_keymap`).
-- Gated on `manage ~= false`: unlike `gra` (which falls back to native code
-- actions), these maps have no native equivalent, so a managed-off user owns them.
-- Each callback lazy-requires its plugin and notifies on absence, so binding is
-- always safe — that's also what lets the keymap-presence spec run dep-light.

-- slot → textobjects query (selected on {x,o}).
local TEXTOBJECT_QUERIES = {
  function_outer = "@function.outer",
  function_inner = "@function.inner",
  class_outer = "@class.outer",
  class_inner = "@class.inner",
  parameter_outer = "@parameter.outer",
  parameter_inner = "@parameter.inner",
}

-- slot → :Treewalker direction (motion on {n,x}). Up/Down = prev/next neighbour,
-- Left = ancestor (out), Right = into child (in).
local TREEWALKER_DIRS = { up = "Up", down = "Down", parent = "Left", child = "Right" }

--- Bind the AST navigation keymaps (textobjects select + treewalker motion)
--- buffer-local for *bufnr*, from `config.keymaps`. Idempotent (buffer-local;
--- re-binding on re-enter is harmless). No-op under `manage = false`. A per-leaf
--- string rebinds; a leaf or whole sub-table set to `false` opts out.
function M.bind_ast_keymaps(bufnr)
  local cfg = require("tyo3.config").get()
  if cfg.manage == false then
    return
  end
  local km = cfg.keymaps or {}

  local textobjects = km.textobjects
  if textobjects ~= false then
    textobjects = textobjects or {}
    for slot, query in pairs(TEXTOBJECT_QUERIES) do
      local key = textobjects[slot]
      if key and key ~= "" then
        vim.keymap.set({ "x", "o" }, key, function()
          local ok, select = pcall(require, "nvim-treesitter-textobjects.select")
          if ok then
            select.select_textobject(query, "textobjects")
          else
            vim.notify("[tyo3] nvim-treesitter-textobjects missing (:checkhealth tyo3)", vim.log.levels.WARN)
          end
        end, { buffer = bufnr, silent = true, desc = "tyo3: select " .. query })
      end
    end
  end

  local treewalker = km.treewalker
  if treewalker ~= false then
    treewalker = treewalker or {}
    for slot, dir in pairs(TREEWALKER_DIRS) do
      local key = treewalker[slot]
      if key and key ~= "" then
        vim.keymap.set(
          { "n", "x" },
          key,
          "<cmd>Treewalker " .. dir .. "<cr>",
          { buffer = bufnr, silent = true, desc = "tyo3: Treewalker " .. dir }
        )
      end
    end
  end
end

-- ── entry point ──────────────────────────────────────────────────────────────

--- Configure the curated stack. Called from `tyo3.setup`. With
--- `opts.manage == false` this is a no-op: the user owns the stack.
function M.setup(opts)
  opts = opts or {}
  if opts.manage == false then
    return
  end
  M.missing = {}
  M.setup_snacks(opts)
  M.setup_tiny_code_action(opts)
  M.setup_edgy(opts)
  M.setup_treesitter(opts)
  M.setup_treewalker(opts)
end

return M
