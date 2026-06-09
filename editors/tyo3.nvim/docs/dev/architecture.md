# TyO3 — architecture (developer)

How the Neovim plugin, the daemon, and the engine fit together.

```
  Neovim (tyo3.nvim, Lua)
    │  JSON-RPC over a unix socket
    ▼
  tyo3-daemon (src/tyo3/daemon/, Python)
    │  in-process calls on one owner thread
    ▼
  TyO3Session / engine (src/tyo3/, Python + Rust via PyO3)
```

## Daemon

`src/tyo3/daemon/` is a thin, daemon-only server: JSON-RPC over a unix socket,
one daemon per project root. Every session call runs through a single
**`SessionActor`** owner thread, so requests are serialized — the plugin must not
flood it (hence the debounce + dedupe on cursor-driven reads).

Reads (`entity_at`, `decorate`, `authored`, `derived`, `diff`) run over a
**frozen committed snapshot**, so they reflect the last commit (saved/synced
state), not the in-flight buffer.

### RPC surface

`ping`, `open`, `sync_buffer`, `sync_buffers`, `entity_at`, `decorate`,
`author`, `authored`, `locate`, `diff`, `derived`, `reindex`, `gc`, `check`.

**Navigation / analysis** (the `convert/` read surface — reads only, served live
over the frozen snapshot; cheap-reverse data like references/diagnostics is never
cached as a layer): `references`, `document_highlights`, `hover`,
`type_hierarchy`, `can_rename`, `rename`, `diagnostics_at`. All take 1-based
`(path, line, col)`. `references` powers the sidebar's "Find callers" action
(results → quickfix list); `rename` returns `{new_name, changes}` where `changes`
is `{path: [{range, new_text}]}` (edit computation only — it does **not** rebind
durable identity; that is AB8).

**Layer discovery:** `layers` describes every layer in the **effective table**
(native config ∪ layers registered programmatically via `tyo3.extend`) — `name`,
`origin`, `entity_kinds`, `history`, `review_on_change`, `serving`,
`key_locality`, `display`, and `schema`. `display` is `panel` / `inline-note` /
`inline-summary` (AB1/QW5): a registered spec's declared display, or a name
heuristic for built-ins (`intent` → `inline-note`, `summary` → `inline-summary`,
else `panel`). `schema` is the layer's JSON Schema (`model_json_schema()`) when a
registered spec declares a pydantic `schema` (AB5), else `null` — a typed client
can build an authoring form from it, and authored writes to a schema'd layer are
validated at author-time (`SchemaValidationError` on mismatch); un-schema'd layers
stay free-form JSON. The editor builds its "Author …" menu from this — one entry per
`origin == "authored"` layer — instead of hardcoding `intent`, and the daemon
picks inline-decoration layers by `display` rather than by name. A layer
registered through `tyo3.extend.register_layer` rides `entity_at`, `decorate`, and
this verb with **no** handler change. `layer_ids(layer, with_values?)` returns the
ids that have a record in a layer (optionally with their values) in **one**
snapshot/actor hop — the authored-notes picker uses it instead of an `authored`
probe per entity.

The headline read is **`entity_at(path, line, col)`** (1-based), which returns
the full cross-layer *card*: identity, location, kind, range, content hash, every
authored layer with a record, every derived layer's artifact, and
`last_affected_revision`. The plugin renders this card both in the `:TyO3Inspect`
float and the sidebar's identity/notes/summary panes (see
[`lua/tyo3/card.lua`]).

## Plugin modules (`editors/tyo3.nvim/lua/tyo3/`)

- `init.lua` — lifecycle (open / debounced sync / write), `rpc` / `with_client`,
  notification routing (`delta` / `refinement` → panel).
- `daemon.lua` — spawn / connect / root discovery.
- `context.lua` — cursor-driven reads: a Treesitter-gated enclosing-node key,
  per-buffer debounce, and a stale-drop guard so a superseded lookup never
  flickers in a prior entity.
- `card.lua` — the entity card → lines formatter, shared by the float and panel.
- `panel.lua` — the side dock: collapsible panes (IDENTITY / NOTES / SUMMARY /
  ACTIONS / DOCS / AFFECTED), each a separate *data type* hanging off the spine.
- `actions.lua` — the context-relevant tool list + execution from the cached card.
- `docs.lua` — the documentation registry + opener (these files).
- `decorate.lua` — inline extmark notes (🏷) and summaries (⟢).

## Notifications

The daemon's bus pump pushes `delta` (an edit's affected set) and `refinement`
(the async-narrowed set) as JSON-RPC notifications; `init.handle_notification`
routes them to the AFFECTED pane. Decorations re-anchor on every commit —
identity is the truth, never trust a drifted extmark across a structural edit.
