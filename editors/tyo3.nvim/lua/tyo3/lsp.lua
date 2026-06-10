-- tyo3.nvim — native LSP bridge (opt-in via `setup{ lsp = true }`).
--
-- Runs an *in-process* `vim.lsp` server (no separate process, no Content-Length
-- framing) whose request handlers forward to the per-project tyo3 daemon over
-- the existing JSON-RPC client. The point is to ride Neovim's native machinery:
-- `K` hover, `grr`/`gra`, `]d`/`[d`, Trouble, lualine, fzf-lua/snacks/telescope
-- LSP pickers all work against tyo3 for free, inheriting the user's own config.
--
-- This is additive and opt-in. The bespoke UI (panel / inspect / decorate /
-- telescope) is untouched; with `lsp = false` (the default) nothing here runs.
--
-- Position conventions (verified empirically against the engine):
--   * daemon positions are 1-based; LSP positions are 0-based.
--   * daemon ranges are 1-based with an *exclusive* end column (a symbol at
--     columns 5..12 reports end.column = 13), so the LSP conversion is a uniform
--     "subtract 1 from every line and column" — for start AND end alike. A rename
--     TextEdit built this way clobbers exactly the symbol, no off-by-one.
--   * columns are Unicode codepoints, so we advertise positionEncoding = "utf-32"
--     and Neovim hands us codepoint columns (ASCII is identical under all three).

local daemon = require("tyo3.daemon")

local M = {}

-- ── Capabilities (advertise only what we implement) ──────────────────────────

local CAPS = {
  positionEncoding = "utf-32", -- daemon columns are Unicode codepoints
  hoverProvider = true,
  definitionProvider = true,
  referencesProvider = true,
  documentHighlightProvider = true,
  typeHierarchyProvider = true,
  -- nvim 0.12's capability map gates the type-hierarchy follow-up requests on
  -- capability keys *literally named* "typeHierarchy/supertypes" / "…/subtypes"
  -- (not the documented typeHierarchyProvider). Advertise both so the client's
  -- supports_method() lets the requests through.
  ["typeHierarchy/supertypes"] = true,
  ["typeHierarchy/subtypes"] = true,
  renameProvider = { prepareProvider = true },
  -- Pull diagnostics stay advertised so `vim.diagnostic` on-demand pulls work;
  -- Phase 2 also *pushes* via dispatchers.notification on bus deltas. Pull is
  -- on-demand, push is event-driven — in practice nvim 0.12 doesn't fire both
  -- for the same edit (nothing auto-pulls), so they don't double-count.
  diagnosticProvider = {
    interFileDependencies = false,
    workspaceDiagnostics = false,
  },
}

-- ── Position / range conversion ──────────────────────────────────────────────

-- LSP position (0-based) → daemon position (1-based).
local function lsp_pos_to_daemon(pos)
  return { line = pos.line + 1, col = pos.character + 1 }
end

-- daemon range (1-based, exclusive end) → LSP range (0-based, exclusive end).
local function daemon_range_to_lsp(r)
  return {
    start = { line = r.start.line - 1, character = r.start.column - 1 },
    ["end"] = { line = r["end"].line - 1, character = r["end"].column - 1 },
  }
end

-- ReferenceKind → LSP DocumentHighlightKind.
local DH_KIND = { read = 2, write = 3, other = 1 }

-- DiagnosticSeverity (StrEnum string) → LSP DiagnosticSeverity.
local DIAG_SEVERITY = { fatal = 1, error = 1, warning = 2, information = 3, hint = 4 }

-- Map the daemon `check` diagnostics list to LSP `Diagnostic[]`. Shared by the
-- pull handler (textDocument/diagnostic) and the push path (publishDiagnostics).
local function daemon_diags_to_lsp(diags)
  local items = {}
  for _, d in ipairs(diags or {}) do
    if d.range then
      table.insert(items, {
        range = daemon_range_to_lsp(d.range),
        severity = DIAG_SEVERITY[d.severity] or 1,
        code = d.code,
        message = d.message,
        source = "tyo3",
      })
    end
  end
  return items
