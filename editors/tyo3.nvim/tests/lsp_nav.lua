-- Headless integration test for the native LSP bridge — Phase 2 (lua/tyo3/lsp.lua).
--
-- Builds the synthetic shop project, opens store.py with `lsp = true`, attaches
-- the in-process `vim.lsp` server, and drives the Phase-2 surface through
-- Neovim's native machinery (`vim.lsp.buf_request_sync`):
--   * textDocument/definition  (goto-definition on a usage)
--   * prepareTypeHierarchy + typeHierarchy/supertypes + /subtypes
--   * spine layer state (needs_review) as a `vim.diagnostic` namespace
--   * push diagnostics (server→client publishDiagnostics on demand)
-- Prints PASS/FAIL per check and exits non-zero on any failure (gates in CI).
--
-- Run:
--   nvim --headless -u editors/tyo3.nvim/tests/minimal_init.lua \
--        -c "luafile editors/tyo3.nvim/tests/lsp_nav.lua"

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
  print(("tyo3.nvim lsp_nav: %d checks, %d failed"):format(#results, failed))
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

-- lsp = true ⇒ layer_diagnostics follows lsp (enabled), so the layer namespace
-- is populated; default-off behaviour is exercised by the other specs.
require("tyo3").setup({
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  auto_start = true,
  debounce_ms = 50,
  lsp = true,
})

local store = proj .. "/store.py"
local store_uri = vim.uri_from_fname(store)
local book = proj .. "/book.py"
local catalog = proj .. "/catalog.py"
local legacy = proj .. "/legacy.py"

vim.cmd("edit " .. vim.fn.fnameescape(store))
vim.bo.filetype = "python"
local bufnr = vim.api.nvim_get_current_buf()

-- ── Connect, open, sync store.py ────────────────────────────────────────────
local prepared, prep_err
local store_lines

require("tyo3").with_client(bufnr, function(client, root)
  client:request("open", { root = root }, function(oerr)
    if oerr then
      prep_err, prepared = oerr.message, true
      return
    end
    store_lines = vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)
    client:request("sync_buffer", { path = store, text = table.concat(store_lines, "\n") .. "\n" }, function(serr)
      if serr then
        prep_err = serr.message
      end
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

check("server stashed dispatchers for the root", require("tyo3.lsp")._dispatchers_by_root[proj] ~= nil)

-- Helper: 0-based position params for the `name` token on `line_1based` of a uri.
local function name_pos(uri, lines, line_1based, token)
  local text = lines[line_1based] or ""
  local byte = text:find(token, 1, true) -- 1-based byte (ASCII == codepoint here)
  return {
    textDocument = { uri = uri },
    position = { line = line_1based - 1, character = (byte or 1) - 1 },
  }, byte ~= nil
end

-- ── Part 1: goto-definition on the `usd(` usage in checkout (store.py:7) ─────
local usd_line
for i, l in ipairs(store_lines) do
  if l:find("usd(", 1, true) and not l:find("import", 1, true) then
    usd_line = i
  end
end
check("found a usd() call site in store.py", usd_line ~= nil)
if usd_line then
  local params = name_pos(store_uri, store_lines, usd_line, "usd")
  local res = vim.lsp.buf_request_sync(bufnr, "textDocument/definition", params, 5000)
  local locs = res and res[client.id] and res[client.id].result
  check(
    "definition returns >=1 Location",
    type(locs) == "table" and #locs >= 1,
    locs and ("count=" .. #locs) or nil
  )
  if locs and locs[1] then
    check("definition Location has uri + range", locs[1].uri ~= nil and locs[1].range ~= nil)
    check(
      "definition resolves usd → money.py",
      tostring(locs[1].uri):find("money.py", 1, true) ~= nil,
      tostring(locs[1].uri)
    )
  end
end

-- ── Part 2: type hierarchy ──────────────────────────────────────────────────
-- Book(Item) in book.py:4 → supertypes include Item. Item in catalog.py:1 →
-- subtypes include Book. (book.py/catalog.py are indexed on open; no sync needed.)
local book_uri = vim.uri_from_fname(book)
local book_lines = vim.fn.readfile(book)
local bparams = name_pos(book_uri, book_lines, 4, "Book")

local pth = vim.lsp.buf_request_sync(bufnr, "textDocument/prepareTypeHierarchy", bparams, 5000)
local items = pth and pth[client.id] and pth[client.id].result
check(
  "prepareTypeHierarchy returns an item for Book",
  type(items) == "table" and items[1] and items[1].name == "Book",
  items and items[1] and items[1].name or vim.inspect(items)
)

if items and items[1] then
  local supers = vim.lsp.buf_request_sync(bufnr, "typeHierarchy/supertypes", { item = items[1] }, 5000)
  local sup = supers and supers[client.id] and supers[client.id].result
  check("supertypes returns an array", type(sup) == "table")
  local names = {}
  for _, it in ipairs(sup or {}) do
    names[it.name] = true
  end
  check("Book's supertypes include Item", names["Item"] == true, vim.inspect(vim.tbl_keys(names)))
end

-- subtypes on Item (catalog.py) should include Book — a non-empty case.
local cat_uri = vim.uri_from_fname(catalog)
local cat_lines = vim.fn.readfile(catalog)
local cparams = name_pos(cat_uri, cat_lines, 1, "Item")
local pth2 = vim.lsp.buf_request_sync(bufnr, "textDocument/prepareTypeHierarchy", cparams, 5000)
local item2 = pth2 and pth2[client.id] and pth2[client.id].result
if item2 and item2[1] then
  local subs = vim.lsp.buf_request_sync(bufnr, "typeHierarchy/subtypes", { item = item2[1] }, 5000)
  local sub = subs and subs[client.id] and subs[client.id].result
  check("subtypes returns an array", type(sub) == "table")
  local names = {}
  for _, it in ipairs(sub or {}) do
    names[it.name] = true
  end
  check("Item's subtypes include Book", names["Book"] == true, vim.inspect(vim.tbl_keys(names)))
end

-- ── Part 3: spine layer state as a vim.diagnostic namespace ─────────────────
-- Author an intent note on legacy_helper (review_on_change=true), then edit its
-- body → the authored record flips to needs_review. review_state then joins it
-- with the node range; refresh_layer_diagnostics publishes it into tyo3-layer.
local legacy_id, layer_done, layer_err
require("tyo3").with_client(bufnr, function(c)
  c:request("decorate", { path = legacy }, function(derr, deco)
    if derr then
      layer_err, layer_done = derr.message, true
      return
    end
    for _, d in ipairs(deco or {}) do
      if d.name == "legacy_helper" then
        legacy_id = d.durable_id
      end
    end
    if not legacy_id then
      layer_err, layer_done = "legacy_helper not found", true
      return
    end
    c:request("author", { layer = "intent", durable_id = legacy_id, value = { note = "watch me" } }, function(aerr)
      if aerr then
        layer_err, layer_done = aerr.message, true
        return
      end
      c:request(
        "sync_buffer",
        { path = legacy, text = "def legacy_helper(x: int) -> int:\n    return x + 100\n" },
        function(serr)
          layer_err = serr and serr.message or nil
          layer_done = true
        end
      )
    end)
  end)
end, function(msg)
  layer_err, layer_done = msg, true
end)
vim.wait(15000, function()
  return layer_done
end, 50)
check("authored intent note + edited legacy_helper body", not layer_err, layer_err)

-- Open legacy.py and refresh its layer diagnostics from review_state.
vim.cmd("edit " .. vim.fn.fnameescape(legacy))
vim.bo.filetype = "python"
local lbuf = vim.api.nvim_get_current_buf()
local ns = require("tyo3.lsp").layer_namespace()
require("tyo3.lsp").refresh_layer_diagnostics(lbuf, proj)

local got_layer = vim.wait(8000, function()
  return #vim.diagnostic.get(lbuf, { namespace = ns }) > 0
end, 50)
check("layer-state diagnostics populate the tyo3-layer namespace", got_layer)
if got_layer then
  local diags = vim.diagnostic.get(lbuf, { namespace = ns })
  local d = diags[1]
  check("layer diagnostic is WARN (needs_review)", d.severity == vim.diagnostic.severity.WARN, tostring(d.severity))
  check("layer diagnostic carries a range + tyo3 source", d.lnum ~= nil and d.end_lnum ~= nil and d.source == "tyo3")
  check("layer diagnostic message mentions review", tostring(d.message):find("review", 1, true) ~= nil, d.message)
end

-- ── Part 4: push diagnostics (server→client publishDiagnostics) ─────────────
-- Spy the publishDiagnostics handler, then trigger a push for store.py (the
-- path the `delta` handler would pass) and assert the notification reached the
-- client with the right uri. store.py is clean, so the diagnostics list may be
-- empty — we assert the *wire path*, not a non-empty set.
local pushed = {}
local orig = vim.lsp.handlers["textDocument/publishDiagnostics"]
vim.lsp.handlers["textDocument/publishDiagnostics"] = function(err, result, ctx, cfg)
  table.insert(pushed, result)
  if orig then
    return orig(err, result, ctx, cfg)
  end
end

require("tyo3.lsp").publish_diagnostics(proj, { "store.py" })
local got_push = vim.wait(8000, function()
  for _, r in ipairs(pushed) do
    if r and tostring(r.uri):find("store.py", 1, true) then
      return true
    end
  end
  return false
end, 50)
vim.lsp.handlers["textDocument/publishDiagnostics"] = orig
check("publishDiagnostics push reached the client for store.py", got_push)
if got_push then
  local last
  for _, r in ipairs(pushed) do
    if r and tostring(r.uri):find("store.py", 1, true) then
      last = r
    end
  end
  check("pushed report carries a diagnostics array", type(last.diagnostics) == "table", last and vim.inspect(last.uri))
end

-- ── Report ──────────────────────────────────────────────────────────────────
report_and_exit(proj)
