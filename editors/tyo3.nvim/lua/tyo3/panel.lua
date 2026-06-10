-- tyo3.nvim — the side dock: collapsible, data-type-separated panes.
--
-- The dock no longer renders one blob. Each kind of data hanging off the code
-- spine gets its own collapsible pane, all driven by the entity under the cursor
-- (when CONTEXT tracking is on) plus the edit log:
--
--   IDENTITY  the durable entity: name · kind, id, location
--   NOTES     authored intent (notes), keyed by identity
--   DOCS      the entity's authored markdown doc + links to the tool's guides
--   SUMMARY   derived artifacts (auto summaries / embeddings)
--   ACTIONS   the tools you can run right now, in this context
--   AFFECTED  the id-level blast radius of the last edit, then its refinement
--
-- Each pane header toggles with <Tab>; <CR> activates a line (toggle a header,
-- run an action, open/edit a doc); `gd` opens the static doc for a pane.

local decorate = require("tyo3.decorate")

local M = {}

M.win = nil
M.buf = nil
M._entity = nil -- the current entity card (or nil)
M._source_buf = nil -- the code buffer the card came from (for RPC routing)
M._affected_lines = {} -- AFFECTED history
M._rev_index = {} -- revision -> index into _affected_lines
M.last_affected_ids = {} -- ids of the most recent non-empty delta (for pickers)
M._collapsed = {} -- section name -> true when collapsed
M._line_meta = {} -- 1-based lnum -> { section, action, payload }

local MAX_LINES = 200

local render -- forward declaration (defined below)

local function short_id(id)
  if id and #id > 10 then
    return "…" .. id:sub(-8)
  end
  return id or "?"
end

local function names_of(ids, limit)
  local out = {}
  for _, id in ipairs(ids) do
    local nm = decorate.name_for(id)
    table.insert(out, nm or ("…" .. id:sub(-6)))
    if #out >= limit then
      table.insert(out, "…")
      break
    end
  end
  return out
end

local function ensure_buf()
  if M.buf and vim.api.nvim_buf_is_valid(M.buf) then
    return M.buf
  end
  local buf = vim.api.nvim_create_buf(false, true)
  vim.api.nvim_buf_set_name(buf, "TyO3://panel")
  vim.bo[buf].buftype = "nofile"
  vim.bo[buf].bufhidden = "hide"
  vim.bo[buf].swapfile = false
  vim.bo[buf].filetype = "tyo3panel"
  -- Buffer-local interaction.
  vim.keymap.set("n", "<Tab>", function() M.toggle_at() end, { buffer = buf, nowait = true, desc = "TyO3: collapse/expand pane" })
  vim.keymap.set("n", "<CR>", function() M.activate_at() end, { buffer = buf, nowait = true, desc = "TyO3: activate line" })
  vim.keymap.set("n", "za", function() M.toggle_at() end, { buffer = buf, nowait = true })
  vim.keymap.set("n", "gd", function() M.open_section_doc() end, { buffer = buf, nowait = true, desc = "TyO3: open pane doc" })
  M.buf = buf
  return buf
end

function M.is_open()
  return M.win ~= nil and vim.api.nvim_win_is_valid(M.win)
end

function M.open()
  local buf = ensure_buf()
  if M.is_open() then
    return
  end
  local cur = vim.api.nvim_get_current_win()
  vim.cmd("botright vsplit")
  M.win = vim.api.nvim_get_current_win()
  vim.api.nvim_win_set_buf(M.win, buf)
  vim.api.nvim_win_set_width(M.win, 44)
  vim.wo[M.win].number = false
  vim.wo[M.win].relativenumber = false
  vim.wo[M.win].wrap = true
  vim.wo[M.win].winfixwidth = true
  vim.api.nvim_set_current_win(cur)
end

function M.close()
  if M.is_open() then
    vim.api.nvim_win_close(M.win, true)
  end
  M.win = nil
end

function M.toggle()
  if M.is_open() then
    M.close()
  else
    M.open()
    render()
  end
end

-- ── Rendering ────────────────────────────────────────────────────────────────

-- Section body builders. Each returns a list of { text, meta? } rows.
local function identity_rows(card)
  local rows = {}
  local where = card.file
  if where and card.qualified_name then
    where = where .. "::" .. card.qualified_name
  end
  table.insert(rows, { text = ("  %s · %s"):format(card.qualified_name or card.name or "<entity>", card.kind or "?") })
  if card.durable_id then
    table.insert(rows, { text = "  id " .. short_id(card.durable_id) })
  end
  if where or card.location then
    table.insert(rows, { text = "  ▪ " .. (where or card.location) })
  end
  return rows
end

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

