-- tyo3.nvim — atomic entity move (:TyO3Move <name> <dest>).
--
-- Moves the named entity from the current file to *dest* (project-relative) in a
-- single commit via `sync_buffers` (→ session.edit_many). One atomic commit is
-- what binds the relocation as a *Moved* — same durable id, same content hash —
-- so an authored note rides along to the new file. Two separate edits would not
-- guarantee it. This is the demo's headline: move the function, the note follows.
--
-- The destination should already be a project file (an empty stub is fine); the
-- entity body is appended verbatim so its content hash is unchanged.

local M = {}

local function relpath(root, abspath)
  local r = root:gsub("/$", "")
  if abspath:sub(1, #r + 1) == r .. "/" then
    return abspath:sub(#r + 2)
  end
  return abspath
end

--- :TyO3Move <name> <dest_relpath>
function M.move(name, dest)
  if not name or name == "" or not dest or dest == "" then
    vim.notify("[tyo3] usage: :TyO3Move <entity_name> <dest_path>", vim.log.levels.WARN)
    return
  end
  local src_buf = vim.api.nvim_get_current_buf()
  local src_path = vim.api.nvim_buf_get_name(src_buf)
  if src_path == "" then
    vim.notify("[tyo3] current buffer has no file", vim.log.levels.WARN)
    return
  end
  local tyo3 = require("tyo3")
  local root = tyo3.root_for_buf(src_buf)
  if not root then
    vim.notify("[tyo3] not in a TyO3 project", vim.log.levels.WARN)
    return
  end

  tyo3.rpc(src_buf, "decorate", { path = src_path }, function(err, items)
    if err or type(items) ~= "table" then
      vim.notify("[tyo3] move: could not resolve entities", vim.log.levels.ERROR)
      return
    end
    local entry
    for _, it in ipairs(items) do
      if it.name == name or it.qualified_name == name then
        entry = it
        break
      end
    end
    if not entry then
      vim.notify("[tyo3] move: no entity named '" .. name .. "' in this file", vim.log.levels.WARN)
      return
    end

    local start_line = entry.range.start.line -- 1-based
    local end_line = entry.range["end"].line -- 1-based, inclusive
    local src_lines = vim.api.nvim_buf_get_lines(src_buf, 0, -1, false)
    local entity_lines = {}
    for i = start_line, end_line do
      table.insert(entity_lines, src_lines[i])
    end

    -- Source buffer with the entity removed (trim a trailing blank gap).
    local new_src = {}
    for i, l in ipairs(src_lines) do
      if i < start_line or i > end_line then
        table.insert(new_src, l)
      end
    end

    -- Destination: existing dest content + a blank separator + the entity.
    local dest_abs = root:gsub("/$", "") .. "/" .. dest
    local dest_buf = vim.fn.bufadd(dest_abs)
    vim.fn.bufload(dest_buf)
    local dest_lines = vim.api.nvim_buf_get_lines(dest_buf, 0, -1, false)
    -- Drop a single trailing empty line so we control spacing.
    while #dest_lines > 0 and dest_lines[#dest_lines] == "" do
      table.remove(dest_lines)
    end
    local new_dest = {}
    vim.list_extend(new_dest, dest_lines)
    if #new_dest > 0 then
      table.insert(new_dest, "")
      table.insert(new_dest, "")
    end
    vim.list_extend(new_dest, entity_lines)

    -- Apply to the editor buffers.
    vim.api.nvim_buf_set_lines(src_buf, 0, -1, false, new_src)
    vim.api.nvim_buf_set_lines(dest_buf, 0, -1, false, new_dest)

    local function text(lines)
      return table.concat(lines, "\n") .. "\n"
    end
    local edits = {}
    edits[relpath(root, src_path)] = text(new_src)
    edits[relpath(root, dest_abs)] = text(new_dest)

    -- One atomic commit ⇒ a Moved bind (id + note + hash preserved).
    tyo3.rpc(src_buf, "sync_buffers", { edits = edits }, function(serr, delta)
      if serr then
        vim.notify("[tyo3] move failed: " .. (serr.message or "error"), vim.log.levels.ERROR)
        return
      end
      local moved = (delta and delta.moved) or {}
      local bound = #moved > 0
      vim.notify(
        ("[tyo3] moved %s → %s%s"):format(name, dest, bound and " (identity preserved)" or ""),
        vim.log.levels.INFO
      )
      require("tyo3.decorate").apply(src_buf)
      require("tyo3.decorate").apply(dest_buf)
    end)
  end)
end

return M
