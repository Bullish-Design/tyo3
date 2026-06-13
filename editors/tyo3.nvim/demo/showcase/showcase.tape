# tyo3.nvim — SHOWCASE demo. The long, deliberately-paced, wow-factor tour.
# Render: devenv shell -- demo-record-showcase
#
# RECORDED FAST, PLAYED SLOW: on-camera sleeps are halved and TypingSpeed is fast,
# then `Set PlaybackSpeed 0.5` stretches the final render 2x — so it RECORDS in
# ~half the wall-time and the gif has ~half the frames (smaller), while playing
# back at the same registrable, slow speed. (Pre-warm async waits — nvim load /
# reindex — stay at real time; they're hidden and PlaybackSpeed doesn't touch them.)
#
#   ACT 1  navigate by structure — treewalker hops, the sidebar morphs to track.
#   ACT 2  one surface to act — `<leader>c`: Author / Write doc / Explain; bloom.
#   ACT 3  WOW #1 — move the function across files; the note AND doc ride it.
#   ACT 4  WOW #2 — change a shared method; the affected picker (from the hub)
#          shows the blast radius, jump to one BY IDENTITY (into the moved file).
#   ACT 5  WOW #3 — it remembered: a drift flags needs_review; the diagnostics
#          picker (from the hub) shows it next to a type error; one keystroke acks.
#
# Two keys carry every TyO3 interaction: `<leader>c` (act on this entity) and
# `<leader>t` (the hub — views + ops). No `:TyO3*` command is typed on camera.
# Act-menu j-counts (display lines incl. headers/blanks from Simplify):
#   Simplify=0  Move=1  [blank]=2  ##Source=3  Author=4  Explain=5  Write doc=6.
# Snacks pickers need TWO escapes to close (1st exits insert, 2nd closes).

Output editors/tyo3.nvim/demo/showcase/showcase.gif
Output editors/tyo3.nvim/demo/showcase/showcase.webm
Output editors/tyo3.nvim/demo/showcase/showcase.txt
Output editors/tyo3.nvim/demo/showcase/frames/ # a directory of frames as a PNG sequence

Require nvim
Require python

Set Shell "bash"
Set FontSize 16
Set Width 1360
Set Height 840
Set Framerate 24
Set PlaybackSpeed 0.5
Set Theme "Catppuccin Mocha"
Set TypingSpeed 28ms

# ── Setup OFF camera, then reveal a clean prompt ─────────────────────────────
Hide
Type "source editors/tyo3.nvim/demo/setup.sh && cd $TYO3_DEMO_DIR" Enter
Sleep 2s
Type "clear" Enter
Sleep 500ms
Show

# ── LOOP MARKER: launch command typed on a clean prompt (on camera) ───────────
Sleep 700ms
Type "$TYO3_NVIM --clean -u $TYO3_PLUGIN_DIR/demo/showcase/init.lua store.py" Enter
Sleep 1300ms
# Pre-warm async waits stay at REAL time (nvim load + reindex + land), hidden.
Hide
Sleep 7s
Type ":TyO3Reindex" Enter
Sleep 3s
Type "/def checkout" Enter
Sleep 2500ms
Show

# ══ ACT 1 — Navigate by structure (the sidebar morphs to track each entity) ═══
Sleep 1500ms
Ctrl+j
Sleep 1800ms
Ctrl+j
Sleep 1800ms
Ctrl+k
Sleep 1800ms
# AST-precise selection: a whole function body as a textobject.
Type "vif"
Sleep 1500ms
Escape
Sleep 800ms
# Cross-file jump by the durable code graph, over stock goto-definition.
Type "/Book" Enter
Sleep 700ms
Type "gd"
Sleep 2000ms
Ctrl+h
Sleep 1800ms
Type ":e store.py" Enter
Sleep 700ms
Type "/def checkout" Enter
Sleep 1300ms

