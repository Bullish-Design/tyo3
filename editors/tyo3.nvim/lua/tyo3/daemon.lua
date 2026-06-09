-- tyo3.nvim — per-project daemon lifecycle.
--
-- Finds a buffer's project root, derives a stable per-root socket path, and
-- ensures exactly one `tyo3-daemon` per root. `ensure(root, cb)` connects to a
-- running daemon if one is already listening, otherwise spawns it and polls the
-- socket until it comes up — then hands back a ready RPC client. One daemon +
-- one client is shared across every split/pane on the same project.

local uv = vim.uv or vim.loop
local rpc = require("tyo3.rpc")
local config = require("tyo3.config")

local M = {}

-- root -> { status = "connecting"|"ready"|"down", client, socket, job, waiters,
--           attempting, attempts, timer, log = {} }
M._by_root = {}

--- Resolve the project root for an absolute file *path* (or nil).
function M.find_root(path)
  if not path or path == "" then
    return nil
  end
  local start = vim.fs.dirname(path)
  local markers = config.get().root_markers
  local found = vim.fs.find(markers, { upward = true, path = start, limit = 1 })
  if found[1] then
    return vim.fs.dirname(found[1])
  end
  return nil
end

local function socket_dir()
  local rundir = os.getenv("XDG_RUNTIME_DIR")
  local base = rundir and (rundir .. "/tyo3") or (vim.fn.stdpath("cache") .. "/tyo3")
  vim.fn.mkdir(base, "p")
  return base
end

--- Stable per-root socket path. The plugin passes this to `--socket`, so it owns
--- the path (no need to mirror the daemon's own default-hash scheme).
function M.socket_path(root)
  local hash = vim.fn.sha256(root):sub(1, 16)
  return socket_dir() .. "/" .. hash .. ".sock"
end

local function daemon_cmd(root, sock)
  local cfg = config.get()
  local base = cfg.daemon_cmd
  if base == nil then
    if vim.fn.executable("tyo3-daemon") == 1 then
      base = { "tyo3-daemon" }
    else
      base = { "python", "-m", "tyo3.daemon" }
    end
  end
  local cmd = vim.deepcopy(base)
  vim.list_extend(cmd, { "--root", root, "--socket", sock, "--print-socket" })
  return cmd
end

local function notify(msg, level)
  vim.schedule(function()
    vim.notify("[tyo3] " .. msg, level or vim.log.levels.INFO)
  end)
end

local function finish(root, err, client)
  local st = M._by_root[root]
  if not st then
    return
  end
  local waiters = st.waiters or {}
  st.waiters = {}
  if err then
    st.status = "down"
  end
  for _, cb in ipairs(waiters) do
    pcall(cb, err, client)
  end
end

local function new_client(root, st)
  return rpc.new({
    socket = st.socket,
    on_notification = function(method, params)
      require("tyo3").handle_notification(root, method, params)
    end,
    on_close = function()
      local s = M._by_root[root]
      if s and s.status == "ready" then
        s.status = "down"
        s.client = nil
      end
    end,
  })
end

-- One connect attempt; on success, mark the root ready and flush waiters.
local function try_connect(root, st, on_done)
  local client = new_client(root, st)
  client:connect(function(err)
    if err then
      pcall(function()
        client:close()
      end)
      on_done(err)
    else
      st.client = client
      st.status = "ready"
      on_done(nil, client)
    end
  end)
end

local function start_spawn(root, st)
  local cmd = daemon_cmd(root, st.socket)
  st.log = {}
  local job = vim.fn.jobstart(cmd, {
    on_stderr = function(_, data)
      for _, line in ipairs(data or {}) do
        if line and #line > 0 then
          table.insert(st.log, line)
          if #st.log > 200 then
            table.remove(st.log, 1)
          end
        end
      end
    end,
    on_exit = function(_, code)
      local s = M._by_root[root]
      if s then
        s.status = "down"
        s.client = nil
        s.job = nil
      end
      if code ~= 0 then
        notify(("daemon for %s exited (code %d)"):format(vim.fn.fnamemodify(root, ":t"), code), vim.log.levels.WARN)
      end
    end,
  })
  if job <= 0 then
    finish(root, { message = "failed to spawn tyo3-daemon (is it on PATH? set daemon_cmd)" })
    return
  end
  st.job = job

  -- Poll the socket until the daemon is listening, then connect.
  st.attempts = 0
  local timer = uv.new_timer()
  st.timer = timer
  timer:start(
    150,
    150,
    vim.schedule_wrap(function()
      local s = M._by_root[root]
      if not s or s.status ~= "connecting" then
        timer:stop()
        timer:close()
        return
      end
      s.attempts = (s.attempts or 0) + 1
      if s.attempts > 400 then -- ~60s (a cold first sync_all can be slow)
        timer:stop()
        timer:close()
        finish(root, { message = "daemon did not come up within timeout" })
        return
      end
      if s.attempting then
        return
      end
      if not uv.fs_stat(s.socket) then
        return -- socket not bound yet
      end
      s.attempting = true
      try_connect(root, s, function(err, client)
        s.attempting = false
        if not err then
          timer:stop()
          timer:close()
          finish(root, nil, client)
        end
      end)
    end)
  )
end

--- Ensure a ready daemon+client for *root*; cb(err, client).
function M.ensure(root, cb)
  local st = M._by_root[root]
  if st and st.status == "ready" and st.client then
    cb(nil, st.client)
    return
  end
  if st and st.status == "connecting" then
    table.insert(st.waiters, cb)
    return
  end
  st = { status = "connecting", waiters = { cb }, socket = M.socket_path(root) }
  M._by_root[root] = st

  -- Try an already-running daemon first; only spawn if that fails.
  try_connect(root, st, function(err, client)
    if not err then
      finish(root, nil, client)
    else
      if not config.get().auto_start then
        finish(root, { message = "no daemon running and auto_start=false" })
        return
      end
      start_spawn(root, st)
    end
  end)
end

--- The ready client for *root*, or nil.
function M.client_for(root)
  local st = M._by_root[root]
  if st and st.status == "ready" then
    return st.client
  end
  return nil
end

--- The captured daemon stderr log lines for *root* (for overseer / debugging).
function M.log_for(root)
  local st = M._by_root[root]
  return st and st.log or {}
end

--- Stop every daemon this editor session spawned and close clients.
function M.shutdown_all()
  for _, st in pairs(M._by_root) do
    if st.timer then
      pcall(function()
        st.timer:stop()
        st.timer:close()
      end)
    end
    if st.client then
      pcall(function()
        st.client:close()
      end)
    end
    if st.job then
      pcall(function()
        vim.fn.jobstop(st.job)
      end)
    end
  end
  M._by_root = {}
end

return M
