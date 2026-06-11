# tyo3.nvim — Opinionated UI & AST-native interaction (proj 28)

**Date:** 2026-06-11
**Companion:** [`PLAN.md`](PLAN.md) — the phased, file-level build guide.
**Predecessor:** proj 27 (presentability + code-action UX) shipped in v0.4.0; this
builds on the registry, resolvable Simplify, and bridge it landed.

---

## 0. The reframe

tyo3.nvim today is an **additive toolkit**: opt-in LSP bridge (default off), a
bespoke UI that works standalone, telescope-or-`vim.ui.select` pickers, graceful
degradation everywhere. Its pitch was "rides whatever you already have."

proj 28 makes it an **opinionated distribution**: one curated stack, one path
through every interaction, no UI fallbacks. The bet: a cohesive
*navigate → observe → act* loop that makes "you are interacting with the durable
code spine" land immediately is worth more than drop-in flexibility.

This is as much **deletion** as addition. The codebase should get smaller.

---

## 1. Locked decisions (from the kickoff)

1. **TyO3 owns the dependency setup** (opt-out via `opts.manage = false`). On
   `setup()`, TyO3 calls `snacks`/`tiny-code-action`/`edgy` setup, registers edgy
   views, configures treesitter-textobjects + treewalker, and binds
   buffer-local, project-scoped keymaps. One escape hatch, otherwise batteries
   included.
2. **"No degradation" = UI / interaction paths only.** Exactly one way to pick,
   to act, to view the sidebar — no `vim.ui.select` fallback, no `lsp = false`
   standalone path. **AI stays optional**: the `explain`/`simplify` actions only
   appear when the project declares the layer, and Simplify keeps its graceful
   parse-guard → prose-float fallback (a real model is still optional). LLM
   backend degradation (anthropic → callable → stub) is unchanged.
3. **snacks.nvim is the picker/UI ecosystem**, replacing telescope. `Snacks.picker`
   for TyO3's own pickers; `Snacks.input` for prompts; snacks' `vim.ui` overrides
   so the whole editor's select/input route through snacks. (folke stack:
   snacks + edgy is cohesive.)
4. **Default action surface = tiny-code-action buffer picker** (hotkey-driven).
   Navigation stays native (`gd`/`grr`/`K`/`]d`); the picker is for *acting*.
5. **AST-native navigation by default**: treesitter-textobjects (select
   function/class/param) + treewalker.nvim (parent/child/sibling motion), bound
   by default, buffer-local.
6. **Sidebar via edgy.nvim**: vertically stacked views, one category each,
   accordion-expanding the focused section.
7. **Start by writing this plan** (done); build in the phase order below.

---

## 2. The target interaction model

The unifying loop — and the reason the durable-identity story finally *shows*:

- **Navigate** — treewalker moves you through the *syntax tree* (parent/child/
  sibling); textobjects select nodes (`af`/`ic`/`ia`). Native LSP (`gd`/`grr`/`K`/
  `]d`) for semantic jumps.
- **Observe** — the edgy accordion sidebar resolves the *enclosing durable
  entity* (via `id_for`, already done) and shows what's glued to it: IDENTITY,
  NOTES, DOCS, SUMMARY, AFFECTED/REVIEW. The focused section expands.
- **Act** — one hotkey opens tiny-code-action's buffer picker: every action on
  the entity (author note, write doc, explain, simplify-with-diff, acknowledge
  review, move, run any registered generator) with a preview pane.

**Key framing:** treewalker navigates *syntax*; TyO3 names the *semantics*. Don't
snap motions to entities — let motion be raw AST and let the sidebar do semantic
resolution. "Syntax under the cursor, durable identity in the dock" is the pitch.
TyO3 entities ≈ treesitter function/class nodes, so AST motion ≈ entity motion
for free.

**Clean separation:** the sidebar is *observe* (read-only knowledge surface);
tiny-code-action is *act* (transactional). This lets us delete the panel ACTIONS
pane entirely.

---

## 3. The dependency stack (hard deps, nvim 0.12)

