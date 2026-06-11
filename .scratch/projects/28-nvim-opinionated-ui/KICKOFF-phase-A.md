# Phase A kickoff — Tier-1 LSP bridges (proj 28)

> Paste the block below as the first message in a clean new session. It is
> self-contained; it assumes no memory of the planning conversation.

---

You are implementing **Phase A** of a planned tyo3.nvim redesign. The plan
already exists — read it first, then execute Phase A only. Do **not** start the
later phases (B–F) or the broader UI rewrite; Phase A is pure-additive and ships
on its own.

## Read these first (in order)
1. `.scratch/projects/28-nvim-opinionated-ui/CONCEPT.md` — the vision and the
   locked decisions (why the redesign; you only need §6 phase map for context).
2. `.scratch/projects/28-nvim-opinionated-ui/PLAN.md` — **§"Phase A"** is your
   spec (A.1–A.5). Skim the rest for context but build only Phase A.

## Goal
Add three LSP capabilities to the native bridge, over data the engine already
has: **`textDocument/documentSymbol`**, **`workspace/symbol`**, and **call
hierarchy** (`prepareCallHierarchy` + incoming/outgoing). These are additive —
no UI change, no behavior change to existing features — and unlock breadcrumbs,
symbol pickers, and caller/callee navigation.

## Working rules (this repo — also in CLAUDE.md / auto-memory)
- **Always run project commands through devenv**, never raw pytest/cargo/nvim:
  - Build: `devenv shell -- build`
  - Daemon tests: `devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov`
  - Full suite (final gate): `devenv shell -- test-fast`
  - Headless Lua specs:
    ```
    devenv shell -- nvim --headless --clean \
      -u editors/tyo3.nvim/tests/minimal_init.lua \
      -c "luafile editors/tyo3.nvim/tests/<spec>.lua"
    ```
    Existing specs: `smoke`, `context`, `lsp`, `lsp_nav`, `lsp_codeaction`,
    `review_dedup`. Suites are slow — give them ~15 min; run full suites in the
    background.
- **No AI attribution** anywhere — no "Co-Authored-By" / "Generated with" in
  commits, PRs, or comments. Match surrounding comment density and idiom.
- Work on a **branch off `main`** (e.g. `proj28-phase-a-bridges`), not `main`.
  Commit when green. Don't push or open a PR unless asked.
- Phase A does **not** touch the single-path/always-on conversion (that's Phase
  B). The bridge stays opt-in here; tests attach it explicitly (see below).

