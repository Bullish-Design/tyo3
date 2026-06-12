# tyo3.nvim — SYMBOLS demo (proj 28, Phase A). The native LSP symbol surfaces,
# all answered from tyo3's durable code graph. Render with:
#   devenv shell -- demo-record-symbols
#   (or: vhs editors/tyo3.nvim/demo/symbols/symbols.tape)
#
# Four beats, each a stock Neovim LSP command driving tyo3 — no tyo3-specific UI:
#   1. documentSymbol  — the file outline (vim.lsp.buf.document_symbol → loclist);
#   2. workspace symbol — project-wide, across files (…workspace_symbol);
#   3. outgoing calls   — what `checkout` calls (…outgoing_calls → quickfix);
#   4. incoming calls    — who calls `usd`, cross-file (…incoming_calls).
# The cold open + first index is kept OFF camera with Hide/Show so the on-camera
# queries are instant. Sleeps wait past every async daemon round-trip.

Output editors/tyo3.nvim/demo/symbols/symbols.gif
Output editors/tyo3.nvim/demo/symbols/symbols.txt

Require nvim
Require python

Set Shell "bash"
Set FontSize 16
Set Width 1360
Set Height 840
Set Framerate 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms

# ── Pre-warm OFF camera: open store.py and run one query so the daemon has ──
# ── opened + indexed the project (no cold-start dead air on stage). ──
Type "source editors/tyo3.nvim/demo/setup.sh && cd $TYO3_DEMO_DIR" Enter
Sleep 2s
Hide
Type "$TYO3_NVIM --clean -u $TYO3_PLUGIN_DIR/demo/symbols/init.lua store.py" Enter
Sleep 7s
Type ":lua vim.lsp.buf.document_symbol()" Enter
Sleep 3s
Type ":silent! lclose | silent! cclose" Enter
Sleep 500ms
Show

# ── Beat 1: documentSymbol — store.py's outline, from the durable graph ──
# Breadcrumbs / outline / `gO` / Snacks.picker.lsp_symbols all ride this.
Type ":lua vim.lsp.buf.document_symbol()" Enter
Sleep 3500ms
Type ":silent! lclose | silent! cclose" Enter
Sleep 1s

# ── Beat 2: workspace symbol — project-wide, across files ──
# `label` matches Item.label (catalog.py) AND show_label (store.py).
Type ":lua vim.lsp.buf.workspace_symbol('label')" Enter
Sleep 3500ms
Type ":silent! lclose | silent! cclose" Enter
Sleep 1s

# ── Beat 3: outgoing calls — what `checkout` depends on ──
# checkout calls usd(...) and Book().price() → usd, Book, price.
Type "/checkout" Enter
Sleep 1200ms
Type ":lua vim.lsp.buf.outgoing_calls()" Enter
Sleep 3500ms
Type ":silent! lclose | silent! cclose" Enter
Sleep 1s

# ── Beat 4: incoming calls — who calls `usd` (cross-file) ──
# Open money.py, land on the usd definition → checkout is the caller (store.py).
Type ":e money.py" Enter
Sleep 1500ms
Type "/usd" Enter
Sleep 1000ms
Type ":lua vim.lsp.buf.incoming_calls()" Enter
Sleep 3500ms

Type ":qa!" Enter
Sleep 1s
