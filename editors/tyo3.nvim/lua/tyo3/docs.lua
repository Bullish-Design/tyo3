-- tyo3.nvim — static documentation registry + opener.
--
-- These are the *tool's* docs (user guides + developer/architecture), shipped as
-- markdown under `editors/tyo3.nvim/docs/`. They are distinct from a per-entity
-- authored doc (see `entitydoc.lua`), which is durable data attached to a code
-- entity. The sidebar's DOCS pane links to both; this module owns the static set.

local M = {}

-- Resolve the plugin root from this file: lua/tyo3/docs.lua → editors/tyo3.nvim.
local function plugin_root()
  local src = debug.getinfo(1, "S").source:sub(2)
  return vim.fn.fnamemodify(src, ":h:h:h")
end

M.dir = plugin_root() .. "/docs"

-- Ordered registry. `kind` groups user- vs developer-oriented docs so the demo
-- (and the pane) can show both.
M.entries = {
  { id = "workflow", title = "User · workflow", kind = "user", file = "user/workflow.md" },
  { id = "commands", title = "User · command reference", kind = "user", file = "user/commands.md" },
  { id = "architecture", title = "Dev · architecture", kind = "dev", file = "dev/architecture.md" },
  { id = "identity", title = "Dev · durable identity", kind = "dev", file = "dev/identity.md" },
}

-- The most relevant static doc for a given sidebar section (opened with `gd`).
M.section_doc = {
  IDENTITY = "identity",
  NOTES = "workflow",
  DOCS = "workflow",
  SUMMARY = "identity",
  ACTIONS = "commands",
  AFFECTED = "architecture",
}

function M.by_id(id)
  for _, e in ipairs(M.entries) do
    if e.id == id then
      return e
    end
  end
  return nil
end

function M.path(entry)
  return M.dir .. "/" .. entry.file
end

--- Open a static doc (read-only markdown scratch) in a left vsplit.
function M.open(entry)
  if type(entry) == "string" then
    entry = M.by_id(entry)
  end
  if not entry then
    vim.notify("[tyo3] unknown doc", vim.log.levels.WARN)
    return
  end
  local path = M.path(entry)
  local lines = vim.fn.filereadable(path) == 1 and vim.fn.readfile(path) or { "# " .. entry.title, "", "(doc not found: " .. path .. ")" }

  vim.cmd("topleft vsplit")
  local win = vim.api.nvim_get_current_win()
  local buf = vim.api.nvim_create_buf(false, true)
  vim.api.nvim_win_set_buf(win, buf)
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.bo[buf].modifiable = false
  vim.bo[buf].bufhidden = "wipe"
  vim.bo[buf].filetype = "markdown"
  pcall(vim.api.nvim_buf_set_name, buf, "TyO3Doc://" .. entry.file)
  vim.api.nvim_win_set_width(win, math.min(92, math.floor(vim.o.columns * 0.5)))
  vim.wo[win].wrap = true
  vim.wo[win].linebreak = true
  local function close()
    if vim.api.nvim_win_is_valid(win) then
      vim.api.nvim_win_close(win, true)
    end
  end
  vim.keymap.set("n", "q", close, { buffer = buf, nowait = true })
  return win
end

--- :TyO3Docs — choose a static doc to open.
function M.index()
  vim.ui.select(M.entries, {
    prompt = "TyO3 docs",
    format_item = function(e)
      return e.title
    end,
  }, function(choice)
    if choice then
      M.open(choice)
    end
  end)
end

return M
