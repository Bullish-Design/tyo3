# Kickoff prompt — tyo3.nvim presentability + code-action work (proj 27)

Paste everything below the line into a fresh session to begin implementation.

---

You are implementing a planned body of work on the **tyo3.nvim** Neovim integration.
The full review and a phase-by-phase implementation guide already exist — read them
first, then execute. Do **not** re-derive the plan; follow it.

## Read these first (in order)
1. `.scratch/projects/27-nvim-presentability-review/REVIEW.md` — the findings and the
   "why" behind each change (demo presentability + code-action UX).
2. `.scratch/projects/27-nvim-presentability-review/IMPLEMENTATION.md` — the concrete,
   ordered, 7-phase guide with drop-in code, file paths, tests, and verify commands.
   **This is your spec.**

## Goal
Make the Neovim integration demo-ready and make the code-action popup (esp.
tiny-code-action) genuinely good. The headline outcomes: a stalled RPC can't hang the
UI; the `gra`/tiny-code-action menu stops offering blind actions and gains a
review-acknowledge quickfix; a short hero demo loop; a real LLM call can't freeze the
editor; and (stretch) "Simplify" shows a real diff preview.

## Working rules (this repo)
- **Always run project commands through devenv** — never raw pytest/cargo/nvim:
  - Build: `devenv shell -- build`
  - Tests: `devenv shell -- test-fast` (parallel, no-cov)
  - Daemon pytest: `devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov`
  - Headless Lua specs:
    ```
    devenv shell -- nvim --headless --clean \
      -u editors/tyo3.nvim/tests/minimal_init.lua \
      -c "luafile editors/tyo3.nvim/tests/<spec>.lua"
    ```
    Specs: `smoke`, `context`, `lsp`, `lsp_nav`, `lsp_codeaction`, `review_dedup`.
  - Demos: `devenv shell -- demo-record` / `demo-record-context` (and the new
    `demo-record-hero` you'll add in Phase 3).
- Test suites are slow — give them ~15 min and prefer running full suites in the
  background.
- **No AI attribution** anywhere — no "Co-Authored-By", no "Generated with" trailers in
  commits, PRs, or comments. Match the surrounding code's comment density and idiom.
- Work on a branch, not `main`. Commit per phase with a clear message. Don't push or open
  PRs unless asked.

## Execute in this order
Implement **Phase 1 and Phase 2 first** (low-risk, pure-Lua, immediately improves both
stability and the popup), verify them green, commit, then continue 3 → 7. Each phase in
IMPLEMENTATION.md is independently shippable; do not batch them into one giant change.

- **Phase 1 — rpc timeout** (`config.lua`, `rpc.lua`, `daemon.lua`): arm/disarm a per-id
  timer; cancel all timers on close.
- **Phase 2 — code-action UX** (`lsp.lua` only): async entity-gated handler, named
  titles, split kinds + `codeActionKinds`, the `tyo3.ack` quickfix + command, updated
  `executeCommandProvider`. Extend `tests/lsp_codeaction.lua`.
- **Phase 3 — hero demo** (`demo/hero/`): new tape+init, devenv script, README hero swap.
- **Phase 4 — cleanups + README** (`card.lua`, `init.lua`, `context.lua`, `plugin/tyo3.lua`,
  README): delete dead `context_lines`, buffer-wipeout cleanup, doc accuracy.
- **Phase 5 — responsive `explain`** (`server.py`, `handlers.py`, `lsp.lua`): see the
  critical note below.
- **Phase 6 — resolvable Simplify** (engine verb + `codeAction/resolve`): stretch.
- **Phase 7 — polish**: push-diag debounce/cancel, refinement dedup, telescope precise jump.

## Critical facts you must not get wrong
- **Phase 5 needs TWO coordinated changes, not one.** The editor uses a *single* daemon
  connection whose reader thread (`server.py::_serve_client`) dispatches requests
  **serially**. Moving the LLM off the session actor alone is NOT enough — the connection
  still blocks. You must *also* offload dispatch to a bounded `ThreadPoolExecutor` so one
  slow request doesn't serialize the others. Both changes together; see Phase 5 in the
  guide for the exact diffs and safety notes (`client.send` is `_send_lock`-guarded;
  the actor stays the serialization point; id-matched out-of-order replies are fine).
- **Phase 2 must stay backward-compatible with the existing test.** At a clean on-entity
  position (`checkout`), the menu must still return exactly 2 built-in actions (then 4
  with the two registered providers in the test). Entity-gating is *additive*: only the
  built-in explain/simplify provider and `register_entity_action` gate on `ctx.entity`;
  the raw `register_code_action` seam stays ungated. The ack provider yields 0 unless the
  entity is `needs_review`.
- **Position conventions** (already handled in `lsp.lua`, don't regress): daemon positions
  are 1-based with an *exclusive* end column; LSP is 0-based — the conversion is a uniform
  "subtract 1 from every line and column". `positionEncoding = "utf-32"`.
- **vim.NIL is truthy.** A JSON `null` result decodes to `vim.NIL`; `daemon_request`
  already normalizes it to `nil`. Keep that normalization when you touch that path.
- The `tyo3.ack` command clears `needs_review` by **re-authoring each flagged layer's
  current value** (re-read fresh via `authored`, then `author`) — re-authoring is the
  engine's acknowledge (it re-stamps the reviewed body hash). Verbs already exist; no
  engine change needed for ack.

## Definition of done (per phase)
- The phase's code change matches the guide (adapt names to fit existing idiom, but keep
  the behavior).
- New/extended tests for that phase pass via the devenv commands above.
- No regression in the existing specs (`smoke`, `context`, `lsp`, `lsp_nav`,
  `lsp_codeaction`) or `daemon/tests`.
- For Phase 3/6, the relevant demo renders and looks right.

## When you finish a phase
Report: what changed (files), what you ran to verify, and the pass/fail output. Then
proceed to the next phase unless I say otherwise. If you hit a "decisions to confirm"
item from the end of IMPLEMENTATION.md (timeout default, pool vs. fully-async, rewrite
parse-guard), pause and ask rather than guessing.

Start by reading the two docs, creating a working branch, running `devenv shell -- build`
to confirm the baseline is green, then implement Phase 1.
