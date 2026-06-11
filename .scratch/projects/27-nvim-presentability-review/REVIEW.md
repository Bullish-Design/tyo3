# tyo3.nvim — Presentability Review & Code-Action UX Plan

**Date:** 2026-06-10
**Scope:** `editors/tyo3.nvim/` (plugin, demos, docs) + the daemon surface it drives
(`src/tyo3/daemon/`). Lens: make the Neovim integration **demo-ready** so recorded
demos advertise it well, and make the **tiny-code-action** popup interface genuinely good.
**Method:** full read of every Lua module, both demo tapes + harness, the README/docs,
and `daemon/handlers.py` + `daemon/bus_pump.py` to validate the wire contract.

---

## 0. Verdict

This is a **well-engineered plugin** — clean layering (`rpc` → `daemon` → `init` →
feature modules), one-daemon-per-root with a shared client, idempotent LSP attach,
careful position-encoding reasoning, and unusually disciplined demo determinism
(pristine-nvim resolution, offline LLM seam). The code is not the problem.

The gap to "advertisement-grade demos" is **not** correctness — it's **presentation
packaging** plus a small set of **"looks great offline, janky the moment a real user
touches it"** seams. This document leads with the demo findings, then the code findings
that bite *during* a recording or a viewer's first install, then a concrete plan to make
the code-action popup (esp. tiny-code-action) good.

---

## Part A — Presentation / demo findings (the core ask)

### A1 — The hero GIF is a 3-minute feature tour, not an advertisement ⭐ highest-leverage

`demo/default/tour.tape` has **~122s of explicit `Sleep`** + typing at 55ms/char across
**12 scenes** → rendered `tour.gif` is **1.6 MB** and runs well over 2.5 minutes. The
context demo is **1.9 MB / ~83s** of sleeps.

For an ad this is too long — autoplay in a README means the viewer sees ~10s and scrolls.
You're showing *everything* (bespoke UI + hover + goto + references + type-hierarchy +
rename + AI code action + durable review), which is a great **manual** but buries the lede.

**Recommendation:** keep `tour.tape` as `full-tour`, add a **30–45s hero loop** showing one
thing — the money shot you already name in the README: author a note, `:TyO3Move`, the note
rides to the new file — then one LSP "wow" (Scene-12's `]d` walking a `needs_review` drift
**and** a type error in the same stream is genuinely novel; lead with that). Short, looping,
< 800 KB.

### A2 — The richest visual isn't in the hero image

The README hero is `demo/default/tour.gif`, which runs with `context = "off"`. The panel's
headline feature — the **data-type-separated sidebar** (IDENTITY / NOTES / DOCS / SUMMARY /
ACTIONS) — only renders when `M._entity` is set, and `set_context` is *only* called from
`context.lua` (cursor tracking), which is **off** in the default tour. So the hero GIF's
right dock shows only the `AFFECTED` log. The most impressive composition lives in the
*second*, non-hero `context.gif`.

**Recommendation:** the hero should feature the rich sidebar. Promote a context-style
recording to hero, or split-screen the sidebar in the short loop.

### A3 — Pacing is conservative to a fault

