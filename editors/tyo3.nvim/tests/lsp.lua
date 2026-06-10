-- Headless integration test for the native LSP bridge (lua/tyo3/lsp.lua).
--
-- Spawns a real tyo3-daemon against a fresh copy of the synthetic shop project,
-- opens store.py with `lsp = true`, attaches the in-process `vim.lsp` server,
-- and drives the bridged verbs through Neovim's *native* machinery
-- (`vim.lsp.buf_request_sync`): hover, references, documentHighlight,
-- prepareRename + rename (asserting the TextEdit range maps back to the symbol
-- exactly), and pull diagnostics. Prints PASS/FAIL per check and exits non-zero
-- on any failure (so it gates in CI).
--
-- Run:
--   nvim --headless -u editors/tyo3.nvim/tests/minimal_init.lua \
--        -c "luafile editors/tyo3.nvim/tests/lsp.lua"

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
  print(("tyo3.nvim lsp: %d checks, %d failed"):format(#results, failed))
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

-- ── Configure the plugin with lsp = true ────────────────────────────────────
require("tyo3").setup({
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  auto_start = true,
  debounce_ms = 50,
  lsp = true,
})

local store = proj .. "/store.py"
vim.cmd("edit " .. vim.fn.fnameescape(store))
vim.bo.filetype = "python"
local bufnr = vim.api.nvim_get_current_buf()

-- ── Phase 0: connect, open, sync, decorate (so the project is indexed and we
-- learn checkout's range to aim the LSP positions at) ───────────────────────
local prepared, prep_err
local ranges = {}

require("tyo3").with_client(bufnr, function(client, root)
  client:request("open", { root = root }, function(oerr)
    if oerr then
      prep_err = oerr.message
      prepared = true
      return
    end
    client:request("sync_buffer", { path = store, text = table.concat(
      vim.api.nvim_buf_get_lines(bufnr, 0, -1, false),
      "\n"
    ) .. "\n" }, function(serr)
      if serr then
        prep_err = serr.message
        prepared = true
        return
      end
      client:request("decorate", { path = store }, function(derr, items)
        if not derr and type(items) == "table" then
          for _, it in ipairs(items) do
            ranges[it.name] = it
          end
        end
        prepared = true
      end)
    end)
  end)
end, function(msg)
  prep_err = msg
  prepared = true
end)

vim.wait(30000, function()
  return prepared
end, 50)

check("daemon connected + project synced", not prep_err, prep_err)
check("decorate carries checkout range", ranges.checkout and ranges.checkout.range and true or false)

if prep_err or not ranges.checkout then
  report_and_exit(proj)
  return
end

-- `decorate` reports the entity's *full* range (the `def` statement), whose
-- start column is the `d` of `def`. Hover/rename need the *identifier*, so aim
-- at the `checkout` name on the declaration line (ASCII, so byte == codepoint).
local decl_line = ranges.checkout.range.start.line -- 1-based (== 6)
local decl_text = vim.api.nvim_buf_get_lines(bufnr, decl_line - 1, decl_line, false)[1] or ""
local name_byte = decl_text:find("checkout", 1, true) -- 1-based byte index of `c`
check("found checkout identifier on its declaration line", name_byte ~= nil, decl_text)
if not name_byte then
  report_and_exit(proj)
  return
end
local lsp_line = decl_line - 1
local lsp_char = name_byte - 1 -- 0-based

local function pos_params(extra)
  local p = {
    textDocument = { uri = vim.uri_from_fname(store) },
    position = { line = lsp_line, character = lsp_char },
  }
  for k, v in pairs(extra or {}) do
    p[k] = v
  end
  return p
end

-- ── Phase 1: the tyo3 LSP client attaches to the buffer ─────────────────────
-- (on_buf_enter already called attach when the buffer was opened with lsp=true;
-- attach is idempotent, so calling it again is harmless and removes test-order
-- dependence on the autocmd having fired.)
require("tyo3.lsp").attach(bufnr, proj)

local attached = vim.wait(10000, function()
  return #vim.lsp.get_clients({ name = "tyo3", bufnr = bufnr }) > 0
end, 50)
check("tyo3 LSP client attaches to the buffer", attached)

local clients = vim.lsp.get_clients({ name = "tyo3", bufnr = bufnr })
local client = clients[1]
check("client negotiated utf-32 position encoding", client and client.offset_encoding == "utf-32", client and client.offset_encoding)

if not client then
  report_and_exit(proj)
  return
end

-- Wait until initialize has completed (our in-process server answers it
-- synchronously, so this is effectively immediate).
vim.wait(5000, function()
  return client.initialized == true
end, 25)

-- ── Phase 2: hover ──────────────────────────────────────────────────────────
local hov = vim.lsp.buf_request_sync(bufnr, "textDocument/hover", pos_params(), 5000)
local hov_res = hov and hov[client.id] and hov[client.id].result
local hov_value = hov_res and hov_res.contents and hov_res.contents.value
check(
  "hover returns non-empty markdown contents",
  type(hov_value) == "string" and #hov_value > 0,
  hov_value
)
check("hover carries a range", hov_res and hov_res.range ~= nil)

-- ── Phase 3: references ─────────────────────────────────────────────────────
local refs = vim.lsp.buf_request_sync(
  bufnr,
  "textDocument/references",
  pos_params({ context = { includeDeclaration = true } }),
  5000
)
local refs_res = refs and refs[client.id] and refs[client.id].result
check(
  "references returns >=1 Location",
  type(refs_res) == "table" and #refs_res >= 1,
  refs_res and ("count=" .. #refs_res) or nil
)
check(
  "reference Location has uri + range",
  refs_res and refs_res[1] and refs_res[1].uri ~= nil and refs_res[1].range ~= nil
)

-- ── Phase 4: documentHighlight ──────────────────────────────────────────────
local dh = vim.lsp.buf_request_sync(bufnr, "textDocument/documentHighlight", pos_params(), 5000)
local dh_res = dh and dh[client.id] and dh[client.id].result
check(
  "documentHighlight returns >=1 highlight",
  type(dh_res) == "table" and #dh_res >= 1,
  dh_res and ("count=" .. #dh_res) or nil
)

-- ── Phase 5: prepareRename + rename (range maps back exactly) ───────────────
local pr = vim.lsp.buf_request_sync(bufnr, "textDocument/prepareRename", pos_params(), 5000)
local pr_res = pr and pr[client.id] and pr[client.id].result
check("prepareRename returns a Range", pr_res and pr_res.start and pr_res["end"] and true or false, vim.inspect(pr_res))

local rn = vim.lsp.buf_request_sync(
  bufnr,
  "textDocument/rename",
  pos_params({ newName = "buy" }),
  5000
)
local rn_res = rn and rn[client.id] and rn[client.id].result
local changes = rn_res and rn_res.changes
local store_uri = vim.uri_from_fname(store)
local store_edits = changes and changes[store_uri]
check(
  "rename produces a WorkspaceEdit with a TextEdit for store.py",
  type(store_edits) == "table" and #store_edits >= 1,
  changes and vim.inspect(vim.tbl_keys(changes)) or nil
)

if store_edits and store_edits[1] then
  -- Find the edit covering the declaration (line == lsp_line) and confirm its
  -- range, applied to the buffer text, clobbers exactly "checkout" — the
  -- inclusive/exclusive off-by-one tell for rename precision.
  local decl_edit
  for _, e in ipairs(store_edits) do
    if e.range.start.line == lsp_line then
      decl_edit = e
    end
  end
  check("rename TextEdit covers the declaration line", decl_edit ~= nil)
  if decl_edit then
    local line_text = vim.api.nvim_buf_get_lines(bufnr, lsp_line, lsp_line + 1, false)[1] or ""
    -- LSP range is 0-based, end-exclusive; Lua sub() is 1-based, end-inclusive.
    local covered = line_text:sub(decl_edit.range.start.character + 1, decl_edit.range["end"].character)
    check("rename range maps back to the symbol exactly", covered == "checkout", covered)
    check("rename newText is the new name", decl_edit.newText == "buy", decl_edit.newText)
  end
end

-- ── Phase 6: pull diagnostics ───────────────────────────────────────────────
local dg = vim.lsp.buf_request_sync(
  bufnr,
  "textDocument/diagnostic",
  { textDocument = { uri = store_uri } },
  10000
)
local dg_res = dg and dg[client.id] and dg[client.id].result
check(
  "pull diagnostic returns a full report",
  type(dg_res) == "table" and dg_res.kind == "full" and type(dg_res.items) == "table",
  dg_res and vim.inspect(dg_res.kind) or nil
)

-- ── Report ──────────────────────────────────────────────────────────────────
report_and_exit(proj)
