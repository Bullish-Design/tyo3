# Neovim plugin — scripted, recorded demo

> A deliverable of the plugin build (see `KICKOFF.md`). Goal: a **real**,
> **reproducible**, **scripted** Neovim session that shows the plugin working —
> driven by scripted keystrokes, recorded to a shareable artifact, regenerable in
> CI. Not a hand-waved GIF; a checked-in script anyone can re-run.

---

## 1. The "graphical nvim via asciinema" question — resolved

There's a real tension to name up front:

- **asciinema records a *terminal*.** It captures the **terminal (TUI) Neovim**
  UI — which *is* the full, real, visual Neovim interface (statusline, splits,
  virtual text, floats, colours). It does **not** capture a GUI build (Neovide /
  nvim-qt); those are pixel windows, outside a terminal.
- So "graphical Neovim recorded via asciinema" = **terminal Neovim, recorded as a
  real visual session.** That is the right interpretation and the reproducible
  one. Virtual-text notes, the floating inspector, and the affected panel all
  render perfectly in the TUI — nothing about the demo needs a GUI build.

**If a true GUI (Neovide) recording is ever wanted** it's a different pipeline:
screen-record (wf-recorder/OBS) a Neovide window driven by `xdotool`/`ydotool`
keystrokes. Heavier, non-deterministic, not CI-friendly — treat as a one-off
marketing capture, **not** the canonical demo. The canonical demo is TUI + vhs.

---

## 2. Tooling: vhs (primary), asciinema cast as a secondary output

Use **[vhs](https://github.com/charmbracelet/vhs)** (charmbracelet) — it is
purpose-built for exactly this: a `.tape` DSL of scripted keystrokes
(`Type`, `Enter`, `Sleep`, `Set`, `Hide/Show`) that runs a real terminal program
headlessly (ttyd + ffmpeg) and renders to **GIF / MP4 / WebM**, and can also emit
an **asciinema `.cast`**. It is deterministic (explicit `Sleep`s, fixed window
size/theme), single-binary, and runs in CI with no display.

- **Add to devenv** (`devenv.nix` packages): `pkgs.vhs`. (`nvim` and `tmux` are
  already on PATH in this environment; `asciinema`/`vhs` are not yet.)
- Outputs: a committed **`tour.gif`** (for the README) and **`tour.cast`**
  (asciinema, for asciinema.org embedding). Regenerate both from one `.tape`.
- Why not raw asciinema + tmux `send-keys`: it works (record an asciinema session
  while `tmux send-keys` drives nvim in the pane), but it's more moving parts and
  less deterministic than vhs. Keep it as the fallback if vhs can't be packaged.

---

## 3. What the demo shows (scene → keystrokes)

Reuse the **synthetic "shop" project** from `src/tyo3/demo/tour.py` (same files)
so the demo is deterministic and matches the CLI tour. The plugin's daemon points
at a copy of it. Scenes, in order:

1. **Open & ambient intelligence** — open `store.py`; the affected panel /
   statusline shows the session is live; entities are decorated.
2. **Inspect** — cursor on `checkout`, `:TyO3Inspect` → floating window with the
   DurableId, kind, derived summary.
3. **Author a note** — `:TyO3Note load-bearing checkout path` → virtual-text note
   appears above `checkout`.
4. **The money shot — the note survives a move.** Cut `checkout` from `store.py`,
   paste it into a new `checkout.py` (or `:TyO3Move`-style edit). The virtual-text
   note **rides along to the new file** — durable identity, visibly.
5. **Affected set** — edit `Item.price` in `catalog.py`; the affected panel lights
   up with `{Item, Book, checkout, …}`; with `precision=method` the refinement
   narrows it a beat later. Show the panel updating live.
6. **Navigate** — Telescope `affected` picker → jump to an affected entity.

The single frame that sells it is **Scene 4**: a human-readable note glued to a
function as it moves between files.

---

## 4. Sample `.tape` (starting point — lives at `editors/tyo3.nvim/demo/tour.tape`)

```tape
# TyO3 Neovim plugin — scripted demo. Render: `vhs editors/tyo3.nvim/demo/tour.tape`
Output editors/tyo3.nvim/demo/tour.gif
Output editors/tyo3.nvim/demo/tour.cast      # asciinema cast

Require nvim
Set Shell "bash"
Set FontSize 18
Set Width 1280
Set Height 800
Set Theme "Catppuccin Mocha"
Set TypingSpeed 60ms

# The demo project + a config that points nvim at the plugin are prepared by
# editors/tyo3.nvim/demo/setup.sh (builds the synthetic shop project in a tmpdir,
# writes a minimal init.lua that loads tyo3.nvim and starts the daemon).
Type "source editors/tyo3.nvim/demo/setup.sh && cd $TYO3_DEMO_DIR" Enter
Sleep 1s

Type "nvim store.py" Enter
Sleep 3s                                   # daemon: open + initial decorate

# Scene 2 — inspect checkout
Type "/def checkout" Enter
Sleep 500ms
Type ":TyO3Inspect" Enter
Sleep 2.5s
Escape

# Scene 3 — author a note
Type ":TyO3Note load-bearing checkout path" Enter
Sleep 2s                                   # virtual-text note appears

# Scene 4 — move checkout to a new file; the note follows
# (dd the function, write it into checkout.py)
Type ":TyO3Move checkout checkout.py" Enter   # plugin verb, or scripted yank+paste
Sleep 3s
Type ":e checkout.py" Enter
Sleep 2.5s                                  # note is now here, on checkout

# Scene 5 — edit a base method; watch the affected panel
Type ":e catalog.py" Enter
Sleep 1s
Type "/return 100" Enter
Type "ciw250" Escape
Sleep 500ms
Type ":w" Enter                             # debounced commit fires
Sleep 4s                                    # delta + async refinement land in panel

# Scene 6 — telescope the affected set
Type ":TyO3Affected" Enter
Sleep 2.5s
Escape
Type ":qa!" Enter
Sleep 1s
```

> The `.tape` is the contract; the plugin must expose the verbs it drives
> (`:TyO3Inspect`, `:TyO3Note`, `:TyO3Affected`, and either a `:TyO3Move` or a
> scripted yank/paste for the move). Keep `Sleep`s generous around the async
> refinement and the daemon round-trips — determinism over speed.

---

## 5. Determinism rules
- Fixed `Width`/`Height`/`FontSize`/`Theme`/`TypingSpeed` in the tape.
- The synthetic project is built fresh by `setup.sh` (no user state); the daemon
  socket is per-tmpdir.
- `Sleep` past every async beat (daemon open, debounced commit, precision
  refinement) — never race the recording against the engine.
- Pin nvim config to a **minimal `init.lua`** in `demo/` (no user plugins), so the
  recording is identical on any machine / in CI.

---

## 6. Acceptance
- `vhs editors/tyo3.nvim/demo/tour.tape` regenerates `tour.gif` + `tour.cast`
  headlessly (CI-runnable, no display).
- The README embeds `tour.gif` and links the `.cast`.
- A `devenv` script (e.g. `demo-record`) wraps the vhs invocation.
- Re-running the tape produces a visually-equivalent recording (determinism).

---

## 7. Where it plugs into the build
Add to `KICKOFF.md`'s definition-of-done and sequence: after the plugin surfaces
work (step 6), **step 6.5 — author the demo**: `setup.sh` + minimal `init.lua` +
`tour.tape`, package `vhs` in devenv, record `tour.gif`/`tour.cast`, embed in the
README. This is the canonical, regenerable proof the plugin works — it replaces
the manual checklist as the primary demo (the checklist stays as a human
fallback).
