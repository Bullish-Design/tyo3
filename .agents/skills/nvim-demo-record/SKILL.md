---
name: nvim-demo-record
description: "Author or edit the scripted tyo3.nvim demo recordings (vhs .tape → .gif/.txt/.cast). Use when creating a new demo scene, editing an existing tour/context tape, changing the demo project or nvim init, fixing a racey/broken recording, adding a new demo variant, or re-rendering the GIFs. Covers editors/tyo3.nvim/demo/ (setup.sh, default/, context/), determinism rules, the pristine-nvim resolution, and the devenv demo-record entrypoints."
---

# Authoring tyo3.nvim demo recordings

The tyo3.nvim demos are **scripted, reproducible** terminal-Neovim sessions driven
by [vhs](https://github.com/charmbracelet/vhs). A `.tape` of scripted keystrokes
runs a real headless terminal nvim against a freshly-built synthetic project and
renders to a committed `.gif` (+ a `.txt` transcript, and for `default/` an
asciinema `.cast`). This is the canonical, regenerable proof the plugin works —
not a hand-waved screen capture. Treat the `.tape` as the contract.

The companion skill **`nvim-demo-review`** covers reading the `.txt` transcript
frame-by-frame to verify a render. Use it after every re-record.

## Layout — `editors/tyo3.nvim/demo/`

```
demo/
  setup.sh              # shared: builds the demo project, sets the LLM seam, resolves $TYO3_NVIM
  default/              # the headline tour (bespoke UI → native LSP bridge → durable review-state)
    tour.tape           # vhs script (the contract)
    init.lua            # minimal, plugin-only nvim config
    record_cast.py      # PTY driver → asciinema tour.cast (secondary artifact)
    tour.gif            # rendered GIF (committed)
    tour.txt            # frame-by-frame text transcript (committed) — see nvim-demo-review
    tour.cast           # asciinema cast (committed)
  context/              # the cursor-CONTEXT sidebar tour (collapsible IDENTITY/NOTES/DOCS/… panes)
    context.tape
    init.lua            # like default but context = "cursor"
    context.gif
    context.txt
```

A demo is a **directory** with a `*.tape` + `*.init.lua`; the `.gif`/`.txt`/`.cast`
are *outputs* it declares via `Output` lines. `setup.sh` is shared by both.

## How to render

Always from the repo root, **inside the devenv shell** (vhs is a devenv package):

```bash
devenv shell -- demo-record           # default/ → tour.gif + tour.cast
devenv shell -- demo-record-context   # context/ → context.gif
# or drive vhs directly:
devenv shell -- vhs editors/tyo3.nvim/demo/default/tour.tape
```

`demo-record` runs `vhs …/tour.tape` then `python …/record_cast.py …/tour.cast`
(vhs 0.11 doesn't emit `.cast` natively, so a tiny dependency-free PTY driver
records the *same* scenes for asciinema). These are wired in `devenv.nix`
(`scripts.demo-record` / `scripts.demo-record-context`).

Rendering is CI-runnable and needs no display. It takes a while — the tapes
deliberately `Sleep` past every async beat. **Run it in the background** and let
it finish; don't race it.

## The anatomy of a tape

```tape
Output editors/tyo3.nvim/demo/default/tour.gif     # declared outputs
Output editors/tyo3.nvim/demo/default/tour.txt
Require nvim
Require python
Set Shell "bash"
Set FontSize 18 / Width 1280 / Height 800 / Theme "Catppuccin Mocha" / TypingSpeed 55ms

Type "source editors/tyo3.nvim/demo/setup.sh && cd $TYO3_DEMO_DIR" Enter
Sleep 2s
Type "$TYO3_NVIM --clean -u $TYO3_PLUGIN_DIR/demo/default/init.lua store.py" Enter
Sleep 6s
# … scenes: Type / Enter / Escape / Ctrl+] / Sleep …
Type ":qa!" Enter
```

Key vhs verbs in use: `Type "…"` (types literal text — note it does NOT press
Enter), `Enter`, `Escape`, `Tab`, `Ctrl+]` / `Ctrl+O` / `Ctrl+W` (chords), and
`Sleep <dur>`. `Set` lines fix the window/theme/typing speed. `Output` declares a
render target — add a second `Output …/foo.txt` to get the transcript.

The `record_cast.py` `ACTIONS` list is the **same scene script in Python** (a
`(delay_seconds, key_bytes)` list, with `"\r"`=Enter, `"\x1b"`=Esc, `"\x1d"`=Ctrl+],
`"\x0f"`=Ctrl+O). It only exists for `default/`. **If you edit `tour.tape`'s
scenes, mirror the change in `record_cast.py`** or the cast and GIF will diverge.

