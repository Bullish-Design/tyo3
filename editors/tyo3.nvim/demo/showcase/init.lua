-- tyo3.nvim SHOWCASE demo — the long, guided tour (proj 28).
--
-- The flagship walkthrough: navigate by structure (treewalker + textobjects),
-- enrich an entity through the full code-action hub, prove durable identity, then
-- use the diagnostics picker + review-acknowledge to refactor safely — with the
-- edgy sidebar narrating throughout (every pane populated at once).
--
-- Loads only the curated stack (via demo/pack.lua → TYO3_NVIM_DEPS inside devenv,
-- else the local vim.pack opt dir) + tyo3.nvim, so the recording is identical on
-- any machine / in CI. Launched as:
--   nvim --clean -u editors/tyo3.nvim/demo/showcase/init.lua store.py

vim.opt.compatible = false
vim.cmd("syntax enable")
vim.cmd("filetype plugin indent on")
vim.opt.swapfile = false
vim.opt.number = true
vim.opt.signcolumn = "yes"
vim.opt.termguicolors = true
-- edgy needs the global statusline to fully collapse views to title height.
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
  vim.notify("[tyo3-demo] not found: " .. table.concat(missing, ", ") .. " — showcase will degrade", vim.log.levels.ERROR)
end
if not pack.grammars() then
  vim.notify("[tyo3-demo] no python treesitter parser found — context tracking will fall back", vim.log.levels.ERROR)
end

-- Resolve the plugin root: demo/showcase/init.lua → editors/tyo3.nvim.
local plugin_root = vim.fn.fnamemodify(here, ":h:h")
vim.opt.runtimepath:append(plugin_root)
vim.cmd("runtime! plugin/tyo3.lua")

-- Default setup: the bridge, the sidebar, the AST keymaps, the diagnostic config
-- (rounded float-on-jump) — all on via `manage = true` (the default).
require("tyo3").setup({
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  debounce_ms = 250,
  virtual_text = true,
  context_debounce_ms = 100,
})

-- Treesitter highlighting so the syntax tree reads clearly as motion moves.
vim.api.nvim_create_autocmd("FileType", {
  pattern = "python",
  callback = function(ev)
    pcall(vim.treesitter.start, ev.buf, "python")
  end,
})

-- ── Robust `gra` for the scripted demo ───────────────────────────────────────
-- tyo3's `gra` (the action hub) is buffer-local to python buffers, so it wins
-- there. nvim 0.11+ also ships a GLOBAL default `gra` → vim.lsp.buf.code_action().
-- An async beat (the explain float / the action picker's scratch buffer) can
-- leave the cursor in a non-python scratch buffer between scripted keystrokes;
-- the next `gra` then falls through to that global default and errors
-- ("codeAction not supported"). Override the global `gra` to RECOVER: jump focus
-- back to the python code window and re-fire `gra` (which now hits the
-- buffer-local tyo3 map). Only ever triggers off a python buffer, so it never
-- shadows the real action hub. Invisible — no cmdline, no tape changes.
vim.keymap.set("n", "gra", function()
  for _, win in ipairs(vim.api.nvim_list_wins()) do
    local cfg = vim.api.nvim_win_get_config(win)
    local buf = vim.api.nvim_win_get_buf(win)
    if cfg.relative == "" and vim.bo[buf].filetype == "python" then
      vim.api.nvim_set_current_win(win)
      vim.api.nvim_feedkeys("gra", "m", false) -- re-fire; now hits the buffer-local map
      return
    end
  end
end, { desc = "tyo3 demo: recover `gra` to the python code window" })
