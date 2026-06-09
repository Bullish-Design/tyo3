# tyo3.nvim — comprehensive CONTEXT demo. Render from the repo root with:
#   devenv shell -- demo-record-context
#   (or: vhs editors/tyo3.nvim/demo/context/context.tape)
#
# A guided tour of TyO3's real, user-facing capabilities with the data-type-
# separated sidebar on throughout. The right dock hosts collapsible panes, each a
# different kind of data hanging off the entity under the cursor — IDENTITY,
# NOTES, DOCS, SUMMARY, ACTIONS — plus an AFFECTED log. The headline: notes AND a
# markdown doc you author in-editor stay glued to the entity across an edit and an
# atomic move. Sleeps wait past every async beat so the recording is deterministic.

Output editors/tyo3.nvim/demo/context/context.gif
Output editors/tyo3.nvim/demo/context/context.txt

Require nvim
Require python

Set Shell "bash"
Set FontSize 15
Set Width 1440
Set Height 900
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms

# Build the synthetic shop project and resolve a pristine nvim ($TYO3_NVIM).
Type "source editors/tyo3.nvim/demo/setup.sh && cd $TYO3_DEMO_DIR" Enter
Sleep 2s
Type "$TYO3_NVIM --clean -u $TYO3_PLUGIN_DIR/demo/context/init.lua store.py" Enter
Sleep 6s

# ── Scene 1: the sidebar separates the data types on the code spine ──
# Land in `checkout`: IDENTITY / NOTES / DOCS / SUMMARY / ACTIONS panes fill in.
Type "/def checkout" Enter
Sleep 3500ms

# ── Scene 2: author durable intent (NOTES pane) ──
Type ":TyO3Note load-bearing checkout path" Enter
Sleep 1500ms
Type "/def show_label" Enter
Sleep 1200ms
Type "/def checkout" Enter
Sleep 2500ms

# ── Scene 3: write a markdown doc *while navigating* (DOCS pane) ──
# :TyO3Doc opens a markdown editor for the entity under the cursor; :w authors it
# into the `docs` layer, keyed by the entity's durable id.
Type ":TyO3Doc" Enter
Sleep 1500ms
Type "ggcc# Checkout — the load-bearing path"
Escape
Type "Go"
Type "Converts a Book price to USD. This doc is glued to the function's"
Enter
Type "durable identity — it survives edits and moves, not line numbers."
Escape
Sleep 600ms
Type ":w" Enter
Sleep 1500ms
Type ":q" Enter
Sleep 2500ms

# ── Scene 4: identity survives an edit — note + doc stay attached ──
Type "o"
Sleep 200ms
Type "# audited"
Escape
Type ":w" Enter
Sleep 2500ms
Type "/def checkout" Enter
Sleep 3s

# ── Scene 5: the headline — atomic MOVE carries id + note + doc to a new file ──
Type ":TyO3Move checkout checkout.py" Enter
Sleep 3s
Type ":e checkout.py" Enter
Sleep 2s
Type "/def checkout" Enter
Sleep 3500ms

# ── Scene 6: panes are collapsible — separation you control ──
Ctrl+W
Type "l"
Sleep 800ms
Type "/SUMMARY" Enter
Sleep 600ms
Tab
Sleep 1500ms
Type "/ACTIONS" Enter
Sleep 400ms
Tab
Sleep 1500ms
Tab
Sleep 800ms
Ctrl+W
Type "h"
Sleep 800ms

# ── Scene 7: the docs link to BOTH user and developer guides ──
Type ":TyO3Docs" Enter
Sleep 1200ms
Type "1" Enter
Sleep 3500ms
Type "q"
Sleep 800ms
Type ":TyO3Docs" Enter
Sleep 1200ms
Type "3" Enter
Sleep 3500ms
Type "q"
Sleep 800ms

# ── Scene 8: live blast radius + async precision refinement (AFFECTED pane) ──
Type ":e money.py" Enter
Sleep 1500ms
Type "/100" Enter
Sleep 600ms
Type "ciw1000"
Escape
Type ":w" Enter
Sleep 5s

Type ":qa!" Enter
Sleep 1s
