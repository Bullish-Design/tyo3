# ROADMAP — making TyO3 a flexible "attach-anything" spine

> Prioritized improvement opportunities toward the §2 vision (a programmatic
> Python registration API for custom layers/generators/stores). Framed by
> impact × effort, split into quick wins and architectural bets, sequenced, with
> risks against the §12 landmines. Evidence is in `ASSESSMENT.md`; the API itself
> is in `API_DESIGN.md`.

## How to read this

- **Impact** = how much it advances "attach a new kind of data trivially" and the
  interactive-nvim workflow.
- **Effort** = rough size (S < 1 day, M a few days, L 1–2 weeks, XL more).
- Each item names the **files it touches** and the **§12 landmine** it must
  respect (Rust owns committed truth; reads over a frozen snapshot; tiered parity
  oracle; identity binding rules; single-threaded actor; keep
  `opt-level=3` on dev deps).

---

## Quick wins (high impact / low effort) — do these first

### QW1. Expose the `convert/` surface as RPC verbs — **impact: high, effort: S–M**
The engine already computes references, hover, rename, type-hierarchy and they are
already on `_ReadOps` (`read_ops.py:135-579`); they are simply absent from the
daemon's `_METHODS` table (`handlers.py:456`). Add `references`, `hover`,
`rename`/`can_rename`, `type_hierarchy`, `document_highlights` handlers that
translate wire ↔ engine exactly like the existing ones.
*Unlocks:* "find callers/neighbors," real go-to-definition (replacing the
`actions.lua:69` heuristic name-search), and the foundation for identity-preserving
rename (QW2). *Risk:* none structural — read-only, runs through the actor like
every other read. *Landmine:* reads must stay on the frozen snapshot path.

### ~~QW2~~ → AB8. Identity-preserving rename — **impact: high, effort: M (small native change)**
> ⚠ **Reclassified from a quick win after `SPIKE_FINDINGS.md` Spike D.** The
> "route the rename through an atomic `edit_many`" idea **does not work**: the
> entity *name is part of the content hash* (`hash.rs:337`), so a rename changes
> both the hash *and* the qualified_path, and rule-3 `Struct` requires the *same
> name* — all three reconcile keys miss ⇒ the id is **minted fresh** and the note
> is lost (verified: `created=[new] deleted=[old]`, note orphaned→absent). An
> atomic commit doesn't help; reconciliation **cannot infer a rename by
> construction.**

Rename-survival requires an **explicit rename intent** carried from the editor
(which *knows* it is renaming — it is invoking LSP rename) to a deliberate native
rebind: a `Mutation::Rename { id, new_name }` (or a rebind hint on the commit)
that re-keys the existing `DurableId` instead of letting reconciliation mint a new
one. The editor flow: `can_rename`/`rename` (QW1) computes the workspace edit, and
the daemon applies it **with the id-preservation hint** so the anchor follows.
*Risk:* this is the *one* place that must touch Rust (a new mutation) — keep it an
explicit, caller-asserted rebind, **never** a matcher heuristic (a "name changed,
same neighbourhood" auto-rule risks mis-binding swapped names and breaks the
deterministic-matcher landmine §12). *Landmine:* identity rules; Rust owns
committed truth (this is a legitimate native addition, not new code *semantics*).

### QW3. A `layers` discovery RPC + generic `author`/`layer_value` — **impact: high, effort: S**
`open` returns only layer *names* (`handlers.py:88`). Add a `layers` verb that
returns, per layer: `name`, `origin`, `entity_kinds`, `history`,
`review_on_change`, (and, once typed, the value schema). Then the plugin can build
its author UI generically instead of hardcoding `intent`/`docs`
(`notes.lua:12`, `actions.lua:37`, `entitydoc.lua:14`). `author` already takes a
layer string — it just needs a discoverable menu. *Unlocks:* "author any layer"
without per-layer Lua. *Risk:* none. *Landmine:* none.

### QW4. One shared snapshot per card — **impact: medium, effort: S**
`_entity_dict` (`handlers.py:304`) calls `s.authored`/`s.derived` per layer, and
`session.derived` opens+closes a fresh snapshot each call (`session.py:707-718`).
Open **one** snapshot in `_entity_dict` and read every layer off it. Removes N
pin/unpin cycles per cursor move on the serial actor. *Risk:* low. *Landmine:*
frozen-snapshot reads (this *is* the snapshot path — just shared).

