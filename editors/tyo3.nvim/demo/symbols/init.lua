-- tyo3.nvim SYMBOLS demo — minimal, deterministic nvim config for the recording.
--
-- Turns the native LSP bridge ON (lsp = true) so the Phase-A symbol surfaces ride
-- Neovim's own machinery: `vim.lsp.buf.document_symbol()`, `…workspace_symbol()`,
-- and `…incoming_calls()` / `…outgoing_calls()` all answer against tyo3's durable
-- graph and render into the native quickfix/loclist. Loads only tyo3.nvim
-- (resolved from this file's location) so the recording is identical anywhere.
-- Launched as:
--   nvim --clean -u editors/tyo3.nvim/demo/symbols/init.lua store.py

vim.opt.compatible = false
vim.cmd("syntax enable")
vim.cmd("filetype plugin indent on")
vim.opt.swapfile = false
vim.opt.number = true
vim.opt.signcolumn = "yes"
vim.opt.termguicolors = true
vim.opt.laststatus = 2

-- Resolve the plugin root: demo/symbols/init.lua → editors/tyo3.nvim.
local here = vim.fn.fnamemodify(vim.fn.expand("<sfile>:p"), ":h")
local plugin_root = vim.fn.fnamemodify(here, ":h:h")
vim.opt.runtimepath:append(plugin_root)
vim.cmd("runtime! plugin/tyo3.lua")

require("tyo3").setup({
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  auto_start = true,
  debounce_ms = 200,
  -- The features under demo: the in-process LSP bridge attaches on BufEnter and
  -- serves documentSymbol / workspace symbol / call hierarchy from the spine.
  lsp = true,
  -- Keep the native quickfix/loclist the focus — no inline virtual text.
  virtual_text = false,
})

vim.opt.statusline = "  TyO3 ● symbols  %=%f  %l:%c "
