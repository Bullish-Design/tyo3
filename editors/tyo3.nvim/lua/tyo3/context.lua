-- tyo3.nvim — cursor-driven CONTEXT updates.
--
-- As the cursor moves through a Python buffer, surface the durable entity under
-- it (and its linked notes / summary) in the panel's CONTEXT section. The lookup
-- fires *only when the enclosing code node changes* — gated by Treesitter,
-- debounced, and resilient to stale responses — so it never floods the
-- single-threaded session actor or flickers to a previous entity.
--
-- Default-off (`context = "off"`); opt in with `setup{ context = "cursor" }` or
-- toggle at runtime with :TyO3Context.

local config = require("tyo3.config")

local M = {}

M._last_key = {} -- bufnr -> enclosing-node key (dedupe + stale-drop anchor)
M._debounce = {} -- bufnr -> uv timer

--- Compute the enclosing definition-node key for the cursor in *bufnr*.
--- Returns: key (string), line, col (1-based, for the daemon), used_fallback.
--- When no Treesitter parser is available (or the parse fails) it degrades to a
--- line-based key + the raw cursor position so the feature still works, coarser.
function M.node_key(bufnr)
  local pos = vim.api.nvim_win_get_cursor(0) -- {row(1-based), col(0-based)}
  local cursor_line, cursor_col = pos[1], pos[2]
  local function fallback()
    return ("line:" .. cursor_line), cursor_line, cursor_col + 1, true
  end

  local ok, node = pcall(vim.treesitter.get_node, { bufnr = bufnr })
  if not ok or not node then
    return fallback()
  end
  -- Climb to the nearest function/class definition.
  while
    node
    and not node:type():match("function_definition")
    and not node:type():match("class_definition")
  do
    node = node:parent()
  end
  if not node then
    return fallback()
  end
  local sr, sc, er, ec = node:range() -- 0-based
  local key = table.concat({ sr, sc, er, ec }, ":")
  return key, sr + 1, sc + 1, false
end

-- Fire the entity_at lookup for the captured key/position, dropping stale results.
local function lookup(bufnr, key, line, col)
  if not vim.api.nvim_buf_is_loaded(bufnr) then
    return
  end
  local path = vim.api.nvim_buf_get_name(bufnr)
  if path == "" then
    return
  end
  require("tyo3").rpc(bufnr, "entity_at", { path = path, line = line, col = col }, function(err, card)
    -- Stale-drop: the cursor moved to a different node after we asked.
    if M._last_key[bufnr] ~= key then
      return
    end
    if err then
      return
    end
    require("tyo3.panel").set_context(card, bufnr)
  end)
end

--- CursorMoved / CursorMovedI handler for *bufnr*.
function M.on_cursor(bufnr)
  bufnr = bufnr or vim.api.nvim_get_current_buf()
  local cfg = config.get()
  if cfg.context ~= "cursor" then
    return
  end
  if not vim.api.nvim_buf_is_loaded(bufnr) then
    return
  end
  if vim.bo[bufnr].filetype ~= "python" then
    return
  end
  if not require("tyo3").root_for_buf(bufnr) then
    return
  end

  local key, line, col = M.node_key(bufnr)
  -- Dedupe: only react when the enclosing node changes.
  if key == M._last_key[bufnr] then
    return
  end
  M._last_key[bufnr] = key

  -- Debounce the actual RPC (copy of init.on_text_changed's timer dance).
  local existing = M._debounce[bufnr]
  if existing then
    existing:stop()
    existing:close()
    M._debounce[bufnr] = nil
  end
  local uv = vim.uv or vim.loop
  local timer = uv.new_timer()
  M._debounce[bufnr] = timer
  timer:start(
    cfg.context_debounce_ms,
    0,
    vim.schedule_wrap(function()
      timer:stop()
      timer:close()
      M._debounce[bufnr] = nil
      lookup(bufnr, key, line, col)
    end)
  )
end

--- :TyO3Context — flip `context` between "cursor" and "off" at runtime.
function M.toggle()
  local cfg = config.get()
  if cfg.context == "cursor" then
    cfg.context = "off"
    M._last_key = {}
    require("tyo3.panel").clear_context()
    vim.notify("[tyo3] context: off", vim.log.levels.INFO)
  else
    cfg.context = "cursor"
    vim.notify("[tyo3] context: cursor", vim.log.levels.INFO)
    M.on_cursor(vim.api.nvim_get_current_buf())
  end
end

return M
