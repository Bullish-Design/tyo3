# Phase C kickoff — Action hub via tiny-code-action (proj 28)

> Paste the block below as the first message in a clean new session. It is
> self-contained; it assumes no memory of the planning or Phase A/B conversations.

---

You are implementing **Phase C** of an in-flight tyo3.nvim redesign (proj 28).
The plan and a file-level guide already exist — read them first, then execute
**Phase C only**. Do **not** start Phases D–F (AST nav, edgy sidebar, release).

## Goal (one line)

Make the LSP code-action registry the **single** "act on the entity" surface,
driven through the **tiny-code-action buffer picker** (default keymap), and delete
`actions.lua` + the panel ACTIONS pane. Navigation stays native (`gd`/`grr`/`K`/
`]d`). This is an addition-then-deletion phase.

## Context: where the project is

- **Phase A shipped** (3 native LSP bridges: documentSymbol, workspace/symbol,
  call hierarchy) — merged to `main`.
- **Phase B shipped** on branch `proj28-phase-b-singlepath` (off `main`, **not
  pushed/merged**): single-path conversion — `deps.lua` owns the curated stack
  (snacks/tiny-code-action/edgy/treesitter/treewalker) on `setup` (opt out via
  `manage = false`); the in-process LSP bridge is **always on**; snacks-backed
  `picker.lua` replaced `telescope.lua` (deleted); prompts route through snacks'
  `vim.ui` overrides; config slimmed (dropped `lsp`/`auto_start`/
  `layer_diagnostics`/`panel`/`context`, added `manage`/`keymaps`); cursor-context
  + panel + daemon spawn are hardwired always-on. A `demo/picker/` vhs recording
  verifies the pickers.
- **Phase C makes the code-action registry the act surface.** The registry and the
  tiny-code-action buffer-picker config **already exist** (Phase B); C wires the
  remaining entity actions (author/doc/move) into the registry, binds the
  buffer-picker keymap, and deletes the old `actions.lua` catalog + panel ACTIONS
  pane. AI stays optional.
- **Branch off the Phase-B work:** `git checkout proj28-phase-b-singlepath &&
  git checkout -b proj28-phase-c-actionhub` (if B has since merged to `main`,
  branch off `main`).

## Read these first (in order)

1. `.scratch/projects/28-nvim-opinionated-ui/GUIDE-phase-C.md` — **the file-level
   build spec for Phase C, written against the real post-B code.** This is your
   primary reference (§0 = current state, §1 = the action mapping, §2 = build
   order C.1–C.5, §3 = critical facts, §8 = the verification wrinkle).
2. `.scratch/projects/28-nvim-opinionated-ui/CONCEPT.md` §2 (the navigate/observe/
   act model) + §4 (what gets deleted).
3. `.scratch/projects/28-nvim-opinionated-ui/PLAN.md` §"Phase C" (skim — the GUIDE
   supersedes it where they differ).

## Working rules (this repo — non-negotiable)

- **Run all project commands through devenv**, never raw pytest/cargo/nvim:
  - Daemon tests (sanity): `devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov`
  - Full Python gate (if you touch Python — unlikely in C): `devenv shell -- test-fast`
- **Headless Lua specs — use the PRISTINE nvim, not the PATH `nvim`** (the PATH
  one is a home-manager wrapper that injects user config even under `--clean` and
  can hang/erupt). Drive specs with the unwrapped 0.12.2 ELF + a `timeout` guard:
  ```
  PRISTINE=$(echo /nix/store/*neovim-unwrapped-0.12.2/bin/nvim | tr ' ' '\n' | head -1)
  GRAMMARS=$(for d in /nix/store/*nvim-treesitter-grammars; do [ -e "$d/parser/python.so" ] && echo "$d" && break; done)
  devenv shell -- timeout 240 "$PRISTINE" --headless --clean --cmd "set rtp^=$GRAMMARS" \
    -u editors/tyo3.nvim/tests/minimal_init.lua -c "luafile editors/tyo3.nvim/tests/<spec>.lua"
  ```
  (Resolve `$PRISTINE` with a glob piped through `tr`, NOT `ls` — an `ls` alias on
  this machine injects icons that corrupt the path.) `context.lua` needs the TS
  grammar on the rtp (the `--cmd "set rtp^=…"` above) or its treesitter checks
  fail for lack of a python parser — not a regression.