### QW5. Generic decoration over the preferred-layer hardcode — **impact: low, effort: S**
`decorate`/`_note_layer`/`_summary_layer` prefer `intent`/`summary`
(`handlers.py:347-359`). Let a layer declare `display: inline-note|inline-summary|
panel-only` (config or registration) so a new authored layer can opt into inline
extmarks without editing the handler. *Risk:* none. *Landmine:* none.

### QW6. Fix the renderer status gates — **impact: medium, effort: S (bug)**
The panel SUMMARY pane (`panel.lua:169`) and the compact context card
(`card.lua:124`) gate on `status == "present"`, but **derived statuses are
`fresh|stale|failed|absent`** (`models/derived.py:23`) — so they render *no*
derived artifacts today, and drop authored `needs_review`/`orphaned` records too
(`models/authored.py:30`). The `:TyO3Inspect` float is correct (`card.lua:73-80`).
Treat any non-`absent` (and non-`failed`, for inline) record as renderable, matching
the daemon's own inclusion rule (`handlers.py:328,339`). This is a prerequisite for
"a registered layer surfaces everywhere" — without it a new derived layer shows in
the float but not the panel. *Risk:* none. *Landmine:* none.

### QW7. A batch "ids-with-records-in-layer" verb — **impact: medium, effort: S**
The authored picker fires one `authored` RPC **per id** (`telescope.lua:146`) — N
enqueued reads on the serial actor for one picker. Add a `layer_ids(layer)` verb
returning the ids that have a present record (the read views already expose
`ids()` per `LayerView`, `layers/base.py:45`). Generic over any registered layer.
*Risk:* none. *Landmine:* one shared snapshot for the scan.

---

## Architectural bets (high impact / higher effort)

### AB1. The programmatic registration API — **impact: very high, effort: L** (the headline)
Implement `register_layer(...)`, `register_generator(...)`, `register_store(...)`
plus `importlib.metadata` entry-point discovery, as designed in `API_DESIGN.md`.
This replaces config-string indirection with real Python objects (lifecycles,
deps, optional schema) and removes the two hardcoded dispatches
(`make_generator` `generators.py:53`, `open_store` `stores/__init__.py:22`) in
favour of registries seeded by built-ins + plugins.
*Sequencing:* land the registries behind the existing config first (config entries
resolve *through* the registry), so built-ins keep working and the API is purely
additive (see migration in API_DESIGN §6). *Risk:* the layer set is currently
fixed at `open()` and validated in Rust (`config.rs::validate`); registration must
either run *before* `open()` or register a *Python overlay* of layers the Rust
validator never sees — decide explicitly (API_DESIGN §3 picks pre-open
registration + a Python-side layer table merged with the validated native config).
*Landmine:* **Rust owns committed truth** — registration adds Python-side
*authored/derived* layers (which already live in Python); it must NOT try to teach
Rust new code-layer semantics.

### AB2. A richer producer protocol **with traced read-sets** — **impact: high, effort: M–L**
Today `Generator.generate(inputs) -> list[bytes]` gets only entity text + kind
(`generators.py:42`, `dag.py:_entity_source`). Define a `Producer` protocol whose
input is a **read handle to the pinned snapshot** (so a generator can ask for the
AST, type, references, supertypes — i.e. the `convert/` surface — and sibling
entities). This is what makes "a `tests` layer that matches names," an LLM summary
over an entity's call-sites, etc. expressible as ordinary derived layers.

