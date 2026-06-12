-- tyo3.nvim — sidebar section renderers (pure card → string[]).
--
-- Lifted from panel.lua (proj 28 Phase E). Each renderer takes a card (or
-- internal state) and returns a flat list of display lines for its buffer.
-- The sidebar is observe-only (read-only knowledge surface), so the meta/
-- interaction bits (toggle, activate, section_doc) stay in panel.lua.
-- card.build_lines serves as the IDENTITY renderer (see card.lua).

local M = {}

-- Word-wrap *text* to *width* columns → a list of lines.
local function wrap(text, width)
  local out, line = {}, ""
  for word in tostring(text):gmatch("%S+") do
    if #line + #word + 1 > width and #line > 0 then
      table.insert(out, line)
      line = word
    else
      line = (#line == 0) and word or (line .. " " .. word)
    end
  end
  if #line > 0 then
    table.insert(out, line)
  end
  return out
end

--- IDENTITY — the entity's identity (name, kind, durable id, location, hash).
--- Reuses card.build_lines; authored/derived layers live in NOTES / SUMMARY.
function M.identity_rows(card)
  return require("tyo3.card").build_lines(card)
end

--- NOTES — authored intent layers (note, explain, etc.) + needs_review ⚠.
function M.note_rows(card)
  local rows = {}
  for layer, rec in pairs(card.authored or {}) do
    if layer ~= "docs" and rec.status ~= "absent" then
      local val = rec.value
      local flag = rec.status == "needs_review" and " ⚠" or ""
      if type(val) == "table" and val.text then
        -- LLM-derived explanation (the `explain` layer): a 🤖-marked, wrapped
        -- mini-paragraph glued to the entity.
        local first = true
        for _, l in ipairs(wrap(val.text, 46)) do
          table.insert(rows, (first and "🤖 " or "   ") .. l)
          first = false
        end
        if flag ~= "" and #rows > 0 then
          rows[#rows] = rows[#rows] .. flag
        end
      else
        if type(val) == "table" and val.note then
          val = val.note
        elseif type(val) == "table" then
          val = vim.json.encode(val)
        end
        table.insert(rows, "🏷 " .. tostring(val) .. flag)
      end
    end
  end
  if #rows == 0 then
    table.insert(rows, "(none)")
  end
  return rows
end

--- DOCS — the entity's authored markdown doc + reference links to the tool's
--- static guides.
function M.doc_rows(card)
  local rows = {}
  local md = require("tyo3.entitydoc").markdown_of(card)
  if md then
    local first = vim.split(md, "\n", { plain = true })[1] or ""
    first = first:gsub("^#+%s*", "")
    table.insert(rows, "📄 " .. (first ~= "" and first or "doc"))
  else
    table.insert(rows, "📄 (no doc)")
  end
  table.insert(rows, "reference:")
  for _, e in ipairs(require("tyo3.docs").entries) do
    table.insert(rows, "  • " .. e.title)
  end
  return rows
end

--- SUMMARY — derived artifacts (auto summaries, embeddings, …).
function M.summary_rows(card)
  local rows = {}
  for layer, rec in pairs(card.derived or {}) do
    if rec.status ~= "absent" and rec.status ~= "failed" then
      local art = rec.artifact
      if art ~= nil and art ~= vim.NIL then
        table.insert(rows, "⟢ " .. layer .. ": " .. tostring(art))
      end
    end
  end
  if #rows == 0 then
    table.insert(rows, "(none)")
  end
  return rows
end

--- AFFECTED — the blast-radius log lines, already formatted by on_delta /
--- on_refinement.
function M.affected_rows(lines)
  return lines or {}
end

return M
