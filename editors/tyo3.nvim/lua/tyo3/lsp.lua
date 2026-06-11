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
  -- Code actions are the "act on what's under the cursor" trigger (proj 26).
  -- We return client-side commands (tyo3.explain / tyo3.ack), resolved through
  -- `vim.lsp.commands`, so tiny-code-action / `gra` / vim.lsp.buf.code_action()
  -- all drive them. Distinct kinds give tiny-code-action distinct icons and let
  -- `context.only` filter: Simplify is a refactor.rewrite, Explain an
  -- informational source.tyo3, the review-ack a quickfix.
  codeActionProvider = {
    -- Simplify carries `data` and no `edit`, resolved lazily via
    -- `codeAction/resolve` into a WorkspaceEdit — so the menu stays instant (no
    -- LLM per keystroke) and tiny-code-action shows the rewrite diff on focus.
    resolveProvider = true,
    codeActionKinds = { "refactor.rewrite", "quickfix", "source.tyo3" },
  },
  executeCommandProvider = { commands = { "tyo3.explain", "tyo3.run", "tyo3.ack" } },
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

-- The code-action kinds we emit. `context.only` is prefix-matched per LSP: a
-- request for "refactor" matches "refactor.rewrite". Return true if any tyo3
-- kind satisfies the filter (so a kind-scoped invocation still reaches us).
local TYO3_KINDS = { "refactor.rewrite", "quickfix", "source.tyo3" }
local function has_tyo3_kind(only)
  for _, want in ipairs(only or {}) do
    for _, k in ipairs(TYO3_KINDS) do
      if k == want or k:sub(1, #want + 1) == want .. "." then
        return true
      end
    end
  end
  return false
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
local function daemon_request(root, method, params, cb, opts)
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
    end, opts)
  end)
end

-- A real LLM-backed explain/simplify (or a cold `check`) legitimately runs past
-- the default per-request timeout, so those paths pass a longer ceiling.
local SLOW_VERB_OPTS = { timeout_ms = 60000 }

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

-- ── Code actions — an extensible registry (proj 26) ──────────────────────────
--
-- The code-action surface is a *registry of providers*, not a hardcoded list,
-- so a third-party spine plugin contributes actions with no fork of this file:
-- it calls `M.register_code_action` (or the `M.register_entity_action`
-- convenience) at setup, and its actions appear in `gra`, tiny-code-action, and
-- `vim.lsp.buf.code_action` like any other. We don't round-trip the daemon to
-- *offer* the actions — the work happens when the user picks one (a client
-- command, resolved through `vim.lsp.commands`, which every code-action UI
-- including tiny-code-action honours via `client:exec_cmd`).
--
-- The selection's `range.start` (from treesitter-textobjects, visual mode, or
-- the cursor) resolves up to its enclosing durable entity via the daemon's
-- id_for, so a sub-expression selection still lands on the function/class.

-- Each provider is `function(ctx) -> CodeAction[]` where
-- ctx = { uri, line, col, bufnr, root, entity, diagnostics }: line/col are
-- 1-based daemon positions; `entity` is the resolved entity card under the
-- selection (nil ⇒ nothing there, so entity providers return {}); `diagnostics`
-- is the LSP context's diagnostics list. Old providers that only read
-- uri/line/col/bufnr keep working — the new fields are purely additive.
M._code_action_providers = M._code_action_providers or {}

--- Register a code-action provider — the raw extensibility seam. The provider
--- is called for every `textDocument/codeAction` request; return a list of LSP
--- CodeActions (commonly each with a `command` resolved by a `vim.lsp.commands`
--- entry you registered via `M.register_command`).
function M.register_code_action(provider)
  table.insert(M._code_action_providers, provider)
end

--- Register a client-side LSP command so a CodeAction's
--- `command = { command = name, arguments = {...} }` runs `fn(command, ctx)`
--- when chosen. Every code-action front end (gra / tiny-code-action /
--- vim.lsp.buf.code_action) resolves client commands through `vim.lsp.commands`,
--- so this is all a plugin needs to make its action actually do something.
function M.register_command(name, fn)
  vim.lsp.commands[name] = fn
