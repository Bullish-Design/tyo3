-- tyo3.nvim — edgy-managed sidebar (proj 28 Phase E).
--
-- Replaces panel.lua's hand-rolled split + monolithic render with edgy-managed,
-- vertically stacked, one-category-per-view section windows (IDENTITY / NOTES /
-- DOCS / SUMMARY / AFFECTED), fed by the *same* events the panel consumed, with
-- a content-driven accordion (the relevant section expands, the rest collapse to
-- title height).
--
-- Public surface (panel-compatible): set_context, clear_context, reload,
-- on_delta, on_derived, on_refinement, toggle/open/close, last_affected_ids.

local render = require("tyo3.sidebar.render")
local decorate = require("tyo3.decorate")

local M = {}

M._did_setup = false
M._entity = nil -- the current entity card (or nil)
M._source_buf = nil -- the code buffer the card came from (for RPC routing)
M._affected_lines = {} -- AFFECTED history
M._rev_index = {} -- revision -> index into _affected_lines
M._rev_narrowed = {} -- revision -> true once its line is annotated (dedupe)
M.last_affected_ids = {} -- ids of the most recent non-empty delta (for pickers)

M.bufs = {} -- ft -> bufnr (e.g. tyo3_identity -> bufnr)
M._edgy = nil -- the edgy module (stashed for the accordion)

local MAX_LINES = 200

-- ── Helpers ─────────────────────────────────────────────────────────────────

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

-- ── Buffer lifecycle ────────────────────────────────────────────────────────

local VIEW_FTS = { "tyo3_identity", "tyo3_notes", "tyo3_docs", "tyo3_summary", "tyo3_affected" }
local VIEW_TITLES = {
  tyo3_identity = "IDENTITY",
  tyo3_notes = "NOTES",
  tyo3_docs = "DOCS",
  tyo3_summary = "SUMMARY",
  tyo3_affected = "AFFECTED",
}

--- Create one persistent scratch buffer per view.
local function create_buffers()
  for _, ft in ipairs(VIEW_FTS) do
    if not (M.bufs[ft] and vim.api.nvim_buf_is_valid(M.bufs[ft])) then
      local buf = vim.api.nvim_create_buf(false, true)
      vim.api.nvim_buf_set_name(buf, "TyO3://" .. VIEW_TITLES[ft])
      vim.bo[buf].buftype = "nofile"
      vim.bo[buf].bufhidden = "hide"
      vim.bo[buf].swapfile = false
      vim.bo[buf].filetype = ft
      M.bufs[ft] = buf
    end
  end
end

--- Write *lines* into the buffer for *ft*.
local function set_buffer_lines(ft, lines)
  local buf = M.bufs[ft]
  if not buf or not vim.api.nvim_buf_is_valid(buf) then
    return
  end
  pcall(vim.api.nvim_buf_set_lines, buf, 0, -1, false, lines)
end

--- Clear all section buffers (used on clear_context).
local function clear_all_buffers()
  for _, ft in ipairs(VIEW_FTS) do
    local buf = M.bufs[ft]
    if buf and vim.api.nvim_buf_is_valid(buf) then
      pcall(vim.api.nvim_buf_set_lines, buf, 0, -1, false, {})
    end
  end
end

-- ── Accordion ───────────────────────────────────────────────────────────────

--- Expand or collapse one edgy view by ft.
local function set_view_visible(ft, visible)
  if not M._edgy then
    return
  end
  local ok, layout = pcall(function()
    return require("edgy.config").layout
  end)
  if not ok or not layout then
    return
  end
  local bar = layout["right"]
  if not bar or not bar.views then
    return
  end
  for _, view in ipairs(bar.views) do
    if view.ft == ft then
      local win = view.wins[1] or view.pinned_win
      if win then
        win:show(visible)
      end
      break
    end
  end
end

