-- tyo3.nvim — optional overseer.nvim integration (supporting actor only).
--
-- overseer is the right primitive for the *plumbing*, not the live surface. The
-- live notes / affected / inspector surfaces are extmarks + floats + the bus and
-- never route through overseer. What overseer is good at — owning a process and
-- surfacing its log, and one-shot tasks — is exactly the daemon ops:
-- `reindex` / `gc` / `check`.
--
-- This module exposes `M.run(method)` for those ops (used by the user commands
-- whether or not overseer is installed) and, when `setup({overseer=true})` and
-- overseer is present, opens the daemon's captured stderr log in an overseer-
-- style scratch view on demand. It is a no-op without overseer.

local M = {}

M.enabled = false

local function has_overseer()
  return pcall(require, "overseer")
end

function M.setup()
  M.enabled = has_overseer()
  if not M.enabled then
    vim.notify("[tyo3] overseer.nvim not found; ops will report via notifications", vim.log.levels.WARN)
  end
end

--- Run a one-shot daemon op (reindex / gc / check) and report the result.
function M.run(method)
  local bufnr = vim.api.nvim_get_current_buf()
  require("tyo3").rpc(bufnr, method, {}, function(err, res)
    if err then
      vim.notify("[tyo3] " .. method .. " failed: " .. (err.message or "error"), vim.log.levels.ERROR)
      return
    end
    vim.notify("[tyo3] " .. method .. ": " .. vim.json.encode(res), vim.log.levels.INFO)
  end)
end

--- Open the captured daemon stderr log for the current project in a scratch buf.
function M.daemon_log()
  local bufnr = vim.api.nvim_get_current_buf()
  local root = require("tyo3").root_for_buf(bufnr)
  if not root then
    vim.notify("[tyo3] not in a TyO3 project", vim.log.levels.WARN)
    return
  end
  local log = require("tyo3.daemon").log_for(root)
  local buf = vim.api.nvim_create_buf(false, true)
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, #log > 0 and log or { "(no daemon log captured yet)" })
  vim.bo[buf].bufhidden = "wipe"
  vim.bo[buf].filetype = "log"
  vim.cmd("botright split")
  vim.api.nvim_win_set_buf(0, buf)
  vim.api.nvim_win_set_height(0, 12)
end

return M
