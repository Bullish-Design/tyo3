# Phase D kickoff — AST-native navigation (proj 28)

> Paste the block below as the first message in a clean new session. It is
> self-contained; it assumes no memory of the planning or Phase A/B/C conversations.

---

You are implementing **Phase D** of an in-flight tyo3.nvim redesign (proj 28).
The plan and a file-level guide already exist — read them first, then execute
**Phase D only**. Do **not** start Phase E (edgy sidebar) or Phase F (test infra /
release).

## Goal (one line)

Make AST motion the default feel: bind **treesitter-textobjects** selections
(`af`/`if`, `ac`/`ic`, `aa`/`ia`) + **treewalker** motion (parent/child/sibling)
**buffer-local on project python buffers**, so "you're navigating the syntax tree"
is the default and a textobject selection feeds straight into the existing
entity-resolving code-action path. Navigation stays *raw AST*; the dock does the
semantic naming. This is a **pure-addition** phase (no deletion).

## Context: where the project is

- **Phase A shipped** (3 native LSP bridges) — merged to `main`.
- **Phase B shipped** (`proj28-phase-b-singlepath`, not pushed): single-path
  conversion — `deps.lua` owns the curated stack on `setup` (opt out via
  `manage = false`); always-on bridge; snacks pickers; slim config with
  `manage`/`keymaps`.
- **Phase C shipped** (`proj28-phase-c-actionhub`, off B, **not pushed/merged**):
  the LSP code-action registry is the single "act on the entity" surface, driven
  by the tiny-code-action buffer picker (`gra` keymap, bound buffer-local on
  attach). `actions.lua` + the panel ACTIONS pane are deleted. The
  selection→entity path is live: the `codeAction` handler resolves `range.start`
  up to its enclosing durable entity, so `vif` then `gra` already acts on exactly
  that function — **Phase D just makes the selection + motion ergonomic.**
- **Phase D fills the two remaining `deps.lua` stubs** (`setup_treesitter`,
  `setup_treewalker`), adds a buffer-local keymap binder, and wires nothing else —
  the dock already follows the cursor via `context.on_cursor`.
- **Branch off Phase C:** `git checkout proj28-phase-c-actionhub && git checkout
  -b proj28-phase-d-astnav` (if C has since merged to `main`, branch off `main`).

## Read these first (in order)

1. `.scratch/projects/28-nvim-opinionated-ui/GUIDE-phase-D.md` — **the file-level
   build spec, written against the real post-C code AND the real installed plugin
   versions.** Primary reference (§0 = current state, §1 = the keymaps, §2 = build
   order D.1–D.5, §3 = critical facts incl. the main-vs-master finding, §8 = the
   demo parser wrinkle).
2. `.scratch/projects/28-nvim-opinionated-ui/CONCEPT.md` §2 (navigate/observe/act)
   + §5 (the risks D resolves).
3. `.scratch/projects/28-nvim-opinionated-ui/PLAN.md` §"Phase D" (skim — the GUIDE
   supersedes it where they differ).

## Working rules (this repo — non-negotiable)

- **Run all project commands through devenv**, never raw pytest/cargo/nvim.
- **Headless Lua specs — use the PRISTINE nvim, not the PATH `nvim`** (the PATH one
  is a home-manager wrapper that injects user config even under `--clean` and can
  hang/erupt). Drive specs with the unwrapped 0.12.2 ELF + a `timeout` guard, and
  put the python parser on the rtp:
  ```
  PRISTINE=$(echo /nix/store/*neovim-unwrapped-0.12.2/bin/nvim | tr ' ' '\n' | head -1)
  GRAMMARS=$(for d in /nix/store/*nvim-treesitter-grammars; do [ -e "$d/parser/python.so" ] && echo "$d" && break; done)
  devenv shell -- timeout 240 "$PRISTINE" --headless --clean --cmd "set rtp^=$GRAMMARS" \
    -u editors/tyo3.nvim/tests/minimal_init.lua -c "luafile editors/tyo3.nvim/tests/<spec>.lua"
  ```
  (Resolve `$PRISTINE` with a glob piped through `tr`, NOT `ls` — an `ls` alias on
  this machine injects icons that corrupt the path.)
- **vhs demos / `setup.sh` must run INSIDE one `devenv shell` invocation, from the
  repo root** (the demo project lives under `mktemp` in `/tmp`; a separate sandbox
  crashes the daemon). The `demo-record*` scripts already `cd $DEVENV_ROOT`.
- **No AI attribution** anywhere (commits/PRs/comments). Match surrounding idiom.
- Commit per logical step when green; **don't push or open a PR** unless asked.

## Build, in this order (see GUIDE §2 for the detail)

1. **D.1 — `deps.setup_treesitter`.** ⚠The installed nvim-treesitter +
   nvim-treesitter-textobjects are on **`main`** (NOT `master`) — use
   `require("nvim-treesitter-textobjects").setup{ select = { lookahead = true } }`,
   **not** `nvim-treesitter.configs`. Record each absent plugin in `M.missing`.
2. **D.2 — `deps.setup_treewalker`.** `require("treewalker").setup{}` (keep the
   default post-jump highlight). Record absent.
