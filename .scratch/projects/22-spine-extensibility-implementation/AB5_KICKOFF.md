# KICKOFF — PR6 = AB5: optional per-layer value schemas

You are continuing the **TyO3 spine-extensibility build**. AB1 (the registration
keystone) is **landed** — `tyo3.extend` exists and already carries a `schema`
field on both spec dataclasses (unused so far). AB5 turns that field on:
**validate authored values at author-time when a schema is declared, and expose
the JSON Schema through the `layers` verb** — strictly opt-in, no storage change.

This is a **small, pure-Python PR** (no Rust, no `build`). Start coding at Task 1.

## State on entry (all true as of 2026-06-09 — do NOT redo)

- **AB1 is landed** on branch `spine-extend-ab1` at **`c48f262`**, PR **#7** open
  → `nvim-plugin` (not yet merged). AB5 **depends on AB1's `extend.py`**, so it
  must stack on it.
- **Branch:** create `spine-extend-ab5` **off `spine-extend-ab1`** (`c48f262`):
  `git checkout spine-extend-ab1 && git pull && git checkout -b spine-extend-ab5`.
  Open the PR with **base `spine-extend-ab1`** (a clean stacked diff); retarget it
  to `nvim-plugin` once #7 merges. (If #7 is already merged into `nvim-plugin` when
  you start, branch off `nvim-plugin` instead and target it directly.)
- **The `schema` field already exists** on `AuthoredLayerSpec` (`extend.py:68`)
  and `DerivedLayerSpec` (`extend.py:121`) as `schema: type[BaseModel] | None =
  None`. AB1 left it declared-but-unused on purpose. You are wiring it.
- `pydantic` is already a project dependency (the `models/` package uses
  `BaseModel`/`model_validate`/`model_dump`). No new dependency.

## Read first (in order)
1. `21-.../IMPLEMENTATION_GUIDE.md` → **"TASK AB5"** (the 3-step spec; §0 golden
   rules + §0.1 the devenv build/test loop).
2. `21-.../API_DESIGN.md` §2.2/§2.3 (the `schema` field), §8 open-question #3
   (schema transport — **JSON Schema via `model_json_schema()`**, decided), §5.1
   (the `TestLinks` worked example the test ports).
3. `21-.../ASSESSMENT.md` §9 (why typing is *enabling but opt-in* — values are
   free-form dicts today; a schema adds validation + self-describing cards).
4. `AB1_DESIGN_NOTE.md` §4/§9 + the `spine-extensibility-implementation` memory —
   how `effective_layers` / `_display_for` / `_LAYERS` already work (you mirror
   `_display_for` with a `_schema_for`).

## The exact shape (grounded in current source)

### 1. Author-time validation — `session/session.py:803` (`TyO3Session.author`)
Today `author` is `payload = json.dumps(value)` → `self._inner.author(...)`. Add,
**before** `json.dumps`, a registered-schema check:

```python
from tyo3.extend import _LAYERS
spec = _LAYERS.get(layer)
if spec is not None and spec.schema is not None:
    try:
        spec.schema.model_validate(value)     # validate ONLY; do not mutate
    except Exception as exc:                   # pydantic.ValidationError
        raise SchemaValidationError(
            f"value for layer '{layer}' does not match its schema: {exc}"
        ) from exc
```

- **Validate, don't transform.** Store the original `value` unchanged
  (`json.dumps(value)` as today) — storage stays free-form JSON (landmine: *don't
  change storage*).
- Only **registered** layers can carry a schema (a native config layer has no
  pydantic type). `_LAYERS.get(layer)` is the only lookup.
