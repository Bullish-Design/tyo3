# AB1 — Programmatic registration API: design note (for sign-off)

> PR5 keystone. Read against `21-.../IMPLEMENTATION_GUIDE.md` "TASK AB1",
> `API_DESIGN.md` §1–§3/§6, `ASSESSMENT.md` §4. This note is grounded in the
> *current* source (line refs below), not the design doc's snapshot.

## 0. The one-paragraph shape

`tyo3.extend` holds three process-global registries (`_LAYERS`, `_GENERATORS`,
`_STORES`) + `register_layer/register_generator/register_store` + `load_plugins()`.
Built-in generator types (`python`/`command`/`http`) and store backends
(`fs`/`lancedb`) are **moved into factories seeded into those registries**, and
`make_generator`/`open_store` become thin dispatchers through them — a pure,
behaviour-identical refactor that must keep `test-fast` green *before* anything new
is added. Registered **derived** layers are merged into the Python `DerivationDAG`
at build time and surfaced through a thin `session.effective_layers` projection that
the snapshot, the card loops, and the `layers`/`layer_ids` verbs consume. Registered
**authored** layers additionally need writability, which is the *one* native shim:
`PyTyProject.register_authored_layer(name, history, review_on_change)` inserts a
synthesized `LayerCfg{origin: Authored, …}` into the in-memory `ValidatedConfig`, so
the native commit validator keeps owning author validation without learning any new
code semantics.

## 1. Registry data model (`src/tyo3/extend.py`, NEW)

```python
_LAYERS:     dict[str, AuthoredLayerSpec | DerivedLayerSpec] = {}
_GENERATORS: dict[str, Callable[[GeneratorConfig], Generator]] = {}   # type-name → factory
_STORES:     dict[str, StoreFactory] = {}                              # backend-name → factory
_PLUGINS_LOADED = False   # module-flag guard for load_plugins()
```

- Spec dataclasses copied verbatim from API_DESIGN §2.2 (`AuthoredLayerSpec`) and
  §2.3 (`DerivedLayerSpec`), plus enums §2.1 (`KeyLocality/Serving/Recompute/Display`)
  and the `StoreContext`/`StoreFactory` from §2.5. `Producer`/`ProduceContext` are
  **declared as Protocols now but not wired** — the recording read-set is AB2. AB1's
  `DerivedLayerSpec.produce` accepts the *legacy* `Generator` contract
  (`generate(inputs)->list[bytes]`) or a `"type:<name>"` / `"mod:fn"` string.
- `register_layer(spec, *, override=False)` — idempotent by `spec.name`; a duplicate
  name raises `ValueError` unless `override=True`. Same for `register_generator` /
  `register_store` (keyed by `name`). These are the dup-raises the test asserts.

## 2. Built-ins resolve *through* the registry (step 1 — refactor first)

- **Generators** (`derive/generators.py:53`). Move the `python`/`command`/`http`
  bodies of `make_generator` into three factory callables; seed `_GENERATORS` with
  them at import. `make_generator(gen_cfg, name=…)` becomes:
  `factory = _GENERATORS.get(gen_cfg.type or "python") or raise; return factory(gen_cfg, name=name)`.
- **Stores** (`stores/__init__.py:22`). Move the `fs`/`lancedb` arms of `open_store`
  into factories seeded into `_STORES`; `open_store` dispatches on `backend`.
  `_OPTIONAL_BACKENDS` + the `not implemented in Gate 4` stub (`:39-46`) is *deleted*
  — an unknown backend is simply "not in `_STORES`" (raise `StoreBackendUnavailable`
  if the optional import is missing, else `ValueError`).
- **Gate:** run `devenv shell -- build` (none needed yet — pure Python) + `test-fast`
  here, before adding any new capability. Must be green = proves the refactor is
  behaviour-identical. Only then layer on registration.

`extend.py` importing the built-in factories from `derive`/`stores` while those
modules import `make_generator`/`open_store` is a cycle risk — seed the registries
*inside* `generators.py`/`stores/__init__.py` at their own import time (registries
live in `extend.py`, factories register themselves), so `extend.py` has no import-time
dependency on the built-ins.

## 3. Discovery: `load_plugins()` (step 2)

```python
def load_plugins() -> None:
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED: return
    _PLUGINS_LOADED = True
    for ep in importlib.metadata.entry_points(group="tyo3.plugins"):
        ep.load()()        # each entry point is a zero-arg callable that calls register_*()
```

