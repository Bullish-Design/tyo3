# Phase B kickoff — Single-path conversion + dependency adoption (proj 28)

> Paste the block below as the first message in a clean new session. It is
> self-contained; it assumes no memory of the planning or Phase-A conversation.

---

You are implementing **Phase B** of an in-flight tyo3.nvim redesign (proj 28).
The plan already exists — read it first, then execute Phase B only. Do **not**
start Phases C–F (action hub, AST nav, edgy sidebar, release). Phase B is the
"slimming" phase: it converts the plugin from an opt-in toolkit to a single
curated path and is mostly **deletion**.

## Context: where the project is

- **Phase A shipped** on branch `proj28-phase-a-bridges` (off `main`): it added
  three native LSP bridges — `documentSymbol`, `workspace/symbol`, and call
  hierarchy — plus a `demo/symbols/` recording. It was pure-additive; the bridge
  is still **opt-in** (`setup{ lsp = true }`).
- **Phase B makes the bridge always-on, adopts a curated dependency stack
  (snacks/edgy/tiny-code-action/treesitter/treewalker), routes pickers/prompts
  through snacks, and deletes the fallback paths.** "No degradation" applies to
  UI/interaction paths only — **AI stays optional** (explain/simplify only when
  the project declares the layer; the Simplify parse-guard fallback stays).
- **Branch off the Phase-A work**, not a stale main:
  `git checkout proj28-phase-a-bridges && git checkout -b proj28-phase-b-singlepath`
  (if Phase A has since been merged to `main`, branch off `main` instead).

## Read these first (in order)

1. `.scratch/projects/28-nvim-opinionated-ui/CONCEPT.md` — the vision and locked
   decisions. §1 (locked decisions), §3 (the dep stack), §4 (what gets deleted)
   are the spec's spine for Phase B.
2. `.scratch/projects/28-nvim-opinionated-ui/PLAN.md` — **§"Phase B"** (B.1–B.6)
   is your build spec. Skim Phase A (done) and C–F (later) for context only.

## Goal

One curated stack, one path through every interaction, no UI fallbacks:
- A new `lua/tyo3/deps.lua` **owns** dependency setup on `M.setup` (opt-out via
  `opts.manage = false`): snacks (picker + input + `vim.ui` overrides),
  tiny-code-action (buffer picker), and stubs for edgy/treesitter/treewalker
  (wired in later phases). Missing hard dep ⇒ a clear `:checkhealth` error, **no
  silent fallback**.
- The in-process LSP bridge attaches on **every** project python buffer
  unconditionally (no `lsp` flag).
- `telescope.lua` → a snacks-backed `picker.lua`; delete
  `has_telescope`/`select_fallback`/`vim.ui.select` fallbacks.
- Slim `config.lua`: drop the now-always-on flags, add `manage` + `keymaps`.

## Working rules (this repo — non-negotiable)