end

handlers["textDocument/codeAction"] = function(root, params, reply)
  local pctx = params.context or {}
  -- Respect an explicit kind filter (kind-scoped invocations / pickers).
  if pctx.only and not has_tyo3_kind(pctx.only) then
    return reply(nil, {})
  end
  -- Don't run daemon work for automatic (lightbulb / cursorhold) triggers.
  if pctx.triggerKind == 2 then
    return reply(nil, {})
  end

  local uri = params.textDocument.uri
  local start = (params.range and params.range.start) or { line = 0, character = 0 }
  local p = lsp_pos_to_daemon(start)

  -- Resolve the entity under the selection ONCE; entity providers key off
  -- ctx.entity (and stay silent when nothing is there), so the popup no longer
  -- offers actions that fail when picked. `reply` is already async-safe.
  daemon_request(root, "entity_at", {
    path = uri_to_path(uri),
    line = p.line,
    col = p.col,
  }, function(_err, card)
    local ctx = {
      uri = uri,
      line = p.line,
      col = p.col,
      bufnr = vim.fn.bufnr(uri_to_path(uri)),
      root = root,
      entity = card or nil,
      diagnostics = pctx.diagnostics or {},
    }
    local actions = {}
    for _, provider in ipairs(M._code_action_providers) do
      local ok, contributed = pcall(provider, ctx)
      if ok and type(contributed) == "table" then
        vim.list_extend(actions, contributed)
      elseif not ok then
        vim.schedule(function()
          vim.notify("[tyo3] a code-action provider errored: " .. tostring(contributed), vim.log.levels.WARN)
        end)
      end
    end
    -- Deterministic order (stable across recordings): preferred first, then title.
    table.sort(actions, function(a, b)
      local ap, bp = a.isPreferred and true or false, b.isPreferred and true or false
      if ap ~= bp then
        return ap
      end
      return (a.title or "") < (b.title or "")
    end)
    reply(nil, actions)
  end)
end

-- Built-in: the proj-26 explain / simplify actions — also the reference example
-- of a registered provider. Entity-gated (silent when nothing is under the
-- cursor) and named after the entity, so the popup reads `tyo3: Explain
-- \`checkout\`` rather than a generic label. Distinct kinds (informational
-- vs. refactor.rewrite) get distinct tiny-code-action icons.
M.register_code_action(function(ctx)
  if not ctx.entity then
    return {} -- nothing under the cursor → no noise in the menu
  end
  local who = ctx.entity.qualified_name or ctx.entity.name or "entity"
  return {
    -- Explain is informational (prose → a float), so it runs as a client
    -- command — there is no edit to preview.
    {
      title = ("tyo3: Explain `%s`"):format(who),
      kind = "source.tyo3",
      command = {
        title = "tyo3: Explain",
        command = "tyo3.explain",
        arguments = { { uri = ctx.uri, line = ctx.line, col = ctx.col, mode = "explain", root = ctx.root } },
      },
    },
    -- Simplify is a rewrite: carry `data` and no `edit`, resolved lazily into a
    -- WorkspaceEdit by `codeAction/resolve` so the menu stays instant and a
    -- resolve-capable client (tiny-code-action) previews the diff on focus.
    {
      title = ("tyo3: Simplify `%s`"):format(who),
      kind = "refactor.rewrite",
      data = { uri = ctx.uri, line = ctx.line, col = ctx.col, root = ctx.root, kind = "simplify" },
    },
  }
end)

