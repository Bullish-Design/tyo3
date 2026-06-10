# IMPLEMENTATION_GUIDE — building the extensible TyO3 spine

> A step-by-step build plan for everything in `ROADMAP.md` + `API_DESIGN.md`,
> written for a new contributor ("the intern"). Each task is self-contained:
> **Goal → Why → Files → Steps (with code skeletons) → Test → Acceptance →
> Landmines → Estimate.** Do the tasks in the order given; later tasks assume the
> earlier ones. Read `ASSESSMENT.md` once for context and keep `SPIKE_FINDINGS.md`
> open — it explains *why* two of these tasks are shaped the way they are.

---

## 0. Read me first

### 0.1 Environment & the build/test loop (NON-NEGOTIABLE)

Everything runs through **devenv** — never call `pytest`/`cargo` directly.

```bash
devenv shell -- build            # build the Rust .so + copy into the package (run after ANY rust/ change)
devenv shell -- test-fast        # fast Python suite (parallel, no coverage) — your inner loop
devenv shell -- test-final       # the end-to-end acceptance suite (run before a PR)
devenv shell -- clippy           # rust lints (-D warnings) — must be clean before a rust PR
devenv shell -- pytest src/tyo3/daemon/tests --no-cov   # daemon-only tests
```

- Pure-Python change → just `test-fast` (the `.so` is already built).
- Any `rust/src/**` change → `build` **first**, then `test-fast`.
- Suites are slow (~2–3 min). Run full suites in the background; budget ~15 min.
- Lua smoke test:
  ```bash
  nvim --headless --clean -u editors/tyo3.nvim/tests/minimal_init.lua \
       -c "luafile editors/tyo3.nvim/tests/smoke.lua"
  ```
- A standing **manual probe harness** lives at
  `.scratch/projects/21-spine-extensibility-review/spikes/run_spikes.py` — copy its
  project-setup helpers when you need to exercise the engine by hand.

### 0.2 Repo map (where you'll work)

```
rust/src/
  config.rs            # config schema + validate()  (touch: AB1 authored-layer shim)
  identity.rs          # IdentityRegistry, Binding rules, rebind()  (touch: AB8)
  project/commit.rs    # Mutation enum, build_plan()  (touch: AB8)
  project/methods.rs   # PyTyProject methods exposed to Python  (touch: AB1, AB8)
src/tyo3/
  config.py            # TyConfig / LayerConfig (read-only mirror of native config)
  derive/{generators,dag,layer,scheduler,cache}.py   # derived pipeline (AB2, AB6)
  stores/{__init__,base,fs}.py                        # store registry (AB6)
  session/{session,views,read_ops}.py                # engine facade + reads
  extend.py            # NEW — the registration API (AB1)
  daemon/handlers.py   # the RPC table _METHODS  (QW1, QW3, QW7)
  daemon/{server,session_actor,bus_pump,tracking}.py # daemon plumbing (AB4, AB7)
  tests/               # add contract tests here
editors/tyo3.nvim/lua/tyo3/
  card.lua panel.lua   # renderers  (QW5, QW6)
  actions.lua notes.lua telescope.lua  # author/picker sites  (QW3)
  rpc.lua init.lua     # JSON-RPC client + lifecycle
```

### 0.3 The golden rules (violating these breaks invariants — see ASSESSMENT §12)

