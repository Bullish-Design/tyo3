-- Minimal init for headless tyo3.nvim tests.
-- Adds the plugin to the runtimepath so `require("tyo3.*")` and plugin/ load.

local here = vim.fn.fnamemodify(vim.fn.expand("<sfile>:p"), ":h")
local plugin_root = vim.fn.fnamemodify(here, ":h") -- editors/tyo3.nvim
-- Isolate from any ambient user config: keep filetype *detection* on (so .py
-- buffers are recognised) but disable ftplugin/indent so a stray user ftplugin
-- can't load. Then add only this plugin.
vim.cmd("filetype plugin indent off")
vim.opt.runtimepath:append(plugin_root)
vim.opt.swapfile = false
vim.cmd("runtime! plugin/tyo3.lua")
