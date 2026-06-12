# tyo3.nvim — HERO demo. The short (~38s), looping advertisement, in the
# single-path snacks/sidebar idiom. Render with:
#   devenv shell -- demo-record-hero
#   (or: vhs editors/tyo3.nvim/demo/hero/hero.tape)
#
# The navigate → observe → act loop, sidebar on throughout:
#   1. NAVIGATE by structure — treewalker motion between entities; the edgy
#      accordion sidebar tracks the entity under the cursor (IDENTITY expands).
#   2. OBSERVE durable identity — author a note, atomically move the entity to
#      another file; the note rides the move (the whole pitch). NOTES shows it.
#   3. ACT — drift a noted entity so needs_review fires, walk it in the `]d`
#      stream, then `gra` opens the action picker and Acknowledge clears the ⚠.
# The cold pre-warm (build + open + reindex + sidebar) is kept OFF camera with
# Hide/Show so the first on-camera beat is hot. Sleeps wait past every async beat.

Output editors/tyo3.nvim/demo/hero/hero.gif
Output editors/tyo3.nvim/demo/hero/hero.txt

Require nvim
Require python

Set Shell "bash"
Set FontSize 16
Set Width 1360
Set Height 840
Set Framerate 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms

# ── Pre-warm OFF camera: hide the WHOLE cold start (shell + build + launch + ──
# ── reindex) and land on `checkout` so cursor-context auto-opens the sidebar ──
# ── with IDENTITY tracking it — the first on-camera frame is a hot, ──
# ── sidebar-open nvim. (No :TyO3Sidebar toggle: the sidebar auto-opens on ──
# ── context, and a toggle here would CLOSE the reindex-opened bar.) ──
Hide
Type "source editors/tyo3.nvim/demo/setup.sh && cd $TYO3_DEMO_DIR" Enter
Sleep 2s
Type "$TYO3_NVIM --clean -u $TYO3_PLUGIN_DIR/demo/hero/init.lua store.py" Enter
Sleep 7s
Type ":TyO3Reindex" Enter
Sleep 3s
Type "/def checkout" Enter
Sleep 2500ms
Show

# ── Beat 1: NAVIGATE by structure — treewalker between the two functions; the ──
# edgy sidebar IDENTITY tracks the entity under the cursor. Cursor starts on
# `checkout` (sidebar showing it); <C-j> walks to the next sibling def
# (show_label) and <C-k> back — IDENTITY re-resolves on each hop.
Ctrl+j
Sleep 2200ms
Ctrl+k
Sleep 2200ms

# ── Beat 2: OBSERVE durable identity — note an entity, move it, the note rides ──
Type "/def checkout" Enter
Sleep 1200ms
Type ":TyO3Note load-bearing checkout path" Enter
Sleep 2000ms
Type ":TyO3Move checkout checkout.py" Enter
Sleep 1800ms
Type ":e checkout.py" Enter
Sleep 2500ms

# ── Beat 3: ACT — drift a noted entity, walk needs_review, acknowledge via gra ──
Type ":e store.py" Enter
Sleep 1000ms
Type "/def show_label" Enter
Sleep 600ms
Type ":TyO3Note keep this label stable" Enter
Sleep 1500ms
Type "/\.label" Enter
Type "lciwprice"
Escape
Sleep 600ms
Type ":w" Enter
Sleep 4s
# The durable needs_review WARN + the type error both surface in the snacks
# diagnostics picker — one cohesive, bordered list across namespaces (no raw
# `:lua` float; the picker rhymes with the `gra` action picker below).
Type ":TyO3Diagnostics" Enter
Sleep 3400ms
# TWO escapes to close the snacks picker: the first only exits the prompt's
# insert mode (→ normal), the second closes it. One Esc leaves it open and the
# acknowledge keys leak into the picker's scratch buffer (E21).
Escape
Sleep 500ms
Escape
Sleep 400ms
# `gra` opens the action picker on show_label; Acknowledge re-authors the note
# (the engine's acknowledge) so review_state goes clean and the statusline WARN
# clears (E:1 W:1 → E:1). The acknowledge quickfix is the preferred action, so it
# sorts to the top of the menu — <CR> applies it. Pause on the cleared state.
Type "/def show_label" Enter
Sleep 800ms
Type "gra"
Sleep 2800ms
Enter
Sleep 3000ms

# Hide the quit so the loop doesn't flash the bare shell between replays.
Hide
Type ":qa!" Enter
Sleep 1s
