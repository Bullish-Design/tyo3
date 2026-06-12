-- tyo3.nvim demo — resolve the curated plugin stack (plugins ONLY, not config).
--
-- The recordings need the real installed plugins (snacks / tiny-code-action /
-- edgy / treewalker / treesitter) but must NOT load the user's nvim *config*
-- (~/.dotfiles/nvim/init.lua), whose `--clean` config injection (which-key etc.)
-- erupts headless. So a demo `init.lua` launches the pristine nvim with a
-- plugin-only config and calls `pack.add{...}` to put just the plugins it needs
-- on the runtimepath.
--
-- Source of truth, in order: (1) `TYO3_NVIM_DEPS` — the `:`-separated Nix store
-- dirs devenv exports from the pinned curated stack (Phase F; the same signal
-- `tests/bootstrap.lua` reads, so specs and demos provision identically); then
-- (2) the local `vim.pack` opt dir
-- (`~/.local/share/nvim/site/pack/core/opt/<plugin>`), where nvim 0.12's built-in
-- package manager installs the git plugins listed in the user's init.lua; then
-- (3) a nix-store glob for the plugins also packaged in nixpkgs (snacks/edgy), so
-- a machine without either still records. Inside the devenv shell (1) is set;
-- outside it, the resolution falls through to the local checkout.

local M = {}

local OPT = vim.fn.expand("~/.local/share/nvim/site/pack/core/opt")

-- `TYO3_NVIM_DEPS` entries (Nix store plugin dirs + grammar dir), or {} when unset.
local function env_deps()
  local deps = os.getenv("TYO3_NVIM_DEPS")
  if not deps or deps == "" then
    return {}
  end
  local entries = {}
  for entry in vim.gsplit(deps, ":", { plain = true }) do
    if entry ~= "" and vim.fn.isdirectory(entry) == 1 then
      table.insert(entries, entry)
    end
  end
  return entries
end

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
---
--- When `TYO3_NVIM_DEPS` is set (inside the devenv shell), every provisioned entry
--- is put on the rtp and a name counts as resolved if any entry's basename
--- contains it; otherwise each name resolves via the local vim.pack/nix path.
function M.add(names)
  local entries = env_deps()
  if #entries > 0 then
    for _, dir in ipairs(entries) do
      vim.opt.runtimepath:prepend(dir)
    end
    local missing = {}
    for _, name in ipairs(names) do
      local found = false
      for _, dir in ipairs(entries) do
        if vim.fn.fnamemodify(dir, ":t"):find(name, 1, true) then
          found = true
          break
        end
      end
      if not found then
        table.insert(missing, name)
      end
    end
    return missing
  end

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

--- Prepend a treesitter *grammar* dir carrying the python parser to the rtp, and
--- return it (or nil). AST nav (treewalker / textobjects) needs a live python
--- parser, which a pristine `--clean` nvim lacks (it bundles c/lua/vim/markdown,
--- not python). nixpkgs ships the compiled parsers under
--- `/nix/store/*nvim-treesitter-grammars*/parser/python.so` — the same dir the
--- headless verify harness puts on the rtp. (Hermetic CI provisioning is Phase F.)
function M.grammars()
  -- Phase F: prefer the provisioned grammar dir (the entry carrying
  -- `parser/python.so`) from `TYO3_NVIM_DEPS`; fall back to the nix-store glob.
  for _, p in ipairs(env_deps()) do
    if vim.fn.filereadable(p .. "/parser/python.so") == 1 then
      vim.opt.runtimepath:prepend(p)
      return p
    end
  end
  for _, p in ipairs(vim.fn.glob("/nix/store/*nvim-treesitter-grammars*", true, true)) do
    if vim.fn.filereadable(p .. "/parser/python.so") == 1 then
      vim.opt.runtimepath:prepend(p)
      return p
    end
  end
  return nil
end

return M
