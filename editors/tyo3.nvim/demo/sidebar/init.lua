-- tyo3.nvim demo — EDGY ACCORDION SIDEBAR (proj 28, Phase E). Minimal,
-- deterministic nvim config for the sidebar recording. Launched as:
--   nvim -u editors/tyo3.nvim/demo/sidebar/init.lua store.py
--
-- Shows the edgy-managed sidebar tracking the cursor: IDENTITY/NOTES/DOCS/SUMMARY/
-- AFFECTED sections in vertically stacked accordion views. As you move through
-- code, the section with data expands and empty sections collapse to title height.

vim.opt.compatible = false
vim.cmd("syntax enable")
vim.cmd("filetype plugin indent on")
vim.opt.swapfile = false
vim.opt.number = true
vim.opt.signcolumn = "yes"
vim.opt.termguicolors = true
-- edgy needs laststatus=3 to fully collapse views to title height.
vim.opt.laststatus = 3
vim.opt.splitkeep = "screen"

-- The curated stack (plugins only, no user config).
local here = vim.fn.fnamemodify(vim.fn.expand("<sfile>:p"), ":h")
local pack = dofile(vim.fn.fnamemodify(here, ":h") .. "/pack.lua")
local missing = pack.add({
  "nvim-treesitter",
  "nvim-treesitter-textobjects",
  "treewalker.nvim",
  "tiny-code-action.nvim",
  "snacks.nvim",
  "edgy.nvim",
})
if #missing > 0 then
  vim.notify("[tyo3-demo] not found: " .. table.concat(missing, ", ") .. " — sidebar will error", vim.log.levels.ERROR)
end
if not pack.grammars() then
  vim.notify("[tyo3-demo] no python treesitter parser found — context tracking will fall back", vim.log.levels.ERROR)
end

-- Resolve the plugin root from this file: demo/sidebar/init.lua → editors/tyo3.nvim.
local plugin_root = vim.fn.fnamemodify(here, ":h:h")
vim.opt.runtimepath:append(plugin_root)
vim.cmd("runtime! plugin/tyo3.lua")

require("tyo3").setup({
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  debounce_ms = 250,
  virtual_text = true,
  context_debounce_ms = 100,
})

-- Treesitter highlighting on the buffer so AST navigation reads clearly.
vim.api.nvim_create_autocmd("FileType", {
  pattern = "python",
  callback = function(ev)
    pcall(vim.treesitter.start, ev.buf, "python")
  end,
})
