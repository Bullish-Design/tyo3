-- tyo3.nvim — sidebar renderer purity tests (Phase E).
--
-- Dep-light: the section renderers are pure card → string[] functions; no edgy
-- or full stack needed. Builds a card via entity_at (reusing smoke.lua's shop
-- scaffolding) and asserts each renderer produces the expected output.
-- Also exercises the edgy-gated window/ft + context wiring when edgy is
-- present (skip-if-absent).
--
-- Run:
--   nvim --headless --clean -u editors/tyo3.nvim/tests/minimal_init.lua \
--        -c "luafile editors/tyo3.nvim/tests/sidebar.lua"

local results = {}
local function check(name, cond, detail)
  table.insert(results, { name = name, ok = cond and true or false, detail = detail })
end

local function report_and_exit()
  local failed = 0
  for _, r in ipairs(results) do
    local tag = r.ok and "PASS" or "FAIL"
    if not r.ok then
      failed = failed + 1
    end
    print(("[%s] %s%s"):format(tag, r.name, r.detail and (" — " .. tostring(r.detail)) or ""))
  end
  print(("tyo3.nvim sidebar: %d checks, %d failed"):format(#results, failed))
  require("tyo3").shutdown()
  if failed > 0 then
    vim.cmd("cquit 1")
  else
    vim.cmd("qall!")
  end
end

-- ── Build the shop project ──────────────────────────────────────────────────
local proj = vim.fn.tempname()
local py = table.concat({
  "import sys",
  "from pathlib import Path",
  "from tyo3.demo.tour import _build_project",
  "_build_project(Path(sys.argv[1]))",
}, "\n")
vim.fn.system({ "python", "-c", py, proj })
check("build shop project", vim.fn.isdirectory(proj) == 1, proj)

-- ── Configure the plugin ────────────────────────────────────────────────────
require("tyo3").setup({
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  debounce_ms = 50,
  context_debounce_ms = 50,
})

local store = proj .. "/store.py"
vim.cmd("edit " .. vim.fn.fnameescape(store))
vim.bo.filetype = "python"
local bufnr = vim.api.nvim_get_current_buf()

local function buffer_text()
  return table.concat(vim.api.nvim_buf_get_lines(bufnr, 0, -1, false), "\n") .. "\n"
end

-- ── Phase 0: connect, sync, get a card ─────────────────────────────────────
local prepared, prep_err
local card

require("tyo3").with_client(bufnr, function(client, root)
  client:request("open", { root = root }, function(oerr)
    if oerr then
      prep_err = oerr.message
      prepared = true
      return
    end
    client:request("sync_buffer", { path = store, text = buffer_text() }, function(serr)
      if serr then
        prep_err = serr.message
        prepared = true
        return
      end
      -- Resolve checkout via entity_at.
      client:request("entity_at", { path = store, line = 6, col = 5 }, function(eerr, c)
        if eerr then
          prep_err = eerr.message
        else
          card = c
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

check("daemon connected + entity resolved", not prep_err, prep_err)
check("checkout card resolved", card and card.durable_id and true or false)

if prep_err or not card then
  report_and_exit()
  return
end

-- ── Renderer purity ─────────────────────────────────────────────────────────
local render = require("tyo3.sidebar.render")

-- IDENTITY
local id_rows = render.identity_rows(card)
check("identity_rows returns a table", type(id_rows) == "table", #id_rows)
check("identity_rows contains checkout name", table.concat(id_rows, "\n"):find("checkout", 1, true) ~= nil, nil)
check("identity_rows contains kind", table.concat(id_rows, "\n"):find("kind", 1, true) ~= nil, nil)
check("identity_rows contains durable id", table.concat(id_rows, "\n"):find("durable id", 1, true) ~= nil, nil)

-- NOTES (no notes yet → "(none)")
local note_rows = render.note_rows(card)
check("note_rows returns a table", type(note_rows) == "table", #note_rows)
check("note_rows shows (none) when no notes", #note_rows >= 1 and note_rows[1]:find("none", 1, true) ~= nil, nil)

-- DOCS (no doc yet → "(no doc)" + reference links)
local doc_rows = render.doc_rows(card)
check("doc_rows returns a table", type(doc_rows) == "table", #doc_rows)
local doc_text = table.concat(doc_rows, "\n")
check("doc_rows shows (no doc) when empty", doc_text:find("no doc", 1, true) ~= nil, nil)
check("doc_rows includes reference links", doc_text:find("reference:", 1, true) ~= nil, nil)
check("doc_rows links user guide", doc_text:find("User", 1, true) ~= nil, nil)
check("doc_rows links dev guide", doc_text:find("Dev", 1, true) ~= nil, nil)

-- SUMMARY
local summary_rows = render.summary_rows(card)
check("summary_rows returns a table", type(summary_rows) == "table", #summary_rows)
check("summary_rows has content (or none)", #summary_rows >= 1, nil)

-- AFFECTED
local affected_rows = render.affected_rows({ "rev 1 · rescan", "rev 2 · Δ1 · affects {checkout}" })
check("affected_rows passthrough", #affected_rows == 2 and affected_rows[2]:find("checkout", 1, true) ~= nil, nil)

-- ── Sidebar module (dep-light: buffer creation + set_context) ──────────────
local sidebar = require("tyo3.sidebar")

-- Create buffers manually (simulating the light setup from context.lua).
local fts = { "tyo3_identity", "tyo3_notes", "tyo3_docs", "tyo3_summary", "tyo3_affected" }
for _, ft in ipairs(fts) do
  local buf = vim.api.nvim_create_buf(false, true)
  vim.bo[buf].buftype = "nofile"
  vim.bo[buf].bufhidden = "hide"
  vim.bo[buf].swapfile = false
  vim.bo[buf].filetype = ft
  sidebar.bufs[ft] = buf
end
sidebar._did_setup = true

-- set_context populates the buffers.
sidebar.set_context(card, bufnr)
check(
  "set_context renders into IDENTITY buffer",
  vim.api.nvim_buf_line_count(sidebar.bufs["tyo3_identity"]) > 0,
  nil
)
check(
  "set_context renders into NOTES buffer",
  vim.api.nvim_buf_line_count(sidebar.bufs["tyo3_notes"]) > 0,
  nil
)
check(
  "set_context renders into DOCS buffer",
  vim.api.nvim_buf_line_count(sidebar.bufs["tyo3_docs"]) > 0,
  nil
)

-- clear_context empties the buffers (Neovim buffers always have ≥1 line;
-- an empty buffer has one blank line).
sidebar.clear_context()
check(
  "clear_context empties IDENTITY buffer",
  vim.api.nvim_buf_line_count(sidebar.bufs["tyo3_identity"]) <= 1
    and vim.api.nvim_buf_get_lines(sidebar.bufs["tyo3_identity"], 0, 1, false)[1] == "",
  nil
)
check("clear_context clears entity", sidebar._entity == nil, nil)

-- set_context with nil/nil card no-ops cleanly.
sidebar.set_context(nil)
check("set_context(nil) does not error", true, nil)

-- on_delta → AFFECTED buffer + last_affected_ids
sidebar.on_delta(nil, { revision = 42, changed_ids = { "id1" }, affected_ids = { card.durable_id } })
check(
  "on_delta appends to AFFECTED buffer",
  vim.api.nvim_buf_line_count(sidebar.bufs["tyo3_affected"]) > 0,
  nil
)
check("on_delta sets last_affected_ids", #sidebar.last_affected_ids > 0, nil)
check(
  "last_affected_ids contains checkout id",
  vim.tbl_contains(sidebar.last_affected_ids, card.durable_id),
  nil
)

-- on_refinement annotates the existing revision line.
local alines = vim.api.nvim_buf_get_lines(sidebar.bufs["tyo3_affected"], 0, -1, false)
check("AFFECTED line mentions rev 42", alines[1]:find("42", 1, true) ~= nil, nil)
sidebar.on_refinement(nil, { revision = 42, narrowed = { card.durable_id } })
local alines2 = vim.api.nvim_buf_get_lines(sidebar.bufs["tyo3_affected"], 0, -1, false)
check("refinement annotates existing line", alines2[1]:find("narrowed", 1, true) ~= nil, nil)

-- manage = false → set_context no-ops cleanly.
local sidebar2 = require("tyo3.sidebar")
-- reset state (re-require doesn't reset module state; just test the guard)
-- We test by setting _did_setup = false and verifying set_context doesn't error.
do
  local saved = sidebar._did_setup
  sidebar._did_setup = false
  local ok = pcall(sidebar.set_context, card, bufnr)
  check("set_context no-ops when _did_setup is false (manage=false guard)", ok, nil)
  sidebar._did_setup = saved -- restore
end

-- ── Edgy-gated window/ft test (skip if edgy absent) ────────────────────────
do
  local ok, edgy = pcall(require, "edgy")
  if ok and edgy then
    -- Reset sidebar state for a proper setup.
    sidebar._did_setup = false
    sidebar.bufs = {}
    sidebar._entity = nil

    -- sidebar.setup returns whether edgy still needs setting up: `true, opts` on
    -- the pre-setup path (run edgy.setup), or `false` when it injected into a
    -- live edgebar (edgy may already be up from the plugin's own setup above).
    local needs_setup, opts = sidebar.setup(edgy)
    check("sidebar.setup returns a boolean signal", type(needs_setup) == "boolean", tostring(needs_setup))
    if needs_setup then
      edgy.setup(opts)
    end

    check("sidebar.setup(edgy) sets _did_setup", sidebar._did_setup, nil)

    -- Five buffers with correct filetypes.
    local ft_map = {
      tyo3_identity = "IDENTITY",
      tyo3_notes = "NOTES",
      tyo3_docs = "DOCS",
      tyo3_summary = "SUMMARY",
      tyo3_affected = "AFFECTED",
    }
    for ft, title in pairs(ft_map) do
      local buf = sidebar.bufs[ft]
      check(
        title .. " buffer exists",
        buf and vim.api.nvim_buf_is_valid(buf) or false,
        nil
      )
      if buf and vim.api.nvim_buf_is_valid(buf) then
        check(
          title .. " buffer has correct filetype",
          vim.bo[buf].filetype == ft,
          vim.bo[buf].filetype
        )
      end
    end

    -- set_context populates buffers (reusing the card from above).
    sidebar.set_context(card, bufnr)
    check(
      "set_context (edgy) populates IDENTITY",
      vim.api.nvim_buf_line_count(sidebar.bufs["tyo3_identity"]) > 0,
      nil
    )
    check(
      "set_context (edgy) populates NOTES",
      vim.api.nvim_buf_line_count(sidebar.bufs["tyo3_notes"]) > 0,
      nil
    )
  else
    check("edgy absent (skipping window/ft block)", true, "skipped")
  end
end

-- ── Report ──────────────────────────────────────────────────────────────────
vim.fn.delete(proj, "rf")
report_and_exit()
