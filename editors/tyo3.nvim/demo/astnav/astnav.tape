# tyo3.nvim — AST NAV demo (proj 28, Phase D). AST motion is the default feel:
# treewalker moves through the syntax tree and the dock resolves the *enclosing
# durable entity* as you go; textobjects select a node that feeds the act path.
# Render with:
#   devenv shell -- demo-record-astnav
#   (or: vhs editors/tyo3.nvim/demo/astnav/astnav.tape)
#
# Beats:
#   1. land on `checkout` → the dock opens on it (IDENTITY = checkout).
#   2. treewalker <C-j>/<C-k> between sibling functions → the dock TRACKS the tree
#      (checkout ↔ show_label) — "syntax under the cursor, identity in the dock".
#   3. <C-l> descends into the function body (dock still names the enclosing entity).
#   4. `vaf` selects the whole function (textobject), then `gra` acts on exactly
#      that node via the code-action hub. Selection → entity, for free.
# The cold open + first index is kept OFF camera with Hide/Show.

Output editors/tyo3.nvim/demo/astnav/astnav.gif
Output editors/tyo3.nvim/demo/astnav/astnav.txt

Require nvim
Require python

Set Shell "bash"
Set FontSize 16
Set Width 1360
Set Height 840
Set Framerate 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms

# ── Pre-warm OFF camera: open store.py so the daemon opens + indexes the ──
# ── project, the parser loads, and store.py's entities land in decorate. ──
Type "source editors/tyo3.nvim/demo/setup.sh && cd $TYO3_DEMO_DIR" Enter
Sleep 2s
Hide
Type "$TYO3_NVIM --clean -u $TYO3_PLUGIN_DIR/demo/astnav/init.lua store.py" Enter
Sleep 8s
Show

# ── Beat 1: land on `checkout` → the dock opens on it ──
Type "/def checkout" Enter
Sleep 2500ms

# ── Beat 2: treewalker between sibling functions → the dock tracks the tree ──
# <C-j> = next neighbour (→ show_label), <C-k> = previous (→ checkout). The brief
# post-jump highlight shows the node; the dock IDENTITY follows.
Ctrl+J
Sleep 2500ms
Ctrl+K
Sleep 2500ms

# ── Beat 3: <C-l> descends into the function body (dock still names checkout) ──
Ctrl+L
Sleep 2500ms

# ── Beat 4: select the whole function (textobject), then act on it via `gra` ──
Type "/def checkout" Enter
Sleep 800ms
Type "vaf"
Sleep 2500ms
Escape
Sleep 500ms
Type "gra"
Sleep 3s
Type "q"
Sleep 800ms

Type ":qa!" Enter
Sleep 1s