Called **once, lazily, at the very top of `TyO3Session.__init__`** (before
`_native.TyProject.open`), guarded by `_PLUGINS_LOADED`. This satisfies the golden
rule "registration runs before `open()` freezes the layer table." Test seam: tests
register directly via `register_layer(...)` (no entry point needed); the entry-point
path gets one test using a tmp `importlib.metadata` shim / installed stub.

## 4. The merge — `session.effective_layers` (steps 3–4)

The crux is that **reads run over a frozen snapshot** and the snapshot resolves layers
from a config table. Today three places read the layer table:
`Snapshot.layer()` (`views.py:134`, `self._config.layers`), the DAG builder
(`dag.py:60`, `config.layers`), and the daemon card loop (`handlers.py:338/347`,
already takes a `config` param from QW4).

**Decision: build the effective table once at open and thread it through.**

- `session.effective_layers: dict[str, LayerConfig]` = `dict(self._config.layers)`
  unioned with each `_LAYERS` spec **projected to a `LayerConfig`-shaped object**
  (origin/depends_on/generator/store/serving/recompute/entity_kinds/history/
  review_on_change/key_locality). Collision: a registered name that already exists
  natively is allowed only with `override=True`, else raise at open.
  - Authored registered layers will *also* appear in `self._config.layers` already
    (because the §6 native shim ran before we read `config_json()` — see §5), so the
    union is effectively "native ∪ registered-derived"; authored dedupe to native.
- **DAG** (`dag.py:from_session`): iterate the effective table (not `config.layers`),
  resolving `produce`/`store` through `_GENERATORS`/`_STORES` (objects bypass the
  registry). Topo order: registered derived layers depend only on `("code",)` or other
  declared layers; extend `config.topo_order` with registered derived names (after a
  local toposort of just the additions) so `iter_layers()` still yields in order.
- **Snapshot**: pass `effective_layers` into the `Snapshot(...)` ctor (new kwarg) and
  have `Snapshot.layer()` consult it instead of `self._config.layers`. `derived()` already
  goes through the shared DAG via `derivation_getter`, so derived registered layers ride
  frozen-snapshot reads with no further change.
- **Handlers**: `entity_at`/`decorate` pass `s.effective_layers` to `_entity_dict`
  (one-line change — it already takes `config`); the `layers` verb (`handlers.py:89`)
  and `layer_ids` iterate the effective table. The renderer is already generic.

`effective_layers` is a **thin `LayerConfig` projection** (decision (b)). Rich
per-spec metadata (`schema`, `display`, `render`) stays on the `_LAYERS` spec objects,
looked up by name where a consumer needs them (the `layers` verb adds `display`;
`schema` is AB5; `render` is editor-side). Keeping the projection thin means every
existing `for n, c in config.layers.items()` site works unchanged.

## 5. The one native shim — authored writability (step 5)

`session.author()` → `_inner.author()` → `commit::build_plan` validates with
`head.config.authored_layer_config(layer)` (`commit.rs:634`), which only knows layers
in `head.config.raw.layers`. For a *registered* authored layer to be writable:

```rust
// rust/src/project/methods.rs (new #[pymethod] on PyTyProject)
fn register_authored_layer(&self, name: &str, history: bool, review_on_change: bool) -> PyResult<()> {
    let mut guard = lock_state(&self.inner, "register_authored_layer")?;
    let head = guard.as_mut().unwrap();
    if let Some(existing) = head.config.raw.layers.get(name) {
        if !matches!(existing.origin, LayerOrigin::Authored) {
            return Err(PyConfigError::new_err(format!(
                "'{name}' already declared as a non-authored layer")));
        }
        return Ok(());   // idempotent: already an authored layer
    }
    head.config.raw.layers.insert(name.to_string(), LayerCfg {
        origin: LayerOrigin::Authored,
        depends_on: vec![], generator: None, generator_version: None,
        hash_profile: None, store: None,
        serving: default_serving(), recompute: default_recompute(),
        key_locality: None, entity_kinds: vec![],
        history, review_on_change,
    });
    Ok(())
}
```

