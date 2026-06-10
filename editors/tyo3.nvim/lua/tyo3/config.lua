-- tyo3.nvim — configuration.
--
-- One table of defaults, merged with the user's `setup{}` opts. Read everywhere
-- via `require("tyo3.config").get()`.

local M = {}

M.defaults = {
  -- Spawn a daemon automatically when a Python buffer in a project is opened.
  auto_start = true,
  -- Debounce window (ms) before a TextChanged commit fires. One commit per pause.
  debounce_ms = 300,
  -- Affected-set precision hint surfaced in :checkhealth (the daemon owns the
  -- real setting via the project's .tyo3/config.toml).
  precision = "method",
  -- Render authored notes + derived summaries inline as virtual text.
  virtual_text = true,
  -- Affected-set panel: "auto" (open on first delta), "always", or "off".
  panel = "auto",
  -- Cursor-context section in the panel: "cursor" (auto-update from the entity
  -- under the cursor) or "off". Default off so existing behaviour is unchanged.
  context = "off",
  -- Opt-in native LSP bridge: run an in-process `vim.lsp` server that forwards
  -- hover / references / documentHighlight / rename / pull-diagnostics to the
  -- daemon, so the user's own `K`/`grr`/`]d`/Trouble/pickers drive tyo3. Default
  -- off; the bespoke UI is unchanged either way. See lua/tyo3/lsp.lua.
  lsp = false,
  -- Surface spine layer state (needs_review / orphaned) as a dedicated
  -- `vim.diagnostic` namespace so `]d`/`[d`/Trouble/lualine navigate it. This is
  -- the durable-identity half that has no LSP vocabulary. `nil` ⇒ follow `lsp`
  -- (on when the bridge is on); set `true`/`false` to decouple it from the bridge.
  layer_diagnostics = nil,
  -- Debounce (ms) before the cursor-context lookup fires.
  context_debounce_ms = 150,
  -- Command used to launch the daemon. nil ⇒ auto-detect `tyo3-daemon`, else
  -- fall back to `python -m tyo3.daemon`. Override e.g. {"uv","run","tyo3-daemon"}.
  daemon_cmd = nil,
  -- Project-root markers, searched upward from the buffer.
  root_markers = { ".tyo3", "pyproject.toml", ".git" },
  -- Opt-in overseer.nvim integration (daemon supervision + ops task templates).
  overseer = false,
  -- Notify level floor: "debug" | "info" | "warn" | "error".
  log_level = "info",
}

M.options = vim.deepcopy(M.defaults)

function M.setup(opts)
  M.options = vim.tbl_deep_extend("force", vim.deepcopy(M.defaults), opts or {})
  return M.options
end

function M.get()
  return M.options
end

--- Whether layer-state diagnostics are enabled: the explicit `layer_diagnostics`
--- flag if set, else it follows `lsp`.
function M.layer_diagnostics_enabled()
  local o = M.options
  if o.layer_diagnostics ~= nil then
    return o.layer_diagnostics
  end
  return o.lsp
end

return M