## `setup.sh` — what it guarantees (read before touching anything)

Sourced at the top of every tape; it `export`s three things the tape relies on:

- **`$TYO3_DEMO_DIR`** — a fresh synthetic "shop" project in a `mktemp -d`, built by
  `tyo3.demo.tour._build_project` (the *same* project the CLI tour uses, so the
  recording is deterministic and matches the CLI). It also pre-creates an
  empty-but-importing `checkout.py` so the Scene-4 `:TyO3Move` binds as a **Moved**
  (durable identity preserved) rather than a fresh insert. If you change which
  entities/files a scene touches, update `_build_project` expectations here.
- **`$TYO3_PLUGIN_DIR`** — `editors/tyo3.nvim`, used to locate `init.lua`.
- **`$TYO3_NVIM`** — a **pristine** nvim binary (see below).
- **The LLM seam** — `TYO3_LLM=callable` + `TYO3_LLM_CALLABLE=tyo3.demo.explanations:explain`
  point the `explain` code action at a curated **offline** backend so AI prose
  renders deterministically (no network, no key). The daemon nvim spawns inherits
  this. A real install uses `TYO3_LLM=anthropic` + `ANTHROPIC_API_KEY`.

### The pristine-nvim gotcha (`$TYO3_NVIM`)

The `nvim` on PATH here is a Nix/home-manager **wrapper** that force-injects the
user config dir onto the runtimepath **even under `--clean`**. That user
`after/ftplugin/python.lua` requires which-key (absent in the isolated env) and
erupts with an E5113 traceback at startup, overwriting the recording. `setup.sh`'s
`_resolve_nvim()` follows the wrapper's `exec` chain down to the real
`neovim-unwrapped` ELF (which honours `--clean`) **with no hardcoded store hash**,
and the tape drives *that* via `$TYO3_NVIM --clean -u …/init.lua`. If a render
suddenly fills with a Lua traceback, this resolution is the first suspect.

## `init.lua` — the demo's nvim config

Minimal and plugin-only so the recording is identical on any machine / in CI. It
enables syntax + filetype (safe now that nvim is pristine), appends the plugin
root to `runtimepath`, `runtime! plugin/tyo3.lua`, and calls `require("tyo3").setup{…}`.
Notable knobs:

- `daemon_cmd = { "python", "-m", "tyo3.daemon" }` — module form, no console-script install needed.
- `debounce_ms` — `default/` uses **400** (just above `TypingSpeed` so a multi-keystroke
  edit like `ciwprice` commits **once**, after typing settles); `context/` uses 200.
- `panel = "always"`, `virtual_text = true`.
- `default/init.lua` also registers a **third-party-style code action** in one call
  (`lsp.register_entity_action{…}`) to showcase the registry seam — it shows up in
  the same code-action menu as Explain/Simplify.
- `context/init.lua` adds `context = "cursor"` + `context_debounce_ms = 120` — the
  feature that demo is about (the CONTEXT pane tracks the entity under the cursor).

A `vim.schedule(function() require("tyo3.panel").open() end)` opens the ambient
panel up front so Scene 1 already shows it.

## Determinism rules (do not break these)

1. **`Sleep` past every async beat** — daemon open (~6s after launch), debounced
   commit (after `:w`), precision refinement, pushed diagnostics, bus-driven layer
   refresh, and each async LSP round-trip (hover, typehierarchy is a *two-hop*
   prepare→super/subtypes query). Generous sleeps; **determinism over speed**.