# ══ ACT 2 — One surface to act; the sidebar blooms ═══════════════════════════
Space
Type "c"
Sleep 2300ms
# Author (= 4).
Type "jjjj"
Sleep 900ms
Enter
Sleep 1000ms
Type "load-bearing checkout path"
Sleep 900ms
Enter
Sleep 2000ms
# Write doc (= 6): markdown editor (bottom split); :w authors, :q returns.
Space
Type "c"
Sleep 1300ms
Type "jjjjjj"
Sleep 900ms
Enter
Sleep 1000ms
Type "GoSettles the cart and charges the customer."
Sleep 500ms
Escape
Sleep 700ms
Type ":w" Enter
Sleep 900ms
Type ":q" Enter
Sleep 700ms
Type "/def checkout" Enter
Sleep 1300ms
# Explain (= 5): the offline-LLM 🤖 prose.
Space
Type "c"
Sleep 1300ms
Type "jjjjj"
Sleep 900ms
Enter
Sleep 2300ms
Escape
Sleep 800ms
# Simplify (index 0): the resolvable refactor. The menu is title-only (the
# rewrite materializes on apply, not preview); here we surface it and close.
Space
Type "c"
Sleep 2000ms
Escape
Sleep 800ms
# LAND IT: the fully-bloomed sidebar — IDENTITY + NOTES (note + 🤖) + DOCS + SUMMARY.
Sleep 2600ms

# ══ ACT 3 — WOW #1: durable identity (the note + doc ride a move) ════════════
# <leader>c → Move (j, offset 1) → dest prompt.
Space
Type "c"
Sleep 1300ms
Type "j"
Sleep 900ms
Enter
Sleep 1000ms
Type "checkout.py"
Sleep 700ms
Enter
Sleep 1300ms
Type ":e checkout.py" Enter
Sleep 800ms
Type "/def checkout" Enter
# LAND IT (the headline): NOTES + DOCS present on checkout in the NEW file.
Sleep 3300ms

# ══ ACT 4 — WOW #2: live blast radius (across files, by identity) ════════════
Type ":e catalog.py" Enter
Sleep 900ms
Type "/return 100" Enter
Sleep 800ms
Type ":s/100/250/" Enter
Sleep 800ms
Type ":w" Enter
# LAND IT: the affected closure lights up + the refinement narrows it.
Sleep 2600ms
# Jump to an affected entity BY IDENTITY via the hub → "Affected" (read the list).
Space
Type "t"
Sleep 1300ms
Type "Affected"
Sleep 1100ms
Enter
Sleep 1800ms
Type "checkout"
Sleep 1000ms
Enter
# LAND IT: checkout.py — we navigated the blast radius by identity.
Sleep 2600ms

# ══ ACT 5 — WOW #3: it remembered (durable review + the diagnostics picker) ══
Type ":e store.py" Enter
Sleep 800ms
Type "/def show_label" Enter
Sleep 800ms
# Note show_label via <leader>c → Author (jjjj) — before any drift, so the menu
# has no Quick-fix group yet; Author is at offset 4.
Space
Type "c"
Sleep 1300ms
Type "jjjj"
Sleep 900ms
Enter
Sleep 1000ms
Type "keep this label stable"
Sleep 900ms
Enter
Sleep 1500ms
Type "/\.label" Enter
Sleep 500ms
Type "lciwprice"
Escape
Sleep 600ms
Type ":w" Enter
# LAND IT: the needs_review ⚠ appears in the sidebar NOTES.
Sleep 2600ms
# The snacks diagnostics picker (from the hub → "Diagnostics"): needs_review next
# to a real type error. TWO escapes to close (1st exits insert → normal, 2nd
# closes — else keys leak in).
Space
Type "t"
Sleep 1300ms
Type "Diagnostics"
Sleep 1100ms
Enter
Sleep 2600ms
Escape
Sleep 400ms
Escape
Sleep 800ms
# `<leader>c` → Acknowledge: with needs_review live the menu prepends a Quick-fix
# group and Acknowledge is the preferred action at offset 0 — <CR> applies it.
Type "/def show_label" Enter
Sleep 800ms
Space
Type "c"
Sleep 1500ms
Enter
# LAND IT: the cleared state — no ⚠ in NOTES, E:1 in the statusline.
Sleep 2800ms

# ── Return to the shell (on camera) so the loop boundary is obvious ──
Type ":qa!" Enter
Sleep 1300ms
