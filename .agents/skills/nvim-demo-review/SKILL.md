---
name: nvim-demo-review
description: "Review a tyo3.nvim demo recording by reading its .txt transcript frame-by-frame. Use when verifying a re-rendered demo, checking a recording landed correctly, diagnosing a broken/racey/half-typed GIF, confirming a scene shows the right panel/virtual-text/diagnostic/quickfix content, or reviewing a demo change in a diff without opening the GIF. Covers the editors/tyo3.nvim/demo/*/*.txt frame format and what to look for per scene."
---

# Reviewing tyo3.nvim demo recordings (the `.txt` transcript)

Every vhs render of a tyo3.nvim demo emits a `.txt` alongside the `.gif` — a
**frame-by-frame plain-text transcript** of the same recording. It is the review
surface: you can read panel contents, virtual-text notes, diagnostics, quickfix
lists and floats as text, diff them, and grep them — without watching the GIF or
needing a display. **Prefer the `.txt` over the `.gif` for verification.**

The companion skill **`nvim-demo-record`** covers authoring the tapes that produce
these. This skill is purely about *reading and judging* the output.

The files:

- `editors/tyo3.nvim/demo/default/tour.txt` — the headline tour.
- `editors/tyo3.nvim/demo/context/context.txt` — the cursor-CONTEXT sidebar tour.

## The frame format

vhs writes the terminal screen as a sequence of **frames**, each a full snapshot
of the visible grid, separated by an **80-character `─` rule** line:

```
────────────────────────────────────────────────────────────────────────────────
<full terminal screen: every visible row, including the split, panel, and floats>
<the command line at the very bottom — often the :command just typed>
────────────────────────────────────────────────────────────────────────────────
<next frame …>
```

Mechanics worth knowing:

- The **first block** (before the first rule) is the bare shell prompt (`>`).
- Each frame is one captured screen state; vhs emits roughly one per scripted step,
  so `tour.txt` has ~168 frames and `context.txt` ~123. Reading top-to-bottom walks
  the demo in order.
- The **bottom line of a frame is usually the command being typed** (`:TyO3Affected`,
  `:lua vim.diagnostic.open_float()`, `/def checkout`). That tells you which scene
  step produced the frame after it.
- Frames are the rendered TUI: `~` are empty buffer lines, `│` is the vertical
  window split (code on the left, the tyo3 panel on the right), and the
  second-to-last line is the **statusline** (`  TyO3 ● live  …  store.py  9:12`).
- The transcript is **wrapped to the terminal width**, so long panel/diagnostic
  text wraps mid-word across lines (e.g. `narrowe` / `d {}`). Read wrapped lines
  together; don't treat a wrap as a missing word.

### How to navigate it

- **Grep for the marker of a scene**, then read the surrounding frame: the panel
  header `▾ AFFECTED`, a virtual-text note glyph (`🏷`/`⟢`), `Diagnostics:`, a
  quickfix path, the inspector float, an `Explain` paragraph (`🤖`).
- To pull a single frame programmatically, split on rule lines
  (`set(line.strip()) == {"─"}`) and print the block between two rules.
- The sign-column gutter prefixes the line number: `E` / `W` mark error/warning
  diagnostic signs (e.g. `E   9     return Product().price()`).

## Worked example — the Scene-12 headline frame

```
    1 from book import Book                               │  ▾ AFFECTED  (5)
    2 from catalog import Product                         │  rev 8 · Δ0 · affects {checkout}  →
    …
        🏷  keep this label stable                         │  rev 26 · Δ0 · affects {Item, show_…
W   8 def show_label() -> str:   ⟢ summary<>              │  rev 27 · Δ1 · affects {show_label}
E   9     return Product().price()                        │  …
~                Diagnostics:
~                1. Return type does not match returned value [invalid-
~                return-type]
~                2. needs review (intent changed)
  TyO3 ● live                              store.py  9:12    TyO3 ● live             TyO3://pan…
:lua vim.diagnostic.open_float()
```

Everything the scene promises is verifiable here: the `Item`→`Product` rename
landed (`from catalog import Product`, `Product().price()`); the durable note
`🏷 keep this label stable` is still glued to `show_label`; the `W`/`E` gutter and
the diagnostic float show **both** kinds on the same stream — the type error
(`invalid-return-type`) *and* `needs review (intent changed)`; and the AFFECTED
panel logged the edit. If any of those were missing or the float showed a Lua
error, the render is broken.

