-- Headless integration test for the Phase-A symbol surfaces (lua/tyo3/lsp.lua).
--
-- Builds the synthetic shop project, opens store.py with `lsp = true`, attaches
-- the in-process `vim.lsp` server, and drives the new providers through
-- Neovim's native machinery (`vim.lsp.buf_request_sync`):
--   * textDocument/documentSymbol   (hierarchical outline of store.py)
--   * workspace/symbol              (project-wide picker source)
--   * prepareCallHierarchy + callHierarchy/incomingCalls (callers of usd)
-- Prints PASS/FAIL per check and exits non-zero on any failure (gates in CI).
--
-- Run:
--   nvim --headless -u editors/tyo3.nvim/tests/minimal_init.lua \
--        -c "luafile editors/tyo3.nvim/tests/lsp_symbols.lua"

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
  print(("tyo3.nvim lsp_symbols: %d checks, %d failed"):format(#results, failed))
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
  auto_start = true,
  debounce_ms = 50,
  lsp = true,
})

local store = proj .. "/store.py"
local store_uri = vim.uri_from_fname(store)
local money = proj .. "/money.py"
local money_uri = vim.uri_from_fname(money)

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

-- The server advertises the new providers.
local caps = client.server_capabilities or {}
check("documentSymbolProvider advertised", caps.documentSymbolProvider == true)
check("workspaceSymbolProvider advertised", caps.workspaceSymbolProvider == true)
check("callHierarchyProvider advertised", caps.callHierarchyProvider == true)

-- ── Part 1: textDocument/documentSymbol on store.py ─────────────────────────
-- store.py defines two top-level functions: checkout + show_label.
local ds = vim.lsp.buf_request_sync(
  bufnr,
  "textDocument/documentSymbol",
  { textDocument = { uri = store_uri } },
  5000
)
local syms = ds and ds[client.id] and ds[client.id].result
check("documentSymbol returns a list", type(syms) == "table" and #syms >= 2, syms and ("count=" .. #syms) or nil)
local by_name = {}
for _, s in ipairs(syms or {}) do
  by_name[s.name] = s
end
check("documentSymbol includes checkout", by_name["checkout"] ~= nil)
check("documentSymbol includes show_label", by_name["show_label"] ~= nil)
if by_name["checkout"] then
  -- SymbolKind.Function = 12; each symbol carries a range + selectionRange.
  check("checkout kind is Function (12)", by_name["checkout"].kind == 12, tostring(by_name["checkout"].kind))
  check(
    "checkout carries range + selectionRange",
    by_name["checkout"].range ~= nil and by_name["checkout"].selectionRange ~= nil
  )
end

-- ── Part 2: workspace/symbol {query="checkout"} ─────────────────────────────
local ws = vim.lsp.buf_request_sync(bufnr, "workspace/symbol", { query = "checkout" }, 5000)
local wsyms = ws and ws[client.id] and ws[client.id].result
check("workspace/symbol returns a result", type(wsyms) == "table" and #wsyms >= 1, wsyms and ("count=" .. #wsyms) or nil)
if wsyms and wsyms[1] then
  local hit
  for _, s in ipairs(wsyms) do
    if s.name == "checkout" then
      hit = s
    end
  end
  check("workspace/symbol includes checkout", hit ~= nil)
  if hit then
    check(
      "checkout result carries a location uri + range",
      hit.location ~= nil and hit.location.uri ~= nil and hit.location.range ~= nil,
      hit.location and tostring(hit.location.uri) or nil
    )
    check(
      "checkout location resolves to store.py",
      hit.location and tostring(hit.location.uri):find("store.py", 1, true) ~= nil
    )
  end
end

-- ── Part 3: call hierarchy — callers of usd (money.py) ──────────────────────
-- Aim at the `usd` name token on money.py:1 (`def usd(...)`).
local money_lines = vim.fn.readfile(money)
local usd_text = money_lines[1] or ""
local usd_byte = usd_text:find("usd", 1, true) -- ASCII == codepoint here
local cparams = {
  textDocument = { uri = money_uri },
  position = { line = 0, character = (usd_byte or 1) - 1 },
}
local pch = vim.lsp.buf_request_sync(bufnr, "textDocument/prepareCallHierarchy", cparams, 5000)
local items = pch and pch[client.id] and pch[client.id].result
check(
  "prepareCallHierarchy returns an item for usd",
  type(items) == "table" and items[1] and items[1].name == "usd",
  items and items[1] and items[1].name or vim.inspect(items)
)

if items and items[1] then
  check("call item carries uri + selectionRange", items[1].uri ~= nil and items[1].selectionRange ~= nil)
  local inc = vim.lsp.buf_request_sync(bufnr, "callHierarchy/incomingCalls", { item = items[1] }, 5000)
  local calls = inc and inc[client.id] and inc[client.id].result
  check("incomingCalls returns an array", type(calls) == "table")
  local callers = {}
  local checkout_call
  for _, c in ipairs(calls or {}) do
    callers[c.from.name] = true
    if c.from.name == "checkout" then
      checkout_call = c
    end
  end
  check("usd's incoming callers include checkout", callers["checkout"] == true, vim.inspect(vim.tbl_keys(callers)))
  if checkout_call then
    check("incoming call carries fromRanges (call sites)", type(checkout_call.fromRanges) == "table" and #checkout_call.fromRanges >= 1)
  end
end

-- ── Report ──────────────────────────────────────────────────────────────────
report_and_exit(proj)