end

-- ── URI <-> path translation ─────────────────────────────────────────────────

-- The daemon emits project-relative posix paths and accepts absolute paths
-- (it relativizes). `root` anchors both directions.
local function uri_to_path(uri)
  return vim.uri_to_fname(uri)
end

-- The daemon is inconsistent about path shape on output: navigation verbs
-- (references / rename `changes`) hand back *absolute* paths, while others use a
-- project-relative posix path. Accept either — anchor a relative path at `root`.
local function path_to_uri(root, p)
  local abs = p:sub(1, 1) == "/" and p or (root .. "/" .. p)
  return vim.uri_from_fname(abs)
end

-- ── Daemon access ────────────────────────────────────────────────────────────

-- Fire a daemon verb for `root`; `cb(err, result)`. The daemon client is shared
-- with the rest of the plugin (one per project root); `ensure` returns the ready
-- client immediately if it already exists, else connects/spawns first.
local function daemon_request(root, method, params, cb)
  daemon.ensure(root, function(err, client)
    if err then
      cb(err, nil)
      return
    end
    client:request(method, params, function(rerr, res)
      -- A JSON `null` result (e.g. hover on a keyword, rename of a non-symbol)
      -- decodes to vim.NIL — a *truthy* userdata. Normalise it to Lua nil so
      -- handlers can treat "no result" uniformly with `not res`.
      if res == vim.NIL then
        res = nil
      end
      cb(rerr, res)
    end)
  end)
end

local function lsp_error(err)
  return { code = err.code or -32603, message = err.message or "tyo3 daemon error" }
end

-- ── Hover formatting ─────────────────────────────────────────────────────────

-- Join the daemon's typed hover contents into one markdown blob. type/signature
-- segments render as Python code fences; everything else stays as-is.
local function hover_markdown(contents)
  local parts = {}
  for _, c in ipairs(contents or {}) do
    local v = c.value
    if v ~= nil and v ~= "" then
      if c.kind == "type" or c.kind == "signature" then
        table.insert(parts, "```python\n" .. v .. "\n```")
      else
        table.insert(parts, v)
      end
    end
  end
  return table.concat(parts, "\n\n")
end

-- ── Request handlers (method → function(root, params, reply)) ─────────────────
--
-- `reply(err, result)` resolves the LSP request; `err` is an LSP ResponseError
-- (or nil). Daemon failures map to an error; an absent result (null) is a
-- successful empty response, which Neovim renders as "no information".

local handlers = {}

handlers["textDocument/hover"] = function(root, params, reply)
  local p = lsp_pos_to_daemon(params.position)
  daemon_request(root, "hover", {
    path = uri_to_path(params.textDocument.uri),
    line = p.line,
    col = p.col,
  }, function(err, res)
    if err then
      reply(lsp_error(err))
    elseif not res then
      reply(nil, nil)
    else
      local hover = { contents = { kind = "markdown", value = hover_markdown(res.contents) } }
      if res.location and res.location.range then
        hover.range = daemon_range_to_lsp(res.location.range)
      end
      reply(nil, hover)
    end
  end)
end

handlers["textDocument/references"] = function(root, params, reply)
  local p = lsp_pos_to_daemon(params.position)
  local include_decl = true
  if params.context and params.context.includeDeclaration ~= nil then
    include_decl = params.context.includeDeclaration
  end
  daemon_request(root, "references", {
    path = uri_to_path(params.textDocument.uri),
    line = p.line,
    col = p.col,
    include_declaration = include_decl,
  }, function(err, res)
    if err then
      reply(lsp_error(err))
      return
    end
    local out = {}
    for _, r in ipairs((res and res.references) or {}) do
      table.insert(out, { uri = path_to_uri(root, r.path), range = daemon_range_to_lsp(r.range) })
    end
    reply(nil, out)
  end)
end

