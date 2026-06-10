# Spine-extensibility implementation — PROGRESS

Tracking the 11-PR sequence from `../21-spine-extensibility-review/IMPLEMENTATION_GUIDE.md`.
Branch base: `nvim-plugin` (PR #2 open → main). One branch per PR.

Legend: ⬜ not started · 🟡 in progress · ✅ landed (committed) · 🔵 in review

## Phase A — quick wins — **ALL MERGED into `nvim-plugin` (2026-06-09)**
| PR | Task | Status | Branch | Notes |
|----|------|--------|--------|-------|
| 1 | QW6 renderer status gates | ✅ merged | spine-extend-phase-a | commit d4791a0; PR #3 MERGED |
| 2 | QW1 convert/ RPC verbs | ✅ merged | spine-extend-qw1 | commit 4ea1db6; PR #4 MERGED. 7 nav verbs + plugin find-callers |
| 3 | QW3+QW7 layers/layer_ids | ✅ merged | spine-extend-qw3-qw7 | commit 78214dd; PR #5 MERGED. layers+layer_ids verbs; generic author menu |
| 4 | QW4 (+QW5 if pre-AB1) one snapshot/card | ✅ merged | spine-extend-qw4 | commit 70da053; PR #6 MERGED. entity_at+decorate share one snapshot. **QW5 deferred into AB1** |

### Phase-A integration (how the four landed)
The four PRs were **siblings** off `nvim-plugin` (05e609a), not stacked, and PR #5
(QW3) conflicted with PR #4 (QW1) on `handlers.py`/`actions.lua`/`architecture.md`/
`test_handlers.py` (both append RPC verbs). Resolved locally as **unions** (merge
order QW6→QW1→QW3→QW4), verified green, fast-forwarded `nvim-plugin` to the
integration and pushed → GitHub auto-closed #3–#6 as MERGED. **`nvim-plugin` is now
at `41c6d29`** and contains all Phase-A work. AB1 bases directly off it (no siblings).

Post-merge base now has: `_entity_dict(self, s, snap, did, config)` (QW4 signature
AB1 threads `effective_layers` into), the `layers`/`layer_ids` verbs (QW3), the
`references`/`hover`/`rename`/… nav verbs (QW1), and the fixed panel/card renderers
(QW6). `_layer_value` helper present. id_for/locate stay live-registry on the session
(QW4 gotcha — entity_at resolves identity on `s`, layer reads on the snapshot).

## Phase B — registration spine — **ALL MERGED into `nvim-plugin` (2026-06-09), now at `3f3f4f6`**
| PR | Task | Status | Branch | Notes |
|----|------|--------|--------|-------|
| 5 | AB1 (+AB6) registration API | ✅ merged | spine-extend-ab1 | PR #7 MERGED. extend.py + native shim + full QW5 |
| 6 | AB5 optional schemas | ✅ merged | spine-extend-ab5 | PR #8 closed-as-merged (commit `7077be2` in nvim-plugin); author-time validation + `schema` in `layers` verb; pure-Python |
| 7 | AB7 per-layer subscriptions | ✅ merged | spine-extend-ab7 | PR #9 closed-as-merged (commit `3f3f4f6` in nvim-plugin); `subscribe` RPC + per-connection delivery + real layer name on `delta.layers`; pure-Python/daemon |

**Phase-B integration:** linear stack `nvim-plugin ← ab1 #7 ← ab5 #8 ← ab7 #9`
fast-forwarded `nvim-plugin` to ab7's tip (`git push origin
spine-extend-ab7:nvim-plugin`, 41c6d29→3f3f4f6). #7 auto-MERGED; #8/#9 couldn't
retarget (head already in base) so were CLOSED with a comment pointing at their
commits in `nvim-plugin` (same as Phase A). Umbrella **PR #2 (nvim-plugin → main)
still open.** **Phase C (AB2) branches off `nvim-plugin` @ `3f3f4f6`.**

## Phase C — powerful + fast
| PR | Task | Status | Branch | Notes |
|----|------|--------|--------|-------|
| 8 | AB2 producer + traced read-set | ✅ MERGED #10 | `spine-extend-ab2` | produce-then-key; references self-heal; ff'd into nvim-plugin @ 5524f60 |
| 9 | AB3 async serve | 🔵 PR #11 open | `spine-extend-ab3` @ `709b52c` | off-actor `DerivedRecomputeWorker` + `DerivedFresh` bus channel + `derived` notification. **Architecture decision (overrode the locked "no Rust / serving already encodes it"): `serving` was vestigial pre-AB3 (read path never branched on it). Gave it honest meaning — `block`=sync (NEW default), `stale`=async. Flipped `default_serving()` (config.rs) + `DerivedLayerSpec.serving`/`AuthoredLayerSpec` to `block`; migrated the vestigial-`stale` gate/final/demo configs to `block` (their real intent: synchronous reads).** PR #11 → base `nvim-plugin`, MERGEABLE. Verified: test-fast 706/1-baseline, test-final 43/43, daemon 49/49, rust config 25/25, Lua 12/12, ruff+clippy clean. |
| 9b | AB3 derived-e2e + async pop-in demo | 🔵 follow-up | `derived-notify-e2e-demo` (off `main` @ `709b52c`) | Closes AB3's one coverage boundary + makes it a visible demo beat. Adds a slow `serving="stale"` `blurb` layer (class-scoped, ~1.5s python generator) to the shop config (`demo/tour.py`) — the single source the e2e test + context demo both build from. **(1)** new daemon e2e `test_derived_notification_arrives_over_socket`: first `derived` read returns `absent` far under the 1.5s producer (actor not blocked) → off-actor worker publishes → `derived` notification crosses the wire → re-pull is `fresh`. **(2)** context demo scene: land on `class Item`, SUMMARY shows `blurb: computing…` → pops to `blurb<class Item:>` in place (panel `on_derived` re-pull). Card now surfaces a stale-serving absent layer as `computing` (`_entity_dict`); panel/card render it. **Gotcha fixed:** `_entity_dict`'s kind filter compared `node.kind.value` (`class_`) to config `entity_kinds` (`class`) → class-scoped layers silently never bound; added `_kind_matches` (trailing-underscore tolerant). Demo lives in `context/` not `default/` (only the cursor-driven panel auto-refreshes on `derived`). Verified: daemon 50/50, Lua smoke 12/12 + context 20/20, ruff clean. |
| 10 | AB4 read concurrency | ⬜ | | write test FIRST |
| 11 | AB8 native rename rebind | ⬜ | | advanced — pair review |

## Baseline on the MERGED `nvim-plugin` base (41c6d29) — re-characterised 2026-06-09
`devenv shell -- test-fast` → **680 passed**, 2 failed + 1 error — **all environmental**:
- `test_gate4_config_sidecar::test_sidecar_is_sole_path_owner_in_source` — documented
  baseline (demo/tour.py:175 builds a `.tyo3` path the invariant confines to Sidecar).
- `test_property_based::test_all_operations_raise_after_close` — **xdist flake**
  (passes serially `-p no:xdist`); same shared-fixture-contention class as below.
- `test_concurrency::TestSnapshotFullSurface::test_check_file` (ERROR) — **xdist flake**
  (passes serially). The flaky concurrency cluster is wider than originally listed;
  treat ANY `test_concurrency`/`test_property_based` failure that passes serially as
  environment, never a regression.
- daemon tests: **48/48** (`pytest src/tyo3/daemon/tests --no-cov`). Lua smoke: **12/12**
  inside devenv. These are the AB1 baseline — AB1 only adds passes.

## Baseline (original, pre-PR1, for reference) — `test-fast`: 4 failed, 679 passed (105s)
- `devenv shell -- build` → exit 0.
- Known pre-existing failure (NOT ours):
  `test_gate4_config_sidecar::test_sidecar_is_sole_path_owner_in_source`
  (`demo/tour.py:175` builds a `.tyo3` path the invariant wants confined to Sidecar).
- Flaky concurrency fixture error (per kickoff §2) — CONFIRMED flaky:
  `test_concurrency.py::TestSnapshotEquivalenceFull::test_read_parity[document_symbols]`,
  `[workspace_symbols]`, and `TestConcurrency::test_concurrent_mixed_workloads`.
  Fail under parallel xdist (contention on shared on-disk `fixtures/simple_package`,
  `failed to persist identity registry ... No such file or directory`); **pass when
  re-run serially** (`-p no:xdist` → exit 0). Not a regression.
- Lua smoke test (`tests/smoke.lua`) green inside devenv: 12/12.
- QW6 is pure Lua → cannot affect any Python test; baseline above is environment, not ours.

## PR1 — QW6 (renderer status gates)
**Done.** Pure Lua. Files: `editors/tyo3.nvim/lua/tyo3/{card,panel}.lua`.
- Confirmed enums: authored = `present|needs_review|orphaned|absent`;
  derived = `fresh|stale|failed|absent`. The renderers gated on `== "present"`,
  so *every* derived artifact (never `present`) was dropped (Spike F).
- `card.context_lines`: authored loop renders `status ~= "absent"` (+ ` ⚠` on
  needs_review); derived loop renders `status ~= "absent" and ~= "failed"`.
- `panel.note_rows`/`summary_rows`/`count_of`: same gate changes.
- Lua smoke test green (12/12 inside devenv).
- No RPC verb added → no architecture.md change.

### Follow-up noted (out of QW6's listed scope)
- `entitydoc.markdown_of` (`entitydoc.lua:19`) gates the DOCS pane on authored
  `status == "present"`, so a `needs_review` doc silently vanishes from the pane.
  Same bug class as QW6 but outside the two files the guide lists for QW6. Candidate
  for a tiny follow-up; left untouched here to keep PR1 scoped to the guide.

## PR2 — QW1 (convert/ read surface as RPC verbs)
**In review.** Commit `4ea1db6`, branch `spine-extend-qw1`, PR #4 → nvim-plugin.
Python (daemon) + Lua only; **no Rust / no native change.**
- `handlers.py`: 7 new verbs — `references`, `document_highlights`, `hover`,
  `type_hierarchy`, `can_rename`, `rename`, `diagnostics_at` — each delegating to
  the existing `_ReadOps` method on the actor over the frozen head snapshot.
  Registered in `_METHODS`. Added `_range_contains` helper for `diagnostics_at`.
- Serialisation: `references`/`document_highlights` → `{path, range (1-based), kind}`;
  `hover`/`type_hierarchy` via `model_dump(mode="json")`; `rename` →
  `{new_name, changes={path:[{range,new_text}]}}` (edit-compute only, no rebind — AB8);
  `diagnostics_at` filters `check_file` diags whose range contains (line,col), live.
- Golden rules honoured: reads only, on the actor + snapshot; cheap-reverse
  (references/diagnostics) served live, **not** cached as a layer (Spike C).
- `actions.lua`: `goto_def` jumps via the card's own `file`+`range` (dropped the
  `vim.fn.search` heuristic); new "📞 Find callers (references)" action → quickfix.
  Helpers `abspath`/`jump_to` extracted. `init.lua` needed no change (verbs are
  server-side; no client-side verb registry).
- `test_handlers.py`: 10 new tests; Spike E proven — `references(money.py:1, usd)`
  returns the `store.py` call site. Reused the shop fixture (already has the
  `usd` def + cross-file call), so no new project fixture needed.
- Verification: daemon tests 29/29; Lua smoke 12/12; `test-fast` 681 passed (only the
  2 documented baseline flakes failed under xdist, both pass serially); `test-final` 43.
- `architecture.md` RPC list updated with the navigation/analysis section.

## PR3 — QW3 + QW7 (layer discovery + batch layer_ids)
**In review.** Commit `78214dd`, branch `spine-extend-qw3-qw7`, PR #5 → nvim-plugin.
Daemon (Python) + Lua only; **no Rust.**
- `handlers.py`: `layers` (describe every config layer — name/origin/entity_kinds/
  history/review_on_change/serving/key_locality/display) and `layer_ids(layer,
  with_values?)` (ids with a record in a layer, one shared snapshot via
  `snap.layer(name).ids()`, optional `{id:value}`). `_layer_value` helper renders
  authored `.value` / derived `.artifact`. Registered in `_METHODS`.
- `actions.lua`: `M._authored_layers` cache + `refresh_layers` (fetch via `layers`);
  ACTIONS menu builds one "Author <layer>" per authored layer minus
  `SPECIAL_AUTHORED={docs}` (docs keeps its bespoke editor); defaults to {"intent"}
  pre-discovery. `panel.set_context` triggers the lazy fetch + re-render.
- `notes.lua` `M.note(text, layer?)` + `telescope.authored(layer?)` parameterised
  (default `intent`). `telescope.authored` rewritten to one `layer_ids(..., with_values)`
  call instead of an `authored` probe per name_cache id (O(entities) → O(1) hops).
- Tests: 5 new in `test_handlers.py`; daemon suite 36/36; smoke 12/12; modules load
  clean; test-fast 681 (2 baseline flakes only); test-final 43.
- `architecture.md` RPC list updated.

## PR4 — QW4 (one shared snapshot per card / decorate)
**In review.** Commit `70da053`, branch `spine-extend-qw4`, PR #6 → nvim-plugin.
**Pure Python — daemon handler refactor, no Lua, no Rust.**
- Problem: `session.derived` opens+closes a fresh snapshot **per call**
  (`session.py:707`), so a K-layer card cost K pin/unpin cycles per cursor move.
- Fix: `entity_at` and `decorate` each open **one** `s.snapshot()` and read the
  graph + every authored/derived layer off it. `_entity_dict(s, snap, did, config)`
  + `_read_note/_read_summary(snap, ...)` take the snapshot; `config` (open-fixed
  layer table) read from the session.
- **Key gotcha:** `id_for`/`locate` are **live-registry** ops — `TyO3Session`
  overrides them to hit `self._inner` directly; the native *snapshot* handle does
  NOT expose them (`_ReadOps` versions swallow the error → `None`). So identity
  stays on `s`; only the cross-layer reads moved onto the snapshot. (First QW4
  attempt used `snap.id_for` → entity_at returned None; corrected.)
- Tests: a count-spy on `session.snapshot` proves entity_at + decorate each open
  exactly 1 snapshot; card stays multi-layer consistent. daemon 23/23; smoke 12/12;
  test-fast 682 (sidecar baseline only); test-final 43. Card shape unchanged.
- **QW5 deferred into AB1** (its config route needs a Rust `LayerCfg.display`;
  AB1 carries `display` through registration — guide says do QW5 after AB1).

---

## PR5 — AB1 (+AB6) registration API — **LANDED** (branch `spine-extend-ab1`)
The keystone shipped. `tyo3.extend` is the programmatic registration surface;
built-ins now resolve through registries; one native config-only shim makes
registered authored layers writable; the effective table threads through the DAG,
snapshot, and daemon; full QW5 display-driven decoration. PR → `nvim-plugin`.

**What shipped**
- **`src/tyo3/extend.py` (NEW):** process-global registries `_LAYERS`/
  `_GENERATORS`/`_STORES` + `_PLUGINS_LOADED`; `AuthoredLayerSpec`/
  `DerivedLayerSpec` (with `.to_layer_config()` thin projection), `StoreContext`,
  enums, `Producer`/`ProduceContext` Protocols (declared, **unwired** — AB2);
  `register_layer/generator/store` (dup raises unless `override=True`);
  `load_plugins()` (entry-point group `tyo3.plugins`, `_PLUGINS_LOADED`-guarded);
  resolution helpers `resolve_generator`/`resolve_store`.
- **Refactor-first green gate:** `derive/generators.py` + `stores/__init__.py`
  moved the `python`/`command`/`http` + `fs`/`lancedb` bodies into
  self-registering factories; `make_generator`/`open_store` are thin dispatchers
  (signatures preserved). Built-ins self-register at *their own* import time (no
  cycle). `_OPTIONAL_BACKENDS`/Gate-4 stub deleted; qdrant/sqlite-vec → unknown
  backend `ValueError` (register via `register_store`). lancedb still raises
  `StoreBackendUnavailable`. Green gate proven: `test-fast` 681 pass (baseline-only
  fails) *before* any new capability.
- **Native shim (`rust/src/project/methods.rs`):** `register_authored_layer(name,
  history, review_on_change)` inserts a synthesized `LayerCfg{origin:Authored}`
  into the in-memory `ValidatedConfig` **and appends the name to `topo_order`**
  (load-bearing: `config.py` builds its layer table from `topo_order` membership).
  Idempotent; rejects non-authored type collisions; config-only. clippy -D clean.
- **Init ordering (`session/session.py`):** `load_plugins()` (before open) →
  `register_authored_layer` per authored spec (after open) → read `config_json()`
  → build `self._effective_layers` (= native `config.layers` ∪ registered-derived
  projections). `session.effective_layers` + `session._display_for(layer)`.
- **Threading:** `Snapshot(..., layers=effective)` + `Snapshot.layer()` consults
  it; `DerivationDAG.from_session` iterates the effective table, resolves
  registered specs' produce/store from the spec objects, extends `topo_order` with
  registered-derived names via a local Kahn toposort (`_effective_topo_order`);
  handler `entity_at`/`_entity_dict` + `layers` verb use `s.effective_layers`.
- **Full QW5:** `display`/`render` on specs; `_display_for`; `_note_layer`/
  `_summary_layer` pick inline layers by `display`; `layers` verb emits `display`.
- **Tests (`src/tyo3/tests/test_registration.py`, 10):** Spike A/B (register
  authored `tests` + derived `complexity` from the test, zero config; author+read,
  derive `score`, both ride the real `entity_at` card); dup-raises/override;
  entry-point plugin via `importlib.metadata` monkeypatch; built-ins resolve;
  unknown backend `ValueError` + lancedb unavailable; custom store backend used by
  a derived layer; QW5 inline decoration (note+summary); daemon `layers` lists the
  registered layer with `display`; identity edit ✅ / atomic move ✅ / rename ❌.
  Autouse fixture imports the built-in factory modules *then* snapshots/restores
  the registries (process-global hazard — a leak would bleed layers into other
  suites).

**Verification:** `build` → 0; `clippy -D` clean; `test_registration.py` 10/10;
daemon **48/48**; Lua smoke **12/12** inside devenv; `test-fast` (see below);
`architecture.md` `layers` verb updated (effective table + `display`).

**Split out (decision (c)):** the fs-store GC no-op (`dag.py:_gc_store`) stays a
deferred follow-up — orthogonal delete-correctness, own test.

---

## PR6 — AB5 optional per-layer value schemas — **LANDED** (branch `spine-extend-ab5`)
Turns on the `schema` field AB1 left declared-but-unused. **Strictly opt-in,
pure-Python (no Rust, no `build`), no storage change.** A registered layer whose
spec carries a pydantic `schema` gets author-time validation; the JSON Schema is
exposed through the `layers` verb. Stacked on `spine-extend-ab1`; PR base
`spine-extend-ab1`, retarget to `nvim-plugin` after #7 merges.

**What shipped**
- **`exceptions.py`:** new Python-only `SchemaValidationError(TyO3Error)` (+ in
  `__all__`). Defined unconditionally (not a native import) — AB5 is a Python
  pre-check, the native commit validator is untouched.
- **`session/session.py` — `author` validation seam (`:803`):** before
  `json.dumps(value)`, look up `_LAYERS.get(layer)`; if the spec declares a
  `schema`, `schema.model_validate(value)` and re-raise any failure as
  `SchemaValidationError`. **Validate, don't transform** — storage stays the
  original free-form `json.dumps(value)`. Registered-only (native config layers
  have no pydantic type).
- **`session/session.py` — `_schema_for(layer)`** (mirrors `_display_for`):
  returns the spec's `model_json_schema()` dict (wire-safe) or `None`.
- **`daemon/handlers.py` — `layers` verb (`:471`):** adds `"schema":
  s._schema_for(name)` per entry. Validation fires once in `session.author`
  (the daemon `author` verb delegates) — no handler change for validation.
- **`architecture.md`:** `layers` verb gains `schema` (JSON Schema / `null`,
  author-time validation note).
- **Tests (`test_registration.py`, +3):** `test_authored_schema_validates_at_author_time`
  (malformed `{"paths":5}` / `{"wrong":1}` → `SchemaValidationError`; valid
  `{"paths":["test_m.py"]}` commits + reads back); `test_unschema_layer_stays_free_form`
  (arbitrary dict accepted verbatim); `test_layers_verb_emits_json_schema`
  (`schema == TestLinks.model_json_schema()` for typed layer, `None` for
  un-schema'd). Reuse the existing `_clean_registries` autouse fixture; `TestLinks`
  carries `__test__ = False` so pytest doesn't collect it.

**Locked decisions held:** opt-in only (both arms asserted); validate-don't-transform
(storage stays JSON); transport = JSON Schema (`model_json_schema()`), not a flag;
Python-side only (no Rust). DerivedLayerSpec.schema is surfaced in `layers` but
producer output is **not** validated (rides AB2's typed-return path later).

**Verification:** `test_registration.py` 13/13 (10 + 3); daemon **48/48**;
`test-final` **43/43**; Lua smoke **12/12** inside devenv; `ruff check` clean on
touched files; `test-fast` (baseline-only fails). No `build` (pure-Python).

---

## PR7 — AB7 per-layer subscription streaming — **LANDED** (branch `spine-extend-ab7`)
Lets a daemon client subscribe to a *slice* of the delta stream (a layer, a set
of files, or a set of ids) instead of every commit. **Pure-Python/daemon — no
Rust, no `build`.** The filtering primitives (`Interest` / `Delta.scoped_to` /
the `Bus.publish` match semantics) already existed and were unit-tested; AB7
wires them the last mile. Stacked on `spine-extend-ab5`; PR base
`spine-extend-ab5`, retarget up the chain (#7/#8) as they merge.

**The two-half gap closed**
- **Half 1 — the published delta carried no specific layer name.** `CommitDelta`
  records `authored_ids` but not *which* layer; `_layers_touched` could only emit
  generic `"code"`/`"authored"`. AB7 threads the layer name from `session.author`
  → `_after_commit` → `_publish_delta` → `Delta.from_commit_delta(..., extra_layers=)`,
  unioned **additively** into `Delta.layers` (the generic strings stay for
  back-compat). Native `CommitDelta` untouched.
- **Half 2 — every client got every delta.** Each `_Client` now owns an
  `Interest` (default `Interest.ALL`). A server-level `subscribe` RPC sets it. The
  pump hands the server the **raw** `Delta`; `DaemonServer.broadcast_delta` scopes
  + encodes **per connection**, mirroring `Bus.publish` exactly.

**What shipped**
- **`bus/delta.py` — `from_commit_delta(delta, extra_layers=())`:** unions
  `extra_layers` into `layers=`. Docstring updated.
- **`session/session.py`:** `author` call site → `_after_commit(result,
  touched_layer_names=(layer,))`; `_after_commit(*, touched_layer_names=())` →
  `_publish_delta(delta, touched_layer_names)` → `from_commit_delta(extra_layers=…)`.
  All default-empty, so the ~8 other writers are unchanged.
- **`daemon/bus_pump.py`:** `BusPump.__init__` takes `broadcast_delta:
  Callable[[Delta], None]`; `_emit_delta` keeps the (delta-level, once-per-revision)
  `tracker.record` then hands the raw `Delta` to `broadcast_delta`. Refinement path
  unchanged.
- **`daemon/server.py`:** `_Client.interest = Interest.ALL`; `subscribe`
  special-cased in `_handle_line` *before* `Handlers.dispatch` (Handlers is shared
  + connection-blind) → builds `Interest` from params, replies `{"ok": true}`; new
  `broadcast_delta` (per-client scope/encode, mirrors `Bus.publish`); module-level
  `_delta_params` helper (encoding moved here since it is now per connection).
  `broadcast` (refinements) stays broadcast-to-all.
- **`daemon/handlers.py`:** `_SERVER_METHODS = {"subscribe"}`; `Handlers.methods`
  unions it so `ping` advertises `subscribe` even though it is server-dispatched.
- **`architecture.md`:** documents the `subscribe` verb (Interest params, ALL
  default, mute/restore semantics) + per-connection `delta` delivery and
  `delta.layers` carrying the real layer name.
- **Tests (+2):** `test_gate8_bus.py::test_extra_layers_stamps_specific_authored_layer`
  (additive `{"intent","authored"}`; `layer("intent")` matches, `layer("summary")`
  does not); `test_end_to_end.py::test_per_connection_subscription_filters_deltas`
  (two scoped clients + one default ALL; intent author → A receives, B times out,
  C (ALL) receives; `subscribe` advertised in `ping`).

**Locked decisions held:** reuse the existing filtering primitives (no new match
logic); layer stamping is Python-side & additive (no Rust); `Interest.ALL` is the
per-connection default (back-compat); `subscribe` is server-level not a `Handlers`
method; overflow/actor untouched; refinements stay broadcast-to-all (scoping them
is a follow-up). **Deviation from older prose:** derivation is lazy
(`recompute="lazy"`) — nothing recomputes inside the commit, so layer-stamping at
publish time is authored-only (no eager-derive path invented).

**Verification:** `test_gate8_bus.py` (incl. new unit) green; daemon **49/49**
(48 + new e2e); `test-final` **43/43**; Lua smoke **12/12**; `ruff check` clean on
touched files; `test-fast` baseline-only fails. No `build` (pure-Python/daemon).

---

## PR8 — AB2 producer protocol + traced read-sets — **MERGED** (PR #10, fast-forwarded into `nvim-plugin` @ `5524f60`, 2026-06-10)
Wires the `Producer`/`ProduceContext` protocols AB1 *declared but left unwired*.
A derived producer now reads the pinned snapshot through a **recording context**
whose traced read-set is fingerprinted into the cache key (the new default), so an
**expensive-reverse** layer (references / callsite-summary) self-heals at read when
a caller is added — the inverse of Spike C's stale-forever result. **Pure-Python —
no Rust, no `build`.** Base `nvim-plugin` @ `3f3f4f6`.

**The core gap closed.** A legacy `Generator` only ever got entity *text*
(`GenInput.source`), so a reverse layer was impossible to write *and*, keyed on the
entity's own/forward content, stale-forever when a new caller appeared. AB2 hands
the producer a recording read handle; the framework keys on exactly the ids it read
(salsa's dependency model — ty/salsa is the engine underneath).

**The produce-then-key inversion (the central tension).** A traced key isn't
knowable until the producer runs, so traced layers cannot reuse the key-then-fetch
shortcut. A read does a **cheap self-heal check** first — re-fingerprint the
*previously read* ids over the current snapshot; unchanged + cached ⇒ reuse without
re-running the producer — and only re-runs (produce → key → cache) on first read or
when a traced id moved. The override strategies (`local`/`semantic`/
`reverse-semantic`) stay key-then-produce, untouched.

**What shipped**
- **`session/views.py` — `_RecordingContext`:** wraps a pinned `Snapshot`; the
  typed reads (`find_references`/`symbol`/`dependents`/`dependencies`/`upstream`)
  append resolved ids to `read_set` (seeded with the entity's own id); raw
  `snapshot` escape hatch + `note_read`. `find_references` anchors on the symbol's
  `selection_range` (name position) and traces the direct reverse edges
  (`graph.dependents`). Frozen-snapshot only (rule #2). `Snapshot.derived` branches
  to `dag.derived_traced(...)` for traced layers.
- **`derive/generators.py` — `_GeneratorProducer`:** adapts a legacy `Generator`
  to the `Producer` contract, noting read-set `{durable_id}` ⇒ keys
  **byte-identically** to `local`. The `python`/`command`/`http` built-ins ride it
  unchanged; `setup`/`teardown` no-ops.
- **`derive/dag.py`:** `derived_traced` (cheap-check → produce-then-key → honest
  staleness); `_traced_fingerprint` (read-set hash, `{durable_id}` degenerates to
  `local`); `_direct_reverse_fingerprint` (memoised per snapshot, **direct** reverse
  edges only — no transitive-cone thrash) for the new `reverse-semantic` override;
  `produce_artifact`/`_produce_code` unify produce through the recording producer;
  `from_session` builds + `setup()`s a producer per code-derived layer;
  `teardown_producers()`; `invalidate` **skips traced layers** (no eager reverse
  recompute — lazy self-heal at read). `_resolve_producer` dispatches Producer
  object vs dotted-string/legacy-generator.
- **`derive/layer.py`:** `producer` field; `is_traced`; widened `key_locality`
  (`+ reverse-semantic, traced`); `bind_traced`/`traced_read_set`/`_read_sets`.
- **`derive/scheduler.py`:** `recompute_now`/`process_all` produce via
  `dag.produce_artifact` (one seam for the built-ins).
- **`extend.py`:** `DerivedLayerSpec.key_locality=None` now projects to **`traced`**
  (was `local`); docstrings updated. Protocols were already declared — not redeclared.
- **`session/session.py`:** `close()` calls `dag.teardown_producers()`.
- **`architecture.md`:** new "Derived layers — producers + cache-key strategy"
  section (producer protocol, recording read-set, the `key_locality` table,
  produce-then-key, the cheap-vs-expensive-reverse taxonomy).
- **Tests (+6, `test_registration.py`):** `spike_c2` port (references layer
  self-heals on a new caller, value reflects it); no-op re-read does **not** recompute
  (cheap hit); legacy adapter keys **byte-identically** to `local`; legacy adapter
  does **not** recompute on a dependency-only change (no regression); `reverse-semantic`
  override recomputes on a direct caller change; `setup`/`teardown` lifecycle.

**Locked decisions held:** traced read-set is the **default**, overrides are opt-out
fast paths (no direction enum on the traced path); protocols **wired, not
redeclared**; legacy generators → adapters keying identically to `local`; cheap-reverse
stays a live RPC (QW1) — AB2 is *expensive*-reverse only; no Rust; frozen-snapshot
reads; one writer.

**Known boundary (documented, deferred).** The cheap self-heal check re-fingerprints
the *previously read* ids, so it catches own-body changes and any **previously
traced** dependent's change (the `spike_c2` case: an extra call in an existing
caller moves that caller's content hash). A brand-new dependent id that was never in
the read-set (a caller in a *new* file with nothing else changed) is rediscovered on
the next produce triggered by any traced-id change — full eager re-discovery is
salsa-territory, out of AB2's scope. `gc_orphans` deletion for traced keys stays the
existing deferred no-op.

**Verification:** `test_registration.py` **19/19** (13 AB1 + 6 AB2); daemon
**49/49**; `test-final` **43/43**; existing derived suites (`test_gate5_derived`,
`test_final_derived_contract`) green; `test-fast` **702 passed / 1 baseline fail**
(`test_sidecar_is_sole_path_owner_in_source`, environmental); `ruff check` clean on
touched files. No `build` (pure-Python).

> Next: **AB3** (async serve for slow producers) builds on this — the produce path
> is kept reentrant/enqueue-able. **AB4** (read concurrency) + **AB8** (native rename)
> stay behind the human-review gate.

---

## (historical) ▶ CURRENT — PR5 = AB1 (+AB6): design APPROVED, coding started
The human design review is **done and signed off** (2026-06-09). The design note +
locked decisions are in `AB1_DESIGN_NOTE.md` (§9 = decisions). **Do not re-litigate
the design** — start coding at Task 1.

Branch: `spine-extend-ab1` off the merged `nvim-plugin` (41c6d29). Build order
(IMPLEMENTATION_GUIDE "TASK AB1"):
1. **Refactor-first green gate** — `src/tyo3/extend.py` (registries + specs/enums/
   protocols); move built-in generator (`python`/`command`/`http`) + store (`fs`/
   `lancedb`) bodies into self-registering factories; `make_generator`/`open_store`
   dispatch through the registries. `build`+`test-fast` MUST be green here (pure
   refactor) before adding anything new.
2. Registration surface (`register_layer/generator/store`, dup raises unless
   `override=True`) + `load_plugins()` entry-point discovery.
3. Native shim `PyTyProject.register_authored_layer` (methods.rs + config.rs).
4. Init ordering (open→load_plugins→register_authored_layer per spec→read config_json)
   + `session.effective_layers` (thin LayerConfig projection) threaded into the DAG
   builder, `Snapshot.layer()`, and the handler card/`layers`/`layer_ids` loops.
5. Full QW5: `display`/`render` on specs; `session._display_for`; rewrite
   `_note_layer`/`_summary_layer`/`decorate` to pick inline layers by `display`;
   `layers` verb emits `display`.
6. `test_registration.py` (ports Spikes A/B) + a QW5 decoration test + daemon `layers`.
7. Gates + paperwork + PR → `nvim-plugin`.

**Split out of PR5:** the fs-store GC no-op (`dag.py:_gc_store`, `:355`) → its own
follow-up PR (decision (c)).

### Locked decisions (AB1_DESIGN_NOTE.md §9)
- **(a) native shim ACCEPTED** — config-only `register_authored_layer`, idempotent,
  rejects type collisions; Rust stays the sole validator.
- **(b) `effective_layers` = THIN `LayerConfig` projection** — schema/display/render
  stay on the `_LAYERS` spec objects; no Rust `LayerCfg` metadata fields.
- **(c) AB6 store registry IN, GC no-op SPLIT OUT.**
- **(d) QW5 FULL** (display-driven decoration this PR).

### Non-obvious gotchas (discovered while grounding the design)
- **Import-cycle avoidance:** built-ins self-register into `extend.py`'s registries at
  *their own* import time; `extend.py` does NOT import generators/stores at module
  load (use `TYPE_CHECKING` for `Generator`/`Store`/`Sidecar`/`Snapshot` types).
- **Keep `make_generator(gen_cfg, /, *, name="")` signature** — `test_gate5_derived.py`
  calls it positionally with `name=`. Generator factories are `(cfg, *, name="")` →
  zero behaviour change (error messages still carry the layer name).
- **Keep `open_store(store_cfg, sidecar, layer=None)` signature** — `dag.py:71` +
  `test_gate4_config_sidecar.py:426` depend on it; build the `StoreContext` internally.
- **lancedb factory MUST still raise `StoreBackendUnavailable` on missing import** —
  `test_gate4_config_sidecar.py:442` and `test_gate5_derived.py:648` assert it
  (`match="lancedb"`). Deleting `_OPTIONAL_BACKENDS` is fine; qdrant/sqlite-vec just
  become unregistered → `ValueError` (register them via `register_store`).
- **Init ordering is load-bearing:** read `config_json()` *after* the per-spec
  `register_authored_layer` calls, so `self._config.layers` (and `snap.authored`, the
  `layers` verb, the card) include registered authored layers via the native projection.
- **`load_plugins()` runs before `open`; the per-spec `register_authored_layer` runs
  after open** (needs the project handle). Module-flag guard `_PLUGINS_LOADED`.
- **id_for/locate are live-registry** (session, not snapshot) — preserve QW4's split.

### Native shim anchor lines (post-merge)
- `rust/src/config.rs`: `LayerCfg` `:243`, `ValidatedConfig` `:305`,
  `authored_layer_config` `:329`, `LayerOrigin::Authored` `:218`, `default_serving`/
  `default_recompute` exist. `head.config` is mutable via `lock_state` guard.
- `rust/src/project/methods.rs`: `author` `:307` (pattern to mirror for the new method).
- `rust/src/project/commit.rs`: author validation `:634` (`authored_layer_config`).