- **Run all project commands through devenv**, never raw pytest/cargo/nvim:
  - Build (only if you touch Rust/Python — Phase B is Lua + maybe a tiny config
    note, so you likely won't): `devenv shell -- build`
  - Daemon tests (sanity, unchanged by B): `devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov`
  - Full Python gate: `devenv shell -- test-fast`
- **Headless Lua specs — use the PRISTINE nvim, not the PATH `nvim`.** The PATH
  `nvim` is a home-manager wrapper that injects `~/.dotfiles/nvim/init.lua` even
  under `--clean`; it can hang on headless exit (deadlocking a sweep) or erupt.
  Drive specs with the unwrapped 0.12.2 ELF and a `timeout` guard:
  ```
  PRISTINE=$(ls -d /nix/store/*neovim-unwrapped-0.12.2/bin/nvim | head -1)
  devenv shell -- timeout 240 "$PRISTINE" --headless --clean \
    -u editors/tyo3.nvim/tests/minimal_init.lua \
    -c "luafile editors/tyo3.nvim/tests/<spec>.lua"
  ```
  Engine/bridge specs: `smoke`, `context`, `lsp`, `lsp_nav`, `lsp_codeaction`,
  `review_dedup`, `lsp_symbols`. **`context.lua` has treesitter-gated checks** —
  under bare `--clean` they fail for lack of a Python parser; add a grammar to
  the rtp to get 20/0: `--cmd "set rtp^=/nix/store/*nvim-treesitter-grammars"`
  (one that has `parser/python.so`). Not a regression — just a missing parser.
- **vhs demos / `setup.sh` must run INSIDE one `devenv shell` invocation** (the
  shop project lives under `mktemp` in `/tmp`; a separate `devenv shell --` call
  has its own sandboxed `/tmp` and the daemon will crash with PathResolutionError
  / exit 137). The `demo-record*` scripts already do this correctly.
- **No AI attribution** anywhere (commits/PRs/comments). Match surrounding idiom.
- Commit per logical step when green; don't push or open a PR unless asked.

## Study these existing patterns before writing code

- `lua/tyo3/init.lua` — `M.setup`, `M.on_buf_enter` (the attach path: it already
  attaches the bridge when `config.get().lsp`, and seeds layer diagnostics when
  `config.layer_diagnostics_enabled()`), `M.handle_notification` (push
  diagnostics guarded by `config.get().lsp`).
- `plugin/tyo3.lua` — the BufReadPost/BufEnter autocmd, the `:TyO3*` commands
  (incl. `:TyO3Lsp`, `:TyO3Affected/Entities/Authored`).
- `lua/tyo3/config.lua` — current defaults. **Removing in B.5:** `lsp` (default
  false), `auto_start`, `layer_diagnostics` (+ the `layer_diagnostics_enabled()`
  helper that follows `lsp`), `panel`, `context`. **Keeping:** `debounce_ms`,
  `context_debounce_ms`, `precision`, `root_markers`, `daemon_cmd`,
  `request_timeout_ms`, `log_level`, `overseer`, `virtual_text`. **Adding:**
  `manage = true`, `keymaps = {}` (defaults filled in C/D), and an `ai`/`explain`
  note that AI actions are project-config-driven.
- `lua/tyo3/telescope.lua` — the three pickers (`entities()`, `affected()`,
  `authored(layer)`), `has_telescope`, the `select_fallback`/`vim.ui.select`
  path, and the precise `jump_to` (decorate-fresh range — **keep this**).
- `lua/tyo3/lsp.lua` — `M.attach`, `M.toggle` + the `lsp`/`layer_diagnostics`
  guards to delete; `refresh_layer_diagnostics`.
- `lua/tyo3/notes.lua`, `docs.lua`, `actions.lua` — `vim.ui.input`/`vim.ui.select`
  call sites (they become snacks-backed automatically once snacks overrides
  `vim.ui`; leave the call sites, drop the "fallback when telescope absent"
  comments).
- `lua/tyo3/health.lua` — `:checkhealth` surface (the missing-dep errors land here).

## Build, in this order (B.1–B.5)

1. **B.1 — `lua/tyo3/deps.lua` (new).** `M.setup(opts)` returns early if
   `opts.manage == false`; else calls `setup_snacks`, `setup_tiny_code_action`,
   and stubs `setup_edgy`/`setup_treesitter`/`setup_treewalker` (real bodies land
   in D/E — leave clear TODO + a no-op or minimal call now). Each `setup_*` does
   `pcall(require, mod)`; on failure push a message into `M.missing` for
   `:checkhealth`. ⚠**API** — pin the snacks + tiny-code-action versions and
   verify the `setup{}` key shape before coding (these evolve fast):
   `require("snacks").setup({ picker = { enabled = true }, input = { enabled =
   true } })` (or detect an already-configured snacks and only enable what TyO3
   needs); `require("tiny-code-action").setup({ picker = { "buffer", opts = {
   hotkeys = true } } })`. Call `deps.setup(opts)` from `M.setup`.
2. **B.2 — bridge always-on.** In `init.on_buf_enter`, attach the in-process
   server on every project python buffer unconditionally (drop the
   `if config.get().lsp` guard). Layer-state diagnostics always refresh (hardwire
   true / drop the `layer_diagnostics_enabled()` indirection). Delete
   `M.toggle`, the `:TyO3Lsp` command, and the `config.get().lsp` guard in
   `init.handle_notification` (push diagnostics always run).
3. **B.3 — snacks pickers.** Rewrite the three pickers over `Snacks.picker` in a
   new `lua/tyo3/picker.lua`; delete `telescope.lua` (and `has_telescope` /
   `select_fallback` / `vim.ui.select`). **Keep the precise `jump_to`** (re-reads
   the entity's fresh range via `decorate` before jumping). ⚠**API** — verify the
   custom-source signature for the pinned snacks (`Snacks.picker({ items=…,
   format=…, confirm=function(picker,item) picker:close(); jump_to(...) end })`).
   Update callers: `plugin/tyo3.lua` (`:TyO3Affected/Entities/Authored`) and the
   `actions.lua` "Affected set (picker)" entry.
4. **B.4 — prompts through snacks.** Enable snacks' `vim.ui.select`/`vim.ui.input`
   overrides in `setup_snacks` so `notes.lua`/`docs.lua`/`actions.lua` prompts
   route through snacks with **no call-site change**. Remove the "fallback when
   telescope absent" comments.
5. **B.5 — config slimming.** Apply the remove/keep/add list above in
   `config.lua`, and delete the `auto_start = false` autocmd asymmetry (the
   cursor/textchanged autocmds always run now).

## Critical facts & decisions (don't get these wrong — pause & ask if unsure)

- **Removing flags will break every demo init and several specs that pass them.**
  `demo/{symbols,context,hero,default}/init.lua` set `lsp`/`auto_start`/`panel`/
  `context`; the headless specs (`lsp_nav.lua`, `lsp_symbols.lua`, etc.) call
  `setup({ …, lsp = true, auto_start = true })` and then `require("tyo3.lsp").attach`.
  **Decide and apply one of:** (a) make `config.set` *silently ignore* unknown
  keys (so old opts don't error — recommended, and future-proof), AND (b) update
  the demo inits + specs to drop the dead flags. Do both: tolerate unknown keys
  *and* clean up the in-repo call sites. The specs' explicit `attach` becomes
  redundant (on_buf_enter now always attaches) but is idempotent — leave or
  remove, your call; just keep them green.
- **Verification gap: the snacks picker rewrite is not headless-testable yet.**
  The test harness is deliberately dependency-light and the UI-dep test infra is
  **Phase F**. So Phase B's hard rule is: **the dep-light engine/bridge specs
  must STILL pass** (always-on bridge + config slim must not break
  smoke/context/lsp/lsp_nav/lsp_codeaction/review_dedup/lsp_symbols). For the
  *new* snacks pickers, verify one of: (i) **record a short vhs session** that
  drives `:TyO3Entities`/`:TyO3Affected` through the snacks picker (this caught a
  real bug in Phase A — recommended), or (ii) pull a *minimal* snacks bootstrap
  forward (a `tests/deps/` clone on the rtp) and write a thin `picker.lua` spec.
  **Recommend (i) + keeping a Phase-F TODO for the real spec.** Flag this choice
  before building B.3.
- **snacks/tiny-code-action API drift** — pin a version and verify the
  `require(...).setup{}` keys and the picker source signature against THAT
  version's docs/source before coding (don't trust the plan's sketch).
- **AI stays optional** — do not make snacks or any UI dep gate the
  explain/simplify actions; those remain project-config-driven. The LLM backend
  degradation (anthropic → callable → stub) is unchanged.
- **`manage = false` must fully opt out** — when set, `deps.setup` does nothing
  and the user is responsible for the stack; TyO3 still functions (bridge,
  commands), it just doesn't configure anyone else's plugins.

## Definition of done

- `deps.lua` owns snacks + tiny-code-action setup (opt-out via `manage = false`),
  records missing hard deps for `:checkhealth`.
- Bridge attaches on every project python buffer; `M.toggle` + `:TyO3Lsp` gone;
  layer diagnostics always on; no `config.get().lsp` guards remain
  (`rg "\.lsp\b|layer_diagnostics|auto_start|\bpanel\b|TyO3Lsp|has_telescope|select_fallback"
  editors/tyo3.nvim/lua editors/tyo3.nvim/plugin` comes back clean except
  intended references).
- `telescope.lua` deleted; `picker.lua` drives the three pickers over
  `Snacks.picker`; precise `jump_to` preserved; callers updated.
- `config.lua` slimmed (flags removed, `manage`/`keymaps` added); unknown opts no
  longer error; demo inits updated.
- **Dep-light specs green** (the regression sweep below) and the snacks pickers
  verified by your chosen method (recorded session preferred).

## Verify

```
# Dep-light regression sweep (must all stay 0 failed). Use the pristine binary;
# add the TS grammar for context.lua.
PRISTINE=$(ls -d /nix/store/*neovim-unwrapped-0.12.2/bin/nvim | head -1)
GRAMMARS=$(ls -d /nix/store/*nvim-treesitter-grammars 2>/dev/null | while read d; do [ -e "$d/parser/python.so" ] && echo "$d" && break; done)
for t in smoke context lsp lsp_nav lsp_codeaction review_dedup lsp_symbols; do
  devenv shell -- timeout 240 "$PRISTINE" --headless --clean \
    --cmd "set rtp^=$GRAMMARS" \
    -u editors/tyo3.nvim/tests/minimal_init.lua \
    -c "luafile editors/tyo3.nvim/tests/$t.lua" 2>&1 | grep -E "checks, [0-9]+ failed|^\[FAIL\]"
done
# Daemon sanity (unchanged by B): devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov
# Snacks pickers: record a session (see verification note) or a minimal spec.
```

## When you finish

Report what changed (files added/deleted/modified), what you ran, and pass/fail
output. Commit on the branch with a clear message (e.g. `refactor(nvim):
single-path UI — snacks pickers, always-on bridge, drop fallbacks`). Then stop
and report — **do not start Phase C**. If the snacks/tiny-code-action API shape,
the verification approach, or the removed-flag backward-compat decision is
ambiguous, pause and ask rather than guessing.
