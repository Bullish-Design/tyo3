-- Headless test for Phase D — AST-native navigation (proj 28, deps.lua).
--
-- Primary gate (dep-light, no stack needed): the buffer-local keymap binder
-- `deps.bind_ast_keymaps` wires every default textobject-select map (`af`/`if`/
-- `ac`/`ic`/`aa`/`ia`, modes x+o) and treewalker-motion map (`<C-k>/<C-j>/<C-h>/
-- <C-l>`, mode n) buffer-local, honours `manage = false` (binds nothing) and a
-- per-feature `false` opt-out. Binding lazy-requires the plugins, so the maps
-- bind with no stack installed — which is why this runs in the dep-light harness.
--
-- Secondary (stack-gated, skip-if-absent): select `@function.outer` over a
-- function then request `textDocument/codeAction` and assert the entity resolved
-- — proving the selection→entity path. Skips cleanly where the textobjects plugin
-- or a python parser isn't on the runtimepath (the standard sweep).
--
-- Run:
--   nvim --headless -u editors/tyo3.nvim/tests/minimal_init.lua \
--        -c "luafile editors/tyo3.nvim/tests/ast_nav.lua"

local results = {}
local function check(name, cond, detail)
  table.insert(results, { name = name, ok = cond and true or false, detail = detail })
end

-- A buffer-local mapping exists for `lhs` in `mode` on the *current* buffer.
-- maparg's dict form normalises `<C-k>`-style notation and reports `.buffer`.
local function has_buf_map(lhs, mode)
  local m = vim.fn.maparg(lhs, mode, false, true)
  return type(m) == "table" and m.buffer == 1 and m.lhs ~= nil
end

-- ── Build a fresh shop project (shared by both blocks) ──────────────────────
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
vim.cmd("edit " .. vim.fn.fnameescape(store))
vim.bo.filetype = "python"
local bufnr = vim.api.nvim_get_current_buf()

-- ── Part 1: keymap presence (default config, manage = true) ─────────────────
-- Call the binder explicitly (idempotent; the BufEnter autocmd also calls it).
require("tyo3.deps").bind_ast_keymaps(bufnr)

-- textobject selects bind on BOTH visual (x) and operator-pending (o).
for _, lhs in ipairs({ "af", "if", "ac", "ic", "aa", "ia" }) do
  check(("textobject `%s` bound buffer-local (visual)"):format(lhs), has_buf_map(lhs, "x"))
end
check("textobject `af` bound buffer-local (operator-pending)", has_buf_map("af", "o"))
check("textobject `if` bound buffer-local (operator-pending)", has_buf_map("if", "o"))

-- treewalker motion binds on normal (n).
for _, lhs in ipairs({ "<C-k>", "<C-j>", "<C-h>", "<C-l>" }) do
  check(("treewalker `%s` bound buffer-local (normal)"):format(lhs), has_buf_map(lhs, "n"))
end

-- ── Part 2: behavioural select → entity (stack-gated, skip-if-absent) ───────
-- Runs only when the textobjects plugin + a python parser are on the rtp (the
-- demo/Phase-F path); the standard dep-light sweep skips it.
local has_select = pcall(require, "nvim-treesitter-textobjects.select")
local has_parser = pcall(vim.treesitter.get_parser, bufnr, "python")
if has_select and has_parser then
  -- Attach the bridge + wait for the daemon so codeAction can resolve.
  require("tyo3.lsp").attach(bufnr, proj)
  vim.wait(10000, function()
    return #vim.lsp.get_clients({ name = "tyo3", bufnr = bufnr }) > 0
  end, 50)
  local client = vim.lsp.get_clients({ name = "tyo3", bufnr = bufnr })[1]
  -- Sync the buffer so the daemon has store.py's overlay.
  local synced
  require("tyo3").with_client(bufnr, function(c, root)
    c:request("open", { root = root }, function()
      local text = table.concat(vim.api.nvim_buf_get_lines(bufnr, 0, -1, false), "\n") .. "\n"
      c:request("sync_buffer", { path = store, text = text }, function()
        synced = true
      end)
    end)
  end)
  vim.wait(15000, function()
    return synced
  end, 50)

  -- Put the cursor inside `checkout`, select its outer function via the same
  -- entry point the keymap calls, then read the visual marks into an LSP range.
  local def_line
  for i, l in ipairs(vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)) do
    if l:match("^def checkout") then
      def_line = i
      break
    end
  end
  check("found checkout def line", def_line ~= nil)
  if def_line and client then
    vim.api.nvim_win_set_cursor(0, { def_line + 1, 4 }) -- inside the body
    require("nvim-treesitter-textobjects.select").select_textobject("@function.outer", "textobjects")
    vim.cmd("normal! \27") -- leave visual so '< '> are set
    local s = vim.api.nvim_buf_get_mark(bufnr, "<")
    local e = vim.api.nvim_buf_get_mark(bufnr, ">")
    check("function textobject selected a multi-line range", e[1] > s[1], ("%d..%d"):format(s[1], e[1]))

    local params = {
      textDocument = { uri = vim.uri_from_fname(store) },
      range = {
        start = { line = s[1] - 1, character = s[2] },
        ["end"] = { line = e[1] - 1, character = e[2] },
      },
      context = { diagnostics = {} },
    }
    local ca = vim.lsp.buf_request_sync(bufnr, "textDocument/codeAction", params, 5000)
    local actions = ca and ca[client.id] and ca[client.id].result
    local resolved = false
    for _, a in ipairs(actions or {}) do
      if a.command and tostring(a.command.command):match("^tyo3%.") then
        resolved = true
      elseif a.data and a.data.kind == "simplify" then
        resolved = true
      end
    end
    check("textobject selection resolves to the entity (codeAction offers tyo3 actions)", resolved)
  end
else
  print("[SKIP] behavioural select→entity — textobjects plugin / python parser not on rtp")
end

-- ── Part 3: manage = false ⇒ the binder is a no-op ──────────────────────────
require("tyo3.config").setup({ manage = false })
local buf_off = vim.api.nvim_create_buf(true, false)
vim.api.nvim_set_current_buf(buf_off)
require("tyo3.deps").bind_ast_keymaps(buf_off)
check("manage=false binds no textobject map", not has_buf_map("af", "x"))
check("manage=false binds no treewalker map", not has_buf_map("<C-k>", "n"))

-- ── Part 4: a per-feature `false` opts that feature out ─────────────────────
require("tyo3.config").setup({ keymaps = { treewalker = false } })
local buf_partial = vim.api.nvim_create_buf(true, false)
vim.api.nvim_set_current_buf(buf_partial)
require("tyo3.deps").bind_ast_keymaps(buf_partial)
check("treewalker=false keeps textobject maps", has_buf_map("af", "x"))
check("treewalker=false drops motion maps", not has_buf_map("<C-k>", "n"))

-- ── Report ──────────────────────────────────────────────────────────────────
local failed = 0
for _, r in ipairs(results) do
  local tag = r.ok and "PASS" or "FAIL"
  if not r.ok then
    failed = failed + 1
  end
  print(("[%s] %s%s"):format(tag, r.name, r.detail and (" — " .. tostring(r.detail)) or ""))
end
print(("tyo3.nvim ast_nav: %d checks, %d failed"):format(#results, failed))

require("tyo3").shutdown()
vim.fn.delete(proj, "rf")

if failed > 0 then
  vim.cmd("cquit 1")
else
  vim.cmd("qall!")
end
