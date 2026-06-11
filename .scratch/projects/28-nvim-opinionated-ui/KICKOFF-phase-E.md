# Phase E kickoff — edgy accordion sidebar (proj 28)

> Paste the block below as the first message in a clean new session. It is
> self-contained; it assumes no memory of the planning or Phase A–D conversations.

---

You are implementing **Phase E** of an in-flight tyo3.nvim redesign (proj 28).
The plan and a file-level guide already exist — read them first, then execute
**Phase E only**. Do **not** start Phase F (test infra / hero re-record / README /
release).

## Goal (one line)

Replace `lua/tyo3/panel.lua`'s hand-rolled split + monolithic render with
**edgy-managed, vertically stacked, one-category-per-view section windows**
(IDENTITY / NOTES / DOCS / SUMMARY / AFFECTED), fed by the *same* events the panel
consumed, with a focus/content accordion (the relevant section expands, the rest
collapse to title height). Delete `panel.lua` + `inspect.lua`. This is the
highest-risk phase, but **most of the work is deletion + re-pointing** — the one
genuine unknown (the accordion) is already spiked and decided in the guide.

## Context: where the project is

- **Phase A shipped** (3 native LSP bridges) — merged to `main`.
- **Phase B shipped** (`proj28-phase-b-singlepath`, not pushed): single-path
  conversion — `deps.lua` owns the curated stack on `setup` (opt out via
  `manage = false`); always-on bridge; snacks pickers.
- **Phase C shipped** (`proj28-phase-c-actionhub`, not pushed): the code-action
  registry is the single "act on the entity" surface (tiny-code-action buffer
  picker, `gra`); the panel is now **observe-only** (ACTIONS pane deleted).
- **Phase D shipped** (`proj28-phase-d-astnav`, off C, **not pushed/merged**):
  AST-native navigation — treesitter-textobjects selects + treewalker motion bound
  buffer-local; the dock follows the cursor via `context.on_cursor`.
- **Phase E swaps the dock's *hosting*** (one hand-rolled window → N edgy windows)
  without changing where the data comes from. `deps.setup_edgy` is a ready stub.
- **Branch off Phase D:** `git checkout proj28-phase-d-astnav && git checkout -b
  proj28-phase-e-sidebar` (if D has since merged to `main`, branch off `main`).

## Read these first (in order)

1. `.scratch/projects/28-nvim-opinionated-ui/GUIDE-phase-E.md` — **the file-level
   build spec, written against the real post-D code AND the real installed edgy
   (v1.10.2).** Primary reference (§0 = current state + the consumer re-point table,
   §1 = the five views + package layout, §2 = build order E.0–E.4, §3 = the spiked
   accordion decision + guardrails, §8 = the test/demo split).
2. `.scratch/projects/28-nvim-opinionated-ui/CONCEPT.md` §2 (navigate/observe/act)
   + §4 (what gets deleted) + §5.1 (the accordion spike).
3. `.scratch/projects/28-nvim-opinionated-ui/PLAN.md` §"Phase E" (skim — the GUIDE
   supersedes it where they differ).

## Working rules (this repo — non-negotiable)

- **Run all project commands through devenv**, never raw pytest/cargo/nvim.
- **Headless Lua specs — use the PRISTINE nvim, not the PATH `nvim`** (the PATH one
  is a home-manager wrapper that injects user config even under `--clean` and can
  hang/erupt). Drive specs with the unwrapped 0.12.2 ELF + a `timeout` guard:
  ```
  PRISTINE=$(echo /nix/store/*neovim-unwrapped-0.12.2/bin/nvim | tr ' ' '\n' | head -1)
  GRAMMARS=$(for d in /nix/store/*nvim-treesitter-grammars; do [ -e "$d/parser/python.so" ] && echo "$d" && break; done)
  devenv shell -- timeout 240 "$PRISTINE" --headless --clean --cmd "set rtp^=$GRAMMARS" \
    -u editors/tyo3.nvim/tests/minimal_init.lua -c "luafile editors/tyo3.nvim/tests/<spec>.lua"
  ```
  (Resolve `$PRISTINE` with a glob piped through `tr`, NOT `ls` — an `ls` alias on
  this machine injects icons that corrupt the path.) The full curated stack
  (incl. **edgy.nvim v1.10.2**) is installed locally and resolvable via
  `editors/tyo3.nvim/demo/pack.lua`.
- **vhs demos / `setup.sh` must run INSIDE one `devenv shell` invocation, from the
  repo root** (the demo project lives under `mktemp` in `/tmp`; a separate sandbox
  crashes the daemon). The `demo-record*` scripts already `cd $DEVENV_ROOT`.
- **No AI attribution** anywhere (commits/PRs/comments). Match surrounding idiom.
- Commit per logical step when green; **don't push or open a PR** unless asked.

## Build, in this order (see GUIDE §2 for the detail)

1. **E.0 — Sanity-check the spike (10 min).** The accordion finding is decided in
   GUIDE §3.1 (edgy expands the entered view but does NOT collapse siblings; the
   lever is `require("edgy.config").layout["right"].views[i].wins[1]:show(bool)` +
   `require("edgy.layout").update()`; edgy needs `laststatus=3`/`splitkeep=screen`).
   Stand up two throwaway views and confirm. If it diverges, PAUSE and flag.
