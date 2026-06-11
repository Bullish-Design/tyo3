-- tyo3.nvim — author intent notes (:TyO3Note).
--
-- Authors a durable, identity-anchored note on the entity under the cursor. The
-- note survives renames, moves, and reformats — that anchor is the whole point.

local M = {}

-- Author shares ONE implementation with the `tyo3.author` code-action command:
-- both funnel through `lsp.author_note` (the daemon `author` verb + re-decorate +
-- layer-state refresh), so :TyO3Note and the picker can never diverge.
local function author(bufnr, durable_id, text, layer)
  layer = layer or "intent"
  local root = require("tyo3").root_for_buf(bufnr)
  if not root then
    vim.notify("[tyo3] not in a TyO3 project", vim.log.levels.WARN)
    return
  end
  require("tyo3.lsp").author_note(bufnr, root, durable_id, layer, { note = text }, function(err)
    vim.schedule(function()
      if err then
        vim.notify("[tyo3] " .. layer .. " note failed: " .. (err.message or "error"), vim.log.levels.ERROR)
      else
        vim.notify("[tyo3] " .. layer .. " note authored", vim.log.levels.INFO)
      end
    end)
  end)
end

--- :TyO3Note [text] — author a note on the entity under the cursor, in *layer*
--- (default ``intent``). With no text, prompts via vim.ui.input.
function M.note(text, layer)
  layer = layer or "intent"
  local bufnr = vim.api.nvim_get_current_buf()
  local path = vim.api.nvim_buf_get_name(bufnr)
  if path == "" then
    vim.notify("[tyo3] buffer has no file", vim.log.levels.WARN)
    return
  end
  local pos = vim.api.nvim_win_get_cursor(0)
  local line, col = pos[1], pos[2] + 1
  require("tyo3").rpc(bufnr, "entity_at", { path = path, line = line, col = col }, function(err, card)
    if err then
      vim.notify("[tyo3] " .. (err.message or "error"), vim.log.levels.ERROR)
      return
    end
    if card == nil or card == vim.NIL then
      vim.notify("[tyo3] no entity under cursor to annotate", vim.log.levels.WARN)
      return
    end
    if text and #text > 0 then
      author(bufnr, card.durable_id, text, layer)
    else
      vim.ui.input({ prompt = "TyO3 " .. layer .. " note for " .. (card.qualified_name or card.name) .. ": " }, function(input)
        if input and #input > 0 then
          author(bufnr, card.durable_id, input, layer)
        end
      end)
    end
  end)
end

return M
