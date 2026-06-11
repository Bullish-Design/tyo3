-- tyo3.nvim demo — minimal, deterministic nvim config for the recording.
--
-- No user plugins; loads only tyo3.nvim (resolved from this file's location) so
-- the recording is identical on any machine / in CI. Launched as:
--   nvim -u editors/tyo3.nvim/demo/default/init.lua store.py

vim.opt.compatible = false
-- Full syntax highlighting + filetype handling. setup.sh resolves a *pristine*
-- nvim (the real neovim-unwrapped ELF, not the home-manager wrapper) to drive
-- the recording, so no stray user after/ftplugin is on the runtimepath and we
-- can enable everything the normal editor would: .py buffers are coloured and
-- the plugin's *.py autocmds fire.
vim.cmd("syntax enable")
vim.cmd("filetype plugin indent on")
vim.opt.swapfile = false
vim.opt.number = true
vim.opt.signcolumn = "yes"
vim.opt.termguicolors = true
vim.opt.laststatus = 2

-- Resolve the plugin root from this file: demo/default/init.lua → editors/tyo3.nvim.
local here = vim.fn.fnamemodify(vim.fn.expand("<sfile>:p"), ":h")
local plugin_root = vim.fn.fnamemodify(here, ":h:h")
vim.opt.runtimepath:append(plugin_root)
vim.cmd("runtime! plugin/tyo3.lua")

require("tyo3").setup({
  -- Use the module form so no console-script install is required for the demo.
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  -- A touch above the typing speed so a multi-keystroke edit (ciwprice) commits
  -- once, after typing settles — one overlay sync, not a partial then a full one.
  debounce_ms = 400,
  virtual_text = true,
})

-- Open the ambient affected-set panel up front (Scene 1).
vim.schedule(function()
  require("tyo3.panel").open()
  -- Showcase the spine code-action plugin seam (proj 26): a third-party-style
  -- action registered in ONE call. It appears in the SAME code-action menu
  -- (gra / tiny-code-action / vim.lsp.buf.code_action) as the built-in
  -- Explain / Simplify — the whole point of the registry. A real plugin would
  -- point `verb` at its own daemon verb; here it reuses `explain`.
  local ok, lsp = pcall(require, "tyo3.lsp")
  if ok then
    lsp.register_entity_action({
      title = "myplugin: Draft a docstring",
      verb = "explain",
      params = { mode = "explain" },
    })
  end
end)

-- A friendly statusline note that the engine is live.
vim.opt.statusline = "  TyO3 ● live  %=%f  %l:%c "