> ⚠ **`SPIKE_FINDINGS.md` Spike C found a correctness wall — and the fix is a
> taxonomy, not just a new enum value.** A layer whose value depends on an entity's
> *dependents* (references/callers/"my subclasses") is **stale-forever** today: the
> affected set is the *dependents*-closure of a change and never flows to a change's
> *dependencies*, and the `semantic` fingerprint walks *forward* deps only — so a
> new caller invalidates *nothing* on the callee (verified: 0 recompute on read).
> The right response depends on **cost × direction**:
>
> | | Forward (depends on what I call) | Reverse (depends on who calls me) |
> |---|---|---|
> | **Cheap** | `local` ✅ today | **live RPC, not a layer** (references, diagnostics) — QW1 |
> | **Expensive** | `semantic` ✅ today | needs real reverse keying (LLM-over-callsites) |
>
> 1. **Cheap reverse data should not be a cached layer at all.** The engine
>    recomputes `find_references`/diagnostics in ms (Spike E); caching buys nothing
>    and the invalidation is the hardest in the system. **Serve them via QW1 live
>    RPCs and document that explicitly** — this dissolves ~90% of Spike C at the
>    lowest effort and highest correctness.
> 2. **For the expensive-reverse quadrant, key on a *traced read-set*, not a
>    direction enum.** Make the `ProduceContext` snapshot handle **record every id
>    the producer touches** (`find_references`, `symbol`, graph walks) and fingerprint
>    *exactly that set* into the cache key. This is correct-by-construction (it is
>    salsa's own model — and ty/salsa is the stack), keys `complexity`/`summary`/
>    `embedding`/references *uniformly*, and makes the locality enum stop being
>    load-bearing. Lazy read then self-heals whenever any traced id changes.
> 3. **`reverse-semantic` (direct, lazy, memoized) only as an interim** if (2)
>    slips: fingerprint *direct* reverse edges (the maintained `reverse_deps` index),
>    **not** the transitive cone (which thrashes on hot symbols — a base class with
>    300 dependents would recompute on any of them); **lazy serving only** (no eager
>    reverse — eager + central symbol = a recompute storm); **memoize the fingerprint
>    per snapshot**. Handle `deleted_ids` (a deleted caller shrinks the set too).

*Greenfield, so no migration risk:* the existing config-string layers keep
`local`/`semantic` untouched; traced read-sets are the default only for the *new*
registration API. *Risk:* must run over the **frozen snapshot** (not live head) to
stay correct and not perturb writers (`session-reads-via-frozen-snapshot`).
*Landmine:* frozen snapshot; invalidation keying (`phase8`). *Avoid:* the
"expand the commit dirty-set with deps-of-each-changed-id" invalidator hack as the
primary mechanism — it over-fires and gives no lazy self-heal.

### AB3. Async serve for slow producers — **impact: medium-high, effort: M**
A cache-miss `derived` recomputes synchronously at read (`views.py:244`), stalling
the single-threaded actor on LLM/HTTP generators. Make slow layers serve
`stale|absent` immediately and recompute on the existing refinement/worker
channel, publishing when ready (the `PrecisionRefiner`/bus machinery already
exists, `session.py:601-629`). *Risk:* honest staleness semantics already exist
(`views.py` status model) — keep them. *Landmine:* single-threaded actor (the
worker must be off it); bus overflow policy stays non-blocking (`config.rs:464`).

### AB4. Read concurrency: serve reads off the actor — **impact: medium, effort: L**
Snapshots are independent and thread-shareable (`session.py:303`, `views.py`
Snapshot docstring), yet every read serializes on the actor. Let read RPCs run on
a small pool against a shared pinned snapshot, reserving the actor for writes.
*Risk:* must preserve revision ordering for writes and the "no read advances head"
invariant; needs care around snapshot lifetime. *Landmine:* single-threaded
*writes* must stay single-threaded; reads over frozen snapshots are already safe to
share — this bet leans on exactly that property.

### AB5. Optional per-layer value schemas — **impact: medium (secondary), effort: M**
Carry an optional schema (pydantic model / JSON Schema) on a registered layer;
validate at author-time; surface via the `layers` RPC (QW3) so the editor renders
typed values with the right widget. Free-form stays the default (don't force it).
*Risk:* migration of existing free-form records — make schema *opt-in* and
validate only when declared. *Landmine:* none (authored values are already
Python-JSON; this adds a validation step, not a storage change).

### AB7. Per-layer subscription streaming — **impact: medium, effort: S–M**
The bus already has the primitive: `Interest.layer(name)` / compound filters with a
`touched_layers` match arm (`interest.py:44-47,79`). Today the daemon subscribes
only `Interest.ALL` (`bus_pump.py:65`). Populate `touched_layers` on the published
`Delta` and expose a `subscribe(interest)` RPC (or per-connection interest
registration) so a client can say "stream me when any `summary`/`embed` recomputes
for these ids/files." This makes a registered layer's *updates* pushable, not just
its *values* pullable. *Risk:* keep overflow non-blocking (`config.rs:464`).
*Landmine:* single-threaded actor — delivery already runs off it (`bus_pump.py`),
so this rides the existing concurrent push channel.

### AB6. Custom store backends as a registry — **impact: medium, effort: M**
Fold AB1's `register_store` so `open_store` (`stores/__init__.py:22`) dispatches
through a registry, making sqlite-vec/qdrant/custom KV first-class instead of the
current `not implemented` stubs (`stores/__init__.py:39-46`). Also finish fs-store
GC (`dag.py:_gc_store` is a documented no-op, `:355`). *Risk:* low. *Landmine:*
none.

---

## Sequencing

```
Phase A (days, ship independently — immediate workflow value):
  QW1 convert/ RPCs           QW3 layers discovery + generic author
  QW4 shared snapshot per card QW5 declarative decoration
  QW6 fix renderer status gates (bug; unblocks "registered layer surfaces everywhere")
  QW7 batch layer_ids verb
  (QW2 was here — reclassified to AB8 after Spike D: rename needs a native rebind)

Phase B (the registration spine — the §2 headline):
  AB1 registration API + entry points   (built-ins resolve through it)
     └─► AB6 store registry (subsumed by AB1)
     └─► AB5 optional schemas (carried by registration)
  AB7 per-layer subscription streaming  (primitive already in the bus)

Phase C (make the spine powerful + fast):
  QW1 ──► AB8 identity-preserving rename (native Mutation::Rename; uses QW1's edit)
  QW1 ──► cheap reverse data (references/diagnostics) = live RPCs, NOT layers
  AB2 producer protocol w/ snapshot access + TRACED read-set keying
        ──► "tests / LLM-over-callsites as ordinary layers" (correct-by-construction)
  AB3 async serve for slow producers
  AB4 read concurrency off the actor
```

Phase A is pure additive RPC/UX value and needs none of the API work — it makes
the *existing* spine feel complete in the editor and de-risks AB2 (it proves the
`convert/` data is what the workflow wants). Phase B is the registration API.
Phase C makes "attach anything" *powerful* (engine-aware producers) and *scalable*
(async + concurrent reads).

---

## Risks vs. the §12 landmines (consolidated)

| Landmine | Items that touch it | Guardrail |
|---|---|---|
| Rust owns committed truth | AB1 | Registration adds **Python** authored/derived layers only; never teach Rust new code semantics. Native config stays the validator for what it owns. |
| Reads over a frozen snapshot | QW1, QW4, AB2, AB4 | All producer/read access goes through `snapshot()`, never live head; AB2's producer gets a *frozen* read handle. |
| Tiered parity oracle | (none should retighten) | No item changes graph parity; AB2 must keep derived values content-addressed so the oracle is untouched. |
| Identity binding rules | AB8, AB1 | AB8 uses an **explicit** native rename rebind (Spike D proved reconciliation can't infer a rename); **never** loosen the matcher. A registered layer inherits identity exactly — add a contract test (ASSESSMENT §10). |
| Reverse-dep staleness | AB2, QW1 | Cheap reverse data (references/diagnostics) → **live RPCs, not layers** (QW1). Expensive-reverse layers key on a **traced read-set** (correct-by-construction), with `reverse-semantic` (direct, lazy, memoized) only as an interim (Spike C). |
| Single-threaded actor | AB3, AB4 | Writes stay serialized; only reads/slow-producers move off the actor, leaning on independent shareable snapshots. |
| `opt-level=3` dev deps | (none) | No build-profile changes proposed. |

---

## What NOT to do

- Don't re-tighten the parity oracle or move the spine back into Python (§12).
- Don't try to make the matcher survive renames by loosening its rules — it is
  deterministic on purpose, and Spike D proved a rename perturbs all three keys it
  indexes on, so any inference rule is a guess. Solve rename with an **explicit**
  native rename intent (AB8), not a heuristic.
- Don't make value typing mandatory — free-form is the right default; schemas are
  opt-in (AB5).
- Don't block the writer on slow generators or bus subscribers (the config already
  forbids a blocking overflow policy, `config.rs:464` — keep async serve off the
  actor).

---

## Baseline note (honesty about the current branch)

`devenv shell -- test-fast` on `nvim-plugin`: **681 passed, 1 failed, 1 error**
(145s). The single failure —
`test_gate4_config_sidecar::test_sidecar_is_sole_path_owner_in_source` — is
**pre-existing and unrelated to this review**: `demo/tour.py:175`
(`cfg = root / ".tyo3"`, committed at `05e609a`) constructs a `.tyo3` path, which
an over-strict invariant test wants confined to `sidecar.{rs,py}`. The
`test_concurrency::...test_document_symbols` error is a separate fixture issue.
Neither touches the spine/extensibility code under review; both predate it. (This
is itself a tiny roadmap item: either move the demo's path construction behind the
`Sidecar` helper or exempt `demo/` in the invariant test.)
