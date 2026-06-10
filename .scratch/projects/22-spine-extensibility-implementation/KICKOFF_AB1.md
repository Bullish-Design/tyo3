# KICKOFF — PR5 = AB1 (+AB6): the registration-API keystone (design APPROVED)

You are continuing the **TyO3 spine-extensibility build**. The AB1 design review is
**done and signed off** — **do not re-open it.** Start coding at Task 1 below.

## State on entry (all true as of 2026-06-09 — do NOT redo)

- **Phase A is MERGED.** PRs #3–#6 (QW6 / QW1 / QW3+QW7 / QW4) are all merged into
  `nvim-plugin`, now at **`41c6d29`**. The base already has: `_entity_dict(self, s,
  snap, did, config)` (QW4 — AB1 threads `effective_layers` in as `config`), the
  `layers`/`layer_ids` verbs (QW3), the `references`/`hover`/`rename`/… nav verbs
  (QW1), the fixed panel/card renderers (QW6), and the `_layer_value` helper.
- **Branch `spine-extend-ab1` already exists** off `41c6d29` (no commits yet).
  `git checkout spine-extend-ab1`. Keep targeting `nvim-plugin` for the PR.
- **Design note + LOCKED decisions:** `.scratch/projects/22-.../AB1_DESIGN_NOTE.md`
  (§9 = decisions). Live tracker: `.scratch/projects/22-.../PROGRESS.md` — read its
  "CURRENT" section first (build order, gotchas, native-shim anchor lines).

## Read first (in order)
1. `AB1_DESIGN_NOTE.md` end-to-end (it is grounded in the real source with line refs).
2. `PROGRESS.md` → "CURRENT — PR5 = AB1" section.
3. `21-.../IMPLEMENTATION_GUIDE.md` → "TASK AB1" (+ skim §0 golden rules).
4. `21-.../API_DESIGN.md` §1 (surface), §2.2/§2.3/§2.5 (spec dataclasses to copy),
   §3 (wiring), §6 (migration), §5.1/§5.4 (worked examples to port into the test).
5. `21-.../ASSESSMENT.md` §4; keep `SPIKE_FINDINGS.md` (Spikes A/B) handy — the test
   ports them.

## Locked decisions (AB1_DESIGN_NOTE.md §9 — do NOT re-litigate)
- **(a) Native shim ACCEPTED** — `PyTyProject.register_authored_layer(name, history,
  review_on_change)` inserts a synthesized `LayerCfg{origin:Authored}` into the
  in-memory `ValidatedConfig`. Config-only, idempotent, rejects type collisions.
  Rust stays the sole author-validator.
- **(b) `effective_layers` = THIN `LayerConfig` projection** (native ∪ registered).
  `schema`/`display`/`render` stay on the `_LAYERS` spec objects (looked up by name);
  **no Rust `LayerCfg` metadata fields.**
- **(c) AB6: store registry IN; fs-store GC no-op SPLIT OUT** to a later follow-up PR
  (`dag.py:_gc_store`, `:355`). Do not finish GC here.
- **(d) QW5 FULL** — display-driven inline decoration this PR.

## Build order (one logical step at a time; IMPLEMENTATION_GUIDE "TASK AB1")
1. **Refactor-first GREEN GATE.** New `src/tyo3/extend.py` (registries `_LAYERS`/
   `_GENERATORS`/`_STORES` + spec dataclasses §2.2/§2.3 + enums §2.1 + `Producer`/
   `ProduceContext` Protocols *declared but unwired* (AB2) + `StoreContext`/
   `StoreFactory`). Move the `python`/`command`/`http` generator bodies and the
   `fs`/`lancedb` store bodies into **self-registering factories**; make
   `make_generator`/`open_store` dispatch through the registries. **Run `build` +
   `test-fast` here — must be green (pure, behaviour-identical refactor) BEFORE any
   new capability.**
2. **Registration surface** — `register_layer/register_generator/register_store`
   (idempotent by name; dup raises unless `override=True`) + `load_plugins()`
   (`importlib.metadata.entry_points(group="tyo3.plugins")`).
3. **Native shim** — `register_authored_layer` in `methods.rs` + `config.rs`.
   `build` + `clippy -D` clean.
4. **Init ordering + merge** — `TyO3Session.__init__`: `open → load_plugins() →
   register_authored_layer per authored spec → read config_json()`. Add
   `session.effective_layers` (thin) + `session._display_for(layer)`; thread the
   effective table into `DerivationDAG.from_session`, `Snapshot.layer()`, and the
   handler card/`layers`/`layer_ids` loops (pass `s.effective_layers` to
   `_entity_dict`).
5. **Full QW5** — `display`/`render` on specs; rewrite `_note_layer`/`_summary_layer`/
   `decorate` to pick inline layers by `display` (via `_display_for`); `layers` verb
   emits `display`.