3. **D.3 — `deps.bind_ast_keymaps(bufnr)`.** New directly-callable function (model:
   `lsp.bind_code_action_keymap`). Bind textobject select (`af`/`if`/`ac`/`ic`/
   `aa`/`ia`, modes `{x,o}`) via
   `require("nvim-treesitter-textobjects.select").select_textobject("@function.outer",
   "textobjects")`, and treewalker motion (`<C-k>/<C-j>/<C-h>/<C-l>`, modes `{n,x}`)
   via `<cmd>Treewalker Up/Down/Left/Right<cr>` — all **buffer-local**, from
   `config.keymaps`, **no-op under `manage = false`**, honouring per-leaf/sub-table
   `false` + string rebinds, lazy-requiring each plugin in the callback (notify on
   absence). Call it from `init.on_buf_enter` right after `lsp.attach`.
4. **D.4 — motion → dock is FREE.** `context.on_cursor` already debounces cursor
   moves → resolves the enclosing entity → updates the dock; treewalker/textobject
   motion fires `CursorMoved`, so nothing to wire. (Optional: drop
   `context_debounce_ms` 150→~100 for snappier tracking.)
5. **D.5 — config defaults.** Add `keymaps.textobjects` + `keymaps.treewalker`
   sub-tables (GUIDE §1).

## Critical facts & decisions (the big one is DECIDED — GUIDE §3)

- **DECIDED — both plugins are on `main`, not `master`** (the CONCEPT §5.2 spike,
  now verified). No `nvim-treesitter.configs` module system; textobjects config is
  `require("nvim-treesitter-textobjects").setup{}` and **you bind the select
  keymaps yourself** with `select.select_textobject(query, "textobjects")`.
  Parsers are NOT auto-installed on `main` — don't install/highlight in `setup_*`;
  the python textobjects queries ship with the textobjects plugin
  (`queries/python/textobjects.scm`, found via runtimepath). Full detail GUIDE §3.1.
- **DECIDED — keep navigation raw; do NOT snap motion to entities.** Bind plain
  treewalker/textobjects; the payoff is letting motion be raw AST while
  `context.on_cursor` names the enclosing entity in the dock. No "jump to next
  entity" wrapper. GUIDE §3.2.
- **DECIDED — one binding site, gated on `manage`.** Bind in
  `deps.bind_ast_keymaps`, gated `manage ~= false` (no native fallback, unlike
  `gra`). Lazy-require + notify on absence, so binding is always safe and the
  keymap-presence spec is dep-light. GUIDE §3.3.
- **Guardrail — the `<C-hjkl>` window-nav clash.** treewalker's defaults shadow
  window-move maps on project python buffers (buffer-local only, rebindable,
  `false` opts out). Document it; keep the defaults unless you have a reason. §3.4.
- **DECIDED — test split.** Primary gate = dep-light **keymap presence** in
  `tests/ast_nav.lua` (binding lazy-requires the plugins, so the maps bind with no
  stack installed — assert via `nvim_buf_get_keymap`, incl. `manage=false` ⇒ none,
  and a `treewalker=false` opt-out). The **behavioural** select→entity check is
  stack-gated (resolve via `demo/pack.lua` + a python parser on the rtp; skip
  cleanly if absent). GUIDE §3.5 / §8.

## Definition of done

See GUIDE §4. In short: the two `deps.setup_*` bodies configure the `main`-API
plugins (+ record absence); `deps.bind_ast_keymaps(bufnr)` binds the textobject +
treewalker maps buffer-local from `config.keymaps`, no-op under `manage=false`,
honouring `false`/rebinds, called from `on_buf_enter`; config gains the two
sub-tables; the dock follows motion for free; the dep-light sweep stays green and
`tests/ast_nav.lua` passes its keymap-presence assertions; a `demo/astnav/` GIF
shows real motion + select → act (the demo init must put a python parser on the
rtp — GUIDE §8).

## Verify

```
PRISTINE=$(echo /nix/store/*neovim-unwrapped-0.12.2/bin/nvim | tr ' ' '\n' | head -1)
GRAMMARS=$(for d in /nix/store/*nvim-treesitter-grammars; do [ -e "$d/parser/python.so" ] && echo "$d" && break; done)
for t in smoke context lsp lsp_nav lsp_codeaction review_dedup lsp_symbols ast_nav; do
  devenv shell -- timeout 240 "$PRISTINE" --headless --clean --cmd "set rtp^=$GRAMMARS" \
    -u editors/tyo3.nvim/tests/minimal_init.lua \
    -c "luafile editors/tyo3.nvim/tests/$t.lua" 2>&1 | grep -E "checks, [0-9]+ failed|^\[FAIL\]"
done
# Daemon sanity (unchanged by D): devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov
```

## When you finish

Report what changed (files added/modified), what you ran, and pass/fail output.
Commit on the branch with a clear message (e.g. `feat(nvim): default AST
navigation (treesitter-textobjects + treewalker)`). Then stop and report — **do
not start Phase E**. The main-vs-master finding, the keymap defaults, the `manage`
gate, the test split, and the demo parser wrinkle are all decided (GUIDE §3, §8).
The only thing worth flagging mid-build is an unexpected **`main`-API shape** in
the installed textobjects/treewalker vs what GUIDE §3.1 records. Pause and ask on
that rather than guessing.
