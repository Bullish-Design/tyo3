-- tyo3.nvim — JSON-RPC 2.0 client over a unix-domain pipe (vim.uv).
--
-- Newline-delimited frames: one JSON object per line. Requests carry an `id`
-- and resolve their callback on the matching response; notifications (no `id`)
-- are dispatched to `on_notification`. All user-facing callbacks run on the main
-- loop (via vim.schedule) so they may touch buffers freely.

local uv = vim.uv or vim.loop

local M = {}

local Client = {}
Client.__index = Client

--- Create (but do not connect) a client.
--- opts = { socket = path, on_notification = fn(method, params), on_close = fn() }
function M.new(opts)
  return setmetatable({
    socket = opts.socket,
    on_notification = opts.on_notification,
    on_close = opts.on_close,
    pipe = nil,
    connected = false,
    next_id = 0,
    pending = {},
    buffer = "",
  }, Client)
end

function Client:connect(cb)
  local pipe = uv.new_pipe(false)
  self.pipe = pipe
  pipe:connect(self.socket, function(err)
    if err then
      pcall(function()
        pipe:close()
      end)
      self.pipe = nil
      vim.schedule(function()
        cb(err)
      end)
      return
    end
    self.connected = true
    pipe:read_start(function(rerr, chunk)
      if rerr then
        self:_on_close(rerr)
      elseif chunk then
        self:_on_data(chunk)
      else
        self:_on_close(nil) -- EOF
      end
    end)
    vim.schedule(function()
      cb(nil)
    end)
  end)
end

function Client:_on_data(chunk)
  self.buffer = self.buffer .. chunk
  while true do
    local nl = self.buffer:find("\n", 1, true)
    if not nl then
      break
    end
    local line = self.buffer:sub(1, nl - 1)
    self.buffer = self.buffer:sub(nl + 1)
    if #line > 0 then
      self:_dispatch(line)
    end
  end
end

function Client:_dispatch(line)
  vim.schedule(function()
    local ok, obj = pcall(vim.json.decode, line)
    if not ok or type(obj) ~= "table" then
      return
    end
    if obj.id ~= nil then
      local cb = self.pending[obj.id]
      self.pending[obj.id] = nil
      if cb then
        if obj.error ~= nil then
          cb(obj.error, nil)
        else
          cb(nil, obj.result)
        end
      end
    elseif obj.method ~= nil and self.on_notification then
      self.on_notification(obj.method, obj.params or {})
    end
  end)
end

function Client:_on_close(_err)
  if not self.connected and vim.tbl_isempty(self.pending) then
    return
  end
  self.connected = false
  local pending = self.pending
  self.pending = {}
  for _, cb in pairs(pending) do
    vim.schedule(function()
      cb({ code = -32099, message = "connection closed" }, nil)
    end)
  end
  if self.on_close then
    vim.schedule(self.on_close)
  end
end

--- Send a request. `cb(err, result)` is optional (omit for fire-and-forget).
function Client:request(method, params, cb)
  if not self.connected or not self.pipe then
    if cb then
      vim.schedule(function()
        cb({ code = -32099, message = "not connected" }, nil)
      end)
    end
    return
  end
  self.next_id = self.next_id + 1
  local id = self.next_id
  if cb then
    self.pending[id] = cb
  end
  -- An empty params table must encode as a JSON object, not [].
  if params == nil or (type(params) == "table" and vim.tbl_isempty(params)) then
    params = vim.empty_dict()
  end
  local frame = vim.json.encode({ jsonrpc = "2.0", id = id, method = method, params = params })
  self.pipe:write(frame .. "\n")
end

function Client:close()
  self.connected = false
  local pipe = self.pipe
  self.pipe = nil
  if pipe then
    pcall(function()
      pipe:read_stop()
    end)
    pcall(function()
      pipe:close()
    end)
  end
  local pending = self.pending
  self.pending = {}
  for _, cb in pairs(pending) do
    vim.schedule(function()
      cb({ code = -32099, message = "closed" }, nil)
    end)
  end
end

return M