handlers["textDocument/definition"] = function(root, params, reply)
  local p = lsp_pos_to_daemon(params.position)
  daemon_request(root, "definition", {
    path = uri_to_path(params.textDocument.uri),
    line = p.line,
    col = p.col,
  }, function(err, res)
    if err then
      reply(lsp_error(err))
      return
    end
    local out = {}
    for _, t in ipairs((res and res.definitions) or {}) do
      -- Prefer selection_range so the cursor lands on the name token, not the
      -- `def`/`class` keyword (decorate-style full range starts at col 1).
      local rng = t.selection_range or t.range
      table.insert(out, { uri = path_to_uri(root, t.path), range = daemon_range_to_lsp(rng) })
    end
    reply(nil, out)
  end)
end

-- ── Type hierarchy ───────────────────────────────────────────────────────────
--
-- The daemon `type_hierarchy` verb returns the *whole* thing at once
-- ({item, supertypes, subtypes}); LSP splits it across prepare / supertypes /
-- subtypes. The daemon is stateless and cheap, so we re-query rather than cache:
-- the LSP item we emit round-trips its uri + selectionRange back to us, which we
-- convert to a daemon position to re-run the query.

-- daemon TypeHierarchyItem → LSP TypeHierarchyItem (SymbolKind.Class = 5).
local function lsp_type_item(root, t)
  return {
    name = t.name,
    kind = 5,
    detail = t.detail,
    uri = path_to_uri(root, t.path),
    range = daemon_range_to_lsp(t.full_range),
    selectionRange = daemon_range_to_lsp(t.selection_range),
  }
end

handlers["textDocument/prepareTypeHierarchy"] = function(root, params, reply)
  local p = lsp_pos_to_daemon(params.position)
  daemon_request(root, "type_hierarchy", {
    path = uri_to_path(params.textDocument.uri),
    line = p.line,
    col = p.col,
  }, function(err, res)
    if err then
      reply(lsp_error(err))
    elseif not res or not res.item then
      reply(nil, nil)
    else
      reply(nil, { lsp_type_item(root, res.item) })
    end
  end)
end

-- supertypes / subtypes share a re-query off the incoming item's selectionRange.
local function type_hierarchy_relatives(field)
  return function(root, params, reply)
    local item = params.item
    if not item or not item.selectionRange then
      reply(nil, {})
      return
    end
    local p = lsp_pos_to_daemon(item.selectionRange.start)
    daemon_request(root, "type_hierarchy", {
      path = uri_to_path(item.uri),
      line = p.line,
      col = p.col,
    }, function(err, res)
      if err then
        reply(lsp_error(err))
        return
      end
      local out = {}
      for _, t in ipairs((res and res[field]) or {}) do
        table.insert(out, lsp_type_item(root, t))
      end
      reply(nil, out)
    end)
  end
end

handlers["typeHierarchy/supertypes"] = type_hierarchy_relatives("supertypes")
handlers["typeHierarchy/subtypes"] = type_hierarchy_relatives("subtypes")

handlers["textDocument/documentHighlight"] = function(root, params, reply)
  local p = lsp_pos_to_daemon(params.position)
  daemon_request(root, "document_highlights", {
    path = uri_to_path(params.textDocument.uri),
    line = p.line,
    col = p.col,
  }, function(err, res)
    if err then
      reply(lsp_error(err))
      return
    end
    local out = {}
    for _, h in ipairs((res and res.highlights) or {}) do
      table.insert(out, { range = daemon_range_to_lsp(h.range), kind = DH_KIND[h.kind] or 1 })
    end
    reply(nil, out)
  end)
end

handlers["textDocument/prepareRename"] = function(root, params, reply)
  local p = lsp_pos_to_daemon(params.position)
  daemon_request(root, "can_rename", {
    path = uri_to_path(params.textDocument.uri),
    line = p.line,
    col = p.col,
  }, function(err, res)
    if err then
      reply(lsp_error(err))
    elseif res and res.can_rename and res.range then
      reply(nil, daemon_range_to_lsp(res.range))
    else
      -- null → Neovim reports "nothing to rename" rather than prompting.
      reply(nil, nil)
    end
  end)
