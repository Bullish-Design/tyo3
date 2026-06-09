# tyo3.nvim — scripted demo. Render from the repo root with:
#   devenv shell -- demo-record         (or: vhs editors/tyo3.nvim/demo/tour.tape)
#
# Drives a real terminal Neovim through the plugin's verbs against the synthetic
# "shop" project. Sleeps are generous and deliberate — we wait past every async
# beat (daemon open, debounced commit, precision refinement) so the recording is
# deterministic. Determinism over speed.

Output editors/tyo3.nvim/demo/tour.gif
Output editors/tyo3.nvim/demo/tour.cast

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
Type "nvim -u $TYO3_PLUGIN_DIR/demo/init.lua store.py" Enter
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
Type "/return 100" Enter
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

Type ":qa!" Enter
Sleep 1s