## A review checklist

When verifying a re-record, walk the `.txt` and confirm:

1. **No startup traceback.** Early frames must show nvim opening `store.py`, not an
   `E5113`/Lua stack trace. A traceback means the pristine-nvim resolution failed
   (`$TYO3_NVIM`) — see `nvim-demo-record`. This is the single most common breakage.
2. **The daemon came up.** The statusline reads `TyO3 ● live` (or `● context`) and
   entities are **decorated** — `⟢ summary<…>` virtual text on defs, the panel
   populated. A dead daemon leaves bare code and an empty panel.
3. **No half-typed / mis-landed commands.** Each `:command` at a frame bottom should
   be complete and the *next* frame should show its effect. A `ciw` that landed on
   the wrong token (e.g. clobbered `return` instead of the literal) shows up as
   corrupted code in the following frames.
4. **Every scene's payload is present** (see the scene map below): the inspector
   float, the note riding to `checkout.py`, the affected set lighting up **and
   narrowing**, hover/goto/references/typehierarchy/rename results, the AI Explain
   paragraph, and the dual `]d` diagnostics.
5. **Async beats resolved, not raced.** The narrowed affected set, pushed
   diagnostics, and typehierarchy quickfix must actually appear in their frames. If
   a panel shows the wide set but never the narrowed one, a `Sleep` was too short.
6. **Quickfix queries ran against code, not the quickfix window.** Typehierarchy /
   references frames should list real entries, not be empty (the symptom of running
   the LSP query with focus in the quickfix window).

## Reviewing a demo change from a diff

Because the `.txt` is committed, a tape/scene change shows up as a transcript diff.
Useful judgement calls:

- A **window-size / theme / typing-speed** change (`Set …`) re-flows everything →
  the whole `.txt` churns. That's expected; don't read it line-by-line, just spot-
  check a few key frames render cleanly.
- A **scene edit** should produce a *localized* diff around that scene's frames.
  Churn far from the edited scene hints at a timing shift that pushed every later
  frame — re-check the sleeps.
- For `default/`, confirm the change is **mirrored in `record_cast.py`** (the
  `.cast` is driven separately); a `.tape`/`.cast` divergence won't show in the
  `.txt` but will desync the asciinema cast from the GIF.

## Scene map (what to look for, in order)

**`tour.txt`** — Scenes 1–6 (bespoke UI): open + ambient decorate → `:TyO3Inspect`
float (DurableId/kind/summary) → `:TyO3Note` virtual text on `checkout` → `:TyO3Move`
and the note **riding to `checkout.py`** → edit `Item.price` and the AFFECTED panel
lighting up then narrowing → `:TyO3Affected` picker list. Scenes 7–11 (native LSP
bridge after `:TyO3Lsp`): hover `K` type popup, goto-def jump + `<C-o>` back,
`grr` references quickfix, type-hierarchy super/subtypes quickfix, cross-file
`Item`→`Product` rename visible in `store.py`, and the AI **Explain** action's `🤖`
paragraph. Scene 12 (headline): one edit drifts the note (`needs review`) **and**
breaks the return type, both walked by `]d` in the diagnostic float (the worked
example above).

**`context.txt`** — the data-type-separated sidebar on throughout: IDENTITY /
NOTES / DOCS / SUMMARY / ACTIONS panes + an AFFECTED log, each tracking the entity
under the cursor (`context = "cursor"`). Confirm: the note authored into NOTES, the
AI Explain run from the ACTIONS pane landing as a `🤖` paragraph in NOTES, a
markdown doc written via `:TyO3Doc` into DOCS, and notes + doc + AI summary all
surviving an edit and an atomic move. Panes are collapsible (Tab) — frames should
show them open/close.

## References

- `nvim-demo-record` — authoring the tapes; the determinism rules and the
  pristine-nvim gotcha that explain *why* a transcript breaks.
- `editors/tyo3.nvim/demo/*/`*.tape` — the scene comments map 1:1 to frames and
  explain each sleep/landing; read them alongside the `.txt`.