end

handlers["textDocument/rename"] = function(root, params, reply)
  local p = lsp_pos_to_daemon(params.position)
  daemon_request(root, "rename", {
    path = uri_to_path(params.textDocument.uri),
    line = p.line,
    col = p.col,
    new_name = params.newName,
  }, function(err, res)
    if err then
      reply(lsp_error(err))
    elseif not res then
      reply(nil, nil)
    else
      local changes = {}
      for relpath, edits in pairs(res.changes or {}) do
        local uri = path_to_uri(root, relpath)
        local text_edits = {}
        for _, e in ipairs(edits) do
          table.insert(text_edits, { range = daemon_range_to_lsp(e.range), newText = e.new_text })
        end
        changes[uri] = text_edits
      end
      reply(nil, { changes = changes })
    end
  end)
end

handlers["textDocument/diagnostic"] = function(root, params, reply)
  daemon_request(root, "check", {
    path = uri_to_path(params.textDocument.uri),
  }, function(err, res)
    if err then
      reply(lsp_error(err))
      return
    end
    reply(nil, { kind = "full", items = daemon_diags_to_lsp(res and res.diagnostics) })
  end)
end

-- ── In-process server object (the vim.lsp.rpc.PublicClient contract) ──────────

-- Per-root sink for server→client notifications (push diagnostics). Stashed by
-- the server `cmd` so `M.publish_diagnostics` can reach the live client.
M._dispatchers_by_root = M._dispatchers_by_root or {}

-- Build the server object `vim.lsp.start{ cmd = fn }` expects. `dispatchers` is
-- the client's notification/server-request sink; we stash it so the push path
-- (publishDiagnostics) can deliver server→client notifications.
function M._server(root, dispatchers)
  M._dispatchers_by_root[root] = dispatchers
  local closed = false
  local next_id = 0

  local function next_request_id()
    next_id = next_id + 1
    return next_id
  end

  return {
    request = function(method, params, callback, notify_reply_callback)
      local id = next_request_id()

      local function reply(err, result)
        if closed then
          return
        end
        if notify_reply_callback then
          pcall(notify_reply_callback, id)
        end
        callback(err, result, id)
      end

      if method == "initialize" then
        reply(nil, { capabilities = CAPS, serverInfo = { name = "tyo3" } })
      elseif method == "shutdown" then
        reply(nil, vim.NIL)
      else
        local h = handlers[method]
        if h then
          h(root, params, reply)
        else
          -- Unimplemented request: respond MethodNotFound so Neovim moves on.
          reply({ code = -32601, message = "method not found: " .. method })
        end
      end

      return true, id
    end,

    notify = function(method, _params)
      -- didOpen/didChange/didClose are no-ops: the plugin already syncs buffer
      -- text to the daemon on BufEnter and debounced TextChanged. `exit` closes.
      if method == "exit" then
        closed = true
        M._dispatchers_by_root[root] = nil
      end
      return true
    end,

    is_closing = function()
      return closed
    end,

    terminate = function()
      closed = true
      M._dispatchers_by_root[root] = nil
    end,
  }
end

-- ── Push diagnostics (server→client publishDiagnostics) ──────────────────────

--- Push type-checker diagnostics for *relpaths* to the client, off the bus.
--- Driven by `delta` notifications (init.lua), so check diagnostics refresh on
--- edit without the editor polling. Runs `check` per file (the ty type-checker
--- is ~hundreds of ms — pass only the touched files, not the whole project).
function M.publish_diagnostics(root, relpaths)
  local dispatchers = M._dispatchers_by_root[root]
  if not dispatchers or not dispatchers.notification then
    return -- no live tyo3 client for this root
  end
  for _, path in ipairs(relpaths or {}) do
    daemon_request(root, "check", { path = path }, function(err, res)
      if err then
        return
      end
      dispatchers.notification("textDocument/publishDiagnostics", {
        uri = path_to_uri(root, path),
        diagnostics = daemon_diags_to_lsp(res and res.diagnostics),
      })
    end)
  end
