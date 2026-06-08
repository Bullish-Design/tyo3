We're continuing the TyO3 spine refactor. All planning is done and lives in
`.scratch/projects/15-implementation-plan/`. Your job this session is to implement
**Phase 12: Single config source** — make the **native validated config the one
authority**, have Python consume it as JSON, and **delete the Python re-read of
`config.toml` with its silent default-fallback** so invalid config **fails loudly
at open** instead of being silently ignored (V1 §6.3 deviation #9 / §5.12).

This is **native + Python**, but in practice **mostly Python**: the Rust side
already emits and validates the coordination + precision config (Phases 6 and 9
front-loaded it — see "Critical context" below). If you do touch Rust,
`devenv shell -- build` **before any pytest gate** (the #1 phantom-failure source
is testing a stale extension). You likely won't need to rebuild much.

Before writing any code, read in this order:
  1. `.scratch/projects/15-implementation-plan/START_HERE_V2.md`         (orientation — the V2 phase-numbering table; Phase 12 = V1 "Phase 10")
  2. `REFINED_IMPLEMENTATION_CONCEPT.md` (V1) §5.12 and §6.3 deviation #9 (the error model + the exact defect — normative)
  3. `REFINED_IMPLEMENTATION_PLAN_V2.md`  (Phase 12 section, ~line 173)  (current state + phase map)
  4. `PHASE_12_IMPLEMENTATION_GUIDE.md`                                  (the step-by-step 12.1–12.3 you'll execute)
Then skim, for current ground truth:
  5. `.scratch/projects/16-refined-implementation-work/PROGRESS.md` §9.9 (precision knobs landed) and §9.11 (Phase 11, just done)

## Critical context — Phase 11 is LANDED, and Phases 6/9 already did most of 12.2

- **Phase 11 just landed (commit `7c5b729`)**: owned-lifetime convenience reads
  (`_OwnedView`), floating-latest `graph()` made non-canonical, and the layer
  views stopped swallowing read failures into `None`. **The baseline going into
  Phase 12 is a fully green `pytest -q --no-cov`** (gate7 read-surface 15/15 +
  no-read-side-writes 3/3, zero new xfail/XPASS). Don't reintroduce a failure.
- **The native side already surfaces and validates most of what 12.2 asks for** —
  verify this first, don't rebuild it:
    - `rust/src/config.rs` has `CoordinationCfg` / `BusCfg` / `WatcherCfg`
      (~lines 497–540) and `CodeGraphCfg` (~558), **all deriving `Serialize`**.
    - `config.rs::validate` (~line 337) **already rejects** a writer-blocking/
      unknown **bus overflow** (~line 462, Phase 6), an unknown **`code_graph.precision`**
      (~470, Phase 9), and an unknown **`code_graph.refinement`** (~480, Phase 9),
      each as `ConfigError::InvalidValue` **at open**. `RawConfig` and every nested
      struct use `#[serde(deny_unknown_fields)]`.
    - `project.rs::config_json` (~line 2204) serialises the `ValidatedConfig` as
      `{"raw": {…RawConfig…}, "topo_order": [...]}` — so **`coordination` and
      `code_graph` are already in the JSON Python receives.**
  So **12.2 is largely "confirm + maybe one validation", not new machinery.** The
  heart of this phase is the **Python consolidation (12.1)** and the **loud-failure
  tests (12.3)**.

## The concrete bug — what exists vs what you build

**The exact defect (V1 §6.3 deviation #9) is live at `src/tyo3/session.py:865-892`.**
`_read_coordination_config()` **re-parses `config.toml` with `tomllib`** and wraps
the whole thing in `except Exception: pass → defaults`:

```python
def _read_coordination_config(self) -> dict:
    cfg = {"bus_capacity": 1024, "bus_overflow": "coalesce",
           "watcher_enabled": False, "watcher_debounce_ms": 200}
    try:
        import tomllib
        ...
        bus = data.get("coordination", {}).get("bus", {})
        watcher = data.get("coordination", {}).get("watcher", {})
        ...
    except Exception:
        pass          # ← invalid config silently becomes defaults
    return cfg
```

Meanwhile Python **already** holds the native validated config at
`self._config = TyConfig.from_json(self._inner.config_json())` (`session.py:742`),
but `TyConfig.from_json` (`src/tyo3/config.py:95`) **parses `code_graph` but not
`coordination`** — there is no `CoordinationConfig` on `TyConfig`. So the
coordination settings are read from the *second*, silently-fallback parser instead
of the *one* validated source. **That is the duplication this phase removes.**

**You build:** a typed `coordination` view on `TyConfig` fed from the native JSON,
and route every coordination consumer through it; then **delete**
`_read_coordination_config`.

## The `_coord_cfg` consumers you must re-route (verify each line first)

`self._coord_cfg = self._read_coordination_config()` is read at exactly these sites
in `session.py` (confirm before editing — line numbers drift):
  - `:760` assignment (replace with the typed native config)
  - `:762` `self._coord_cfg.get("watcher_enabled")` (auto-start watcher)
  - `:912` `self._coord_cfg.get("watcher_debounce_ms", 200) / 1000.0` (debounce)
  - `:961-962` `capacity=self._coord_cfg["bus_capacity"]`, `overflow=self._coord_cfg["bus_overflow"]` (bus construction)

All four must come from `self._config.coordination` after the change.

## What Phase 12 changes — the three steps

- **12.1 — Python consumes the native config; delete the re-read.**
    - `src/tyo3/config.py`: add a `CoordinationConfig` (bus capacity/overflow,
      watcher enabled/debounce — mirror `BusCfg`/`WatcherCfg`) and parse
      `raw["coordination"]` in `TyConfig.from_json` (alongside the existing
      `_code_graph`). Follow the existing `@dataclass(frozen=True)` + `_helper`
      pattern; export it in `__all__`.
    - `src/tyo3/session.py`: **delete `_read_coordination_config`** and re-route the
      four consumers above to `self._config.coordination`.
  *Verify:* `devenv shell -- pytest -q --no-cov -k config`
- **12.2 — Surface all settings natively (mostly confirm).** Confirm `config_json`
  emits `coordination`, `code_graph`, and the hash policy/profiles (it does). Add
  validation **only for any knob not yet validated** (e.g., a nonsensical
  `watcher.debounce_ms` or `bus.queue_capacity` if you decide to bound them) — the
  overflow/precision/refinement arms already exist. **Rebuild if you touch Rust.**
  *Verify:* `devenv shell -- build && devenv shell -- cargo test --manifest-path rust/Cargo.toml config`
- **12.3 — Loud failure at open.** A config with an invalid overflow policy,
  precision value, or refinement value raises `tyo3.exceptions.ConfigError` at
  `TyO3Session(root)` — **a test per knob**. (The cargo side already proves the
  native arms; 12.3 proves the *Python open path* surfaces it, and that the deleted
  fallback no longer hides it.)
  *Verify:* `devenv shell -- pytest -q --no-cov -k "config or sidecar"`

## The decision you must make and state (12.1)

`coordination` as a flat `CoordinationConfig` (e.g. `bus_capacity`, `bus_overflow`,
`watcher_enabled`, `watcher_debounce_ms`) **vs.** nested `bus`/`watcher` sub-configs
mirroring the Rust shape (`coordination.bus.queue_capacity`, …). **Let the consumer
sites and the existing `config.py` conventions drive it** — the four call sites want
`bus_capacity`/`bus_overflow`/`watcher_enabled`/`watcher_debounce_ms`, and the rest
of `config.py` uses flat frozen dataclasses. Pick one, justify it in the PROGRESS
record, and keep the field names stable for the consumers.

## The target tests (this is the acceptance, be precise)

- `src/tyo3/tests/test_gate4_config_sidecar.py` — the config/sidecar contract.
- `src/tyo3/tests/test_config_discovery.py` — config load/discovery.
- `src/tyo3/tests/test_watch.py` — the watcher (it reads `watcher_enabled` /
  `watcher_debounce_ms` — confirm it still auto-starts/debounces after the re-route).
- **Add 12.3 tests** (per-knob loud failure at open). Check whether an existing file
  already has an "invalid config raises ConfigError" home before adding a new one.
- cargo: `cargo test --manifest-path rust/Cargo.toml config` (the Phase 6/9
  accept/reject/defaults cases — keep green).

**Run them first, see which pass, and let them define "done."** If the watcher/bus
behaviour is already covered, 12.1 is a *refactor that must not regress them*; 12.3
adds the missing loud-failure coverage.

## Things to verify before relying on them

- **`config_json` really contains `coordination`.** Open a session and print
  `self._inner.config_json()` (or read it in a scratch test) — confirm
  `raw.coordination.bus.{queue_capacity,overflow}` and `raw.coordination.watcher.
  {enabled,debounce_ms}` are present before you wire `from_json` to them.
- **Whether any *other* Python file re-parses `config.toml`.** Grep
  `tomllib`/`toml.load`/`config.toml` across `src/tyo3` — `sidecar.py` may keep a
  legitimate *path* facade (`config_path()`), which is fine; a second *semantics*
  parser is not. Only the silent-fallback semantics re-read is the target.
- **The watcher debounce units.** `_coord_cfg["watcher_debounce_ms"] / 1000.0` —
  preserve the ms→s conversion when you re-route.

## Working rules (every phase)

- Run EVERYTHING through `devenv shell --` (Nix toolchain). Never bare `pytest`/`cargo`.
- **If you touch Rust, `devenv shell -- build` before any pytest gate.** This phase
  may need no Rust change at all — confirm first.
- Suites are slow (~10–15 min); run the full suite in the background.
- **Fail loudly, never silently default.** After this phase there is **no**
  `try/except → defaults` config path left in Python. Grep for it (Pitfall #1).
- Don't regress the Phase-11 baseline (fully green) or the Phase 6/9 native config
  tests.

## The milestone gate after the phase

```bash
devenv shell -- build   # only if Rust changed
devenv shell -- pytest src/tyo3/tests/test_gate4_config_sidecar.py src/tyo3/tests/test_config_discovery.py src/tyo3/tests/test_watch.py -q --no-cov -rA
devenv shell -- pytest -q --no-cov
devenv shell -- cargo test --manifest-path rust/Cargo.toml config sidecar
```

**Exit criteria:** invalid coordination/precision/hash config raises `ConfigError`
at `TyO3Session(root)`; Python no longer parses `config.toml` for semantics
(`_read_coordination_config` deleted, no surviving `try/except → defaults`); config
+ sidecar + watcher tests green; the full suite stays green; no new xfails/XPASS.

Track progress in `.scratch/projects/16-refined-implementation-work/PROGRESS.md`
(add a `§9.12 — V2 Phase 12` record alongside §9.11). Update the §6.3 defect table
row #9 to DONE. Commit at the end with
`refactor(config): single validated config source; surface precision knobs`
(no AI attribution — house rule).

## Start here

Read the docs above (incl. PROGRESS §9.9/§9.11 and the actual current
`src/tyo3/session.py:742` + `:760-762` + `:865-892` + `:912` + `:961-962`,
`src/tyo3/config.py:83-150` (`TyConfig`/`from_json`/`_code_graph`), and
`rust/src/config.rs` `validate` (~337) + `CoordinationCfg`/`BusCfg`/`WatcherCfg`
(~497-540) + `project.rs::config_json` (~2204)), **read the target test files to see
the API/behaviour they expect**, then give me a short Phase 12 implementation plan:
steps 12.1–12.3 mapped to the real functions/lines you'll touch, your
**coordination-config shape decision** (flat vs nested — grounded in the four
consumer sites and `config.py` conventions), a confirmation that `config_json`
already carries `coordination` (or exactly what Rust change is needed if not), and
the list of `try/except → defaults` / second-parser sites you'll delete. Note which
target test each step is gated by, and whether any Rust change (and therefore a
rebuild) is actually required.