-- Resolve a Simplify action's `data` into a WorkspaceEdit (the daemon rewrites
-- the entity body). Degrades gracefully: if the rewrite is absent/unparseable
-- (the daemon returns no changes), return the action unchanged — no edit, no
-- preview, still selectable (the explain float remains the prose path).
handlers["codeAction/resolve"] = function(root, action, reply)
  local d = action.data or {}
  if d.kind ~= "simplify" then
    return reply(nil, action)
  end
  daemon_request(d.root or root, "simplify_edit", {
    path = uri_to_path(d.uri),
    line = d.line,
    col = d.col,
  }, function(err, res)
    if err or not res or not res.changes then
      return reply(nil, action)
    end
    local changes = {}
    for relpath, edits in pairs(res.changes) do
      local uri = path_to_uri(d.root or root, relpath)
      local text_edits = {}
      for _, e in ipairs(edits) do
        table.insert(text_edits, { range = daemon_range_to_lsp(e.range), newText = e.new_text })
      end
      changes[uri] = text_edits
    end
    action.edit = { changes = changes }
    reply(nil, action)
  end, SLOW_VERB_OPTS)
end

-- Offer an acknowledge quickfix when the entity under the cursor is flagged
-- needs_review on any authored layer. Re-authoring the note IS the engine's
-- acknowledge (it re-stamps the reviewed body hash), so this clears the WARN.
M.register_code_action(function(ctx)
  if not ctx.entity then
    return {}
  end
  local flagged = {}
  for layer, rec in pairs(ctx.entity.authored or {}) do
    if rec.status == "needs_review" then
      table.insert(flagged, layer)
    end
  end
  if #flagged == 0 then
    return {}
  end
  return {
    {
      title = "tyo3: Acknowledge review (re-author)",
      kind = "quickfix",
      isPreferred = true,
      command = {
        title = "tyo3: Acknowledge review",
        command = "tyo3.ack",
        arguments = { { uri = ctx.uri, root = ctx.root, did = ctx.entity.durable_id, layers = flagged } },
      },
    },
  }
end)

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

-- Push-diagnostics debounce state. A burst of edits (rapid `:w` / TextChanged)
-- would otherwise stack overlapping `check` runs (the ty type-checker is
-- ~hundreds of ms) on the actor. We accumulate the touched paths across a 150ms
-- window into one check per file, and a per-(root, path) generation drops the
-- result of a check superseded by a newer check for the *same* file.
M._diag_debounce = M._diag_debounce or {} -- root -> uv timer
M._diag_pending = M._diag_pending or {} -- root -> set of paths awaiting a check
M._diag_gen = M._diag_gen or {} -- root -> { path -> generation }

--- Push type-checker diagnostics for *relpaths* to the client, off the bus.
--- Driven by `delta` notifications (init.lua), so check diagnostics refresh on
--- edit without the editor polling. Debounced per root and accumulated across
--- the window (so a later, differently-targeted call never drops a file an
--- earlier call queued), one `check` per file, superseded results dropped.
function M.publish_diagnostics(root, relpaths)
  local dispatchers = M._dispatchers_by_root[root]
  if not dispatchers or not dispatchers.notification then
    return -- no live tyo3 client for this root
  end
  local pending = M._diag_pending[root] or {}
  M._diag_pending[root] = pending
  for _, path in ipairs(relpaths or {}) do
    pending[path] = true
  end
  local uv = vim.uv or vim.loop
  local prev = M._diag_debounce[root]
  if prev then
    pcall(function()
      prev:stop()
      prev:close()
    end)
  end
  local timer = uv.new_timer()
  M._diag_debounce[root] = timer
  timer:start(
    150,
    0,
    vim.schedule_wrap(function()
      pcall(function()
        timer:stop()
        timer:close()
      end)
      if M._diag_debounce[root] == timer then
        M._diag_debounce[root] = nil
      end
      local paths = M._diag_pending[root] or {}
      M._diag_pending[root] = nil
      local gens = M._diag_gen[root] or {}
      M._diag_gen[root] = gens
      for path in pairs(paths) do
        local gen = (gens[path] or 0) + 1
        gens[path] = gen
        daemon_request(root, "check", { path = path }, function(err, res)
          -- Drop a superseded check (a newer check for this same file started)
          -- or a client that went away while the check was in flight.
          if err or (M._diag_gen[root] or {})[path] ~= gen then
            return
          end
          local d = M._dispatchers_by_root[root]
          if not d or not d.notification then
            return
          end
          d.notification("textDocument/publishDiagnostics", {
            uri = path_to_uri(root, path),
            diagnostics = daemon_diags_to_lsp(res and res.diagnostics),
          })
        end)
      end
    end)
  )
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