end

-- ── Layer-state diagnostics (the bespoke-half "go native" piece) ─────────────
--
-- needs_review / orphaned are durable-identity concepts with no LSP vocabulary,
-- so they ride a *dedicated* `vim.diagnostic` namespace (not an LSP method).
-- That gives `]d`/`[d`, setqflist, Trouble and lualine for free.

local LAYER_NS = vim.api.nvim_create_namespace("tyo3-layer")

-- needs_review → WARN (intent drifted, human review needed); orphaned → HINT.
local LAYER_SEVERITY = {
  needs_review = vim.diagnostic.severity.WARN,
  orphaned = vim.diagnostic.severity.HINT,
}
local LAYER_MESSAGE = {
  needs_review = "needs review (intent changed)",
  orphaned = "orphaned derived artifact",
}

--- The namespace layer diagnostics live in (exposed for tests/introspection).
function M.layer_namespace()
  return LAYER_NS
end

--- Refresh the tyo3-layer diagnostics for *bufnr* (project *root*) from
--- `review_state`. Default-off: only call this when the flag is on.
function M.refresh_layer_diagnostics(bufnr, root)
  local path = vim.api.nvim_buf_get_name(bufnr)
  if path == nil or path == "" then
    return
  end
  daemon_request(root, "review_state", { path = path }, function(err, res)
    if err or not res then
      return
    end
    local diags = {}
    for _, it in ipairs(res.items or {}) do
      local r = daemon_range_to_lsp(it.range) -- 0-based, uniform -1 rule
      table.insert(diags, {
        lnum = r.start.line,
        col = r.start.character,
        end_lnum = r["end"].line,
        end_col = r["end"].character,
        severity = LAYER_SEVERITY[it.state] or vim.diagnostic.severity.INFO,
        source = "tyo3",
        message = LAYER_MESSAGE[it.state] or it.state,
      })
    end
    if vim.api.nvim_buf_is_loaded(bufnr) then
      vim.diagnostic.set(LAYER_NS, bufnr, diags)
    end
  end)
end

-- ── Attach ────────────────────────────────────────────────────────────────────

--- Attach the in-process tyo3 LSP server to *bufnr* for project *root*.
--- Idempotent: `vim.lsp.start` dedupes by `{name, root_dir}`, so one server is
--- reused across every buffer of the same project.
function M.attach(bufnr, root)
  return vim.lsp.start({
    name = "tyo3",
    root_dir = root,
    cmd = function(dispatchers)
      return M._server(root, dispatchers)
    end,
  }, { bufnr = bufnr })
end

--- Flip the `lsp` flag at runtime (`:TyO3Lsp`). Enabling attaches every loaded
--- Python buffer in a known project; disabling stops the tyo3 clients. The
--- BufEnter autocmd keeps newly-opened buffers in sync with the flag.
function M.toggle()
  local cfg = require("tyo3.config").get()
  cfg.lsp = not cfg.lsp
  if cfg.lsp then
    local tyo3 = require("tyo3")
    local layer_on = require("tyo3.config").layer_diagnostics_enabled()
    for _, b in ipairs(vim.api.nvim_list_bufs()) do
      if vim.api.nvim_buf_is_loaded(b) and vim.bo[b].filetype == "python" then
        local root = tyo3.root_for_buf(b)
        if root then
          M.attach(b, root)
          -- Seed layer-state diagnostics so enabling reflects current state
          -- immediately (don't wait for the next bus delta to surface a
          -- pre-existing needs_review / orphaned).
          if layer_on then
            M.refresh_layer_diagnostics(b, root)
          end
        end
      end
    end
    vim.notify("[tyo3] native LSP bridge enabled", vim.log.levels.INFO)
  else
    for _, c in ipairs(vim.lsp.get_clients({ name = "tyo3" })) do
      c:stop()
    end
    vim.notify("[tyo3] native LSP bridge disabled", vim.log.levels.INFO)
  end
end

return M