Determinism via generous sleeps is the right instinct, but 55ms typing + many 2.5–6s holds
reads as sluggish. Several waits (the 6s after `:w` in Scene 12; the dual 4.5s typehierarchy
holds) are sized for worst-case cold async. Shave 30–40% by (a) **pre-warming** the daemon
before the recorded portion (open + an off-camera `reindex` so the first on-camera sync
isn't cold) and (b) gating on observable state instead of fixed sleeps where vhs allows.

---

## Part B — Code findings that affect a demo or a first install

### B1 — `explain` runs the LLM **synchronously inside the single session actor** ⭐ biggest "janky in reality" gap

`handlers.explain` does `actor.submit(work)` where `work` calls `_llm(...)` inline
(`handlers.py:617-635`). The actor is the *one* thread that owns the session — every
`decorate`, `entity_at`, `sync_buffer`, `check` for that project queues behind it. With the
demo's offline `callable` stub this is instant, so it looks perfect. But the README and
`setup.sh` both *invite* flipping `TYO3_LLM=anthropic`, and the moment a user does, a
multi-second network call **freezes the entire tyo3 surface** for that project (typing → no
decorations, cursor → no context, save → no diagnostics) until the API returns.

This is the single biggest "the demo lied" risk. Run the LLM off-actor (gather context on
the actor, call the model outside the lock, author the result back on a second hop), or
document loudly that `explain` is synchronous and not production-pathed. At minimum surface
a "thinking…" state so a 4s freeze isn't silent.

### B2 — No per-request timeout in `rpc.lua` ⭐ demo-stability

`Client:request` (`rpc.lua:115`) registers the callback in `self.pending` and never times
it out. Callbacks resolve only on a matching response or connection close. A cold first
`sync_all` can take up to 60s (daemon.lua sizes its connect poll for it), and a stalled
`check`/`explain` (see B1) leaves the feature callback **pending forever** — the UI just
never updates, with no error and no feedback. In a live recording a single stalled request
is unrecoverable dead air. Add a per-request timer that fires
`cb({code=-32099, message="request timed out"})` and drops the pending id.

### B3 — Dead code + a lying comment in `card.lua`

`card.lua`'s header claims *"Two consumers render it: the `:TyO3Inspect` float (full) and
the panel's CONTEXT section (compact)."* That's false. `M.context_lines`
(`card.lua:84-144`, ~60 lines) is **never called** — grep confirms only `build_lines` is
used (by `inspect.lua`). The panel reimplements its own `identity_rows`/`note_rows`/
`summary_rows`. Two divergent card renderers, one unused, and the comment will mislead the
next person who "fixes the panel" by editing `card.lua` and sees no effect. Delete
`context_lines` (and fix the header), or have the panel call it. Removal is cleaner.

### B4 — Buffer-keyed state is never reclaimed

`init.lua` keeps `_root_by_buf`, `_last_synced`, `_debounce`; `context.lua` keeps
`_last_key`, `_debounce`; `decorate.lua` keeps `name_cache`. There is **no `BufDelete`/
`BufWipeout` autocmd** anywhere. Two consequences: (1) a slow leak over long sessions, and
(2) Neovim **reuses bufnrs** after wipeout, so a cached `_root_by_buf[bufnr] = false`
(non-project) can mis-classify a freshly-opened project file under the recycled bufnr. Low
severity, but the kind of thing that bites exactly once on stage. Add a cleanup autocmd
that nils these by bufnr.

### B5 — `publish_diagnostics` has no coalescing/cancellation

On each `delta`, `lsp.publish_diagnostics` runs `check` (the ty checker, ~hundreds of ms)
per touched file with no debounce and no cancellation of in-flight checks (README's
"follow-ups" admits this). A rapid save/edit burst stacks overlapping checks on the actor,
compounding B1's contention. Fine for the scripted demo; visibly laggy under real typing.
Debounce + cancel-previous.

---

## Part C — Documentation accuracy (read alongside the demo, so it matters)

- **Neovim version floor is wrong for the bridge.** README says "Neovim ≥ 0.10 (`vim.uv`)",
  but the LSP bridge is built and verified against **nvim 0.12** semantics: the in-process
  `cmd = function` PublicClient contract, the `typeHierarchy/supertypes` capability-key
  quirk (`lsp.lua:34-39`), client-command resolution via `vim.lsp.commands` +
  `client:exec_cmd`, and `vim.lsp.start` `{name,root_dir}` dedupe. The *bespoke* UI works on
  0.10; the *bridge* does not. Split the requirement.
- **Protocol tables are incomplete.** "Notifications" omits `derived` (the plugin handles
  it; `bus_pump.py:159` emits it). "Requests" omits `layers`, `layer_ids`, `diagnostics_at`,
  `context_pack`, `explain`, and `subscribe` — all real, dispatched verbs. Since
  `:checkhealth` advertises the full method list, a curious viewer notices the discrepancy.

---

## Part D — Smaller stuff (note, don't block)

- **`panel.on_refinement`** appends `→ narrowed {…}` to the revision's line every time a
  refinement for that revision arrives; multiple refinements concatenate. Cosmetic, possible
  during Scene-5 narrow.
- **Telescope `jump_to`** (`telescope.lua:31-35`) degrades by-identity navigation to a
  `vim.fn.search` name regex after `locate`, whereas the ACTIONS `goto_def` path jumps
  precisely off the card's range. Inconsistent precision; the picker can land on the wrong
  same-named symbol.
- **`docs.index()`** uses `vim.ui.select`; the context demo drives it with literal
  `"1"`/`"3"` keystrokes, which only works with the *default* selector. A viewer with
  `dressing.nvim`/`snacks` copying the flow gets different behavior. Note it in the demo.
- **`auto_start = false` asymmetry:** `on_text_changed`/`sync_now`/`on_cursor` fire
  regardless of `auto_start` (their autocmds aren't gated on it), only `on_buf_enter` is.
  Harmless (they no-op without a client) but inconsistent with the flag's intent.

---

## Part E — What's genuinely strong (keep / lead with)

- The **opt-in, additive in-process LSP bridge** is the architectural highlight: it rides
  the user's *own* `K`/`grr`/`]d`/Trouble/pickers with zero extra config, and the bespoke UI
  is untouched when off. Better pitch than "we built a UI."
- **Position-encoding handling** (utf-32, uniform exclusive-end `-1`, selection-range
  preference for definition) is carefully reasoned and documented — normally subtly broken.
- The **`needs_review` durability story** (durable level state surviving saves/restarts; the
  per-buffer hash dedup that stops `:w` from clearing it) is novel and is your best
  LSP-adjacent differentiator. Scene 12 should be the *hero*, not 12-of-12.
- **Demo determinism discipline** (the `_resolve_nvim` exec-chain unwrapping, offline LLM,
  plugin-only init) is rare and correct.

---

## Part F — The code-action popup (tiny-code-action) interface

### F.0 — How tyo3's actions render in tiny-code-action today

tiny-code-action's selling point is the **preview pane** (it diffs the `WorkspaceEdit` an
action would apply) plus **diagnostic-grouped, icon-keyed** entries. Against that, the
current `handlers["textDocument/codeAction"]` (`lsp.lua:268-285`) under-delivers in four
specific ways:

1. **Every action is `command`-only with no `edit`** → tiny-code-action has *nothing to
   preview*. Its signature pane renders empty for every tyo3 action.
2. **Actions are offered blind.** The handler builds them synchronously from the providers
   with no daemon round-trip (the "we don't round-trip the daemon to *offer* the actions"
   comment), so `gra` on an import line / blank line / comment still lists *"tyo3: Explain
   this entity"* — which then notifies *"no entity under the cursor"* when picked. In a
   prominent popup that's a visible misfire.
3. **Flat `kind = "refactor"` on all three** → identical icon, no grouping, no kind-filter.
4. **`context.diagnostics` is received and ignored** (the test even passes
   `context = { diagnostics = {} }`). You have the richest diagnostic surface around —
   `needs_review`, `orphaned`, pushed type errors — and offer *zero* actions on any of them.
   That's exactly the interaction tiny-code-action exists to make pretty
   (`]d` → `gra` → one-key fix).

### F.1 — Resolve the entity *before* offering (and enrich the provider ctx) — pure Lua

`reply` is already a callback, so the handler can go async for free. Resolve once via the
existing `entity_at` verb, then only contribute entity actions when something is there — and
pass the resolved entity into `ctx` so providers and titles can use it:

```lua
handlers["textDocument/codeAction"] = function(root, params, reply)
  local only = params.context and params.context.only
  if only and not has_tyo3_kind(only) then return reply(nil, {}) end       -- respect kind filters
  if params.context and params.context.triggerKind == 2 then               -- automatic (lightbulb)
    return reply(nil, {})                                                   -- don't spam the actor
  end
  local start = (params.range and params.range.start) or { line = 0, character = 0 }
  local p = lsp_pos_to_daemon(start)
  daemon_request(root, "entity_at", { path = uri_to_path(params.textDocument.uri),
                                      line = p.line, col = p.col }, function(_, card)
    local ctx = { uri = ..., line = p.line, col = p.col, bufnr = ...,
                  entity = card,            -- nil ⇒ no entity here
                  diagnostics = (params.context or {}).diagnostics or {} }
    -- providers that key off ctx.entity simply return {} when it's nil
    ...
  end)
end
```

Kills finding #2 outright (no more failing actions in the menu) **and** lets every title
read ``tyo3: Explain `checkout` `` instead of a generic label — what the viewer's eye lands
on in the popup. The registry contract stays backward-compatible: old providers ignore the
new `ctx.entity`/`ctx.diagnostics` fields; the built-in explain/simplify providers start
returning `{}` when `ctx.entity == nil`.

### F.2 — Distinct kinds + advertise `codeActionKinds` — pure Lua

```lua
codeActionProvider = { codeActionKinds = { "refactor.rewrite", "quickfix", "source.tyo3" } },
```

Give Simplify `kind = "refactor.rewrite"` (diff-capable, refactor icon), Explain a
`"source.tyo3"` informational kind, and the review-ack action `"quickfix"`. tiny-code-action
icons them distinctly, and `context.only` filtering works.

### F.3 — Diagnostic-/review-gated actions — the tiny-code-action sweet spot — pure Lua

Because F.1 already resolved the entity card (which carries authored status), the handler
knows when the entity is in `needs_review` — no namespace plumbing needed. Add a provider:

```lua
-- when ctx.entity has a needs_review authored layer:
{ title = "tyo3: Acknowledge review (re-author)", kind = "quickfix", isPreferred = true,
  command = { command = "tyo3.ack", arguments = { { uri, did = ctx.entity.durable_id } } } }
```

`tyo3.ack` reads the note via `authored` and re-writes the same value via `author` —
re-authoring **is** the engine's acknowledge (per the README), so this clears the warning.
That gives the `]d` → `gra` → *"Acknowledge review"* → warning-clears loop: a fantastic
8-second clip and the best argument for the tiny-code-action integration.

> Namespace caveat: tyo3-layer diagnostics live in their own `vim.diagnostic` namespace, not
> as LSP diagnostics, so they may not land in `context.diagnostics`. Resolving review state
> from `ctx.entity` (or a direct `review_state` check) sidesteps that entirely — offer the
> ack whenever the entity is flagged, independent of what made it into `context.diagnostics`.

### F.4 — `isPreferred` + stable titles — pure Lua

Mark the most likely action `isPreferred` so tiny-code-action (and `gra`) highlight it, and
sort deterministically so popup order is stable across recordings.

### F.5 — The one engine-level change that unlocks the preview pane

Everything above makes the menu *correct and rich*, but tiny-code-action's preview only
lights up for actions carrying a **`WorkspaceEdit`**. Explain is inherently non-editing
(prose → a float), so it'll never have a diff — leave it informational.

**Simplify is the opportunity.** It currently returns text to a float. Make it
**resolvable to a rewrite edit**:

- Advertise `codeActionProvider = { resolveProvider = true, ... }`.
- Return Simplify with a `data` field and **no** edit (menu stays instant — no LLM per
  keystroke).
- Implement `codeAction/resolve`: run the daemon rewrite, return the action now carrying
  `edit` = a `WorkspaceEdit` replacing the entity body.

tiny-code-action then calls resolve on focus, shows the **before/after diff in its preview
pane**, and Enter applies it — and because it's a real edit through the sync path, the
durable note rides the change. Needs a daemon verb that returns a *replacement edit* rather
than prose (the current `simplify` mode returns only `{text}`). Highest-value follow-up, but
an engine ask, not a Lua one.

---

## Part G — Prioritized action list

| # | Item | Effort | Why now |
|---|---|---|---|
| 1 | **B2** request timeout in `rpc.lua` | S, Lua | A single stalled request = dead air in a live recording. |
| 2 | **F.1–F.4** code-action: async entity-gating, enriched ctx, distinct kinds, `tyo3.ack` quickfix, `isPreferred` | M, Lua + existing verbs | Turns the popup from "three generic refactor stubs" into an intentional, demoable surface. |
| 3 | **A1 + A2** short sidebar-forward hero loop | M, tape only | Biggest marketing ROI, no engine changes. |
| 4 | **B1** off-actor `explain` (or a visible "thinking" state) | M–L, engine | Stops the editor freezing when a real LLM is wired — the top "demo lied" risk. |
| 5 | **B3, B4, C** dead-renderer removal, buffer-state cleanup, README accuracy | S, Lua + docs | Cheap; protects credibility when code/README is read next to the GIF. |
| 6 | **F.5** resolvable Simplify → `WorkspaceEdit` preview | L, engine | Unlocks tiny-code-action's preview pane fully — the killer clip. |
| 7 | **B5, D** push-diagnostic debounce, telescope precision, refinement-line dedup | S–M | Polish. |

### Notes on the code-action work (item 2)

- All of F.1–F.4 is **pure Lua over verbs that already exist** (`entity_at`, `authored`,
  `author`, `review_state`) — individually shippable.
- Keep the existing `tests/lsp_codeaction.lua` assertions valid: entity-gating is *additive*
  for the on-entity, clean-state case (checkout has an entity and no needs_review → still
  exactly 2/4 actions). Extend the test with: "no action offered off-entity" and "ack action
  appears + clears needs_review."
- Going async doesn't break `vim.lsp.buf_request_sync` callers — `reply` still fires from the
  daemon callback within the request's timeout window.

---

## Appendix — files reviewed

Plugin: `init.lua`, `daemon.lua`, `rpc.lua`, `config.lua`, `lsp.lua`, `panel.lua`,
`actions.lua`, `decorate.lua`, `context.lua`, `card.lua`, `inspect.lua`, `move.lua`,
`notes.lua`, `entitydoc.lua`, `telescope.lua`, `diff.lua`, `docs.lua`, `overseer.lua`,
`health.lua`, `plugin/tyo3.lua`.
Demos: `demo/setup.sh`, `demo/default/{tour.tape,init.lua}`, `demo/context/{context.tape,init.lua}`.
Docs: `README.md`, `docs/user/*`, `docs/dev/*`.
Daemon (contract validation): `daemon/handlers.py`, `daemon/bus_pump.py`.
Tests (contract grounding): `tests/lsp_codeaction.lua`.