--- Content-accordion: expand sections that have data for the current entity,
--- collapse the empty ones. IDENTITY always expands when we have an entity.
local function content_accordion()
  if not M._entity or not M._edgy then
    return
  end
  local buf = M.bufs["tyo3_identity"]
  local ident_nonempty = buf and vim.api.nvim_buf_is_valid(buf)
    and vim.api.nvim_buf_line_count(buf) > 0
  local function is_nonempty(ft)
    local b = M.bufs[ft]
    return b and vim.api.nvim_buf_is_valid(b) and vim.api.nvim_buf_line_count(b) > 0
  end

  set_view_visible("tyo3_identity", true) -- always expand when we have an entity
  set_view_visible("tyo3_notes", is_nonempty("tyo3_notes"))
  set_view_visible("tyo3_docs", is_nonempty("tyo3_docs"))
  set_view_visible("tyo3_summary", is_nonempty("tyo3_summary"))
  set_view_visible("tyo3_affected", is_nonempty("tyo3_affected"))

  pcall(function()
    require("edgy.layout").update()
  end)
end

--- Focus-accordion (WinEnter): when the user enters one of our tyo3_* views,
--- expand it and collapse the other tyo3_* views.
local function focus_accordion(win)
  if not M._edgy or not M.bufs then
    return
  end
  local buf = vim.api.nvim_win_get_buf(win)
  local ft = vim.bo[buf].filetype
  if not ft or not vim.tbl_contains(VIEW_FTS, ft) then
    return
  end
  for _, vft in ipairs(VIEW_FTS) do
    set_view_visible(vft, vft == ft)
  end
  require("edgy.layout").update()
end

-- ── Edgy view registration ─────────────────────────────────────────────────

--- Build the edgy right-edge view specs, one per section.
local function edgy_view_specs()
  local specs = {}
  -- IDENTITY starts expanded (always has content when an entity is shown);
  -- the rest start collapsed.
  local start_collapsed = {
    tyo3_identity = false,
    tyo3_notes = true,
    tyo3_docs = true,
    tyo3_summary = true,
    tyo3_affected = true,
  }
  for _, ft in ipairs(VIEW_FTS) do
    local buf = M.bufs[ft]
    table.insert(specs, {
      title = VIEW_TITLES[ft],
      ft = ft,
      pinned = true,
      collapsed = start_collapsed[ft],
      open = function()
        if buf and vim.api.nvim_buf_is_valid(buf) then
          vim.cmd("vertical sbuffer " .. buf)
        end
      end,
    })
  end
  return specs
end

-- ── Setup ───────────────────────────────────────────────────────────────────

--- Register the five right-edge section views with edgy and install the
--- accordion autocmd. Idempotent (guard: M._did_setup).
---
--- Handles two scenarios:
--- 1. edgy not yet set up (test/demo): merge view specs into edgy.config.opts
---    so a subsequent edgy.setup() picks them up.
--- 2. edgy already set up (lazy.nvim): inject View objects directly into the
---    live right edgebar.
function M.setup(edgy)
  if M._did_setup then
    return
  end
  M._edgy = edgy

  create_buffers()
  local ours = edgy_view_specs()

  -- Try to add views to the live layout (edgy already set up). If the layout
  -- doesn't exist yet, merge into opts so a later edgy.setup picks them up.
  local edgy_config = require("edgy.config")
  local layout_ok, layout = pcall(function()
    return edgy_config.layout
  end)
  if layout_ok and layout and layout["right"] then
    -- Live injection: add View objects to the existing right edgebar.
    local edgebar = layout["right"]
    local View = require("edgy.view")
    for _, spec in ipairs(ours) do
      local view = View.new(spec, edgebar)
      table.insert(edgebar.views, view)
    end
  else
    -- Not yet set up: merge into opts so edgy.setup builds them.
    local ok, opts = pcall(function()
      return edgy_config.opts
    end)
    if not ok then
      opts = {}
    end
    opts.right = opts.right or {}
    opts.right.views = opts.right.views or {}
    for _, spec in ipairs(ours) do
      table.insert(opts.right.views, spec)
    end
  end

  -- Install the focus-accordion WinEnter autocmd.
  vim.api.nvim_create_autocmd("WinEnter", {
    callback = function(ev)
      focus_accordion(ev.win)
    end,
  })

  M._did_setup = true
