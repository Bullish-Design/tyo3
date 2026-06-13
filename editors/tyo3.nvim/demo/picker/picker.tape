# tyo3.nvim — HUB demo (proj 28). One keyboard surface — the hub (`<leader>t`) —
# reaches every TyO3 view; notes are authored from the act surface (`<leader>c`).
# No `:TyO3*` command is ever typed. Render with:
#   devenv shell -- demo-record-picker
#   (or: vhs editors/tyo3.nvim/demo/picker/picker.tape)
#
# Four beats:
#   1. <leader>t → "Entities" → pick a decorated entity → jump by identity;
#   2. <leader>c → Author → snacks prompt → author a note on the entity;
#   3. <leader>t → "Authored" → pick the note back → jump;
#   4. edit Item.price, then <leader>t → "Affected" → the last edit's closure.
# The cold open + first index is kept OFF camera with Hide/Show. Sleeps wait past
# every async daemon round-trip.

Output editors/tyo3.nvim/demo/picker/picker.gif
Output editors/tyo3.nvim/demo/picker/picker.webm
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

# ── Beat 1: hub → "Entities" → pick a decorated entity, jump by identity ──
# One key opens the hub; "Entities" filters the hub to that view and <CR> opens
# it. The entities picker lists every entity seen so far; filtering to
# "show_label" and confirming jumps to its *current* range (decorate-fresh).
Space
Type "t"
Sleep 1500ms
Type "Entities"
Sleep 1200ms
Enter
Sleep 1800ms
Type "show_label"
Sleep 1800ms
Enter
Sleep 2500ms

# ── Beat 2: <leader>c → Author → author a note through the snacks prompt ──
# The cursor landed on show_label from the jump; the act menu opens on it. Step
# to Author (jjjj), confirm, and the snacks prompt takes the note.
Space
Type "c"
Sleep 1500ms
Type "jjjj"
Sleep 800ms
Enter
Sleep 1500ms
Type "keep this label stable"
Sleep 1s
Enter
Sleep 2500ms

# ── Beat 3: hub → "Authored" → pick the note back, jump ──
Space
Type "t"
Sleep 1500ms
Type "Authored"
Sleep 1200ms
Enter
Sleep 2000ms
Enter
Sleep 2000ms

# ── Beat 4: edit Item.price, then hub → "Affected" → the last edit's closure ──
# Open catalog.py and change the price literal (100 → 250) — a real structural
# edit whose closure reaches checkout. `/100` lands on the literal so `ciw` swaps
# just the value (catalog.py has no other 100). `:e`/`:w` are plain editor
# mechanics; the TyO3 view is reached from the hub.
Type ":e catalog.py" Enter
Sleep 1500ms
Type "/100" Enter
Sleep 800ms
Type "ciw250"
Escape
Sleep 500ms
Type ":w" Enter
Sleep 4s
Space
Type "t"
Sleep 1500ms
Type "Affected"
Sleep 1200ms
Enter
Sleep 2000ms
Enter
Sleep 2000ms

Type ":qa!" Enter
Sleep 1s
