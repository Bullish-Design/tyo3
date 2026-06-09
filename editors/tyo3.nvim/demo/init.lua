-- tyo3.nvim demo — minimal, deterministic nvim config for the recording.
--
-- No user plugins; loads only tyo3.nvim (resolved from this file's location) so
-- the recording is identical on any machine / in CI. Launched as:
--   nvim -u editors/tyo3.nvim/demo/init.lua store.py

vim.opt.compatible = false
vim.cmd("filetype plugin indent on")
vim.opt.swapfile = false
vim.opt.number = true
vim.opt.signcolumn = "yes"
vim.opt.termguicolors = true
vim.opt.laststatus = 2

-- Resolve the plugin root from this file: demo/init.lua → editors/tyo3.nvim.
local here = vim.fn.fnamemodify(vim.fn.expand("<sfile>:p"), ":h")
local plugin_root = vim.fn.fnamemodify(here, ":h")
vim.opt.runtimepath:append(plugin_root)
vim.cmd("runtime! plugin/tyo3.lua")

require("tyo3").setup({
  -- Use the module form so no console-script install is required for the demo.
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  auto_start = true,
  debounce_ms = 200,
  virtual_text = true,
  panel = "always",
})

-- Open the ambient affected-set panel up front (Scene 1).
vim.schedule(function()
  require("tyo3.panel").open()
end)

-- A friendly statusline note that the engine is live.
vim.opt.statusline = "  TyO3 ● live  %=%f  %l:%c "
