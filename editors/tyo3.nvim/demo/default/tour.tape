# tyo3.nvim — scripted demo. Render from the repo root with:
#   devenv shell -- demo-record   (or: vhs editors/tyo3.nvim/demo/default/tour.tape)
#
# Drives a real terminal Neovim through the plugin's verbs against the synthetic
# "shop" project. Scenes 1-6 are tyo3's bespoke UI (inspect / note / move /
# affected panel); scenes 7-12 flip on the opt-in native LSP bridge (:TyO3Lsp) and
# drive Neovim's own machinery through tyo3 — hover (K) + goto-definition (<C-]>),
# project references (grr), type hierarchy (super/subtypes → quickfix), a
# cross-file rename, and the headline: a durable note's drift (needs_review) and a
# type-checker error riding the *same* ]d stream, surfaced on a normal :w now that
# review-state is durable. Sleeps are generous and deliberate — we wait past every
# async beat (daemon open, debounced commit, precision refinement, pushed
# diagnostics, bus-driven layer refresh) so the recording is deterministic.
# Determinism over speed.

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

# ── Scene 10: type hierarchy — supertypes & subtypes (→ quickfix) ──
# A genuinely structural query, not just goto-def: Book extends Item, Item is
# extended by Book. tyo3 answers textDocument/prepareTypeHierarchy +
# typeHierarchy/supertypes|subtypes; Neovim's vim.lsp.buf.typehierarchy lists the
# result in the quickfix. (book.py / catalog.py were indexed on open — no sync.)
Type ":e book.py" Enter
Sleep 1500ms
Type "/Book(" Enter
Sleep 600ms
# typehierarchy is a two-hop async query (prepare → supertypes → quickfix). Run it
# with the cursor in the CODE buffer (a :copen first would steal focus to the
# quickfix window, which has no LSP client), wait past the round trip, then open
# the now-populated quickfix.
Type ":lua vim.lsp.buf.typehierarchy('supertypes')" Enter
Sleep 4500ms
Type ":copen" Enter
Sleep 2500ms
Type ":cclose" Enter
Sleep 600ms
Type ":e catalog.py" Enter
Sleep 1200ms
Type "/Item" Enter
Sleep 600ms
Type ":lua vim.lsp.buf.typehierarchy('subtypes')" Enter
Sleep 4500ms
Type ":copen" Enter
Sleep 2500ms
Type ":cclose" Enter
Sleep 800ms

# ── Scene 11: project-wide rename (grn's engine), cross-file ──
# Rename the Item class → Product. tyo3's `rename` verb returns a WorkspaceEdit
# spanning every file that uses it (catalog.py defines it; book.py subclasses it;
# store.py constructs it) and Neovim applies it. The same identity index behind
# references drives the rename — one edit, three files.
Type "/Item" Enter
Sleep 600ms
Type ":lua vim.lsp.buf.rename('Product')" Enter
Sleep 3s
Type ":e store.py" Enter
Sleep 2s
Type "/Product().label" Enter
Sleep 2500ms

# ── Scene 12: the headline — durable review-state AND a type error, unified in ]d ──
# Author durable intent on show_label, then make one edit that both *drifts* the
# note (Product().label → Product().price changes the body) and breaks the return
# type (price() is int, show_label is -> str). tyo3 surfaces the drift as a native
# `needs_review` diagnostic and the type error rides the same stream — ]d walks
# both. Type-checker diagnostics are *pushed* live on :w; the layer state refreshes
# off the same bus delta. We `:w` like any editor would — needs_review is durable
# (anchored to the body at author time) and the write path dedups the save's
# double-commit, so saving no longer clears the flag.
Type "/def show_label" Enter
Sleep 600ms
Type ":TyO3Note keep this label stable" Enter
Sleep 2000ms
Type "/\.label" Enter
Type "l"
Type "ciwprice"
Escape
Sleep 800ms
Type ":w" Enter
Sleep 6s
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
