# KICKOFF — Native LSP bridge, Phase 2: navigation + live diagnostics

You are working in **TyO3** (Python + Rust spine engine) on its Neovim plugin,
`editors/tyo3.nvim`. **Phase 1** shipped an opt-in, in-process `vim.lsp` bridge
(branch `native-lsp-bridge`, **PR #13**) covering hover, references,
documentHighlight, prepareRename + rename, and **pull** diagnostics. This phase
adds the four follow-ups that were explicitly deferred:

1. **goto-definition** — `textDocument/definition` (needs a small new daemon verb).
2. **type hierarchy** — `prepareTypeHierarchy` + `supertypes` + `subtypes`
   (daemon verb already exists; the work is LSP-shape plumbing).
3. **Spine layer state as a `vim.diagnostic` namespace** — surface
   `needs_review` / `orphaned` (and, optionally, derived `computing`/`stale`) so
   `]d` / `[d` / Trouble / lualine navigate them. This is the high-value
   "go native for the bespoke half" piece. **Not** an LSP method — its own
   `vim.diagnostic` namespace, refreshed off the bus.
4. **Push diagnostics** — server→client `textDocument/publishDiagnostics` driven
   by bus `delta` notifications, so the type-checker diagnostics refresh on edit
   without the editor polling.

> **Still a prototype.** Additive and opt-in; default-off behaviour and the
> bespoke UI stay unchanged. Ship a coherent, tested slice; leave clearly-marked
> stretch items.

---

## State on entry

- **Base branch:** `native-lsp-bridge` (PR #13, **not yet merged** as of writing).
  **Branch off `native-lsp-bridge`** so you build on Phase 1's `lua/tyo3/lsp.lua`.
  If #13 has merged to `main` by the time you start, branch off `main` instead and
  confirm `lua/tyo3/lsp.lua` is present.
- **Independent in-flight work — do NOT touch:** PR #12 (`derived-notify-e2e-demo`).
- **Small additive daemon work IS expected this phase** (two new read verbs). It
  is pure-Python handler work over the already-tested session API — **no Rust, no
  `build`**. `ruff check` any touched Python.
- **nvim 0.12.2** here. The in-process server pattern is proven in Phase 1.

### What Phase 1 already gives you (reuse, don't reinvent)
`editors/tyo3.nvim/lua/tyo3/lsp.lua`:
- `M._server(root, dispatchers)` — the in-process server object. The `dispatchers`
  arg is **currently ignored** (`_dispatchers`). Phase 2 must **stash it** to push
  notifications (see Push diagnostics).
- `M.attach(bufnr, root)`, `M.toggle()`.
- Converters: `lsp_pos_to_daemon(pos)`, `daemon_range_to_lsp(r)` (uniform `-1`),
  `path_to_uri(root, p)` (absolute-or-relative), `uri_to_path(uri)`.
- `daemon_request(root, method, params, cb)` — the choke point; already normalises
  JSON `null` (`vim.NIL`) → `nil`.
- `DH_KIND`, `DIAG_SEVERITY`, `hover_markdown`, `lsp_error`.
- `handlers` table (method → `function(root, params, reply)`), `CAPS`.

`config.lua` has `lsp = false`; `init.lua`'s `on_buf_enter` calls
`require("tyo3.lsp").attach` when `config.get().lsp`. `init.lua`'s
`handle_notification(root, method, params)` routes bus `delta` / `derived` /
`refinement` — **this is your push-diagnostics + layer-diagnostics hook point**
(it already fans out to `panel.on_delta/on_derived/on_refinement` and re-decorates
buffers).

**The crucial wire facts Phase 1 learned the hard way (see memory
`native-lsp-bridge-shipped`):**
- Daemon ranges are **1-based, exclusive end column** → uniform `-1` conversion.
- **Navigation verbs return ABSOLUTE paths**; others relative → use `path_to_uri`.
- Columns are **Unicode codepoints** → server advertises `positionEncoding="utf-32"`.
- `decorate` returns the entity's **full** range (the `def` keyword at col 1), not
  the identifier — aim positions at the name token in tests.
- JSON `null` → truthy `vim.NIL`; `daemon_request` already nilifies it.

---

## Part 1 — goto-definition

### Daemon (new verb, ~12 lines in `src/tyo3/daemon/handlers.py`)
`session.goto_definition(rel, line, col) -> list[DefinitionTarget]` already exists
(`src/tyo3/session/read_ops.py:135`; `DefinitionTarget` =
`{path, range, selection_range?, symbol?, module_name?}`,
`src/tyo3/models/navigation.py:70`). Add, mirroring `references`:

```python
def definition(self, params):
    path = _require(params, "path", str); line = _require(params, "line", int)
    col = _require(params, "col", int); rel = self._relpath(path)
    def work(s):
        return {"definitions": [
            {"path": str(t.path), "range": _range_dict(t.range),
             "selection_range": _range_dict(t.selection_range) if t.selection_range else None}
            for t in s.goto_definition(rel, line, col)]}
    return self._actor.submit(work)
```
Register in `_METHODS` (`"definition": Handlers.definition`). Update the README
protocol table + the daemon's verb list. Add a handler unit test under
`src/tyo3/daemon/tests/` (mirror an existing `references`/`hover` handler test).

### LSP bridge
- `CAPS.definitionProvider = true`.
- `handlers["textDocument/definition"]`: call `definition`; return a `Location[]`
  (`{uri=path_to_uri(root, t.path), range=daemon_range_to_lsp(t.selection_range or t.range)}`).
  Prefer `selection_range` so the cursor lands on the name. Empty list when none.

This lights up `grd`/`gd`/`<C-]>` and definition pickers.

---

## Part 2 — type hierarchy

The daemon `type_hierarchy` verb already returns the **whole** thing at once:
`{item, supertypes, subtypes}` where each item is a `TypeHierarchyItem`
`{name, detail?, path, full_range, selection_range}` (`navigation.py:110`). LSP
splits this across three requests. Because the daemon is stateless and cheap,
**re-query** rather than caching:

- `CAPS.typeHierarchyProvider = true`.
- `textDocument/prepareTypeHierarchy` → call `type_hierarchy`; return
  `[ lsp_item(res.item) ]` (or `null`).
- `typeHierarchy/supertypes` → params carry `{item}`; convert
  `item.selectionRange.start` back to a daemon position (`lsp_pos_to_daemon`),
  re-call `type_hierarchy`, return `res.supertypes` as `lsp_item[]`.
- `typeHierarchy/subtypes` → same, return `res.subtypes`.

`lsp_item(t)` → LSP `TypeHierarchyItem`:
```lua
{ name = t.name, kind = 5,  -- SymbolKind.Class (type hierarchy is classes)
  detail = t.detail, uri = path_to_uri(root, t.path),
  range = daemon_range_to_lsp(t.full_range),
  selectionRange = daemon_range_to_lsp(t.selection_range) }
```
The `item` arriving in supertypes/subtypes params is the LSP item you emitted, so
its `uri`/`selectionRange` round-trip cleanly. Drives `:Telescope lsp_*` hierarchy
pickers and any type-hierarchy UI.

---

## Part 3 — spine layer state as a `vim.diagnostic` namespace

**This is NOT an LSP method.** Layer state (durable-identity concepts:
`needs_review`, `orphaned`, derived `computing`/`stale`) has no LSP vocabulary, so
publish it into a dedicated `vim.diagnostic` namespace. It then rides `]d`/`[d`,
`vim.diagnostic.setqflist`, Trouble, and lualine for free — exactly the win.

### Daemon (new read verb)
`session.needs_review() -> list[str]` and `session.orphaned() -> list[str]` exist
(`read_ops.py:485`/`:496`), returning durable ids. Add `review_state` to join
those ids with their graph node ranges (like `decorate` does its file walk):

```python
def review_state(self, params):
    path = params.get("path")
    rel = self._relpath(path) if isinstance(path, str) else None
    def work(s):
        flagged = [(i, "needs_review") for i in s.needs_review()] \
                + [(i, "orphaned") for i in s.orphaned()]
        items = []
        with s.snapshot() as snap:
            g = snap.graph()
            for did, state in flagged:
                node = _node_by_id(g, did)              # helper already in handlers.py
                if node is None: continue
                if rel is not None and node.file != rel: continue
                items.append({"durable_id": did, "name": node.name,
                              "path": node.file, "range": _range_dict(node.range),
                              "state": state})
        return {"items": items}
    return self._actor.submit(work)
```
Register + README + handler test. Note `needs_review`/`orphaned` are live-registry
reads; the ranges come from the pinned snapshot graph (golden rule #2 — one
snapshot for the join).

### Plugin (`lua/tyo3/lsp.lua` or a new `lua/tyo3/layerdiag.lua`)
- `local NS = vim.api.nvim_create_namespace("tyo3-layer")`.
- A `refresh_layer_diagnostics(bufnr, root)` that calls `review_state {path}` for
  the buffer's file and `vim.diagnostic.set(NS, bufnr, items)` where each item maps
  `range → {lnum,col,end_lnum,end_col}` (0-based; reuse the `-1` rule) with
  `severity = needs_review→WARN, orphaned→HINT`, `source="tyo3"`,
  `message` like `"needs review (intent changed)"` / `"orphaned derived artifact"`.
- **Refresh triggers:** in `init.lua` `handle_notification`, when `config.get().lsp`
  (or a new `layer_diagnostics` flag — your call, document it), on `delta`/
  `derived`/`refinement` re-run `refresh_layer_diagnostics` for every loaded buffer
  of that root (the panel already iterates them — mirror that loop). Also refresh
  once on attach.
- Keep it behind the flag; default-off must place zero diagnostics.

> Decision to make and document: reuse the `lsp` flag, or add a separate
> `layer_diagnostics = false`. Reusing `lsp` is simpler; a separate flag lets users
> take layer-state diagnostics without the full LSP bridge. Recommend a separate
> flag defaulting to the value of `lsp` if you can do it cleanly; otherwise reuse
> `lsp` and note it.

---

## Part 4 — push diagnostics (type-checker, server→client)

Phase 1 implemented **pull** (`textDocument/diagnostic`). Add **push** so check
diagnostics refresh on edit. The mechanism is the `dispatchers` table passed to
the server `cmd` function — `dispatchers.notification(method, params)` delivers a
server→client notification that Neovim routes to its built-in
`textDocument/publishDiagnostics` handler (which populates `vim.diagnostic`).

- In `M._server(root, dispatchers)`: **stash** the dispatchers, e.g.
  `M._dispatchers_by_root[root] = dispatchers`. (Phase 1 ignores it.)
- Add `M.publish_diagnostics(root, relpaths)`: for each path, `daemon_request`
  `check {path}`, map to LSP `Diagnostic[]` (reuse `DIAG_SEVERITY` + the existing
  pull mapping — factor that mapping into a shared helper), then
  `dispatchers.notification("textDocument/publishDiagnostics", {uri=path_to_uri(root,path), diagnostics=...})`.
- **Trigger:** in `init.lua` `handle_notification`, on `delta` (when `lsp`), call
  `M.publish_diagnostics(root, params.touched_files)` (the delta carries
  `touched_files` / `affected_files`). Debounce-free is fine for a prototype;
  note that `check` runs the ty type-checker and can be ~hundreds of ms — consider
  only the touched files, not the whole project.

### Pull + push coexistence (verify, document)
With both the pull `diagnosticProvider` capability AND push `publishDiagnostics`
active, the same diagnostics can render twice (different `vim.diagnostic`
mechanisms). Decide one of:
- **Recommended:** keep `diagnosticProvider` advertised (so `:lua vim.diagnostic`
  pull still works on demand) but rely on push for liveness; verify empirically
  that nvim 0.12 doesn't double-count (pull is on-demand, push is event-driven —
  in practice they don't both fire unless a plugin auto-pulls). If they collide,
  drop the pull capability when push is enabled.
Document whichever you pick.

---

## Conversion / capability recap

`CAPS` additions this phase:
```lua
definitionProvider = true,
typeHierarchyProvider = true,
-- diagnosticProvider stays (pull); push rides dispatchers.notification.
```
All ranges use the Phase-1 `daemon_range_to_lsp` (uniform `-1`). All paths use
`path_to_uri`. New daemon verbs return the same `_range_dict` shape; navigation
verbs (`definition`) will hand back **absolute** paths like `references` did.

---

## Verification (Definition of Done)

Extend the headless harness. Either grow `editors/tyo3.nvim/tests/lsp.lua` or add
`editors/tyo3.nvim/tests/lsp_nav.lua` (mirror the existing structure: build the
shop project via `tyo3.demo.tour._build_project`, `setup{ lsp = true, daemon_cmd =
{"python","-m","tyo3.daemon"} }`, open `store.py`, drive the project synced, aim at
the **identifier** not the entity-start). Assert:

- `textDocument/definition` on a **usage** (e.g. `Book` / `Item` / `usd` in
  `store.py`) returns ≥1 `Location` whose uri+range resolve to the definition.
- `textDocument/prepareTypeHierarchy` on a class (the shop project has classes in
  `catalog.py`/`book.py`) returns an item; `typeHierarchy/supertypes` and
  `/subtypes` return arrays (may be empty — assert the call shape, and assert a
  non-empty case if the fixture has an inheritance edge).
- **Layer diagnostics:** author an `intent` note (or trigger a `review_on_change`
  layer) so `needs_review` is non-empty, then assert `review_state` returns the id
  AND that `vim.diagnostic.get(bufnr, { namespace = <tyo3-layer ns> })` is
  non-empty with the expected range/severity. (Check how the demo/test produces a
  `needs_review` state — `review_on_change` layers flag on a structural edit; see
  `handlers.layers` `review_on_change` field and the engine's review flagging.)
- **Push diagnostics:** after a `sync_buffer` edit that produces a `delta`, assert
  a `publishDiagnostics` reached the client — e.g. wait until
  `vim.diagnostic.get(bufnr)` (the LSP namespace) reflects the pushed set, or
  install a temporary `vim.lsp.handlers["textDocument/publishDiagnostics"]` spy.

Run:
```
devenv shell -- bash -c 'nvim --headless -u editors/tyo3.nvim/tests/minimal_init.lua -c "luafile editors/tyo3.nvim/tests/lsp_nav.lua"'
```
Print `PASS/FAIL` per check; `cquit 1` on any failure.

- **New daemon handler tests** for `definition` and `review_state` under
  `src/tyo3/daemon/tests/` (mirror existing). Run the daemon suite:
  `devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov`.
- **No regressions:** Lua **smoke 12/12**, **context 20/20**, and the Phase-1
  **lsp 17/17** still green (everything opt-in). `ruff check` on touched Python.

---

## Scope boundary

**In (Phase 2):** goto-definition; type hierarchy (prepare/supertypes/subtypes);
layer state (`needs_review`/`orphaned`) as a `vim.diagnostic` namespace; push
diagnostics on bus deltas. Two new daemon read verbs (`definition`,
`review_state`). Opt-in; tests; no regressions.

**Out (explicit follow-ups — note in the PR, don't build):**
- Derived `computing`/`stale` state as diagnostics (depends on per-id derived
  status fan-out; the `derived` bus notification gives `(layer, id)` — a later
  pass can map those to transient `computing` diagnostics).
- Call hierarchy (`textDocument/prepareCallHierarchy` — no daemon verb yet).
- workspace/symbol, document symbols, code actions (e.g. "author intent note" as a
  code action), inlay hints.
- Non-ASCII position-encoding correctness (still a follow-up from Phase 1).
- Debounced/coalesced push diagnostics + cancellation of in-flight `check`.

## Ground rules

- **Everything through devenv.** Pure-Lua + small additive Python handlers; **no
  Rust, no `build`**. Suites are slow — background long runs, budget ~15 min.
- Branch off **`native-lsp-bridge`** (or `main` if #13 merged); open a PR marked a
  **feature prototype, Phase 2**. PR #12 is independent — don't touch it.
- **No AI attribution** anywhere (commits, PR, code, docs).
- Additive + opt-in: default-off behaviour and the existing bespoke UI unchanged.

## Memories to recall
`native-lsp-bridge-shipped` (the Phase-1 wire facts — read this first),
`native-lsp-kickoff`, `nvim-integration-pr`, `nvim-context-panel-demos`,
`devenv-test-entrypoints`, `test-run-timeouts`, `commit-no-ai-attribution`,
`durable-identity-binding-rules`.

## First moves for the new session
1. Read memory `native-lsp-bridge-shipped`, then `lua/tyo3/lsp.lua` (the Phase-1
   converters/handlers/server) and `init.lua` `handle_notification`.
2. Confirm the `dispatchers.notification` contract (`:h vim.lsp.rpc`, runtime
   `lua/vim/lsp/rpc.lua` `default_dispatchers`) and the LSP type-hierarchy /
   definition request shapes.
3. Add the daemon `definition` verb + handler test; bridge `textDocument/definition`
   and smoke it (`vim.lsp.buf_request_sync` on a usage) before moving on.
4. Layer in type hierarchy → `review_state` + the `vim.diagnostic` namespace →
   push diagnostics, testing each. Then finalise the headless test, verify no
   regressions (smoke 12 / context 20 / lsp 17 + daemon pytest), open the PR.
