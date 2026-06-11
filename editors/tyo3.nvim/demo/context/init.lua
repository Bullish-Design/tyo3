-- tyo3.nvim CONTEXT demo — minimal, deterministic nvim config for the recording.
--
-- Like demo/init.lua, but turns the cursor-context feature ON (context =
-- "cursor") so the panel's CONTEXT section tracks the entity under the cursor.
-- Loads only tyo3.nvim (resolved from this file's location) so the recording is
-- identical on any machine / in CI. Launched as:
--   nvim --clean -u editors/tyo3.nvim/demo/context/init.lua store.py

vim.opt.compatible = false
vim.cmd("syntax enable")
vim.cmd("filetype plugin indent on")
vim.opt.swapfile = false
vim.opt.number = true
vim.opt.signcolumn = "yes"
vim.opt.termguicolors = true
vim.opt.laststatus = 2

-- Resolve the plugin root: demo/context/init.lua → editors/tyo3.nvim.
local here = vim.fn.fnamemodify(vim.fn.expand("<sfile>:p"), ":h")
local plugin_root = vim.fn.fnamemodify(here, ":h:h")
vim.opt.runtimepath:append(plugin_root)
vim.cmd("runtime! plugin/tyo3.lua")

require("tyo3").setup({
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  debounce_ms = 200,
  virtual_text = true,
  -- The feature under demo: the CONTEXT section auto-updates from the entity
  -- under the cursor (always on now), only when the enclosing def node changes.
  context_debounce_ms = 120,
})

-- Open the panel up front so the CONTEXT section is visible from the start.
vim.schedule(function()
  require("tyo3.panel").open()
end)

vim.opt.statusline = "  TyO3 ● context  %=%f  %l:%c "
