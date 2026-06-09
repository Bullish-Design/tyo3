-- tyo3.nvim — inline decorations (authored notes + derived summaries).
--
-- One extmark namespace. On each commit the daemon is re-asked for the file's
-- entity → annotation map (`decorate {path}`) and the namespace is reset — we
-- never trust a drifted extmark across a structural edit; durable identity is
-- the truth. A side effect: every decorate response refreshes the id → name
-- cache the panel and pickers read.

local M = {}

M.ns = vim.api.nvim_create_namespace("tyo3_decorations")

-- durable_id -> { name, qualified_name, kind, file } (best-effort, last-seen).
M.name_cache = {}

function M.setup_highlights()
  -- Defined with default=true so a user colorscheme can override them.
  vim.api.nvim_set_hl(0, "TyO3Note", { link = "DiagnosticInfo", default = true })
  vim.api.nvim_set_hl(0, "TyO3Summary", { link = "Comment", default = true })
  vim.api.nvim_set_hl(0, "TyO3Icon", { link = "Special", default = true })
end

function M.name_for(durable_id)
  local rec = M.name_cache[durable_id]
  if rec then
    return rec.qualified_name or rec.name
  end
  return nil
end

local function place(bufnr, items)
  local cfg = require("tyo3.config").get()
  vim.api.nvim_buf_clear_namespace(bufnr, M.ns, 0, -1)
  if not cfg.virtual_text then
    return
  end
  local line_count = vim.api.nvim_buf_line_count(bufnr)
  for _, item in ipairs(items) do
    local rng = item.range and item.range.start
    if rng then
      local row = rng.line - 1 -- daemon ranges are 1-based; extmarks are 0-based.
      if row >= 0 and row < line_count then
        if item.note then
          vim.api.nvim_buf_set_extmark(bufnr, M.ns, row, 0, {
            virt_lines = { { { "  ", "TyO3Icon" }, { "🏷 " .. item.note, "TyO3Note" } } },
            virt_lines_above = true,
          })
        end
        if item.summary then
          vim.api.nvim_buf_set_extmark(bufnr, M.ns, row, 0, {
            virt_text = { { "  ⟢ " .. item.summary, "TyO3Summary" } },
            virt_text_pos = "eol",
            hl_mode = "combine",
          })
        end
      end
    end
  end
end

--- Re-anchor decorations for *bufnr* from the daemon.
function M.apply(bufnr)
  bufnr = bufnr or vim.api.nvim_get_current_buf()
  if not vim.api.nvim_buf_is_loaded(bufnr) then
    return
  end
  local path = vim.api.nvim_buf_get_name(bufnr)
  if path == "" then
    return
  end
  require("tyo3").rpc(bufnr, "decorate", { path = path }, function(err, items)
    if err or type(items) ~= "table" then
      return
    end
    for _, item in ipairs(items) do
      M.name_cache[item.durable_id] = {
        name = item.name,
        qualified_name = item.qualified_name,
        kind = item.kind,
        file = path,
      }
    end
    if vim.api.nvim_buf_is_loaded(bufnr) then
      place(bufnr, items)
    end
  end)
end

--- Clear decorations from *bufnr*.
function M.clear(bufnr)
  bufnr = bufnr or vim.api.nvim_get_current_buf()
  if vim.api.nvim_buf_is_loaded(bufnr) then
    vim.api.nvim_buf_clear_namespace(bufnr, M.ns, 0, -1)
  end
end

return M
