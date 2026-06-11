# tyo3.nvim — HERO demo. The short (~35s), looping advertisement. Render with:
#   devenv shell -- demo-record-hero
#   (or: vhs editors/tyo3.nvim/demo/hero/hero.tape)
#
# Three beats, sidebar on throughout, tight pacing:
#   1. the money shot — note an entity, atomically move it, the note rides along;
#   2. durable review + a type error in ONE `]d` stream (the novel part);
#   3. acknowledge the review straight from the code-action menu (Phase 2).
# The cold pre-warm (build + open + reindex) is kept OFF camera with Hide/Show so
# the first on-camera sync is hot. Sleeps wait past every async beat.

Output editors/tyo3.nvim/demo/hero/hero.gif
Output editors/tyo3.nvim/demo/hero/hero.txt

Require nvim
Require python

Set Shell "bash"
Set FontSize 16
Set Width 1360
Set Height 840
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms

# ── Pre-warm OFF camera: build the project, open + reindex so the first ──
# ── on-camera sync is hot (no cold-start dead air on stage). ──
Type "source editors/tyo3.nvim/demo/setup.sh && cd $TYO3_DEMO_DIR" Enter
Sleep 2s
Hide
Type "$TYO3_NVIM --clean -u $TYO3_PLUGIN_DIR/demo/hero/init.lua store.py" Enter
Sleep 6s
Type ":TyO3Reindex" Enter
Sleep 3s
Show

# ── Beat 1: the money shot — note an entity, move it, the note rides the move ──
Type "/def checkout" Enter
Sleep 1500ms
Type ":TyO3Note load-bearing checkout path" Enter
Sleep 1800ms
Type ":TyO3Move checkout checkout.py" Enter
Sleep 1800ms
Type ":e checkout.py" Enter
Sleep 2500ms

# ── Beat 2: durable review + a type error surface in ONE `]d` stream ──
Type ":e store.py" Enter
Sleep 1200ms
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
Type "gg"
Sleep 300ms
Type "]d"
Sleep 1800ms
Type ":lua vim.diagnostic.open_float()" Enter
Sleep 2200ms
Escape

# ── Beat 3: acknowledge the review from the code-action menu (Phase 2) ──
Type "]d"
Sleep 800ms
Type ":lua vim.lsp.buf.code_action()" Enter
Sleep 2200ms
Type "1" Enter
Sleep 2200ms

Type ":qa!" Enter
Sleep 1s
