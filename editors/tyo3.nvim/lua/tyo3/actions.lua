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

-- Resolve a project-relative posix path to an absolute path for this buffer.
local function abspath(src_buf, file)
  if file:sub(1, 1) == "/" then
    return file
  end
  local root = require("tyo3").root_for_buf(src_buf)
  if root then
    return root:gsub("/$", "") .. "/" .. file
  end
  return file
end

-- Open *file* at the 1-based *(line, col)* in a non-panel window.
local function jump_to(src_buf, file, line, col)
  local abs = abspath(src_buf, file)
  local target = code_win()
  if target then
    vim.api.nvim_set_current_win(target)
  end
  vim.cmd("edit " .. vim.fn.fnameescape(abs))
  if line then
    -- nvim_win_set_cursor: row 1-based, col 0-based.
    pcall(vim.api.nvim_win_set_cursor, 0, { line, math.max((col or 1) - 1, 0) })
  end
end

-- Jump to the entity's own definition. The card already carries the exact
-- file + range (resolved on the frozen snapshot), so we jump precisely — no
-- regex search. Falls back to `locate` only when the card lacks a range.
local function goto_def(card, src_buf)
  local rng = card.range
  if card.file and card.file ~= vim.NIL and rng and rng ~= vim.NIL then
    jump_to(src_buf, card.file, rng.start.line, rng.start.column)
    return
  end
  require("tyo3").rpc(src_buf, "locate", { durable_id = card.durable_id }, function(err, res)
    if err or not res or not res.location or res.location == vim.NIL then
      notify("could not locate entity", vim.log.levels.WARN)
      return
    end
    local loc = res.location
    local file = loc:match("^(.-)::") or loc
    jump_to(src_buf, file)
  end)
end

-- Find callers (references) of the entity and drop them into the quickfix list.
local function find_callers(card, src_buf)
  local rng = card.range
  if not (card.file and card.file ~= vim.NIL and rng and rng ~= vim.NIL) then
    notify("entity has no resolved position to query references", vim.log.levels.WARN)
    return
  end
  require("tyo3").rpc(
    src_buf,
    "references",
    { path = card.file, line = rng.start.line, col = rng.start.column },
    function(err, res)
      if err then
        notify("references failed: " .. (err.message or "error"), vim.log.levels.ERROR)
        return
      end
      local refs = (res and res.references) or {}
      local items = {}
      for _, r in ipairs(refs) do
        table.insert(items, {
          filename = abspath(src_buf, r.path),
          lnum = r.range.start.line,
          col = r.range.start.column,
          text = ("%s reference to %s"):format(r.kind or "?", card.name or card.durable_id),
        })
      end
      if #items == 0 then
        notify("no references found", vim.log.levels.INFO)
        return
      end
      vim.fn.setqflist({}, " ", { title = "TyO3 callers: " .. (card.name or card.durable_id), items = items })
      local target = code_win()
      if target then
        vim.api.nvim_set_current_win(target)
      end
      vim.cmd("copen")
    end
  )
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
    table.insert(items, { label = "📞 Find callers (references)", run = find_callers })
  end
  table.insert(items, { label = "🌐 Affected set (picker)", run = function(_, _) require("tyo3.telescope").affected() end })
  return items
end

return M
