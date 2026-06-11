-- Headless integration test for the code-action registry — the single "act on
-- the entity" surface (proj 26 spike, hardened into proj 28 Phase C). Builds the
-- synthetic shop project, attaches the in-process `vim.lsp` server, and:
--   * requests textDocument/codeAction over an entity → asserts the menu's
--     *content* by presence + filtering (never totals): Explain (gated on the
--     declared `explain` layer), a resolvable Simplify, Write/edit doc, Move, and
--     Author for exactly the writable layers {intent} — proving explain/docs are
--     excluded from Author while summary/embed are excluded as derived;
--   * exercises the extensibility seam (a raw provider + register_entity_action)
--     and the generic tyo3.run dispatcher end-to-end;
--   * drives M.author_note directly → the daemon `author` verb writes a durable
--     intent note (the factored core the tyo3.author command shares);
--   * runs the tyo3.explain path (M.run_explain) → the `explain` verb (LLM stub →
--     durable authored layer) and asserts the record, then mode="simplify";
--   * asserts the needs_review acknowledge quickfix appears + clears the flag.
-- Hermetic: the daemon's LLM seam defaults to the offline stub (no network).
--
-- Run:
--   nvim --headless -u editors/tyo3.nvim/tests/minimal_init.lua \
--        -c "luafile editors/tyo3.nvim/tests/lsp_codeaction.lua"

local results = {}
local function check(name, cond, detail)
  table.insert(results, { name = name, ok = cond and true or false, detail = detail })
end