-- ── tyo3.explain client command (proj 26) ────────────────────────────────────
--
-- Registered client-side so every code-action entry point (`gra`,
-- tiny-code-actions, vim.lsp.buf.code_action) drives it for free. Resolves the
-- selection to a durable entity, runs the daemon `explain` verb (LLM → durable
-- authored layer), shows the text, then re-decorates + refreshes the layer-state
-- diagnostics so the new record and its future needs_review surface.

--- Run daemon *verb* with *params* for *bufnr* (project *root*); on success
--- re-decorate + refresh layer-state diagnostics (so a new/updated layer record
--- and its future needs_review surface), then `cb(err, res)`. The generic core
--- a spine plugin's command builds on — and what run_explain / tyo3.run use.
function M.run_verb(bufnr, root, verb, params, cb)
  daemon_request(root, verb, params, function(err, res)
    if not err and res and vim.api.nvim_buf_is_loaded(bufnr) then
      pcall(function()
        require("tyo3.decorate").apply(bufnr)
      end)
      if require("tyo3.config").layer_diagnostics_enabled() then
        M.refresh_layer_diagnostics(bufnr, root)
      end
    end
    if cb then
      cb(err, res)
    end
  end, SLOW_VERB_OPTS)
end

--- Run the `explain` daemon verb for *args* (`{uri,line,col,mode}`) on *bufnr*.
--- Split out from the command so headless tests can call it directly.
function M.run_explain(bufnr, root, args, cb)
  M.run_verb(bufnr, root, "explain", {
    path = (args.uri and uri_to_path(args.uri)) or vim.api.nvim_buf_get_name(bufnr),
    line = args.line,
    col = args.col,
    mode = args.mode or "explain",
  }, cb)
end

M.register_command("tyo3.explain", function(command, ctx)
  local args = (command.arguments or {})[1] or {}
  local bufnr = (ctx and ctx.bufnr) or vim.api.nvim_get_current_buf()
  local root = args.root or require("tyo3").root_for_buf(bufnr)
  if not root then
    return
  end
  -- Surface a "thinking" beat so the popup → float gap isn't silent (a real
  -- LLM can take a few seconds; Phase 5 keeps the rest of the editor responsive).
  vim.notify("[tyo3] " .. (args.mode or "explain") .. " …", vim.log.levels.INFO)
  M.run_explain(bufnr, root, args, function(err, res)
    vim.schedule(function()
      if err then
        vim.notify("[tyo3] explain failed: " .. (err.message or "daemon error"), vim.log.levels.ERROR)
      elseif not res then
        vim.notify("[tyo3] no entity under the cursor to explain", vim.log.levels.WARN)
      else
        vim.lsp.util.open_floating_preview(
          vim.split(res.text, "\n", { plain = true }),
          "markdown",
          { border = "rounded", wrap = true, title = "tyo3: " .. (res.mode or "explain") }
        )
      end
    end)
  end)
end)

