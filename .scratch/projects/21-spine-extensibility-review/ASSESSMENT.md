# ASSESSMENT — the TyO3 spine as an "attach-anything-to-an-AST-node" backend

> Review engagement per `REVIEW_BRIEF.md`. Read-only; every claim is backed by a
> `file:line` or a traced call path. Baseline: native extension built and working
> (`devenv shell -- check-so` ✅, `_native_impl.abi3.so` present); `test-fast` run
> in the background to confirm green (see §11).

## 0. TL;DR verdict

The spine concept is **real and already working**: a durable id with a uniform
`LayerView` read protocol, and — critically — the `entity_at` card and the nvim
panel **already enumerate arbitrary layers generically**. Adding a *value-shaped*
attachment that uses an existing mechanism (an authored dict, or a derived
python/command/http generator over an fs/lancedb store) is **config-only and
touches zero core files**, and it auto-appears in the editor card. That is the
strong part, and it is stronger than the brief's §3 framing implies on the *read*
side.

The weak part is exactly the §2 vision: **there is no programmatic registration
API**, and the moment a new attachment needs *new behaviour* — a stateful
generator, a custom store backend, a typed value, a bespoke author/edit affordance
in the editor, or any of the rich `convert/` data (references, hover, rename,
diagnostics) — you fall off a cliff: you must edit core dispatch
(`make_generator`, `open_store`), write a module and reference it by a dotted
string, and hand-wire the daemon RPC table and the Lua plugin. The "attach
anything" promise holds for *reads of declared layers* and breaks for
*everything that defines new production, storage, typing, or editor surface*.

Distance to the §2 vision: **medium overall** — the read/serve/identity/card
plumbing is generic and reusable; the registration surface, the production/store
extension points, and the daemon's layer-agnostic write/serve API are the missing
third.

---

## 1. The end-to-end trace (grounding the whole review)

One authored attachment, cursor → store → card → render, traced through the code:

1. **Editor write.** `:TyO3Note` → `notes.lua:12` sends RPC `author` with
   `{layer="intent", durable_id, value={note=text}}` (the layer name is a
   **hardcoded string literal**; `actions.lua:37` does the same in the panel).
2. **Daemon.** `handlers.author` (`handlers.py:185`) → `SessionActor.submit` →
   `session.author("intent", id, value)` (`session.py:739`). Value is
   `json.dumps`-ed (`session.py:747`) — **free-form**, no schema.
3. **Native commit.** `PyTyProject.author` (`methods.rs:307`) parses the JSON,
   builds `Mutation::Author{layer,id,value}`, and `commit()`s it. `build_plan`
   validates the layer via `config.authored_layer_config(&layer)`
   (`commit.rs:634`) — **a non-authored / undeclared layer fails cleanly here**.
   The record is a `serde_json::Value` payload (`authored.rs:22`,
   `AuthoredVersion.value`), revision-stamped, with optional history.
4. **Post-commit.** `session._after_commit` (`session.py:582`) runs the one hook:
   invalidate head snapshot → `_apply_graph_delta` (a **no-op for an
   authored-only delta**, `session.py:546` / `_is_authored_only` at `:563`) →
   `_schedule_derived` (no-op, authored layers are sinks) → `_publish_delta`.
5. **Read-back / card.** `entity_at` (`handlers.py:129`) → `id_for` →
   `_entity_dict` (`handlers.py:304`). The card loops **every** authored layer
   (`for lname, lcfg in s.config.layers.items()` at `:324`) and **every** derived
   layer (`:333`), including any present record. This is the generic seam.