end

-- ── Public API (panel-compatible) ───────────────────────────────────────────

--- Show the entity card under the cursor; *src_buf* is the code buffer it came
--- from. nil/NIL card clears it.
function M.set_context(card, src_buf)
  if not M._did_setup then
    return -- manage = false ⇒ no sidebar; no-op safely
  end
  if card == nil or card == vim.NIL then
    M._entity = nil
    clear_all_buffers()
    return
  end
  M._entity = card
  M._source_buf = src_buf or M._source_buf

  -- Render each section into its buffer.
  set_buffer_lines("tyo3_identity", render.identity_rows(card))
  set_buffer_lines("tyo3_notes", render.note_rows(card))
  set_buffer_lines("tyo3_docs", render.doc_rows(card))
  set_buffer_lines("tyo3_summary", render.summary_rows(card))

  -- Drive the content-accordion: expand sections with data, collapse empty ones.
  content_accordion()

  -- Auto-open the sidebar on the first entity (mirror panel's auto-open).
  if not M.is_open() then
    M.open()
  end
end

function M.clear_context()
  if not M._did_setup then
    return
  end
  M._entity = nil
  clear_all_buffers()
end

--- Re-resolve the shown entity (e.g. after authoring a note/doc) and re-render.
function M.reload()
  if not M._did_setup then
    return
  end
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

function M.is_open()
  if not M._edgy then
    return false
  end
  local ok, layout = pcall(function()
    return require("edgy.config").layout
  end)
  if not ok or not layout or not layout["right"] then
    return false
  end
  return layout["right"].visible > 0
end

function M.open()
  if M._edgy and not M.is_open() then
    M._edgy.open("right")
  end
end

function M.close()
  if M._edgy and M.is_open() then
    M._edgy.close("right")
  end
end

function M.toggle()
  if not M._edgy then
    return
  end
  if M.is_open() then
    M.close()
  else
    M.open()
  end
end

--- Focus the IDENTITY view (used by :TyO3Inspect).
function M.focus_identity()
  if not M._did_setup then
    return
  end
  M.open()
  -- Find the window showing our identity buffer and focus it.
  local ident_buf = M.bufs["tyo3_identity"]
  if not ident_buf or not vim.api.nvim_buf_is_valid(ident_buf) then
    return
  end
  for _, win in ipairs(vim.api.nvim_list_wins()) do
    if vim.api.nvim_win_get_buf(win) == ident_buf then
      vim.api.nvim_set_current_win(win)
      return
    end
  end
end

-- ── AFFECTED log (delta / refinement notifications) ──────────────────────────

local function append(line)
  table.insert(M._affected_lines, line)
  if #M._affected_lines > MAX_LINES then
    table.remove(M._affected_lines, 1)
    M._rev_index = {}
  end
end

--- Handle a `delta` notification.
function M.on_delta(_root, params)
  if not M._did_setup then
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
  set_buffer_lines("tyo3_affected", render.affected_rows(M._affected_lines))
  content_accordion()
  -- Auto-open on the first delta so the affected-set log surfaces.
  if not M.is_open() then
    M.open()
  end
end

--- Handle a `derived` notification (AB3).
function M.on_derived(_root, params)
  if not M._did_setup then
    return
  end
  if not M.is_open() then
    return
  end
  if M._entity and params.durable_id and M._entity.durable_id ~= params.durable_id then
    return
  end
  M.reload()
end

--- Handle a `refinement` notification.
function M.on_refinement(_root, params)
  if not M._did_setup then
    return
  end
  local idx = M._rev_index[params.revision]
  if not idx or not M._affected_lines[idx] then
    return
  end
  if M._rev_narrowed[params.revision] then
    return
  end
  M._rev_narrowed[params.revision] = true
  local narrowed = names_of(params.narrowed or {}, 6)
  M._affected_lines[idx] = M._affected_lines[idx]
    .. ("  → narrowed {%s}"):format(table.concat(narrowed, ", "))
  set_buffer_lines("tyo3_affected", render.affected_rows(M._affected_lines))
  content_accordion()
end

return M
