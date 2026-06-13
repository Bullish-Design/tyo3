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
#      stream, then `<leader>c` opens the act menu and Acknowledge clears the ⚠.
# Every TyO3 interaction rides two keys: `<leader>c` (act on this entity) and
# `<leader>t` (the hub — views + ops). No `:TyO3*` command is typed on camera.
# The cold pre-warm (build + open + reindex + sidebar) is kept OFF camera with
# Hide/Show so the first on-camera beat is hot. Sleeps wait past every async beat.

Output editors/tyo3.nvim/demo/hero/hero.gif
Output editors/tyo3.nvim/demo/hero/hero.webm
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
# Author via <leader>c → Author (jjjj) → snacks prompt.
Type "/def checkout" Enter
Sleep 1200ms
Space
Type "c"
Sleep 1500ms
Type "jjjj"
Sleep 800ms
Enter
Sleep 1500ms
Type "load-bearing checkout path"
Sleep 1s
Enter
Sleep 2500ms
# Move via <leader>c → Move (j, offset 1) → dest prompt → checkout.py. The note
# rides the atomic move (the whole pitch).
Type "/def checkout" Enter
Sleep 800ms
Space
Type "c"
Sleep 1300ms
Type "j"
Sleep 800ms
Enter
Sleep 1500ms
Type "checkout.py"
Sleep 1s
Enter
Sleep 2000ms
Type ":e checkout.py" Enter
Sleep 2500ms

# ── Beat 3: ACT — drift a noted entity, walk needs_review, acknowledge via gra ──
Type ":e store.py" Enter
Sleep 1000ms
Type "/def show_label" Enter
Sleep 600ms
# Note show_label via <leader>c → Author (jjjj) → prompt (before any drift, so
# the menu has no Quick-fix group yet; Author is at offset 4).
Space
Type "c"
Sleep 1300ms
Type "jjjj"
Sleep 800ms
Enter
Sleep 1500ms
Type "keep this label stable"
Sleep 1s
Enter
Sleep 1800ms
Type "/\.label" Enter
Type "lciwprice"
Escape
Sleep 600ms
Type ":w" Enter
Sleep 4s
# The durable needs_review WARN + the type error both surface in the snacks
# diagnostics picker — opened from the hub (<leader>t → "Diagnostics"). One
# cohesive, bordered list across namespaces (the picker rhymes with the act menu
# below); no `:lua` float, no `:TyO3*` typed.
Space
Type "t"
Sleep 1300ms
Type "Diagnostics"
Sleep 1200ms
Enter
Sleep 3000ms
# TWO escapes to close the snacks picker: the first only exits the prompt's
# insert mode (→ normal), the second closes it. One Esc leaves it open and the
# acknowledge keys leak into the picker's scratch buffer (E21).
Escape
Sleep 500ms
Escape
Sleep 400ms
# `<leader>c` opens the act menu on show_label; Acknowledge re-authors the note
# (the engine's acknowledge) so review_state goes clean and the statusline WARN
# clears (E:1 W:1 → E:1). With needs_review live the menu prepends a Quick-fix
# group and Acknowledge is the preferred action at offset 0 — <CR> applies it.
Type "/def show_label" Enter
Sleep 800ms
Space
Type "c"
Sleep 2800ms
Enter
Sleep 3000ms

# Hide the quit so the loop doesn't flash the bare shell between replays.
Hide
Type ":qa!" Enter
Sleep 1s
