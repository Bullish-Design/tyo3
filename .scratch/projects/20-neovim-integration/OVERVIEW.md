# Neovim integration — design overview

> **Status:** design sketch (no code yet). **Created:** 2026-06-08, on `v0.2.0`.
> **Goal:** surface TyO3's incremental engine *inside* the editor — durable
> identity, authored memory, the id-level affected closure, and derived
> artifacts — as you type, not as a batch tool.

---

## 1. The thesis

The compelling editor feature is the one no LSP gives you: **knowledge anchored to
a durable identity that survives edits**. A note (or a derived summary, or a
"last reviewed at rev N") pinned to a function *stays on that function* through a
rename, a move, and a reformat. Everything else (affected-set highlighting,
time-travel diff) is gravy on top of that anchor.

So the integration is **not** "another diagnostics source." It's a *reactive,
per-entity intelligence layer*: edit a buffer → one commit → a precise id-level
delta → update annotations and an affected-set view.

This mirrors TyO3's own architecture one level up: **Rust owns committed truth,
Python projects it** becomes **a daemon owns the session, Neovim projects it.**

---

## 2. Architecture: daemon + thin Lua client

```
  ┌─────────────┐   buffer text (debounced)     ┌──────────────────────┐
  │  Neovim     │ ───── sync_buffer ──────────▶ │  tyo3d (Python)      │
  │  (Lua       │                               │   owns TyO3Session   │
  │   plugin)   │ ◀──── delta / refinement ──── │   + overlay + bus    │
  │             │       (bus push, async)       │                      │
  └─────────────┘                               └──────────────────────┘
        │  extmarks · floats · panel · telescope         │  socket: JSON-RPC
        ▼                                                 ▼  (unix domain)
   render only                                      single source of truth
```

- **`tyo3d` — a new long-running daemon** (Python). Owns exactly one
  `TyO3Session` per project root, plus the bus subscription. TyO3 ships the
  session *in-process* today; the daemon is the new surface to build (a thin
  wrapper: socket server + the session + a bus→socket pump). Nothing in the core
  engine needs to change.
- **Transport:** a unix-domain socket per project root, **JSON-RPC 2.0**,
  newline-delimited. Requests (Neovim→daemon) get responses; the bus pushes
  **notifications** (daemon→Neovim) with no request. Neovim's `vim.system` /
  `uv.new_pipe` or a tiny Lua JSON-RPC client handles framing.
- **Why a daemon, not an in-process embed (pynvim remote plugin):** the bus is a
  *push* channel and the session must outlive any one buffer/window and be shared
  across splits; a separate process with one socket keeps the session
  authoritative and decouples plugin reloads from session state. (A pynvim remote
  plugin is a viable v0 shortcut but couples to Neovim's Python host and muddies
  the bus lifecycle — not recommended past a spike.)

### The buffer ↔ overlay sync model (the crux)
TyO3 already edits in an **in-memory overlay** (no disk writes) and commits
atomically — this maps almost perfectly onto an editor:

- On `BufEnter`/`BufReadPost`: `open {root}` (idempotent), then push the buffer's
  current text as the overlay for that path (so the session reflects unsaved
  state, not just disk).
- On `TextChanged`/`TextChangedI` → **debounce ~250–400ms** → `sync_buffer
  {path, text}`. One commit per *pause*, never per keystroke. The async
  `precision=method` refinement means the writer never blocks on the narrowing.
- On `BufWritePost`: a final `sync_buffer` (now disk and overlay agree).
- The daemon's commit returns the `CommitDelta`; the bus *also* pushes it — the
  plugin can use either, but the **bus notification is the canonical update
  signal** (it also carries the later refinement for the same revision).

> This lockstep (Neovim buffer text ⇄ session overlay) is the single most
> important and most failure-prone piece. Get it right first (§7 MVP) before any
> fancy rendering.

---

## 3. Surfaces, by Neovim primitive

| Want | Primitive | Why |
|---|---|---|
| Authored notes + derived summaries **inline on the entity** | **extmarks / virtual text** | Durable-id anchored; survives edits — *the* killer feature. Re-place on each commit from `entity_at`/`locate`. |
| On-demand "what is this entity?" | **floating window** (enriched hover) | `entity_at {path,line,col}` → DurableId, authored records, derived artifact, "last affected at rev N". Transient + focused — the one place a popup is right. |
| "what did my last edit affect?" | **side panel** (scratch buffer) or **`nvim-notify`** toast | Ambient/persistent → a dock or toast, **not** a popup (popups dismiss on cursor move and steal focus). Driven by the bus `delta`. |
| Navigate by identity | **Telescope** pickers | `entities`, `affected (since last edit)`, `authored notes` → jump via `locate{id}`. |
| Annotate | **command + keymap** | `:TyO3Note` authors an intent note on the entity under the cursor (`author{layer,id,value}`). |

**Decoration discipline:** extmarks move with text edits, but identity is the
truth. On each commit, re-resolve the visible entities (`entity_at` per
fold/region, or a `decorate {path}` batch call returning `[{range, id,
note, summary}]`) and reset the namespace's extmarks. Don't trust an extmark's
drifted position across a structural edit — re-anchor from the daemon.

---

## 4. Where overseer.nvim fits (and doesn't)

**Honest take: overseer is the wrong primitive for the live surface, the right
one for the plumbing.**

- overseer.nvim is a **task runner** — launch a command, capture output, manage
  the job, show it in a panel. TyO3's live intelligence is *reactive and
  per-entity*, the opposite of "run a thing, read its output." Forcing the
  affected-set / notes view into a task-output window fights the tool.
