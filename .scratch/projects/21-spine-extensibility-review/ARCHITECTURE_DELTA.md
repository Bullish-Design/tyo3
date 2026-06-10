# ARCHITECTURE_DELTA — what the codebase looks like after the work lands

> The end-state of implementing `ROADMAP.md` / `API_DESIGN.md` / `IMPLEMENTATION_GUIDE.md`,
> framed as deltas from today's architecture. Read `ASSESSMENT.md` for the
> starting point and `SPIKE_FINDINGS.md` for why two of these are shaped the way
> they are. Every claim is anchored to a real file.

## The thesis

Today the **read/serve/card** half of the spine is generic, but the
**produce / store / type / author / discover** half is hardcoded (ASSESSMENT §1
calls this the asymmetry). After this work, **both halves are generic** — adding a
new kind of attachment is a registration call, not a core edit. The spine refactor
V2 made Rust own committed truth; this round makes that truth **extensible and
concurrently servable** without disturbing it.

## New components (net-new, don't exist today)

| Component | What it is | Replaces |
|---|---|---|
| `src/tyo3/extend.py` | Registration layer: process-global `_LAYERS`/`_GENERATORS`/`_STORES` registries, `register_*` fns, `importlib.metadata` entry-point discovery | nothing — there is no plugin seam today |
| **Effective layer table** | native-validated config layers ∪ Python-registered layers, consumed by every read/serve/card/daemon path | direct reads of `config.layers` (`config.py:24`) |
| **`Producer` + recording `ProduceContext`** | producer with frozen-snapshot access whose reads are *traced* and fingerprinted into the cache key (salsa-style auto-deps) | `Generator.generate(text)->bytes` (`generators.py:42`) |
| **Async derived worker + daemon read pool** | new execution contexts beside the single actor thread | everything serialized on `SessionActor` |

## Subsystems that change shape

**1. Extension model: config-string indirection → programmatic registration.**
`make_generator` (`generators.py:53`) and `open_store` (`stores/__init__.py:22`)
stop being hand-edited `if/elif` dispatchers and become registry lookups *seeded*
with the built-ins. `config.toml` becomes **sugar over the registry**, not the only
door. Custom generator *types* and store *backends* stop requiring core edits
(closes the §4 "four hardpoints").

**2. Derived pipeline: fixed locality enum → traced read-sets.**
`resolve_input` / `_dependency_fingerprint` (`dag.py:210`/`:288`) gain a default
keying strategy driven by *what the producer touched*; `local`/`semantic` demote to
fast-path overrides. This is a new invalidation **mechanism** — it is what makes
reverse-direction data (references-as-data) correct by construction instead of
stale-forever (SPIKE_FINDINGS.md Spike C).

**3. Daemon concurrency: one actor for everything → write-actor + read-pool + async worker.**
The biggest runtime change. Today every call serializes on `SessionActor`
(`session_actor.py:127`). After AB3/AB4: **writes** stay serialized on the actor;
**reads** run concurrently on shared frozen snapshots (already independent +
thread-shareable, `session.py:303`, `views.py` Snapshot); **slow producers**
recompute off-thread and publish when ready. The bus gains interest-scoped
subscriptions (the unused `Interest.layer()` primitive, `interest.py:44`) instead of
`ALL`-only (`bus_pump.py:65`).

**4. RPC surface: ~14 verbs → ~22, split by a query/layer taxonomy.**
The stranded `convert/` surface (references/hover/rename/type-hierarchy/diagnostics,
ASSESSMENT §7) is exposed as **live query verbs**; layers gain **discovery + generic
author** verbs (`layers`, `layer_ids`). The architectural rule that emerges and is
documented for layer authors: *cheap reverse data is a live query; expensive data is
a cached layer* (SPIKE_FINDINGS.md Spike C taxonomy).

**5. Identity: implicit-only binding → implicit + one explicit operation.**
A new native `Mutation::RenameRebind` (`commit.rs`) and a deliberate rebind path in
reconcile (`identity.rs:147` `rebind` already updates path+hash). The model goes
from "edit/move preserve, rename mints" to "edit/move/**explicit-rename** preserve."
This is the *one* change to committed-truth semantics (plus the authored-layer
config shim) — everything else is additive.

**6. Data model: free-form → optionally typed.**
Authored/derived values gain an optional schema dimension (pydantic/JSON-Schema),
validated at author-time and surfaced to the editor via `layers`. Free-form stays
the default (ASSESSMENT §9 — typing is enabling, not required).