- **vhs demos / `setup.sh` must run INSIDE one `devenv shell` invocation, from the
  repo root** (the demo project lives under `mktemp` in `/tmp`; a separate sandbox
  crashes the daemon). The `demo-record*` scripts already do this — invoke them
  from the repo root (`cd $DEVENV_ROOT` is built in; don't launch from a subdir or
  devenv prints a banner instead of running).
- **No AI attribution** anywhere (commits/PRs/comments). Match surrounding idiom.
- Commit per logical step when green; **don't push or open a PR** unless asked.

## Build, in this order (see GUIDE §2 for the detail)

1. **C.1 — Fold `actions.lua` → providers + commands (`lua/tyo3/lsp.lua`).**
   Fetch the `layers` verb in the codeAction handler → `ctx.layers` (only when an
   entity resolved), and **gate the existing Explain/Simplify provider on
   `ctx.layers.explain`**. Add `tyo3.author` (one action per writable layer from
   `ctx.layers`: `origin=="authored"` minus `docs`/`explain`, **no `intent`
   fallback**; factor a `M.author_note(...)` core, prompt via `vim.ui.input` then
   call it), `tyo3.doc` (→ `entitydoc.edit_card`), and `tyo3.move`
   ("Move `<name>` to…" → prompt dest → `move.move`). Register them through the
   existing `M.register_code_action` / `M.register_command` seam. Extend
   `codeActionKinds` + `TYO3_KINDS` + `executeCommandProvider.commands`
   (`refactor.move`, `tyo3.author`, `tyo3.doc`, `tyo3.move`). **Drop** the prose
   `mode="simplify"` float (the resolvable Simplify supersedes it) and the
   Inspect / goto / find-callers entries (IDENTITY pane + native `gd`/`grr`).
2. **C.2 — Default buffer-picker keymap.** Bind a buffer-local key on project
   python buffers → `require("tiny-code-action").code_action()`, from
   `config.keymaps.code_action` (suggest default `gra` and/or `<leader>a`). ⚠Verify
   the tiny-code-action entry-point name/signature.
3. **C.3 — Preview pane.** Simplify already resolves to a diff (free). Accept
   title-only for informational actions (defer enrichment).
4. **C.4 — Delete.** `git rm lua/tyo3/actions.lua`; strip the ACTIONS pane +
   `actions` require from `panel.lua` (keep IDENTITY/CONTEXT/DOCS/AFFECTED).
5. **C.5 — Thin ex-command wrappers.** Keep `:TyO3Note`/`:TyO3Doc`/`:TyO3Move`
   sharing **one** implementation with the client commands (no duplicate logic).

## Critical facts & decisions (the two big ones are DECIDED — see GUIDE §3)

- **DECIDED — gate Explain/Simplify on the declared `explain` layer, via
  `ctx.layers`.** Layers are never implicit (`config.toml [layers.*]` ∪ `tyo3.extend`
  registrations); `session.author` does NOT validate that a layer is declared, so
  offering Explain where `explain` isn't declared writes an **orphaned, review-less
  record** — a latent bug. The deleted `actions.lua` already gated this; restore it.
  Source the gate from a `layers` fetch in the codeAction handler exposed as
  `ctx.layers` (fetch only when an entity resolved) — **not** a cache. The same
  `ctx.layers` drives the per-layer Author actions. The shop project declares
  `[layers.explain]`, so Explain still appears in `lsp_codeaction`. No `{"intent"}`
  Author fallback — offer Author only for layers `ctx.layers` reports as writable
  (`origin=="authored"` minus `docs`/`explain`). Full rationale: GUIDE §3.1–3.2.
