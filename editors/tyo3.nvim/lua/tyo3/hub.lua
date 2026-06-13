-- tyo3.nvim — the hub: one picker for every observe/navigate/ops view.
--
-- proj 28: the ACT surface (`<leader>c` → tiny-code-action) mutates the entity
-- under the cursor. The HUB (`<leader>t`) is its sibling — the single keyboard
-- entry point to every project/buffer-scoped view and op (Entities / Affected /
-- Diagnostics / Authored / Docs / Sidebar / Reindex / Check / Gc). Fuzzy-filter
-- by intent ("aff" → Affected) instead of recalling a `:TyO3*` command name; the
-- commands still exist underneath — the hub just calls them.
--
-- Entries are `{ label, desc, run }`. Spine plugins append via `M.register`
-- (mirrors `lsp.register_entity_action` for the act surface).

local M = {}

-- The curated defaults. Each `run` defers its `require` so a missing optional
-- dep (overseer) only errors when that entry is chosen, not on hub open.
local function default_items()
  return {
    { label = "Entities", desc = "Browse all known entities", run = function()
      require("tyo3.picker").entities()
    end },
    { label = "Affected set", desc = "The last edit's blast radius", run = function()
      require("tyo3.picker").affected()
    end },
    { label = "Diagnostics", desc = "Type errors + durable layer state", run = function()
      require("tyo3.picker").diagnostics()
    end },
    { label = "Authored notes", desc = "Entities carrying an intent note", run = function()
      require("tyo3.picker").authored()
    end },
    { label = "Docs", desc = "Open the tyo3 documentation", run = function()
      require("tyo3.docs").index()
    end },
    { label = "Sidebar", desc = "Toggle the accordion sidebar", run = function()
      require("tyo3.sidebar").toggle()
    end },
    { label = "Reindex", desc = "Reindex the project (sync_all)", run = function()
      require("tyo3.overseer").run("reindex")
    end },
    { label = "Check", desc = "Type-check the project", run = function()
      require("tyo3.overseer").run("check")
    end },
    { label = "Gc", desc = "Garbage-collect derived artifacts", run = function()
      require("tyo3.overseer").run("gc")
    end },
  }
end

-- Extra entries registered by spine plugins.
M._extra = M._extra or {}

--- Register a hub entry: `{ label, desc, run }`.
function M.register(item)
  table.insert(M._extra, item)
end

--- Open the hub picker. Selecting an entry runs its `run` fn.
function M.open()
  local ok, Snacks = pcall(require, "snacks")
  if not ok or not Snacks.picker then
    vim.notify("[tyo3] snacks.nvim is required for the hub (see :checkhealth tyo3)", vim.log.levels.ERROR)
    return
  end
  local entries = default_items()
  for _, e in ipairs(M._extra) do
    table.insert(entries, e)
  end
  -- The text carries both label and desc so the fuzzy filter matches either
  -- ("blast" finds Affected); the select preset hides the (empty) preview.
  local items = {}
  for _, e in ipairs(entries) do
    table.insert(items, { text = ("%-15s  %s"):format(e.label, e.desc), _run = e.run })
  end
  Snacks.picker.pick({
    title = "TyO3",
    items = items,
    format = "text",
    layout = { preset = "select" },
    confirm = function(picker, item)
      picker:close()
      if item and item._run then
        vim.schedule(item._run)
      end
    end,
  })
end

--- Bind the buffer-local hub keymap for *bufnr* from `config.keymaps.hub`.
--- Idempotent (buffer-local). A string rebinds; `false`/`nil`/`""` opts out.
function M.bind_keymap(bufnr)
  local keymaps = require("tyo3.config").get().keymaps or {}
  local key = keymaps.hub
  if key == false or key == nil or key == "" then
    return
  end
  vim.keymap.set("n", key, M.open, { buffer = bufnr, desc = "tyo3: open the hub (views + ops)" })
end

return M