2. **E.1 — `deps.setup_edgy`.** Set `laststatus=3`/`splitkeep=screen`, register five
   right-edge views (IDENTITY/NOTES/DOCS/SUMMARY/AFFECTED), `record_missing` when
   absent, then `require("tyo3.sidebar").setup(edgy)`.
3. **E.2 — `lua/tyo3/sidebar/`.** `render.lua` = the section renderers lifted from
   `panel.lua` (pure `card → string[]`). `init.lua` = persistent `tyo3_*` scratch
   buffers + the edgy view specs (`pinned=true` + `open` displays the buffer) + the
   panel-compatible public API (`set_context`/`clear_context`/`reload`/`on_delta`/
   `on_derived`/`on_refinement`/`toggle`/`last_affected_ids`) + the accordion. Then
   **re-point all six consumers** (the GUIDE §0 table).
4. **E.3 — Delete** `panel.lua` + `inspect.lua`; `:TyO3Panel`→`:TyO3Sidebar`;
   `:TyO3Inspect`→ focus IDENTITY; sweep `require("tyo3.panel")`/`inspect` to empty.
5. **E.4 — Tests.** `tests/sidebar.lua`: dep-light renderer purity (primary) +
   edgy-gated window/ft + context wiring (skip-if-absent). Update `tests/context.lua`
   (it asserts the old panel text).

## Critical facts & decisions (the spike is DECIDED — GUIDE §3)

- **DECIDED — the accordion is NOT native** (edgy v1.10.2, read from source):
  `on_win_enter` expands the entered collapsed view but never collapses siblings.
  Use the §3.1 lever (`view.wins[1]:show(bool)` + `edgy.layout.update()`). Drive it
  **content-first** (expand sections that have data for the current entity; collapse
  empty ones) — the dock is observe-only and cursor-driven, so "focused section" =
  "the section with data", not a focused window — plus a `WinEnter` focus-accordion
  as a secondary affordance. **Explicit fallback if the lever is fragile across
  resizes: ship all-sections-open with edgy `size` + `(none)` placeholders.** The
  DoD is met either way. GUIDE §3.1, §3.3.
- **DECIDED — each view is a window+buffer joined by `ft`.** Persistent `tyo3_*`
  scratch buffers created once in `sidebar.setup`; views are `pinned = true` with an
  `open` that displays the existing buffer (`vertical sbuffer …`). GUIDE §3.2.
- **DECIDED — re-point ALL six consumers; keep `last_affected_ids`.** Easy to miss:
  `picker.lua:125` reads `panel.last_affected_ids` (the :TyO3Affected picker), and
  `entitydoc.lua:69` calls `panel.reload()` after a doc save. GUIDE §0 table, §3.4.
- **DECIDED — `:TyO3Inspect` → focus the IDENTITY view; delete `inspect.lua`**
  (`card.build_lines` survives as the IDENTITY renderer). GUIDE §3.5.
- **Guardrail — `manage = false` ⇒ no sidebar; `set_context` must no-op safely**
  (it's called unconditionally from `context.on_cursor`). GUIDE §3.6.
- **Guardrail — edgy is a global window manager** (sets `laststatus=3`/`splitkeep`,
  arbitrates all layout) — the opinionated-distribution tradeoff, gated by `manage`.
  Note it in the commit. GUIDE §3.7.

## Definition of done

See GUIDE §4. In short: `deps.setup_edgy` sets the globals + registers five `tyo3_*`
views; `lua/tyo3/sidebar/` renders each section from the card/bus, exposes the
panel-compatible API, and drives the accordion (or the documented fallback); all six
consumers re-pointed; `panel.lua` + `inspect.lua` deleted (grep empty);
`:TyO3Sidebar` toggles; `:TyO3Inspect` focuses IDENTITY; `manage=false` ⇒ no sidebar,
no error; the dep-light sweep stays green; `tests/sidebar.lua` renderer purity passes
(window block skips where edgy is absent); a `demo/sidebar/` GIF shows the dock
tracking the cursor with the accordion.

## Verify

```
PRISTINE=$(echo /nix/store/*neovim-unwrapped-0.12.2/bin/nvim | tr ' ' '\n' | head -1)
GRAMMARS=$(for d in /nix/store/*nvim-treesitter-grammars; do [ -e "$d/parser/python.so" ] && echo "$d" && break; done)
for t in smoke context lsp lsp_nav lsp_codeaction review_dedup lsp_symbols ast_nav sidebar; do
  devenv shell -- timeout 240 "$PRISTINE" --headless --clean --cmd "set rtp^=$GRAMMARS" \
    -u editors/tyo3.nvim/tests/minimal_init.lua \
    -c "luafile editors/tyo3.nvim/tests/$t.lua" 2>&1 | grep -E "checks, [0-9]+ failed|^\[FAIL\]"
done
# Daemon sanity (unchanged by E): devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov
```

(`tests/context.lua` will need its assertions re-pointed to the new sidebar — that's
expected churn, part of E, not a regression.)

## When you finish

Report what changed (files added/deleted/modified), what you ran, and pass/fail
output. Commit on the branch with a clear message (e.g. `feat(nvim): edgy accordion
sidebar (replaces panel.lua)`). Then stop and report — **do not start Phase F**. The
accordion spike, the view table, the `pinned`/`open` pattern, the consumer re-point,
and the observe-only reframe are all decided (GUIDE §3). The one thing worth pausing
to flag mid-build is if the §3.1 collapse lever does NOT cleanly collapse/expand
across resizes in E.0 — take the all-open fallback and note it rather than fighting
edgy internals.
