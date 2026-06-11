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
-- bare runtimepath. Resolve a snacks checkout from the nix store and prepend it,
-- with no hardcoded store hash (mirrors setup.sh's $TYO3_NVIM resolution). The
-- real test-dep provisioning is Phase F; this glob is enough to drive the demo.
local function add_snacks()
  for _, p in ipairs(vim.fn.glob("/nix/store/*vimplugin-snacks.nvim-*", true, true)) do
    if vim.fn.filereadable(p .. "/lua/snacks/init.lua") == 1 then
      vim.opt.runtimepath:prepend(p)
      return p
    end
  end
  return nil
end
local snacks_dir = add_snacks()
if not snacks_dir then
  vim.notify("[tyo3-demo] snacks.nvim not found on the nix store — pickers will error", vim.log.levels.ERROR)
end

-- Resolve the plugin root from this file: demo/picker/init.lua → editors/tyo3.nvim.
local here = vim.fn.fnamemodify(vim.fn.expand("<sfile>:p"), ":h")
local plugin_root = vim.fn.fnamemodify(here, ":h:h")
vim.opt.runtimepath:append(plugin_root)
vim.cmd("runtime! plugin/tyo3.lua")

require("tyo3").setup({
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  debounce_ms = 250,
  virtual_text = true,
  context_debounce_ms = 120,
})