## Study these existing patterns before writing code
- `editors/tyo3.nvim/lua/tyo3/lsp.lua`:
  - `CAPS` table (advertise new providers here).
  - `daemon_range_to_lsp`, `path_to_uri`, `uri_to_path`, `lsp_pos_to_daemon`
    (reuse — don't reinvent position conversion).
  - `handlers["textDocument/prepareTypeHierarchy"]`, `type_hierarchy_relatives`,
    `lsp_type_item` — **call hierarchy mirrors this exact pattern** (prepare
    returns an item carrying its `selectionRange`; incoming/outgoing re-query the
    daemon off that selectionRange).
  - The nvim-0.12 capability-key quirk: `["typeHierarchy/supertypes"] = true`
    etc. — verify whether call hierarchy needs the analogous literal keys
    (`["callHierarchy/incomingCalls"]`, `["callHierarchy/outgoingCalls"]`).
- `src/tyo3/daemon/handlers.py`:
  - `decorate` (A.1 reuses its output verbatim — **no new daemon verb for
    documentSymbol**), `entity_at`/`_entity_dict`, `type_hierarchy`,
    `find_references`, `_range_dict`, `_node_by_id`, the `_METHODS` dispatch table
    (register new verbs there), and the one-snapshot pattern (`with s.snapshot()`).
- `src/tyo3/graph/queries.py`: `dependencies` / `dependents` (for outgoing calls).
- `editors/tyo3.nvim/tests/lsp_codeaction.lua` and `lsp_nav.lua` — copy their
  scaffolding (build shop project, `setup{lsp=true}`, `require("tyo3.lsp").attach`,
  resolve positions via `decorate`) for the new `lsp_symbols.lua` spec.

## Build, in this order (independently verifiable)
1. **A.1 documentSymbol** — `CAPS.documentSymbolProvider = true`; handler maps
   `decorate {path}` → hierarchical `DocumentSymbol[]` (nest methods under their
   class via the dotted `qualified_name`); add a `kind.value → LSP SymbolKind`
   map. No daemon change.
2. **A.2 workspace/symbol** — new daemon `symbols(params{query?})` verb walking
   the head graph (same entity filter as `decorate`: `is_entity_durable_id`, not
   external; one snapshot; optional substring filter on `qualified_name`, capped).
   `CAPS.workspaceSymbolProvider = true`; handler maps to `SymbolInformation[]`
   `{name, kind, location={uri, range}}`.
3. **A.3 call hierarchy** — new daemon `call_hierarchy(params{path,line,col})`
   returning `{item, incoming:[{from:<item>, ranges}], outgoing:[{to:<item>,
   ranges}]}` (incoming from `find_references` grouped by enclosing entity via
   `id_for`, ranges = call sites; outgoing from `dependencies` resolved to entity
   nodes). Bridge: `CAPS.callHierarchyProvider = true`; `prepareCallHierarchy`
   returns the item; `incomingCalls`/`outgoingCalls` re-query off the item's
   selectionRange and map to `CallHierarchy{Incoming,Outgoing}Call[]`.

## Critical facts you must not get wrong
- **documentSymbol needs no daemon verb** — `decorate` already returns
  `{durable_id, name, qualified_name, kind, range, …}` per file. Build the tree
  in Lua from the dotted `qualified_name` (e.g. `Item.price` nests under `Item`).
- **Position conventions** (already handled by the helpers — reuse them, don't
  regress): daemon ranges are 1-based with an *exclusive* end column; LSP is
  0-based; the conversion is a uniform −1 on every line and column
  (`daemon_range_to_lsp`). `positionEncoding = "utf-32"`.
- **Path shape**: navigation verbs return absolute paths, others
  project-relative — `path_to_uri(root, p)` already anchors either. Use it.
- **`id_for` at a method position resolves to the enclosing class** (a documented
  binding rule). This matters for grouping incoming callers by entity — a call
  site inside a method resolves to that method's class as the "caller" entity.
  That's acceptable; just don't assume method-level granularity.
- **Call hierarchy re-query**: emit items whose `selectionRange` round-trips back
  to a daemon position (exactly how `type_hierarchy_relatives` works) so
  incoming/outgoing can re-resolve without server state.
- **Bridge is opt-in in Phase A** — the spec attaches it explicitly in tests
  (`setup{lsp=true}` + `require("tyo3.lsp").attach(bufnr, root)`); do **not**
  make it always-on (Phase B).

## Tests (Definition of done)
- `src/tyo3/daemon/tests/test_handlers.py`: `symbols` lists checkout +
  `Item.price` across files; `call_hierarchy` on `usd` (money.py) lists
  `checkout` as an incoming caller with a call-site range, and `checkout`'s
  outgoing includes `usd`/`Book`.
- New `editors/tyo3.nvim/tests/lsp_symbols.lua`: attach the bridge, then assert
  - `textDocument/documentSymbol` on store.py returns `checkout` + `show_label`
    with sensible kinds;
  - `workspace/symbol {query="checkout"}` returns a result with a location uri +
    range;
  - `prepareCallHierarchy` on `usd` returns an item; `callHierarchy/incomingCalls`
    includes `checkout`.
- **No regression** in `smoke`, `lsp`, `lsp_nav`, `lsp_codeaction`,
  `review_dedup`, `context`.

## Verify
```
devenv shell -- build
devenv shell -- pytest src/tyo3/daemon/tests -q --no-cov
devenv shell -- nvim --headless --clean -u editors/tyo3.nvim/tests/minimal_init.lua \
  -c "luafile editors/tyo3.nvim/tests/lsp_symbols.lua"
# regression sweep:
for t in smoke context lsp lsp_nav lsp_codeaction review_dedup; do
  devenv shell -- nvim --headless --clean -u editors/tyo3.nvim/tests/minimal_init.lua \
    -c "luafile editors/tyo3.nvim/tests/$t.lua"
done
# final gate:
devenv shell -- test-fast
```

## When you finish
Report what changed (files), what you ran, and the pass/fail output. Commit on
the branch with a clear message (e.g. `feat: documentSymbol + workspace/symbol +
call hierarchy bridges`). Then stop and report — do not start Phase B. If you hit
the call-hierarchy capability-key question or anything ambiguous in the spec,
pause and ask rather than guessing.
