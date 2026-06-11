# tyo3.nvim — EDGY ACCORDION SIDEBAR demo (proj 28, Phase E).
# Shows the sidebar tracking the cursor with the content-driven accordion.
# Render with:
#   devenv shell -- demo-record-sidebar
#   (or: vhs editors/tyo3.nvim/demo/sidebar/sidebar.tape)
#
# Beats:
#   1. Land on `checkout` → sidebar opens: IDENTITY section expands, NOTES/DOCS/
#      SUMMARY collapse (empty). The dock is observe-only and cursor-driven.
#   2. Move to `show_label` → the sidebar tracks — IDENTITY updates showing
#      show_label's card. NOTES/DOCS/SUMMARY stay collapsed (still empty).
#   3. Return to `checkout`, author a note via `gra` → Author (intent) → type
#      a note → NOTES section expands with the new note.
#   4. The accordion is content-first: sections with data expand, empty ones
#      collapse to title height.

Output editors/tyo3.nvim/demo/sidebar/sidebar.gif
Output editors/tyo3.nvim/demo/sidebar/sidebar.txt

Require nvim
Require python

Set Shell "bash"
Set FontSize 16
Set Width 1360
Set Height 840
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms

# ── Pre-warm OFF camera: open store.py so the daemon opens + indexes ──
Type "source editors/tyo3.nvim/demo/setup.sh && cd $TYO3_DEMO_DIR" Enter
Sleep 2s
Hide
Type "$TYO3_NVIM --clean -u $TYO3_PLUGIN_DIR/demo/sidebar/init.lua store.py" Enter
Sleep 8s
Show

# ── Beat 1: land on `checkout` → the dock opens on it ──
# The sidebar shows IDENTITY expanded (checkout · function, durable id, kind),
# NOTES/DOCS/SUMMARY collapsed (empty — no notes yet).
Type "/def checkout" Enter
Sleep 3000ms

# ── Beat 2: move to `show_label` → sidebar tracks the tree ──
Type "/def show_label" Enter
Sleep 3000ms

# ── Beat 3: return to `checkout` ──
Type "/def checkout" Enter
Sleep 2000ms

# ── Beat 4: author a note → NOTES section expands ──
Type "gra"
Sleep 1500ms
# Navigate tiny-code-action buffer picker to "Author (intent)"
# (The menu groups by category; Author is under Source:)
Type "j" Sleep 100ms
Type "j" Sleep 100ms
Type "j" Sleep 100ms
Type "j" Sleep 100ms
Type "j" Sleep 100ms
Enter
Sleep 1500ms
# Type a note in the snacks input prompt
Type "this is the load-bearing checkout path" Enter
Sleep 3000ms

Type ":qa!" Enter
Sleep 1s
