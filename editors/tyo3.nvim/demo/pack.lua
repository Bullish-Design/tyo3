-- tyo3.nvim demo — resolve the curated plugin stack (plugins ONLY, not config).
--
-- The recordings need the real installed plugins (snacks / tiny-code-action /
-- edgy / treewalker / treesitter) but must NOT load the user's nvim *config*
-- (~/.dotfiles/nvim/init.lua), whose `--clean` config injection (which-key etc.)
-- erupts headless. So a demo `init.lua` launches the pristine nvim with a
-- plugin-only config and calls `pack.add{...}` to put just the plugins it needs
-- on the runtimepath.
--
-- Source of truth: the local `vim.pack` opt dir
-- (`~/.local/share/nvim/site/pack/core/opt/<plugin>`), where nvim 0.12's built-in
-- package manager installs the git plugins listed in the user's init.lua. Falls
-- back to a nix-store glob for the two plugins also packaged in nixpkgs
-- (snacks/edgy), so a machine without the vim.pack checkout still records.
--
-- NOTE: this is the *local* recording path. Hermetic CI provisioning of the
-- stack (the project's devenv providing pinned vimPlugins) is the Phase-F task;
-- the user's init.lua `vim.pack` version pins are the ready-made source of truth
-- for those refs.

local M = {}

local OPT = vim.fn.expand("~/.local/share/nvim/site/pack/core/opt")

local function nix_glob(pat)
  for _, p in ipairs(vim.fn.glob("/nix/store/*" .. pat .. "*", true, true)) do
    if vim.fn.isdirectory(p .. "/lua") == 1 then
      return p
    end
  end
  return nil
end

-- Resolve one plugin dir: the vim.pack opt checkout first, then a nix-store glob.
local function resolve(name)
  local dir = OPT .. "/" .. name
  if vim.fn.isdirectory(dir) == 1 then
    return dir
  end
  -- nixpkgs names the dir e.g. `vimplugin-snacks.nvim-<ver>`; try that then bare.
  return nix_glob("vimplugin-" .. name) or nix_glob(name)
end

--- Prepend each named plugin (e.g. "snacks.nvim", "tiny-code-action.nvim") to the
--- runtimepath. Returns the list of names that couldn't be resolved (caller can
--- surface them, matching deps.lua's missing-dep contract).
function M.add(names)
  local missing = {}
  for _, name in ipairs(names) do
    local dir = resolve(name)
    if dir then
      vim.opt.runtimepath:prepend(dir)
    else
      table.insert(missing, name)
    end
  end
  return missing
end

return M
