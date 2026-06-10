# Review brief — the TyO3 "spine" as an extensible attach-anything-to-an-AST-node backend

> Hand this file to a fresh session as its sole brief. It is self-contained:
> read the files and memories it points at, then produce the deliverables in
> §5. This is a **review + design** engagement, not a build. Read-only by
> default; throwaway spikes are allowed (see §6).

## 1. Mission

Do a thorough, intense, full-stack review of the TyO3 library **as a backend
"spine" for attaching arbitrary, user-defined data to any code entity (AST
node)** — and assess how well it supports an interactive editor (Neovim)
workflow built on top of that spine. Then design the path to make it a
*flexible, easily-customizable* backend.

The motivating workflow already exists and stresses the backend: `tyo3.nvim`
(under `editors/tyo3.nvim/`) surfaces, per cursor position, the durable entity
under the cursor and the data attached to it — authored notes (`intent`),
authored markdown docs (`docs`), derived summaries (`summary`) — in a
data-type-separated sidebar, and lets the user author notes/docs in-editor that
**stay glued to the entity across edits and atomic moves** (durable identity).
That feature is the proof that the spine concept works; this review asks how
good the spine *really* is and how to make "attach anything" trivial.

## 2. The vision (the thing we are designing toward)

> A flexible backend spine where a user can **easily customize what they want to
> attach to any given AST node.**

