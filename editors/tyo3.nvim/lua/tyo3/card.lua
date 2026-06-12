-- tyo3.nvim — card → IDENTITY lines formatter.
--
-- The daemon's `entity_at` returns a cross-layer "card" for the entity under the
-- cursor. `build_lines` renders the *identity* of that card (name, kind, durable
-- id, location, hash, last-affected revision) for the sidebar's IDENTITY view.
-- The authored/derived layers are deliberately omitted here — they have their
-- own NOTES / SUMMARY views, so duplicating them in IDENTITY would be redundant.

local M = {}

local function short_id(id)
  if #id > 10 then
    return "…" .. id:sub(-8)
  end
  return id
end

--- Card → identity lines, for the sidebar IDENTITY view.
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
  return lines
end

return M
