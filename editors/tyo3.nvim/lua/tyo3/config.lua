-- tyo3.nvim — configuration.
--
-- One table of defaults, merged with the user's `setup{}` opts. Read everywhere
-- via `require("tyo3.config").get()`.

local M = {}

M.defaults = {
  -- Whether TyO3 owns the curated dependency stack (snacks / tiny-code-action /
  -- edgy / treesitter / treewalker) — configured on `setup`. Opt out with
  -- `manage = false` to own the stack yourself; TyO3 still works either way.
  -- See lua/tyo3/deps.lua.
  manage = true,
  -- Buffer-local, project-scoped keymaps for the curated navigate/observe/act
  -- loop. Defaults are filled in by Phases C/D (action picker + AST motion);
  -- empty for now. Set per-feature tables to override or `false` to opt out.
  keymaps = {},
  -- Debounce window (ms) before a TextChanged commit fires. One commit per pause.
  debounce_ms = 300,
  -- Affected-set precision hint surfaced in :checkhealth (the daemon owns the
  -- real setting via the project's .tyo3/config.toml).
  precision = "method",
  -- Render authored notes + derived summaries inline as virtual text.
  virtual_text = true,
  -- AI actions (explain / simplify) are *project-config-driven*, not a client
  -- flag: they appear only when the project declares the `explain` layer, and
  -- the LLM backend stays optional (anthropic → callable → stub). There is no
  -- `ai`/`explain` toggle here — declare the layer in the project to enable them.
  -- Debounce (ms) before the cursor-context lookup fires.
  context_debounce_ms = 150,
  -- Per-request timeout (ms) for daemon RPCs. A stalled request errors its
  -- callback instead of hanging the UI forever. Slow verbs (explain/check) can
  -- override per-call. Set 0 to disable.
  request_timeout_ms = 20000,
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

return M