Safety properties:
- **Config-only, no new code semantics** (golden rule #1). It inserts a `LayerCfg`
  whose origin is `Authored`; authored layers carry no generator/store/derivation —
  they are commit-validated sinks. The native validator keeps owning author validation.
- **Idempotent** and **rejects type collisions** (a name already derived → error).
- **No topo/validation re-run** — authored layers are excluded from the derived DAG
  and the topo order (`validate` only orders derived layers; `commit.rs` reads the
  `LayerCfg` directly). Inserting one cannot create a cycle. (Verify: `topo_order`
  is consumed only for derived layers; confirm nothing asserts authored ∈ topo_order.)

**Init ordering (critical):** `TyO3Session.__init__` must run
`open → load_plugins() → for each registered AuthoredLayerSpec: _inner.register_authored_layer(...)
→ self._config = TyConfig.from_json(self._inner.config_json())`.
Reading `config_json()` *after* the shims means `self._config.layers` (and therefore
`snap.authored`, the `layers` verb, the card) naturally include registered authored
layers via the existing native projection — no second path. (`load_plugins()` itself
runs before `open`; the per-spec `register_authored_layer` calls run after, since they
need the open project handle.)

## 6. AB6 fold-in

- **Store registry**: delivered by §2 (the `open_store` refactor) — `register_store`
  works, `_OPTIONAL_BACKENDS` + the Gate-4 stub are gone.
- **fs-store GC no-op** (`dag.py:_gc_store`, `:355`): a pre-existing deferred TODO
  (reverse digest→store-key lookup for FsStore GC). It is *orthogonal* to registration
  and carries its own correctness surface (deleting cached artifacts). **Recommend
  splitting it out** of the keystone PR — see decision (c).

## 7. Test plan (`src/tyo3/tests/test_registration.py`)

Ports Spikes A/B (`SPIKE_FINDINGS.md`) — custom layers via registration, zero config:
1. `register_layer(AuthoredLayerSpec(name="tests", entity_kinds=…))` +
   `register_layer(DerivedLayerSpec(name="complexity", produce=<inline Generator>,
   entity_kinds=("function","method"), key_locality="local"))` — both **from the test**.
2. Open a session on a tmp fixture; `author("tests", id, {...})`; read it back;
   read `derived("complexity", id)` → status `fresh`, artifact reflects the body.
3. Assert both ride `entity_at`'s card (call `_entity_dict` / the `entity_at` verb).
4. Assert a dup `register_layer(name="tests")` raises; `override=True` succeeds.
5. Assert an entry-point plugin loads (installed stub or `entry_points` monkeypatch).
6. Assert built-ins unchanged: the existing derive/store suites stay green.
7. Daemon-side: a registered layer appears in the `layers` verb output.

Identity binding (edit ✅ / move ✅ / rename ❌) is inherited from the native path —
assert edit/move preserve the authored record; rename mints (unchanged until AB8).

## 8. Build / verify gates (DoD)

`build` → 0 (one rust change) · `clippy -D` clean · `test-fast` green (only the two
documented baseline failures) · daemon tests green
(`pytest src/tyo3/daemon/tests --no-cov`) · `test-final` before merge · Lua smoke
12/12 *inside devenv* · `architecture.md` updated if any verb shape changes ·
PROGRESS.md + proj-22 memory updated. No AI attribution anywhere.

## 9. Decisions (SIGNED OFF — 2026-06-09)

(a) **Native shim — ACCEPTED.** Ship `register_authored_layer` as a real,
documented, idempotent native method (config-only, rejects type collisions). Keeps
Rust the sole validator. Registered authored layers are re-registered from code at
every open (not persisted to `config.toml`), before the layer table freezes.
(b) **`effective_layers` — THIN `LayerConfig` projection.** `schema`/`display`/
`render` stay on the `_LAYERS` spec objects, looked up by name. No Rust `LayerCfg`
metadata fields. `display` resolved via `session._display_for(layer)` =
`spec.display if registered else name-heuristic` (intent→inline-note,
summary→inline-summary, else panel).
(c) **AB6 — store registry IN, GC no-op SPLIT OUT.** PR5 ships `register_store` +
deletes `_OPTIONAL_BACKENDS`/the Gate-4 stub. The fs-store GC reverse-lookup
(`dag.py:_gc_store`, `:355`) is a separate follow-up PR (delete-correctness, own test).
(d) **QW5 — FULL.** Carry `display`/`render` on specs; `session._display_for`;
rewrite `_note_layer`/`_summary_layer`/`decorate` to pick inline layers by `display`;
`layers` verb emits `display`. Adds a decoration test.
