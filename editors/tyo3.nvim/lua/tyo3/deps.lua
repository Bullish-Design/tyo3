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

-- ── edgy / treesitter / treewalker — stubs (wired in Phases D/E) ──────────────
--
-- These are hard deps of the full experience, so we record them as missing now
-- (single path — `:checkhealth` tells the user to install the stack), but the
-- real configuration lands in later phases:
--   * treesitter-textobjects + treewalker keymaps  → Phase D
--   * edgy accordion sidebar views                 → Phase E
-- Leaving a presence check here keeps the install story honest before the bodies
-- exist, and gives each phase a ready-made `setup_*` to fill in.

function M.setup_edgy(_opts)
  if not pcall(require, "edgy") then
    record_missing("edgy.nvim", "accordion sidebar (Phase E)")
    return
  end
  -- TODO(Phase E): register the right-edge section views (IDENTITY/NOTES/…).
end

function M.setup_treesitter(_opts)
  if not pcall(require, "nvim-treesitter") then
    record_missing("nvim-treesitter (+ textobjects)", "AST text-object selections (Phase D)")
    return
  end
  -- TODO(Phase D): configure textobjects select keymaps (af/if, ac/ic, aa/ia).
end

function M.setup_treewalker(_opts)
  if not pcall(require, "treewalker") then
    record_missing("treewalker.nvim", "AST motion: parent/child/sibling (Phase D)")
    return
  end
  -- TODO(Phase D): bind parent/child/prev/next-sibling motion keymaps.
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
