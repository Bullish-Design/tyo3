-- tyo3.nvim — the floating entity inspector (:TyO3Inspect).
--
-- Resolves the entity under the cursor (`entity_at`) and shows its cross-layer
-- card in a transient float: DurableId, qualified name, kind, location, status,
-- authored records, derived artifacts, and the last revision that affected it.
-- A popup is the *one* place a popup is right — on-demand, focused, dismissable.

local card_fmt = require("tyo3.card")

local M = {}

local function open_float(lines)
  local width = 0
  for _, l in ipairs(lines) do
    width = math.max(width, vim.fn.strdisplaywidth(l))
  end
  width = math.min(math.max(width + 2, 30), 100)
  local height = math.min(#lines, 24)

  local buf = vim.api.nvim_create_buf(false, true)
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.bo[buf].modifiable = false
  vim.bo[buf].bufhidden = "wipe"
  vim.bo[buf].filetype = "tyo3inspect"

  local win = vim.api.nvim_open_win(buf, false, {
    relative = "cursor",
    row = 1,
    col = 0,
    width = width,
    height = height,
    style = "minimal",
    border = "rounded",
    title = " TyO3 ",
    title_pos = "center",
  })
  vim.wo[win].wrap = false

  -- Dismiss on q/Esc, and auto-close when the cursor moves in the parent window.
  local function close()
    if vim.api.nvim_win_is_valid(win) then
      vim.api.nvim_win_close(win, true)
    end
  end
  vim.keymap.set("n", "q", close, { buffer = buf, nowait = true })
  vim.keymap.set("n", "<Esc>", close, { buffer = buf, nowait = true })
  vim.api.nvim_create_autocmd({ "CursorMoved", "BufLeave", "InsertEnter" }, {
    once = true,
    callback = close,
  })
  return win
end

--- :TyO3Inspect — inspect the entity under the cursor.
function M.inspect()
  local bufnr = vim.api.nvim_get_current_buf()
  local path = vim.api.nvim_buf_get_name(bufnr)
  if path == "" then
    vim.notify("[tyo3] buffer has no file", vim.log.levels.WARN)
    return
  end
  local pos = vim.api.nvim_win_get_cursor(0) -- {row(1-based), col(0-based)}
  local line = pos[1]
  local col = pos[2] + 1 -- daemon columns are 1-based
  require("tyo3").rpc(bufnr, "entity_at", { path = path, line = line, col = col }, function(err, card)
    if err then
      vim.notify("[tyo3] inspect failed: " .. (err.message or "error"), vim.log.levels.ERROR)
      return
    end
    if card == nil or card == vim.NIL then
      vim.notify("[tyo3] no entity under cursor", vim.log.levels.INFO)
      return
    end
    open_float(card_fmt.build_lines(card))
  end)
end

return M