1. **Rust owns committed truth.** Don't move spine logic into Python. New
   *authored/derived* layers live in Python (that's where they already live);
   never teach Rust new *code-layer* semantics.
2. **Reads run over a frozen snapshot** (`session.snapshot()` / the head snapshot),
   never the live head DB. Every read path you add must use a snapshot.
3. **One writer.** All session calls funnel through the `SessionActor` thread.
   Don't touch the session from another thread except via `actor.submit(...)`.
4. **Don't re-tighten the parity oracle** and don't change `[profile.dev.package."*"]
   opt-level=3`.
5. **Identity:** edit ✅ / atomic move ✅ / rename ❌ (mints a new id). Only AB8
   changes this, and only via an *explicit* native rename — never by loosening the
   matcher.

### 0.4 How to make a change land

One task ≈ one PR. Each PR: code + tests + green `test-fast` (+ `clippy`/`build`
if Rust). Update `editors/tyo3.nvim/docs/dev/architecture.md`'s RPC list whenever
you add a verb.

---

## PHASE A — quick wins (ship each independently; no registration API needed)

These deliver immediate editor value and de-risk Phase C. Recommended order:
**QW6 → QW1 → QW3 → QW7 → QW4 → QW5.**

---

### TASK QW6 — Fix the renderer status gates *(bug; do this first)*

**Goal.** Make the panel + compact card actually render derived artifacts (and
non-`present` authored records).

**Why.** `SPIKE_FINDINGS.md` Spike F: derived statuses are `fresh|stale|failed|
absent` (`models/derived.py:23`) but `panel.lua:169` and `card.lua:124` only render
`status == "present"`, so **every derived artifact is silently dropped** from the
panel/compact views. This must be fixed before any new layer can "surface
everywhere."

**Files.** `editors/tyo3.nvim/lua/tyo3/panel.lua`, `editors/tyo3.nvim/lua/tyo3/card.lua`.

**Steps.**
1. In `card.lua` `context_lines` (~line 123) change the derived loop guard from
   `if rec.status == "present"` to render any artifact present and not failed:
   ```lua
   for layer, rec in pairs(card.derived or {}) do
     if rec.status ~= "absent" and rec.status ~= "failed" then
       local art = derived_artifact(rec)
       if art then add("  ⟢ " .. art) end
     end
   end
   ```
2. In `card.lua` authored loop (~line 116): render `rec.status ~= "absent"`
   (so `needs_review`/`orphaned` show, ideally with a marker, e.g. append ` ⚠` when
   `rec.status == "needs_review"`).
3. Mirror both in `panel.lua` `summary_rows` (~166), `note_rows` (~130), and the
   `count_of` helper (~190) — replace `== "present"` with `~= "absent"` (skip
   `failed` for derived).
4. Keep `:TyO3Inspect`'s `build_lines` as-is (already correct).

**Test.** Run the spike harness — it already prints `derived status = 'fresh'`.
Then headless: open a project with a derived `summary` layer, move the cursor onto
an entity, confirm the SUMMARY pane shows the artifact. Lua smoke test passes.

**Acceptance.** A derived artifact with status `fresh`/`stale` appears in the panel
SUMMARY pane and the compact context card. A `needs_review` note still shows.

**Landmines.** None. Pure Lua.  **Estimate.** 0.5 day.

---

### TASK QW1 — Expose the `convert/` surface as RPC verbs

**Goal.** Add `references`, `hover`, `rename`/`can_rename`, `type_hierarchy`,
`document_highlights`, `diagnostics_at` to the daemon RPC table.

**Why.** ASSESSMENT §7: these are fully built and already on `_ReadOps`
(`read_ops.py:135-579`) but absent from the daemon `_METHODS` (`handlers.py:456`).
The plugin currently fakes go-to-def with a regex search (`actions.lua:69`). This
also unblocks AB8 (rename) and the "references = live RPC" resolution of Spike C.

**Files.** `src/tyo3/daemon/handlers.py`, `src/tyo3/daemon/tests/test_handlers.py`,
`editors/tyo3.nvim/lua/tyo3/{actions,init}.lua`, `editors/tyo3.nvim/docs/dev/architecture.md`.

**Steps.**
1. In `handlers.py`, follow the existing handler pattern (see `entity_at`,
   `handlers.py:129`). Each verb: require params, translate path with
   `self._relpath`, run on the actor, dump models to JSON. Example:
   ```python
   def references(self, params):
       path = _require(params, "path", str); line = _require(params, "line", int)
       col = _require(params, "col", int); rel = self._relpath(path)
       def work(s):
           return {"references": [
               {"path": r.path, "range": _range_dict(r.range), "role": getattr(r, "role", None)}
               for r in s.find_references(rel, line, col)
           ]}
       return self._actor.submit(work)
   ```
   Note positions are **1-based** on the wire (see `_range_dict`, `handlers.py:401`).
2. Add `hover`, `type_hierarchy`, `document_highlights`, `can_rename`, `rename`
   the same way (delegating to the matching `_ReadOps` method). `rename` returns a
   `WorkspaceEdit` — serialise its `changes` as `{path: [{range, new_text}]}`.
3. Add `diagnostics_at(path,line,col)`: call `s.check_file(rel)` and filter
   diagnostics whose range contains the position (cheap-reverse → live, per Spike C
   taxonomy; do NOT cache).
4. Register every new function in the `_METHODS` dict (`handlers.py:456`).
5. Wire the plugin: in `actions.lua`, replace `goto_def`'s `locate`+`vim.fn.search`
   heuristic with a `references`/`goto_definition`-backed jump. Add a "Find
   callers" action that calls `references`.
6. Update the RPC list in `architecture.md` and `docs/dev/`.

**Test.** Add daemon tests in `test_handlers.py` mirroring the spike: a 2-file
project where `references(lib.py, target)` returns the call site in `app.py`
(Spike E proved this works). `devenv shell -- pytest src/tyo3/daemon/tests --no-cov`.

**Acceptance.** `references`/`hover`/`rename`/`type_hierarchy` reachable over the
socket; plugin "find callers" works; `ping`'s method list grows.

**Landmines.** Reads only — keep them on the actor + snapshot path. Don't add a
*cached* references layer (Spike C).  **Estimate.** 1.5 days.

---

### TASK QW3 — Layer discovery RPC + generic author

**Goal.** A `layers` verb that describes every declared layer; make the plugin
author/render any layer without hardcoding `intent`/`docs`.

**Why.** `open` returns only layer *names* (`handlers.py:88`), so the plugin
hardcodes `intent` at every author site (`notes.lua:12`, `actions.lua:37`,
`telescope.lua:146`). For "attach anything" the editor must *discover* layers.

**Files.** `src/tyo3/daemon/handlers.py`, the nvim author/picker modules,
`test_handlers.py`.

**Steps.**
1. Add a `layers` handler returning, per layer from `s.config.layers`:
   ```python
   {"name": n, "origin": c.origin, "entity_kinds": list(c.entity_kinds),
    "history": c.history, "review_on_change": c.review_on_change,
    "display": getattr(c, "display", "panel")}   # display added in QW5/AB1
   ```
   (Authored layers are the writable ones — the editor offers "author" only for
   `origin == "authored"`.)
2. In `actions.lua`, build the "Author …" action list from the `layers` result
   (one entry per authored layer) instead of the single hardcoded `author_note`.
   Keep `intent` as the default/first if present.
3. Make `notes.lua` / `telescope.lua` take the layer as a parameter (default
   `intent`) rather than a literal.

**Test.** Daemon test: `layers` lists a config with `tests`(authored) +
`summary`(derived) and reports correct origins. Spike A's project is a ready fixture.

**Acceptance.** Adding an authored layer in config makes it appear in the plugin's
"Author …" menu with no Lua change.

**Landmines.** None.  **Estimate.** 1 day.

---

### TASK QW7 — Batch `layer_ids` verb

**Goal.** One RPC that returns the ids having a record in a layer (replaces the
per-id loop).

**Why.** The authored picker fires one `authored` RPC **per id**
(`telescope.lua:146`) — N enqueued reads on the serial actor.

**Files.** `handlers.py`, `telescope.lua`, `test_handlers.py`.

**Steps.**
1. Add `layer_ids(layer)`: open one snapshot, use the layer view's `ids()`
   (`layers/base.py:45` — `snap.layer(name).ids()`), return the list. One snapshot,
   one actor hop.
   ```python
   def layer_ids(self, params):
       layer = _require(params, "layer", str)
       def work(s):
           with s.snapshot() as snap:
               return {"layer": layer, "ids": sorted(snap.layer(layer).ids())}
       return self._actor.submit(work)
   ```
2. Rewrite `telescope.authored()` to call `layer_ids("intent")` then fetch values
   (or extend `layer_ids` to also return values for small layers).

**Test.** Daemon test: author two notes, `layer_ids("intent")` returns exactly
those two ids.

**Acceptance.** The picker makes O(1) actor hops, not O(entities).

**Landmines.** Use one shared snapshot (golden rule #2).  **Estimate.** 0.5 day.

---

### TASK QW4 — One shared snapshot per card

**Goal.** `entity_at`'s card reads every layer off **one** snapshot.

**Why.** `_entity_dict` (`handlers.py:304`) calls `s.authored`/`s.derived` per
layer, and `session.derived` opens+closes a fresh snapshot **per call**
(`session.py:707-718`) — several pin/unpin cycles per cursor move on the serial
actor.

**Files.** `src/tyo3/daemon/handlers.py` (and optionally a `Snapshot`-based read
helper in `session/views.py`).

**Steps.**
1. Change `_entity_dict(self, s, did)` to `_entity_dict(self, snap, did)` taking a
   `Snapshot`. Inside, use `snap.authored(layer, did)` and `snap.derived(layer,
   did)` (both exist on `Snapshot`, `views.py:325`, `:206`) and `snap.locate`/graph
   off the same snapshot.
2. In `entity_at`'s `work(s)`, open one snapshot, resolve the id, call
   `_entity_dict(snap, did)`, close in `finally`.
3. `decorate` (`handlers.py:145`) similarly: one snapshot for the whole file walk.

**Test.** `test_handlers` still green; add an assertion that a multi-layer card
matches the per-call version. Optionally count snapshot opens via a debug counter.

**Acceptance.** A card with K layers opens 1 snapshot, not K.

**Landmines.** Don't leak the snapshot — `with`/`finally`. Don't change card shape
(the plugin depends on it).  **Estimate.** 0.5 day.

---

### TASK QW5 — Declarative inline decoration

**Goal.** A layer declares how it decorates (`display`) instead of the handler
hardcoding `intent`/`summary`.

**Why.** `_note_layer`/`_summary_layer` (`handlers.py:347-359`) prefer literal
names. A new authored layer can't opt into inline extmarks without editing the
handler.

**Files.** `config.py` + `rust/src/config.rs` (add the field) **or** carry it via
AB1's registration (preferred — do QW5 *after* AB1 if you can); `handlers.py`;
`decorate.lua`.

**Steps (config route, if before AB1).**
1. Add `display: Option<String>` to `LayerCfg` (`config.rs:244`), default `panel`;
   project into `LayerConfig` (`config.py:24`). Rebuild.
2. In `decorate`/`_note_layer`/`_summary_layer`, pick layers by
   `lcfg.display == "inline-note"` / `"inline-summary"` instead of name.
3. `decorate.lua` already renders `item.note`/`item.summary`; no change if the
   handler keeps those keys.

**Test.** Config a non-`intent` authored layer with `display="inline-note"`;
`decorate` returns its text inline.

**Acceptance.** Inline decoration is driven by `display`, not layer name.

**Landmines.** `deny_unknown_fields` in `config.rs` — add the field to the struct
or parsing fails.  **Estimate.** 0.5 day (config) — fold into AB1 if possible.

---

## PHASE B — the registration spine (the §2 headline)

Order: **AB1 (with AB6 folded in) → AB5 → AB7.**

---

### TASK AB1 — The programmatic registration API

**Goal.** `tyo3.extend` with `register_layer/register_generator/register_store` +
`importlib.metadata` entry-point discovery, so custom layers are real Python
objects, not config strings. Built-ins resolve *through* the registry.

**Why.** ASSESSMENT §4: today there is no registration path; custom generator
*types* require editing `make_generator` and store *backends* require editing
`open_store`. This is the centerpiece.

**Files.** NEW `src/tyo3/extend.py`; `src/tyo3/derive/{generators,dag}.py`;
`src/tyo3/stores/__init__.py`; `src/tyo3/session/session.py`;
`rust/src/project/methods.rs` + `rust/src/config.rs` (the one native shim);
new `src/tyo3/tests/test_registration.py`.

**Steps.**
1. **Build the registries (refactor-first, behaviour-identical).**
   - In `extend.py`, create process-global dicts:
     `_LAYERS: dict[str, AuthoredLayerSpec|DerivedLayerSpec]`,
     `_GENERATORS: dict[str, Callable[[GeneratorConfig], Generator]]`,
     `_STORES: dict[str, StoreFactory]`, plus `register_*` functions
     (idempotent; `override=False` raises on dup). Copy the dataclasses from
     `API_DESIGN.md` §2.2/§2.3/§2.5.
   - Seed `_GENERATORS` with the existing `python`/`command`/`http` and `_STORES`
     with `fs`/`lancedb` by **moving** the bodies of `make_generator`
     (`generators.py:53`) and `open_store` (`stores/__init__.py:22`) into
     factories. Then make `make_generator`/`open_store` *dispatch through the
     registry*. Run `test-fast` — must stay green (pure refactor).
2. **Entry-point discovery.** In `extend.py`, `load_plugins()` enumerates
   `importlib.metadata.entry_points(group="tyo3.plugins")` and calls each. Call it
   once, lazily, the first time a `TyO3Session` is constructed (top of
   `session/session.py` `__init__`, guarded by a module flag).
3. **Merge registered layers with the validated native config.** In
   `DerivationDAG.from_session` (`dag.py:39`), after reading `config.layers`, union
   in `_LAYERS` entries whose origin is `derived` (resolving `produce`/`store`
   through the registries instead of config strings). For authored specs see step 5.
   Build an *effective layer table* the rest of the code reads from.
4. **Surface registered layers.** `TyConfig`/`session.config.layers` is the native
   projection; add a thin `session.effective_layers` (native ∪ registered) that
   `handlers._entity_dict`, `layers` (QW3), and the card loops consume. (Don't fake
   entries back into the frozen native config.)
5. **The one native shim for authored layers.** The native `author` validates the
   layer via `head.config.authored_layer_config` (`commit.rs:634`). For a
   *registered* authored layer to be writable, add a native
   `PyTyProject.register_authored_layer(name, history, review_on_change)`
   (`methods.rs`) that inserts a synthesized `LayerCfg{origin: Authored, ...}` into
   the in-memory `ValidatedConfig` (`config.rs`). `session.__init__` calls it for
   each registered authored spec after open. This keeps Rust the validator without
   teaching it new semantics.
6. **Fold in AB6 (store registry).** Already done by step 1 — now
   `register_store("qdrant", factory)` works, and the stubbed `_OPTIONAL_BACKENDS`
   (`stores/__init__.py:39-46`) is replaced by registry lookups. Also finish the
   fs-store GC no-op (`dag.py:_gc_store`, `:355`) while you're here.

**Test.** `test_registration.py`: register an authored `tests` layer and a derived
`complexity` layer *from the test* (no config file), open a session, author + read
`tests`, derive `complexity`, and assert both appear in `entity_at`'s card (port
Spikes A/B). Assert a dup registration raises; assert an entry-point plugin loads.

**Acceptance.** A custom authored + derived layer works end-to-end **registered in
Python, zero config file, zero core dispatch edits**. Built-ins still green.

**Landmines.** Registration must run **before** `open()` freezes the layer table
(golden rule: layer set fixed at open). The native shim adds *config*, never *code
semantics* (golden rule #1). Keep `make_generator`/`open_store` back-compatible.

**Estimate.** 4–5 days (the keystone).

---

### TASK AB5 — Optional per-layer value schemas

**Goal.** A layer may carry a pydantic/JSON-Schema; validate at author-time;
expose via `layers` so the editor renders typed values.

**Why.** ASSESSMENT §9: values are free-form dicts; typing is *enabling* (validation
+ self-describing cards) but must stay **opt-in**.

**Files.** `extend.py` (the `schema` field already on the specs), `session.author`
(`session/session.py:739`), `handlers.layers` (QW3), `test_registration.py`.

**Steps.**
1. In `session.author`, if the layer's registered spec has `schema`, validate the
   value (`schema.model_validate(value)`) **before** the native commit; raise a
   typed error on failure.
2. In the `layers` RPC, include `"schema": spec.schema.model_json_schema() if
   spec.schema else None` so a client can build a typed form.
3. Leave un-schema'd layers exactly as today (free-form).

**Test.** Register `tests` with a `TestLinks` schema; authoring a malformed value
raises; a valid value commits; `layers` returns the JSON Schema.

**Acceptance.** Schema validation fires only when declared; free-form still works.

**Landmines.** Don't make schemas mandatory; don't change storage (still JSON).

**Estimate.** 1.5 days.

---

### TASK AB7 — Per-layer subscription streaming

**Goal.** Clients subscribe to "notify me when layer X / these ids / these files
change," not just `ALL`.

**Why.** ASSESSMENT §6: `Interest.layer(name)` already exists (`interest.py:44`)
with a `touched_layers` match arm (`:79`) — the daemon just subscribes `ALL`
(`bus_pump.py:65`) and the published `Delta` doesn't populate `touched_layers`.

**Files.** `src/tyo3/bus/delta.py` (populate `touched_layers`),
`src/tyo3/daemon/{bus_pump,server,handlers}.py`, `editors/tyo3.nvim/lua/tyo3/init.lua`.

**Steps.**
1. Populate `touched_layers` on the published `Delta` (the set of layers whose
   records changed this commit — authored layers from `authored_ids`, derived
   layers that recomputed). Verify `Interest.matches` then filters correctly.
2. Add a per-connection interest registry in the server: a `subscribe(interest)`
   RPC stores the connection's `Interest`; the `BusPump._emit_delta` broadcasts
   each delta only to connections whose interest matches (use `Interest.matches`
   + `delta.scoped_to`). Keep `ALL` as the default for back-compat.
3. Plugin: optional — register interest for the open buffers' files.

**Test.** Daemon e2e: two subscriptions (`layer("intent")` vs `layer("summary")`);
an authored-intent commit reaches only the first.

**Acceptance.** A client receives only deltas matching its interest.

**Landmines.** Overflow policy stays non-blocking (`config.rs:464`); delivery stays
off the actor (it already is — `bus_pump.py`).  **Estimate.** 2 days.

---

## PHASE C — make the spine powerful + fast

Order: **QW1→cheap-reverse docs, AB2 → AB3 → AB4 → AB8 (advanced, last).**

---

### TASK AB2 — Producer protocol with traced read-sets

**Goal.** A `Producer` whose input is a **recording** snapshot handle; the cache
key is fingerprinted from the ids the producer actually read. This makes
expensive forward *and* reverse layers correct by construction.

**Why.** `SPIKE_FINDINGS.md` Spike C: a references-style layer is *stale-forever*
under `semantic` (forward) fingerprinting. The fix is a taxonomy (§Appendix A):
cheap-reverse → live RPC (QW1, already done); expensive-reverse → **traced
read-set keying**, the default for the new protocol. The current `Generator` only
gets text (`generators.py:42`) so reverse layers are impossible today.

**Files.** `src/tyo3/derive/{generators,layer,dag}.py`, `src/tyo3/extend.py`,
`src/tyo3/session/views.py` (the recording context), `test_registration.py`.

**Steps.**
1. **Build the recording context.** Implement `ProduceContext` (API_DESIGN §2.4) as
   a wrapper around a `Snapshot` that, on each `find_references`/`symbol`/
   `dependents`/`dependencies`/`upstream` call, appends the resolved ids to a
   per-call `read_set: set[str]`. Provide `note_read(ids)` for the raw-snapshot
   escape hatch.
2. **Define `Producer`** (API_DESIGN §2.4) with `produce(ctxs) -> list[bytes|
   BaseModel|None]` + optional `setup`/`teardown`. Adapt the legacy generators:
   wrap `Generator.generate(inputs)->list[bytes]` so its read_set is `{durable_id}`
   (⇒ keys exactly like `local` today — no behaviour change; assert this).
3. **Key on the traced set.** In `DerivationDAG.resolve_input` (`dag.py:210`), when
   a layer uses the default (traced) strategy, run the producer through the
   recording context, collect `read_set`, and compute
   `input_hash = hash(sorted((id, snapshot.symbol(id).content_hash) for id in read_set))`.
   Keep `local`/`semantic`/`reverse-semantic` as override branches (reuse the
   existing forward `_dependency_fingerprint`; add a direct-reverse variant for
   `reverse-semantic` using the maintained reverse edges — lazy-only, memoized per
   snapshot).
4. **Lifecycle.** Call `setup()` once when the DAG builds the layer
   (`dag.from_session`), `teardown()` on `session.close()`.
5. **Wire into registration.** A `DerivedLayerSpec.produce` that is a `Producer`
   object uses the new path; a dotted string keeps the legacy adapter.

**Test.** Port `spike_c2.py` as a contract test with a *real* references producer
(now possible): prime `refs` on the callee, add a new caller, **re-read** → assert
the producer recomputed and the value reflects the new caller (the inverse of
today's stale-forever result). Also assert a `local`-style producer does NOT
recompute on a dependency-only change (no regression).

**Acceptance.** An expensive-reverse layer (e.g. callsite-summary, API_DESIGN
§5.2b) self-heals at read when a caller is added; forward layers unchanged.

**Landmines.** The recording context reads the **frozen snapshot** (rule #2). Don't
fingerprint the *transitive* reverse cone in `reverse-semantic` (thrash — direct
edges only). Don't add eager reverse recompute.  **Estimate.** 4 days.

---

### TASK AB3 — Async serve for slow producers

**Goal.** A slow (LLM/HTTP) layer serves `stale`/`absent` immediately and
recomputes off the actor, publishing when ready.

**Why.** `views.py:244` recomputes synchronously at read (`recompute_now`),
stalling the single-threaded actor for seconds on an LLM generator.

**Files.** `src/tyo3/session/views.py` (`Snapshot.derived`), `src/tyo3/derive/
scheduler.py`, reuse `src/tyo3/precision/refiner.py`'s worker shape +
`bus/refinement.py`.

**Steps.**
1. For a layer declared `serving="stale"` with a slow producer, on a cache miss in
   `Snapshot.derived` (`views.py:233`): return last-good (`stale`) or `absent`
   immediately and **enqueue** a recompute on a background worker (mirror
   `PrecisionRefiner`'s daemon-thread pattern, `refiner.py:74-121`).
2. When the worker finishes, `cache.put` + publish a `refinement`-style "layer X
   id Y now fresh" notification on the bus so the editor re-pulls.
3. `serving="block"` keeps today's synchronous behaviour.

**Test.** A generator that sleeps; assert the read returns promptly (`stale`/
`absent`) and a later read returns `fresh`; assert the actor wasn't blocked.

**Acceptance.** A slow layer never blocks the cursor path.

**Landmines.** Worker runs off the actor (rule #3); honest status model preserved
(`views.py` statuses); bus overflow non-blocking.  **Estimate.** 3 days.

---

### TASK AB4 — Serve reads off the actor

**Goal.** Read RPCs run on a small pool against shared pinned snapshots; the actor
serializes writes only.

**Why.** Snapshots are independent + thread-shareable (`session.py:303`,
`views.py` Snapshot docstring), yet every read serializes on the actor.

**Files.** `src/tyo3/daemon/{session_actor,server,handlers}.py`.

**Steps.**
1. Add `SessionActor.snapshot()` that returns a pinned `Snapshot` (taken on the
   actor thread). Cache the head snapshot; invalidate it when a write commits.
2. Route **read** handlers (`entity_at`, `decorate`, `authored`, `derived`,
   `diff`, `references`, …) to run on a `ThreadPoolExecutor` against a shared head
   snapshot, **not** `actor.submit`. Keep **write** handlers (`sync_buffer(s)`,
   `author`, `reindex`, `gc`) on the actor.
3. Guard snapshot lifetime across the in-flight reads (refcount or "don't close
   until readers drain").

**Test.** Concurrency test: many parallel `entity_at` calls complete without
serializing behind a slow read; a write still commits in order; "no read advances
head" holds (port `test_final_no_read_side_writes.py` assertions).

**Acceptance.** Parallel reads don't queue behind each other; writes stay ordered.

**Landmines.** **Writes must stay single-threaded** (rule #3); reads must use the
frozen snapshot. This is the highest-concurrency-risk task — write the test first.

**Estimate.** 5 days.

---

### TASK AB8 — Identity-preserving rename (native) *(advanced — pair with a maintainer)*

**Goal.** A rename driven from the editor preserves the durable id + its
attachments, via an **explicit** native rename — not matcher inference.

**Why.** `SPIKE_FINDINGS.md` Spike D + `hash.rs:337`: the entity name is part of
the content hash, so a rename changes hash *and* qualified_path *and* name → all
reconcile keys miss → a new id is minted and the note is lost. Routing through an
atomic move does **not** help (verified). The editor *knows* it is renaming, so it
can carry that intent to a deliberate rebind.

**Files.** `rust/src/project/commit.rs` (`Mutation`, `build_plan`),
`rust/src/identity.rs` (`rebind`, `:147`), `rust/src/project/methods.rs` (new
Py method), `src/tyo3/session/session.py`, `src/tyo3/daemon/handlers.py`, the
nvim rename action; `src/tyo3/tests/test_gate2_identity.py`.

**Steps.**
1. **Plumb a rename hint through the commit.** Extend the overlay/commit path with
   an optional `rename_hint: Option<(DurableId, /*new_qualified_path*/ String)>`.
   Add `Mutation::RenameRebind { id, edits, new_qualified_path }` (mirrors
   `Mutation::Overlay`, `commit.rs:537`) that stages the rename text edits.
2. **Rebind during reconcile instead of mint/retire.** In the reconcile/classify
   step, when the hint's `id` would otherwise be retired (old name gone) and a new
   entity appears at `new_qualified_path`, call `IdentityRegistry.rebind(id,
   new_path, new_hash)` (`identity.rs:147` already updates path+hash) and **exclude
   that pair** from `created`/`deleted`. Emit it as a `moved`/`changed` entry.
   Because authored records are keyed by `durable_id`, the note rides automatically
   (no migration needed).
3. **Expose it.** `PyTyProject.rename_entity(id, new_name)` in `methods.rs`:
   compute the workspace edit (the `convert/rename` surface), build the
   `RenameRebind` mutation, commit. Surface as `session.rename_entity(...)` and a
   daemon `rename_entity` verb that the editor calls (instead of applying the LSP
   edit blindly).
4. **Editor.** The rename action calls `rename_entity` (carrying the id from the
   card) rather than a plain multi-file edit.

**Test.** `test_gate2_identity`: author a note on `target`, `rename_entity(id,
"renamed")`, assert **same id**, note status `present` (rides), `created`/`deleted`
empty, and `locate(id)` points at the new name. (This is the exact inverse of
Spike D's current `created=[new] deleted=[old]` result.)

**Acceptance.** A rename via `rename_entity` preserves id + note; a *plain* edit
rename still mints (unchanged — only the explicit path rebinds).

**Landmines.** Do **not** add an automatic "name changed, same neighbourhood"
matcher rule (mis-binds swapped names; breaks determinism — rule #5). The rebind is
caller-asserted only. This touches committed truth — review carefully, run
`clippy` + `test-rust` + `test-final`.  **Estimate.** 5–6 days.

---

## Cross-cutting: contract tests to add

Add to `src/tyo3/tests/` (and `daemon/tests/`) — these protect the new contracts
(ASSESSMENT §10):

1. **`test_registration.py`** — a *custom* registered authored + derived layer:
   appears in `config`/`open`/`layers`; rides the `entity_at` card; invalidates per
   strategy; binds to identity like a built-in (edit/move yes, rename no until AB8);
   bad registration raises. (AB1)
2. **Reverse-dep contract** — a references producer recomputes when a caller is
   added (the AB2 inverse of Spike C). (AB2)
3. **Rename contract** — `rename_entity` preserves id + note. (AB8)
4. **Async-serve contract** — slow layer returns stale-then-fresh without blocking.
   (AB3)
5. **Concurrency contract** — parallel reads don't serialize; writes stay ordered;
   no read advances head. (AB4)
6. **Render contract (daemon-level)** — the card includes derived `fresh`/`stale`
   records (QW6's daemon-side equivalent).

---

## Suggested PR sequence & definition of done

| PR | Task(s) | DoD |
|----|---------|-----|
| 1 | QW6 | panel/card render derived; Lua smoke green |
| 2 | QW1 | convert/ verbs + daemon tests; plugin find-callers |
| 3 | QW3 + QW7 | `layers`/`layer_ids`; plugin author menu generic |
| 4 | QW4 (+QW5 if pre-AB1) | one snapshot per card |
| 5 | AB1 (+AB6) | registration API; `test_registration` green; built-ins unchanged |
| 6 | AB5 | optional schemas |
| 7 | AB7 | per-layer subscriptions |
| 8 | AB2 | producer + traced read-set; reverse contract green |
| 9 | AB3 | async serve |
| 10 | AB4 | read concurrency (write test FIRST) |
| 11 | AB8 | native rename rebind (pair review) |

**Every PR:** `devenv shell -- build` (if rust) → `test-fast` green → `clippy`
clean (if rust) → `test-final` before merge → architecture.md RPC list updated.

---

## Appendix A — the decision rule for layer authors (put this in user docs)

When adding an attachment, classify it by **cost × direction** (Spike C):

| | Forward (value depends on what I call) | Reverse (value depends on who calls me) |
|---|---|---|
| **Cheap** | derived layer, `local` | **live RPC, not a layer** (references, diagnostics) |
| **Expensive** | derived layer, `semantic` | derived layer, **traced read-set** (default) |

- Cheap reverse data (callers, diagnostics) is recomputed in ms — **don't cache
  it**, serve it live (QW1).
- Expensive data: make it a layer; leave `key_locality` unset to get traced
  read-set keying (correct for any direction).
- Human-entered data: `AuthoredLayerSpec` (no generator/store).

## Appendix B — glossary of anchors

- Affected set = **dependents**-closure of a change (flows to callers/subclasses),
  never to a change's dependencies (Spike C).
- Frozen snapshot = `session.snapshot()` / cached head snap; all reads use it.
- Durable id survives edit + atomic move, **not** rename (until AB8).
- `_after_commit` (`session.py:582`) = the one post-commit hook every write funnels
  through (graph apply → derived invalidate → publish → refine).
