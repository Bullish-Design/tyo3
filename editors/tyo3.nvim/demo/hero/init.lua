-- tyo3.nvim HERO demo — the short, looping, single-path advertisement.
--
-- The navigate → observe → act loop in the snacks/sidebar idiom (proj 28): the
-- curated stack on the rtp (snacks/edgy/tiny-code-action/treesitter/treewalker),
-- the edgy accordion sidebar tracking the cursor, the always-on LSP bridge (so
-- `]d` walks needs_review + the type error and `gra` opens the action picker).
-- Loads only the curated plugins (resolved by demo/pack.lua from TYO3_NVIM_DEPS
-- inside devenv, else the local vim.pack opt dir) + tyo3.nvim, so the recording
-- is identical on any machine / in CI. Launched as:
--   nvim --clean -u editors/tyo3.nvim/demo/hero/init.lua store.py

vim.opt.compatible = false
vim.cmd("syntax enable")
vim.cmd("filetype plugin indent on")
vim.opt.swapfile = false
vim.opt.number = true
vim.opt.signcolumn = "yes"
vim.opt.termguicolors = true
-- edgy needs the global statusline (laststatus=3) to fully collapse views to
-- title height; splitkeep avoids scroll jumps as the accordion resizes.
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
  vim.notify("[tyo3-demo] not found: " .. table.concat(missing, ", ") .. " — hero will degrade", vim.log.levels.ERROR)
end
if not pack.grammars() then
  vim.notify("[tyo3-demo] no python treesitter parser found — context tracking will fall back", vim.log.levels.ERROR)
end

-- Resolve the plugin root from this file: demo/hero/init.lua → editors/tyo3.nvim.
local plugin_root = vim.fn.fnamemodify(here, ":h:h")
vim.opt.runtimepath:append(plugin_root)
vim.cmd("runtime! plugin/tyo3.lua")

require("tyo3").setup({
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  debounce_ms = 250,
  virtual_text = true,
  -- The cursor-context sidebar and the native LSP bridge (so `]d` walks
  -- needs_review + the type error and `gra` opens the action picker with the
  -- acknowledge quickfix) are always on now — no flags needed.
  context_debounce_ms = 100,
})

-- Treesitter highlighting on the buffer so the syntax tree reads clearly as
-- treewalker motion moves through it.
vim.api.nvim_create_autocmd("FileType", {
  pattern = "python",
  callback = function(ev)
    pcall(vim.treesitter.start, ev.buf, "python")
  end,
})
