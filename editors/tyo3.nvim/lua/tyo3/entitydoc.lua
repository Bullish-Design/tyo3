-- tyo3.nvim — per-entity authored markdown docs.
--
-- Write a markdown document *about the entity under the cursor*, in-editor, while
-- navigating. It is authored into the `docs` layer keyed by the entity's durable
-- id — so, like an intent note, it stays glued to the entity across edits and
-- atomic moves (durable identity for documentation, not for a file path).
--
-- `:TyO3Doc` resolves the entity under the cursor and opens a markdown editor
-- prefilled with any existing doc; `:w` in that editor authors it. The DOCS pane
-- and decorations then surface it.

local M = {}

M.LAYER = "docs"

--- The authored markdown string carried by a card's `docs` layer, or nil.
function M.markdown_of(card)
  local rec = card and card.authored and card.authored[M.LAYER]
  if rec and rec.status == "present" and type(rec.value) == "table" then
    local md = rec.value.markdown
    if type(md) == "string" and md ~= "" then
      return md
    end
  end
  return nil
end

-- Open a markdown editor bound to (src_buf, durable_id). `:w` authors the doc.
local function open_editor(src_buf, durable_id, title, existing)
  local lines
  if existing then
    lines = vim.split(existing, "\n", { plain = true })
  else
    lines = { "# " .. (title or durable_id), "", "" }
  end

  vim.cmd("botright new")
  local win = vim.api.nvim_get_current_win()
  local buf = vim.api.nvim_get_current_buf()
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.bo[buf].buftype = "acwrite" -- we own :w via BufWriteCmd
  vim.bo[buf].bufhidden = "wipe"
  vim.bo[buf].swapfile = false
  vim.bo[buf].filetype = "markdown"
  pcall(vim.api.nvim_buf_set_name, buf, "TyO3Doc://" .. (title or durable_id))
  vim.bo[buf].modified = false
  vim.api.nvim_win_set_height(win, math.min(18, math.max(8, #lines + 3)))
  vim.wo[win].wrap = true
  vim.wo[win].linebreak = true

  vim.api.nvim_create_autocmd("BufWriteCmd", {
    buffer = buf,
    callback = function()
      local content = table.concat(vim.api.nvim_buf_get_lines(buf, 0, -1, false), "\n")
      require("tyo3").rpc(
        src_buf,
        "author",
        { layer = M.LAYER, durable_id = durable_id, value = { markdown = content } },
        function(err)
          if err then
            vim.notify("[tyo3] doc save failed: " .. (err.message or "error"), vim.log.levels.ERROR)
            return
          end
          if vim.api.nvim_buf_is_valid(buf) then
            vim.bo[buf].modified = false
          end
          vim.notify("[tyo3] doc saved — glued to the entity", vim.log.levels.INFO)
          require("tyo3.decorate").apply(src_buf)
          require("tyo3.panel").reload()
        end
      )
    end,
  })
  return buf, win
end

--- Open the doc editor for an already-resolved card (used by the DOCS pane).
function M.edit_card(card, src_buf)
  if not card or card == vim.NIL or not card.durable_id then
    vim.notify("[tyo3] no entity to document", vim.log.levels.WARN)
    return
  end
  open_editor(src_buf, card.durable_id, card.qualified_name or card.name, M.markdown_of(card))
end

--- :TyO3Doc — write/edit the markdown doc for the entity under the cursor.
function M.edit()
  local bufnr = vim.api.nvim_get_current_buf()
  local path = vim.api.nvim_buf_get_name(bufnr)
  if path == "" then
    vim.notify("[tyo3] buffer has no file", vim.log.levels.WARN)
    return
  end
  local pos = vim.api.nvim_win_get_cursor(0)
  require("tyo3").rpc(bufnr, "entity_at", { path = path, line = pos[1], col = pos[2] + 1 }, function(err, card)
    if err or card == nil or card == vim.NIL then
      vim.notify("[tyo3] no entity under cursor", vim.log.levels.INFO)
      return
    end
    open_editor(bufnr, card.durable_id, card.qualified_name or card.name, M.markdown_of(card))
  end)
end

return M
