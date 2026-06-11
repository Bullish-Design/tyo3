-- tyo3.nvim — card → lines formatter.
--
-- The daemon's `entity_at` returns a cross-layer "card" for the entity under the
-- cursor. `build_lines` renders the full card for the `:TyO3Inspect` float. (The
-- panel's CONTEXT section builds its own compact rows in panel.lua.)

local M = {}

local function short_id(id)
  if #id > 10 then
    return "…" .. id:sub(-8)
  end
  return id
end

-- Collapse an authored record's value down to a display string.
local function authored_value(rec)
  local val = rec.value
  if type(val) == "table" and val.note then
    return tostring(val.note)
  elseif type(val) == "table" then
    return vim.json.encode(val)
  end
  return tostring(val)
end

-- Collapse a derived record's artifact down to a display string, or nil.
local function derived_artifact(rec)
  local art = rec.artifact
  if art == nil or art == vim.NIL then
    return nil
  end
  return tostring(art)
end

--- Full card → lines, for the :TyO3Inspect float.
function M.build_lines(card)
  local lines = {}
  local function add(s)
    table.insert(lines, s)
  end
  add("  " .. (card.qualified_name or card.name or "<entity>"))
  add("  " .. ("─"):rep(40))
  add("  kind        " .. (card.kind or "?"))
  add("  durable id  " .. short_id(card.durable_id))
  if card.location then
    add("  location    " .. card.location)
  end
  if card.content_hash then
    add("  hash        " .. card.content_hash:sub(1, 12))
  end
  if card.last_affected_revision ~= nil and card.last_affected_revision ~= vim.NIL then
    add("  affected@   rev " .. tostring(card.last_affected_revision))
  end

  local authored = card.authored or {}
  if not vim.tbl_isempty(authored) then
    add("")
    add("  authored")
    for layer, rec in pairs(authored) do
      add(("    %s: %s  [%s]"):format(layer, authored_value(rec), rec.status))
    end
  end

  local derived = card.derived or {}
  if not vim.tbl_isempty(derived) then
    add("")
    add("  derived")
    for layer, rec in pairs(derived) do
      add(("    %s: %s  [%s]"):format(layer, derived_artifact(rec) or "—", rec.status))
    end
  end
  return lines
end

return M
