-- tyo3.nvim — public entry point and orchestration.
--
-- `setup{}` wires defaults; the buffer lifecycle (open / debounced sync / write)
-- and notification routing live here. Feature modules (decorate, inspect, panel,
-- notes, telescope, move) call back through `M.rpc` / `M.with_client`.

local config = require("tyo3.config")
local daemon = require("tyo3.daemon")

local M = {}

-- bufnr -> root (cached); bufnr -> uv timer for debounce.
M._root_by_buf = {}
M._debounce = {}
M._setup_done = false

local function buf_path(bufnr)
  local name = vim.api.nvim_buf_get_name(bufnr)
  if name == nil or name == "" then
    return nil
  end
  return name
end

--- The project root for *bufnr*, or nil if the buffer is not in a TyO3 project.
function M.root_for_buf(bufnr)
  bufnr = bufnr or vim.api.nvim_get_current_buf()
  if M._root_by_buf[bufnr] ~= nil then
    local r = M._root_by_buf[bufnr]
    return r ~= false and r or nil
  end
  local path = buf_path(bufnr)
  local root = path and daemon.find_root(path) or nil
  M._root_by_buf[bufnr] = root or false
  return root
end

--- Run *cb(client, root)* once a ready daemon exists for *bufnr*'s project.
function M.with_client(bufnr, cb, on_err)
  local root = M.root_for_buf(bufnr)
  if not root then
    if on_err then
      on_err("buffer is not inside a TyO3 project")
    end
    return
  end
  daemon.ensure(root, function(err, client)
    if err then
      if on_err then
        on_err(err.message or "daemon unavailable")
      end
      return
    end
    cb(client, root)
  end)
end

--- Send an RPC for *bufnr*'s project. `cb(err, result)`.
function M.rpc(bufnr, method, params, cb)
  M.with_client(bufnr, function(client)
    client:request(method, params, cb)
  end, function(msg)
    if cb then
      cb({ message = msg }, nil)
    end
  end)
end

-- ── Buffer lifecycle ────────────────────────────────────────────────────────

local function buffer_text(bufnr)
  local lines = vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)
  return table.concat(lines, "\n") .. "\n"
end

--- BufEnter / BufReadPost: open, push the buffer text as the overlay, decorate.
function M.on_buf_enter(bufnr)
  local root = M.root_for_buf(bufnr)
  if not root then
    return
  end
  if vim.bo[bufnr].filetype ~= "python" then
    return
  end
  local path = buf_path(bufnr)
  M.with_client(bufnr, function(client)
    client:request("open", { root = root }, function()
      client:request("sync_buffer", { path = path, text = buffer_text(bufnr) }, function()
        require("tyo3.decorate").apply(bufnr)
      end)
    end)
  end, function(_) end)
end

--- TextChanged / TextChangedI: debounce, then commit the buffer as the overlay.
function M.on_text_changed(bufnr)
  local root = M.root_for_buf(bufnr)
  if not root or vim.bo[bufnr].filetype ~= "python" then
    return
  end
  local existing = M._debounce[bufnr]
  if existing then
    existing:stop()
    existing:close()
    M._debounce[bufnr] = nil
  end
  local uv = vim.uv or vim.loop
  local timer = uv.new_timer()
  M._debounce[bufnr] = timer
  timer:start(
    config.get().debounce_ms,
    0,
    vim.schedule_wrap(function()
      timer:stop()
      timer:close()
      M._debounce[bufnr] = nil
      if not vim.api.nvim_buf_is_loaded(bufnr) then
        return
      end
      M.sync_now(bufnr)
    end)
  )
end

--- Force a sync of *bufnr* now (BufWritePost, or after the debounce fires).
function M.sync_now(bufnr)
  local path = buf_path(bufnr)
  if not path then
    return
  end
  M.rpc(bufnr, "sync_buffer", { path = path, text = buffer_text(bufnr) }, function(err, _delta)
    if not err then
      require("tyo3.decorate").apply(bufnr)
    end
  end)
end

-- ── Notification routing (from the bus pump) ────────────────────────────────

function M.handle_notification(root, method, params)
  if method == "delta" then
    require("tyo3.panel").on_delta(root, params)
    -- Re-anchor decorations on every loaded buffer of this project: identity is
    -- the truth; never trust drifted extmarks across a structural edit.
    for _, bufnr in ipairs(vim.api.nvim_list_bufs()) do
      if vim.api.nvim_buf_is_loaded(bufnr) and M.root_for_buf(bufnr) == root then
        require("tyo3.decorate").apply(bufnr)
      end
    end
  elseif method == "derived" then
    -- A slow `serving="stale"` layer's value became fresh off the actor (AB3):
    -- re-pull the card (if the panel is on that entity) and re-decorate every
    -- loaded buffer of this project so an inline summary swaps stale → fresh.
    require("tyo3.panel").on_derived(root, params)
    for _, bufnr in ipairs(vim.api.nvim_list_bufs()) do
      if vim.api.nvim_buf_is_loaded(bufnr) and M.root_for_buf(bufnr) == root then
        require("tyo3.decorate").apply(bufnr)
      end
    end
  elseif method == "refinement" then
    require("tyo3.panel").on_refinement(root, params)
  end
end

-- ── setup ───────────────────────────────────────────────────────────────────

function M.setup(opts)
  config.setup(opts)
  if not M._setup_done then
    require("tyo3.decorate").setup_highlights()
    M._setup_done = true
  end
  return M
end

function M.shutdown()
  daemon.shutdown_all()
end

return M
