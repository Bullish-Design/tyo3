# tyo3.nvim — comprehensive CONTEXT demo. Render from the repo root with:
#   devenv shell -- demo-record-context
#   (or: vhs editors/tyo3.nvim/demo/context/context.tape)
#
# A guided tour of TyO3's real, user-facing capabilities with the cursor-driven
# CONTEXT panel on throughout as the narrative spine. The right dock shows two
# live sections: CONTEXT (the durable entity under the cursor — its id, location,
# notes, and last-affecting revision) and AFFECTED (the id-level blast radius of
# each edit, then its async refinement). Sleeps wait past every async beat
# (daemon open, debounced commit, precision refinement) so the recording is
# deterministic.

Output editors/tyo3.nvim/demo/context/context.gif
Output editors/tyo3.nvim/demo/context/context.txt

Require nvim
Require python

Set Shell "bash"
Set FontSize 16
Set Width 1360
Set Height 840
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms

# Build the synthetic shop project and resolve a pristine nvim ($TYO3_NVIM).
Type "source editors/tyo3.nvim/demo/setup.sh && cd $TYO3_DEMO_DIR" Enter
Sleep 2s

# Open store.py with the CONTEXT feature on, panel docked on the right.
Type "$TYO3_NVIM --clean -u $TYO3_PLUGIN_DIR/demo/context/init.lua store.py" Enter
Sleep 6s

# ── Scene 1: ambient identity — CONTEXT tracks the entity under the cursor ──
# Land in `checkout`: CONTEXT shows  name · kind / durable id / location.
Type "/def checkout" Enter
Sleep 3s
# Move to a different definition → CONTEXT switches (fires only on node change).
Type "/def show_label" Enter
Sleep 3s
# Back to checkout.
Type "/def checkout" Enter
Sleep 2500ms

# ── Scene 2: the full cross-layer card (on-demand inspector) ──
Type ":TyO3Inspect" Enter
Sleep 3500ms
Escape
Sleep 600ms

# ── Scene 3: author durable intent (note glued to the entity, not the line) ──
Type ":TyO3Note load-bearing checkout path" Enter
Sleep 2500ms
# Nudge off and back so CONTEXT re-resolves and shows the 🏷 note in the card.
Type "/def show_label" Enter
Sleep 1200ms
Type "/def checkout" Enter
Sleep 3s

# ── Scene 4: identity survives ordinary editing (same id, note stays) ──
# Add a line inside checkout's body, then commit.
Type "o"
Sleep 300ms
Type "# audited: see note above"
Escape
Sleep 400ms
Type ":w" Enter
Sleep 3s
# Re-resolve: same durable id, the 🏷 note is still attached; AFFECTED logs the commit.
Type "/def checkout" Enter
Sleep 3s

# ── Scene 5: the headline — atomic MOVE; the id and the note ride along ──
Type ":TyO3Move checkout checkout.py" Enter
Sleep 3s
Type ":e checkout.py" Enter
Sleep 2500ms
# In a *different file* now: CONTEXT shows the SAME durable id, a new location
# (checkout.py::checkout), and the 🏷 note still glued. Identity, made visible.
Type "/def checkout" Enter
Sleep 3500ms

# ── Scene 6: live blast radius + async precision refinement ──
# Edit the top-level usd() formatter — everything that calls it is affected.
Type ":e money.py" Enter
Sleep 1500ms
Type "/100" Enter
Sleep 600ms
Type "ciw1000"
Escape
Sleep 500ms
Type ":w" Enter
# AFFECTED lights up with the id-level closure (usd + its callers, e.g. checkout),
# then the async precision refinement narrows it — both visible in the dock.
Sleep 5s
# Land back on usd → its CONTEXT `affected@ rev N` advanced to this very edit.
Type "/def usd" Enter
Sleep 3500ms

# ── Scene 7: entity-level diff across the session (structural, not textual) ──
# rev 1 is the freshly-indexed baseline; head reflects every edit — so the diff
# names `moved` (checkout → checkout.py) and `changed` (price) by identity.
Type ":TyO3Diff 1" Enter
Sleep 4s
Type "q"
Sleep 800ms

Type ":qa!" Enter
Sleep 1s
