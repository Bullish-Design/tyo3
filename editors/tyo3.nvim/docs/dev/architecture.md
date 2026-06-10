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
`author`, `authored`, `locate`, `diff`, `derived`, `reindex`, `gc`, `check`,
`subscribe`.

**`subscribe`** sets *this connection's* delta-stream slice (AB7). It is handled
at the server, not in the shared `Handlers` table (which has no per-connection
identity), but is advertised in `ping`'s `methods` so it stays discoverable. Its
params build an `Interest`: `{"files": [...], "ids": [...], "layers": [...],
"all": bool}` — any combination, all optional. A connection defaults to
`Interest.ALL` (every committed delta) until it subscribes; `subscribe {"all":
true}` restores that, and an empty `subscribe {}` mutes the connection (matches
nothing). It replies `{"ok": true}`.

**Navigation / analysis** (the `convert/` read surface — reads only, served live
over the frozen snapshot; cheap-reverse data like references/diagnostics is never
cached as a layer): `references`, `definition`, `document_highlights`, `hover`,
`type_hierarchy`, `can_rename`, `rename`, `diagnostics_at`. All take 1-based
`(path, line, col)`. `references`/`definition` (navigation verbs) hand back
**absolute** paths; `references` powers the sidebar's "Find callers" action
(results → quickfix list); `rename` returns `{new_name, changes}` where `changes`
is `{path: [{range, new_text}]}` (edit computation only — it does **not** rebind
durable identity; that is AB8).

**Layer state** (durable-identity review concepts, no LSP vocabulary):
`review_state {path?}` joins the live-registry `needs_review` / `orphaned` ids
with their pinned-snapshot node ranges (one snapshot for the join), returning
`{items:[{durable_id, name, path, range, state}]}`. The native LSP bridge
surfaces these in a dedicated `vim.diagnostic` namespace (`tyo3-layer`) rather
than an LSP method, so `]d` / Trouble / lualine navigate them.

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

**Per-connection `delta` delivery (AB7).** The pump drains the session bus once
(on `Interest.ALL`) and hands the server the *raw* `Delta`; the server then
scopes and encodes it **per connection** against each client's `Interest` —
mirroring `Bus.publish`'s match/scope semantics (ALL/rescan deliver
unconditionally so a client can pin a snapshot at that revision; a scoped client
gets only a non-empty intersection of its ids/files/layers). A client that never
calls `subscribe` keeps receiving every delta. `delta.layers` carries the
**specific** authored layer name (e.g. `intent`/`summary`) in addition to the
generic `code`/`authored` strings — the name is threaded from `session.author`
through the post-commit hook (the native `CommitDelta` does not encode it), so a
`subscribe {"layers": ["intent"]}` client matches an authored intent write while
a `summary` subscriber does not. Derived layers are lazy (nothing recomputes
inside the commit), so layer-stamping at publish time is authored-only.
`refinement` notifications stay broadcast-to-all (a client that didn't receive a
revision's delta simply ignores its refinement); scoping them is a follow-up.

## Derived layers — producers + cache-key strategy (AB2)

A derived layer's compute is a **`Producer`** (`tyo3.extend`): `produce(ctxs) ->
list[bytes | BaseModel | None]` plus optional `setup`/`teardown` lifecycle (open
an LLM/embedding client once at DAG build, close it at `session.close`). The
legacy `Generator` (`generate(inputs) -> list[bytes]`, the `python`/`command`/
`http` built-ins) rides a thin `_GeneratorProducer` adapter unchanged — config
stays valid.

Each `ctx` is a **recording read handle** to the *pinned snapshot* (never the live
head): `find_references` / `symbol` / `dependents` / `dependencies` / `upstream`
log every id the producer touches into a per-call **read-set** (the raw
`ctx.snapshot` is an escape hatch — reads there are recorded only via
`ctx.note_read`). The read-set is seeded with the entity's own id, because a
producer always depends on its own `source`.

**Cache key by `key_locality`** (`derive/dag.py`):

| `key_locality` | key = | when it recomputes |
|---|---|---|
| `traced` *(default)* | fingerprint of the producer's read-set (each id's content hash) | any read id changes — forward, reverse, sibling, mixed |
| `local` | the entity's own content hash | own body changes |
| `semantic` | own hash ⊕ forward-dependency-closure fingerprint | own body or a dependency changes |
| `reverse-semantic` | own hash ⊕ *direct* reverse-edge fingerprint | own body or a direct caller changes |

`traced` is **produce-then-key**: the key isn't knowable until the producer runs,
so it inverts the usual key-then-fetch. A read first does a cheap self-heal check
— re-fingerprint the *previously read* ids over the current snapshot; unchanged +
cached ⇒ reuse without re-running the producer — and only re-runs (produce → key →
cache) when a traced id moved or on first read. A `{durable_id}`-only read-set (the
legacy adapter) degenerates to **byte-identically** the `local` key. The override
strategies stay key-then-fetch. Self-healing is **lazy-at-read**: adding a caller
does not eagerly recompute the callee (commit-time invalidation skips traced
layers); the next *read* of it does.

**Cheap vs expensive reverse (the Spike C taxonomy).** A *reverse*-direction value
(references, callers, "who implements me") is invisible to forward invalidation, so
a naive `semantic` references layer is **stale-forever**. The resolution is cost ×
direction, not one enum: **cheap-reverse** (references, diagnostics — ms to
recompute) is served as a **live RPC**, never cached as a layer (the `convert/`
surface); **expensive-reverse** (e.g. an LLM summarising an entity's call-sites) is
a `traced` layer — the recording read-set fingerprints exactly the callee + the
call-site ids it read, so adding a caller self-heals the cached value by
construction (salsa's dependency model; ty/salsa is the engine underneath).

**Serving: `block` (sync, default) vs `stale` (async).** `serving` is the
reader-observable freshness contract — orthogonal to `key_locality` (which decides
*when* a value is invalid) and `recompute` (lazy/eager invalidation). On a cache
miss:

| `serving` | behaviour on a miss | for |
|---|---|---|
| `block` *(default)* | produce **synchronously** at read, return `fresh` (or serve last-good honestly on failure) | cheap producers — the common case |
| `stale` | serve last-good/`absent` **immediately** and recompute **off the actor**, publishing a `derived` notification when the value warms | slow producers (LLM/HTTP/embeddings) that must never block the cursor path |

The daemon serves reads on the single `SessionActor` thread, so a slow producer on
the `block` path would stall *every* read. A `stale` layer hands the produce to an
off-actor `DerivedRecomputeWorker` (`derive/async_recompute.py`, the same daemon-
thread + queue shape as the precision refiner): it pins a frozen snapshot at the
read's revision, recomputes through the **same** reentrant DAG seam
(`derived_traced` / `recompute_now`), warms the content-addressed cache, and — only
if the bus has subscribers — publishes a `DerivedFresh` on a dedicated out-of-band
channel (like a refinement; never on the primary `revision > last` stream). The bus
pump turns that into a `derived` JSON-RPC notification carrying `{layer, id,
revision}`; the editor re-pulls and swaps the stale card/decoration for the fresh
value. The worker is a **reader/recompute over a frozen snapshot** — never a writer
to committed truth, so the one-writer rule holds. A failed recompute or an evicted
revision is never a miss: the honest last-good/`absent` already served, and the
cache is warm for the next read regardless of any notification (in-process use
self-heals). Per-`(layer, id, revision)` dedup keeps a burst of reads from
stampeding the producer. The default is `block` because async-by-default would add
flicker + a background worker for cheap layers that gain nothing, and a
mis-declared slow layer fails *loudly* (it blocks) rather than silently flickering.
