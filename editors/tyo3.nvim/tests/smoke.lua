-- Headless integration smoke test for tyo3.nvim.
--
-- Spawns a real tyo3-daemon (via `python -m tyo3.daemon`) against a fresh copy
-- of the synthetic shop project, then drives rpc.lua / daemon.lua end to end:
-- daemon spawn + connect, ping, decorate placement, entity_at, note authoring,
-- and a sync_buffer commit. Prints PASS/FAIL per check and exits non-zero on any
-- failure (so it gates in CI).
--
-- Run:
--   nvim --headless -u editors/tyo3.nvim/tests/minimal_init.lua \
--        -c "luafile editors/tyo3.nvim/tests/smoke.lua"

local results = {}
local function check(name, cond, detail)
  table.insert(results, { name = name, ok = cond and true or false, detail = detail })
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

-- ── Configure the plugin (force python -m so no console-script install needed) ──
require("tyo3").setup({
  daemon_cmd = { "python", "-m", "tyo3.daemon" },
  debounce_ms = 50,
})

-- Open store.py so we have a project buffer.
vim.cmd("edit " .. vim.fn.fnameescape(proj .. "/store.py"))
vim.bo.filetype = "python"
local bufnr = vim.api.nvim_get_current_buf()

local done = false
local checkout_id = nil

local function finish()
  done = true
end

require("tyo3").with_client(bufnr, function(client)
  check("daemon connected", true)

  -- 0) per-request timeout: a 1ms override on a real verb errors the callback
  --    with the timeout sentinel rather than hanging forever.
  client:request("ping", {}, function(terr)
    check(
      "1ms timeout errors the callback",
      terr and terr.code == -32099 and tostring(terr.message):match("timed out"),
      terr and terr.message
    )
  end, { timeout_ms = 1 })

  -- 1) ping
  client:request("ping", {}, function(err, res)
    check("ping ok", not err and res and res.ok == true, err and err.message)
    check("ping reports methods", res and res.methods and #res.methods > 0)

    -- 2) decorate store.py → find checkout
    client:request("decorate", { path = proj .. "/store.py" }, function(derr, items)
      local entry = nil
      if not derr and type(items) == "table" then
        for _, it in ipairs(items) do
          if it.name == "checkout" then
            entry = it
          end
        end
      end
      check("decorate finds checkout", entry ~= nil)
      if not entry then
        finish()
        return
      end
      checkout_id = entry.durable_id
      check("decorate carries a range", entry.range and entry.range.start and entry.range.start.line ~= nil)

      -- 3) entity_at at checkout's start
      local pos = entry.range.start
      client:request(
        "entity_at",
        { path = proj .. "/store.py", line = pos.line, col = pos.column },
        function(eerr, card)
          check(
            "entity_at resolves checkout",
            not eerr and card and card.qualified_name == "checkout",
            eerr and eerr.message
          )

          -- 4) author a note → read it back
          client:request(
            "author",
            { layer = "intent", durable_id = checkout_id, value = { note = "smoke note" } },
            function(aerr)
              check("author ok", not aerr, aerr and aerr.message)
              client:request("authored", { layer = "intent", durable_id = checkout_id }, function(rerr, av)
                check(
                  "authored round-trips",
                  not rerr and av and av.value and av.value.note == "smoke note",
                  rerr and rerr.message
                )

                -- 5) decorate again → note is present (placement source)
                client:request("decorate", { path = proj .. "/store.py" }, function(d2err, items2)
                  local has_note = false
                  if not d2err and type(items2) == "table" then
                    for _, it in ipairs(items2) do
                      if it.durable_id == checkout_id and it.note == "smoke note" then
                        has_note = true
                      end
                    end
                  end
                  check("decorate surfaces the note", has_note)

                  -- 6) sync_buffer commit (edit Item.price) → id-level delta
                  local catalog = "class Item:\n    def price(self) -> int:\n        return 250\n\n    def label(self) -> str:\n        return \"item\"\n"
                  client:request(
                    "sync_buffer",
                    { path = proj .. "/catalog.py", text = catalog },
                    function(serr, delta)
                      check(
                        "sync_buffer returns a delta",
                        not serr and delta and delta.revision ~= nil,
                        serr and serr.message
                      )
                      check(
                        "delta has affected ids",
                        delta and delta.affected_ids and #delta.affected_ids > 0
                      )
                      finish()
                    end
                  )
                end)
              end)
            end
          )
        end
      )
    end)
  end)
end, function(msg)
  check("daemon connected", false, msg)
  finish()
end)

-- ── Drive the event loop until the chain completes (or times out) ───────────
vim.wait(30000, function()
  return done
end, 50)

-- ── Report ──────────────────────────────────────────────────────────────────
local failed = 0
for _, r in ipairs(results) do
  local tag = r.ok and "PASS" or "FAIL"
  if not r.ok then
    failed = failed + 1
  end
  print(("[%s] %s%s"):format(tag, r.name, r.detail and (" — " .. tostring(r.detail)) or ""))
end
print(("tyo3.nvim smoke: %d checks, %d failed"):format(#results, failed))

require("tyo3").shutdown()
vim.fn.delete(proj, "rf")

-- Hard-exit (not :qall): the full curated stack can leave a libuv handle that
-- blocks nvim's orderly quit past the gate timeout. os.exit is deterministic.
os.exit((failed > 0 or not done) and 1 or 0)
