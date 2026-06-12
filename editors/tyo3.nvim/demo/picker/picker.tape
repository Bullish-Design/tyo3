# tyo3.nvim — PICKER demo (proj 28, Phase B). The single-path, snacks-backed
# pickers + prompt, navigating by durable identity. Render with:
#   devenv shell -- demo-record-picker
#   (or: vhs editors/tyo3.nvim/demo/picker/picker.tape)
#
# Four beats, each one TyO3 surface that now rides snacks (no telescope /
# vim.ui.select fallback):
#   1. :TyO3Entities  — pick any decorated entity → jump by identity;
#   2. :TyO3Note      — author a note via the snacks `vim.ui.input` prompt;
#   3. :TyO3Authored  — pick the note back from the snacks picker → jump;
#   4. :TyO3Affected  — edit, then pick the last edit's affected set.
# The cold open + first index is kept OFF camera with Hide/Show. Sleeps wait past
# every async daemon round-trip.

Output editors/tyo3.nvim/demo/picker/picker.gif
Output editors/tyo3.nvim/demo/picker/picker.txt

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
# ── project and store.py's entities land in the decorate cache. ──
Type "source editors/tyo3.nvim/demo/setup.sh && cd $TYO3_DEMO_DIR" Enter
Sleep 2s
Hide
Type "$TYO3_NVIM --clean -u $TYO3_PLUGIN_DIR/demo/picker/init.lua store.py" Enter
Sleep 8s
Show

# ── Beat 1: :TyO3Entities — pick a decorated entity, jump by identity ──
# The snacks picker lists every entity seen so far; filtering to "show_label"
# and confirming jumps to its *current* range (decorate-fresh, proj 27).
Type ":TyO3Entities" Enter
Sleep 2s
Type "show_label"
Sleep 2s
Enter
Sleep 2500ms

# ── Beat 2: :TyO3Note — author a note through the snacks vim.ui.input prompt ──
# No argument ⇒ TyO3 prompts; the prompt is snacks' input float now.
Type ":TyO3Note" Enter
Sleep 1500ms
Type "keep this label stable"
Sleep 1s
Enter
Sleep 2500ms

# ── Beat 3: :TyO3Authored — pick the note back from the snacks picker ──
Type ":TyO3Authored" Enter
Sleep 2500ms
Enter
Sleep 2000ms

# ── Beat 4: edit Item.price, then :TyO3Affected — pick the last edit's closure ──
# Open catalog.py and change the price literal (100 → 250) — a real structural
# edit whose closure reaches checkout. `/100` lands on the literal so `ciw` swaps
# just the value (catalog.py has no other 100).
Type ":e catalog.py" Enter
Sleep 1500ms
Type "/100" Enter
Sleep 800ms
Type "ciw250"
Escape
Sleep 500ms
Type ":w" Enter
Sleep 4s
Type ":TyO3Affected" Enter
Sleep 2500ms
Enter
Sleep 2000ms

Type ":qa!" Enter
Sleep 1s