- **DECIDED — rewrite `lsp_codeaction.lua` to assert presence, never totals.**
  Replace `#actions == 2` / `== 4` with command/kind/`author_layers` set assertions:
  `tyo3.explain` present, a Simplify (`data.kind=="simplify"`/`refactor.rewrite`),
  `tyo3.doc`, `tyo3.move`; `author_layers == {"intent"}` (the one line that proves
  the filtering — explain/docs excluded from Author, derived excluded); `kinds ⊇
  {source.tyo3, refactor.rewrite, refactor.move}`; off-entity `{}`. End-to-end: call
  the factored core `M.author_note(...)` directly (no faking the snacks prompt) and
  poll `authored`. Test **providers**, not picker chrome. Detail: GUIDE §3.3.
- **AI stays optional** — no UI dep (snacks/tiny-code-action) may gate
  explain/simplify; the LLM backend degradation and Simplify parse-guard are
  unchanged. (The layer gate above is orthogonal — durable-sink existence, not the
  model.)
- **Keep the registry contract** — add your built-ins through the same
  `register_code_action` / `register_command` calls third-party plugins use; don't
  special-case them in the handler (lsp_codeaction asserts the seam still works).
- **The full curated stack IS installed locally** via the user's `vim.pack`
  checkout (`~/.local/share/nvim/site/pack/core/opt/` — snacks, **tiny-code-action**,
  edgy, treewalker, treesitter, at proj-28's target versions). Phase B added a
  shared resolver `editors/tyo3.nvim/demo/pack.lua` (`pack.add{ "tiny-code-action.nvim",
  "snacks.nvim" }` — plugins only, NOT the user's config). So the **primary
  verification is headless `lsp_codeaction.lua`** (no UI dep), AND a
  `demo/codeaction/` GIF of the **real** buffer picker is feasible now (model it on
  `demo/picker/`). Only *CI-hermetic* provisioning is still Phase F. Detail: GUIDE §8.

## Definition of done

See GUIDE §4. In short: the entity menu offers Author `<layer>`/doc/move as client
commands; caps/kinds extended; a buffer-local keymap opens the tiny-code-action
picker; `actions.lua` deleted and `panel.lua` no longer references it or renders an
ACTIONS pane (IDENTITY/CONTEXT/DOCS/AFFECTED stay); `:TyO3Note`/`:TyO3Doc`/
`:TyO3Move` share one implementation; the dep-light sweep stays green and
`lsp_codeaction.lua` is updated + passing (incl. the `tyo3.author` round-trip).

## Verify

```
PRISTINE=$(echo /nix/store/*neovim-unwrapped-0.12.2/bin/nvim | tr ' ' '\n' | head -1)
GRAMMARS=$(for d in /nix/store/*nvim-treesitter-grammars; do [ -e "$d/parser/python.so" ] && echo "$d" && break; done)
for t in smoke context lsp lsp_nav lsp_codeaction review_dedup lsp_symbols; do
  devenv shell -- timeout 240 "$PRISTINE" --headless --clean --cmd "set rtp^=$GRAMMARS" \
    -u editors/tyo3.nvim/tests/minimal_init.lua \
    -c "luafile editors/tyo3.nvim/tests/$t.lua" 2>&1 | grep -E "checks, [0-9]+ failed|^\[FAIL\]"
done
# Daemon sanity (unchanged by C): devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov
```

## When you finish

Report what changed (files added/deleted/modified), what you ran, and pass/fail
output. Commit on the branch with a clear message (e.g. `feat(nvim): unify entity
actions into the code-action registry (tiny-code-action hub)`). Then stop and
report — **do not start Phase D**. The explain-layer gate, the test restructure,
and the demo approach are all decided (GUIDE §3.1–3.3, §8 — the curated stack is
installed locally via `vim.pack`, resolved by `demo/pack.lua`). The only thing
worth flagging mid-build is an unexpected **tiny-code-action API shape** vs the
installed version. Pause and ask on that rather than guessing.