- **Where overseer genuinely helps (supporting actor):**
  - **Daemon lifecycle** — an overseer template/strategy to start/supervise/
    restart `tyo3d`, with its stdout/log in an overseer panel. overseer is good
    at owning a long-running process and surfacing its log.
  - **One-shot tasks** — `reindex`, `gc` (the FsStore GC from project 19),
    `check` (diagnostics) as overseer templates that call the daemon and stream
    results into task panels. These *are* tasks; overseer fits.
- **Where overseer does NOT belong:** the inline notes, the entity inspector, the
  affected-set panel, the bus-driven updates. Those are extmarks + floats + a
  dedicated panel + the socket — not task output.

So: **popups for on-demand inspection; overseer for the daemon process + batch
tasks; extmarks + a side panel + the bus for the reactive core.**

---

## 5. Protocol sketch (JSON-RPC 2.0)

**Requests (Neovim → daemon):**

| Method | Params | Returns |
|---|---|---|
| `open` | `{root}` | `{session_id, revision}` |
| `sync_buffer` | `{path, text}` | `CommitDelta` `{revision, changed_ids, affected_ids, moved, touched_files}` |
| `entity_at` | `{path, line, col}` | `{durable_id, qualified_name, kind, location, authored:[…], derived:{layer:artifact}}` or `null` |
| `decorate` | `{path}` | `[{range, durable_id, note?, summary?}]` (for batch extmark placement) |
| `author` | `{layer, durable_id, value}` | `{revision}` |
| `locate` | `{durable_id}` | `file::qualified_path` |
| `diff` | `{from_rev, to_rev?}` | entity-level diff `{changed, removed, moved}` |
| `derived` | `{layer, durable_id}` | `{status, artifact}` |
| `reindex` / `gc` / `check` | `{}` | task-style result |

**Notifications (daemon → Neovim, from the bus):**

| Method | Params |
|---|---|
| `delta` | `{revision, changed_ids, affected_ids, moved, touched_files}` |
| `refinement` | `{revision, narrowed}` (only with `precision=method`) |

The daemon subscribes to the session bus (`session.subscribe(Interest.ALL)`),
pumps each `Delta`/refinement onto the socket as a notification, and serves the
request methods against the same session.

---

## 6. Plugin layout (Lua)

```
tyo3.nvim/
  lua/tyo3/
    init.lua        -- setup(), config, autocmds (BufEnter/TextChanged/BufWritePost)
    rpc.lua         -- JSON-RPC client over the unix socket (uv pipe)
    daemon.lua      -- spawn/locate tyo3d (or hand off to overseer)
    decorate.lua    -- extmark namespace; virtual-text for notes/summaries
    inspect.lua     -- floating-window entity inspector (hover)
    panel.lua       -- affected-set side panel; consumes `delta` notifications
    notes.lua       -- :TyO3Note → author{intent}
    telescope.lua   -- pickers: entities / affected / authored
  plugin/tyo3.lua   -- commands: :TyO3Inspect :TyO3Note :TyO3Affected :TyO3Diff
```

---

## 7. MVP & phasing

- **Phase A — bridge & read (proves the socket).** `tyo3d` + JSON-RPC; `open` +
  `entity_at`; `:TyO3Inspect` floating inspector. No buffer sync yet (read disk
  state). Smallest thing that shows real TyO3 data in Neovim.
- **Phase B — reactive core (the hard part).** Buffer→overlay `sync_buffer`
  (debounced) + bus `delta` notifications → affected-set panel + re-decorate. This
  is where buffer/overlay lockstep must be solid.
- **Phase C — the payoff.** Authored notes as virtual text (`:TyO3Note`),
  derived summaries inline. The "rename the file, the note stays" demo.
- **Phase D — navigation & ops.** Telescope pickers, `:TyO3Diff` (snapshot diff
  view), overseer templates for daemon supervision + `reindex`/`gc`/`check`.

A good first milestone screenshot: **author a note on a function, `:Move` it to
another file, watch the virtual-text note ride along.** That single moment sells
the whole integration.

---

## 8. Risks & open questions

- **Buffer/overlay drift** — the #1 risk. Multi-file edits, undo, external file
  changes, and rapid typing must keep Neovim's buffer text and the session
  overlay in lockstep. Debounce + commit-on-pause + a periodic reconcile.
- **Decoration staleness** — re-anchor extmarks from the daemon on each commit;
  never trust a drifted extmark across a structural edit.
- **Perf** — one commit per pause, not per keystroke; the precision refinement is
  async so it won't stall the writer. Measure commit latency on a real repo.
- **Multi-project / multi-root** — one daemon + socket per project root; the
  plugin routes by buffer's root.
- **Process lifecycle** — who owns `tyo3d`'s lifetime (overseer vs the plugin)?
  Leaning overseer for supervision + log, plugin for autostart-on-open.
- **Packaging** — `tyo3d` ships with the Python package (a `tyo3-daemon` entry
  point, mirroring `tyo3-demo`); the Lua plugin is a separate `tyo3.nvim` repo.

---

## 9. References
- `.scratch/projects/19-post-0.2.0-backlog/OVERVIEW.md` — `gc`/`reindex` are the
  natural overseer task templates (§4).
- `src/tyo3/demo/tour.py` — the API surface the daemon would wrap is exactly what
  the tour exercises (`edit`, `subscribe`, `snapshot`, `diff`, `author`,
  `derived`, `id_for`/`locate`).
- Memory: `[[phase14-acceptance-done]]`.
