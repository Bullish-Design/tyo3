-- tyo3.nvim — Telescope pickers (navigate by durable identity).
--
-- `entities` (everything decorated so far), `affected` (the last edit's closure),
-- and `authored` (notes). Selecting an item resolves its current location via
-- `locate {id}` and jumps there — navigation by identity, not by line.
--
-- Telescope is an optional dependency: every picker degrades to a clear notice
-- (or, where possible, a `vim.ui.select` fallback) when it is not installed.

local M = {}

local function has_telescope()
  return pcall(require, "telescope")
end

local function jump_to(bufnr, durable_id)
  require("tyo3").rpc(bufnr, "locate", { durable_id = durable_id }, function(err, res)
    if err or not res or not res.location or res.location == vim.NIL then
      vim.notify("[tyo3] could not locate entity", vim.log.levels.WARN)
      return
    end
    -- location is "file::qualified_name"; jump to the file then search the name.
    local loc = res.location
    local file = loc:match("^(.-)::") or loc
    local root = require("tyo3").root_for_buf(bufnr)
    local abs = file
    if root and file:sub(1, 1) ~= "/" then
      abs = root:gsub("/$", "") .. "/" .. file
    end
    vim.cmd("edit " .. vim.fn.fnameescape(abs))
    local name = loc:match("::([^:]+)$")
    if name then
      local bare = name:match("([^.]+)$") or name
      vim.fn.search("\\<" .. vim.fn.escape(bare, "\\") .. "\\>", "w")
    end
  end)
end

-- Generic vim.ui.select fallback when telescope is absent.
local function select_fallback(title, entries)
  if #entries == 0 then
    vim.notify("[tyo3] nothing to show: " .. title, vim.log.levels.INFO)
    return
  end
  vim.ui.select(entries, {
    prompt = title,
    format_item = function(e)
      return e.label
    end,
  }, function(choice)
    if choice then
      jump_to(vim.api.nvim_get_current_buf(), choice.durable_id)
    end
  end)
end

local function run_picker(title, entries)
  if not has_telescope() then
    select_fallback(title, entries)
    return
  end
  local pickers = require("telescope.pickers")
  local finders = require("telescope.finders")
  local conf = require("telescope.config").values
  local actions = require("telescope.actions")
  local action_state = require("telescope.actions.state")
  local bufnr = vim.api.nvim_get_current_buf()

  pickers
    .new({}, {
      prompt_title = title,
      finder = finders.new_table({
        results = entries,
        entry_maker = function(e)
          return { value = e, display = e.label, ordinal = e.label }
        end,
      }),
      sorter = conf.generic_sorter({}),
      attach_mappings = function(prompt_bufnr)
        actions.select_default:replace(function()
          local sel = action_state.get_selected_entry()
          actions.close(prompt_bufnr)
          if sel then
            jump_to(bufnr, sel.value.durable_id)
          end
        end)
        return true
      end,
    })
    :find()
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

--- Entities that carry an authored note (scanned from the name cache).
function M.authored()
  local bufnr = vim.api.nvim_get_current_buf()
  local decorate = require("tyo3.decorate")
  local ids = {}
  for id in pairs(decorate.name_cache) do
    table.insert(ids, id)
  end
  if #ids == 0 then
    vim.notify("[tyo3] no entities seen yet — open some files first", vim.log.levels.INFO)
    return
  end
  -- Probe each id's intent layer; collect those with a present note.
  local entries = {}
  local pending = #ids
  for _, id in ipairs(ids) do
    require("tyo3").rpc(bufnr, "authored", { layer = "intent", durable_id = id }, function(err, av)
      if not err and av and av.status ~= "absent" and av.value ~= nil and av.value ~= vim.NIL then
        local note = type(av.value) == "table" and av.value.note or av.value
        local rec = decorate.name_cache[id]
        table.insert(entries, {
          durable_id = id,
          label = ("%s — %s"):format(rec and (rec.qualified_name or rec.name) or id:sub(-8), tostring(note)),
        })
      end
      pending = pending - 1
      if pending == 0 then
        run_picker("TyO3 Authored notes", entries)
      end
    end)
  end
end

return M
