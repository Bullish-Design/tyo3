# tyo3.nvim — scripted demo. Render from the repo root with:
#   devenv shell -- demo-record   (or: vhs editors/tyo3.nvim/demo/default/tour.tape)
#
# Drives a real terminal Neovim through the plugin's verbs against the synthetic
# "shop" project. Scenes 1-6 are tyo3's bespoke UI (inspect / note / move /
# affected panel); scenes 7-10 flip on the opt-in native LSP bridge (:TyO3Lsp) and
# drive Neovim's own K / <C-]> / grr / ]d through tyo3 — ending on the headline:
# a durable note's drift (needs_review) and a type-checker error riding the *same*
# ]d diagnostic stream. Sleeps are generous and deliberate — we wait past every
# async beat (daemon open, debounced commit, precision refinement, pushed
# diagnostics) so the recording is deterministic. Determinism over speed.

Output editors/tyo3.nvim/demo/default/tour.gif
Output editors/tyo3.nvim/demo/default/tour.txt

Require nvim
Require python

Set Shell "bash"
Set FontSize 18
Set Width 1280
Set Height 800
Set Theme "Catppuccin Mocha"
Set TypingSpeed 55ms

# Build the demo project and point a minimal nvim at tyo3.nvim.
Type "source editors/tyo3.nvim/demo/setup.sh && cd $TYO3_DEMO_DIR" Enter
Sleep 2s

# Scene 1 — open & ambient intelligence (daemon: open + initial decorate; panel).
# $TYO3_NVIM is the pristine neovim-unwrapped binary setup.sh resolved (the
# home-manager wrapper would inject a user after/ftplugin even under --clean);
# --clean + -u loads only $VIMRUNTIME + the plugin-only demo init.
Type "$TYO3_NVIM --clean -u $TYO3_PLUGIN_DIR/demo/default/init.lua store.py" Enter
Sleep 6s

# Scene 2 — inspect checkout.
Type "/def checkout" Enter
Sleep 800ms
Type ":TyO3Inspect" Enter
Sleep 3s
Escape
Sleep 500ms

# Scene 3 — author a durable note (virtual-text appears above checkout).
Type ":TyO3Note load-bearing checkout path" Enter
Sleep 2500ms

# Scene 4 — the money shot: move checkout to checkout.py; the note rides along.
Type ":TyO3Move checkout checkout.py" Enter
Sleep 3s
Type ":e checkout.py" Enter
Sleep 3s

# Scene 5 — edit a base method; watch the affected panel light up, then narrow.
Type ":e catalog.py" Enter
Sleep 1s
# Land the cursor on the literal 100 (the price body), then change just that
# number → 250. Searching "return 100" would put the cursor on `return`, so
# ciw would clobber the keyword instead of the value.
Type "/100" Enter
Sleep 500ms
Type "ciw250" Escape
Sleep 700ms
Type ":w" Enter
Sleep 5s

# Scene 6 — navigate the affected set (picker → jump).
Type ":TyO3Affected" Enter
Sleep 2s
Type "1" Enter
Sleep 2500ms

# ── Scene 7: opt in to the native LSP bridge (:TyO3Lsp) ──
# Everything so far is tyo3's *bespoke* UI. The same engine also speaks LSP: flip
# it on and tyo3 drives your own K / <C-]> / grr / ]d — one in-process server, no
# extra config. The bespoke panel keeps working alongside it.
Type ":e store.py" Enter
Sleep 1500ms
Type ":TyO3Lsp" Enter
Sleep 3s

# ── Scene 8: native hover (K) + goto-definition (<C-]>), served by tyo3 ──
# Land on the `Item` usage inside show_label; K shows the type, <C-]> jumps to the
# class in catalog.py, <C-o> comes back. These are Neovim's own defaults — tyo3 is
# just the language server behind them.
Type "/Item().label" Enter
Sleep 800ms
Type "K"
Sleep 2500ms
Ctrl+]
Sleep 2500ms
Ctrl+O
Sleep 1500ms

# ── Scene 9: project-wide references (grr → quickfix) ──
Type "grr"
Sleep 3s
Type ":cclose" Enter
Sleep 800ms

# ── Scene 10: the headline — bespoke review-state AND type errors, unified in ]d ──
# Author durable intent on show_label, then make one edit that both *drifts* the
# note (Item().label → Item().price changes the body) and breaks the return type
# (price() is int, show_label is -> str). tyo3 surfaces the drift as a native
# `needs_review` diagnostic and the type error rides the same stream — ]d walks
# both. The type-checker diagnostics are *pushed* live on :w, no polling.
Type "/def show_label" Enter
Sleep 600ms
Type ":TyO3Note keep this label stable" Enter
Sleep 2000ms
Type "/\.label" Enter
Type "l"
Type "ciwprice"
Escape
# One commit only: the debounced TextChanged sync overlays the edit (sync_buffer
# is overlay-only — no disk write). We deliberately do NOT `:w` — an identical
# re-commit would re-settle the body and clear the needs_review flag.
Sleep 4s
# The bus refreshes layer-state diagnostics asynchronously; force it here so the
# WARN is deterministically on-screen for the recording (demos wait past async
# beats — this makes that beat explicit).
Type ":lua local b=vim.api.nvim_get_current_buf(); require('tyo3.lsp').refresh_layer_diagnostics(b, require('tyo3').root_for_buf(b))" Enter
Sleep 2500ms
Type "gg"
Sleep 400ms
Type "]d"
Sleep 2500ms
Type ":lua vim.diagnostic.open_float()" Enter
Sleep 3s
Escape
Type "]d"
Sleep 2500ms
Type ":lua vim.diagnostic.open_float()" Enter
Sleep 3s
Escape

Type ":qa!" Enter
Sleep 1s
