# tyo3.nvim — CODE ACTION demo (proj 28, Phase C). The single "act on the entity"
# surface: one keystroke (`gra`) opens the tiny-code-action buffer picker with
# every action glued to the durable entity under the cursor. Render with:
#   devenv shell -- demo-record-codeaction
#   (or: vhs editors/tyo3.nvim/demo/codeaction/codeaction.tape)
#
# Three beats:
#   1. `gra` on `checkout` → the hub menu (Author / Write doc / Move / Explain /
#      Simplify) — every provider in the registry, surfaced at once.
#   2. pick Author `intent` → snacks vim.ui.input → author a durable note (its
#      virtual text lands glued to the entity).
#   3. `gra` again → pick Explain → the offline-LLM prose float.
# The cold open + first index is kept OFF camera with Hide/Show. Sleeps wait past
# every async daemon round-trip.

Output editors/tyo3.nvim/demo/codeaction/codeaction.gif
Output editors/tyo3.nvim/demo/codeaction/codeaction.txt

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
Type "$TYO3_NVIM --clean -u $TYO3_PLUGIN_DIR/demo/codeaction/init.lua store.py" Enter
Sleep 8s
Show

# ── Beat 1: land on `checkout`, open the action hub with `gra` ──
# `checkout` is the demo's load-bearing entrypoint; `gra` is the buffer-local
# default the bridge binds on attach. The menu lists every registry provider,
# grouped (Refactor: Simplify/Move · Source: Author/Explain/Write doc).
Type "/def checkout" Enter
Sleep 1s
Type "gra"
Sleep 3s

# ── Beat 2: pick Author `intent`, then author a note via the snacks prompt ──
# The cursor opens on the first action (Simplify); step down to Author (`j`×4),
# <CR> applies it, then snacks' vim.ui.input takes the note and the daemon
# authors it on the entity — its virtual-text note lands glued to checkout.
Type "jjjj"
Sleep 800ms
Enter
Sleep 1500ms
Type "load-bearing checkout path"
Sleep 1s
Enter
Sleep 3s

# ── Beat 3: open the hub again, step to Explain → the offline-LLM prose float ──
Type "gra"
Sleep 2s
Type "jjjjj"
Sleep 800ms
Enter
Sleep 4s
Escape
Sleep 1s

Type ":qa!" Enter
Sleep 1s
