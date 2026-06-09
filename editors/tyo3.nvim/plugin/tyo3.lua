-- tyo3.nvim — commands + autocmds (loaded once on startup).

if vim.g.loaded_tyo3 then
  return
end
vim.g.loaded_tyo3 = true

local function cmd(name, fn, opts)
  vim.api.nvim_create_user_command(name, fn, opts or {})
end

-- ── Commands ────────────────────────────────────────────────────────────────

cmd("TyO3Inspect", function()
  require("tyo3.inspect").inspect()
end, { desc = "Inspect the entity under the cursor" })

cmd("TyO3Note", function(a)
  require("tyo3.notes").note(a.args)
end, { nargs = "*", desc = "Author an intent note on the entity under the cursor" })

cmd("TyO3Doc", function()
  require("tyo3.entitydoc").edit()
end, { desc = "Write/edit the markdown doc for the entity under the cursor" })

cmd("TyO3Docs", function()
  require("tyo3.docs").index()
end, { desc = "Open the TyO3 documentation (user + developer)" })

cmd("TyO3Affected", function()
  require("tyo3.telescope").affected()
end, { desc = "Telescope the last edit's affected set" })

cmd("TyO3Entities", function()
  require("tyo3.telescope").entities()
end, { desc = "Telescope all known entities" })

cmd("TyO3Authored", function()
  require("tyo3.telescope").authored()
end, { desc = "Telescope authored notes" })

cmd("TyO3Diff", function(a)
  require("tyo3.diff").show(a.args)
end, { nargs = "*", desc = "Entity-level diff: :TyO3Diff <from_rev> [to_rev]" })

cmd("TyO3Move", function(a)
  local name, dest = a.fargs[1], a.fargs[2]
  require("tyo3.move").move(name, dest)
end, { nargs = "+", desc = "Atomic entity move: :TyO3Move <name> <dest_path>" })

cmd("TyO3Panel", function()
  require("tyo3.panel").toggle()
end, { desc = "Toggle the affected-set panel" })

cmd("TyO3Context", function()
  require("tyo3.context").toggle()
end, { desc = "Toggle the cursor-context panel section (context = cursor/off)" })

cmd("TyO3Start", function()
  local bufnr = vim.api.nvim_get_current_buf()
  require("tyo3").on_buf_enter(bufnr)
end, { desc = "Start/attach the TyO3 daemon for this buffer's project" })

cmd("TyO3Stop", function()
  require("tyo3").shutdown()
  vim.notify("[tyo3] daemons stopped", vim.log.levels.INFO)
end, { desc = "Stop all TyO3 daemons spawned by this session" })

cmd("TyO3Reindex", function()
  require("tyo3.overseer").run("reindex")
end, { desc = "Reindex the project (sync_all)" })

cmd("TyO3Gc", function()
  require("tyo3.overseer").run("gc")
end, { desc = "Garbage-collect orphaned derived artifacts" })

cmd("TyO3Check", function()
  require("tyo3.overseer").run("check")
end, { desc = "Run the type-checker over the project" })

cmd("TyO3DaemonLog", function()
  require("tyo3.overseer").daemon_log()
end, { desc = "Show the captured daemon stderr log" })

-- ── Autocmds ────────────────────────────────────────────────────────────────

local group = vim.api.nvim_create_augroup("tyo3", { clear = true })

vim.api.nvim_create_autocmd({ "BufReadPost", "BufEnter" }, {
  group = group,
  pattern = "*.py",
  callback = function(ev)
    if require("tyo3.config").get().auto_start then
      require("tyo3").on_buf_enter(ev.buf)
    end
  end,
})

vim.api.nvim_create_autocmd({ "TextChanged", "TextChangedI" }, {
  group = group,
  pattern = "*.py",
  callback = function(ev)
    require("tyo3").on_text_changed(ev.buf)
  end,
})

vim.api.nvim_create_autocmd("BufWritePost", {
  group = group,
  pattern = "*.py",
  callback = function(ev)
    require("tyo3").sync_now(ev.buf)
  end,
})

vim.api.nvim_create_autocmd({ "CursorMoved", "CursorMovedI" }, {
  group = group,
  pattern = "*.py",
  callback = function(ev)
    require("tyo3.context").on_cursor(ev.buf)
  end,
})

vim.api.nvim_create_autocmd("VimLeavePre", {
  group = group,
  callback = function()
    require("tyo3").shutdown()
  end,
})