| Dep | Role | Replaces / why |
|---|---|---|
| nvim ≥ 0.12 | in-process LSP bridge contract | bridge is now always-on |
| nvim-treesitter (+ textobjects) | AST selections | new |
| treewalker.nvim | AST motion (parent/child/sibling) | new |
| tiny-code-action.nvim | the action surface (buffer picker, hotkeys) | replaces panel ACTIONS pane + scattered ex-commands |
| snacks.nvim | picker + input + `vim.ui` overrides | **replaces telescope + `vim.ui.select` fallback** |
| edgy.nvim | sidebar window/layout management | replaces hand-rolled `panel.lua` windows |

The LLM is **not** a hard dep (AI optional). Overseer stays opt-in.

---

## 4. What gets deleted (single-path consequences)

- `telescope.lua` → rewritten as snacks-backed pickers (`Snacks.picker`); the
  `has_telescope`/`select_fallback`/`vim.ui.select` fallback is gone.
- `lsp.lua`: the `lsp = false` branch, `M.toggle`, `:TyO3Lsp` (bridge always on).
- `actions.lua` (the ACTIONS-pane catalog) — folded into the LSP code-action
  registry (one source of "act on the entity"), surfaced via tiny-code-action.
- `panel.lua` hand-rolled window management → edgy views; the ACTIONS pane.
- `inspect.lua` float → the edgy IDENTITY section.
- `config.lua`: `lsp`, `auto_start`, `layer_diagnostics`, `panel`, `context`
  flags (now always-on); keep display prefs (`virtual_text`), tunables
  (`debounce_ms`, `request_timeout_ms`, …), and add `manage`, `keymaps`.
- The `auto_start = false` autocmd asymmetry (REVIEW Part D).

Net: fewer code paths, fewer flags, one renderer per concern.

---

## 5. Risks / things to verify early (spikes)

1. **edgy accordion is not strictly native.** edgy gives stacked, sized,
   titled views; "expand focused / collapse others to title-height" likely needs
   a small `WinEnter` resize hook over edgy's size API. *Spike this before
   committing the sidebar design (Phase E).*
2. **nvim-treesitter `main` vs `master` API split.** The textobjects config
   surface differs across the rewrite. Pin a version and the matching config
   shape (Phase D).
3. **Headless test infra.** The current Lua specs are deliberately
   dependency-light; hard deps on snacks/edgy/tiny-code-action/treesitter/
   treewalker mean `minimal_init.lua` must provision them. Keep the engine/bridge
   specs (smoke/lsp/lsp_nav/lsp_codeaction/review_dedup) dep-light where possible;
   gate UI specs on the stack. This is real infra cost (Phase F).
4. **tiny-code-action preview for non-edit actions.** Only resolvable edits
   (Simplify, via P6 `codeAction/resolve`) get a diff; note/doc/explain show the
   command/title. Decide whether to enrich the preview via `codeAction/resolve`
   for informational actions, or accept title-only (Phase C).
5. **Loss of drop-in friendliness** — accepted. The install story (a `lazy.nvim`
   dependency spec) becomes part of the deliverable.
6. **callHierarchy capability-key quirk.** Type hierarchy needed literal nvim
   0.12 capability keys; verify whether `callHierarchy/incomingCalls` etc. need
   the same (Phase A).

---

## 6. Phase map (see PLAN.md for file-level detail)

| Phase | Theme | Risk | Engine? |
|---|---|---|---|
| **A** | Tier 1 LSP bridges (documentSymbol, workspace/symbol, callHierarchy) | low | yes (small verbs) |
| **B** | Single-path conversion + dep adoption (snacks, always-on bridge, delete fallbacks) | medium (deletion) | no |
| **C** | Action hub: actions.lua → code-action registry, tiny-code-action buffer picker | medium | no |
| **D** | AST navigation: textobjects + treewalker, default keymaps | low | no |
| **E** | edgy accordion sidebar (replaces panel.lua) | high (UI refactor) | no |
| **F** | tests + dep CI + hero re-record + README + release | medium | no |

A is pure-additive (do first / independently shippable). B unblocks the rest by
slimming. C/D/E are the experience. F validates and ships.
