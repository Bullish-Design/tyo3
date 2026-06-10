-- tyo3.nvim — public entry point and orchestration.
--
-- `setup{}` wires defaults; the buffer lifecycle (open / debounced sync / write)
-- and notification routing live here. Feature modules (decorate, inspect, panel,
-- notes, telescope, move) call back through `M.rpc` / `M.with_client`.

local config = require("tyo3.config")
local daemon = require("tyo3.daemon")

local M = {}

-- bufnr -> root (cached); bufnr -> uv timer for debounce;
-- bufnr -> sha256 of the last-synced overlay text (double-commit dedup).
M._root_by_buf = {}
M._debounce = {}
M._last_synced = {}
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
  local text = buffer_text(bufnr)
  M.with_client(bufnr, function(client)
    client:request("open", { root = root }, function()
      client:request("sync_buffer", { path = path, text = text }, function()
        -- Seed the dedup hash so the first `:w` of an unedited buffer is a no-op.
        M._last_synced[bufnr] = vim.fn.sha256(text)
        require("tyo3.decorate").apply(bufnr)
      end)
    end)
  end, function(_) end)
  -- Opt-in: attach the native LSP bridge (additive; idempotent via lsp.start
  -- dedupe). Independent of the open/sync chain above — the in-process server
  -- resolves the daemon client lazily on its first request.
  if config.get().lsp then
    require("tyo3.lsp").attach(bufnr, root)
  end
  -- Seed layer-state diagnostics once on open (refreshed thereafter off the bus).
  if config.layer_diagnostics_enabled() then
    require("tyo3.lsp").refresh_layer_diagnostics(bufnr, root)
  end
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
--
-- The editor commits twice per save: the debounced TextChanged sync, then the
-- BufWritePost sync of identical bytes. A redundant re-commit re-reconciles the
-- file and would clear durable level state (e.g. needs_review) on the engine.
-- Dedup on a per-buffer content hash so an unchanged buffer is never re-synced.
function M.sync_now(bufnr)
  local path = buf_path(bufnr)
  if not path then
    return
  end
  local text = buffer_text(bufnr)
  local h = vim.fn.sha256(text)
  if h == M._last_synced[bufnr] then
    return
  end
  M.rpc(bufnr, "sync_buffer", { path = path, text = text }, function(err, _delta)
    if not err then
      M._last_synced[bufnr] = h
      require("tyo3.decorate").apply(bufnr)
    end
  end)
end

-- ── Notification routing (from the bus pump) ────────────────────────────────

-- Refresh layer-state diagnostics on every loaded buffer of *root* (gated by the
-- layer_diagnostics flag). Mirrors the decorate fan-out loop.
local function refresh_layer_diags_for_root(root)
  if not config.layer_diagnostics_enabled() then
    return
  end
  local lsp = require("tyo3.lsp")
  for _, bufnr in ipairs(vim.api.nvim_list_bufs()) do
    if vim.api.nvim_buf_is_loaded(bufnr) and M.root_for_buf(bufnr) == root then
      lsp.refresh_layer_diagnostics(bufnr, root)
    end
  end
end

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
    -- Push type-checker diagnostics for the touched files so they refresh on
    -- edit without the editor polling (server→client publishDiagnostics).
    if config.get().lsp then
      local touched = params.touched_files or params.affected_files
      require("tyo3.lsp").publish_diagnostics(root, touched)
    end
    -- A structural edit can flip authored notes to needs_review.
    refresh_layer_diags_for_root(root)
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
    refresh_layer_diags_for_root(root)
  elseif method == "refinement" then
    require("tyo3.panel").on_refinement(root, params)
    refresh_layer_diags_for_root(root)
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