6. **Render.** `card.lua:115` ("Every authored layer with a present record …, not
   just `intent`") and `card.lua:122` (every derived layer) render whatever the
   card contains. The panel's NOTES/SUMMARY/DOCS panes (`panel.lua`) likewise
   iterate.

**The asymmetry is the headline of the whole review**: steps 5–6 (serve/render)
are *fully generic over layers*; steps 1–3 (author/produce) are *hardcoded to the
specific built-in layer names* at every call site.

---

## 2. Identity as the foundation

**Support today: good. Distance to vision: small (with one sharp edge).**

- The durable id is a minted ULID held in an `IdentityRegistry` with two indices,
  `by_path` (exact) and `by_hash` (`identity.rs:77-99`). `reconcile()` classifies
  each entity into one of **four `Binding` rules** (`identity.rs:476-493`):
  - **Rule 1 `Exact`** — same `qualified_path`. Reported as `changed` *only if the
    content hash differs* (no-over-fire, `identity.rs:530-538`); an unchanged exact
    is omitted entirely.
  - **Rule 2 `Moved`** — same content hash at a new path → a structured
    `MovedBinding` (id + old/new path + old/new file, `identity.rs:540-551`), never
    `changed`/`created`.
  - **Rule 3 `Struct`** — structural match: **same name** + kind + container, low
    confidence (`identity.rs:485-490`); rebinds the id, flags `needs_review`, and
    reports `changed` when the hash differs.
  - **Rule 4 `Minted`** — no match → a fresh ULID.
  The set sorts its input so results are deterministic (matcher note
  `identity.rs:13`; `classify` preserves the pre-sorted order, `:509`).
- **Why rename mints a new id (and rule 3 does *not* rescue it):** rule 3 keys on
  *same name* + kind + container, but a rename **changes the name**, so it falls
  through to rule 4 (`Minted`) — exactly the empirically-verified
  remove-old+add-new behaviour. Rule 3 is the seam a *future* rename-survival rule
  would extend, but it would need to key on the structural *neighbourhood* (not the
  name), and it is deliberately low-confidence + `needs_review` to stay
  deterministic. This is why the path (ROADMAP AB8) is an **explicit native rename
  rebind** driven by the editor's known rename intent — not a matcher heuristic
  (Spike D proved reconciliation can't infer it: a rename perturbs path *and* hash
  *and* name simultaneously).
  editor's rename through the atomic-move commit rather than loosen the matcher.
- **Empirically verified binding rules** (re-confirmed by `SPIKE_FINDINGS.md`
  Spike D against the live engine): in-place cosmetic edit → same id, note
  `present`; meaningful body edit → same id (`Changed`), note `needs_review`;
  atomic move (same content hash, different file in one commit) → same id
  (`Moved`, note rides `present`); **rename → NEW id** (`created=[new]
  deleted=[old]`), note `orphaned`→`absent`. The plugin's identity-preserving move
  (`move.lua` → `sync_buffers` → `edit_many`, one atomic commit) is the safe path;
  `card.lua`/decorations re-anchor on every commit and never trust a drifted
  extmark (architecture.md §Notifications).
- **Mechanism (why rename can't be inferred — `hash.rs:337`).** The entity's
  **name is part of its content hash** (`visit_identifier` "Captures def/class
  names"). So an atomic *move* keeps the name ⇒ content hash unchanged ⇒ `by_hash`
  matches at the new path ⇒ `Moved`. A *rename* changes the name ⇒ hash **and**
  qualified_path both change, and rule-3 `Struct` needs the *same* name ⇒ all three
  reconcile keys miss ⇒ `Minted`. This is why rename-survival cannot be a
  daemon-only trick (it overturned the first-pass "route rename through atomic
  move" idea — see ROADMAP AB8): it needs an **explicit** native rename rebind, not
  reconciliation inference.
- **Nested-entity caveat:** `entity_at` on a method's def line resolves to the
  enclosing class (the registry only mints top-level ids; the graph stores a
  compound `durable_id::qualified_name` for nested nodes — `identity.py:49-55`,
  `derive_durable_id`). So an attachment on a method is keyed under a compound id
  that is *not* what `id_for` returns at the method's own position.
- **Surfaced/queryable enough?** Yes for clients: `id_for(path,line,col)`,
  `locate(id)`, `needs_review()`, `orphaned()` are all on the session and the RPC
  table. Good enough to key any attachment.

**Sharp edge for "attach anything":** any attachment keyed on the durable id
**inherits the rename hole** — author a `tests` link or a `review` note, rename
the function with a normal edit, and the attachment is orphaned exactly as an
`intent` note is. Rename-survival is feasible only via an **explicit** native
rename rebind driven by the editor's rename intent (ROADMAP AB8) — **not** by
inference: Spike D proved an atomic commit can't help (a rename changes path, hash,
*and* name at once), so the only inference option would be a new neighbourhood-keyed
matcher rule, which is a real cost and a known landmine (§12). Do not naively "fix"
the matcher; it is intentionally conservative to stay deterministic and never
mis-bind.

---

## 3. The layer model: read side vs write/compute side

**Support today: partial→good on reads; accidental split on writes. Distance:
medium.**

- **Read side is clean and generic.** `LayerView` (`layers/base.py:34`) is a real
  Protocol: `name`, `origin`, `ids()`, `value(id)`, `diff(other)`. `Snapshot.layer(name)`
  (`views.py:123`) dispatches purely by `config.origin` to `CodeLayerView` /
  `DerivedLayerView` / `AuthoredLayerView` — **no per-layer special-casing**. The
  cross-layer join (`EntityView`) and the combined `SnapshotDiff` consume the
  protocol uniformly. This is genuinely "attach anything and it reads back."
- **The write/compute side is three disjoint mechanisms, not one contract:**
  - *Authored*: write goes native (`Mutation::Author`), value is a free-form
    `serde_json::Value`. The Python `AuthoredLayer` (`authored/layer.py`) is a
    **read-only config view** carrying *no* store/generator — authored layers are
    sinks (`config.rs:422-435` rejects derived-only keys on authored layers).
  - *Derived*: `DerivedLayer` (`derive/layer.py`) owns a cache + generator +
    declared contract, assembled by `DerivationDAG.from_session` (`dag.py:39-91`).
  - *Code*: produced natively, projected into the graph.
- **What the *actual* contract for a layer is** is therefore split: "to be a
  layer" you must (a) be declared in `config.toml`, (b) have an `origin` the
  `Snapshot.layer` dispatch knows (`derived`/`authored`/`code` — `views.py:137-146`),
  and (c) if derived, satisfy the generator protocol + a store. There is **no
  single `Layer` object** you can register that bundles "value shape + how it's
  produced + where it's stored + how it's invalidated." The split is partly
  *essential* (authored is a human sink; derived is a computed pipeline) but
  partly *accidental* (the read protocol already unifies them; the write side
  never grew the matching abstraction).

---

## 4. Extension seams — enumerated precisely

This is the core of the §3 question. Every way to add behaviour today:

| Seam | How | Where it's defined | Hard-coded dispatch? |
|---|---|---|---|
| New authored layer | `[layers.X] origin="authored"` in `config.toml` | validated `config.rs:413-435` | **No** — fully generic; `author`/`authored`/card all key off the string |
| New derived layer | `[layers.X] origin="derived" generator=g store=s` | `config.rs:368-392`, assembled `dag.py:56-89` | **No** for assembly; **yes** for the pieces below |
| New generator **instance** | `[generators.g] type="python" callable="mod:fn"` | `generators.py:69` (PythonGenerator) | string import via `importlib.import_module` (`generators.py:79`) |
| New generator **type** | — | `make_generator` dispatch (`generators.py:53-63`) | **Yes** — `python`/`command`/`http` only; a 4th requires editing this `if/elif` |
| New store **backend** | `[stores.s] backend="..."` | `open_store` dispatch (`stores/__init__.py:22-46`) | **Yes** — `fs`/`lancedb` real; others raise `not implemented` (`:45`) |
| New value shape | put any JSON / bytes | authored `serde_json::Value`; derived `bytes` | **No typing** — free-form everywhere |
| New RPC verb | — | `_METHODS` table (`handlers.py:456-471`) | **Yes** — static dict, edited by hand |
| New editor affordance | — | Lua (`actions.lua`, `notes.lua`, …) | **Yes** — author layer names are literals |

**What blocks a pure-Python plugin from registering a layer without touching
core:**

1. **No registration entry point at all.** No `register_layer/register_generator/
   register_store`, no `importlib.metadata` entry points, no plugin protocol.
   `grep` confirms: the only extensibility is `config.toml` + dotted-string
   `callable`. The config is parsed and *validated in Rust* (`config.rs::validate`)
   and projected read-only into Python (`config.py:TyConfig.from_json`), so even
   "add a layer at runtime" has no Python-side door — the layer set is fixed at
   `open()`.
2. **Custom *types* require editing core dispatch.** A stateful generator (holds a
   model client, batches across calls, declares its own deps) cannot be a
   `module:function`; you must add a branch to `make_generator` (`generators.py:53`).
   A custom store (sqlite-vec, qdrant, an HTTP KV) is stubbed but unimplemented and
   requires editing `open_store` (`stores/__init__.py:39-46`).
3. **The generator contract is narrow.** `Generator.generate(inputs) -> list[bytes]`
   (`generators.py:42-47`). Input is `GenInput{durable_id, source, kind, meta}`
   (`:29`); `source` is the entity's *sliced source text* (`dag.py:_entity_source`,
   `:362`) or an upstream artifact's bytes. A generator that needs the AST, the
   type, references, or sibling-entity context has **no way to ask for it** — the
   only inputs are text + kind + the declared `depends_on` upstream artifact.

**Net:** the *declaration + read + card* path is genuinely generic and
config-driven (better than §3 feared). The *production, storage, typing, and
editor-write* paths are string-indirected and dispatch-hardcoded (exactly as §3
feared). The gap is real and precisely localized to four files:
`generators.py::make_generator`, `stores/__init__.py::open_store`,
`handlers.py::_METHODS`, and the Lua author call sites.

---

## 5. Derived pipeline — deps, invalidation, async refinement

**Support today: good (for what it covers). Distance: small.**

- `DerivationDAG` (`dag.py`) assembles `DerivedLayer`s in the config's validated
  topo order (`config.rs::topo_order`), excludes authored sinks, and defensively
  re-checks acyclicity (`dag.py:34-37`). Deps are declared (`depends_on`); a
  layer-derived layer reads its upstream artifact and keys on its bytes hash
  (`dag.py:247-268`).
- **Invalidation is the strong part** (memory `phase8-derived-invalidation-done`,
  confirmed): locality lives in **one place — the cache key**. `resolve_input`
  (`dag.py:210`) computes `input_hash` per declared `key_locality`: `local` ⇒ the
  entity's own `content_hashes[profile]`; `semantic` ⇒ `hash(content_hash +
  dependency-closure fingerprint)` (`dag.py:237-241`, `_dependency_fingerprint`
  walks the transitive forward-dep closure over the **pinned snapshot**, never the
  live head — `:288`). The read path (`views.py:226`), the commit-time invalidate
  loop (`dag.py:117`), and the scheduler all key off that one hash, so
  "local-no-over-recompute" and "semantic-recompute-on-dep-change" fall out of one
  mechanism. This is exactly what an "attach a custom derived value with declared
  deps" story needs, and it already works — `test_final_acceptance.py` proves it
  by generator call count.
- **Async precision refinement** (Phase 9): with `[code_graph] precision="method"`
  a `PrecisionRefiner` narrows the coarse container-granular affected set to
  method level over a frozen snapshot and republishes on the bus refinement
  channel (`session.py:609-629`). Correctness never depends on it (the coarse set
  is a sound superset). A custom derived layer rides this for free — it consumes
  durable ids, not graph specifics.
- **The scheduler is small but correct.** `RecomputeScheduler` (`scheduler.py`)
  keys work items by `(layer_name, input_hash)` and computes each **at most once**
  (`scheduler.py:58-66`); `process_all` walks the DAG in topo order so upstreams
  compute first (`:68-116`); `recompute_now` is the synchronous read-path heal
  (`:118`). **Failure isolation is real:** a `GeneratorFailed` (or any exception)
  marks the id `failed` and leaves the prior artifact intact — partial artifacts
  are never written (`:99-113`, `:134-137`). This is the machinery a custom layer
  inherits for free.
- **How a custom derived layer plugs in today:** purely via config — declare it,
  point `generator`/`store`, set `key_locality`, `entity_kinds`,
  `serving`/`recompute`. The DAG picks it up at `from_session`. **No core change
  needed *unless* you need a generator type or store backend that isn't built in.**
- **⚠ The reverse-dependency wall (`SPIKE_FINDINGS.md` Spike C — the deepest
  finding).** Invalidation only flows **forward** (from a change to its
  *dependents*), and the `semantic` fingerprint walks **forward** deps only
  (`dag.py:_dependency_fingerprint` → `graph.dependencies`). So a layer whose value
  depends on an entity's *dependents* — references/callers, "my subclasses", "who
  implements me" — is **invisible to every freshness mechanism**: adding a new
  caller leaves the callee out of `affected_ids` (verified: callee `False` in
  affected), doesn't change the callee's forward-dep fingerprint, and so a re-read
  returns a **cache hit with no recompute** (verified: 0 recomputes). A naive
  `references` layer is therefore **stale-forever**. The fix is a taxonomy by
  cost×direction (ROADMAP AB2): **cheap reverse data (references, diagnostics) is
  served as a live RPC, not cached at all** (QW1 — the engine recomputes it in ms,
  Spike E); **expensive reverse data keys on a *traced read-set*** (the producer's
  recording snapshot handle fingerprints exactly the ids it touched — correct for
  any direction by construction, salsa's model); `reverse-semantic` (direct, lazy,
  memoized) only as an interim. The wall must be designed around (API_DESIGN
  §2.4/§5.2/§8), or "attach a references layer" silently serves stale data.
- **Other gaps:** (a) GC for fs stores is a documented no-op (`dag.py:_gc_store`,
  `:355-359` "deferred to the full implementation"). (b) The recompute is
  synchronous at read on cache miss (`views.py:244`, `recompute_now`) — for a slow
  generator (LLM/HTTP) this blocks the single-threaded actor (see §8). (c)
  `_entity_source` slices the file off disk by range (`dag.py:362-401`), which is a
  re-read per entity and is fragile for virtual/unsaved buffers.

---

## 6. Daemon / editor fit — the RPC surface vs. what the workflow needs

**Support today: partial. Distance: medium — this is the second-biggest gap.**

The RPC table (`handlers.py:456`) is exactly 14 verbs: `ping`, `open`,
`sync_buffer(s)`, `entity_at`, `decorate`, `author`, `authored`, `locate`,
`diff`, `derived`, `reindex`, `gc`, `check`. Findings:

- **`entity_at` already generalizes to arbitrary layers** — confirmed by reading
  `_entity_dict` (`handlers.py:304-345`): it loops all authored and all derived
  layers from `config`, no allow-list. The nvim `docs` layer was added this way
  and rode the card automatically. ✅ This is the best news for the daemon.
- **But there is no generic write/serve surface.** `author` takes a layer string
  (good), but the editor has **no way to *discover* what layers exist and how to
  author them**: `open` returns `sorted(config.layers)` (`handlers.py:88`) — names
  only, no origin/value-shape/affordance metadata. So the plugin **hardcodes**
  `intent`/`docs` at every author site (`notes.lua:12`, `actions.lua:37`,
  `entitydoc.lua:14`, `telescope.lua:146`) and special-cases `docs` in the panel
  (`panel.lua:133,197`). Add a `review` authored layer and it shows in the card
  for free but you must write Lua to author it. **The card is generic; the actions
  are not.**
- **The rich `convert/` surface is invisible to the editor** (see §7). The plugin
  wants references/callers — there is no `references` RPC, so "Go to definition"
  in `actions.lua:51` resorts to `locate` + a **heuristic `vim.fn.search` for the
  bare name** (`actions.lua:69-73`) instead of the real `goto_definition`/
  `find_references` that already exist in Python.
- **Reverse-deps / neighbors are not served.** The bus delivers an `affected` set
  on commit, and `decorate` walks the head graph for one file, but there is no
  "who calls this id / what does this id depend on" RPC (reverse-deps exist in the
  Rust producer; `find_references` exists in `_ReadOps`; neither is on the table).
- **Streaming is delta/refinement only — but the per-layer primitive already
  exists, unused.** `BusPump` subscribes exactly `Interest.ALL` (`bus_pump.py:65`)
  and forwards `delta` + `refinement` notifications. Yet `Interest` *already*
  supports `Interest.layer(name)` / compound `files`/`ids`/`layers` filters with a
  `touched_layers` match arm (`interest.py:44-47`, `:79-81`). So "notify me when any
  `summary` recomputes" is a bus capability that is simply **not wired at the
  daemon** (and the published `Delta` would need to populate `touched_layers`).
  This is a small lift, not a missing mechanism.
- **The notification wire is richer than the card / commit-delta wire.** `_emit_delta`
  ships `authored_ids` and `moved_ids` (`bus_pump.py:121-128`) that `_commit_delta_dict`
  (`handlers.py:409`) omits — so authored writes *do* reach the editor as deltas,
  but the per-call `sync_buffer` response doesn't echo them. Minor inconsistency,
  worth unifying.
- **A real render seam even for the built-ins (confirmed, `SPIKE_FINDINGS.md`
  Spike F).** The card *assembly* is generic (it includes every non-`absent`
  authored/derived record, `handlers.py:328,339`), but two of the three
  *renderers* gate on `status == "present"` (`panel.lua:169` SUMMARY pane;
  `card.lua:124` compact context) — and **derived statuses are
  `fresh|stale|failed|absent`** (`models/derived.py:23`; the spike's live card
  emitted `status='fresh'`), which *never equal* `present`. So the panel's SUMMARY pane and the compact context card
  silently drop **all** derived artifacts (the `:TyO3Inspect` float renders them
  correctly via `build_lines`, `card.lua:73-80`; inline summaries use the separate
  `decorate` path). Authored `needs_review`/`orphaned` records
  (`models/authored.py:30`) are dropped the same way. A *newly registered* derived
  layer would hit exactly this — proof that "surfacing generalizes" holds at the
  card/float level but the panel/compact renderers were tuned to the built-ins'
  happy path.
- **Project-wide layer queries are N round-trips.** The "authored notes" picker
  (`telescope.lua:131-161`) iterates the client-side name cache and fires one
  `authored {layer="intent", ...}` RPC **per id** (`:146`) — there is no batch
  "list ids with a record in layer X" verb. On the serial actor (§8) that is N
  enqueued reads for one picker.

---

## 7. The Rust `convert/` surface — what it offers, and that it's stranded at the daemon

**Support today: good in Python, NONE at the daemon. Distance: small to expose,
large to make attachable.**

- `convert/` is a full LSP-shaped surface: `diagnostics`, `hierarchy`,
  `occurrences`, `symbols`, `navigation` (defs + references), `hover`, `tokens`,
  `rename`, `folding`, `signature`, `completion`, `hints`, `code_action`
  (`convert/mod.rs:1-13`). It is wired into `project/analysis.rs` (23
  `convert::` call sites, e.g. `analysis.rs:239` references, `:589` rename,
  `:649` hover) and **exposed all the way to Python** via `_ReadOps`:
  `find_references`, `document_highlights`, `goto_definition/declaration/
  type_definition`, `hover`, `rename`/`can_rename`, `type_hierarchy`,
  `class_supertypes`, `file_occurrences`, `completions`, `signature_help`,
  `semantic_tokens`, `code_actions`, `inlay_hints`, `folding_ranges`, `hints`
  (`read_ops.py:135-579`).
- **Why it exists:** TyO3 wraps Astral's `ty` semantic engine; this is the
  LSP-grade query surface the engine already computes. It is the single richest
  under-used asset in the codebase for an interactive editor.
- **The system already eats its own `convert/` dog food — internally.** The
  precision refiner (`precision/refiner.py`) narrows the affected set by calling
  `snapshot.find_references` on each changed member and mapping each reference back
  to its enclosing entity (`refiner.py:192-204`, `_enclosing_entity_id` at `:264`).
  So with `precision=method` the daemon **already depends on the references surface
  transitively** — it just never offers it to the editor as an RPC. The capability
  is proven in production code; only the handler is missing.
- **The gap:** **none of it is on the daemon RPC table.** `_METHODS`
  (`handlers.py:456`) has `check` but not `find_references`, `hover`, `rename`,
  `type_hierarchy`, … So the editor cannot reach references/callers/hover/rename
  even though the engine produces them and Python exposes them. The plugin's
  fallbacks (heuristic name search in `actions.lua`) are a direct symptom.
- **Should the spine present `convert/` data as *attachable/derivable*?** Two
  framings, both valuable:
  1. **As live RPCs** (cheap, high-impact): expose `references`, `hover`,
     `rename`, `type_hierarchy` verbs — a half-day of handler wiring unlocks
     callers/neighbors/rename in the editor. (Rename then becomes the natural home
     for *identity-preserving rename*: the engine computes the workspace edit, the
     daemon applies it through `sync_buffers` so the move/edit binding preserves
     the id — closing the §2 rename hole at the workflow level without touching the
     matcher.)
  2. **As derived layers** (the deeper idea): a `diagnostics` or `references`
     attachment is just a derived layer whose "generator" reads the `convert/`
     surface instead of an LLM. This is exactly what the §2 API should make
     trivial — and it reveals that the current `Generator` contract is too narrow
     (it gets text, not the engine), see §4.3 and API_DESIGN §4.

---

## 8. Performance & concurrency

**Support today: partial (correct but serial). Distance: medium.**

- **One `SessionActor` owner thread** serializes every session call: handlers hand
  it a closure via `submit` and **block** on a `Future` (`session_actor.py:127-137`).
  Reads and writes alike funnel through this one thread (the salsa/MVCC constraint,
  `session_actor.py:1-13`). This is why the cursor path debounces
  (`context.lua:105`) and dedupes on the enclosing-node key (`context.lua:90`) and
  stale-drops superseded responses (`context.lua:61`).
- **But notification *delivery* is concurrent.** The `BusPump` does **not** go
  through the actor to deliver — it calls `subscribe` once *through* the actor (so
  the lazy bus build serialises) and then polls a **thread-safe subscription
  queue** off its own worker thread (`bus_pump.py:10-14`, `:90-110`). So delta and
  refinement broadcasts never contend with reads/writes. The precision refiner is
  likewise a separate daemon thread that opens its own frozen snapshot
  (`refiner.py:78`, `:149`). The serialization bottleneck is therefore specifically
  **synchronous request/response reads + writes**, not the push channel.
- **Reads run over a frozen snapshot** (`session._native()` caches
  `snapshot(None)`, `session.py:293-301`; memory `session-reads-via-frozen-snapshot`).
  Snapshots are independent (own Zalsa), so holding one does not block a writer
  (`session.py:303-311`), and a single snapshot is thread-shareable
  (`views.py:Snapshot` docstring). **This is the latent throughput win that is not
  exploited:** the actor serializes reads that *could* run concurrently on a shared
  pinned snapshot off the owner thread.
- **`entity_at` does N derived/authored reads per card** (`_entity_dict` loops all
  layers), and `session.derived` **opens and closes a fresh snapshot per call**
  (`session.py:707-718`). For a card with several layers that is several snapshot
  pin/unpin cycles on the serial actor. A cursor move with `precision=method` and
  several layers is the worst case.
- **A slow generator blocks everyone.** A cache-miss `derived` recomputes
  synchronously at read (`views.py:244`); for an LLM/HTTP generator that is a
  multi-second stall on the one actor thread. There is a scheduler and eager
  recompute, but the read path's self-heal is synchronous.
- **Opportunities:** (a) batch the card's per-layer reads over **one** shared
  snapshot (already feasible — `_entity_dict` could open one snapshot and pass it
  down instead of `s.derived`/`s.authored` each opening their own). (b) Serve
  reads from a shared frozen snapshot on a read pool, reserving the actor for
  writes. (c) Make slow-generator derived reads return `stale|absent` immediately
  and recompute async (the refinement-channel machinery already exists).

---

## 9. Value typing

**Support today: none (free-form). Distance: medium; flagged secondary.**

- Authored values are arbitrary JSON (`authored.rs:22` `serde_json::Value`;
  `session.author` does `json.dumps(value)` `session.py:747`). Derived artifacts
  are `bytes` (`generators.py` returns `list[bytes]`; the card renders them as a
  utf-8 string, `handlers.py:_artifact_str`). The only convention is
  `{"note": text}` / `{"markdown": text}`, decoded by `_note_text`
  (`handlers.py:441-451`) — a *renderer-side guess*, not a contract.
- **What typing would buy:** validation at author-time (reject a malformed
  `tests` link), self-describing cards (the editor could render a typed value with
  the right widget), and a discovery surface (the API in §2 could publish each
  layer's schema so the plugin builds its author UI generically instead of
  hardcoding `intent`). **What it costs:** a schema mechanism (pydantic models or
  JSON Schema) threaded from registration → daemon → editor, plus migration of the
  free-form records. The maintainer flagged this secondary; the assessment agrees
  it is **enabling, not blocking** — the registration API (§2) should *carry an
  optional schema* but not *require* one.

---

## 10. Tests & invariants

**Support today: good for the built-ins. Distance: the API needs new contract
tests.**

- Gate suites protect the contracts: `test_gate2_identity` (binding rules),
  `test_gate5_derived`, `test_gate6_authored`, `test_final_acceptance.py`
  (the V2 end-to-end story — durable ids survive edit+move, content-hash changes
  only on meaningful edits, `affected_ids` transitive, local-vs-semantic recompute
  by call count, `precision=method` narrowing, parity oracle, no read advances
  head, no file mutated). `test_inference_flow_coverage.py` guards the
  "container-granular nominally-complete" soundness claim. The daemon has its own
  protocol/handlers/socket tests (`daemon/tests/`).
- **The parity oracle is intentionally structural-strict + cosmetic-downgradeable**
  (memory `parity-oracle-tiered`) — do not re-tighten.
- **What a registration API would need:** a contract test that a *custom*
  registered layer (authored and derived) (a) appears in `config`/`open`, (b)
  rides the `entity_at` card, (c) invalidates correctly per `key_locality`, (d)
  binds to identity exactly like a built-in (edit/move yes, rename no), and (e) is
  rejected cleanly on a bad declaration. Today's tests cover the built-in three;
  none exercises a layer that didn't ship with the engine.

---

## 11. Rubric

| Subsystem | Support today | Distance to §2 vision | Single highest-leverage change |
|---|---|---|---|
| Identity | **good** | small | Explicit native rename rebind from the editor's rename intent (AB8 — atomic-move routing proven insufficient, Spike D) |
| Layer model | partial→good (reads generic, writes split) | medium | A single `Layer` registration object bundling value+produce+store+invalidate |
| Extension seams | partial (config+string only) | **large** | `register_layer/generator/store` Python API + entry points; stop hardcoding `make_generator`/`open_store` |
| Derived pipeline | **good** (one correctness wall) | small–medium | Producer-with-snapshot protocol **keyed on a traced read-set**; cheap-reverse data served as live RPCs, not cached (references/subclass layers are stale-forever otherwise — Spike C) |
| Daemon/editor surface | partial | medium | Generic `layers`/`author-any`/`layer_value` RPCs **+ expose `convert/`** (references/hover/rename) |
| `convert/` reuse | good in Python, **none at daemon** | small to expose | Add `references`/`hover`/`rename`/`type_hierarchy` RPC verbs |
| Value typing | **none** | medium (secondary) | Optional per-layer schema carried by registration → daemon → editor |
| Performance | partial (serial, frozen-snapshot correct) | medium | One shared snapshot per card; async serve for slow generators |
| Testability | good for built-ins | medium | Contract test for a *custom* registered layer (author + derived) |

---

## 12. Plain-English verdict: how good is the spine for attach-anything today?

**Good enough to prove the concept, and genuinely generic on the half that's hard
to get right — identity, the uniform read protocol, the affected-set/invalidation
machinery, and the auto-enumerating card.** If your new attachment is "some JSON a
human writes" or "some bytes a `module:function` / subprocess / HTTP endpoint
computes from entity text, cached in fs or lancedb, invalidated by content or
dependency hash," you can add it in `config.toml` alone, it inherits durable
identity, it invalidates correctly, and it shows up in the editor card **without
touching engine or plugin code.** That is a real, working spine.

It stops being trivial the instant the attachment needs *new behaviour or new
surface*: a stateful/engine-aware generator, a custom store, a typed value, a
"find callers"/"rename" capability the engine already computes but the daemon
doesn't expose, or a first-class author/edit affordance in the editor. There is
**no programmatic registration API**, custom *types* require editing core
dispatch, and the daemon/plugin write-and-discover surface is hardcoded to the
three built-in layers. The strongest single observation: **the read/serve/card
path is already layer-agnostic, so the §2 vision is mostly about giving the
write/produce/discover path the same generality** — plus turning the stranded
`convert/` surface into either RPCs or first-class derivable layers.
