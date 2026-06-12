-- tyo3.nvim tests — hermetic dep provisioning (Phase F).
--
-- One env signal, two consumers: `TYO3_NVIM_DEPS` is a `:`-separated list of
-- plugin dirs + the python treesitter grammar dir, exported by devenv from the
-- pinned Nix-built stack (devenv.nix). This module (read by the headless specs
-- via minimal_init.lua) and `demo/pack.lua` (read by the demos) both consume it.
--
-- No-op when the var is unset: the dep-light engine specs (smoke/lsp*/review_dedup)
-- run with nothing on the runtimepath, exactly as before; the UI specs
-- (ast_nav/sidebar) keep their skip-if-absent guards. So the cheap gate still runs
-- everywhere, and the UI gate runs where the stack is provisioned. Dependency-free.

local M = {}

--- Prepend every entry in `TYO3_NVIM_DEPS` to the runtimepath (the grammar dir
--- included, so `parser/python.so` resolves). Returns the dirs added (empty when
--- the var is unset or empty — the no-stack case).
function M.setup()
  local deps = os.getenv("TYO3_NVIM_DEPS")
  if not deps or deps == "" then
    return {}
  end
  local added = {}
  for entry in vim.gsplit(deps, ":", { plain = true }) do
    if entry ~= "" and vim.fn.isdirectory(entry) == 1 then
      vim.opt.runtimepath:prepend(entry)
      table.insert(added, entry)
    end
  end
  return added
end

return M
