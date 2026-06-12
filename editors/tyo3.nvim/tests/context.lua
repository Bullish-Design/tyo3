-- Headless integration test for the cursor-driven CONTEXT sidebar.
--
-- Spawns a real tyo3-daemon against a fresh copy of the synthetic shop project,
-- opens store.py (cursor context is always on, proj 28), and drives context.lua
-- end to end: the Treesitter enclosing-node gate, entity_at resolution + sidebar
-- buffer rendering, note surfacing, stale-drop, the no-parser fallback, and
-- always-on. Prints PASS/FAIL per check and exits non-zero on any failure.
--
-- Run:
--   nvim --headless --clean -u editors/tyo3.nvim/tests/minimal_init.lua \
--        -c "luafile editors/tyo3.nvim/tests/context.lua"

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
  print(("tyo3.nvim context: %d checks, %d failed"):format(#results, failed))
  require("tyo3").shutdown()
  -- Hard-exit instead of `:qall!`. With the full curated stack on the rtp a
  -- lingering libuv handle (a snacks/edgy/tiny-code-action timer) makes nvim's
  -- orderly quit *block* — :qall enters the quit sequence and never returns, so
  -- the gate's `timeout` SIGTERMs it (exit 124) despite every check passing. The
  -- assertions have run and the summary is flushed above, so a libc exit() with
  -- the right code is deterministic and correct for a headless one-shot spec.
  os.exit(failed > 0 and 1 or 0)
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

-- ── Configure the plugin (cursor context is always on, proj 28) ─────────────
require("tyo3").setup({
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  debounce_ms = 50,
  context_debounce_ms = 50,
})

local store = proj .. "/store.py"
vim.cmd("edit " .. vim.fn.fnameescape(store))
vim.bo.filetype = "python"
local bufnr = vim.api.nvim_get_current_buf()

-- Ensure a Python tree is built in headless mode before we read nodes.
pcall(vim.treesitter.start, bufnr, "python")
pcall(function()
  vim.treesitter.get_parser(bufnr, "python"):parse()
end)

local context = require("tyo3.context")
local sidebar = require("tyo3.sidebar")

-- Manually initialise sidebar state for dep-light testing (edgy not needed).
-- In the real path, deps.setup_edgy calls sidebar.setup(edgy) which creates
-- buffers and installs the accordion. Here we create the buffers directly and
-- set _did_setup so set_context renders into them.
local function sidebar_setup_light()
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
end

local function sidebar_text(ft)
  local buf = sidebar.bufs[ft]
  if not (buf and vim.api.nvim_buf_is_valid(buf)) then
    return ""
  end
  return table.concat(vim.api.nvim_buf_get_lines(buf, 0, -1, false), "\n")
end

local function sidebar_all_text()
  local parts = {}
  for _, ft in ipairs({ "tyo3_identity", "tyo3_notes", "tyo3_docs", "tyo3_summary", "tyo3_affected" }) do
    local t = sidebar_text(ft)
    if t ~= "" then
      table.insert(parts, t)
    end
  end
  return table.concat(parts, "\n")
end

local function buffer_text()
  return table.concat(vim.api.nvim_buf_get_lines(bufnr, 0, -1, false), "\n") .. "\n"
end

-- ── Phase 0: connect, open, sync, decorate (to learn entity ranges/ids) ─────
local prepared, prep_err
local ranges = {}

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
check("decorate carries checkout + show_label", ranges.checkout and ranges.show_label and true or false)

-- Set up sidebar buffers in dep-light mode (no edgy needed for render checks).
sidebar_setup_light()

if prep_err or not ranges.checkout then
  report_and_exit()
  return
end

local checkout_id = ranges.checkout.durable_id
local checkout_line = ranges.checkout.range.start.line -- 1-based (== 6)
local show_label_line = ranges.show_label.range.start.line -- 1-based (== 10)

-- ── Gate 1: Treesitter enclosing-node key ───────────────────────────────────
vim.api.nvim_win_set_cursor(0, { checkout_line, 4 })
local key_checkout, _, _, fb1 = context.node_key(bufnr)
check("TS gate resolves enclosing def (no fallback)", not fb1 and key_checkout:match("^%d+:%d+:%d+:%d+$") ~= nil, key_checkout)

local key_sr = tonumber(key_checkout:match("^(%d+):"))
check("key anchored at checkout def line", key_sr ~= nil and (key_sr + 1) == checkout_line, tostring(key_sr))

-- Moving within the same function does NOT change the key.
vim.api.nvim_win_set_cursor(0, { checkout_line + 1, 8 })
local key_same = context.node_key(bufnr)
check("same function → same key", key_same == key_checkout, key_same)

-- Moving to another def DOES change the key (and is not a line-fallback).
vim.api.nvim_win_set_cursor(0, { show_label_line, 4 })
local key_other, _, _, fb_other = context.node_key(bufnr)
check("different def → different key", key_other ~= key_checkout and not fb_other, key_other)

-- ── Gate 2: entity resolves + renders in the sidebar ────────────────────────
context._last_key[bufnr] = nil
vim.api.nvim_win_set_cursor(0, { checkout_line, 4 })
context.on_cursor(bufnr)
local got2 = vim.wait(30000, function()
  local t = sidebar_text("tyo3_identity")
  return t:find("checkout", 1, true) ~= nil and t:find("function", 1, true) ~= nil
end, 50)
check("entity resolves + renders in IDENTITY", got2, sidebar_text("tyo3_identity"))
do
  local it = sidebar_text("tyo3_identity")
  local nt = sidebar_text("tyo3_notes")
  local dt = sidebar_text("tyo3_docs")
  local st = sidebar_text("tyo3_summary")
  -- IDENTITY shows the full card (kind, durable id, location).
  check("IDENTITY shows kind", it:find("kind", 1, true) ~= nil, nil)
  check("IDENTITY shows durable id", it:find("durable id", 1, true) ~= nil, nil)
  check("NOTES buffer populated", #vim.api.nvim_buf_get_lines(sidebar.bufs["tyo3_notes"], 0, -1, false) > 0, nil)
  check("DOCS buffer populated", #vim.api.nvim_buf_get_lines(sidebar.bufs["tyo3_docs"], 0, -1, false) > 0, nil)
  check("SUMMARY buffer populated", #vim.api.nvim_buf_get_lines(sidebar.bufs["tyo3_summary"], 0, -1, false) > 0, nil)
  check(
    "DOCS links both user + dev guides",
    dt:find("User", 1, true) and dt:find("Dev", 1, true),
    nil
  )
end

-- ── Gate 3: notes (and docs/summary if present) surface ─────────────────────
local authored_done, author_err
require("tyo3").rpc(
  bufnr,
  "author",
  { layer = "intent", durable_id = checkout_id, value = { note = "load-bearing checkout path" } },
  function(e)
    author_err = e and e.message
    authored_done = true
  end
)
vim.wait(30000, function()
  return authored_done
end, 50)
check("author intent note ok", not author_err, author_err)

context._last_key[bufnr] = nil
vim.api.nvim_win_set_cursor(0, { checkout_line, 4 })
context.on_cursor(bufnr)
local got3 = vim.wait(30000, function()
  return sidebar_text("tyo3_notes"):find("load-bearing checkout path", 1, true) ~= nil
end, 50)
check("note surfaces in NOTES", got3, sidebar_text("tyo3_notes"))
-- Summary is derived/optional; report presence without forcing it.
check("summary surfaced (informational)", true, sidebar_text("tyo3_summary"):find("⟢", 1, true) and "present" or "absent")

-- ── Gate 3b: per-entity authored markdown doc surfaces in the DOCS buffer ─
local doc_done, doc_err
require("tyo3").rpc(
  bufnr,
  "author",
  { layer = "docs", durable_id = checkout_id, value = { markdown = "# Checkout doc\n\nThe load-bearing path." } },
  function(e)
    doc_err = e and e.message
    doc_done = true
  end
)
vim.wait(30000, function()
  return doc_done
end, 50)
check("author docs layer ok", not doc_err, doc_err)

context._last_key[bufnr] = nil
vim.api.nvim_win_set_cursor(0, { checkout_line, 4 })
context.on_cursor(bufnr)
local got_doc = vim.wait(30000, function()
  return sidebar_text("tyo3_docs"):find("Checkout doc", 1, true) ~= nil
end, 50)
check("authored doc surfaces in DOCS", got_doc, sidebar_text("tyo3_docs"))

-- ── Gate 4: stale-drop — only the latest key's card is rendered ─────────────
context._last_key[bufnr] = nil
vim.api.nvim_win_set_cursor(0, { checkout_line, 4 })
context.on_cursor(bufnr) -- schedules a checkout lookup
vim.api.nvim_win_set_cursor(0, { show_label_line, 4 })
context.on_cursor(bufnr) -- supersedes it before the debounce fires
local got4 = vim.wait(30000, function()
  return sidebar_text("tyo3_identity"):find("show_label", 1, true) ~= nil
end, 50)
local t4 = sidebar_text("tyo3_identity")
check(
  "stale-drop: only latest entity rendered",
  got4 and t4:find("load-bearing checkout path", 1, true) == nil,
  t4
)

-- ── Gate 5: no-parser fallback keys by line and does not error ──────────────
local fb_buf = vim.api.nvim_create_buf(true, false)
vim.api.nvim_buf_set_lines(fb_buf, 0, -1, false, { "plain text", "no parser here" })
vim.api.nvim_set_current_buf(fb_buf)
vim.bo[fb_buf].filetype = "" -- nothing Treesitter can parse under --clean
vim.api.nvim_win_set_cursor(0, { 2, 1 })
local ok5, key5, _, _, fb5 = pcall(context.node_key, fb_buf)
check("fallback: node_key does not error", ok5, ok5 and "" or tostring(key5))
check("fallback: keys by cursor line", ok5 and fb5 == true and key5 == "line:2", tostring(key5))

-- ── Gate 6: cursor context is always on (no flag, no opt-out) ───────────────
sidebar.clear_context()
vim.api.nvim_set_current_buf(bufnr)
vim.api.nvim_win_set_cursor(0, { checkout_line, 4 })
context._last_key[bufnr] = nil
context.on_cursor(bufnr)
local got6 = vim.wait(30000, function()
  return sidebar_text("tyo3_identity"):find("checkout", 1, true) ~= nil
end, 50)
check("always-on: on_cursor populates IDENTITY with no flag", got6, sidebar_text("tyo3_identity"))

-- ── Report ──────────────────────────────────────────────────────────────────
vim.fn.delete(proj, "rf")
report_and_exit()
