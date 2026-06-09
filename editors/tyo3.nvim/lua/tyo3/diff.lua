-- tyo3.nvim — entity-level snapshot diff (:TyO3Diff <from_rev> [to_rev]).
--
-- Shows the id-level diff between two revisions (added / removed / changed /
-- moved) in a scratch float, names resolved where known. With no `to_rev` it
-- diffs against the current head.

local M = {}

local function names(ids)
  local decorate = require("tyo3.decorate")
  local out = {}
  for _, id in ipairs(ids) do
    table.insert(out, decorate.name_for(id) or ("…" .. id:sub(-8)))
  end
  table.sort(out)
  return out
end

local function show_result(d)
  local lines = {
    ("  diff  rev %d → rev %d"):format(d.before_revision, d.after_revision),
    "  " .. ("─"):rep(44),
  }
  local function section(label, ids)
    if #ids > 0 then
      table.insert(lines, ("  %-8s (%d)"):format(label, #ids))
      for _, n in ipairs(names(ids)) do
        table.insert(lines, "    " .. n)
      end
    end
  end
  section("changed", d.changed or {})
  section("moved", d.moved or {})
  section("added", d.added or {})
  section("removed", d.removed or {})
  if #lines == 2 then
    table.insert(lines, "  (no entity-level changes)")
  end

  local width = 48
  for _, l in ipairs(lines) do
    width = math.max(width, vim.fn.strdisplaywidth(l) + 2)
  end
  local buf = vim.api.nvim_create_buf(false, true)
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.bo[buf].modifiable = false
  vim.bo[buf].bufhidden = "wipe"
  local win = vim.api.nvim_open_win(buf, true, {
    relative = "editor",
    width = math.min(width, 100),
    height = math.min(#lines, 30),
    row = 2,
    col = 4,
    style = "minimal",
    border = "rounded",
    title = " TyO3 Diff ",
    title_pos = "center",
  })
  vim.keymap.set("n", "q", function()
    if vim.api.nvim_win_is_valid(win) then
      vim.api.nvim_win_close(win, true)
    end
  end, { buffer = buf, nowait = true })
end

--- :TyO3Diff <from_rev> [to_rev]
function M.show(args)
  local bufnr = vim.api.nvim_get_current_buf()
  local parts = vim.split(vim.trim(args or ""), "%s+", { trimempty = true })
  local from_rev = tonumber(parts[1])
  local to_rev = tonumber(parts[2])

  local function do_diff(frm, to)
    local params = { from_rev = frm }
    if to then
      params.to_rev = to
    end
    require("tyo3").rpc(bufnr, "diff", params, function(err, d)
      if err then
        vim.notify("[tyo3] diff failed: " .. (err.message or "error"), vim.log.levels.ERROR)
        return
      end
      show_result(d)
    end)
  end

  if from_rev then
    do_diff(from_rev, to_rev)
    return
  end
  -- No from_rev given: diff the previous revision against head.
  require("tyo3").rpc(bufnr, "ping", {}, function(err, res)
    if err or not res then
      vim.notify("[tyo3] usage: :TyO3Diff <from_rev> [to_rev]", vim.log.levels.WARN)
      return
    end
    local head = res.revision or 1
    do_diff(math.max(head - 1, 0), head)
  end)
end

return M