Concretely: a function/method/class entity has a stable **durable id** (the
spine). Hanging off each id are **layers** — today `code` (the entity itself),
`authored` (human-entered: `intent`, `docs`), and `derived` (computed:
`summary`, `embed`). The vision is that defining a *new* kind of attachment
(a new layer: its value shape, how it's produced or authored, where it's
stored, how it's invalidated, how it rides identity changes) should be a
first-class, low-friction operation.

**Primary customization path (maintainer's chosen target): a Python
plugin / registration API.** A developer should be able to register custom
layers / generators / stores *programmatically* (richer than today's
config-string indirection): with real Python objects, lifecycles, and
dependencies. Declarative-TOML and editor/runtime attachment are *secondary* —
note them where relevant, but center the design on the programmatic API.

## 3. The gap, stated up front (verify and deepen — do not take on faith)

Extension today is **config-only and string-indirected**:

- Derived layers: `[layers.X] origin="derived"`, `generator="g"`, and
  `[generators.g] type="python" callable="module:function"`. The callable is
  imported by string and must satisfy the `Generator` Protocol
  (`src/tyo3/derive/generators.py`). Batched: `generate(inputs) -> list[bytes]`.
- Authored layers: `[layers.X] origin="authored"` + `history`,
  `review_on_change`. Values are free-form dicts (`{note: "..."}`,
  `{markdown: "..."}`).
- Stores: `[stores.X] backend="fs"|"lancedb"` (`src/tyo3/stores/`).

There is **no programmatic registration API** — no `register_layer(...)`, no
`importlib.metadata` entry points, no plugin protocol. Adding a layer means
editing `.tyo3/config.toml`; adding behaviour means writing a module and
referencing it by a dotted string. Confirm this, map every current extension
seam precisely, and judge how far it is from the §2 vision.

## 4. Scope & lens

**Full stack, spine + layers focused**, evaluated through the
attach-anything + interactive-nvim lens:

- **Rust engine** (`rust/src/`) — owns committed truth post-spine-refactor-V2.
- **Python engine** (`src/tyo3/`) — session, layers, derive, authored, stores,
  config, identity, bus.
- **Daemon** (`src/tyo3/daemon/`) — the JSON-RPC surface the editor speaks.
- **Plugin** (`editors/tyo3.nvim/`) — the consumer; judge what it needs that the
  backend makes hard/easy.

Go deep where it bears on extensibility and the workflow; don't rabbit-hole on
unrelated subsystems. "Intense and thorough" means: read the actual code, trace
real call paths, and back every claim with a file:line or a spike — not vibes.

## 5. Deliverables

Write all output into **this directory**
(`.scratch/projects/21-spine-extensibility-review/`):

1. **`ASSESSMENT.md`** — how well the spine supports attach-anything + the nvim
   workflow *today*. Cover: the identity model as a foundation; the layer model
   (code/authored/derived) and its seams; the daemon RPC surface vs. what the
   workflow needs; the plugin's friction points; what the Rust `convert/`
   surface already offers that's unused. Strengths, sharp edges, and concrete
   limitations (each with evidence).
2. **`ROADMAP.md`** — a prioritized list of improvement opportunities
   (impact × effort), framed around enabling/enhancing these workflows and the
   §2 vision. Separate quick wins from architectural bets. Note risks against
   known invariants (§12).
3. **`API_DESIGN.md`** — a concrete design proposal for the **Python
   plugin/registration API** for custom layers/generators/stores: the
   registration surface, the layer/generator/store protocols, how a custom
   attachment declares its value shape, production/authoring, storage,
   invalidation, and identity-binding behaviour; how it reaches the daemon + the
   editor; migration from today's config-string model; and worked examples
   (e.g. a `tests` layer linking entities to their tests; an LSP-backed
   `diagnostics` layer; a custom embedding layer). Include at least one
   end-to-end "define a new attachment in N lines" narrative.

A short `README.md` in this dir indexing the three is welcome.

## 6. Ground rules

- **Run everything through devenv**: `devenv shell -- <cmd>`. Build with
  `build`; fast tests with `test-fast`; never raw `pytest`/`cargo`. Suites are
  slow — budget ~15 min and run full suites in the background. (See memory
  `devenv-test-entrypoints`, `test-run-timeouts`.)
- **Read-only review.** You may write **throwaway spikes** to validate findings
  — do them on a scratch branch or a git worktree, keep them out of `main`/the
  feature branches, and clearly label them as spikes in the report. Do not
  modify engine/plugin code as part of the review.
- **No AI attribution** anywhere (commits, docs, comments).
- Current feature branch is `nvim-plugin` (PR #2 open). The review is about the
  library generally; don't entangle it with that PR.

## 7. The architecture you are reviewing (annotated anchors)

**Rust (`rust/src/`) — committed truth + AST/LSP surface:**
- `project/` (`open.rs`, `commit.rs`, `snapshot.rs`, `head_view.rs`,
  `analysis.rs`, `methods.rs`) — the spine: open/commit/snapshot lifecycle.
- `identity.rs`, `entity.rs`, `code_layer.rs`, `authored.rs`, `overlay.rs`,
  `content.rs`, `hash.rs`, `sidecar.rs`, `config.rs`.
- `convert/` + `dto/` — a large LSP-shaped surface: `code_action.rs`,
  `hover.rs`, `completion.rs`, `signature.rs`, `diagnostics.rs`, `hints.rs`,
  `navigation.rs`, `occurrences.rs`, `folding.rs`, `hierarchy.rs`, `rename.rs`,
  `symbols.rs`, `tokens.rs`. **Investigate why this exists and whether the
  daemon/plugin should expose it** — it may already provide much of what an
  interactive workflow wants (references/neighbors, rename, diagnostics).

**Python (`src/tyo3/`):**
- `session/session.py` — the public engine API: `author`, `authored`,
  `authored_history`, `derived`, `authored_needs_review`, `authored_orphaned`,
  `sync_buffer(s)`, `snapshot`, `diff`, plus the delta subscription bus.
- `session/views.py` — `CodeLayerView` / `AuthoredLayerView` / `DerivedLayerView`
  read surfaces over a frozen snapshot.
- `layers/` (`base.py` `LayerView` Protocol, `code.py`, `authored.py`,
  `derived.py`) — the read-side layer abstraction.
- `authored/layer.py` (`AuthoredLayer`) — authored write/store side.
- `derive/` (`layer.py` `DerivedLayer`, `dag.py` `DerivationDAG`,
  `scheduler.py`, `cache.py`, `generators.py`) — derived compute/invalidate.
  **The generator extension seam lives here.**
- `stores/` (`base.py` protocol, `fs.py`, `lancedb_store.py`) — artifact stores.
- `config.py` — `LayerConfig` / `GeneratorConfig` / store config + discovery.
- `graph/identity.py` + the matcher — durable-id binding across commits.
- `bus/` — the projection bus that powers `delta`/`refinement` notifications.

**Daemon (`src/tyo3/daemon/`):**
- `handlers.py` — RPC surface: `ping`, `open`, `sync_buffer(s)`, `entity_at`,
  `decorate`, `author`, `authored`, `locate`, `diff`, `derived`, `reindex`,
  `gc`, `check`. Note: read handlers run over a **frozen committed snapshot**
  through a single-threaded `SessionActor`.

**Plugin (`editors/tyo3.nvim/`):** see `docs/dev/architecture.md` and
`docs/dev/identity.md` in that tree, and `lua/tyo3/{panel,context,card,actions,
docs,entitydoc,decorate}.lua`. The plugin is the workload that reveals backend
gaps (e.g. it wants references/callers — there is no `references` RPC today).

**Read these memories first** (they encode hard-won, non-obvious truths):
`spine-refactor-v2-plan`, `phase14-acceptance-done` (refactor complete, Rust
owns committed truth), `parity-oracle-tiered`, `session-reads-via-frozen-snapshot`,
`frozen-walk-sorted-ordering`, `dev-profile-optimizes-deps`,
`durable-identity-binding-rules`, `nvim-context-panel-demos`,
`nvim-integration-pr`, `devenv-test-entrypoints`, `test-run-timeouts`.

## 8. What "attach anything to an AST node" must mean (use as a yardstick)

For a *new* kind of attachment, how hard is each of these today, and how hard
should it be under the §2 API?
- Declare it (value shape, origin authored vs derived, which entity kinds).
- Produce it (authored by a human, or derived by a generator with declared
  dependencies + invalidation).
- Store it (fs / vector / custom backend).
- Serve it (fresh vs stale; read over the frozen snapshot).
- Bind it to identity (does it ride edits / moves? what on rename? — confirm
  against `durable-identity-binding-rules`).
- Surface it to the daemon + editor (does it appear in `entity_at`'s card
  automatically, or need bespoke wiring?).

## 9. Investigation agenda (be exhaustive; cite evidence)

1. **Identity as the foundation.** How stable is the durable id? Trace the
   matcher. Confirm the binding rules (edit ✅, move ✅, rename ❌, nested-method
   `entity_at` resolves to the class). Is rename-survival feasible, and what
   would it cost? Is the id surfaced/queryable enough for clients?
2. **Layer model coherence.** Map `layers/` (views) vs `authored/` + `derive/`
   (write/compute). Is the split clean or accidental? What is the *actual*
   contract a layer must satisfy? How much is generic vs special-cased to the
   three built-in layers?
3. **Extension seams.** Precisely enumerate every way to add behaviour today
   (config layers, `generators.*.callable`, store backends). Where are they
   hard-coded (`make_generator` dispatch, store backend dispatch, daemon card
   assembly in `_entity_dict`)? What blocks a pure-Python plugin from
   registering a layer without touching core?
4. **Derived pipeline.** `DerivationDAG` + scheduler + cache: dependencies,
   invalidation keying (`resolve_input`/key locality per memory
   `phase8-derived-invalidation-done`), async refinement. How would a custom
   derived layer plug in, declare deps, and get invalidated correctly?
5. **Daemon/editor fit.** Is the RPC surface sufficient for rich workflows?
   What's missing (e.g. `references`/`neighbors` over reverse-deps — deferred
   per `phase3-affected-closure-deferred`; a generic "list layers / author any
   layer / open value editor" surface; streaming/subscription beyond delta)?
   Does `entity_at` generalize to arbitrary layers automatically?
6. **The Rust `convert/` surface.** What does it expose, is it wired to the
   daemon, and should the spine present it as attachable/derivable data
   (diagnostics, references, rename) to power the workflow?
7. **Value typing.** Authored values are free-form dicts. What would typed,
   validated per-layer schemas cost/buy (the maintainer flagged this as
   secondary — assess, don't over-invest)?
8. **Performance & concurrency.** The single-threaded `SessionActor` + frozen
   snapshots gate read throughput for a cursor-driven editor. Where are the
   limits for many small reads (the nvim CONTEXT path debounces precisely
   because of this)? Any batch/read-only-snapshot-sharing opportunities?
9. **Tests & invariants.** What protects the layer/identity contracts
   (`test_gate2_identity`, `test_gate5_derived`, `test_gate6_authored`,
   `test_final_*`)? Would the proposed API need new contract tests?

## 10. Evaluation rubric (score each, with evidence)

For each of {identity, layer model, extension seams, derived pipeline,
daemon/editor surface, value typing, performance, testability}: rate
**support today** (none / partial / good) and **distance to the §2 vision**
(small / medium / large), with the single highest-leverage change called out.

## 11. Output structure

`ASSESSMENT.md`: per-subsystem findings (evidence-backed) → the rubric table →
a plain-English verdict on "how good is the spine for attach-anything today."
`ROADMAP.md`: quick wins; architectural bets; sequencing; risks vs §12.
`API_DESIGN.md`: the registration API (protocols + surface), worked examples,
migration, open questions. Prefer concrete Python signatures over prose.

## 12. Known constraints & landmines (do not "fix" naively)

- **Rust owns committed truth** (spine refactor V2). Don't propose moving the
  spine back into Python. The parity oracle is intentionally
  structural-strict + cosmetic-downgradeable (`parity-oracle-tiered`) — don't
  argue to re-tighten it.
- **Reads run over a frozen snapshot** (`session-reads-via-frozen-snapshot`);
  frozen walks are sorted (`frozen-walk-sorted-ordering`).
- **`[profile.dev.package."*"] opt-level=3`** keeps the type-checker fast in
  tests (`dev-profile-optimizes-deps`) — don't remove it.
- **Identity binding**: edits/moves preserve, rename creates a new id, nested
  methods resolve to the class (`durable-identity-binding-rules`). Any
  "attach durably" claim must respect this.
- **Single-threaded session actor**: all session calls serialize.

## 13. Suggested spikes (optional, throwaway, on a branch/worktree)

- Register a *new* authored layer (e.g. `tests`) and a *new* derived layer with
  a custom generator **without editing core** — measure exactly what core files
  you're forced to touch. That delta is the headline finding for `API_DESIGN.md`.
- Probe whether `entity_at`'s card auto-includes an arbitrary new layer (the
  nvim work already added `docs` this way — confirm it generalizes).
- Probe the Rust `convert/` surface (e.g. references/diagnostics) reachability
  from Python/daemon.

## 14. First steps

1. Read the memories in §7 and skim `editors/tyo3.nvim/docs/dev/*.md`.
2. `devenv shell -- build` then `devenv shell -- test-fast` to confirm a green
   baseline before judging anything.
3. Trace one attachment end-to-end (`:TyO3Note` → `author` RPC → session →
   authored layer → store → `entity_at` card → plugin render) and write that
   trace down — it grounds the whole review.
4. Draft `ASSESSMENT.md` as you go; let `ROADMAP.md` and `API_DESIGN.md` follow.