local function note_rows(card)
  local rows = {}
  for layer, rec in pairs(card.authored or {}) do
    if layer ~= "docs" and rec.status ~= "absent" then
      local val = rec.value
      local flag = rec.status == "needs_review" and " ⚠" or ""
      if type(val) == "table" and val.text then
        -- LLM-derived explanation (the `explain` layer): a 🤖-marked, wrapped
        -- mini-paragraph glued to the entity. Rides edits/moves; the ⚠ rides
        -- the last line when the body has drifted from when it was generated.
        local first = true
        for _, l in ipairs(wrap(val.text, 44)) do
          table.insert(rows, { text = (first and "  🤖 " or "     ") .. l })
          first = false
        end
        if flag ~= "" and #rows > 0 then
          rows[#rows].text = rows[#rows].text .. flag
        end
      else
        if type(val) == "table" and val.note then
          val = val.note
        elseif type(val) == "table" then
          val = vim.json.encode(val)
        end
        table.insert(rows, { text = "  🏷 " .. tostring(val) .. flag })
      end
    end
  end
  if #rows == 0 then
    table.insert(rows, { text = "  (none — see ACTIONS)" })
  end
  return rows
end

local function doc_rows(card)
  local rows = {}
  local md = require("tyo3.entitydoc").markdown_of(card)
  if md then
    local first = vim.split(md, "\n", { plain = true })[1] or ""
    first = first:gsub("^#+%s*", "")
    table.insert(rows, { text = "  📄 " .. (first ~= "" and first or "doc"), meta = { action = "edit_doc" } })
  else
    table.insert(rows, { text = "  📄 (no doc — <CR> to write)", meta = { action = "edit_doc" } })
  end
  table.insert(rows, { text = "  reference:" })
  for _, e in ipairs(require("tyo3.docs").entries) do
    table.insert(rows, { text = "    • " .. e.title, meta = { action = "open_doc", payload = e.id } })
  end
  return rows
end

local function summary_rows(card)
  local rows = {}
  for layer, rec in pairs(card.derived or {}) do
    if rec.status ~= "absent" and rec.status ~= "failed" then
      local art = rec.artifact
      if art ~= nil and art ~= vim.NIL then
        table.insert(rows, { text = ("  ⟢ %s: %s"):format(layer, tostring(art)) })
      end
    end
  end
  if #rows == 0 then
    table.insert(rows, { text = "  (none)" })
  end
  return rows
end

local function action_rows(card)
  local rows = {}
  for _, a in ipairs(require("tyo3.actions").list(card)) do
    table.insert(rows, { text = "  " .. a.label, meta = { action = "run_action", payload = a } })
  end
  return rows
end

local function count_of(card, section)
  if not card then
    return nil
  end
  if section == "NOTES" then
    local n = 0
    for layer, rec in pairs(card.authored or {}) do
      if layer ~= "docs" and rec.status ~= "absent" then
        n = n + 1
      end
    end
    return n
  elseif section == "SUMMARY" then
    local n = 0
    for _, rec in pairs(card.derived or {}) do
      if rec.status ~= "absent" and rec.status ~= "failed" and rec.artifact ~= nil and rec.artifact ~= vim.NIL then
        n = n + 1
      end
    end
    return n
  end
  return nil
end

-- Compose the buffer + line metadata from the current entity and affected log.
render = function()
  if not (M.buf and vim.api.nvim_buf_is_valid(M.buf)) then
    return
  end
  local out, meta = {}, {}
  local function emit(text, m)
    table.insert(out, text)
    meta[#out] = m
  end

  local function header(section, count)
    local marker = M._collapsed[section] and "▸" or "▾"
    local label = ("%s %s"):format(marker, section)
    if count ~= nil then
      label = label .. ("  (%d)"):format(count)
    end
    emit(label, { section = section, action = "toggle" })
  end

  local function pane(section, rows)
    header(section, count_of(M._entity, section))
    if not M._collapsed[section] then
      for _, row in ipairs(rows) do
        local m = row.meta or {}
        m.section = section
        emit(row.text, m)
      end
    end
  end

  if M._entity then
    pane("IDENTITY", identity_rows(M._entity))
    pane("NOTES", note_rows(M._entity))
    pane("DOCS", doc_rows(M._entity))
    pane("SUMMARY", summary_rows(M._entity))
    pane("ACTIONS", action_rows(M._entity))
  end

  if #M._affected_lines > 0 then
    header("AFFECTED", #M._affected_lines)
    if not M._collapsed["AFFECTED"] then
      for _, l in ipairs(M._affected_lines) do
        emit(l, { section = "AFFECTED" })
      end
    end
  end

  M._line_meta = meta
  vim.bo[M.buf].modifiable = true
  vim.api.nvim_buf_set_lines(M.buf, 0, -1, false, out)
  vim.bo[M.buf].modifiable = false
end

-- ── Interaction ───────────────────────────────────────────────────────────────

local function cursor_lnum()
  if not M.is_open() then
    return nil
  end
  return vim.api.nvim_win_get_cursor(M.win)[1]
end

--- Collapse/expand the pane the cursor is in.
function M.toggle_at()
  local lnum = cursor_lnum()
  local m = lnum and M._line_meta[lnum]
  if not m or not m.section then
    return
  end
  M._collapsed[m.section] = not M._collapsed[m.section]
  render()
  pcall(vim.api.nvim_win_set_cursor, M.win, { math.min(lnum, math.max(vim.api.nvim_buf_line_count(M.buf), 1)), 0 })
end

--- Activate the line under the cursor.
function M.activate_at()
  local lnum = cursor_lnum()
  local m = lnum and M._line_meta[lnum]
  if not m then
    return
  end
  if m.action == "toggle" then
    M.toggle_at()
  elseif m.action == "edit_doc" then
    require("tyo3.entitydoc").edit_card(M._entity, M._source_buf)
  elseif m.action == "open_doc" then
    require("tyo3.docs").open(m.payload)
  elseif m.action == "run_action" and m.payload and m.payload.run then
    m.payload.run(M._entity, M._source_buf)
  end
end

--- Open the static doc associated with the pane the cursor is in.
function M.open_section_doc()
  local lnum = cursor_lnum()
  local m = lnum and M._line_meta[lnum]
  local id = m and m.section and require("tyo3.docs").section_doc[m.section]
  if id then
    require("tyo3.docs").open(id)
  end
end

-- ── CONTEXT (entity) updates ───────────────────────────────────────────────────

--- Show the entity card under the cursor; *src_buf* is the code buffer it came
--- from (so panel actions can route RPCs there). nil/NIL card clears it.
function M.set_context(card, src_buf)
  if card == nil or card == vim.NIL then
    M._entity = nil
  else
    M._entity = card
    M._source_buf = src_buf or M._source_buf
    -- Discover authored layers once (QW3) so the ACTIONS author menu reflects
    -- config instead of a hardcoded "intent" entry; re-render when it lands.
    local actions = require("tyo3.actions")
    if actions._authored_layers == nil and M._source_buf then
      actions.refresh_layers(M._source_buf, function()
        render()
      end)
    end
  end
  render()
  if require("tyo3.config").get().context == "cursor" and not M.is_open() then
    M.open()
    render()
  end
end

function M.clear_context()
  M._entity = nil
  render()
end

--- Re-resolve the shown entity (e.g. after authoring a note/doc) and re-render.
function M.reload()
  if not (M._entity and M._source_buf and vim.api.nvim_buf_is_valid(M._source_buf)) then
    return
  end
  local r = M._entity.range and M._entity.range.start
  local path = vim.api.nvim_buf_get_name(M._source_buf)
  if not r or path == "" then
    return
  end
  require("tyo3").rpc(M._source_buf, "entity_at", { path = path, line = r.line, col = r.column }, function(err, card)
    if not err and card and card ~= vim.NIL then
      M.set_context(card, M._source_buf)
    end
  end)
end

-- ── AFFECTED log (delta / refinement notifications) ────────────────────────────

local function append(line)
  table.insert(M._affected_lines, line)
  if #M._affected_lines > MAX_LINES then
    table.remove(M._affected_lines, 1)
    M._rev_index = {}
  end
end

--- Handle a `delta` notification.
function M.on_delta(_root, params)
  local cfg = require("tyo3.config").get()
  if cfg.panel == "off" then
    return
  end
  local changed = params.changed_ids or {}
  local affected = params.affected_ids or {}
  if #changed == 0 and #affected == 0 and not params.rescan then
    return
  end
  if params.rescan then
    append(("rev %d · rescan"):format(params.revision))
  else
    local affected_names = names_of(affected, 6)
    append(("rev %d · Δ%d · affects {%s}"):format(params.revision, #changed, table.concat(affected_names, ", ")))
    if #affected > 0 then
      M.last_affected_ids = affected
    end
  end
  M._rev_index[params.revision] = #M._affected_lines
  render()
  if cfg.panel == "auto" and not M.is_open() then
    M.open()
    render()
  end
end

--- Handle a `derived` notification (AB3): a `serving="stale"` value the daemon
--- produced off the actor is now fresh. If the panel is showing that entity,
--- re-pull its card so the stale value is replaced by the fresh one.
function M.on_derived(_root, params)
  if not M.is_open() then
    return
  end
  if M._entity and params.durable_id and M._entity.durable_id ~= params.durable_id then
    return
  end
  M.reload()
end

--- Handle a `refinement` notification: annotate the matching revision's line.
function M.on_refinement(_root, params)
  local idx = M._rev_index[params.revision]
  if not idx or not M._affected_lines[idx] then
    return
  end
  local narrowed = names_of(params.narrowed or {}, 6)
  M._affected_lines[idx] = M._affected_lines[idx]
    .. ("  → narrowed {%s}"):format(table.concat(narrowed, ", "))
  render()
end

return M
