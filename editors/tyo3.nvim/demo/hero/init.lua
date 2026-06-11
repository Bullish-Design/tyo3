-- tyo3.nvim HERO demo — the short, looping, sidebar-forward advertisement.
--
-- Like demo/context/init.lua (rich CONTEXT dock tracking the cursor), but also
-- enables the native LSP bridge so the `]d` / code-action beat works. Loads only
-- tyo3.nvim (resolved from this file's location) so the recording is identical
-- on any machine / in CI. Launched as:
--   nvim --clean -u editors/tyo3.nvim/demo/hero/init.lua store.py

vim.opt.compatible = false
vim.cmd("syntax enable")
vim.cmd("filetype plugin indent on")
vim.opt.swapfile = false
vim.opt.number = true
vim.opt.signcolumn = "yes"
vim.opt.termguicolors = true
vim.opt.laststatus = 2

-- Resolve the plugin root: demo/hero/init.lua → editors/tyo3.nvim.
local here = vim.fn.fnamemodify(vim.fn.expand("<sfile>:p"), ":h")
local plugin_root = vim.fn.fnamemodify(here, ":h:h")
vim.opt.runtimepath:append(plugin_root)
vim.cmd("runtime! plugin/tyo3.lua")

require("tyo3").setup({
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  auto_start = true,
  debounce_ms = 250,
  virtual_text = true,
  panel = "always",
  -- Rich IDENTITY / NOTES / DOCS / SUMMARY / ACTIONS dock, cursor-tracked.
  context = "cursor",
  context_debounce_ms = 100,
  -- Native LSP bridge, so `]d` walks needs_review + the type error in one
  -- stream and the code-action menu offers the acknowledge quickfix.
  lsp = true,
})

-- Open the panel up front so the CONTEXT section is visible from the start.
vim.schedule(function()
  require("tyo3.panel").open()
end)

vim.opt.statusline = "  TyO3 ● live  %=%f  %l:%c "
