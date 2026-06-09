-- tyo3.nvim — the affected-set panel.
--
-- A scratch side buffer subscribed (via the bus pump's `delta` notifications) to
-- "what did my last edit affect?". Ambient and persistent — a dock, not a popup.
-- Each commit appends `rev N · changed C · affects {…}`; a later `refinement`
-- annotates the same revision with its narrowed set.

local decorate = require("tyo3.decorate")

local M = {}

M.win = nil
M.buf = nil
M._lines = {} -- rendered history
M._rev_index = {} -- revision -> line index (1-based) for refinement annotation
M.last_affected_ids = {} -- ids of the most recent non-empty delta (for pickers)

local MAX_LINES = 200

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
  vim.api.nvim_buf_set_name(buf, "TyO3://affected")
  vim.bo[buf].buftype = "nofile"
  vim.bo[buf].bufhidden = "hide"
  vim.bo[buf].swapfile = false
  vim.bo[buf].filetype = "tyo3panel"
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
  vim.api.nvim_win_set_width(M.win, 42)
  vim.wo[M.win].number = false
  vim.wo[M.win].relativenumber = false
  vim.wo[M.win].wrap = false
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
  end
end

local function render()
  if not (M.buf and vim.api.nvim_buf_is_valid(M.buf)) then
    return
  end
  vim.bo[M.buf].modifiable = true
  vim.api.nvim_buf_set_lines(M.buf, 0, -1, false, M._lines)
  vim.bo[M.buf].modifiable = false
  if M.is_open() then
    local n = #M._lines
    pcall(vim.api.nvim_win_set_cursor, M.win, { math.max(n, 1), 0 })
  end
end

local function append(line)
  table.insert(M._lines, line)
  if #M._lines > MAX_LINES then
    table.remove(M._lines, 1)
    -- rev_index line numbers shift; cheapest correct fix is to forget them.
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
  M._rev_index[params.revision] = #M._lines
  render()
  if cfg.panel == "auto" and not M.is_open() then
    M.open()
    render()
  end
end

--- Handle a `refinement` notification: annotate the matching revision's line.
function M.on_refinement(_root, params)
  local idx = M._rev_index[params.revision]
  if not idx or not M._lines[idx] then
    return
  end
  local narrowed = names_of(params.narrowed or {}, 6)
  M._lines[idx] = M._lines[idx] .. ("  → narrowed {%s}"):format(table.concat(narrowed, ", "))
  render()
end

return M