- Applies to **authored** layers only (derived layers produce artifacts, not
  authored values). A `DerivedLayerSpec.schema` is for *artifact* typing/exposure;
  AB5 surfaces it in `layers` but does not validate producer output (that can ride
  AB2's typed-return path later — out of scope here).

### 2. New typed error — `exceptions.py` (after `InternalTyError`, `:104`)
Add a **Python-only** error (this is a Python-side check, not native):
```python
class SchemaValidationError(TyO3Error):
    """Raised when an authored value fails its layer's declared schema (AB5).
    Opt-in: only fires for a registered layer whose spec carries a `schema`."""
```
Add it to `__all__`. (It is not in the native `_native_impl` import block — define
it unconditionally below the try/except, like `ProjectOpenError`/`StoreError`.)

### 3. Expose the JSON Schema — the `layers` verb (`daemon/handlers.py:452`)
The verb already emits `display` via `s._display_for(name)` (`:471`). Add a
parallel `session._schema_for(layer)` (mirror `_display_for` in
`session/session.py`, right after it) returning the JSON Schema dict or `None`:
```python
def _schema_for(self, layer: str) -> dict | None:
    from tyo3.extend import _LAYERS
    spec = _LAYERS.get(layer)
    if spec is not None and spec.schema is not None:
        return spec.schema.model_json_schema()
    return None
```
Then in the `layers` work-fn add `"schema": s._schema_for(name)` to each entry
(and `"has_schema": s._schema_for(name) is not None` if you want the cheap flag
too — API_DESIGN §8.3 leans JSON Schema; emit the full schema).

### 4. Tests — extend `src/tyo3/tests/test_registration.py`
Reuse the **existing `_clean_registries` autouse fixture** (process-global
registry hazard — already handled there). Add:
- `test_authored_schema_validates_at_author_time`: register
  `AuthoredLayerSpec(name="tests", schema=TestLinks)` where
  `class TestLinks(BaseModel): paths: list[str]; last_run: str | None = None`
  (API_DESIGN §5.1). A **malformed** value (`{"paths": 5}` /
  `{"wrong": 1}`) → `pytest.raises(SchemaValidationError)`; a **valid** value
  (`{"paths": ["t.py"]}`) commits and reads back.
- `test_unschema_layer_stays_free_form`: a layer with **no** schema authors an
  arbitrary dict exactly as today (no validation).
- `test_layers_verb_emits_json_schema`: the daemon `layers` verb returns
  `schema == TestLinks.model_json_schema()` for the typed layer and `None` for an
  un-schema'd one. (Use the `SessionActor` + `Handlers` pattern already in
  `test_registration.py`'s daemon tests.)

## Locked decisions (do NOT re-litigate)
- **Opt-in only.** Schemas are never mandatory; un-schema'd layers behave exactly
  as today (free-form JSON). The acceptance test asserts both arms.
- **Validate, don't transform.** Storage stays JSON; the schema is a gate, not a
  serializer. Don't store the pydantic model.
- **Transport = JSON Schema** (`model_json_schema()`), not a name-only flag — a
  future non-Lua client can build a typed form (API_DESIGN §8.3).
- **Python-side only.** No Rust, no `build`. The native commit validator is
  untouched; AB5 is a Python pre-check + a verb field.

## Non-obvious gotchas
- **Registered-only schemas.** `_LAYERS.get(layer)` is the sole source; a native
  `config.toml` layer cannot carry a pydantic schema (`_schema_for` returns None
  for it). That's correct — don't try to synthesize one.
- **Process-global registries leak across tests.** The `_clean_registries`
  autouse fixture in `test_registration.py` snapshots/restores `_LAYERS` (and
  imports the built-in factory modules first). Put the new tests in that file so
  they inherit it; a schema'd `tests` layer leaking into other suites would break
  them.
- **Don't double-validate the daemon `author` verb.** `handlers.author`
  (`daemon/handlers.py`) delegates to `session.author`, so the validation fires
  once, in the session. No handler change needed for validation — only the
  `layers` verb gains `schema`.
- **`model_json_schema()` is JSON-able** (plain dict) — safe to put straight on
  the wire; no custom serialisation.

## Anchor lines (post-AB1, on `spine-extend-ab1` @ c48f262)
- `src/tyo3/session/session.py:803` — `TyO3Session.author` (validation seam);
  `_display_for` is just above it (mirror it for `_schema_for`).
- `src/tyo3/daemon/handlers.py:452` — `layers` verb; `:471` the `display` line.
- `src/tyo3/extend.py:68` / `:121` — the `schema` fields on the specs.
- `src/tyo3/exceptions.py:103-104` — `InternalTyError`; add
  `SchemaValidationError` after it and to `__all__` (`:107+`).
- `src/tyo3/tests/test_registration.py` — `_clean_registries` autouse fixture +
  the `SessionActor`/`Handlers` daemon-test pattern to copy.

## Ground rules (non-negotiable)
- **Everything through devenv.** Pure-Python PR ⇒ no `build` needed; inner loop is
  `devenv shell -- test-fast`. Before the PR: `pytest src/tyo3/daemon/tests
  --no-cov`, `test-final`, and the Lua smoke (inside devenv):
  `nvim --headless --clean -u editors/tyo3.nvim/tests/minimal_init.lua -c
  "luafile editors/tyo3.nvim/tests/smoke.lua"`. Suites are slow — background,
  budget ~15 min. Run `ruff check` on touched files.
- **Golden invariants:** Rust owns committed truth (AB5 adds *no* native change);
  reads over a frozen snapshot; one writer via `SessionActor`; identity rules
  unchanged. Don't touch the parity oracle or `[profile.dev.package."*"]`.
- **No AI attribution** anywhere (commits, PRs, code, docs).

## Baseline (on `spine-extend-ab1` @ c48f262 — all fails are environmental)
`test-fast` → **691 pass** (681 base + 10 AB1 `test_registration.py`); the 2 fails
are documented: `test_sidecar_is_sole_path_owner_in_source` (demo/tour.py) + one
xdist shared-fixture flake (e.g. `test_rust_integration::test_check_after_reload`
or a `test_concurrency`/`test_property_based` case) that **passes serially**
(`-p no:xdist`). daemon **48/48**; Lua smoke **12/12**; `test-final` **43/43**.
AB5 only adds passes (≈3 new tests).

## Definition of done (PR6)
`session.author` validates when a schema is declared (raises
`SchemaValidationError`); `_schema_for` + the `layers` verb emit `schema`
(JSON Schema); un-schema'd layers unchanged; ~3 new tests in
`test_registration.py`; `test-fast` green (baseline only); daemon tests green;
`test-final` green; Lua smoke 12/12; `architecture.md` `layers` verb gains
`schema`; `PROGRESS.md` (mark PR6 landed) + the
`spine-extensibility-implementation` memory updated; push `spine-extend-ab5`, open
a PR (base `spine-extend-ab1`, retarget to `nvim-plugin` after #7 merges; no AI
attribution). Then stop and report.

## Memories to recall
`spine-extensibility-implementation`, `spine-extensibility-review`,
`durable-identity-binding-rules`, `session-reads-via-frozen-snapshot`,
`devenv-test-entrypoints`, `test-run-timeouts`, `commit-no-ai-attribution`,
`nvim-integration-pr`.
