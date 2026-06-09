-- tyo3.nvim — card → lines formatters.
--
-- The daemon's `entity_at` returns a cross-layer "card" for the entity under the
-- cursor. Two consumers render it: the :TyO3Inspect float (full) and the panel's
-- CONTEXT section (compact). Both formatters live here so they stay consistent.

local M = {}

local function short_id(id)
  if #id > 10 then
    return "…" .. id:sub(-8)
  end
  return id
end

-- Truncate *s* to at most *width* display columns (char-wise, ellipsised).
local function truncate(s, width)
  if not width or vim.fn.strdisplaywidth(s) <= width then
    return s
  end
  return vim.fn.strcharpart(s, 0, math.max(width - 1, 1)) .. "…"
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

--- Compact card → lines, for the panel CONTEXT section. Lines are truncated to
--- *width* display columns. A nil / vim.NIL card renders a "no entity" line so
--- the section header still has a body.
function M.context_lines(card, width)
  if card == nil or card == vim.NIL then
    return { "  (no entity under cursor)" }
  end
  local lines = {}
  local function add(s)
    table.insert(lines, truncate(s, width))
  end

  local name = card.qualified_name or card.name or "<entity>"
  add(("  %s · %s"):format(name, card.kind or "?"))

  -- The durable id + location: the id stays constant across a move while the
  -- location follows the entity to its new file — identity made visible. Prefer
  -- the basename `file::qualified_name` (the daemon's `location` is an absolute
  -- path that would just truncate to noise in the narrow dock).
  if card.durable_id then
    add("  id " .. short_id(card.durable_id))
  end
  local where = card.file
  if where and card.qualified_name then
    where = where .. "::" .. card.qualified_name
  end
  where = where or card.location
  if where then
    add("  ▪ " .. where)
  end

  -- Every authored layer with a live record (notes etc.), not just `intent`.
  -- Authored statuses are present|needs_review|orphaned|absent — render all but
  -- `absent`, flagging needs_review so honest staleness is visible.
  for _, rec in pairs(card.authored or {}) do
    if rec.status ~= "absent" then
      local line = "  🏷 " .. authored_value(rec)
      if rec.status == "needs_review" then
        line = line .. " ⚠"
      end
      add(line)
    end
  end

  -- Every derived layer with an artifact (summary etc.), not just `summary`.
  -- Derived statuses are fresh|stale|failed|absent — a fresh/stale artifact is
  -- real and must render; only absent/failed are dropped.
  for _, rec in pairs(card.derived or {}) do
    if rec.status ~= "absent" and rec.status ~= "failed" then
      local art = derived_artifact(rec)
      if art then
        add("  ⟢ " .. art)
      end
    end
  end

  if card.last_affected_revision ~= nil and card.last_affected_revision ~= vim.NIL then
    add("  affected@ rev " .. tostring(card.last_affected_revision))
  end
  return lines
end

return M