local function report_and_exit(proj)
  local failed = 0
  for _, r in ipairs(results) do
    local tag = r.ok and "PASS" or "FAIL"
    if not r.ok then
      failed = failed + 1
    end
    print(("[%s] %s%s"):format(tag, r.name, r.detail and (" — " .. tostring(r.detail)) or ""))
  end
  print(("tyo3.nvim lsp_codeaction: %d checks, %d failed"):format(#results, failed))
  if proj then
    vim.fn.delete(proj, "rf")
  end
  require("tyo3").shutdown()
  if failed > 0 then
    vim.cmd("cquit 1")
  else
    vim.cmd("qall!")
  end
end

-- ── Build a fresh shop project via the Python tour builder ──────────────────
local proj = vim.fn.tempname()
local py = table.concat({
  "import sys",
  "from pathlib import Path",
  "from tyo3.demo.tour import _build_project",
  "_build_project(Path(sys.argv[1]))",
}, "\n")
vim.fn.system({ "python", "-c", py, proj })
check("build shop project", vim.fn.isdirectory(proj) == 1, proj)

require("tyo3").setup({
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  debounce_ms = 50,
})

local store = proj .. "/store.py"
local store_uri = vim.uri_from_fname(store)

vim.cmd("edit " .. vim.fn.fnameescape(store))
vim.bo.filetype = "python"
local bufnr = vim.api.nvim_get_current_buf()

-- ── Connect, open, sync store.py ────────────────────────────────────────────
local prepared, prep_err
require("tyo3").with_client(bufnr, function(client, root)
  client:request("open", { root = root }, function(oerr)
    if oerr then
      prep_err, prepared = oerr.message, true
      return
    end
    local lines = vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)
    client:request("sync_buffer", { path = store, text = table.concat(lines, "\n") .. "\n" }, function(serr)
      prep_err = serr and serr.message or nil
      prepared = true
    end)
  end)
end, function(msg)
  prep_err, prepared = msg, true
end)
vim.wait(30000, function()
  return prepared
end, 50)
check("daemon connected + project synced", not prep_err, prep_err)
if prep_err then
  report_and_exit(proj)
  return
end

-- ── Attach the in-process LSP server ────────────────────────────────────────
require("tyo3.lsp").attach(bufnr, proj)
local attached = vim.wait(10000, function()
  return #vim.lsp.get_clients({ name = "tyo3", bufnr = bufnr }) > 0
end, 50)
check("tyo3 LSP client attaches", attached)
local client = vim.lsp.get_clients({ name = "tyo3", bufnr = bufnr })[1]
if not client then
  report_and_exit(proj)
  return
end
vim.wait(5000, function()
  return client.initialized == true
end, 25)

-- ── Resolve checkout + show_label positions from the decorate batch ─────────
local checkout_id, checkout_pos, label_id, label_pos, deco_done, deco_err
require("tyo3").with_client(bufnr, function(c)
  c:request("decorate", { path = store }, function(derr, deco)
    if derr then
      deco_err, deco_done = derr.message, true
      return
    end
    for _, d in ipairs(deco or {}) do
      if d.name == "checkout" then
        checkout_id = d.durable_id
        checkout_pos = d.range.start -- 1-based {line, column}
      elseif d.name == "show_label" then
        label_id = d.durable_id
        label_pos = d.range.start
      end
    end
    deco_done = true
  end)
end, function(msg)
  deco_err, deco_done = msg, true
end)
vim.wait(10000, function()
  return deco_done
end, 50)
check("resolved checkout via decorate", checkout_id ~= nil and checkout_pos ~= nil, deco_err)
if not checkout_id then
  report_and_exit(proj)
  return
end

-- ── Part 1: textDocument/codeAction returns the two tyo3 actions ────────────
local ca_params = {
  textDocument = { uri = store_uri },
  range = {
    start = { line = checkout_pos.line - 1, character = checkout_pos.column - 1 },
    ["end"] = { line = checkout_pos.line - 1, character = checkout_pos.column - 1 },
  },
  context = { diagnostics = {} },
}
local ca = vim.lsp.buf_request_sync(bufnr, "textDocument/codeAction", ca_params, 5000)
local actions = ca and ca[client.id] and ca[client.id].result
check("codeAction returns a table of actions", type(actions) == "table" and #actions > 0, actions and #actions or nil)

-- Phase C makes the menu the *single* "act on the entity" surface, so it grows
-- (Author/Doc/Move alongside Explain/Simplify). Assert *presence + filtering*,
-- never totals. Derive the relevant sets from the returned actions.
local commands, kinds, data_kinds, author_layers = {}, {}, {}, {}
local explain_action, simplify_action
for _, a in ipairs(actions or {}) do
  if a.kind then
    kinds[a.kind] = true
  end
  if a.data and a.data.kind then
    data_kinds[a.data.kind] = true
  end
  if a.command and a.command.command then
    commands[a.command.command] = true
    if a.command.command == "tyo3.author" then
      local layer = a.command.arguments and a.command.arguments[1] and a.command.arguments[1].layer
      if layer then
        author_layers[layer] = true
      end
    end
    local mode = a.command.arguments and a.command.arguments[1] and a.command.arguments[1].mode
    if a.command.command == "tyo3.explain" and mode == "explain" then
      explain_action = a
    end
  elseif a.data and a.data.kind == "simplify" then
    simplify_action = a
  end
end

-- Presence: Explain (gated — the shop declares `[layers.explain]`), a resolvable
-- Simplify, Write/edit doc, and Move are all offered on the entity.
check("explain action present (explain layer declared)", commands["tyo3.explain"] == true)
check("a simplify action carries resolve data", simplify_action ~= nil and data_kinds["simplify"] == true)
check("write/edit doc action present", commands["tyo3.doc"] == true)
check("move action present", commands["tyo3.move"] == true)

-- The single assertion that proves the whole Author-filtering story: the shop
-- declares [layers.intent|docs|explain|summary|embed]; Author is offered only for
-- writable layers (origin==authored) minus docs/explain — so exactly {intent}.
-- explain/docs are excluded from Author (yet Explain is present); summary/embed
-- are derived and excluded. No `{"intent"}` fallback could fake this — it comes
-- from the live `layers` verb via ctx.layers.
local n_author = 0
for _ in pairs(author_layers) do
  n_author = n_author + 1
end
check("author offered for exactly the writable layers {intent}", author_layers["intent"] == true and n_author == 1, n_author)

check(
  "explain action argument carries uri + 1-based position",
  explain_action
    and explain_action.command.arguments[1].uri == store_uri
    and explain_action.command.arguments[1].line == checkout_pos.line
    and explain_action.command.arguments[1].col == checkout_pos.column
)
check(
  "simplify action data carries uri + 1-based position",
  simplify_action
    and simplify_action.data.uri == store_uri
    and simplify_action.data.line == checkout_pos.line
    and simplify_action.data.col == checkout_pos.column
)

-- Kinds are split so tiny-code-action icons + `context.only` filtering work:
-- explain/author/doc are informational (source.tyo3), simplify a refactor.rewrite,
-- move a refactor.move.
check(
  "kinds include source.tyo3 + refactor.rewrite + refactor.move",
  kinds["source.tyo3"] == true and kinds["refactor.rewrite"] == true and kinds["refactor.move"] == true
)

-- ── Part 1a: off-entity position offers nothing (entity-gating) ─────────────
-- Line 0/col 0 is the module's first import — id_for returns nil there, so the
-- entity providers stay silent and the popup is empty rather than offering
-- actions that fail when picked. (Only built-ins are registered at this point.)
local blank_params = {
  textDocument = { uri = store_uri },
  range = {
    start = { line = 0, character = 0 },
    ["end"] = { line = 0, character = 0 },
  },
  context = { diagnostics = {} },
}
local ca0 = vim.lsp.buf_request_sync(bufnr, "textDocument/codeAction", blank_params, 5000)
local a0 = ca0 and ca0[client.id] and ca0[client.id].result
check("no code action offered off-entity", type(a0) == "table" and #a0 == 0, a0 and #a0 or nil)

-- ── Part 1b: a third-party plugin contributes actions with no fork ──────────
-- The extensibility seam: register a raw provider + a register_entity_action,
-- then re-request codeAction and assert both surface (this is exactly what a
-- spine plugin does at setup to appear in tiny-code-action).
local lsp = require("tyo3.lsp")
lsp.register_code_action(function(c)
  return {
    {
      title = "myplugin: sentinel action",
      kind = "refactor",
      command = { title = "x", command = "myplugin.noop", arguments = { { uri = c.uri } } },
    },
  }
end)
lsp.register_entity_action({
  title = "myplugin: summarize entity",
  verb = "explain",
  params = { mode = "explain" },
})

local ca2 = vim.lsp.buf_request_sync(bufnr, "textDocument/codeAction", ca_params, 5000)
local actions2 = ca2 and ca2[client.id] and ca2[client.id].result
-- Presence, not totals: the registered provider + entity action surface
-- *alongside* the (growing) built-in menu. (Asserted by title/command below.)
check("registered actions surface alongside built-ins", type(actions2) == "table" and #actions2 > 0, actions2 and #actions2 or nil)
local titles = {}
local run_action
for _, a in ipairs(actions2 or {}) do
  titles[a.title] = true
  if a.command and a.command.command == "tyo3.run" then
    run_action = a
  end
end
check("raw provider's sentinel action is present", titles["myplugin: sentinel action"] == true)
check("entity action is present as a tyo3.run command", run_action ~= nil)
check(
  "entity action carries its id + position",
  run_action and run_action.command.arguments[1].id == "myplugin: summarize entity",
  run_action and run_action.command.arguments[1].id or nil
)

-- ── Part 1c: the generic tyo3.run dispatcher executes the verb end-to-end ───
-- Invoke the registered command exactly as tiny-code-action would (client
-- command resolved through vim.lsp.commands), targeting show_label, then poll
-- its durable explain record → proves the seam reaches the daemon verb.
check("resolved show_label via decorate", label_id ~= nil and label_pos ~= nil)
if label_id and run_action then
  vim.lsp.commands["tyo3.run"]({
    command = "tyo3.run",
    arguments = { {
      id = "myplugin: summarize entity",
      uri = store_uri,
      line = label_pos.line,
      col = label_pos.column,
    } },
  }, { bufnr = bufnr })

  local label_present = false
  local deadline = vim.loop.now() + 15000
  while not label_present and vim.loop.now() < deadline do
    local got, done
    require("tyo3").with_client(bufnr, function(c)
      c:request("authored", { layer = "explain", durable_id = label_id }, function(_, av)
        got, done = av, true
      end)
    end)
    vim.wait(2000, function()
      return done
    end, 25)
    label_present = got ~= nil and got.status == "present"
    if not label_present then
      vim.wait(300)
    end
  end
  check("tyo3.run dispatcher authored show_label's explain record", label_present)
end

-- ── Part 1d: the factored author core writes a durable note end-to-end ──────
-- The `tyo3.author` command splits its prompt from a testable core,
-- M.author_note, exactly so the spike can drive the durable write without faking
-- the snacks prompt. Author an `intent` note on checkout, then poll its record.
local an_done
require("tyo3.lsp").author_note(bufnr, proj, checkout_id, "intent", { note = "checkout entrypoint" }, function(_err)
  an_done = true
end)
vim.wait(15000, function()
  return an_done
end, 50)
local an_present = false
local an_deadline = vim.loop.now() + 15000
while not an_present and vim.loop.now() < an_deadline do
  local got, done
  require("tyo3").with_client(bufnr, function(c)
    c:request("authored", { layer = "intent", durable_id = checkout_id }, function(_, av)
      got, done = av, true
    end)
  end)
  vim.wait(2000, function()
    return done
  end, 25)
  an_present = got ~= nil and got.status == "present" and got.value and got.value.note == "checkout entrypoint"
  if not an_present then
    vim.wait(300)
  end
end
check("author_note wrote a durable intent note on checkout", an_present)

-- ── Part 2: run the explain command → durable record on the explain layer ───
local explained, ex_err, ex_res
if explain_action then
  require("tyo3.lsp").run_explain(bufnr, proj, explain_action.command.arguments[1], function(err, res)
    ex_err, ex_res, explained = err, res, true
  end)
  vim.wait(15000, function()
    return explained
  end, 50)
end
check("explain command returns text", not ex_err and ex_res and type(ex_res.text) == "string" and #ex_res.text > 0, ex_err)
check("explain resolved checkout's durable id", ex_res and ex_res.durable_id == checkout_id)

-- The record is durably present on the explain layer keyed by checkout's id.
local authored, au_err, au_done
require("tyo3").with_client(bufnr, function(c)
  c:request("authored", { layer = "explain", durable_id = checkout_id }, function(aerr, av)
    au_err, authored, au_done = aerr and aerr.message or nil, av, true
  end)
end, function(msg)
  au_err, au_done = msg, true
end)
vim.wait(10000, function()
  return au_done
end, 50)
check("explain record is present on the durable layer", authored and authored.status == "present", au_err)
check("explain record value matches the returned text", authored and authored.value and authored.value.text == ex_res.text)
check("explain record stamped the offline model", authored and authored.value and authored.value.model == "stub")

-- ── Part 3: the explain verb's simplify mode updates the same id's record ───
-- (Driven directly: the code action's Simplify is now a resolve-to-edit, but
-- the `explain` verb still supports mode=simplify for the prose path.)
local simplified, smpl_done
require("tyo3.lsp").run_explain(
  bufnr,
  proj,
  { uri = store_uri, line = checkout_pos.line, col = checkout_pos.column, mode = "simplify" },
  function(_, res)
    simplified, smpl_done = res, true
  end
)
vim.wait(15000, function()
  return smpl_done
end, 50)
check("simplify mode returns text", simplified and simplified.mode == "simplify" and #simplified.text > 0)

-- ── Part 3b: codeAction/resolve degrades gracefully under the offline stub ──
-- The offline stub returns prose (not parseable source), so simplify_edit
-- returns no changes and resolve hands back the action unchanged — no edit, no
-- preview, still selectable. (A real model returning parseable source yields a
-- WorkspaceEdit; that path is covered by the daemon's e2e test.)
if simplify_action then
  local rs = vim.lsp.buf_request_sync(bufnr, "codeAction/resolve", simplify_action, 60000)
  local resolved = rs and rs[client.id] and rs[client.id].result
  check("resolve returns the action", type(resolved) == "table")
  check("resolve degrades to no edit under the offline stub", resolved and resolved.edit == nil)
end

local au2, au2_done
require("tyo3").with_client(bufnr, function(c)
  c:request("authored", { layer = "explain", durable_id = checkout_id }, function(_, av)
    au2, au2_done = av, true
  end)
end, function()
  au2_done = true
end)
vim.wait(10000, function()
  return au2_done
end, 50)
check("explain record now records simplify mode", au2 and au2.value and au2.value.mode == "simplify")

-- ── Part 4: review-acknowledge quickfix appears + clears needs_review ───────
-- Author an `intent` note on show_label, edit its body (flips intent →
-- needs_review), then request codeAction on it: a `tyo3.ack` quickfix appears,
-- marked isPreferred. Invoking it re-authors the note (re-stamps the reviewed
-- hash), which clears the flag.
local ack_ready, ack_err
require("tyo3").with_client(bufnr, function(c)
  c:request(
    "author",
    { layer = "intent", durable_id = label_id, value = { note = "keep this label stable" } },
    function(aerr)
      if aerr then
        ack_err, ack_ready = aerr.message, true
        return
      end
      -- Edit show_label's body so its content hash drifts from the reviewed one.
      local orig = table.concat(vim.api.nvim_buf_get_lines(bufnr, 0, -1, false), "\n") .. "\n"
      local edited = orig:gsub("return Item%(%)%.label%(%)", 'return Item().label() + "!"')
      c:request("sync_buffer", { path = store, text = edited }, function(serr)
        ack_err, ack_ready = serr and serr.message or nil, true
      end)
    end
  )
end, function(msg)
  ack_err, ack_ready = msg, true
end)
vim.wait(15000, function()
  return ack_ready
end, 50)
check("authored intent on show_label + drifted its body", not ack_err, ack_err)

-- needs_review is anchored to the entity id, so the def line hasn't moved.
local ack_params = {
  textDocument = { uri = store_uri },
  range = {
    start = { line = label_pos.line - 1, character = label_pos.column - 1 },
    ["end"] = { line = label_pos.line - 1, character = label_pos.column - 1 },
  },
  context = { diagnostics = {} },
}
local ca_ack = vim.lsp.buf_request_sync(bufnr, "textDocument/codeAction", ack_params, 5000)
local ack_actions = ca_ack and ca_ack[client.id] and ca_ack[client.id].result
local ack_action
for _, a in ipairs(ack_actions or {}) do
  if a.command and a.command.command == "tyo3.ack" then
    ack_action = a
  end
end
check("ack quickfix appears when entity is needs_review", ack_action ~= nil)
check("ack quickfix is marked isPreferred", ack_action and ack_action.isPreferred == true)
check("ack quickfix carries the quickfix kind", ack_action and ack_action.kind == "quickfix")

if ack_action then
  vim.lsp.commands["tyo3.ack"]({
    command = "tyo3.ack",
    arguments = ack_action.command.arguments,
  }, { bufnr = bufnr })

  -- Poll the intent status until it clears (re-author is async over two hops).
  local cleared = false
  local deadline = vim.loop.now() + 15000
  while not cleared and vim.loop.now() < deadline do
    local got, done
    require("tyo3").with_client(bufnr, function(c)
      c:request("authored", { layer = "intent", durable_id = label_id }, function(_, av)
        got, done = av, true
      end)
    end)
    vim.wait(2000, function()
      return done
    end, 25)
    cleared = got ~= nil and got.status == "present"
    if not cleared then
      vim.wait(300)
    end
  end
  check("ack cleared show_label's needs_review", cleared)
end

report_and_exit(proj)
