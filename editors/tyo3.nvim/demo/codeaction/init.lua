-- tyo3.nvim demo — CODE ACTION (proj 28, Phase C). Minimal, deterministic nvim
-- config for the tiny-code-action buffer-picker recording. Launched as:
--   nvim -u editors/tyo3.nvim/demo/codeaction/init.lua store.py
--
-- Single path (proj 28): the LSP code-action registry is the *one* "act on the
-- entity" surface, driven through the tiny-code-action buffer picker (the default
-- `<leader>c` keymap, bound buffer-local on attach). Author / Write doc / Move /
-- Explain·Simplify / Acknowledge all flow through it. This recording is the
-- Phase-C verification that the real picker drives those providers end to end.

-- Space is the leader, so the curated act surface (`<leader>c`) and hub
-- (`<leader>t`) trigger as ` c` / ` t`. Set before any keymap binds.
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

-- tiny-code-action (the buffer picker) and snacks (the `vim.ui.input` prompt the
-- Author/Move actions use) are managed hard deps (deps.lua), but the pristine
-- `--clean` nvim has a bare runtimepath. Add the real installed plugins (the
-- user's `vim.pack` opt checkout, nix-store fallback) — plugins only, NOT the
-- user's config. See demo/pack.lua.
local here = vim.fn.fnamemodify(vim.fn.expand("<sfile>:p"), ":h")
local pack = dofile(vim.fn.fnamemodify(here, ":h") .. "/pack.lua")
local missing = pack.add({ "tiny-code-action.nvim", "snacks.nvim" })
if #missing > 0 then
  vim.notify(
    "[tyo3-demo] not found: " .. table.concat(missing, ", ") .. " — the code-action picker will error",
    vim.log.levels.ERROR
  )
end

-- Resolve the plugin root from this file: demo/codeaction/init.lua → editors/tyo3.nvim.
local plugin_root = vim.fn.fnamemodify(here, ":h:h")
vim.opt.runtimepath:append(plugin_root)
vim.cmd("runtime! plugin/tyo3.lua")

require("tyo3").setup({
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  debounce_ms = 250,
  virtual_text = true,
  context_debounce_ms = 120,
})
