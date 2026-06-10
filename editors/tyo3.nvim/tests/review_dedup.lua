-- Headless test for Part E — the editor's double-commit dedup (lua/tyo3/init.lua).
--
-- The editor commits the overlay twice per save: the debounced TextChanged sync,
-- then the BufWritePost sync of byte-identical text. A redundant re-commit
-- re-reconciles the file; under the engine's per-commit review semantics that can
-- clear durable level state (needs_review). `M.sync_now` now dedups on a
-- per-buffer content hash, so an unchanged buffer is never re-synced.
--
-- This drives the real choke point: author an intent note on legacy_helper, edit
-- its body through `M.sync_now` (the debounce path → flags needs_review), then
-- fire a save-equivalent `M.sync_now` with identical text and assert the head
-- revision did NOT advance (deduped) and the note is still flagged.
--
-- Run:
--   nvim --headless -u editors/tyo3.nvim/tests/minimal_init.lua \
--        -c "luafile editors/tyo3.nvim/tests/review_dedup.lua"

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
  print(("tyo3.nvim review_dedup: %d checks, %d failed"):format(#results, failed))
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

-- Drive the sync paths by hand: clear the plugin's BufEnter/TextChanged/
-- BufWritePost autocmds so buffer edits don't fire their own debounced commits.
-- This isolates the unit under test — `M.sync_now`'s per-buffer dedup — from the
-- autocmd fan-out (the real editor wires both; here we call them in sequence).
vim.api.nvim_create_augroup("tyo3", { clear = true })

local tyo3 = require("tyo3")
local legacy = proj .. "/legacy.py"

-- Synchronous-ish RPC helper: run *method* and return its result (or nil).
local function rpc_sync(bufnr, method, params, timeout)
  local done, result, rpc_err
  tyo3.rpc(bufnr, method, params, function(err, res)
    rpc_err, result, done = err, res, true
  end)
  vim.wait(timeout or 8000, function()
    return done
  end, 25)
  return result, rpc_err
end

local function head_rev(bufnr)
  local res = rpc_sync(bufnr, "ping", {})
  return res and res.revision
end

local function flagged(bufnr, id)
  local res = rpc_sync(bufnr, "review_state", { path = legacy })
  for _, it in ipairs(res and res.items or {}) do
    if it.durable_id == id and it.state == "needs_review" then
      return true
    end
  end
  return false
end

-- ── Open legacy.py: on_buf_enter syncs the original body + seeds the hash ────
vim.cmd("edit " .. vim.fn.fnameescape(legacy))
vim.bo.filetype = "python"
local bufnr = vim.api.nvim_get_current_buf()
tyo3.on_buf_enter(bufnr)

-- Wait until the buffer is synced (dedup hash seeded) and the daemon is live.
local synced = vim.wait(30000, function()
  return tyo3._last_synced[bufnr] ~= nil and head_rev(bufnr) ~= nil
end, 50)
check("legacy.py opened + synced (dedup hash seeded)", synced)
if not synced then
  report_and_exit(proj)
  return
end

-- ── Author an intent note on legacy_helper (review_on_change layer) ─────────
local legacy_id
do
  local deco = rpc_sync(bufnr, "decorate", { path = legacy })
  for _, d in ipairs(deco or {}) do
    if d.name == "legacy_helper" then
      legacy_id = d.durable_id
    end
  end
end
check("found legacy_helper durable id", legacy_id ~= nil)
if not legacy_id then
  report_and_exit(proj)
  return
end
do
  local _, aerr = rpc_sync(bufnr, "author", {
    layer = "intent",
    durable_id = legacy_id,
    value = { note = "load-bearing: do not inline" },
  })
  check("authored intent note on legacy_helper", aerr == nil, aerr and aerr.message)
end

-- ── Edit the body through sync_now (the debounce path) → flags needs_review ──
vim.api.nvim_buf_set_lines(bufnr, 0, -1, false, {
  "def legacy_helper(x: int) -> int:",
  "    return x + 100",
})
local rev_before_edit = head_rev(bufnr)
tyo3.sync_now(bufnr)
local committed = vim.wait(15000, function()
  local r = head_rev(bufnr)
  return r ~= nil and rev_before_edit ~= nil and r > rev_before_edit
end, 50)
check("editing the body advances head (a real commit)", committed)
local rev_after_edit = head_rev(bufnr)
check("legacy_helper flagged needs_review after the edit", flagged(bufnr, legacy_id))

-- ── Save-equivalent sync_now with IDENTICAL text → deduped, no commit ────────
-- The buffer is unchanged since the edit, so the hash matches _last_synced and
-- sync_now must skip the RPC entirely.
tyo3.sync_now(bufnr)
-- Give a real commit time to land if the dedup failed to suppress it.
vim.wait(1500, function()
  return false
end, 100)
local rev_after_save = head_rev(bufnr)
check(
  "save-equivalent sync_now did NOT advance the revision (deduped)",
  rev_after_save == rev_after_edit,
  ("before=%s after=%s"):format(tostring(rev_after_edit), tostring(rev_after_save))
)
check("needs_review survives the save (still flagged)", flagged(bufnr, legacy_id))

report_and_exit(proj)
