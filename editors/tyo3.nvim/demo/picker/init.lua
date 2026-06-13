-- tyo3.nvim demo — PICKER (proj 28, Phase B). Minimal, deterministic nvim config
-- for the snacks-backed picker recording. Launched as:
--   nvim -u editors/tyo3.nvim/demo/picker/init.lua store.py
--
-- Single path (proj 28): every TyO3 view is reached from ONE keyboard surface —
-- the hub (`<leader>t`). It fuzzy-filters to Entities / Authored / Affected (and
-- Diagnostics / Docs / Sidebar / ops), then opens that snacks picker; notes are
-- authored from the act surface (`<leader>c`). No `:TyO3*` command is typed.
-- This recording verifies the hub drives the pickers end to end.

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

-- snacks is a managed hard dep (deps.lua), but the pristine `--clean` nvim has a
-- bare runtimepath. Add the real installed snacks (the user's `vim.pack` opt
-- checkout, nix-store fallback) — plugins only, NOT the user's config. See
-- demo/pack.lua.
local here = vim.fn.fnamemodify(vim.fn.expand("<sfile>:p"), ":h")
local pack = dofile(vim.fn.fnamemodify(here, ":h") .. "/pack.lua")
local missing = pack.add({ "snacks.nvim" })
if #missing > 0 then
  vim.notify("[tyo3-demo] not found: " .. table.concat(missing, ", ") .. " — pickers will error", vim.log.levels.ERROR)
end

-- Resolve the plugin root from this file: demo/picker/init.lua → editors/tyo3.nvim.
local plugin_root = vim.fn.fnamemodify(here, ":h:h")
vim.opt.runtimepath:append(plugin_root)
vim.cmd("runtime! plugin/tyo3.lua")

require("tyo3").setup({
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  debounce_ms = 250,
  virtual_text = true,
  context_debounce_ms = 120,
})
