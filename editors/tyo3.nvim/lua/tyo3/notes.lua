-- tyo3.nvim — author intent notes (:TyO3Note).
--
-- Authors a durable, identity-anchored note on the entity under the cursor. The
-- note survives renames, moves, and reformats — that anchor is the whole point.

local M = {}

local function author(bufnr, durable_id, text)
  require("tyo3").rpc(
    bufnr,
    "author",
    { layer = "intent", durable_id = durable_id, value = { note = text } },
    function(err)
      if err then
        vim.notify("[tyo3] note failed: " .. (err.message or "error"), vim.log.levels.ERROR)
        return
      end
      vim.notify("[tyo3] note authored", vim.log.levels.INFO)
      require("tyo3.decorate").apply(bufnr)
    end
  )
end

--- :TyO3Note [text] — author an intent note on the entity under the cursor.
--- With no text, prompts via vim.ui.input.
function M.note(text)
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
      author(bufnr, card.durable_id, text)
    else
      vim.ui.input({ prompt = "TyO3 note for " .. (card.qualified_name or card.name) .. ": " }, function(input)
        if input and #input > 0 then
          author(bufnr, card.durable_id, input)
        end
      end)
    end
  end)
end

return M