6. **Tests** — `src/tyo3/tests/test_registration.py` (ports Spikes A/B: register an
   authored `tests` + derived `complexity` layer FROM THE TEST, zero config; author+
   read, derive, both ride the `entity_at` card; dup raises; entry-point plugin loads
   via `importlib.metadata` monkeypatch; built-ins stay green; identity edit✅/move✅/
   rename❌). A QW5 decoration test. A daemon test that the `layers` verb lists a
   registered layer with its `display`.
7. **Gates + paperwork + PR.**

## Non-obvious gotchas (discovered while grounding the design)
- **Import-cycle avoidance:** built-ins self-register into `extend.py`'s registries at
  *their own* import time. `extend.py` does NOT import generators/stores at module
  load — `TYPE_CHECKING`-only for `Generator`/`Store`/`Sidecar`/`Snapshot` types.
- **Keep `make_generator(gen_cfg, /, *, name="")`** — `test_gate5_derived.py` calls it
  with `name=`. Generator factories are `(cfg, *, name="")` → zero behaviour change.
- **Keep `open_store(store_cfg, sidecar, layer=None)`** — `dag.py:71` +
  `test_gate4_config_sidecar.py:426` depend on it; build `StoreContext` internally.
- **lancedb factory MUST still raise `StoreBackendUnavailable` on missing import** —
  `test_gate4_config_sidecar.py:442` + `test_gate5_derived.py:648` assert it. Deleting
  `_OPTIONAL_BACKENDS` is fine; qdrant/sqlite-vec just become unregistered →
  `ValueError` (register them via `register_store`).
- **Init ordering is load-bearing:** read `config_json()` AFTER the per-spec
  `register_authored_layer` calls, so `self._config.layers` (and `snap.authored`, the
  `layers` verb, the card) include registered authored layers via the native projection.
- **`load_plugins()` runs before `open`; per-spec `register_authored_layer` runs after
  open** (needs the project handle). Module-flag guard `_PLUGINS_LOADED`.
- **id_for/locate are LIVE-registry** (session `s`, not the snapshot) — preserve QW4's
  split; only layer reads move onto the snapshot.
- **No new daemon verb for registration** — registration is Python import-time; the
  daemon only surfaces registered layers via the existing `layers` verb.

## Native shim anchor lines (post-merge)
- `rust/src/config.rs`: `LayerCfg` `:243`, `ValidatedConfig` `:305`,
  `authored_layer_config` `:329`, `LayerOrigin::Authored` `:218`; `default_serving`/
  `default_recompute` exist; `head.config` is mutable via the `lock_state` guard.
- `rust/src/project/methods.rs`: `author` `:307` (mirror its lock/guard pattern).
- `rust/src/project/commit.rs`: author validation `:634`.

## Ground rules (non-negotiable)
- **Everything through devenv.** `build` after ANY `rust/` change, then `test-fast`;
  `clippy` (-D) clean for the shim; daemon tests
  `pytest src/tyo3/daemon/tests --no-cov`; `test-final` before merge. Lua smoke runs
  INSIDE devenv (`nvim --headless --clean -u editors/tyo3.nvim/tests/minimal_init.lua
  -c "luafile editors/tyo3.nvim/tests/smoke.lua"`). Suites are slow — background, ~15 min.
- **Golden invariants:** Rust owns committed truth (the shim adds *config*, never code
  semantics); reads over a frozen snapshot; one writer via `SessionActor`; don't
  re-tighten the parity oracle or touch `[profile.dev.package."*"] opt-level=3`;
  identity edit✅/move✅/rename❌ (AB1 doesn't change this).
- **No AI attribution** anywhere (commits, PRs, code, docs).

## Baseline (merged base 41c6d29 — all failures are environmental, never "fix")
`test-fast` → **680 pass**; `test_sidecar_is_sole_path_owner_in_source` (documented)
+ xdist flakes in `test_concurrency`/`test_property_based` (incl.
`test_all_operations_raise_after_close`) that pass serially (`-p no:xdist`). daemon
**48/48**; Lua smoke **12/12**. AB1 only adds passes.

## Definition of done (PR5)
`extend.py` + refactors + native shim + AB6 store-registry + full QW5 +
`test_registration.py`; `build` → 0; `clippy` clean; `test-fast` green (baseline only);
daemon tests green; `test-final` green; `architecture.md` updated (`layers` gains
`display`); `PROGRESS.md` + the `spine-extensibility-implementation` memory updated;
push `spine-extend-ab1`, open a PR → `nvim-plugin` (no AI attribution). Then stop and
report.

## Memories to recall
`spine-extensibility-implementation`, `spine-extensibility-review`,
`durable-identity-binding-rules`, `session-reads-via-frozen-snapshot`,
`phase8-derived-invalidation-done`, `devenv-test-entrypoints`, `test-run-timeouts`,
`commit-no-ai-attribution`, `nvim-integration-pr`.
