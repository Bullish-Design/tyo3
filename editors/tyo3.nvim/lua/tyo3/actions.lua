-- tyo3.nvim — context actions (the tools you can run right now).
--
-- Raises visibility of the available tools for the entity currently shown in the
-- sidebar. The ACTIONS pane renders `list(card)` and runs the chosen entry's
-- `run(card, src_buf)` against the cached card + its source buffer — so it works
-- even though the cursor is parked in the panel window, not the code.

local M = {}

local function notify(msg, lvl)
  vim.notify("[tyo3] " .. msg, lvl or vim.log.levels.INFO)
end

-- A window that is not the panel, to open jumps/files in.
local function code_win()
  local panel = require("tyo3.panel")
  local cur = vim.api.nvim_get_current_win()
  if cur ~= panel.win then
    return cur
  end
  for _, w in ipairs(vim.api.nvim_list_wins()) do
    if w ~= panel.win then
      return w
    end
  end
  return nil
end

local function author_note(card, src_buf)
  vim.ui.input({ prompt = "intent note: " }, function(text)
    if not text or text == "" then
      return
    end
    require("tyo3").rpc(
      src_buf,
      "author",
      { layer = "intent", durable_id = card.durable_id, value = { note = text } },
      function(err)
        if err then
          notify("note failed: " .. (err.message or "error"), vim.log.levels.ERROR)
          return
        end
        notify("note authored")
        require("tyo3.decorate").apply(src_buf)
        require("tyo3.panel").reload()
      end
    )
  end)
end

local function goto_def(card, src_buf)
  require("tyo3").rpc(src_buf, "locate", { durable_id = card.durable_id }, function(err, res)
    if err or not res or not res.location or res.location == vim.NIL then
      notify("could not locate entity", vim.log.levels.WARN)
      return
    end
    local loc = res.location
    local file = loc:match("^(.-)::") or loc
    local root = require("tyo3").root_for_buf(src_buf)
    local abs = file
    if root and file:sub(1, 1) ~= "/" then
      abs = root:gsub("/$", "") .. "/" .. file
    end
    local target = code_win()
    if target then
      vim.api.nvim_set_current_win(target)
    end
    vim.cmd("edit " .. vim.fn.fnameescape(abs))
    local name = loc:match("::([^:]+)$")
    if name then
      local bare = name:match("([^.]+)$") or name
      vim.fn.search("\\<" .. vim.fn.escape(bare, "\\") .. "\\>", "w")
    end
  end)
end

--- The action list for a card (nil → only the entity-independent tools).
function M.list(card)
  local has = card ~= nil and card ~= vim.NIL
  local items = {}
  if has then
    table.insert(items, { label = "🔍 Inspect entity card", run = function(c, _) require("tyo3.inspect").show_card(c) end })
    table.insert(items, { label = "📝 Author intent note", run = author_note })
    table.insert(items, { label = "📄 Write / edit doc", run = function(c, sb) require("tyo3.entitydoc").edit_card(c, sb) end })
    table.insert(items, { label = "↪ Go to definition", run = goto_def })
  end
  table.insert(items, { label = "🌐 Affected set (picker)", run = function(_, _) require("tyo3.telescope").affected() end })
  return items
end

return M
