-- tyo3.nvim — pickers (navigate by durable identity), over Snacks.picker.
--
-- `entities` (everything decorated so far), `affected` (the last edit's closure),
-- and `authored` (notes). Selecting an item resolves its current location via
-- `locate {id}` and jumps there — navigation by identity, not by line.
--
-- Single path (proj 28): snacks is a managed hard dep (see deps.lua), so there
-- is no telescope / `vim.ui.select` fallback. A missing snacks surfaces as a
-- `:checkhealth tyo3` error rather than a silent downgrade.

local M = {}

-- Search the located file for the entity's bare name (the fallback when a
-- precise range isn't available).
local function search_name(loc)
  local name = loc:match("::([^:]+)$")
  if name then
    local bare = name:match("([^.]+)$") or name
    vim.fn.search("\\<" .. vim.fn.escape(bare, "\\") .. "\\>", "w")
  end
end

-- Jump to *durable_id*'s current location, precisely. Resolves the file by
-- identity (`locate`), then re-reads the id's *fresh* range via `decorate` so we
-- land on it exactly even after it moved — falling back to a name search only
-- when the id isn't in the decorate batch. (Preserved verbatim from proj 27.)
local function jump_to(bufnr, durable_id)
  require("tyo3").rpc(bufnr, "locate", { durable_id = durable_id }, function(err, res)
    if err or not res or not res.location or res.location == vim.NIL then
      vim.notify("[tyo3] could not locate entity", vim.log.levels.WARN)
      return
    end
    -- location is "file::qualified_name"; jump to the file by identity.
    local loc = res.location
    local file = loc:match("^(.-)::") or loc
    local root = require("tyo3").root_for_buf(bufnr)
    local abs = file
    if root and file:sub(1, 1) ~= "/" then
      abs = root:gsub("/$", "") .. "/" .. file
    end
    vim.cmd("edit " .. vim.fn.fnameescape(abs))
    -- Decorate the located file to get the id's *current* range and jump
    -- precisely to it, instead of a name search that can land on the wrong
    -- same-named symbol. The decorate batch is fresh, so this is correct even
    -- after the entity moved. Fall back to the name search if the id isn't found.
    local jbuf = vim.api.nvim_get_current_buf()
    require("tyo3").rpc(jbuf, "decorate", { path = abs }, function(derr, items)
      local placed = false
      if not derr and type(items) == "table" then
        for _, it in ipairs(items) do
          if it.durable_id == durable_id and it.range and it.range.start then
            local row = it.range.start.line
            local col = math.max((it.range.start.column or 1) - 1, 0)
            placed = pcall(vim.api.nvim_win_set_cursor, 0, { row, col })
            break
          end
        end
      end
      if not placed then
        search_name(loc)
      end
    end)
  end)
end

-- Open a Snacks.picker over *entries* ({ durable_id, label }); confirming jumps
-- to the selected entity by identity. `item.text` is the searchable string snacks
-- filters on; the "text" formatter renders it.
local function run_picker(title, entries)
  if #entries == 0 then
    vim.notify("[tyo3] nothing to show: " .. title, vim.log.levels.INFO)
    return
  end
  local ok, Snacks = pcall(require, "snacks")
  if not ok or not Snacks.picker then
    vim.notify("[tyo3] snacks.nvim is required for pickers (see :checkhealth tyo3)", vim.log.levels.ERROR)
    return
  end
  local bufnr = vim.api.nvim_get_current_buf()
  local items = {}
  for _, e in ipairs(entries) do
    table.insert(items, { text = e.label, durable_id = e.durable_id })
  end
  Snacks.picker.pick({
    title = title,
    items = items,
    format = "text",
    -- These are identity entries (durable_id + label), not files/positions, so
    -- the default file preview has nothing to show. The "select" layout hides
    -- the preview window entirely; jumping happens on confirm via the
    -- identity-resolving jump_to.
    layout = { preset = "select" },
    confirm = function(picker, item)
      picker:close()
      if item and item.durable_id then
        jump_to(bufnr, item.durable_id)
      end
    end,
  })
end

-- Build entries from the decorate name cache (everything seen so far).
local function entity_entries()
  local decorate = require("tyo3.decorate")
  local entries = {}
  for id, rec in pairs(decorate.name_cache) do
    table.insert(entries, {
      durable_id = id,
      label = ("%s  [%s]  %s"):format(rec.qualified_name or rec.name, rec.kind or "?", rec.file or ""),
    })
  end
  table.sort(entries, function(a, b)
    return a.label < b.label
  end)
  return entries
end

--- All decorated entities.
function M.entities()
  run_picker("TyO3 Entities", entity_entries())
end

--- The affected set of the most recent edit (from the panel's last delta).
function M.affected()
  local panel = require("tyo3.panel")
  local ids = panel.last_affected_ids or {}
  local decorate = require("tyo3.decorate")
  local entries = {}
  for _, id in ipairs(ids) do
    local rec = decorate.name_cache[id]
    table.insert(entries, {
      durable_id = id,
      label = rec and (rec.qualified_name or rec.name) or ("…" .. id:sub(-8)),
    })
  end
  run_picker("TyO3 Affected (last edit)", entries)
end

--- Entities that carry a note in *layer* (default ``intent``).
---
--- One `layer_ids` call returns exactly the ids with a record plus their values
--- (QW7) — O(1) actor hops, versus the old per-id `authored` probe over every
--- decorated entity.
function M.authored(layer)
  layer = layer or "intent"
  local bufnr = vim.api.nvim_get_current_buf()
  local decorate = require("tyo3.decorate")
  require("tyo3").rpc(bufnr, "layer_ids", { layer = layer, with_values = true }, function(err, res)
    if err or not res then
      vim.notify("[tyo3] could not list " .. layer .. " notes: " .. (err and err.message or "error"), vim.log.levels.WARN)
      return
    end
    local recs = res.ids or {}
    local values = res.values or {}
    if #recs == 0 then
      vim.notify("[tyo3] no " .. layer .. " notes yet", vim.log.levels.INFO)
      return
    end
    local entries = {}
    for _, id in ipairs(recs) do
      local v = values[id]
      if v == vim.NIL then
        v = nil
      end
      local note = type(v) == "table" and v.note or v
      local rec = decorate.name_cache[id]
      table.insert(entries, {
        durable_id = id,
        label = ("%s — %s"):format(rec and (rec.qualified_name or rec.name) or id:sub(-8), tostring(note)),
      })
    end
    run_picker("TyO3 " .. layer .. " notes", entries)
  end)
end

return M
