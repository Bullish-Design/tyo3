# tyo3.nvim — EDGY ACCORDION SIDEBAR demo (proj 28, Phase E).
# Shows the edgy-managed sidebar tracking the cursor with the content-driven
# accordion. Five vertically stacked views on the right edge: IDENTITY / NOTES /
# DOCS / SUMMARY / AFFECTED.
# Render with:
#   devenv shell -- demo-record-sidebar
#
# Beats:
#   1. :TyO3Sidebar → opens the edgy right-edge sidebar with 5 stacked views.
#   2. Cursor to checkout → the sidebar opens with IDENTITY showing checkout's
#      card (name·kind, durable id, location). NOTES shows "(none)".
#   3. Cursor to show_label → sidebar tracks to show_label. Then back to checkout
#      → IDENTITY tracks back.
#   4. :TyO3Note author a note → cursor moves to show_label and back to force a
#      context refresh → NOTES section now shows the note 🏷 "load-bearing
#      checkout path".

Output editors/tyo3.nvim/demo/sidebar/sidebar.gif
Output editors/tyo3.nvim/demo/sidebar/sidebar.webm
Output editors/tyo3.nvim/demo/sidebar/sidebar.txt

Require nvim
Require python

Set Shell "bash"
Set FontSize 16
Set Width 1360
Set Height 840
Set Framerate 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms

# ── Pre-warm OFF camera ──
Type "source editors/tyo3.nvim/demo/setup.sh && cd $TYO3_DEMO_DIR" Enter
Sleep 2s
Hide
Type "$TYO3_NVIM --clean -u $TYO3_PLUGIN_DIR/demo/sidebar/init.lua store.py" Enter
Sleep 8s
Show

# ── Beat 0: explicitly open the sidebar ──
Type ":TyO3Sidebar" Enter
Sleep 3000ms

# ── Beat 1: move to checkout → IDENTITY shows checkout's card ──
Type "/def checkout" Enter
Sleep 4000ms

# ── Beat 2: move to show_label → IDENTITY tracks to show_label ──
Type "/def show_label" Enter
Sleep 4000ms

# ── Beat 3: return to checkout, author a note ──
Type "/def checkout" Enter
Sleep 2000ms

# Author a note via :TyO3Note (deterministic).
Type ":TyO3Note load-bearing checkout path" Enter
Sleep 2000ms

# Move to show_label (different function, triggers context.on_cursor) then back
# to checkout — forces entity_at re-resolution so the sidebar NOTES picks up the
# new note.
Type "/def show_label" Enter
Sleep 2000ms
Type "/def checkout" Enter
Sleep 4000ms

Type ":qa!" Enter
Sleep 1s