-- ── tyo3.ack client command — acknowledge needs_review ───────────────────────
--
-- Clears the needs_review WARN by re-authoring each flagged layer's CURRENT
-- value (re-read fresh at apply time via `authored`, not the menu-time snapshot,
-- so a racing edit can't stamp a stale body). Re-authoring is the engine's
-- acknowledge — it re-stamps the reviewed body hash. No engine change needed.
M.register_command("tyo3.ack", function(command, cmd_ctx)
  local args = (command.arguments or {})[1] or {}
  local bufnr = (cmd_ctx and cmd_ctx.bufnr) or vim.api.nvim_get_current_buf()
  local root = args.root or require("tyo3").root_for_buf(bufnr)
  if not root or not args.did then
    return
  end
  local layers = args.layers or {}
  local pending = #layers
  if pending == 0 then
    return
  end
  local function done_one()
    pending = pending - 1
    if pending > 0 then
      return
    end
    vim.schedule(function()
      if vim.api.nvim_buf_is_loaded(bufnr) then
        pcall(function()
          require("tyo3.decorate").apply(bufnr)
        end)
        if require("tyo3.config").layer_diagnostics_enabled() then
          M.refresh_layer_diagnostics(bufnr, root)
        end
      end
      vim.notify("[tyo3] review acknowledged", vim.log.levels.INFO)
    end)
  end
  for _, layer in ipairs(layers) do
    daemon_request(root, "authored", { layer = layer, durable_id = args.did }, function(err, av)
      if not err and av and av.value ~= nil then
        daemon_request(root, "author", { layer = layer, durable_id = args.did, value = av.value }, function()
          done_one()
        end)
      else
        done_one()
      end
    end)
  end
end)

-- ── register_entity_action: the one-call seam for spine plugins ───────────────
--
-- The 80% case: "call a daemon verb on the entity under the cursor and show the
-- text". A plugin author who registered a layer/generator with Python's
-- `tyo3.extend` writes ONE call here and gets a code action in tiny-code-action:
--
--   require("tyo3.lsp").register_entity_action({
--     title = "tyo3: Summarize for docs",
--     verb  = "explain",                 -- any daemon verb taking {path,line,col}
--     params = { mode = "explain" },     -- extra params merged over the position
--   })

M._entity_actions = M._entity_actions or {}

local function default_entity_result(err, res, _bufnr, _root, spec)
  if err then
    vim.notify("[tyo3] " .. spec.title .. " failed: " .. (err.message or "daemon error"), vim.log.levels.ERROR)
  elseif not res then
    vim.notify("[tyo3] no entity under the cursor", vim.log.levels.WARN)
  else
    local text = (type(res) == "table" and (res.text or vim.inspect(res))) or tostring(res)
    vim.lsp.util.open_floating_preview(
      vim.split(text, "\n", { plain = true }),
      "markdown",
      { border = "rounded", wrap = true, title = spec.title }
    )
  end
end

--- Register a code action that calls daemon *spec.verb* on the entity under the
--- cursor and shows the result. ``spec`` = ``{ id?, title, verb, kind?, params?,
--- on_result? }`` — ``params`` merge over ``{path,line,col}``; ``on_result`` is
--- ``function(err, res, bufnr, root, spec)`` (default floats ``res.text``).
function M.register_entity_action(spec)
  local id = spec.id or spec.title
  M._entity_actions[id] = spec
  M.register_code_action(function(ctx)
    if not ctx.entity then
      return {} -- act-on-the-entity actions stay silent off-entity
    end
    return {
      {
        title = spec.title,
        kind = spec.kind or "refactor",
        command = {
          title = spec.title,
          command = "tyo3.run",
          arguments = { { id = id, uri = ctx.uri, line = ctx.line, col = ctx.col } },
        },
      },
    }
  end)
end

-- Generic dispatcher for register_entity_action's actions.
M.register_command("tyo3.run", function(command, ctx)
  local args = (command.arguments or {})[1] or {}
  local spec = M._entity_actions[args.id]
  if not spec then
    return
  end
  local bufnr = (ctx and ctx.bufnr) or vim.api.nvim_get_current_buf()
  local root = require("tyo3").root_for_buf(bufnr)
  if not root then
    return
  end
  local params = vim.tbl_extend("force", {
    path = (args.uri and uri_to_path(args.uri)) or vim.api.nvim_buf_get_name(bufnr),
    line = args.line,
    col = args.col,
  }, spec.params or {})
  M.run_verb(bufnr, root, spec.verb, params, function(err, res)
    vim.schedule(function()
      (spec.on_result or default_entity_result)(err, res, bufnr, root, spec)
    end)
  end)
end)

return M
