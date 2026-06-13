-- Headless test for the tyo3 hub (proj 28) — the observe/ops sibling of the act
-- surface. Dep-light (no curated stack needed): asserts `hub.bind_keymap` wires
-- a buffer-local normal-mode map from `config.keymaps.hub`, honours the `false`
-- opt-out, `register` appends an entry, and `hub.open()` degrades gracefully
-- (notify, no throw) when snacks is absent.
--
-- Run:
--   nvim --headless -u editors/tyo3.nvim/tests/minimal_init.lua \
--        -c "luafile editors/tyo3.nvim/tests/hub.lua"

local results = {}
local function check(name, cond, detail)
  table.insert(results, { name = name, ok = cond and true or false, detail = detail })
end

-- Find a buffer-local normal-mode map by its `desc` — leader-agnostic, so the
-- assertion holds regardless of the session's `mapleader`.
local HUB_DESC = "tyo3: open the hub (views + ops)"
local function has_hub_map(bufnr)
  for _, m in ipairs(vim.api.nvim_buf_get_keymap(bufnr, "n")) do
    if m.desc == HUB_DESC then
      return true
    end
  end
  return false
end

require("tyo3").setup({})
local hub = require("tyo3.hub")

-- ── Part 1: default config binds the hub map buffer-local ───────────────────
local buf = vim.api.nvim_create_buf(true, false)
hub.bind_keymap(buf)
check("hub bound buffer-local (normal)", has_hub_map(buf))

-- ── Part 2: keymaps.hub = false opts out ────────────────────────────────────
require("tyo3.config").setup({ keymaps = { hub = false } })
local buf_off = vim.api.nvim_create_buf(true, false)
hub.bind_keymap(buf_off)
check("hub=false binds no map", not has_hub_map(buf_off))
require("tyo3.config").setup({}) -- restore defaults

-- ── Part 3: register appends an entry ───────────────────────────────────────
local before = #hub._extra
hub.register({ label = "Spec entry", desc = "added by the spec", run = function() end })
check("register appends a hub entry", #hub._extra == before + 1)

-- ── Part 4: open() degrades gracefully without snacks (or opens with it) ─────
-- The dep-light harness has no snacks ⇒ open notifies and returns; the
-- provisioned harness opens the picker. Either way it must not throw.
local ok = pcall(hub.open)
check("hub.open() does not throw", ok)

-- ── Report ──────────────────────────────────────────────────────────────────
local failed = 0
for _, r in ipairs(results) do
  local tag = r.ok and "PASS" or "FAIL"
  if not r.ok then
    failed = failed + 1
  end
  print(("[%s] %s%s"):format(tag, r.name, r.detail and (" — " .. tostring(r.detail)) or ""))
end
print(("tyo3.nvim hub: %d checks, %d failed"):format(#results, failed))

require("tyo3").shutdown()
os.exit(failed > 0 and 1 or 0)