**7. Plugin: hardcoded → metadata-driven.**
Author menus, decoration, and pickers are driven by discovered layer metadata +
`display` hints instead of literal `intent`/`docs` (`notes.lua:12`,
`actions.lua:37`, `telescope.lua:146`). The render-status bug (`panel.lua:169`,
`card.lua:124` — SPIKE_FINDINGS.md Spike F) is fixed so *any* layer surfaces
everywhere.

## Before → after (one picture)

```
TODAY                                        AFTER
─────                                        ─────
config.toml ──(dotted strings)──┐            register_*() + entry points ──┐
                                ▼                config.toml (sugar) ───────┤
                          make_generator                                    ▼
                          open_store  (hand-edited dispatch)        registries (seeded w/ built-ins)
                                │                                           │
                                ▼                                           ▼
   Generator.generate(text)→bytes                   Producer.produce(recording ctx)→value
   key = local | forward-semantic                   key = TRACED read-set (any direction)
                                                                            │
   ┌──────────── SessionActor (ALL calls) ──────┐    ┌─ actor: WRITES ─┐ ┌─ pool: READS (shared snap) ─┐
   │ 14 RPC verbs                               │    │ author/sync/gc  │ │ entity_at/derived/...        │
   │ convert/ surface = STRANDED                │    └─────────────────┘ └──────────────────────────────┘
   │ bus: Interest.ALL only                     │    + async producer worker   + convert/ as live verbs
   └────────────────────────────────────────────┘    + bus: per-layer/interest subscriptions

   plugin: hardcoded intent/docs, regex goto    plugin: layer-metadata-driven, real references/rename
   identity: edit✓ move✓ rename✗                identity: edit✓ move✓ rename✓ (explicit native rebind)
```

## What deliberately does NOT change (invariants preserved — ASSESSMENT §12)

- **Rust owns committed truth.** Registration adds Python authored/derived layers
  (where they already live); the only native additions are a config-validation shim
  (AB1) and the explicit rename mutation (AB8) — **no new code-layer semantics**.
- **Reads run over frozen snapshots**; the **parity oracle**, **AST-canonical
  hashing**, the durable-id *automatic* matcher rules, and the DerivationDAG
  topo/invalidation core are untouched.
- **Writes stay single-threaded.** Concurrency is added only for reads and
  off-thread producers, leaning on already-independent shareable snapshots.
- `[profile.dev.package."*"] opt-level=3` stays.

## Net properties gained

Extensibility (a real plugin seam + entry points) · correctness for
reverse-direction data (traced keying) · read throughput (concurrent reads + async
slow producers) · editor richness (the whole `convert/` surface + generic layer UX)
· durable identity through rename · opt-in typing — **without a rewrite**.

## Module-level change inventory (for planning/review)

| File / area | Change | Task |
|---|---|---|
| `src/tyo3/extend.py` | **new** registration module | AB1, AB2, AB5, AB6 |
| `derive/generators.py` | `make_generator` → registry dispatch; `Generator`→`Producer` adapter | AB1, AB2 |
| `derive/dag.py` | traced-read-set keying; merge registered layers; finish fs-GC | AB1, AB2 |
| `derive/layer.py`, `derive/scheduler.py` | producer lifecycle; async recompute | AB2, AB3 |
| `stores/__init__.py` | `open_store` → registry dispatch | AB1/AB6 |
| `session/views.py` | recording `ProduceContext`; async serve in `Snapshot.derived` | AB2, AB3 |
| `session/session.py` | plugin load on open; `effective_layers`; schema validate; rename | AB1, AB5, AB8 |
| `daemon/handlers.py` | +`references`/`hover`/`rename`/`type_hierarchy`/`diagnostics_at`/`layers`/`layer_ids`; one-snapshot card | QW1, QW3, QW4, QW7 |
| `daemon/{session_actor,server,bus_pump}.py` | read pool + shared snapshot; interest-scoped subscriptions | AB4, AB7 |
| `bus/delta.py` | populate `touched_layers` | AB7 |
| `rust/src/config.rs`, `project/methods.rs` | `register_authored_layer` shim | AB1 |
| `rust/src/{identity,project/commit,project/methods}.rs` | `Mutation::RenameRebind` + rebind path | AB8 |
| `editors/tyo3.nvim/lua/tyo3/*` | metadata-driven UX; render fix; real goto/references/rename | QW1, QW3, QW5, QW6 |
| `src/tyo3/tests/*`, `daemon/tests/*` | registration / reverse-dep / rename / async / concurrency contracts | all |