2. **Fixed `Width`/`Height`/`FontSize`/`Theme`/`TypingSpeed`.** Changing these
   re-flows the whole transcript — expect a large `.txt` diff.
3. **Fresh project every run** via `setup.sh` (`mktemp -d`, per-tmpdir daemon
   socket) — no user state leaks in.
4. **Pristine, plugin-only nvim** (`$TYO3_NVIM --clean -u init.lua`).
5. **Cursor-landing precision matters.** Search to the exact token before an edit:
   e.g. `/100` lands on the literal (so `ciw250` changes the value), not `/return 100`
   which would put the cursor on `return` and `ciw` would clobber the keyword.
   Case-sensitive searches (`/Item` ≠ `/item`). These comments are load-bearing —
   preserve the reasoning when you move a scene.
6. **Run LSP queries with the cursor in the CODE buffer**, not the quickfix window
   (which has no LSP client). For typehierarchy, fire the query, `Sleep` past the
   round trip, *then* `:copen`.

## Editing / adding scenes — workflow

1. **Pick the demo dir** (`default/` for the headline story; `context/` for the
   sidebar). Add a new sibling dir for a genuinely new variant (copy the structure,
   give it its own `Output` paths and `init.lua`, and a `devenv` `scripts.demo-record-<name>`).
2. **Edit the `.tape`**: add `Type/Enter/Sleep` blocks. Keep the `# ── Scene N ──`
   comment banners and the per-scene rationale comments — they explain *why* a
   sleep or a precise search exists. Match the existing comment density.
3. **If it's `default/`, mirror the new scene in `record_cast.py`'s `ACTIONS`**
   (convert each `Type "…" Enter` to `(delay, "…\r")`, chords to control bytes).
4. **Re-render** (background): `devenv shell -- demo-record` (or `-context`).
5. **Verify with `nvim-demo-review`** — read the new frames in the `.txt`, confirm
   the panel/virtual-text/diagnostic/quickfix content is what the scene promises
   and there's no traceback or half-typed command. Don't trust the GIF alone.
6. **Commit** the `.tape`/`init.lua`/`record_cast.py` source **and** the regenerated
   `.gif`/`.txt`/`.cast` together (they're all git-tracked).

## What the `default/` tour proves (scene map)

Scenes 1–6 are tyo3's **bespoke** UI: open + ambient decorate, `:TyO3Inspect`
float, `:TyO3Note` (durable virtual-text), `:TyO3Move` (note rides to the new
file — the money shot), edit `Item.price` → live affected panel that narrows via
precision refinement, `:TyO3Affected` picker. Scene 7 flips on the **native LSP
bridge** (`:TyO3Lsp`); Scenes 8–11 drive Neovim's own machinery through tyo3 —
hover `K`, goto-def `Ctrl+]`, references `grr`, type hierarchy super/subtypes,
cross-file rename, and an AI "Explain this" code action stored on a durable
identity-keyed layer. Scene 12 is the headline: one edit both **drifts** a durable
note (`needs_review`) and breaks a return type, and **both** ride the same `]d`
diagnostic stream — surfaced on a normal `:w` because review-state is durable.

The `context/` tour keeps the data-type-separated sidebar on throughout
(IDENTITY/NOTES/DOCS/SUMMARY/ACTIONS panes + AFFECTED log), runs an AI Explain
from the ACTIONS pane into NOTES, authors a markdown doc, and shows notes/doc/AI
summary all surviving an edit and an atomic move.

## References

- `.scratch/projects/20-neovim-integration/DEMO_RECORDING.md` — the original design
  rationale (why vhs over asciinema+tmux, the GUI-vs-TUI question, acceptance).
- `devenv.nix` — `scripts.demo-record` / `demo-record-context`, `pkgs.vhs`.
- `src/tyo3/demo/tour.py` (`_build_project`) and `src/tyo3/demo/explanations.py`
  (the offline LLM backend) — the deterministic content sources.
