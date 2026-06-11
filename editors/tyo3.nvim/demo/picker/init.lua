-- tyo3.nvim demo — PICKER (proj 28, Phase B). Minimal, deterministic nvim config
-- for the snacks-backed picker recording. Launched as:
--   nvim -u editors/tyo3.nvim/demo/picker/init.lua store.py
--
-- Single path (proj 28): the three TyO3 pickers (:TyO3Entities / :TyO3Authored /
-- :TyO3Affected) and the `vim.ui.input` prompt for :TyO3Note all route through
-- snacks — there is no telescope / `vim.ui.select` fallback. This recording is
-- the Phase-B verification that the rewrite actually drives end to end.

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
