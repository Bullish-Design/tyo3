-- tyo3.nvim demo — AST NAV (proj 28, Phase D). Minimal, deterministic nvim config
-- for the treesitter-textobjects + treewalker recording. Launched as:
--   nvim -u editors/tyo3.nvim/demo/astnav/init.lua store.py
--
-- Single path (proj 28): AST motion is the default feel — treewalker moves you
-- through the syntax tree (parent/child/sibling) and textobjects select nodes
-- (af/if, ac/ic, aa/ia), both bound buffer-local on attach. The dock resolves the
-- *enclosing durable entity* as the cursor moves, so "syntax under the cursor,
-- durable identity in the dock" lands. This recording is the Phase-D verification.

-- Space is the leader, so the act surface (`<leader>c`) and hub (`<leader>t`)
-- trigger as ` c` / ` t`. Set before any keymap binds.
vim.g.mapleader = " "
vim.g.maplocalleader = " "

vim.opt.compatible = false
vim.cmd("syntax enable")
vim.cmd("filetype plugin indent on")
vim.opt.swapfile = false
vim.opt.number = true
vim.opt.signcolumn = "yes"
vim.opt.termguicolors = true
vim.opt.laststatus = 2

-- The curated stack is a managed hard dep (deps.lua), but the pristine `--clean`
-- nvim has a bare runtimepath. Add the real installed plugins (the user's
-- `vim.pack` opt checkout, nix-store fallback) — plugins only, NOT the user's
-- config — AND a python parser dir (AST nav needs a live parser, which `--clean`
-- nvim lacks). See demo/pack.lua.
local here = vim.fn.fnamemodify(vim.fn.expand("<sfile>:p"), ":h")
local pack = dofile(vim.fn.fnamemodify(here, ":h") .. "/pack.lua")
local missing = pack.add({
  "nvim-treesitter",
  "nvim-treesitter-textobjects",
  "treewalker.nvim",
  "tiny-code-action.nvim",
  "snacks.nvim",
})
if #missing > 0 then
  vim.notify("[tyo3-demo] not found: " .. table.concat(missing, ", ") .. " — AST nav will error", vim.log.levels.ERROR)
end
if not pack.grammars() then
  vim.notify("[tyo3-demo] no python treesitter parser found — AST nav will error", vim.log.levels.ERROR)
end

-- Resolve the plugin root from this file: demo/astnav/init.lua → editors/tyo3.nvim.
local plugin_root = vim.fn.fnamemodify(here, ":h:h")
vim.opt.runtimepath:append(plugin_root)
vim.cmd("runtime! plugin/tyo3.lua")

require("tyo3").setup({
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  debounce_ms = 250,
  virtual_text = true,
  context_debounce_ms = 100,
})

-- Treesitter highlighting on the buffer makes the AST motion read clearly (the
-- node the cursor sits on is obvious). Start it for python buffers.
vim.api.nvim_create_autocmd("FileType", {
  pattern = "python",
  callback = function(ev)
    pcall(vim.treesitter.start, ev.buf, "python")
  end,
})
