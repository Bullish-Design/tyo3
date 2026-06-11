-- tyo3.nvim — :checkhealth tyo3.
--
-- Reports the daemon command resolution and, for the current buffer's project,
-- pings the daemon for liveness + version. Run from inside a project buffer for
-- the live check.

local M = {}

local function start()
  return (vim.health.start or vim.health.report_start)
end
local function ok(msg)
  return (vim.health.ok or vim.health.report_ok)(msg)
end
local function warn(msg)
  return (vim.health.warn or vim.health.report_warn)(msg)
end
local function info(msg)
  return (vim.health.info or vim.health.report_info)(msg)
end
local function err(msg)
  return (vim.health.error or vim.health.report_error)(msg)
end

function M.check()
  start()("tyo3.nvim")

  -- Curated dependency stack (opt out via `setup{ manage = false }`).
  local cfg0 = require("tyo3.config").get()
  if cfg0.manage == false then
    info("dependency management disabled (manage = false) — you own the stack")
  else
    local missing = require("tyo3.deps").missing or {}
    if #missing == 0 then
      ok("curated dependency stack configured (snacks / tiny-code-action / edgy / treesitter / treewalker)")
    else
      for _, m in ipairs(missing) do
        err(("%s missing — required for %s"):format(m.mod, m.role))
      end
    end
  end

  -- Daemon command resolution.
  local cfg = require("tyo3.config").get()
  if cfg.daemon_cmd then
    info("daemon_cmd (configured): " .. table.concat(cfg.daemon_cmd, " "))
  elseif vim.fn.executable("tyo3-daemon") == 1 then
    ok("tyo3-daemon found on PATH")
  elseif vim.fn.executable("python") == 1 then
    warn("tyo3-daemon not on PATH — will fall back to `python -m tyo3.daemon`")
  else
    err("neither tyo3-daemon nor python found on PATH")
  end

  -- Live probe for the current buffer's project.
  local bufnr = vim.api.nvim_get_current_buf()
  local root = require("tyo3").root_for_buf(bufnr)
  if not root then
    info("current buffer is not inside a TyO3 project (open a project file to probe the daemon)")
    return
  end
  info("project root: " .. root)
  info("socket: " .. require("tyo3.daemon").socket_path(root))

  -- Native LSP bridge (always on — rides vim.lsp / vim.diagnostic).
  local attached = vim.lsp.get_clients({ name = "tyo3", bufnr = bufnr })
  if #attached > 0 then
    ok(("native LSP bridge — client attached (encoding %s)"):format(attached[1].offset_encoding))
  else
    info("native LSP bridge — no client attached to this buffer yet")
  end

  -- Synchronous-ish probe: ensure + ping, waiting briefly.
  local done, result, errmsg = false, nil, nil
  require("tyo3").rpc(bufnr, "ping", {}, function(e, r)
    done, result, errmsg = true, r, e and (e.message or "error") or nil
  end)
  vim.wait(5000, function()
    return done
  end, 50)

  if not done then
    err("daemon did not respond to ping within 5s")
  elseif errmsg then
    err("ping failed: " .. errmsg)
  elseif result and result.ok then
    ok(("daemon reachable — engine %s, head rev %s"):format(result.engine_version or "?", tostring(result.revision)))
    ok(("%d RPC methods available"):format(#(result.methods or {})))
  else
    warn("daemon responded but ping was not ok")
  end
end

return M
